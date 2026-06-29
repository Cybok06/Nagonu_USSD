from __future__ import annotations

from typing import Any, Optional
from bson import ObjectId


ADMIN_ROLES = {"admin", "superadmin", "main_admin", "super_admin", "professional_admin", "super_professional"}


def to_object_id(value: Any) -> Optional[ObjectId]:
    if isinstance(value, ObjectId):
        return value
    if not value:
        return None
    try:
        return ObjectId(str(value))
    except Exception:
        return None


def is_admin_role(role: str | None) -> bool:
    return (role or "").strip().lower() in ADMIN_ROLES


def current_admin_id_from_session(session_obj) -> Optional[ObjectId]:
    """
    Returns current tenant owner id for the active session.
    - Admin-like roles own themselves.
    - Customer/agent sessions may carry admin_id set at login.
    """
    role = (session_obj.get("role") or "").strip().lower()
    if is_admin_role(role):
        return to_object_id(session_obj.get("user_id"))
    return to_object_id(session_obj.get("admin_id"))


def resolve_admin_id_from_user_doc(user_doc: dict | None) -> Optional[ObjectId]:
    """
    Resolve tenant owner for a user document:
    - Admin-like user -> self (_id)
    - Non-admin -> explicit admin_id if present
    """
    if not user_doc:
        return None
    role = (user_doc.get("role") or "").strip().lower()
    if is_admin_role(role):
        return to_object_id(user_doc.get("_id"))
    return to_object_id(user_doc.get("admin_id"))


def resolve_admin_id_for_user_id(users_col, user_id: Any) -> Optional[ObjectId]:
    oid = to_object_id(user_id)
    if not oid:
        return None
    user_doc = users_col.find_one({"_id": oid}, {"_id": 1, "role": 1, "admin_id": 1})
    return resolve_admin_id_from_user_doc(user_doc)


def admin_scoped_query(base_query: dict | None, admin_oid: ObjectId | None, field: str = "admin_id") -> dict:
    q = dict(base_query or {})
    if admin_oid:
        q[field] = admin_oid
    return q
