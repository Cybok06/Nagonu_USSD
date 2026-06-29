from __future__ import annotations

import threading
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from bson import ObjectId

from zico_db import db
from zico_store import is_valid_gh_phone, normalize_phone, to_oid, validate_agent_code
import zico_checkout as checkout
from zico_profit_ledger import apply_profit_split, normalize_profit_line, profit_totals
from zico_wallet_ledger import WALLET_OVERDRAFT_LIMIT_MESSAGE, debit_wallets_for_order
from zico_admin_paystack_ledger import evaluate_admin_wallet_low_balance
from zico_order_sms_notifications import send_mtn_mashup_order_sms


balances_col = db["balances"]
balance_logs_col = db["balance_logs"]
orders_col = db["orders"]
transactions_col = db["transactions"]
services_col = db["services"]
store_accounts_col = db["store_accounts"]


def _money(value: Any, default: float = 0.0) -> float:
    try:
        return round(float(str(value).replace(",", "").strip()), 2)
    except Exception:
        return default


def _service_doc(service_id: Any, admin_id: Any = None) -> Optional[Dict[str, Any]]:
    oid = to_oid(service_id)
    if not oid:
        return None
    query: Dict[str, Any] = {"_id": oid}
    admin_oid = to_oid(admin_id)
    if admin_oid:
        query["$or"] = [{"admin_id": admin_oid}, {"_id": "social_boosting"}]
    return services_col.find_one(
        query,
        {
            "type": 1,
            "provider": 1,
            "network_id": 1,
            "name": 1,
            "network": 1,
            "service_network": 1,
            "offers": 1,
            "store_offers": 1,
            "services_offers": 1,
            "base_service_id": 1,
            "store_offers_profit": 1,
            "default_profit_percent": 1,
            "service_category": 1,
            "status": 1,
            "availability": 1,
            "unit": 1,
            "mtn_normal_use_portal02": 1,
            "mtn_express_use_portal02": 1,
            "agent_visible": 1,
        },
    )


def _matching_offer_amount(svc_doc: Optional[Dict[str, Any]], value_obj: Any, value_raw: Any) -> Optional[float]:
    if not svc_doc:
        return None
    target = checkout._build_bundle_key(value_obj if isinstance(value_obj, dict) else {}, {"value": value_raw})
    for offer in svc_doc.get("offers") or []:
        offer_value = checkout._coerce_value_obj(offer.get("value"))
        key = checkout._build_bundle_key(offer_value if isinstance(offer_value, dict) else {}, {"value": offer.get("value")})
        if target and key and target == key:
            return _money(offer.get("amount"), 0.0)
    return None


def _main_base_amount(svc_doc: Optional[Dict[str, Any]], value_obj: Any, value_raw: Any, fallback: float) -> float:
    base_id = to_oid((svc_doc or {}).get("base_service_id"))
    if not base_id:
        return fallback
    base_doc = services_col.find_one({"_id": base_id}, {"offers": 1})
    found = _matching_offer_amount(base_doc, value_obj, value_raw)
    return _money(found if found is not None else fallback)


def _manual_line(base_line: Dict[str, Any], note: str, api_status: str = "not_applicable_network") -> tuple[Dict[str, Any], None]:
    return {
        **base_line,
        "line_status": "processing",
        "api_status": api_status,
        "api_response": {"note": note},
    }, None


