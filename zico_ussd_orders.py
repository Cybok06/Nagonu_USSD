from __future__ import annotations

import re
import threading
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from bson import ObjectId

from zico_db import db
from zico_store import is_valid_gh_phone, normalize_phone, to_oid, validate_agent_code
import zico_checkout as checkout
from zico_profit_ledger import apply_profit_split, normalize_profit_line, profit_totals
from zico_results_checker import build_results_checker_stage, fulfill_results_checker_sale, is_results_checker_service_id
from zico_wallet_ledger import WALLET_OVERDRAFT_LIMIT_MESSAGE, debit_wallets_for_order
from zico_admin_paystack_ledger import evaluate_admin_wallet_low_balance
from zico_order_sms_notifications import send_mtn_mashup_order_sms


balances_col = db["balances"]
balance_logs_col = db["balance_logs"]
orders_col = db["orders"]
transactions_col = db["transactions"]
services_col = db["services"]
store_accounts_col = db["store_accounts"]
pending_orders_col = db["ussd_pending_orders"]


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
    allowed_mtn_providers = {"portal02", "dataconnect", "codecraft", "datakazina", "skplug", "bundleportal"}
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
    use_bundleportal = bool(
        (
            (is_mtn_normal and chosen_mtn_normal_provider == "bundleportal")
            or (is_mtn_express and chosen_mtn_express_provider == "bundleportal")
            or ((not is_mtn_normal and not is_mtn_express) and svc_provider == "bundleportal")
        )
        and not use_portal02
        and not use_codecraft
    )
    use_skplug = bool(
        (
            (is_mtn_normal and chosen_mtn_normal_provider == "skplug")
            or (is_mtn_express and chosen_mtn_express_provider == "skplug")
            or ((not is_mtn_normal and not is_mtn_express) and svc_provider == "skplug")
        )
        and not use_portal02
        and not use_codecraft
        and not use_bundleportal
    )
    use_dataconnect = bool(
        (
            (resolved_network == "mtn" and is_mtn_express and chosen_mtn_express_provider == "dataconnect")
            or (is_mtn_normal and chosen_mtn_normal_provider == "dataconnect")
        )
        and not use_codecraft
        and not use_bundleportal
        and not use_skplug
    )
    use_datakazina = bool(
        (
            (is_mtn_normal and chosen_mtn_normal_provider == "datakazina")
            or (is_mtn_express and chosen_mtn_express_provider == "datakazina")
            or (resolved_network == "mtn" and svc_provider == "datakazina")
        )
        and not use_bundleportal
        and not use_skplug
    )

    external_ref = f"{order_id}_1_{uuid.uuid4().hex[:6]}"

    if use_bundleportal:
        provider_gig = checkout._resolve_package_size_gb(value_obj, {"value": data.get("value")})
        portal_network = checkout._resolve_bundleportal_network_name(
            svc_doc,
            {"serviceName": svc_name},
            admin_id=admin_id,
        )
        normalized_phone = checkout._normalize_bundleportal_phone(phone)
        if not re.fullmatch(r"0\d{9}", normalized_phone) or not provider_gig or not portal_network:
            return _manual_line(base_line, "BundlePortal fields missing; queued for manual processing.", "skipped_missing_fields")
        line = {
            **base_line,
            "phone": normalized_phone,
            "provider": "bundleportal",
            "provider_reference": None,
            "provider_order_id": None,
            "provider_request_order_id": external_ref,
            "provider_network": portal_network,
            "provider_gig": provider_gig,
            "line_status": "processing",
            "api_status": "queued",
            "api_response": {"note": "Queued for BundlePortal API call"},
        }
        job = {
            "provider_request_order_id": external_ref,
            "phone": normalized_phone,
            "provider": "bundleportal",
            "provider_network": portal_network,
            "provider_gig": provider_gig,
            "service_id": svc_doc["_id"],
            "line_index": 1,
        }
        return line, job

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
    recipient = normalize_phone(data.get("recipient"))
    if not is_valid_gh_phone(recipient):
        return {"success": False, "message": "Invalid recipient phone number."}
    order_id = checkout.generate_order_id()
    store = loaded["store"]
    admin_id = to_oid(data.get("admin_id")) or to_oid(loaded.get("admin_id")) or to_oid(store.get("admin_id"))
    if not admin_id:
        return {"success": False, "message": "Store admin not found."}
    if is_results_checker_service_id(data.get("service_id")):
        staged_result = build_results_checker_stage({**data, "recipient": recipient, "dial_phone": dial_phone}, order_id)
        if not staged_result.get("success"):
            return staged_result
        staged_order = {
            "user_id": to_oid(data.get("agent_user_id")) or store.get("owner_id"),
            "admin_id": admin_id,
            "store_slug": store.get("slug") or data.get("store_slug"),
            "store_owner_id": store.get("owner_id"),
            "order_id": order_id,
            "items": staged_result.get("items") or [],
            "release_items": staged_result.get("items") or [],
            "provider_jobs": [],
            "total_amount": staged_result.get("base_amount"),
            "charged_amount": staged_result.get("charged_amount"),
            "gateway_fee": staged_result.get("gateway_fee"),
            "session_id": session_id,
            "dial_phone": normalize_phone(dial_phone),
            "buyer_phone": recipient,
            "agent_code": data.get("agent_code"),
            "agent_user_id": str(data.get("agent_user_id") or ""),
            "pending_order_id": data.get("pending_order_id"),
            "product_kind": "results_checker",
            "checker_type": staged_result.get("checker_type"),
        }
    else:
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
        staged_line = dict(finalized_line)
        staged_line["line_status"] = "awaiting_payment"
        staged_line["api_status"] = "awaiting_payment"
        staged_line["api_response"] = {"note": "Waiting for Paystack payment confirmation before processing."}
        amount = _money(data.get("amount"))
        gateway_fee = round(amount * 0.02, 2)
        charged_amount = round(amount + gateway_fee, 2)
        staged_order = {
            "user_id": to_oid(data.get("agent_user_id")) or store.get("owner_id"),
            "admin_id": admin_id,
            "store_slug": store.get("slug") or data.get("store_slug"),
            "store_owner_id": store.get("owner_id"),
            "order_id": order_id,
            "items": [staged_line],
            "release_items": [finalized_line],
            "provider_jobs": [job] if job else [],
            "total_amount": amount,
            "charged_amount": charged_amount,
            "gateway_fee": gateway_fee,
            "session_id": session_id,
            "dial_phone": normalize_phone(dial_phone),
            "buyer_phone": recipient,
            "agent_code": data.get("agent_code"),
            "agent_user_id": str(data.get("agent_user_id") or ""),
            "pending_order_id": data.get("pending_order_id"),
        }
    pending_order_id = data.get("pending_order_id")
    if pending_order_id:
        try:
            pending_oid = ObjectId(str(pending_order_id))
            pending_query = {"_id": pending_oid}
        except Exception:
            pending_query = {"id": str(pending_order_id)}
        pending_orders_col.update_one(
            pending_query,
            {
                "$set": {
                    "status": "order_staged",
                    "order_id": order_id,
                    "recipient": recipient,
                    "staged_order": staged_order,
                    "updated_at": datetime.utcnow(),
                }
            },
        )

    return {
        "success": True,
        "order_id": order_id,
        "status": "staged_for_payment",
        "charged_amount": charged_amount,
        "base_amount": amount,
        "gateway_fee": gateway_fee,
        "items": [staged_line],
    }


