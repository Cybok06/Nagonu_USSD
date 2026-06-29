from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from bson import ObjectId

from zico_db import db
from zico_tenant import to_object_id


afa_settings_col = db["afa_settings"]
settings_col = db["settings"]
users_col = db["users"]

SETTINGS_ID = "AFA_SETTINGS"
DEFAULT_AFA_PRICE = 2.00
ADMIN_LEVELS = ("admin", "super_admin", "super_professional")
ADMIN_LEVEL_LABELS = {
    "admin": "Admin",
    "super_admin": "Super Admin",
    "super_professional": "Professional Admin",
}


def _to_float(value: Any) -> Optional[float]:
    try:
        if isinstance(value, dict):
            for key in ("$numberDouble", "$numberInt", "$numberDecimal", "$numberLong"):
                if key in value:
                    return float(value[key])
        return float(value)
    except Exception:
        return None


def settings_key(admin_oid: ObjectId | None = None) -> str:
    admin_oid = to_object_id(admin_oid)
    return f"{SETTINGS_ID}:{str(admin_oid)}" if admin_oid else SETTINGS_ID


def normalize_admin_level(raw: str | None) -> str:
    lvl = (raw or "").strip().lower()
    if lvl in {"super_admin", "superadmin"}:
        return "super_admin"
    if lvl in {"super_professional", "professional_admin", "professional"}:
        return "super_professional"
    return "admin"


def level_settings_key(level: str) -> str:
    return f"{SETTINGS_ID}:LEVEL:{normalize_admin_level(level)}"


def get_global_afa_settings_doc() -> Optional[Dict[str, Any]]:
    doc = afa_settings_col.find_one({"_id": SETTINGS_ID})
    if doc:
        return doc
    return settings_col.find_one({"key": "afa_settings"}) or settings_col.find_one({"key": "afa"})


def get_afa_level_settings_doc(level: str) -> Optional[Dict[str, Any]]:
    return afa_settings_col.find_one({"_id": level_settings_key(level), "scope": "admin_level"})


def get_afa_settings_doc(
    admin_oid: ObjectId | None = None,
    *,
    fallback_to_global: bool = True,
) -> Optional[Dict[str, Any]]:
    scoped_admin_oid = to_object_id(admin_oid)
    if scoped_admin_oid:
        doc = afa_settings_col.find_one({"_id": settings_key(scoped_admin_oid)})
        if doc:
            if doc.get("admin_id") != scoped_admin_oid:
                afa_settings_col.update_one(
                    {"_id": doc["_id"]},
                    {"$set": {"admin_id": scoped_admin_oid}},
                )
                doc["admin_id"] = scoped_admin_oid
            return doc
    if fallback_to_global:
        return get_global_afa_settings_doc()
    return None


def load_afa_settings(
    admin_oid: ObjectId | None = None,
    *,
    default_price: float = DEFAULT_AFA_PRICE,
) -> Dict[str, Any]:
    defaults: Dict[str, Any] = {
        "price": round(max(0.0, float(default_price)), 2),
        "is_open": True,
        "in_stock": True,
        "status": "OPEN",
        "availability": "AVAILABLE",
        "disabled_reason": "This service is currently unavailable.",
    }

    scoped_admin_oid = to_object_id(admin_oid)
    used_global_fallback = False
    if scoped_admin_oid:
        doc = get_afa_settings_doc(scoped_admin_oid, fallback_to_global=False)
        if not doc:
            defaults["price"] = load_afa_admin_base_price(
                scoped_admin_oid,
                users_col,
                default=default_price,
            )
            doc = get_global_afa_settings_doc()
            used_global_fallback = True
    else:
        doc = get_global_afa_settings_doc()

    if not doc:
        return defaults

    price = _to_float(doc.get("price"))
    if price is not None and not used_global_fallback:
        defaults["price"] = round(max(0.0, float(price)), 2)

    is_open = bool(doc.get("is_open", True))
    in_stock = bool(doc.get("in_stock", True))
    defaults["is_open"] = is_open
    defaults["in_stock"] = in_stock
    defaults["status"] = "OPEN" if is_open else "CLOSED"
    defaults["availability"] = "AVAILABLE" if in_stock else "OUT_OF_STOCK"

    disabled_reason = str(doc.get("disabled_reason") or "").strip()
    if disabled_reason:
        defaults["disabled_reason"] = disabled_reason

    return defaults


