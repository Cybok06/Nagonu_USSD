from __future__ import annotations

import ast
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from bson import ObjectId

from nagonu_db import db


agent_codes_col = db["agent_codes"]
services_col = db["services"]
stores_col = db["stores"]
users_col = db["users"]

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


def _service_unit(service: Dict[str, Any]) -> str:
    unit = _norm(service.get("unit") or service.get("service_unit"))
    name = _norm(service.get("name") or service.get("network"))
    if "minute" in unit or "mins" in unit or "talk" in name:
        return "minutes"
    return "data"


def _parse_value_field(value: Any) -> Any:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return value
    text = value.strip()
    if text.startswith("{") and text.endswith("}"):
        try:
            parsed = ast.literal_eval(text)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return value
    return value


def _extract_volume(value: Any, unit: str) -> Optional[float]:
    if isinstance(value, dict):
        for key in ("volume", "mb", "shared_bundle", "minutes", "mins"):
            raw = value.get(key)
            if raw not in (None, "", []):
                return _extract_volume(str(raw), unit)
        return None

    if isinstance(value, (int, float)):
        return float(value)

    if isinstance(value, str):
        text = _PKG_TAIL.sub("", value).strip()
        if unit == "minutes":
            found = _MIN.search(text)
            if found:
                return float(found.group(1))
            if _NUM.match(text):
                return float(text)
            return None

        found = _GB.search(text)
        if found:
            return float(found.group(1)) * 1000.0
        found = _MB.search(text)
        if found:
            return float(found.group(1))
        if _NUM.match(text):
            return float(text)
    return None


def _format_volume(value: Optional[float], unit: str) -> str:
    if value is None:
        return "-"
    if unit == "minutes":
        return f"{int(round(value))} mins"
    if value >= 1000:
        gb = value / 1000.0
        return f"{int(gb)}GB" if abs(gb - int(gb)) < 1e-9 else f"{gb:.2f}GB"
    return f"{int(value)}MB"


def _value_text(value: Any, unit: str) -> str:
    parsed = _parse_value_field(value)
    volume = _extract_volume(parsed, unit)
    if volume is not None:
        return _format_volume(volume, unit)
    if isinstance(value, str):
        return _PKG_TAIL.sub("", value).strip() or "-"
    return str(value or "-")


def _svc_offers(service: Dict[str, Any]) -> List[Dict[str, Any]]:
    store_offers = service.get("store_offers")
    if isinstance(store_offers, list) and store_offers:
        return store_offers
    offers = service.get("offers")
    return offers if isinstance(offers, list) else []


def _offer_base_amount(offer: Dict[str, Any]) -> Optional[float]:
    val = money(offer.get("store_amount"), None)
    if val is not None:
        return val
    return money(offer.get("amount"), None)


def _build_pricing_map(pricing: Dict[str, Any]) -> Tuple[float, Dict[str, Dict[str, Any]]]:
    percent_default = money((pricing or {}).get("percent_default"), 0.0) or 0.0
    per_map: Dict[str, Dict[str, Any]] = {}
    for entry in (pricing or {}).get("per_service") or []:
        sid = str(entry.get("service_id") or "")
        if not sid:
            continue
        out: Dict[str, Any] = {"percent": None, "offers": {}}
        if entry.get("percent") is not None:
            out["percent"] = money(entry.get("percent"), None)
        for offer in entry.get("offers") or []:
            try:
                idx = int(offer.get("index"))
                total = money(offer.get("total"), None)
                if total is not None:
                    out["offers"][idx] = float(total)
            except Exception:
                continue
        per_map[sid] = out
    return percent_default, per_map


def _service_state(service: Dict[str, Any]) -> Dict[str, Any]:
    svc_type = str(service.get("type") or "API").upper()
    status = str(service.get("status") or "OPEN").upper()
    availability = str(service.get("availability") or "AVAILABLE").upper()
    can_order = svc_type in {"API", "OFF", "MANUAL"} and status == "OPEN" and availability == "AVAILABLE"
    return {"can_order": can_order, "type": svc_type, "status": status, "availability": availability}


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
        {"_id": user_id, "role": "customer", "$or": [{"deleted": {"$exists": False}}, {"deleted": False}]},
        {"first_name": 1, "last_name": 1, "username": 1, "phone": 1, "stage_label": 1, "status": 1},
    )
    if not user or user.get("status") == "blocked":
        return None
    store = stores_col.find_one(
        {"owner_id": user_id, "status": {"$ne": "deleted"}},
        sort=[("updated_at", -1), ("created_at", -1)],
    )
    if not store:
        return None
    return {"code": code, "record": record, "agent": user, "store": store}


def load_store_services(store_doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    scope = store_doc.get("service_scope") or "all"
    service_ids = store_doc.get("service_ids") or []
    query: Dict[str, Any] = {}
    if scope == "selected" and service_ids:
        ids = [to_oid(sid) for sid in service_ids]
        ids = [sid for sid in ids if sid]
        query = {"_id": {"$in": ids}} if ids else {"_id": {"$in": []}}

    fields = {
        "_id": 1,
        "name": 1,
        "type": 1,
        "status": 1,
        "availability": 1,
        "offers": 1,
        "store_offers": 1,
        "store_offers_profit": 1,
        "service_category": 1,
        "priority": 1,
        "display_order": 1,
        "created_at": 1,
        "unit": 1,
        "default_profit_percent": 1,
        "network": 1,
        "network_id": 1,
    }
    raw = list(services_col.find(query, fields)) if query else list(services_col.find({}, fields))
    percent_default, per_map = _build_pricing_map(store_doc.get("pricing") or {})

    services: List[Dict[str, Any]] = []
    for service in _sort_services(raw):
        state = _service_state(service)
        if not state.get("can_order"):
            continue
        unit = _service_unit(service)
        service_id = str(service.get("_id"))
        pricing = per_map.get(service_id, {})
        svc_percent = pricing.get("percent")
        offer_overrides = pricing.get("offers") or {}
        offers_out: List[Dict[str, Any]] = []
        for idx, offer in enumerate(_svc_offers(service)):
            base_amount = _offer_base_amount(offer)
            if base_amount is None:
                continue
            if idx in offer_overrides:
                total = round(float(offer_overrides[idx]), 2)
            else:
                pct = svc_percent if svc_percent is not None else percent_default
                total = round(base_amount + (base_amount * float(pct or 0.0) / 100.0), 2)
            offers_out.append(
                {
                    "index": idx,
                    "service_id": service_id,
                    "service_name": service.get("name") or service.get("network") or "Service",
                    "value": offer.get("value"),
                    "value_text": _value_text(offer.get("value"), unit),
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
