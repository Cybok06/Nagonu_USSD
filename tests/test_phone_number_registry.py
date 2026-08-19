from __future__ import annotations

import unittest

from phone_number_registry import normalize_registry_phone, order_agent_id, order_phone_numbers


class PhoneNumberRegistryTests(unittest.TestCase):
    def test_phone_formats_share_one_canonical_value(self):
        self.assertEqual(normalize_registry_phone("0241235993"), "0241235993")
        self.assertEqual(normalize_registry_phone("233241235993"), "0241235993")
        self.assertEqual(normalize_registry_phone("+233 24 123 5993"), "0241235993")

    def test_order_phone_numbers_are_deduplicated(self):
        order = {"buyer_phone": "0241235993", "dial_phone": "233241235993", "ussd": {"dial_phone": "+233241235993"}, "items": [{"phone": "0241235993"}, {"phone": "0551234567"}]}
        self.assertEqual(order_phone_numbers(order), ["0241235993", "0551234567"])

    def test_agent_user_id_is_preferred(self):
        self.assertEqual(order_agent_id({"agent_user_id": "agent", "user_id": "user"}), "agent")

    def test_user_id_is_the_standard_order_agent(self):
        self.assertEqual(order_agent_id({"user_id": "user"}), "user")


if __name__ == "__main__":
    unittest.main()
