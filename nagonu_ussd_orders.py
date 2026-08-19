from __future__ import annotations

import threading
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from bson import ObjectId

from nagonu_db import db
from nagonu_results_checker import (
    build_results_checker_stage,
    fulfill_results_checker_sale,
    is_results_checker_service_id,
)
from nagonu_store import is_valid_gh_phone, normalize_phone, to_oid, validate_agent_code
import nagonu_checkout as checkout


orders_col = db["orders"]
services_col = db["services"]
store_accounts_col = db["store_accounts"]
pending_orders_col = db["ussd_pending_orders"]


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
    store_base = _money(data.get("base_amount"), system_base)
    profit_amount = max(0.0, round(store_base - system_base, 2))
    profit_percent_used = round((profit_amount / system_base * 100), 2) if system_base > 0 else 0.0
    store_profit_amount = max(0.0, round(amount - store_base, 2))
    network_id = checkout._resolve_network_id({"serviceId": service_id, "serviceName": svc_name}, value_obj, svc_doc)
    bundle_key = checkout._build_bundle_key(value_obj if isinstance(value_obj, dict) else {}, {"value": data.get("value")})
    amount_key = checkout._normalize_amount_key(amount)
    ported_fields = checkout._extract_ported_fields({"phone": phone, "serviceName": svc_name})

    base_line = {
        "phone": phone,
        "base_amount": store_base,
        "system_base_amount": system_base,
        "amount": amount,
        "profit_amount": profit_amount,
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
    use_bundleportal = bool(
        (
            (is_mtn_normal and chosen_mtn_normal_provider == "bundleportal")
            or (is_mtn_express and chosen_mtn_express_provider == "bundleportal")
            or ((not is_mtn_normal and not is_mtn_express) and svc_provider == "bundleportal")
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
        and not use_bundleportal
    )
    use_datakazina = bool(
        (
            (resolved_network == "mtn" and is_mtn_express and chosen_mtn_express_provider == "datakazina")
            or (is_mtn_normal and chosen_mtn_normal_provider == "datakazina")
        )
        and not use_codecraft
        and not use_bundleportal
        and not use_portal02
    )
    use_skplug = bool(
        (
            (resolved_network == "mtn" and is_mtn_express and chosen_mtn_express_provider == "skplug")
            or (is_mtn_normal and chosen_mtn_normal_provider == "skplug")
        )
        and not use_codecraft
        and not use_bundleportal
        and not use_portal02
    )

    external_ref = f"{order_id}_1_{uuid.uuid4().hex[:6]}"

    if use_bundleportal:
        provider_gig = checkout._resolve_package_size_gb(value_obj, {"value": data.get("value")})
        portal_network = checkout._resolve_bundleportal_network(svc_doc, {"serviceName": svc_name})
        if not provider_gig or not portal_network:
            return _line_for_manual(
                base_line,
                "BundlePortal fields missing; queued for manual processing.",
                "skipped_missing_fields",
            ), None
        line = {
            **base_line,
            "provider": "bundleportal",
            "provider_reference": None,
            "provider_order_id": None,
            "provider_request_order_id": external_ref,
            "provider_network": portal_network,
            "provider_gig": provider_gig,
            "line_status": "pending",
            "api_status": "queued",
            "api_response": {"note": "Queued for BundlePortal API call"},
        }
        job = {
            "provider_request_order_id": external_ref,
            "phone": phone,
            "provider": "bundleportal",
            "provider_network": portal_network,
            "provider_gig": provider_gig,
            "service_id": svc_doc["_id"],
            "line_index": 1,
        }
        return line, job

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


def _stage_line_until_payment(line: Dict[str, Any]) -> Dict[str, Any]:
    staged = dict(line)
    staged["line_status"] = "awaiting_payment"
    staged["api_status"] = "awaiting_payment"
    staged["api_response"] = {"note": "Waiting for Paystack payment confirmation before processing."}
    return staged


def _pending_query(pending_order_id: Any) -> Dict[str, Any]:
    oid = to_oid(pending_order_id)
    return {"_id": oid} if oid else {"id": str(pending_order_id)}


def _build_staged_order(data: Dict[str, Any], session_id: str, dial_phone: str, order_id: str, line: Dict[str, Any], job: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    loaded = validate_agent_code(data.get("agent_code") or "")
    store = (loaded or {}).get("store") or {}
    amount = _money(data.get("amount"))
    paystack_fee = round(amount * 0.02, 2)
    charged_amount = round(amount + paystack_fee, 2)
    staged_line = _stage_line_until_payment(line)
    return {
        "user_id": to_oid(data.get("agent_user_id")) or store.get("owner_id"),
        "store_slug": store.get("slug") or data.get("store_slug"),
        "order_id": order_id,
        "items": [staged_line],
        "release_items": [line],
        "provider_jobs": [job] if job else [],
        "total_amount": amount,
        "charged_amount": charged_amount,
        "gateway_fee": paystack_fee,
        "profit_amount_total": _money(line.get("profit_amount")),
        "session_id": session_id,
        "dial_phone": normalize_phone(dial_phone),
        "buyer_phone": normalize_phone(data.get("recipient") or dial_phone),
        "agent_code": data.get("agent_code"),
        "agent_user_id": str(data.get("agent_user_id") or ""),
        "pending_order_id": data.get("pending_order_id"),
        "product_kind": data.get("product_kind") or "service",
    }


def _create_paid_order_from_stage(staged: Dict[str, Any], payment: Dict[str, Any], paystack_data: Dict[str, Any] | None = None) -> Dict[str, Any]:
    now = datetime.utcnow()
    order_id = str(staged.get("order_id") or "")
    reference = payment.get("paystack_reference") or payment.get("payment_reference") or (paystack_data or {}).get("reference") or ""
    paid_amount = _money(payment.get("amount") or staged.get("charged_amount"))
    base_amount = _money(payment.get("base_amount") or staged.get("total_amount"))
    gateway_fee = _money(payment.get("gateway_fee") or staged.get("gateway_fee") or max(0.0, paid_amount - base_amount))
    release_items = staged.get("release_items") or staged.get("items") or []
    provider_jobs = staged.get("provider_jobs") or []

    return {
        "user_id": staged.get("user_id"),
        "store_slug": staged.get("store_slug"),
        "order_id": order_id,
        "items": release_items,
        "total_amount": base_amount,
        "charged_amount": paid_amount,
        "profit_amount_total": _money(staged.get("profit_amount_total")),
        "status": "processing",
        "paid_from": "ussd",
        "payment_provider": "paystack",
        "payment_reference": reference,
        "payment_gateway": "Paystack",
        "payment_status": "success",
        "payment_channel": "mobile_money",
        "paystack_reference": reference,
        "paystack_charged_amount": paid_amount,
        "paystack_fee_amount": gateway_fee,
        "payment_verified_at": now,
        "payment_raw": paystack_data or {},
        "created_at": payment.get("paid_at") or now,
        "updated_at": now,
        "ussd": {
            "session_id": staged.get("session_id"),
            "dial_phone": staged.get("dial_phone"),
            "agent_code": staged.get("agent_code"),
            "pending_order_id": staged.get("pending_order_id"),
            "release_items": release_items,
            "provider_jobs": provider_jobs,
        },
        "debug": {
            "store_checkout": True,
            "ussd_checkout": True,
            "events": [],
            "paystack_paid_ghs": paid_amount,
            "paystack_expected_ghs": paid_amount,
            "paystack_base_ghs": base_amount,
            "paystack_fee_ghs": gateway_fee,
            "gateway_fee_overage_ghs": gateway_fee,
            "skipped_count": 0,
        },
    }


def create_nagonu_ussd_order(data: Dict[str, Any], session_id: str, dial_phone: str) -> Dict[str, Any]:
    if not validate_agent_code(data.get("agent_code") or ""):
        return {"success": False, "message": "Invalid agent code."}
    recipient = normalize_phone(data.get("recipient"))
    if not is_valid_gh_phone(recipient):
        return {"success": False, "message": "Invalid recipient phone number."}

    order_id = checkout.generate_order_id()
    if is_results_checker_service_id(data.get("service_id")):
        staged_result = build_results_checker_stage({**data, "recipient": recipient, "dial_phone": dial_phone}, order_id)
        if not staged_result.get("success"):
            return staged_result
        staged_order = {
            "user_id": to_oid(data.get("agent_user_id")),
            "store_slug": data.get("store_slug"),
            "order_id": order_id,
            "items": staged_result.get("items") or [],
            "release_items": staged_result.get("items") or [],
            "provider_jobs": [],
            "total_amount": staged_result.get("base_amount"),
            "charged_amount": staged_result.get("charged_amount"),
            "gateway_fee": staged_result.get("gateway_fee"),
            "profit_amount_total": 0.0,
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
        line, job = _build_line_and_job(order_id, {**data, "recipient": recipient})
        staged_order = _build_staged_order({**data, "recipient": recipient}, session_id, dial_phone, order_id, line, job)
    pending_order_id = data.get("pending_order_id")
    if pending_order_id:
        pending_orders_col.update_one(
            _pending_query(pending_order_id),
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
        "charged_amount": staged_order.get("charged_amount"),
        "base_amount": staged_order.get("total_amount"),
        "gateway_fee": staged_order.get("gateway_fee"),
        "items": staged_order.get("items"),
    }


def create_nagonu_guest_checker_order(data: Dict[str, Any], session_id: str, dial_phone: str) -> Dict[str, Any]:
    """Stage a public-price checker order without weakening normal agent checks."""
    recipient = normalize_phone(dial_phone)
    if not is_valid_gh_phone(recipient):
        return {"success": False, "message": "Invalid caller phone number."}
    if not is_results_checker_service_id(data.get("service_id")):
        return {"success": False, "message": "Only Results Checker is available."}

    order_id = checkout.generate_order_id()
    staged_result = build_results_checker_stage(
        {**data, "recipient": recipient, "dial_phone": recipient},
        order_id,
    )
    if not staged_result.get("success"):
        return staged_result
    staged_order = {
        "user_id": None,
        "store_slug": None,
        "order_id": order_id,
        "items": staged_result.get("items") or [],
        "release_items": staged_result.get("items") or [],
        "provider_jobs": [],
        "total_amount": staged_result.get("base_amount"),
        "charged_amount": staged_result.get("charged_amount"),
        "gateway_fee": staged_result.get("gateway_fee"),
        "profit_amount_total": 0.0,
        "session_id": session_id,
        "dial_phone": recipient,
        "buyer_phone": recipient,
        "agent_code": "",
        "agent_user_id": "",
        "pending_order_id": data.get("pending_order_id"),
        "product_kind": "results_checker",
        "checker_type": staged_result.get("checker_type"),
        "guest_checkout": True,
    }
    pending_order_id = data.get("pending_order_id")
    if pending_order_id:
        pending_orders_col.update_one(
            _pending_query(pending_order_id),
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
        "charged_amount": staged_order.get("charged_amount"),
        "base_amount": staged_order.get("total_amount"),
        "gateway_fee": staged_order.get("gateway_fee"),
        "items": staged_order.get("items"),
    }


def release_nagonu_ussd_order(pending_order_id: str, payment: Dict[str, Any], paystack_data: Dict[str, Any] | None = None) -> Dict[str, Any]:
    if not pending_order_id:
        return {"released": False, "reason": "missing_pending_order_id"}

    pending = pending_orders_col.find_one(_pending_query(pending_order_id))
    if not pending:
        return {"released": False, "reason": "pending_order_not_found"}

    staged = pending.get("staged_order") or {}
    order_id = str((pending.get("created_order_id") or staged.get("order_id") or pending.get("order_id") or "").strip())
    if not order_id:
        return {"released": False, "reason": "missing_order_id"}

    order = orders_col.find_one({"order_id": order_id}) or orders_col.find_one({"ussd.pending_order_id": pending_order_id})
    if not order:
        if staged.get("product_kind") == "results_checker":
            checker_result = fulfill_results_checker_sale(staged, payment, paystack_data)
            if checker_result.get("success"):
                final_items = [checker_result.get("line") or {}]
                profit_total = _money((checker_result.get("sale_doc") or {}).get("profit_amount"))
                status_value = checker_result.get("order_status") or "completed"
                order_doc = _create_paid_order_from_stage(
                    {
                        **staged,
                        "release_items": final_items,
                        "items": final_items,
                        "provider_jobs": [],
                        "profit_amount_total": profit_total,
                    },
                    payment,
                    paystack_data,
                )
                order_doc["status"] = status_value
                order_doc["ussd"]["results_checker"] = {
                    "checker_type": staged.get("checker_type"),
                    "sms_status": checker_result.get("sms_status"),
                    "buyer_phone": staged.get("buyer_phone"),
                }
                orders_col.insert_one(order_doc)
                sale_doc = checker_result.get("sale_doc") or {}
                if sale_doc:
                    sales_col = db["ussd_results_checker_sales"]
                    sales_col.update_one(
                        {"order_id": order_id},
                        {"$setOnInsert": sale_doc},
                        upsert=True,
                    )
            else:
                fallback_item = {
                    "phone": staged.get("buyer_phone") or staged.get("dial_phone") or "",
                    "value": str(staged.get("checker_type") or "").upper(),
                    "value_obj": {"checker_type": staged.get("checker_type")},
                    "serviceId": staged.get("release_items", [{}])[0].get("serviceId") if staged.get("release_items") else "results_checker_ussd",
                    "serviceName": "Results Checker",
                    "service_type": "RESULTS_CHECKER",
                    "amount": _money(staged.get("total_amount")),
                    "base_amount": 0.0,
                    "system_base_amount": 0.0,
                    "profit_amount": 0.0,
                    "profit_percent_used": 0.0,
                    "store_profit_amount": 0.0,
                    "checker_type": staged.get("checker_type"),
                    "line_status": "processing",
                    "api_status": "inventory_unavailable",
                    "api_response": {"note": checker_result.get("message") or "Checker inventory is unavailable after payment."},
                }
                order_doc = _create_paid_order_from_stage(
                    {
                        **staged,
                        "release_items": [fallback_item],
                        "items": [fallback_item],
                        "provider_jobs": [],
                        "profit_amount_total": 0.0,
                    },
                    payment,
                    paystack_data,
                )
                order_doc["status"] = "processing"
                order_doc["ussd"]["results_checker_error"] = checker_result.get("reason") or "inventory_unavailable"
                orders_col.insert_one(order_doc)
        else:
            order_doc = _create_paid_order_from_stage(staged, payment, paystack_data)
            orders_col.insert_one(order_doc)
        pending_orders_col.update_one(
            _pending_query(pending_order_id),
            {
                "$set": {
                    "status": "order_created",
                    "created_order_id": order_id,
                    "payment_status": "success",
                    "updated_at": datetime.utcnow(),
                }
            },
        )
        order = order_doc
    else:
        order_id = str(order.get("order_id") or order_id)

    now = datetime.utcnow()
    release_items = ((order.get("ussd") or {}).get("release_items") or order.get("items") or [])
    store_slug = order.get("store_slug")
    store_profit_total = _money(sum(_money(item.get("store_profit_amount")) for item in release_items))
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

    items = release_items
    if staged.get("product_kind") != "results_checker":
        try:
            checkout._send_mashup_order_sms_async(order_id, order.get("created_at") or now, items)
        except Exception:
            pass

    jobs = ((order.get("ussd") or {}).get("provider_jobs") or [])
    if jobs:
        threading.Thread(target=checkout._background_process_providers, args=(order_id, jobs), daemon=True).start()

    pending_orders_col.update_one(
        _pending_query(pending_order_id),
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
