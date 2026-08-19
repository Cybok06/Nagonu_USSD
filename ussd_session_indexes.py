from __future__ import annotations

from typing import Any, Dict, Iterable, Tuple
from pymongo import ASCENDING, DESCENDING


def ensure_session_indexes(database) -> Dict[str, str]:
    sessions = database["ussd_sessions"]
    recent = database["ussd_recent_agent_codes"]
    return {
        "active_session": sessions.create_index([("session_id", ASCENDING), ("phone", ASCENDING), ("status", ASCENDING), ("updated_at", DESCENDING)], name="idx_ussd_session_phone_status_updated", background=True),
        "unfinished_session": sessions.create_index([("phone", ASCENDING), ("status", ASCENDING), ("state", ASCENDING), ("updated_at", DESCENDING)], name="idx_ussd_phone_status_state_updated", background=True),
        "recent_agent_code": recent.create_index([("phone", ASCENDING), ("app", ASCENDING), ("last_used_at", DESCENDING)], name="idx_ussd_recent_phone_app_used", background=True),
    }


def migrate(databases: Iterable[Tuple[str, Any]]) -> Dict[str, Dict[str, str]]:
    return {name: ensure_session_indexes(database) for name, database in databases}


if __name__ == "__main__":
    from nagonu_db import db as nagonu_db
    from zico_db import db as zico_db
    for database_name, indexes in migrate((("nagonu", nagonu_db), ("zico", zico_db))).items():
        print(database_name)
        for purpose, index_name in indexes.items():
            print(f"  {purpose}: {index_name}")
