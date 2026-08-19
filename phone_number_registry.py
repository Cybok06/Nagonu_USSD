from __future__ import annotations

import argparse
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Iterable, List, Mapping, Tuple

from pymongo import ASCENDING, UpdateOne

COLLECTION_NAME = "phone_numbers"
BATCH_SIZE = 1000


def normalize_registry_phone(value: Any) -> str:
    digits = re.sub(r"\D+", "", str(value or ""))
    if re.fullmatch(r"233\d{9}", digits):
        return "0" + digits[3:]
    if re.fullmatch(r"\d{9}", digits):
        return "0" + digits
    if re.fullmatch(r"0\d{9}", digits):
        return digits
    return ""


def order_agent_id(order: Mapping[str, Any]) -> str:
    return str(order.get("agent_user_id") or order.get("user_id") or order.get("store_owner_id") or "").strip()


def order_phone_numbers(order: Mapping[str, Any]) -> List[str]:
    candidates: List[Any] = [
        order.get("buyer_phone"),
        order.get("dial_phone"),
        (order.get("ussd") or {}).get("dial_phone") if isinstance(order.get("ussd"), Mapping) else None,
    ]
    for item in order.get("items") or []:
        if isinstance(item, Mapping):
            candidates.append(item.get("phone"))
    normalized = [normalize_registry_phone(value) for value in candidates]
    return list(dict.fromkeys(phone for phone in normalized if phone))


def ensure_registry_index(database) -> None:
    database[COLLECTION_NAME].create_index(
        [("phone_number", ASCENDING)], name="uq_phone_numbers_phone_number", unique=True
    )


def _flush(collection, operations: Dict[str, UpdateOne]) -> int:
    if not operations:
        return 0
    collection.bulk_write(list(operations.values()), ordered=False)
    count = len(operations)
    operations.clear()
    return count


def export_order_phone_numbers(database) -> Dict[str, int]:
    """Idempotently export one normalized phone per document; latest agent wins."""
    ensure_registry_index(database)
    registry = database[COLLECTION_NAME]
    projection = {
        "_id": 1, "user_id": 1, "agent_user_id": 1, "store_owner_id": 1,
        "items.phone": 1, "buyer_phone": 1, "dial_phone": 1,
        "ussd.dial_phone": 1, "created_at": 1,
    }
    cursor = database["orders"].find({}, projection).sort([("created_at", ASCENDING), ("_id", ASCENDING)])
    operations: Dict[str, UpdateOne] = {}
    orders_scanned = 0
    phone_writes = 0
    for order in cursor:
        orders_scanned += 1
        agent_id = order_agent_id(order)
        for phone_number in order_phone_numbers(order):
            operations[phone_number] = UpdateOne(
                {"phone_number": phone_number},
                {"$set": {"phone_number": phone_number, "agent_id": agent_id}},
                upsert=True,
            )
            if len(operations) >= BATCH_SIZE:
                phone_writes += _flush(registry, operations)
    phone_writes += _flush(registry, operations)
    return {
        "orders_scanned": orders_scanned,
        "phone_writes": phone_writes,
        "unique_phone_numbers": registry.count_documents({}),
    }


def phone_number_exists(collection, phone: Any) -> bool:
    normalized = normalize_registry_phone(phone)
    return bool(normalized and collection.find_one({"phone_number": normalized}, {"_id": 1}))


def phone_number_exists_in_either(nagonu_collection, zico_collection, phone: Any) -> bool:
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="ussd-phone-registry") as executor:
        nagonu_result = executor.submit(phone_number_exists, nagonu_collection, phone)
        zico_result = executor.submit(phone_number_exists, zico_collection, phone)
        return bool(nagonu_result.result() or zico_result.result())


def migrate(databases: Iterable[Tuple[str, Any]]) -> Dict[str, Dict[str, int]]:
    return {name: export_order_phone_numbers(database) for name, database in databases}


if __name__ == "__main__":
    argparse.ArgumentParser(description="Export unique order phones into indexed registries.").parse_args()
    from nagonu_db import db as nagonu_db
    from zico_db import db as zico_db
    results = migrate((("nagonu", nagonu_db), ("zico", zico_db)))
    for database_name, stats in results.items():
        print(f"{database_name}: orders_scanned={stats['orders_scanned']} phone_writes={stats['phone_writes']} unique_phone_numbers={stats['unique_phone_numbers']}")
