from __future__ import annotations

import unittest

from merge_zico_phone_numbers import UNIQUE_INDEX_NAME


class MergeZicoPhoneNumbersTests(unittest.TestCase):
    def test_merge_uses_the_registry_unique_index(self):
        self.assertEqual(UNIQUE_INDEX_NAME, "uq_phone_numbers_phone_number")


if __name__ == "__main__":
    unittest.main()
