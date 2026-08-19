from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from bson import ObjectId
from flask import request

from zico_db import db

activity_logs_col = db["activity_logs"]
users_col = db["users"]


def _to_objectid(value: Any) -> Optional[ObjectId]:
    if isinstance(value, ObjectId):
        return value
    if not value:
        return None
    try:
        return ObjectId(str(value))
    except Exception:
        return None


def _actor_name(actor_id: Optional[ObjectId]) -> str:
    if not actor_id:
        return "System"
    try:
        u = users_col.find_one({"_id": actor_id}, {"full_name": 1, "name": 1, "first_name": 1, "last_name": 1, "username": 1, "email": 1})
    except Exception:
        u = None
    if not u:
        return "User"
    for key in ("full_name", "name"):
        if u.get(key):
            return str(u[key]).strip()
    first = (u.get("first_name") or "").strip()
    last = (u.get("last_name") or "").strip()
    if first or last:
        return (first + " " + last).strip()
    if u.get("username"):
        return str(u["username"]).strip()
    if u.get("email"):
        return str(u["email"]).split("@", 1)[0]
    return "User"


def _get_ip() -> str:
    try:
        xfwd = (request.headers.get("X-Forwarded-For") or "").strip()
        if xfwd:
            return xfwd.split(",")[0].strip()
        xreal = (request.headers.get("X-Real-IP") or "").strip()
        if xreal:
            return xreal
        return request.remote_addr or ""
    except Exception:
        return ""


def log_activity(
    action: str,
    *,
    actor_id: Any | None = None,
    actor_role: str | None = None,
    admin_id: Any | None = None,
    target_type: str | None = None,
    target_id: Any | None = None,
    message: str | None = None,
    meta: Dict[str, Any] | None = None,
) -> None:
    """
    Write a structured activity log entry. Never raises.
    """
    try:
        actor_oid = _to_objectid(actor_id)
        admin_oid = _to_objectid(admin_id)

        doc = {
            "action": (action or "").strip() or "activity",
            "actor_id": actor_oid,
            "actor_role": (actor_role or "").strip().lower() or None,
            "actor_name": _actor_name(actor_oid),
            "admin_id": admin_oid,
            "target_type": (target_type or "").strip() or None,
            "target_id": str(target_id) if target_id is not None else None,
            "message": message or None,
            "meta": meta or {},
            "ip": _get_ip(),
            "created_at": datetime.utcnow(),
        }
        activity_logs_col.insert_one(doc)
    except Exception:
        pass