def _build_line_and_job(order_id: str, data: Dict[str, Any], admin_id: ObjectId) -> tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    phone = normalize_phone(data.get("recipient"))
    svc_doc = _service_doc(data.get("service_id"), admin_id)
    svc_name = (svc_doc or {}).get("name") or data.get("service_name") or "Service"
    svc_type = str((svc_doc or {}).get("type") or "").strip().upper()
    svc_provider = str((svc_doc or {}).get("provider") or "").strip().lower()
    value_obj = checkout._coerce_value_obj(data.get("value"))
    selling_amount = _money(data.get("amount"))
    store_owner_base = _money(data.get("base_amount"), selling_amount)
    admin_base = _matching_offer_amount(svc_doc, value_obj, data.get("value"))
    admin_base = _money(admin_base if admin_base is not None else store_owner_base)
    main_base = _main_base_amount(svc_doc, value_obj, data.get("value"), admin_base)
    store_profit_amount = max(0.0, round(selling_amount - store_owner_base, 2))
    profit_amount = max(0.0, round(store_owner_base - admin_base, 2))
    profit_percent_used = round((profit_amount / admin_base) * 100.0, 2) if admin_base > 0 else 0.0
    network_id = checkout._resolve_network_id({"serviceId": data.get("service_id"), "serviceName": svc_name}, value_obj, svc_doc)
    bundle_key = checkout._build_bundle_key(value_obj if isinstance(value_obj, dict) else {}, {"value": data.get("value")})
    amount_key = checkout._normalize_amount_key(selling_amount)
    ported_fields = checkout._extract_ported_fields({"phone": phone, "serviceName": svc_name})

    base_line = {
        "phone": phone,
        "base_amount": admin_base,
        "main_base_amount": main_base,
        "admin_base_amount": admin_base,
        "store_owner_base_amount": store_owner_base,
        "selling_amount": selling_amount,
        "amount": selling_amount,
        "profit_amount": profit_amount,
        "profit_percent_used": profit_percent_used,
        "store_profit_amount": store_profit_amount,
        **ported_fields,
        "value": data.get("offer_text") or data.get("value"),
        "value_obj": value_obj,
        "serviceId": str(data.get("service_id") or ""),
        "serviceName": svc_name,
        "service_type": svc_type or "unknown",
        "network_id": network_id,
        "bundle_key": {"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None,
        "line_amount_key": amount_key,
    }

    if not phone or not is_valid_gh_phone(phone):
        return _manual_line(base_line, "Invalid or missing phone; queued for manual processing.", "skipped_missing_fields")
    if not svc_doc:
        return _manual_line(base_line, "Service not found; queued for manual processing.", "not_applicable")
    unavailable, reason = checkout._service_unavailability_reason(svc_doc)
    if unavailable and svc_type != "OFF":
        return _manual_line(base_line, reason, "service_unavailable")

    svc_name_norm = (svc_name or "").strip().lower()
    is_mtn_normal = svc_name_norm == "mtn normal" or checkout._is_mtn_normal_service(data.get("service_id"), svc_doc)
    is_mtn_express = svc_name_norm == "mtn express"
    api_allowed = svc_type in {"ON", "API"}
    if not api_allowed:
        return _manual_line(
            base_line,
            "API calls disabled for this service (type OFF); queued for manual processing.",
            "not_applicable_type_off",
        )

    resolved_network = checkout._resolve_dataconnect_network(svc_doc, {"serviceName": svc_name}, admin_id=admin_id)
    allowed_mtn_providers = {"portal02", "dataconnect", "codecraft", "datakazina", "skplug"}
    chosen_mtn_normal_provider = None
    chosen_mtn_express_provider = None
    use_portal02 = False
    if is_mtn_normal:
        chosen_mtn_normal_provider = svc_provider if svc_provider in allowed_mtn_providers else ""
        if not chosen_mtn_normal_provider:
            chosen_mtn_normal_provider = "portal02" if bool(svc_doc.get("mtn_normal_use_portal02")) else "dataconnect"
        use_portal02 = chosen_mtn_normal_provider == "portal02"
    if is_mtn_express:
        chosen_mtn_express_provider = svc_provider if svc_provider in allowed_mtn_providers else ""
        if not chosen_mtn_express_provider:
            chosen_mtn_express_provider = "portal02" if bool(svc_doc.get("mtn_express_use_portal02")) else "dataconnect"
        use_portal02 = chosen_mtn_express_provider == "portal02"

    use_codecraft = bool(
        (
            (is_mtn_normal and chosen_mtn_normal_provider == "codecraft")
            or (is_mtn_express and chosen_mtn_express_provider == "codecraft")
            or ((not is_mtn_normal and not is_mtn_express) and svc_provider == "codecraft")
        )
        and not use_portal02
    )
    use_skplug = bool(
        (
            (is_mtn_normal and chosen_mtn_normal_provider == "skplug")
            or (is_mtn_express and chosen_mtn_express_provider == "skplug")
            or ((not is_mtn_normal and not is_mtn_express) and svc_provider == "skplug")
        )
        and not use_portal02
        and not use_codecraft
    )
    use_dataconnect = bool(
        (
            (resolved_network == "mtn" and is_mtn_express and chosen_mtn_express_provider == "dataconnect")
            or (is_mtn_normal and chosen_mtn_normal_provider == "dataconnect")
        )
        and not use_codecraft
        and not use_skplug
    )
    use_datakazina = bool(
        (
            (is_mtn_normal and chosen_mtn_normal_provider == "datakazina")
            or (is_mtn_express and chosen_mtn_express_provider == "datakazina")
            or (resolved_network == "mtn" and svc_provider == "datakazina")
        )
        and not use_skplug
    )

    external_ref = f"{order_id}_1_{uuid.uuid4().hex[:6]}"

    if use_codecraft:
        codecraft_network = checkout._resolve_codecraft_network_name(svc_doc, {"serviceName": svc_name}, admin_id=admin_id)
        volume_mb = None
        if isinstance(value_obj, dict) and value_obj.get("volume") not in (None, "", []):
            try:
                volume_mb = int(float(value_obj.get("volume")))
            except Exception:
                volume_mb = None
        if volume_mb is None:
            gb_fallback = checkout._resolve_package_size_gb(value_obj, {"value": data.get("value")})
            volume_mb = int(gb_fallback * 1000) if gb_fallback is not None else None
        provider_gig = max(1, int(volume_mb / 1000)) if volume_mb else None
        regular_map, bigtime_map = checkout._codecraft_get_packages_cached()
        provider_mode = None
        provider_amount = None
        key = (codecraft_network, provider_gig)
        if codecraft_network == "TELECEL":
            if regular_map and key in regular_map:
                provider_mode = "regular"
                provider_amount = regular_map.get(key)
        else:
            if bigtime_map and key in bigtime_map:
                provider_mode = "bigtime"
                provider_amount = bigtime_map.get(key)
            elif regular_map and key in regular_map:
                provider_mode = "regular"
                provider_amount = regular_map.get(key)
        if not codecraft_network or not provider_gig or not provider_mode:
            return _manual_line(base_line, "Package not found in CodeCraft; queued for manual processing.", "skipped_package_not_found")
        line = {
            **base_line,
            "provider": "codecraft",
            "provider_reference": None,
            "provider_order_id": None,
            "provider_request_order_id": external_ref,
            "provider_mode": provider_mode,
            "provider_network": codecraft_network,
            "provider_gig": provider_gig,
            "provider_package_amount": provider_amount,
            "line_status": "processing",
            "api_status": "queued",
            "api_response": {"note": "Queued for background API call"},
        }
        job = {
            "provider_request_order_id": external_ref,
            "phone": phone,
            "provider": "codecraft",
            "provider_network": codecraft_network,
            "provider_gig": provider_gig,
            "provider_mode": provider_mode,
            "provider_amount": provider_amount,
            "service_id": svc_doc["_id"],
            "line_index": 1,
        }
        return line, job

    if use_skplug:
        provider_gig = checkout._resolve_package_size_gb(value_obj, {"value": data.get("value")})
        skplug_network = checkout._resolve_skplug_network_name(svc_doc, {"serviceName": svc_name}, admin_id=admin_id)
        if not provider_gig or not skplug_network:
            return _manual_line(base_line, "SKPlug API fields missing; queued for processing.", "skipped_missing_fields")
        line = {
            **base_line,
            "provider": "skplug",
            "provider_reference": None,
            "provider_order_id": None,
            "provider_request_order_id": external_ref,
            "provider_network": skplug_network,
            "provider_gig": provider_gig,
            "line_status": "processing",
            "api_status": "queued",
            "api_response": {"note": "Queued for background API call"},
        }
        job = {
            "provider_request_order_id": external_ref,
            "phone": phone,
            "provider": "skplug",
            "provider_network": skplug_network,
            "provider_gig": provider_gig,
            "service_id": svc_doc["_id"],
        }
        return line, job

    if use_datakazina:
        shared_bundle = checkout._resolve_datakazina_shared_bundle(value_obj, {"value": data.get("value")}, svc_doc)
        if shared_bundle is None:
            return _manual_line(base_line, "DataKazina shared_bundle resolution failed; queued for manual processing.", "datakazina_bundle_resolution_failed")
        line = {
            **base_line,
            "provider": "datakazina",
            "provider_reference": None,
            "provider_order_id": None,
            "provider_request_order_id": external_ref,
            "shared_bundle": shared_bundle,
            "line_status": "processing",
            "api_status": "queued",
            "api_response": {"note": "Queued for background API call"},
        }
        job = {
            "provider_request_order_id": external_ref,
            "phone": phone,
            "provider": "datakazina",
            "shared_bundle": shared_bundle,
            "incoming_api_ref": external_ref,
            "network_id": 3,
            "service_id": svc_doc["_id"],
        }
        return line, job

    if use_dataconnect:
        package_size_gb = checkout._resolve_package_size_gb(value_obj, {"value": data.get("value")})
        shared_bundle = None
        if isinstance(value_obj, dict) and value_obj.get("volume") not in (None, "", []):
            shared_bundle = int(float(value_obj.get("volume")))
        if shared_bundle is None and package_size_gb is not None:
            shared_bundle = int(package_size_gb * 1000)
        if package_size_gb is None:
            return _manual_line(base_line, "API fields missing; queued for processing.", "skipped_missing_fields")
        line = {
            **base_line,
            "provider": "dataconnect",
            "provider_reference": None,
            "provider_order_id": None,
            "provider_request_order_id": external_ref,
            "shared_bundle": shared_bundle,
            "line_status": "processing",
            "api_status": "queued",
            "api_response": {"note": "Queued for background API call"},
        }
        job = {
            "provider_request_order_id": external_ref,
            "phone": phone,
            "provider": "dataconnect",
            "service_id": svc_doc["_id"],
            "network_id": network_id,
            "shared_bundle": shared_bundle,
        }
        return line, job

    return _manual_line(base_line, "Not API eligible for this provider; queued for manual processing.", "not_applicable_network")


def create_zico_ussd_order(data: Dict[str, Any], session_id: str, dial_phone: str) -> Dict[str, Any]:
    loaded = validate_agent_code(data.get("agent_code") or "")
    if not loaded:
        return {"success": False, "message": "Invalid agent code."}
    store = loaded["store"]
    admin_id = to_oid(data.get("admin_id")) or to_oid(loaded.get("admin_id")) or to_oid(store.get("admin_id"))
    if not admin_id:
        return {"success": False, "message": "Store admin not found."}
    recipient = normalize_phone(data.get("recipient"))
    if not is_valid_gh_phone(recipient):
        return {"success": False, "message": "Invalid recipient phone number."}

    existing = orders_col.find_one({"ussd.session_id": session_id}, {"order_id": 1, "status": 1})
    if existing:
        return {"success": True, "order_id": existing.get("order_id"), "status": existing.get("status"), "idempotent": True}

    order_id = checkout.generate_order_id()
    line, job = _build_line_and_job(order_id, {**data, "recipient": recipient}, admin_id)
    finalized_line = apply_profit_split(
        normalize_profit_line(
            line,
            selling_amount=line.get("selling_amount") or line.get("amount"),
            main_base_amount=line.get("main_base_amount"),
            admin_base_amount=line.get("admin_base_amount"),
            store_owner_base_amount=line.get("store_owner_base_amount"),
            store_profit_amount=line.get("store_profit_amount"),
        )
    )
    items = [finalized_line]
    totals = profit_totals(items)
    amount = _money(data.get("amount"))
    admin_wallet_debit_total = round(sum(_money(it.get("admin_base_amount")) for it in items if _money(it.get("amount")) > 0), 2)
    store_profit_total = round(sum(_money(it.get("store_profit_amount")) for it in items), 2)
    debit_ok, debit_message, debit_rows = debit_wallets_for_order(
        balances_col=balances_col,
        balance_logs_col=balance_logs_col,
        transactions_col=transactions_col,
        debits=[{"user_id": admin_id, "amount": admin_wallet_debit_total, "label": "admin_base_debit"}],
        order_id=order_id,
        admin_id=admin_id,
        source="store_checkout",
        note="Store order wallet debit",
        meta={
            "store_slug": store.get("slug"),
            "admin_wallet_debit_total": admin_wallet_debit_total,
            "agent_wallet_debit_total": 0.0,
            "store_profit_total": store_profit_total,
            "customer_charge_total": amount,
            "allow_negative_wallet": True,
            "ussd_checkout": True,
        },
        allow_negative=True,
    )
    if not debit_ok:
        message = debit_message if debit_message == WALLET_OVERDRAFT_LIMIT_MESSAGE else f"Order debit failed: {debit_message}"
        return {"success": False, "message": message}

    try:
        evaluate_admin_wallet_low_balance(admin_id, send_alert=True, run_auto_credit=True)
    except Exception:
        pass

    created_now = datetime.utcnow()
    order_doc = {
        "user_id": to_oid(data.get("agent_user_id")) or store.get("owner_id"),
        "admin_id": admin_id,
        "store_slug": store.get("slug") or data.get("store_slug"),
        "store_owner_id": store.get("owner_id"),
        "order_id": order_id,
        "items": items,
        "total_amount": amount,
        "charged_amount": amount,
        "admin_wallet_debit_total": admin_wallet_debit_total,
        "agent_wallet_debit_total": 0.0,
        "wallet_debit_status": "completed",
        "wallet_debits": debit_rows,
        "profit_amount_total": round(totals["profit_amount_total"], 2),
        "main_admin_profit_total": totals["main_admin_profit_total"],
        "admin_profit_total": totals["admin_profit_total"],
        "store_profit_total": store_profit_total,
        "status": "processing",
        "paid_from": "ussd",
        "payment_provider": "ussd",
        "payment_reference": "",
        "payment_gateway": "USSD",
        "payment_status": "success",
        "payment_verified_at": created_now,
        "payment_raw": {},
        "paystack_reference": "",
        "paystack_charged_amount": amount,
        "paystack_fee_amount": 0.0,
        "payer_phone": normalize_phone(dial_phone),
        "created_at": created_now,
        "updated_at": created_now,
        "ussd": {
            "session_id": session_id,
            "dial_phone": normalize_phone(dial_phone),
            "agent_code": data.get("agent_code"),
            "pending_order_id": data.get("pending_order_id"),
        },
        "debug": {
            "store_checkout": True,
            "ussd_checkout": True,
            "events": [],
            "paystack_paid_ghs": amount,
            "paystack_expected_ghs": amount,
            "paystack_fee_ghs": 0.0,
            "gateway_fee_overage_ghs": 0.0,
            "skipped_count": 0,
        },
    }
    orders_col.insert_one(order_doc)

    try:
        transactions_col.insert_one(
            {
                "user_id": order_doc.get("user_id"),
                "admin_id": admin_id,
                "amount": amount,
                "reference": order_id,
                "status": "success",
                "type": "purchase",
                "source": "store_order",
                "gateway": "USSD",
                "currency": "GHS",
                "created_at": created_now,
                "verified_at": created_now,
                "meta": {
                    "store_checkout": True,
                    "ussd_checkout": True,
                    "store_slug": order_doc.get("store_slug"),
                    "store_owner_id": store.get("owner_id"),
                    "paid_from": "ussd",
                    "charged_amount": amount,
                    "requested_amount": amount,
                    "admin_wallet_debit_total": admin_wallet_debit_total,
                    "agent_wallet_debit_total": 0.0,
                    "profit_amount_total": order_doc.get("profit_amount_total"),
                    "main_admin_profit_total": order_doc.get("main_admin_profit_total"),
                    "admin_profit_total": order_doc.get("admin_profit_total"),
                    "store_profit_total": store_profit_total,
                    "providers_used": [line.get("provider")] if line.get("provider") else [],
                    "provider_request_ids": [line.get("provider_request_order_id")] if line.get("provider_request_order_id") else [],
                },
            }
        )
    except Exception:
        pass

    if store_profit_total > 0:
        store_accounts_col.update_one(
            {"store_slug": order_doc["store_slug"]},
            {
                "$inc": {"total_profit_balance": store_profit_total},
                "$set": {"last_updated_profit": store_profit_total, "updated_at": datetime.utcnow()},
                "$setOnInsert": {"store_slug": order_doc["store_slug"], "admin_id": admin_id, "created_at": datetime.utcnow()},
            },
            upsert=True,
        )

    try:
        send_mtn_mashup_order_sms(order_doc)
    except Exception:
        pass

    if job:
        threading.Thread(target=checkout._background_process_providers, args=(order_id, [job]), daemon=True).start()

    return {
        "success": True,
        "order_id": order_id,
        "status": "processing",
        "charged_amount": amount,
        "admin_wallet_debit_total": admin_wallet_debit_total,
        "store_profit_total": store_profit_total,
        "items": items,
    }
