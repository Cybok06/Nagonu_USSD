from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from zico_db import db


sessions_col = db["ussd_sessions"]
recent_codes_col = db["ussd_recent_agent_codes"]
pending_orders_col = db["ussd_pending_orders"]
logs_col = db["ussd_request_logs"]

SESSION_TTL_MINUTES = 30
RECENT_CODE_DAYS = 30


def now_utc() -> datetime:
    return datetime.utcnow()


def log_request(app_name: str, session_id: str, phone: str, text: str, response: str) -> None:
    try:
        logs_col.insert_one(
            {
                "app": app_name,
                "session_id": session_id,
                "phone": phone,
                "text": text,
                "response": response,
                "created_at": now_utc(),
            }
        )
    except Exception:
        pass


def get_session(session_id: str, phone: str) -> Optional[Dict[str, Any]]:
    cutoff = now_utc() - timedelta(minutes=SESSION_TTL_MINUTES)
    return sessions_col.find_one(
        {
            "session_id": session_id,
            "phone": phone,
            "status": "active",
            "updated_at": {"$gte": cutoff},
        }
    )


def save_session(session_id: str, phone: str, state: str, data: Dict[str, Any]) -> None:
    when = now_utc()
    sessions_col.update_one(
        {"session_id": session_id, "phone": phone},
        {
            "$set": {
                "session_id": session_id,
                "phone": phone,
                "state": state,
                "data": data,
                "status": "active",
                "updated_at": when,
            },
            "$setOnInsert": {"created_at": when},
        },
        upsert=True,
    )


def end_session(session_id: str, phone: str) -> None:
    sessions_col.update_one(
        {"session_id": session_id, "phone": phone},
        {"$set": {"status": "ended", "updated_at": now_utc()}},
    )


def get_unfinished_session(phone: str) -> Optional[Dict[str, Any]]:
    cutoff = now_utc() - timedelta(minutes=SESSION_TTL_MINUTES)
    return sessions_col.find_one(
        {
            "phone": phone,
            "status": "active",
            "state": {"$in": ["payment_pending", "otp_pending"]},
            "updated_at": {"$gte": cutoff},
        },
        sort=[("updated_at", -1)],
    )


def get_recent_agent_code(phone: str, app_name: str) -> Optional[Dict[str, Any]]:
    cutoff = now_utc() - timedelta(days=RECENT_CODE_DAYS)
    return recent_codes_col.find_one(
        {"phone": phone, "app": app_name, "last_used_at": {"$gte": cutoff}},
        sort=[("last_used_at", -1)],
    )


def remember_agent_code(phone: str, app_name: str, agent_code: str, user_id: Any, store_slug: str) -> None:
    when = now_utc()
    recent_codes_col.update_one(
        {"phone": phone, "app": app_name},
        {
            "$set": {
                "agent_code": agent_code,
                "user_id": user_id,
                "store_slug": store_slug,
                "last_used_at": when,
            },
            "$setOnInsert": {"created_at": when},
        },
        upsert=True,
    )


def create_pending_order(data: Dict[str, Any]) -> str:
    when = now_utc()
    doc = {
        **data,
        "status": "payment_not_started",
        "created_at": when,
        "updated_at": when,
    }
    result = pending_orders_col.insert_one(doc)
    pending_orders_col.update_one({"_id": result.inserted_id}, {"$set": {"id": str(result.inserted_id)}})
    return str(result.inserted_id)
