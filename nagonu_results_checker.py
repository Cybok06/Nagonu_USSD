from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import requests
from pymongo import ReturnDocument

from nagonu_db import db


wassce_col = db["wassce_checker"]
sales_col = db["ussd_results_checker_sales"]
checker_settings_col = db["results_checker_settings"]

RESULTS_CHECKER_SERVICE_ID = "results_checker_ussd"
RESULTS_CHECKER_SERVICE_NAME = "Results Checker"
SUPPORTED_CHECKER_TYPES = ("wassce", "bece")

# Copied from Nagonu admin balance as requested for runner-side SMS sends.
ARKESEL_API_KEY = "TGFhVVZvU3NOclJMZFJwWWJ5U2o"
SENDER_ID = "Nagonu"


def checker_type_label(value: str) -> str:
    raw = str(value or "").strip().lower()
    if raw == "bece":
        return "BECE"
    return "WASSCE"


def is_results_checker_service_id(service_id: Any) -> bool:
    return str(service_id or "").strip() == RESULTS_CHECKER_SERVICE_ID


def _normalize_phone(raw: str) -> str | None:
    if not raw:
        return None
    p = raw.strip().replace(" ", "").replace("-", "").replace("+", "")
    if p.startswith("0") and len(p) == 10:
        p = "233" + p[1:]
    elif p.startswith("233") and len(p) == 12:
        pass
    else:
        return None
    return p if p.isdigit() and len(p) == 12 else None


def send_results_checker_sms(msisdn: str, message: str) -> str:
    try:
        url = (
            "https://sms.arkesel.com/sms/api?action=send-sms"
            f"&api_key={ARKESEL_API_KEY}"
            f"&to={msisdn}"
            f"&from={quote(SENDER_ID)}"
            f"&sms={quote(message)}"
        )
        resp = requests.get(url, timeout=12)
        if resp.status_code == 200 and '"code":"ok"' in resp.text:
            return "sent"
        return "failed"
    except Exception:
        return "error"


def _checker_inventory_count(checker_type: str) -> int:
    return wassce_col.count_documents({"type": checker_type, "status": "not_sold"})


def _public_checker_price(checker_type: str) -> float:
    settings = checker_settings_col.find_one({"_id": "checker_prices"}, {"prices": 1}) or {}
    try:
        configured = round(float((settings.get("prices") or {}).get(checker_type) or 0), 2)
    except Exception:
        configured = 0.0
    if configured > 0:
        return configured
    sample = wassce_col.find_one(
        {"type": checker_type, "status": "not_sold"},
        {"amount": 1},
        sort=[("created_at", 1)],
    ) or {}
    try:
        return round(float(sample.get("amount") or 0), 2)
    except Exception:
        return 0.0


def build_public_results_checker_service() -> Optional[Dict[str, Any]]:
    """Build the unaffiliated USSD checker menu from public-page prices."""
    offers: List[Dict[str, Any]] = []
    for checker_type in SUPPORTED_CHECKER_TYPES:
        inventory = _checker_inventory_count(checker_type)
        price = _public_checker_price(checker_type)
        offers.append(
            {
                "index": len(offers),
                "service_id": RESULTS_CHECKER_SERVICE_ID,
                "service_name": RESULTS_CHECKER_SERVICE_NAME,
                "value": checker_type,
                "value_text": checker_type_label(checker_type),
                "amount": price,
                "base_amount": price,
                "checker_type": checker_type,
                "inventory": inventory,
            }
        )
    if not any(float(item.get("amount") or 0) > 0 and int(item.get("inventory") or 0) > 0 for item in offers):
        return None
    return {
        "id": RESULTS_CHECKER_SERVICE_ID,
        "name": RESULTS_CHECKER_SERVICE_NAME,
        "kind": "results_checker",
        "offers": offers,
    }


