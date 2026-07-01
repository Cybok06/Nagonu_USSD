from __future__ import annotations

import threading
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from bson import ObjectId

from nagonu_db import db
from nagonu_store import is_valid_gh_phone, normalize_phone, to_oid, validate_agent_code
import nagonu_checkout as checkout


orders_col = db["orders"]
services_col = db["services"]
store_accounts_col = db["store_accounts"]


def _money(value: Any, default: float = 0.0) -> float:
    try:
        return round(float(str(value).replace(",", "").strip()), 2)
    except Exception:
        return default


def _service_doc(service_id: Any) -> Optional[Dict[str, Any]]:
    oid = to_oid(service_id)
    if not oid:
        return None
    return services_col.find_one(
        {"_id": oid},
        {
            "type": 1,
            "provider": 1,
            "network_id": 1,
            "name": 1,
            "network": 1,
            "service_network": 1,
            "offers": 1,
            "store_offers": 1,
            "store_offers_profit": 1,
            "default_profit_percent": 1,
            "service_category": 1,
            "status": 1,
            "availability": 1,
            "unit": 1,
            "mtn_normal_use_portal02": 1,
            "mtn_express_use_portal02": 1,
        },
    )


def _system_base_for_value(svc_doc: Optional[Dict[str, Any]], value_obj: Any, fallback: Any) -> float:
    fallback_amount = _money(fallback, 0.0)
    if not svc_doc:
        return fallback_amount

    target = checkout._build_bundle_key(value_obj if isinstance(value_obj, dict) else {}, {"value": value_obj})
    best = None
    for offer in svc_doc.get("offers") or []:
        offer_value = checkout._coerce_value_obj(offer.get("value"))
        key = checkout._build_bundle_key(offer_value if isinstance(offer_value, dict) else {}, {"value": offer.get("value")})
        if target and key and target == key:
            best = offer
            break
    if not best:
        return fallback_amount
    base = checkout._money(best.get("amount"))
    return round(float(base or fallback_amount), 2)


def _line_for_manual(item: Dict[str, Any], note: str, api_status: str = "not_applicable_network") -> Dict[str, Any]:
    return {
        **item,
        "line_status": "processing",
        "api_status": api_status,
        "api_response": {"note": note},
    }


