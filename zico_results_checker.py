from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pymongo import ReturnDocument

from zico_db import db
from zico_profit_ledger import apply_profit_split, normalize_profit_line
from zico_sms_sender import resolve_admin_sender_name, send_sms


wassce_col = db["wassce_checker"]
checker_pricing_col = db["checker_pricing"]
purchase_history_col = db["purchase_history"]

RESULTS_CHECKER_SERVICE_ID = "results_checker_ussd"
RESULTS_CHECKER_SERVICE_NAME = "Results Checker"
SUPPORTED_CHECKER_TYPES = ("wassce", "bece")


def checker_type_label(value: str) -> str:
    raw = str(value or "").strip().lower()
    return "BECE" if raw == "bece" else "WASSCE"


def is_results_checker_service_id(service_id: Any) -> bool:
    return str(service_id or "").strip() == RESULTS_CHECKER_SERVICE_ID


def _pricing_doc(checker_type: str) -> Dict[str, Any]:
    return checker_pricing_col.find_one({"checker_type": checker_type}) or {}


def _checker_base_cost(checker_type: str) -> float:
    doc = _pricing_doc(checker_type)
    try:
        return round(float(doc.get("base_cost") or 0), 2)
    except Exception:
        return 0.0


def _checker_inventory_count(checker_type: str) -> int:
    return wassce_col.count_documents({"type": checker_type, "status": "not_sold"})