def _configured_checker_items(store_doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    cfg = (store_doc or {}).get("ussd_results_checker") or {}
    if not cfg.get("enabled"):
        return []

    items = []
    for row in cfg.get("items") or []:
        checker_type = str(row.get("type") or "").strip().lower()
        if checker_type not in SUPPORTED_CHECKER_TYPES:
            continue
        if not row.get("enabled"):
            continue
        try:
            selling_price = round(float(row.get("selling_price") or 0), 2)
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
                "base_amount": item["selling_price"],
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

    paystack_fee = round(selling_price * 0.02, 2)
    charged_amount = round(selling_price + paystack_fee, 2)
    item = {
        "phone": str(data.get("recipient") or data.get("dial_phone") or ""),
        "value": checker_type_label(checker_type),
        "value_obj": {"checker_type": checker_type},
        "serviceId": RESULTS_CHECKER_SERVICE_ID,
        "serviceName": RESULTS_CHECKER_SERVICE_NAME,
        "service_type": "RESULTS_CHECKER",
        "amount": selling_price,
        "base_amount": 0.0,
        "system_base_amount": 0.0,
        "profit_amount": 0.0,
        "profit_percent_used": 0.0,
        "store_profit_amount": 0.0,
        "checker_type": checker_type,
        "line_status": "awaiting_payment",
        "api_status": "awaiting_payment",
        "api_response": {"note": "Waiting for Paystack payment confirmation before sending checker SMS."},
    }
    return {
        "success": True,
        "order_id": order_id,
        "product_kind": "results_checker",
        "checker_type": checker_type,
        "status": "staged_for_payment",
        "charged_amount": charged_amount,
        "base_amount": selling_price,
        "gateway_fee": paystack_fee,
        "items": [item],
    }


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

    try:
        cost_price = round(float(allocated.get("amount") or 0), 2)
    except Exception:
        cost_price = 0.0
    selling_price = round(float(staged.get("total_amount") or 0), 2)
    profit_amount = round(max(0.0, selling_price - cost_price), 2)
    buyer_phone = str(staged.get("buyer_phone") or "").strip()
    sms_phone = _normalize_phone(buyer_phone or "")
    checker_message = str(allocated.get("message") or "").strip()
    sms_status = send_results_checker_sms(sms_phone, checker_message) if sms_phone and checker_message else "invalid_phone"

    line = {
        "phone": buyer_phone,
        "value": checker_type_label(checker_type),
        "value_obj": {"checker_type": checker_type},
        "serviceId": RESULTS_CHECKER_SERVICE_ID,
        "serviceName": RESULTS_CHECKER_SERVICE_NAME,
        "service_type": "RESULTS_CHECKER",
        "amount": selling_price,
        "base_amount": cost_price,
        "system_base_amount": cost_price,
        "profit_amount": profit_amount,
        "profit_percent_used": round((profit_amount / cost_price) * 100, 2) if cost_price > 0 else 0.0,
        "store_profit_amount": profit_amount,
        "checker_type": checker_type,
        "checker_id": str(allocated.get("_id")),
        "checker_message": checker_message,
        "sms_status": sms_status,
        "line_status": "completed" if sms_status == "sent" else "processing",
        "api_status": "sms_sent" if sms_status == "sent" else "sms_failed",
        "api_response": {"note": "Checker delivered by SMS." if sms_status == "sent" else "Checker payment confirmed but SMS delivery needs attention."},
    }
    sale_doc = {
        "app": "nagonu",
        "source": "ussd_results_checker",
        "order_id": staged.get("order_id"),
        "pending_order_id": staged.get("pending_order_id"),
        "store_slug": staged.get("store_slug"),
        "agent_user_id": staged.get("agent_user_id"),
        "agent_code": staged.get("agent_code"),
        "buyer_phone": buyer_phone,
        "checker_type": checker_type,
        "checker_id": str(allocated.get("_id")),
        "selling_price": selling_price,
        "cost_price": cost_price,
        "profit_amount": profit_amount,
        "checker_message": checker_message,
        "sms_status": sms_status,
        "payment_status": "success",
        "created_at": datetime.utcnow(),
        "paid_at": payment.get("paid_at") or datetime.utcnow(),
    }
    return {
        "success": True,
        "line": line,
        "sale_doc": sale_doc,
        "allocated_checker": allocated,
        "sms_status": sms_status,
        "order_status": "completed" if sms_status == "sent" else "processing",
    }
