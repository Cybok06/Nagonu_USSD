from __future__ import annotations

import ast
import json
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from bson import ObjectId

from zico_db import db


agent_codes_col = db["agent_codes"]
services_col = db["services"]
stores_col = db["stores"]
users_col = db["users"]

SOCIAL_BOOSTING_SERVICE_ID = "social_boosting"
SOCIAL_BOOSTING_NAME = "Social Media Boosting"
BULK_SMS_SERVICE_ID = "bulk_sms"

_NUM = re.compile(r"^\s*-?\d+(\.\d+)?\s*$", re.IGNORECASE)
_GB = re.compile(r"(\d+(?:\.\d+)?)[\s]*G(?:B|IG)?\b", re.IGNORECASE)
_MB = re.compile(r"(\d+(?:\.\d+)?)[\s]*MB\b", re.IGNORECASE)
_MIN = re.compile(r"(\d+(?:\.\d+)?)[\s]*(?:MIN|MINS|MINUTE|MINUTES)\b", re.IGNORECASE)
_PKG_TAIL = re.compile(r"\s*\(Pkg\s*\d+\)\s*$", re.IGNORECASE)


def to_oid(value: Any) -> Optional[ObjectId]:
    if isinstance(value, ObjectId):
        return value
    try:
        return ObjectId(str(value))
    except Exception:
        return None


def normalize_phone(raw: Any) -> str:
    digits = re.sub(r"\D+", "", str(raw or ""))
    if digits.startswith("2330") and len(digits) == 13:
        return digits[3:]
    if digits.startswith("233") and len(digits) == 12:
        return "0" + digits[3:]
    if len(digits) == 9:
        return "0" + digits
    return digits


def is_valid_gh_phone(raw: Any) -> bool:
    return bool(re.fullmatch(r"0\d{9}", normalize_phone(raw)))