def load_afa_price(admin_oid: ObjectId | None = None, *, default: float = 0.0) -> float:
    settings = load_afa_settings(admin_oid, default_price=default)
    try:
        return round(max(0.0, float(settings.get("price") or 0.0)), 2)
    except Exception:
        return round(float(default), 2)


def load_afa_base_price(*, default: float = DEFAULT_AFA_PRICE) -> float:
    doc = get_global_afa_settings_doc()
    price = _to_float((doc or {}).get("price"))
    if price is None:
        price = default
    return round(max(0.0, float(price or 0.0)), 2)


def load_afa_level_price(level: str, *, default: float | None = None) -> float:
    doc = get_afa_level_settings_doc(level)
    price = _to_float((doc or {}).get("price"))
    if price is None:
        price = load_afa_base_price(default=DEFAULT_AFA_PRICE) if default is None else default
    return round(max(0.0, float(price or 0.0)), 2)


def load_afa_level_prices(*, default: float | None = None) -> Dict[str, float]:
    return {level: load_afa_level_price(level, default=default) for level in ADMIN_LEVELS}


def save_afa_level_prices(level_prices: Dict[str, Any], *, min_price: float = 0.0) -> Dict[str, float]:
    now = datetime.utcnow()
    saved: Dict[str, float] = {}
    floor = round(max(0.0, float(min_price or 0.0)), 2)
    for raw_level, raw_price in (level_prices or {}).items():
        level = normalize_admin_level(raw_level)
        if level not in ADMIN_LEVELS:
            continue
        price = _to_float(raw_price)
        if price is None:
            continue
        price = round(max(floor, float(price)), 2)
        afa_settings_col.update_one(
            {"_id": level_settings_key(level), "scope": "admin_level"},
            {
                "$set": {
                    "scope": "admin_level",
                    "admin_level": level,
                    "price": price,
                    "updated_at": now,
                }
            },
            upsert=True,
        )
        saved[level] = price
    return saved


def load_afa_admin_base_price(
    admin_oid: ObjectId | None,
    users_col,
    *,
    default: float = DEFAULT_AFA_PRICE,
) -> float:
    scoped_admin_oid = to_object_id(admin_oid)
    if not scoped_admin_oid:
        return load_afa_base_price(default=default)

    user = users_col.find_one({"_id": scoped_admin_oid}, {"role": 1, "admin_level": 1})
    if (user or {}).get("role") == "main_admin":
        return load_afa_base_price(default=default)

    level = normalize_admin_level((user or {}).get("admin_level"))
    return load_afa_level_price(level, default=load_afa_base_price(default=default))


def ensure_admin_afa_settings(
    admin_oid: ObjectId | None,
    *,
    default_price: float = DEFAULT_AFA_PRICE,
) -> Dict[str, Any]:
    scoped_admin_oid = to_object_id(admin_oid)
    if not scoped_admin_oid:
        doc = get_global_afa_settings_doc()
        if doc:
            return doc
        return {
            "_id": SETTINGS_ID,
            "price": round(max(0.0, float(default_price)), 2),
            "is_open": True,
            "in_stock": True,
            "updated_at": datetime.utcnow(),
        }

    existing = afa_settings_col.find_one({"_id": settings_key(scoped_admin_oid)})
    if existing:
        if existing.get("admin_id") != scoped_admin_oid:
            afa_settings_col.update_one(
                {"_id": existing["_id"]},
                {"$set": {"admin_id": scoped_admin_oid}},
            )
            existing["admin_id"] = scoped_admin_oid
        return existing

    seeded = load_afa_settings(default_price=default_price)
    doc: Dict[str, Any] = {
        "_id": settings_key(scoped_admin_oid),
        "admin_id": scoped_admin_oid,
        "price": round(max(0.0, float(default_price)), 2),
        "is_open": bool(seeded.get("is_open", True)),
        "in_stock": bool(seeded.get("in_stock", True)),
        "updated_at": datetime.utcnow(),
    }
    if seeded.get("disabled_reason"):
        doc["disabled_reason"] = seeded["disabled_reason"]

    afa_settings_col.update_one(
        {"_id": doc["_id"]},
        {"$setOnInsert": doc},
        upsert=True,
    )
    return (
        afa_settings_col.find_one({"_id": doc["_id"]})
        or doc
    )
