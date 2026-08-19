from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Set
import re

from bson import ObjectId

from zico_db import db
from zico_sms_sender import get_sms_settings, normalize_ghana_sms_phone, resolve_system_sender_id, send_sms


TARGET_MASHUP_BASE_SERVICE_ID = ObjectId("6a299f7472e6d9d109a67ad8")
DEFAULT_TARGET_MASHUP_SMS_RECIPIENT = "0530393625"

orders_col = db["orders"]
services_col = db["services"]


def _to_oid(value: Any) -> Optional[ObjectId]:
    if isinstance(value, ObjectId):
        return value
    if not value:
        return None
    try:
        return ObjectId(str(value))
    except Exception:
        return None


def _local_phone(raw: Any) -> str:
    digits = re.sub(r"\D+", "", str(raw or ""))
    if digits.startswith("233") and len(digits) == 12:
        return "0" + digits[3:]
    if digits.startswith("0") and len(digits) == 10:
        return digits
    return digits or str(raw or "").strip()


def _target_order_sms_recipient() -> str:
    raw = str((get_sms_settings() or {}).get("order_sms_recipient") or "").strip()
    normalized = normalize_ghana_sms_phone(raw)
    if normalized:
        return "0" + normalized[3:]
    return DEFAULT_TARGET_MASHUP_SMS_RECIPIENT


def _service_base_id(service_id: Any) -> Optional[ObjectId]:
    service_oid = _to_oid(service_id)
    if not service_oid:
        return None
    try:
        svc = services_col.find_one({"_id": service_oid}, {"base_service_id": 1}) or {}
    except Exception:
        return None
    return _to_oid(svc.get("base_service_id")) or service_oid


def _item_matches_target_base(item: Dict[str, Any]) -> bool:
    direct_base = _to_oid(item.get("base_service_id") or item.get("baseServiceId"))
    if direct_base == TARGET_MASHUP_BASE_SERVICE_ID:
        return True
    return _service_base_id(item.get("serviceId") or item.get("service_id")) == TARGET_MASHUP_BASE_SERVICE_ID


def _order_datetime(order_doc: Dict[str, Any]) -> datetime:
    for key in ("created_at", "payment_verified_at", "updated_at"):
        value = order_doc.get(key)
        if isinstance(value, datetime):
            return value
    return datetime.utcnow()


def _format_gb_label(value: Any) -> str:
    try:
        gb = float(value)
    except Exception:
        return ""
    if gb <= 0:
        return ""
    if gb.is_integer():
        return f"{int(gb)}GB"
    return f"{gb:g}GB"


def _volume_text(item: Dict[str, Any]) -> str:
    for key in ("package_size_gb", "provider_gig", "gb", "gb_size", "volume_gb", "size_gb"):
        label = _format_gb_label(item.get(key))
        if label:
            return label

    value_obj = item.get("value_obj")
    if isinstance(value_obj, dict):
        for key in ("gb", "gb_size", "package_size", "volume_gb", "size_gb"):
            label = _format_gb_label(value_obj.get(key))
            if label:
                return label

        volume = value_obj.get("volume")
        if volume not in (None, "", []):
            try:
                volume_num = float(volume)
            except Exception:
                volume_num = None
            if volume_num is not None:
                if volume_num > 50:
                    divisor = 1024.0 if volume_num % 1024 == 0 else 1000.0
                    return _format_gb_label(volume_num / divisor)
                return _format_gb_label(volume_num)

    bundle_key = item.get("bundle_key")
    if isinstance(bundle_key, dict):
        label = _format_gb_label(bundle_key.get("value"))
        if label:
            return label

    for raw in (item.get("value"), item.get("serviceName"), item.get("service_name")):
        text = str(raw or "")
        match = re.search(r"(\d+(?:\.\d+)?)\s*gb\b", text, flags=re.IGNORECASE)
        if match:
            return _format_gb_label(match.group(1))

    return ""


def _sms_key(order_id: str, phone: str, index: int) -> str:
    return f"mtn_mashup:{order_id}:{phone}:{index}"


def send_mtn_mashup_order_sms(order_doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Send fixed-recipient SMS alerts for orders using the MTN Mash Up base service."""
    if not isinstance(order_doc, dict):
        return []

    order_id = str(order_doc.get("order_id") or "").strip()
    if not order_id:
        return []

    already_sent: Set[str] = {
        str(key)
        for key in (order_doc.get("mtn_mashup_sms_keys") or [])
        if key
    }
    sent_rows: List[Dict[str, Any]] = []
    order_dt = _order_datetime(order_doc)
    date_text = order_dt.strftime("%d/%m/%Y %H:%M")
    sender_id = resolve_system_sender_id(order_doc.get("admin_id"), recipient_role="admin")
    recipient = _target_order_sms_recipient()

    for index, item in enumerate(order_doc.get("items") or [], start=1):
        if not isinstance(item, dict) or not _item_matches_target_base(item):
            continue
        if str(item.get("line_status") or "").strip().lower().startswith("skipped"):
            continue
        phone = _local_phone(item.get("phone") or order_doc.get("phone") or order_doc.get("payer_phone"))
        if not phone:
            continue
        key = _sms_key(order_id, phone, index)
        if key in already_sent:
            continue
        claimed = orders_col.update_one(
            {
                "order_id": order_id,
                "mtn_mashup_sms_keys": {"$ne": key},
            },
            {
                "$addToSet": {"mtn_mashup_sms_keys": key},
                "$set": {"mtn_mashup_sms_last_attempt_at": datetime.utcnow()},
            },
        )
        if claimed.modified_count < 1:
            continue

        volume_text = _volume_text(item)
        message = f"{phone} {volume_text} {order_id} {date_text}" if volume_text else f"{phone} {order_id} {date_text}"
        status = send_sms(recipient, message, sender_id=sender_id)
        row = {
            "key": key,
            "status": status,
            "recipient": recipient,
            "message": message,
            "sent_at": datetime.utcnow(),
        }
        orders_col.update_one(
            {"order_id": order_id},
            {
                "$push": {"mtn_mashup_sms_logs": row},
                "$set": {
                    "mtn_mashup_sms_last_status": status,
                    "mtn_mashup_sms_last_message": message,
                },
            },
        )
        sent_rows.append(row)

    return sent_rows