def _build_line_and_job(order_id: str, data: Dict[str, Any]) -> tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    phone = normalize_phone(data.get("recipient"))
    service_id = data.get("service_id")
    svc_doc = _service_doc(service_id)
    svc_name = (svc_doc or {}).get("name") or data.get("service_name") or "Service"
    svc_type = str((svc_doc or {}).get("type") or "").strip().upper()
    svc_provider = str((svc_doc or {}).get("provider") or "").strip().lower()
    value_obj = checkout._coerce_value_obj(data.get("value"))
    amount = _money(data.get("amount"))
    system_base = _system_base_for_value(svc_doc, value_obj, data.get("base_amount"))
    store_profit_amount = max(0.0, round(amount - system_base, 2))
    profit_percent_used = round((store_profit_amount / system_base * 100), 2) if system_base > 0 else 0.0
    network_id = checkout._resolve_network_id({"serviceId": service_id, "serviceName": svc_name}, value_obj, svc_doc)
    bundle_key = checkout._build_bundle_key(value_obj if isinstance(value_obj, dict) else {}, {"value": data.get("value")})
    amount_key = checkout._normalize_amount_key(amount)
    ported_fields = checkout._extract_ported_fields({"phone": phone, "serviceName": svc_name})

    base_line = {
        "phone": phone,
        "base_amount": system_base,
        "amount": amount,
        "profit_amount": 0.0,
        "profit_percent_used": profit_percent_used,
        "store_profit_amount": store_profit_amount,
        **ported_fields,
        "value": data.get("offer_text") or data.get("value"),
        "value_obj": value_obj,
        "serviceId": str(service_id or ""),
        "serviceName": svc_name,
        "service_type": svc_type or "unknown",
        "network_id": network_id,
        "bundle_key": {"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None,
        "line_amount_key": amount_key,
    }

    if not phone or not is_valid_gh_phone(phone):
        return _line_for_manual(base_line, "Invalid or missing phone; queued for manual processing.", "skipped_missing_fields"), None
    if not svc_doc:
        return _line_for_manual(base_line, "Service not found; queued for manual processing.", "not_applicable"), None

    is_unavailable, reason = checkout._service_unavailability_reason(svc_doc)
    if is_unavailable and svc_type != "OFF":
        return _line_for_manual(base_line, reason, "service_unavailable"), None

    is_mtn_normal = ((svc_name or "").strip().lower() == "mtn normal") or checkout._is_mtn_normal_service(service_id, svc_doc)
    is_mtn_express = (svc_name or "").strip().lower() == "mtn express"
    api_allowed = svc_type in {"ON", "API"}

    if not api_allowed:
        return _line_for_manual(
            base_line,
            "API calls disabled for this service (type OFF); queued for manual processing.",
            "not_applicable_type_off",
        ), None

    resolved_network = checkout._resolve_dataconnect_network(svc_doc, {"serviceName": svc_name})
    chosen_mtn_normal_provider = None
    chosen_mtn_express_provider = None
    use_portal02 = False
    if is_mtn_normal:
        chosen_mtn_normal_provider = svc_provider if svc_provider in checkout.SERVICE_PROVIDER_SET else "portal02"
        use_portal02 = chosen_mtn_normal_provider == "portal02"
    if is_mtn_express:
        chosen_mtn_express_provider = svc_provider if svc_provider in checkout.SERVICE_PROVIDER_SET else "dataconnect"
        use_portal02 = chosen_mtn_express_provider == "portal02"

    use_codecraft = bool(
        (is_mtn_normal and chosen_mtn_normal_provider == "codecraft")
        or (is_mtn_express and chosen_mtn_express_provider == "codecraft")
        or ((not is_mtn_normal and not is_mtn_express) and svc_provider == "codecraft")
    )
    use_dataconnect = bool(
        (
            (resolved_network == "mtn" and is_mtn_express and chosen_mtn_express_provider == "dataconnect")
            or (is_mtn_normal and chosen_mtn_normal_provider == "dataconnect")
        )
        and not use_codecraft
    )
    use_datakazina = bool(
        (
            (resolved_network == "mtn" and is_mtn_express and chosen_mtn_express_provider == "datakazina")
            or (is_mtn_normal and chosen_mtn_normal_provider == "datakazina")
        )
        and not use_codecraft
        and not use_portal02
    )
    use_skplug = bool(
        (
            (resolved_network == "mtn" and is_mtn_express and chosen_mtn_express_provider == "skplug")
            or (is_mtn_normal and chosen_mtn_normal_provider == "skplug")
        )
        and not use_codecraft
        and not use_portal02
    )

    external_ref = f"{order_id}_1_{uuid.uuid4().hex[:6]}"

    if use_codecraft:
        codecraft_network = checkout._resolve_codecraft_network_name(svc_doc, {"serviceName": svc_name})
        volume_mb = None
        if isinstance(value_obj, dict) and value_obj.get("volume") not in (None, "", []):
            try:
                volume_mb = int(float(value_obj.get("volume")))
            except Exception:
                volume_mb = None
        if volume_mb is None:
            gb = checkout._resolve_package_size_gb(value_obj, {"value": data.get("value")})
            volume_mb = int(gb * 1000) if gb is not None else None
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
            return _line_for_manual(
                base_line,
                "Package not found in CodeCraft; queued for manual processing.",
                "skipped_package_not_found",
            ), None
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
            "line_status": "pending",
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

    if use_dataconnect:
        package_size_gb = checkout._resolve_package_size_gb(value_obj, {"value": data.get("value")})
        shared_bundle = None
        if isinstance(value_obj, dict) and value_obj.get("volume") not in (None, "", []):
            shared_bundle = int(float(value_obj.get("volume")))
        if shared_bundle is None and package_size_gb is not None:
            shared_bundle = int(package_size_gb * 1000)
        if package_size_gb is None:
            return _line_for_manual(base_line, "API fields missing; queued for processing.", "skipped_missing_fields"), None
        line = {
            **base_line,
            "provider": "dataconnect",
            "provider_reference": None,
            "provider_order_id": None,
            "provider_request_order_id": external_ref,
            "shared_bundle": shared_bundle,
            "line_status": "pending",
            "api_status": "queued",
            "api_response": {"note": "Queued for background API call"},
        }
        job = {
            "provider_request_order_id": external_ref,
            "phone": phone,
            "provider": "dataconnect",
            "service_id": svc_doc["_id"],
            "line_index": 1,
            "network_id": network_id,
            "shared_bundle": shared_bundle,
        }
        return line, job

    if use_datakazina:
        shared_bundle = checkout._resolve_datakazina_shared_bundle(value_obj, {"value": data.get("value")})
        if shared_bundle is None:
            return _line_for_manual(base_line, "DataKazina shared_bundle resolution failed; queued for manual processing.", "datakazina_bundle_resolution_failed"), None
        line = {
            **base_line,
            "provider": "datakazina",
            "provider_reference": None,
            "provider_order_id": None,
            "provider_request_order_id": external_ref,
            "shared_bundle": shared_bundle,
            "line_status": "pending",
            "api_status": "queued",
            "api_response": {"note": "Queued for background API call"},
        }
        job = {
            "provider_request_order_id": external_ref,
            "incoming_api_ref": external_ref,
            "phone": phone,
            "provider": "datakazina",
            "shared_bundle": shared_bundle,
            "network_id": 3,
            "service_id": svc_doc["_id"],
            "line_index": 1,
        }
        return line, job

    if use_skplug:
        gb_size = checkout._resolve_skplug_gb_size(value_obj, {"value": data.get("value")})
        if gb_size is None:
            return _line_for_manual(base_line, "API fields missing; queued for processing.", "skipped_missing_fields"), None
        line = {
            **base_line,
            "provider": "skplug",
            "provider_reference": None,
            "provider_order_id": None,
            "provider_request_order_id": external_ref,
            "provider_network": "MTN",
            "provider_gb_size": gb_size,
            "line_status": "pending",
            "api_status": "queued",
            "api_response": {"note": "Queued for background API call"},
        }
        job = {
            "provider_request_order_id": external_ref,
            "phone": phone,
            "provider": "skplug",
            "skplug_network": "MTN",
            "skplug_gb_size": gb_size,
            "service_id": svc_doc["_id"],
            "line_index": 1,
        }
        return line, job

    return _line_for_manual(
        base_line,
        "API is not configured for this service/provider; queued for manual processing.",
        "not_applicable_network",
    ), None


def create_nagonu_ussd_order(data: Dict[str, Any], session_id: str, dial_phone: str) -> Dict[str, Any]:
    loaded = validate_agent_code(data.get("agent_code") or "")
    if not loaded:
        return {"success": False, "message": "Invalid agent code."}
    store = loaded["store"]
    recipient = normalize_phone(data.get("recipient"))
    if not is_valid_gh_phone(recipient):
        return {"success": False, "message": "Invalid recipient phone number."}

    existing = orders_col.find_one(
        {"ussd.session_id": session_id},
        {"order_id": 1, "status": 1, "total_amount": 1, "charged_amount": 1, "payment_status": 1, "paystack_reference": 1},
    )
    if existing:
        return {
            "success": True,
            "order_id": existing.get("order_id"),
            "status": existing.get("status"),
            "charged_amount": _money(existing.get("charged_amount") or existing.get("total_amount")),
            "payment_status": existing.get("payment_status"),
            "paystack_reference": existing.get("paystack_reference"),
            "idempotent": True,
        }

    order_id = checkout.generate_order_id()
    line, job = _build_line_and_job(order_id, {**data, "recipient": recipient})
    created_now = datetime.utcnow()
    amount = _money(data.get("amount"))
    paystack_fee = round(amount * 0.02, 2)
    charged_amount = round(amount + paystack_fee, 2)
    store_profit_total = _money(line.get("store_profit_amount"))
    order_doc = {
        "user_id": to_oid(data.get("agent_user_id")) or store.get("owner_id"),
        "store_slug": store.get("slug") or data.get("store_slug"),
        "order_id": order_id,
        "items": [line],
        "total_amount": amount,
        "charged_amount": charged_amount,
        "profit_amount_total": _money(line.get("profit_amount")),
        "status": "awaiting_payment",
        "paid_from": "ussd",
        "payment_provider": "paystack",
        "payment_reference": "",
        "payment_gateway": "Paystack",
        "payment_status": "pending",
        "payment_channel": "mobile_money",
        "paystack_reference": "",
        "paystack_charged_amount": charged_amount,
        "paystack_fee_amount": paystack_fee,
        "created_at": created_now,
        "updated_at": created_now,
        "ussd": {
            "session_id": session_id,
            "dial_phone": normalize_phone(dial_phone),
            "agent_code": data.get("agent_code"),
            "pending_order_id": data.get("pending_order_id"),
            "provider_jobs": [job] if job else [],
        },
        "debug": {
            "store_checkout": True,
            "ussd_checkout": True,
            "events": [],
            "paystack_paid_ghs": 0.0,
            "paystack_expected_ghs": charged_amount,
            "paystack_base_ghs": amount,
            "paystack_fee_ghs": paystack_fee,
            "gateway_fee_overage_ghs": paystack_fee,
            "skipped_count": 0,
        },
    }
    orders_col.insert_one(order_doc)

    return {
        "success": True,
        "order_id": order_id,
        "status": "awaiting_payment",
        "charged_amount": charged_amount,
        "base_amount": amount,
        "gateway_fee": paystack_fee,
        "items": [line],
    }


def release_nagonu_ussd_order(order_id: str, payment: Dict[str, Any], paystack_data: Dict[str, Any] | None = None) -> Dict[str, Any]:
    if not order_id:
        return {"released": False, "reason": "missing_order_id"}

    order = orders_col.find_one({"order_id": order_id})
    if not order:
        return {"released": False, "reason": "order_not_found"}

    now = datetime.utcnow()
    reference = payment.get("paystack_reference") or payment.get("payment_reference") or (paystack_data or {}).get("reference") or ""
    paid_amount = _money(payment.get("amount") or order.get("charged_amount"))
    base_amount = _money(payment.get("base_amount") or order.get("total_amount"))
    gateway_fee = _money(payment.get("gateway_fee") or max(0.0, paid_amount - base_amount))

    orders_col.update_one(
        {"order_id": order_id},
        {
            "$set": {
                "status": "processing",
                "payment_status": "success",
                "payment_reference": reference,
                "paystack_reference": reference,
                "payment_verified_at": now,
                "payment_raw": paystack_data or {},
                "paystack_charged_amount": paid_amount,
                "paystack_fee_amount": gateway_fee,
                "charged_amount": paid_amount,
                "debug.paystack_paid_ghs": paid_amount,
                "debug.paystack_expected_ghs": paid_amount,
                "debug.paystack_base_ghs": base_amount,
                "debug.paystack_fee_ghs": gateway_fee,
                "updated_at": now,
            }
        },
    )

    store_slug = order.get("store_slug")
    store_profit_total = _money(sum(_money(item.get("store_profit_amount")) for item in (order.get("items") or [])))
    credit_claim = orders_col.update_one(
        {"order_id": order_id, "store_profit_credited_at": {"$exists": False}},
        {"$set": {"store_profit_credited_at": now, "updated_at": now}},
    )
    if credit_claim.modified_count and store_slug and store_profit_total > 0:
        store_accounts_col.update_one(
            {"store_slug": store_slug},
            {
                "$inc": {"total_profit_balance": store_profit_total},
                "$set": {"last_updated_profit": store_profit_total, "updated_at": now},
                "$setOnInsert": {"store_slug": store_slug, "created_at": now},
            },
            upsert=True,
        )

    release_claim = orders_col.update_one(
        {"order_id": order_id, "ussd.provider_released_at": {"$exists": False}},
        {"$set": {"ussd.provider_released_at": now, "updated_at": now}},
    )
    if not release_claim.modified_count:
        return {"released": False, "reason": "already_released", "order_id": order_id}

    items = order.get("items") or []
    try:
        checkout._send_mashup_order_sms_async(order_id, order.get("created_at") or now, items)
    except Exception:
        pass

    jobs = ((order.get("ussd") or {}).get("provider_jobs") or [])
    if jobs:
        threading.Thread(target=checkout._background_process_providers, args=(order_id, jobs), daemon=True).start()

    return {"released": True, "order_id": order_id, "jobs": len(jobs)}
