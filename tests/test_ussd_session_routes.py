from __future__ import annotations

import unittest
from ussd_session_routes import route_id


class SessionRouteTests(unittest.TestCase):
    def test_route_id_is_stable_and_scoped_to_phone(self):
        first = route_id("session-1", "0241235993")
        self.assertEqual(first, route_id("session-1", "0241235993"))
        self.assertNotEqual(first, route_id("session-1", "0551234567"))

    def test_route_id_does_not_expose_session_or_phone(self):
        value = route_id("session-1", "0241235993")
        self.assertNotIn("session-1", value)
        self.assertNotIn("0241235993", value)
        self.assertEqual(len(value), 64)


if __name__ == "__main__":
    unittest.main()