def release_zico_ussd_order(pending_order_id: str, payment: Dict[str, Any], paystack_data: Dict[str, Any] | None = None) -> Dict[str, Any]:
    if not pending_order_id:
        return {"released": False, "reason": "missing_pending_order_id"}

    try:
        pending_oid = ObjectId(str(pending_order_id))
        pending_query = {"_id": pending_oid}
    except Exception:
        pending_query = {"id": str(pending_order_id)}
    pending = pending_orders_col.find_one(pending_query)
    if not pending:
        return {"released": False, "reason": "pending_order_not_found"}

    staged = pending.get("staged_order") or {}
    order_id = str((pending.get("created_order_id") or staged.get("order_id") or pending.get("order_id") or "").strip())
    if not order_id:
        return {"released": False, "reason": "missing_order_id"}

    existing = orders_col.find_one({"order_id": order_id}) or orders_col.find_one({"ussd.pending_order_id": pending_order_id})
    if existing and existing.get("ussd", {}).get("provider_released_at"):
        return {"released": False, "reason": "already_released", "order_id": order_id}
    if existing:
        return {"released": True, "order_id": str(existing.get("order_id") or order_id), "jobs": len((existing.get("ussd") or {}).get("provider_jobs") or [])}

    admin_id = to_oid(staged.get("admin_id"))
    if not admin_id:
        return {"released": False, "reason": "missing_admin_id"}
    store_slug = staged.get("store_slug")
    store_owner_id = staged.get("store_owner_id")
    items = staged.get("release_items") or []
    if staged.get("product_kind") == "results_checker":
        checker_result = fulfill_results_checker_sale(staged, payment, paystack_data)
        now = datetime.utcnow()
        reference = payment.get("paystack_reference") or payment.get("payment_reference") or (paystack_data or {}).get("reference") or ""
        if checker_result.get("success"):
            items = [checker_result.get("line") or {}]
            totals = profit_totals(items)
            store_profit_total = round(sum(_money(it.get("store_profit_amount")) for it in items), 2)
            order_doc = {
                "user_id": staged.get("user_id"),
                "admin_id": admin_id,
                "store_slug": store_slug,
                "store_owner_id": store_owner_id,
                "order_id": order_id,
                "items": items,
                "total_amount": _money(staged.get("total_amount")),
                "charged_amount": _money(payment.get("amount") or staged.get("charged_amount")),
                "admin_wallet_debit_total": 0.0,
                "agent_wallet_debit_total": 0.0,
                "wallet_debit_status": "completed",
                "wallet_debits": [],
                "profit_amount_total": round(totals["profit_amount_total"], 2),
                "main_admin_profit_total": totals["main_admin_profit_total"],
                "admin_profit_total": totals["admin_profit_total"],
                "store_profit_total": store_profit_total,
                "status": checker_result.get("order_status") or "completed",
                "paid_from": "ussd",
                "payment_provider": "paystack",
                "payment_reference": reference,
                "payment_gateway": "Paystack",
                "payment_status": "success",
                "payment_verified_at": now,
                "payment_raw": paystack_data or {},
                "paystack_reference": reference,
                "paystack_charged_amount": _money(payment.get("amount") or staged.get("charged_amount")),
                "paystack_fee_amount": _money(payment.get("gateway_fee") or staged.get("gateway_fee")),
                "payer_phone": staged.get("dial_phone"),
                "created_at": payment.get("paid_at") or now,
                "updated_at": now,
                "ussd": {
                    "session_id": staged.get("session_id"),
                    "dial_phone": staged.get("dial_phone"),
                    "agent_code": staged.get("agent_code"),
                    "pending_order_id": pending_order_id,
                    "release_items": items,
                    "provider_jobs": [],
                    "provider_released_at": now,
                    "results_checker": {
                        "checker_type": staged.get("checker_type"),
                        "sms_status": checker_result.get("sms_status"),
                        "buyer_phone": staged.get("buyer_phone"),
                    },
                },
                "debug": {
                    "store_checkout": True,
                    "ussd_checkout": True,
                    "events": [],
                    "paystack_paid_ghs": _money(payment.get("amount") or staged.get("charged_amount")),
                    "paystack_expected_ghs": _money(payment.get("amount") or staged.get("charged_amount")),
                    "paystack_fee_ghs": _money(payment.get("gateway_fee") or staged.get("gateway_fee")),
                    "gateway_fee_overage_ghs": _money(payment.get("gateway_fee") or staged.get("gateway_fee")),
                    "skipped_count": 0,
                },
            }
            orders_col.insert_one(order_doc)
            purchase_history_col = db["purchase_history"]
            history_doc = checker_result.get("history_doc") or {}
            if history_doc:
                purchase_history_col.update_one(
                    {"checker_id": history_doc.get("checker_id"), "source": "ussd_results_checker", "order_id": order_id},
                    {"$setOnInsert": {**history_doc, "order_id": order_id}},
                    upsert=True,
                )
            if store_profit_total > 0:
                store_accounts_col.update_one(
                    {"store_slug": store_slug},
                    {
                        "$inc": {"total_profit_balance": store_profit_total},
                        "$set": {"last_updated_profit": store_profit_total, "updated_at": now},
                        "$setOnInsert": {"store_slug": store_slug, "admin_id": admin_id, "created_at": now},
                    },
                    upsert=True,
                )
            pending_orders_col.update_one(
                pending_query,
                {"$set": {"status": "released", "created_order_id": order_id, "payment_status": "success", "updated_at": now}},
            )
            return {"released": True, "order_id": order_id, "jobs": 0}
        fallback_item = {
            "phone": staged.get("buyer_phone") or staged.get("dial_phone") or "",
            "value": str(staged.get("checker_type") or "").upper(),
            "value_obj": {"checker_type": staged.get("checker_type")},
            "serviceId": "results_checker_ussd",
            "serviceName": "Results Checker",
            "service_type": "RESULTS_CHECKER",
            "amount": _money(staged.get("total_amount")),
            "base_amount": 0.0,
            "main_base_amount": 0.0,
            "admin_base_amount": 0.0,
            "profit_amount": 0.0,
            "store_profit_amount": 0.0,
            "line_status": "processing",
            "api_status": "inventory_unavailable",
            "api_response": {"note": checker_result.get("message") or "Checker inventory is unavailable after payment."},
        }
        order_doc = {
            "user_id": staged.get("user_id"),
            "admin_id": admin_id,
            "store_slug": store_slug,
            "store_owner_id": store_owner_id,
            "order_id": order_id,
            "items": [fallback_item],
            "total_amount": _money(staged.get("total_amount")),
            "charged_amount": _money(payment.get("amount") or staged.get("charged_amount")),
            "admin_wallet_debit_total": 0.0,
            "agent_wallet_debit_total": 0.0,
            "wallet_debit_status": "completed",
            "wallet_debits": [],
            "profit_amount_total": 0.0,
            "main_admin_profit_total": 0.0,
            "admin_profit_total": 0.0,
            "store_profit_total": 0.0,
            "status": "processing",
            "paid_from": "ussd",
            "payment_provider": "paystack",
            "payment_reference": reference,
            "payment_gateway": "Paystack",
            "payment_status": "success",
            "payment_verified_at": now,
            "payment_raw": paystack_data or {},
            "paystack_reference": reference,
            "paystack_charged_amount": _money(payment.get("amount") or staged.get("charged_amount")),
            "paystack_fee_amount": _money(payment.get("gateway_fee") or staged.get("gateway_fee")),
            "payer_phone": staged.get("dial_phone"),
            "created_at": payment.get("paid_at") or now,
            "updated_at": now,
            "ussd": {
                "session_id": staged.get("session_id"),
                "dial_phone": staged.get("dial_phone"),
                "agent_code": staged.get("agent_code"),
                "pending_order_id": pending_order_id,
                "release_items": [fallback_item],
                "provider_jobs": [],
                "provider_released_at": now,
                "results_checker_error": checker_result.get("reason") or "inventory_unavailable",
            },
        }
        orders_col.insert_one(order_doc)
        pending_orders_col.update_one(
            pending_query,
            {"$set": {"status": "released", "created_order_id": order_id, "payment_status": "success", "updated_at": now}},
        )
        return {"released": True, "order_id": order_id, "jobs": 0}
    totals = profit_totals(items)
    amount = _money(staged.get("total_amount"))
    charged_amount = _money(payment.get("amount") or staged.get("charged_amount"))
    gateway_fee = _money(payment.get("gateway_fee") or staged.get("gateway_fee"))
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
            "store_slug": store_slug,
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
        return {"released": False, "reason": "wallet_debit_failed", "message": message, "order_id": order_id}

    try:
        evaluate_admin_wallet_low_balance(admin_id, send_alert=True, run_auto_credit=True)
    except Exception:
        pass

    now = datetime.utcnow()
    reference = payment.get("paystack_reference") or payment.get("payment_reference") or (paystack_data or {}).get("reference") or ""
    order_doc = {
        "user_id": staged.get("user_id"),
        "admin_id": admin_id,
        "store_slug": store_slug,
        "store_owner_id": store_owner_id,
        "order_id": order_id,
        "items": items,
        "total_amount": amount,
        "charged_amount": charged_amount,
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
        "payment_provider": "paystack",
        "payment_reference": reference,
        "payment_gateway": "Paystack",
        "payment_status": "success",
        "payment_verified_at": now,
        "payment_raw": paystack_data or {},
        "paystack_reference": reference,
        "paystack_charged_amount": charged_amount,
        "paystack_fee_amount": gateway_fee,
        "payer_phone": staged.get("dial_phone"),
        "created_at": payment.get("paid_at") or now,
        "updated_at": now,
        "ussd": {
            "session_id": staged.get("session_id"),
            "dial_phone": staged.get("dial_phone"),
            "agent_code": staged.get("agent_code"),
            "pending_order_id": pending_order_id,
            "release_items": items,
            "provider_jobs": staged.get("provider_jobs") or [],
            "provider_released_at": now,
        },
        "debug": {
            "store_checkout": True,
            "ussd_checkout": True,
            "events": [],
            "paystack_paid_ghs": charged_amount,
            "paystack_expected_ghs": charged_amount,
            "paystack_fee_ghs": gateway_fee,
            "gateway_fee_overage_ghs": gateway_fee,
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
                "gateway": "Paystack",
                "currency": "GHS",
                "created_at": now,
                "verified_at": now,
                "payment_provider": "paystack",
                "payment_reference": reference,
                "payment_gateway": "Paystack",
                "payment_status": "success",
                "payment_verified_at": now,
                "payment_raw": paystack_data or {},
                "meta": {
                    "store_checkout": True,
                    "ussd_checkout": True,
                    "store_slug": store_slug,
                    "store_owner_id": store_owner_id,
                    "paid_from": "ussd",
                    "charged_amount": charged_amount,
                    "requested_amount": amount,
                    "admin_wallet_debit_total": admin_wallet_debit_total,
                    "agent_wallet_debit_total": 0.0,
                    "profit_amount_total": order_doc.get("profit_amount_total"),
                    "main_admin_profit_total": order_doc.get("main_admin_profit_total"),
                    "admin_profit_total": order_doc.get("admin_profit_total"),
                    "store_profit_total": store_profit_total,
                    "providers_used": [it.get("provider") for it in items if it.get("provider")],
                    "provider_request_ids": [it.get("provider_request_order_id") for it in items if it.get("provider_request_order_id")],
                },
            }
        )
    except Exception:
        pass

    if store_profit_total > 0:
        store_accounts_col.update_one(
            {"store_slug": store_slug},
            {
                "$inc": {"total_profit_balance": store_profit_total},
                "$set": {"last_updated_profit": store_profit_total, "updated_at": now},
                "$setOnInsert": {"store_slug": store_slug, "admin_id": admin_id, "created_at": now},
            },
            upsert=True,
        )

    try:
        send_mtn_mashup_order_sms(order_doc)
    except Exception:
        pass

    jobs = staged.get("provider_jobs") or []
    if jobs:
        threading.Thread(target=checkout._background_process_providers, args=(order_id, jobs), daemon=True).start()

    pending_orders_col.update_one(
        pending_query,
        {
            "$set": {
                "status": "released",
                "created_order_id": order_id,
                "payment_status": "success",
                "updated_at": now,
            }
        },
    )
    return {"released": True, "order_id": order_id, "jobs": len(jobs)}
