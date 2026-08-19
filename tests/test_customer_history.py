from __future__ import annotations

import unittest
from unittest.mock import Mock

from ussd_customer_history import (
    collection_has_order_history,
    has_order_history,
    order_phone_query,
    phone_variants,
    should_start_guest_checker,
)


class CustomerHistoryTests(unittest.TestCase):
    def test_ghana_phone_variants_cover_stored_formats(self):
        self.assertEqual(
            phone_variants("233241235993"),
            ["0241235993", "233241235993", "+233241235993"],
        )

    def test_query_checks_order_item_and_ussd_phone_fields(self):
        query = order_phone_query("0241235993")
        fields = {next(iter(clause)) for clause in query["$or"]}
        self.assertEqual(fields, {"items.phone", "buyer_phone", "dial_phone", "ussd.dial_phone"})

    def test_nagonu_history_is_accepted(self):
        nagonu = Mock()
        zico = Mock()
        nagonu.find_one.return_value = {"_id": "order"}
        self.assertTrue(has_order_history(nagonu, zico, "0241235993"))
        zico.find_one.assert_not_called()

    def test_single_collection_history_check_queries_only_that_collection(self):
        nagonu = Mock()
        nagonu.find_one.return_value = None
        self.assertFalse(collection_has_order_history(nagonu, "0241235993"))
        nagonu.find_one.assert_called_once()

    def test_zico_history_is_accepted(self):
        nagonu = Mock()
        zico = Mock()
        nagonu.find_one.return_value = None
        zico.find_one.return_value = {"_id": "order"}
        self.assertTrue(has_order_history(nagonu, zico, "0241235993"))

    def test_only_new_customer_without_history_starts_guest_flow(self):
        self.assertTrue(should_start_guest_checker(is_new_session=True, has_session=False, has_history=False))
        self.assertFalse(should_start_guest_checker(is_new_session=True, has_session=False, has_history=True))
        self.assertFalse(should_start_guest_checker(is_new_session=False, has_session=False, has_history=False))
        self.assertFalse(should_start_guest_checker(is_new_session=True, has_session=True, has_history=False))


if __name__ == "__main__":
    unittest.main()
