from __future__ import annotations

import unittest

from ussd_order_indexes import ensure_order_history_indexes


class FakeOrdersCollection:
    def __init__(self, indexes=None):
        self.indexes = indexes or {"_id_": {"key": [("_id", 1)]}}
        self.created = []

    def index_information(self):
        return self.indexes

    def create_index(self, keys, name, background):
        self.created.append((keys, name, background))
        self.indexes[name] = {"key": keys}
        return name


class OrderHistoryIndexTests(unittest.TestCase):
    def test_creates_all_missing_history_indexes(self):
        collection = FakeOrdersCollection()
        results = ensure_order_history_indexes(collection)
        self.assertEqual(len(collection.created), 4)
        self.assertTrue(all(value.startswith("created:") for value in results.values()))

    def test_reuses_equivalent_index_with_a_different_name(self):
        collection = FakeOrdersCollection(
            {
                "_id_": {"key": [("_id", 1)]},
                "legacy_items_phone": {"key": [("items.phone", 1)]},
            }
        )
        results = ensure_order_history_indexes(collection)
        self.assertEqual(results["items.phone"], "existing:legacy_items_phone")
        self.assertEqual(len(collection.created), 3)

    def test_migration_is_idempotent(self):
        collection = FakeOrdersCollection()
        ensure_order_history_indexes(collection)
        collection.created.clear()
        results = ensure_order_history_indexes(collection)
        self.assertEqual(collection.created, [])
        self.assertTrue(all(value.startswith("existing:") for value in results.values()))


if __name__ == "__main__":
    unittest.main()
