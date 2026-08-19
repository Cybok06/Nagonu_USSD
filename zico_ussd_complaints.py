from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, Tuple

from bson import ObjectId

from zico_db import db


complaints_col = db["complaints"]
orders_col = db["orders"]
stores_col = db["stores"]
users_col = db["users"]


def to_oid(value: Any) -> ObjectId | None:
    if isinstance(value, ObjectId):
        return value
    try:
        return ObjectId(str(value))
    except Exception:
        return None


def normalize_phone(raw: Any) -> str:
    digits = re.sub(r"\D+", "", str(raw or ""))
    if len(digits) == 10 and digits.startswith("0"):
        return digits
    if len(digits) == 12 and digits.startswith("233"):
        return "0" + digits[3:]
    return digits


def valid_phone(raw: Any) -> bool:
    return bool(re.fullmatch(r"0\d{9}", normalize_phone(raw)))


def parse_payment_date_ddmmyyyy(raw: str) -> Tuple[bool, str, datetime | None]:
    value = str(raw or "").strip()
    try:
        dt = datetime.strptime(value, "%d/%m/%Y")
    except Exception:
        return False, "", None
    return True, dt.strftime("%Y-%m-%d"), dt


def parse_payment_time(raw: str) -> Tuple[bool, str]:
    value = str(raw or "").strip()
    try:
        dt = datetime.strptime(value, "%H:%M")
    except Exception:
        return False, ""
    return True, dt.strftime("%H:%M")


def _admin_id_for_store(store_doc: Dict[str, Any]) -> ObjectId | None:
    admin_id = to_oid(store_doc.get("admin_id"))
    if admin_id:
        return admin_id
    owner = users_col.find_one(
        {"_id": to_oid(store_doc.get("owner_id"))},
        {"_id": 1, "role": 1, "admin_id": 1},
    ) or {}
    role = str(owner.get("role") or "").strip().lower()
    if role in {"admin", "main_admin", "superadmin", "super_admin", "professional_admin", "super_professional"}:
        return to_oid(owner.get("_id"))
    return to_oid(owner.get("admin_id"))


def submit_ussd_complaint(store_slug: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    store_doc = stores_col.find_one({"slug": store_slug, "status": {"$ne": "deleted"}})
    if not store_doc:
        return {"success": False, "message": "Store not found."}

    phone = normalize_phone(payload.get("phone") or "")
    paystack_ref = str(payload.get("paystack_reference") or "").strip()
    payment_date_user = str(payload.get("payment_date_user") or "").strip()
    payment_time = str(payload.get("payment_time") or "").strip()
    service_name = str(payload.get("service_name") or "").strip()
    offer = str(payload.get("offer") or "").strip()
    order_id = str(payload.get("order_id") or "").strip()
    message = str(payload.get("message") or "").strip()
    cart = payload.get("cart") or []

    if not valid_phone(phone):
        return {"success": False, "message": "Phone must be 10 digits in the format 0xxxxxxxxx."}
    if not paystack_ref:
        return {"success": False, "message": "MOMO TRANSACTION ID is required."}
    ok_date, payment_date_iso, _ = parse_payment_date_ddmmyyyy(payment_date_user)
    if not ok_date:
        return {"success": False, "message": "Date of payment must be in dd/mm/yyyy format."}
    ok_time, payment_time = parse_payment_time(payment_time)
    if not ok_time:
        return {"success": False, "message": "Time of payment must be in HH:MM format."}
    try:
        payment_dt = datetime.strptime(f"{payment_date_iso} {payment_time}", "%Y-%m-%d %H:%M")
    except Exception:
        return {"success": False, "message": "Invalid payment date/time."}
    if not cart or not isinstance(cart, list):
        return {"success": False, "message": "Service and offer are required."}

    existing_pending = complaints_col.find_one(
        {
            "store_slug": store_slug,
            "status": "pending",
            "$or": [
                {"customer_phone_normalized": phone},
                {"customer_phone": phone},
            ],
        },
        {"_id": 1},
    )
    if existing_pending:
        return {"success": False, "message": "You submitted already. Please wait while we resolve it shortly."}

    total_amount = 0.0
    for item in cart:
        try:
            total_amount += float(item.get("amount") or 0)
        except Exception:
            continue

    existing_order = orders_col.find_one(
        {"store_slug": store_slug, "paystack_reference": paystack_ref},
        {"order_id": 1, "created_at": 1},
    )
    complaint_doc = {
        "admin_id": _admin_id_for_store(store_doc),
        "sent_to_main_admin": False,
        "store_slug": store_slug,
        "store_name": store_doc.get("name") or "",
        "customer_name": "",
        "customer_phone": phone,
        "customer_phone_normalized": phone,
        "paystack_reference": paystack_ref,
        "payment_date": f"{payment_date_user} {payment_time}",
        "payment_date_dt": payment_dt,
        "payment_date_str": f"{payment_date_user} {payment_time}",
        "order_date": (existing_order or {}).get("created_at"),
        "order_number_provided": order_id or paystack_ref,
        "order_ref": {"order_id": order_id} if order_id else {},
        "service_name": service_name,
        "offer": offer,
        "cart_snapshot": cart,
        "cart_total": round(total_amount, 2),
        "screenshots": {},
        "message": message,
        "description": message,
        "flagged_ref_exists": bool(existing_order),
        "flagged_ref_order_id": (existing_order or {}).get("order_id"),
        "submitted_at": datetime.utcnow(),
        "status": "pending",
        "source": "ussd",
        "channel": "ussd",
    }
    complaints_col.insert_one(complaint_doc)
    return {"success": True, "message": "Complaint submitted"}