def _configured_checker_items(store_doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    cfg = (store_doc or {}).get("checker_product") or {}
    if not cfg.get("enabled"):
        return []

    types_cfg = cfg.get("types") or {}
    items: List[Dict[str, Any]] = []
    for checker_type in SUPPORTED_CHECKER_TYPES:
        row = types_cfg.get(checker_type) or {}
        if not isinstance(row, dict) or not row.get("enabled"):
            continue
        try:
            selling_price = round(float(row.get("price") or 0), 2)
        except Exception:
            selling_price = 0.0
        if selling_price <= 0:
            continue
        inventory = _checker_inventory_count(checker_type)
        if inventory <= 0:
            continue
        items.append(
            {
                "type": checker_type,
                "label": checker_type_label(checker_type),
                "selling_price": selling_price,
                "cost_price": _checker_base_cost(checker_type),
                "inventory": inventory,
            }
        )
    return items


def build_results_checker_service(store_doc: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    items = _configured_checker_items(store_doc)
    if not items:
        return None
    offers = []
    for idx, item in enumerate(items):
        offers.append(
            {
                "index": idx,
                "service_id": RESULTS_CHECKER_SERVICE_ID,
                "service_name": RESULTS_CHECKER_SERVICE_NAME,
                "value": item["type"],
                "value_text": item["label"],
                "amount": item["selling_price"],
                "base_amount": item["cost_price"],
                "checker_type": item["type"],
                "inventory": item["inventory"],
            }
        )
    return {
        "id": RESULTS_CHECKER_SERVICE_ID,
        "name": RESULTS_CHECKER_SERVICE_NAME,
        "kind": "results_checker",
        "offers": offers,
    }


def build_results_checker_stage(data: Dict[str, Any], order_id: str) -> Dict[str, Any]:
    checker_type = str(data.get("checker_type") or data.get("value") or "").strip().lower()
    if checker_type not in SUPPORTED_CHECKER_TYPES:
        return {"success": False, "message": "Invalid checker type selected."}

    selling_price = round(float(data.get("amount") or 0), 2)
    if selling_price <= 0:
        return {"success": False, "message": "Invalid checker price configured for this agent."}

    cost_price = _checker_base_cost(checker_type)
    if cost_price <= 0:
        return {"success": False, "message": "Checker base cost is not configured."}
    if selling_price < cost_price:
        return {"success": False, "message": "Checker selling price cannot be below cost price."}

    line = apply_profit_split(
        normalize_profit_line(
            {
                "phone": str(data.get("recipient") or data.get("dial_phone") or ""),
                "value": checker_type_label(checker_type),
                "value_obj": {"checker_type": checker_type},
                "serviceId": RESULTS_CHECKER_SERVICE_ID,
                "serviceName": RESULTS_CHECKER_SERVICE_NAME,
                "service_type": "RESULTS_CHECKER",
                "checker_type": checker_type,
                "line_status": "awaiting_payment",
                "api_status": "awaiting_payment",
                "api_response": {"note": "Waiting for Paystack payment confirmation before sending checker SMS."},
            },
            selling_amount=selling_price,
            main_base_amount=cost_price,
            admin_base_amount=cost_price,
            store_owner_base_amount=cost_price,
            store_profit_amount=round(max(0.0, selling_price - cost_price), 2),
        )
    )
    paystack_fee = round(selling_price * 0.02, 2)
    charged_amount = round(selling_price + paystack_fee, 2)
    return {
        "success": True,
        "order_id": order_id,
        "product_kind": "results_checker",
        "checker_type": checker_type,
        "status": "staged_for_payment",
        "charged_amount": charged_amount,
        "base_amount": selling_price,
        "gateway_fee": paystack_fee,
        "items": [line],
    }


def _delivery_sms_message(checker: Dict[str, Any], sender_name: str) -> str:
    checker_type = str(checker.get("type") or "").upper() or "RESULT CHECKER"
    body = str(checker.get("message") or "").strip()
    sender_label = sender_name or "Azico"
    return f"{checker_type} via {sender_label}\n{body}" if body else f"{checker_type} via {sender_label}"


def fulfill_results_checker_sale(staged: Dict[str, Any], payment: Dict[str, Any], paystack_data: Dict[str, Any] | None = None) -> Dict[str, Any]:
    checker_type = str(staged.get("checker_type") or "").strip().lower()
    allocated = wassce_col.find_one_and_update(
        {"type": checker_type, "status": "not_sold"},
        {
            "$set": {
                "status": "sold",
                "sold_at": datetime.utcnow(),
                "sold_channel": "ussd",
                "sold_order_id": staged.get("order_id"),
                "sold_to_phone": staged.get("buyer_phone"),
                "sold_store_slug": staged.get("store_slug"),
            }
        },
        sort=[("created_at", 1)],
        return_document=ReturnDocument.AFTER,
    )
    if not allocated:
        return {"success": False, "reason": "checker_out_of_stock", "message": "Checker inventory is currently unavailable."}

    cost_price = _checker_base_cost(checker_type)
    selling_price = round(float(staged.get("total_amount") or 0), 2)
    profit_amount = round(max(0.0, selling_price - cost_price), 2)
    buyer_phone = str(staged.get("buyer_phone") or "").strip()
    admin_id = staged.get("admin_id")
    sender_name = resolve_admin_sender_name(admin_id)
    checker_message = str(allocated.get("message") or "").strip()
    sms_status = send_sms(buyer_phone, _delivery_sms_message(allocated, sender_name), sender_id=sender_name) if buyer_phone and checker_message else "error"

    line = apply_profit_split(
        normalize_profit_line(
            {
                "phone": buyer_phone,
                "value": checker_type_label(checker_type),
                "value_obj": {"checker_type": checker_type},
                "serviceId": RESULTS_CHECKER_SERVICE_ID,
                "serviceName": RESULTS_CHECKER_SERVICE_NAME,
                "service_type": "RESULTS_CHECKER",
                "checker_type": checker_type,
                "checker_id": str(allocated.get("_id")),
                "checker_message": checker_message,
                "sms_status": sms_status,
                "line_status": "completed" if sms_status == "sent" else "processing",
                "api_status": "sms_sent" if sms_status == "sent" else "sms_failed",
                "api_response": {"note": "Checker delivered by SMS." if sms_status == "sent" else "Checker payment confirmed but SMS delivery needs attention."},
            },
            selling_amount=selling_price,
            main_base_amount=cost_price,
            admin_base_amount=cost_price,
            store_owner_base_amount=cost_price,
            store_profit_amount=profit_amount,
        )
    )
    history_doc = {
        "user_id": str(staged.get("user_id") or ""),
        "admin_id": staged.get("admin_id"),
        "checker_id": str(allocated.get("_id")),
        "type": checker_type,
        "amount": selling_price,
        "base_cost_ghs": cost_price,
        "profit_amount": profit_amount,
        "message": checker_message,
        "delivery_phone": buyer_phone,
        "sms_delivery_status": sms_status,
        "store_slug": staged.get("store_slug"),
        "purchased_at": payment.get("paid_at") or datetime.utcnow(),
        "source": "ussd_results_checker",
        "pricing_meta": {
            "base_cost_ghs": cost_price,
            "profit_amount": profit_amount,
            "source": "ussd_results_checker",
        },
    }
    return {
        "success": True,
        "line": line,
        "history_doc": history_doc,
        "allocated_checker": allocated,
        "sms_status": sms_status,
        "order_status": "completed" if sms_status == "sent" else "processing",
    }

