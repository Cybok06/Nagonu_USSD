from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from pymongo.errors import DuplicateKeyError

from nagonu_db import db


events_col = db["ussd_callback_events"]
_index_checked = False


def _ensure_index() -> None:
    global _index_checked
    if _index_checked:
        return
    try:
        events_col.create_index([("provider", 1), ("session_id", 1), ("created_at", -1)], background=True)
    except Exception:
        # Legacy duplicates or temporary DB failure must not prevent startup.
        pass
    _index_checked = True


def input_hash(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def make_key(provider: str, session_id: str, phone: str, state_before: str, text: str) -> str:
    material = "\x1f".join((provider, session_id, phone, state_before, input_hash(text)))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def find_replay(
    provider: str,
    session_id: str,
    phone: str,
    state_now: str,
    text: str,
) -> Optional[Dict[str, Any]]:
    _ensure_index()
    return events_col.find_one(
        {
            "provider": provider,
            "session_id": session_id,
            "phone": phone,
            "input_hash": input_hash(text),
            "state_after": state_now,
            "status": "completed",
            # Moolre supplies no event/turn ID. Restrict response replay to the
            # immediate retry window so a legitimate later selection with the
            # same digit in the next menu is not suppressed.
            "updated_at": {"$gte": datetime.utcnow() - timedelta(seconds=2)},
        },
        sort=[("created_at", -1)],
    )


def claim(provider: str, session_id: str, phone: str, state_before: str, text: str) -> tuple[str, bool]:
    _ensure_index()
    key = make_key(provider, session_id, phone, state_before, text)
    now = datetime.utcnow()
    try:
        events_col.insert_one(
            {
                "_id": key,
                "idempotency_key": key,
                "provider": provider,
                "session_id": session_id,
                "phone": phone,
                "input_hash": input_hash(text),
                "state_before": state_before,
                "status": "processing",
                "created_at": now,
                "updated_at": now,
            }
        )
        return key, True
    except DuplicateKeyError:
        return key, False


def complete(key: str, application: str, state_after: str, response: Dict[str, Any]) -> None:
    events_col.update_one(
        {"_id": key},
        {
            "$set": {
                "application": application,
                "state_after": state_after,
                "response": response,
                "continue_session": bool(response.get("reply")),
                "status": "completed",
                "updated_at": datetime.utcnow(),
            }
        },
    )


def fail(key: str) -> None:
    events_col.update_one(
        {"_id": key},
        {"$set": {"status": "failed", "updated_at": datetime.utcnow()}},
    )
