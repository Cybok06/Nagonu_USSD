from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from nagonu_db import db

routes_col = db["ussd_session_routes"]
ROUTE_TTL_MINUTES = 30


def route_id(session_id: str, phone: str) -> str:
    value = f"{str(session_id).strip()}\x1f{str(phone).strip()}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def get_session_route(session_id: str, phone: str) -> Optional[Dict[str, Any]]:
    cutoff = datetime.utcnow() - timedelta(minutes=ROUTE_TTL_MINUTES)
    return routes_col.find_one(
        {"_id": route_id(session_id, phone), "updated_at": {"$gte": cutoff}},
        {"application": 1, "turn": 1},
    )


def save_session_route(session_id: str, phone: str, application: str, turn: int) -> None:
    now = datetime.utcnow()
    routes_col.update_one(
        {"_id": route_id(session_id, phone)},
        {"$set": {"session_id": str(session_id), "phone": str(phone), "application": str(application or "unknown"), "turn": max(0, int(turn)), "updated_at": now}, "$setOnInsert": {"created_at": now}},
        upsert=True,
    )
