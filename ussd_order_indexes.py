from __future__ import annotations

from typing import Any, Dict, Iterable, Tuple

from pymongo import ASCENDING


ORDER_HISTORY_INDEXES: Tuple[Tuple[str, str], ...] = (
    ("items.phone", "idx_orders_items_phone"),
    ("buyer_phone", "idx_orders_buyer_phone"),
    ("dial_phone", "idx_orders_dial_phone"),
    ("ussd.dial_phone", "idx_orders_ussd_dial_phone"),
)


def _key_pattern(index: Dict[str, Any]) -> Tuple[Tuple[str, Any], ...]:
    return tuple(tuple(part) for part in index.get("key") or ())


def ensure_order_history_indexes(orders_collection) -> Dict[str, str]:
    """Create the indexes required by the first-dial phone-history `$or` query.

    MongoDB can combine one index per `$or` branch, but only when every branch
    has usable index coverage. Existing indexes are detected by key pattern so
    deployments with older index names do not attempt to create duplicates.
    """
    existing = {
        _key_pattern(spec): name
        for name, spec in orders_collection.index_information().items()
    }
    results: Dict[str, str] = {}

    for field, preferred_name in ORDER_HISTORY_INDEXES:
        pattern = ((field, ASCENDING),)
        existing_name = existing.get(pattern)
        if existing_name:
            results[field] = f"existing:{existing_name}"
            continue

        created_name = orders_collection.create_index(
            [(field, ASCENDING)],
            name=preferred_name,
            background=True,
        )
        results[field] = f"created:{created_name}"
        existing[pattern] = created_name

    return results


def migrate(databases: Iterable[Tuple[str, Any]]) -> Dict[str, Dict[str, str]]:
    return {
        database_name: ensure_order_history_indexes(database["orders"])
        for database_name, database in databases
    }


if __name__ == "__main__":
    from nagonu_db import db as nagonu_db
    from zico_db import db as zico_db

    applied = migrate((("nagonu", nagonu_db), ("zico", zico_db)))
    for database_name, indexes in applied.items():
        print(database_name)
        for field, result in indexes.items():
            print(f"  {field}: {result}")