def money(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        return float(str(value).replace(",", "").strip())
    except Exception:
        return default


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _admin_role(role: Any) -> bool:
    return _norm(role) in {"admin", "superadmin", "main_admin", "super_admin", "professional_admin", "super_professional"}


def _admin_id_for_user(user: Dict[str, Any]) -> Optional[ObjectId]:
    if _admin_role(user.get("role")):
        return to_oid(user.get("_id"))
    return to_oid(user.get("admin_id"))


def _service_unit(service: Dict[str, Any]) -> str:
    unit = _norm(service.get("unit") or service.get("service_unit"))
    name = _norm(service.get("name") or service.get("network"))
    if unit in {"min", "mins", "minute", "minutes"} or "talk" in name:
        return "minutes"
    return "data"


def _parse_value_field(value: Any) -> Any:
    if isinstance(value, dict) or value is None:
        return value
    if not isinstance(value, str):
        return value
    text = value.strip()
    if text.startswith("{") and text.endswith("}"):
        for parser in (json.loads, ast.literal_eval):
            try:
                parsed = parser(text)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass
    return text


def _extract_volume(value: Any, unit: str) -> Optional[float]:
    value = _parse_value_field(value)
    if isinstance(value, dict):
        vol = value.get("volume") or value.get("offer") or value.get("gb") or value.get("qty")
        if vol is None:
            return None
        if isinstance(vol, (int, float)) or _NUM.match(str(vol)):
            return float(vol)
        return _extract_volume(str(vol), unit)
    if not isinstance(value, str):
        return None
    text = _PKG_TAIL.sub("", value.strip())
    patterns = [_MIN] if unit == "minutes" else [_GB, _MB]
    for pattern in patterns:
        found = pattern.search(text)
        if found:
            amount = float(found.group(1))
            return amount * 1000.0 if pattern is _GB else amount
    if _NUM.match(text):
        return float(text)
    return None


def _format_volume(value: Optional[float], unit: str) -> str:
    if value is None:
        return "-"
    if unit == "minutes":
        return f"{int(round(float(value)))} mins"
    amount = float(value)
    if amount >= 1000:
        gb = amount / 1000.0
        return f"{int(gb)}GB" if abs(gb - int(gb)) < 1e-9 else f"{gb:.2f}GB"
    return f"{int(amount)}MB"


def _value_text(offer: Dict[str, Any], unit: str) -> str:
    vt = offer.get("value_text")
    if isinstance(vt, str) and vt.strip():
        vol = _extract_volume(vt, unit)
        return _format_volume(vol, unit) if vol is not None else vt.strip()
    value = offer.get("value")
    vol = _extract_volume(value, unit)
    if vol is not None:
        return _format_volume(vol, unit)
    if isinstance(value, str) and value.strip():
        return _PKG_TAIL.sub("", value).strip()
    if offer.get("label"):
        return str(offer.get("label")).strip()
    return "-"


def _offer_merge_key(offer: Dict[str, Any], idx: int) -> str:
    parsed = _parse_value_field((offer or {}).get("value"))
    if isinstance(parsed, dict) and parsed.get("volume") not in (None, ""):
        return f"volume:{parsed.get('volume')}"
    if isinstance(parsed, dict) and parsed.get("id") not in (None, ""):
        return f"id:{parsed.get('id')}"
    raw = (offer or {}).get("value")
    if raw not in (None, ""):
        return f"value:{str(raw).strip()}"
    return f"idx:{idx}"


def _svc_offers(service: Dict[str, Any]) -> List[Dict[str, Any]]:
    default_offers = service.get("offers") if isinstance(service.get("offers"), list) else []
    store_offers = service.get("store_offers") if isinstance(service.get("store_offers"), list) else []
    if not store_offers:
        return default_offers or []
    if not default_offers:
        return store_offers or []

    store_map: Dict[str, Dict[str, Any]] = {}
    for idx, offer in enumerate(store_offers, start=1):
        if isinstance(offer, dict):
            store_map[_offer_merge_key(offer, idx)] = offer

    merged: List[Dict[str, Any]] = []
    used = set()
    for idx, offer in enumerate(default_offers, start=1):
        row = dict(offer or {})
        key = _offer_merge_key(row, idx)
        override = store_map.get(key)
        if isinstance(override, dict):
            for field in ("customer_price", "store_amount", "value_text", "value"):
                if field in override:
                    row[field] = override.get(field)
            if "amount" in override and "store_amount" not in override:
                row["store_amount"] = override.get("amount")
            used.add(key)
        merged.append(row)

    for idx, offer in enumerate(store_offers, start=1):
        key = _offer_merge_key(offer, idx)
        if key not in used and isinstance(offer, dict):
            merged.append(dict(offer))
    return merged


def _offer_base_amount(offer: Dict[str, Any]) -> Optional[float]:
    for field in ("store_amount", "amount", "total", "price", "customer_price"):
        val = money(offer.get(field), None)
        if val is not None:
            return val
    return None


def _build_pricing_map(pricing: Dict[str, Any]) -> Tuple[float, Dict[str, Dict[str, Any]]]:
    percent_default = money(pricing.get("percent_default"), 0.0) or 0.0
    per_map: Dict[str, Dict[str, Any]] = {}
    for row in pricing.get("per_service") or []:
        sid = str(row.get("service_id") or "")
        if not sid:
            continue
        entry: Dict[str, Any] = {"percent": None, "offers": {}}
        if row.get("percent") is not None:
            entry["percent"] = money(row.get("percent"), None)
        for offer in row.get("offers") or []:
            try:
                idx = int(offer.get("index"))
                total = money(offer.get("total"), None)
                if total is not None:
                    entry["offers"][idx] = float(total)
            except Exception:
                continue
        per_map[sid] = entry
    return percent_default, per_map


def _service_state(service: Dict[str, Any]) -> Dict[str, Any]:
    svc_type = str(service.get("type") or "API").upper()
    status = str(service.get("status") or "OPEN").upper()
    availability = str(service.get("availability") or "AVAILABLE").upper()
    return {"can_order": svc_type in {"API", "OFF", "MANUAL"} and status == "OPEN" and availability == "AVAILABLE"}


def _sort_services(raw: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    def key(service: Dict[str, Any]):
        priority = money(service.get("priority"), None)
        display_order = money(service.get("display_order"), None)
        created = service.get("created_at")
        created_ts = -created.timestamp() if isinstance(created, datetime) else 0
        return (
            priority if priority is not None else float("inf"),
            display_order if display_order is not None else float("inf"),
            created_ts,
            _norm(service.get("name")),
        )

    return sorted(raw, key=key)


def validate_agent_code(code: str) -> Optional[Dict[str, Any]]:
    code = re.sub(r"\D+", "", str(code or ""))
    if not re.fullmatch(r"\d{5}", code):
        return None
    record = agent_codes_col.find_one({"agent_code": code, "status": "active"})
    if not record:
        return None
    user_id = to_oid(record.get("user_id"))
    if not user_id:
        return None
    user = users_col.find_one(
        {
            "_id": user_id,
            "role": {"$in": ["agent", "customer"]},
            "$or": [{"deleted": {"$exists": False}}, {"deleted": False}],
        },
        {"first_name": 1, "last_name": 1, "username": 1, "phone": 1, "stage_label": 1, "status": 1, "role": 1, "admin_id": 1},
    )
    if not user or str(user.get("status") or "").lower() == "blocked":
        return None
    admin_id = to_oid(record.get("admin_id")) or _admin_id_for_user(user)
    store = stores_col.find_one(
        {"owner_id": user_id, "status": {"$ne": "deleted"}},
        sort=[("updated_at", -1), ("created_at", -1)],
    )
    if not store:
        return None
    if not admin_id:
        admin_id = to_oid(store.get("admin_id"))
    return {"code": code, "record": record, "agent": user, "admin_id": admin_id, "store": store}


def active_agent_code_exists(code: str) -> bool:
    code = re.sub(r"\D+", "", str(code or ""))
    if not re.fullmatch(r"\d{5}", code):
        return False
    return bool(agent_codes_col.find_one({"agent_code": code, "status": "active"}, {"_id": 1}))


def load_store_services(store_doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    admin_id = to_oid(store_doc.get("admin_id"))
    if not admin_id:
        owner = users_col.find_one({"_id": to_oid(store_doc.get("owner_id"))}, {"_id": 1, "role": 1, "admin_id": 1}) or {}
        admin_id = _admin_id_for_user(owner)
    if not admin_id:
        return []

    scope = store_doc.get("service_scope") or "all"
    service_ids = store_doc.get("service_ids") or []
    query: Dict[str, Any] = {
        "$or": [
            {
                "admin_id": admin_id,
                "_id": {"$ne": SOCIAL_BOOSTING_SERVICE_ID},
                "base_service_id": {"$ne": SOCIAL_BOOSTING_SERVICE_ID},
                "name": {"$ne": SOCIAL_BOOSTING_NAME},
            },
            {"_id": SOCIAL_BOOSTING_SERVICE_ID},
        ],
        "agent_visible": {"$ne": False},
        f"agent_visibility_by_admin.{str(admin_id)}": {"$ne": False},
    }
    if scope == "selected" and service_ids:
        ids = [to_oid(sid) for sid in service_ids]
        ids = [sid for sid in ids if sid]
        query["_id"] = {"$in": ids} if ids else {"$in": []}

    fields = {
        "_id": 1,
        "name": 1,
        "type": 1,
        "status": 1,
        "availability": 1,
        "offers": 1,
        "store_offers": 1,
        "services_offers": 1,
        "base_service_id": 1,
        "service_category": 1,
        "priority": 1,
        "display_order": 1,
        "created_at": 1,
        "unit": 1,
        "default_profit_percent": 1,
        "network": 1,
        "network_id": 1,
        "service_network": 1,
        "provider": 1,
    }
    raw = list(services_col.find(query, fields))
    percent_default, per_map = _build_pricing_map(store_doc.get("pricing") or {})

    services: List[Dict[str, Any]] = []
    for service in _sort_services(raw):
        if str(service.get("_id") or "") == BULK_SMS_SERVICE_ID or _norm(service.get("name")) == "bulk sms":
            continue
        if not _service_state(service).get("can_order"):
            continue
        service_id = str(service.get("_id"))
        pricing = per_map.get(service_id, {})
        svc_percent = pricing.get("percent")
        offer_overrides = pricing.get("offers") or {}
        unit = _service_unit(service)
        offers_out: List[Dict[str, Any]] = []
        for idx, offer in enumerate(_svc_offers(service)):
            base_amount = _offer_base_amount(offer)
            explicit_price = money(offer.get("customer_price"), None)
            if idx in offer_overrides:
                total = round(float(offer_overrides[idx]), 2)
            elif explicit_price is not None:
                total = round(float(explicit_price), 2)
            elif svc_percent is not None:
                total = round(float(base_amount or 0) + (float(base_amount or 0) * float(svc_percent) / 100.0), 2)
            elif percent_default:
                total = round(float(base_amount or 0) + (float(base_amount or 0) * float(percent_default) / 100.0), 2)
            else:
                total = None
            if total is None or base_amount is None:
                continue
            offers_out.append(
                {
                    "index": idx,
                    "service_id": service_id,
                    "service_name": service.get("name") or service.get("network") or "Service",
                    "value": offer.get("value"),
                    "value_text": _value_text(offer, unit),
                    "amount": total,
                    "base_amount": round(float(base_amount), 2),
                }
            )
        if offers_out:
            services.append(
                {
                    "id": service_id,
                    "name": service.get("name") or service.get("network") or "Service",
                    "offers": offers_out,
                }
            )
    return services


def get_service_by_id(store_doc: Dict[str, Any], service_id: str) -> Optional[Dict[str, Any]]:
    for service in load_store_services(store_doc):
        if service.get("id") == service_id:
            return service
    return None


def get_offer_by_index(service: Dict[str, Any], index: int) -> Optional[Dict[str, Any]]:
    for offer in service.get("offers") or []:
        if int(offer.get("index", -1)) == int(index):
            return offer
    return None


def latest_order_for_phone(phone: str) -> Optional[Dict[str, Any]]:
    normalized = normalize_phone(phone)
    return db["orders"].find_one(
        {"items.phone": normalized},
        sort=[("created_at", -1)],
        projection={"order_id": 1, "status": 1, "items": 1, "created_at": 1},
    )
