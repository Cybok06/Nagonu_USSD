from __future__ import annotations

from typing import Dict, List

from pymongo import ASCENDING, UpdateOne

from phone_number_registry import COLLECTION_NAME, normalize_registry_phone

BATCH_SIZE = 1000
UNIQUE_INDEX_NAME = "uq_phone_numbers_phone_number"


def ensure_unique_phone_index(collection) -> None:
    collection.create_index(
        [("phone_number", ASCENDING)], name=UNIQUE_INDEX_NAME, unique=True
    )


def merge_zico_into_nagonu(nagonu_database, zico_database) -> Dict[str, int]:
    """Copy missing Zico phones into Nagonu without replacing Nagonu records."""
    target = nagonu_database[COLLECTION_NAME]
    source = zico_database[COLLECTION_NAME]
    ensure_unique_phone_index(target)
    scanned = attempted = inserted = 0
    operations: List[UpdateOne] = []

    def flush() -> None:
        nonlocal attempted, inserted
        if not operations:
            return
        result = target.bulk_write(operations, ordered=False)
        attempted += len(operations)
        inserted += int(result.upserted_count)
        operations.clear()

    for document in source.find({}, {"_id": 0, "phone_number": 1, "agent_id": 1}):
        scanned += 1
        phone_number = normalize_registry_phone(document.get("phone_number"))
        if not phone_number:
            continue
        operations.append(
            UpdateOne(
                {"phone_number": phone_number},
                {"$setOnInsert": {"phone_number": phone_number, "agent_id": str(document.get("agent_id") or "")}},
                upsert=True,
            )
        )
        if len(operations) >= BATCH_SIZE:
            flush()
    flush()
    return {
        "zico_scanned": scanned,
        "writes_attempted": attempted,
        "inserted": inserted,
        "duplicates_preserved": attempted - inserted,
        "nagonu_total": target.count_documents({}),
    }


if __name__ == "__main__":
    from nagonu_db import db as nagonu_db
    from zico_db import db as zico_db
    stats = merge_zico_into_nagonu(nagonu_db, zico_db)
    print(
        "Zico -> Nagonu phone merge complete: "
        f"zico_scanned={stats['zico_scanned']} inserted={stats['inserted']} "
        f"duplicates_preserved={stats['duplicates_preserved']} nagonu_total={stats['nagonu_total']}"
    )
