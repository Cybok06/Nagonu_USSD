from __future__ import annotations

import json
import unittest

from ussd_providers.base import (
    NormalizedUSSDResponse,
    USSDMalformedRequest,
    USSDUnsupportedMediaType,
    normalize_menu_response,
)
from ussd_providers.moolre import MOOLRE_USSD_GUIDE_URL, MoolreUSSDAdapter


class NormalizedResponseTests(unittest.TestCase):
    def test_continue_response_is_provider_neutral(self):
        result = normalize_menu_response("CON Select service")
        self.assertEqual(result.message, "Select service")
        self.assertTrue(result.continue_session)

    def test_end_response_is_provider_neutral(self):
        result = normalize_menu_response("END Goodbye")
        self.assertEqual(result.message, "Goodbye")
        self.assertFalse(result.continue_session)

    def test_unprefixed_response_ends_safely(self):
        result = normalize_menu_response("Unexpected response")
        self.assertEqual(result.message, "Unexpected response")
        self.assertFalse(result.continue_session)


class MoolreAdapterTests(unittest.TestCase):
    def test_official_guide_url_is_recorded(self):
        self.assertEqual(MOOLRE_USSD_GUIDE_URL, "https://docs.moolre.com/guides/ussd")

    def _parse(self, payload):
        return MoolreUSSDAdapter().parse_request(
            method="POST",
            content_type="application/json",
            headers={},
            body=b"{}",
            json_payload=payload,
            form_payload={},
            query_payload={},
        )

    def test_first_callback_uses_data_as_agent_code(self):
        result = self._parse(
            {
                "sessionId": "3-17074657982460137",
                "new": True,
                "msisdn": "233241235993",
                "network": 3,
                "message": "",
                "extension": "109",
                "data": "11005",
            }
        )
        self.assertEqual(result.session_id, "3-17074657982460137")
        self.assertEqual(result.phone, "0241235993")
        self.assertEqual(result.text, "11005")
        self.assertTrue(result.is_new_session)
        self.assertEqual(result.shortcode, "109")
        self.assertEqual(result.network, "3")

    def test_continuing_callback_uses_latest_message(self):
        result = self._parse(
            {
                "sessionId": "session-2",
                "new": False,
                "msisdn": "233241235993",
                "network": 3,
                "message": "1",
                "extension": "109",
                "data": "11005",
            }
        )
        self.assertEqual(result.text, "1")
        self.assertFalse(result.is_new_session)

    def test_continue_response_uses_only_moolre_fields(self):
        body, status, content_type = MoolreUSSDAdapter().format_response(
            NormalizedUSSDResponse("Select service", True)
        )
        self.assertEqual(body, {"message": "Select service", "reply": True})
        self.assertEqual(status, 200)
        self.assertEqual(content_type, "application/json")

    def test_end_response_uses_only_moolre_fields(self):
        body, _, _ = MoolreUSSDAdapter().format_response(
            NormalizedUSSDResponse("Goodbye", False)
        )
        self.assertEqual(body, {"message": "Goodbye", "reply": False})

    def test_non_json_is_rejected(self):
        with self.assertRaises(USSDUnsupportedMediaType):
            MoolreUSSDAdapter().parse_request(
                method="POST",
                content_type="application/x-www-form-urlencoded",
                headers={},
                body=b"",
                json_payload=None,
                form_payload={},
                query_payload={},
            )

    def test_valid_json_body_with_text_plain_content_type_is_accepted(self):
        payload = {
            "sessionId": "3-17074657982460137",
            "new": True,
            "msisdn": "233241235993",
            "network": 3,
            "message": "",
            "extension": "109",
            "data": "11005",
        }
        result = MoolreUSSDAdapter().parse_request(
            method="POST",
            content_type="text/plain",
            headers={},
            body=json.dumps(payload).encode("utf-8"),
            json_payload=None,
            form_payload={},
            query_payload={},
        )
        self.assertEqual(result.session_id, "3-17074657982460137")
        self.assertEqual(result.text, "11005")

    def test_documented_lowercase_sessionid_is_accepted(self):
        result = self._parse(
            {
                "sessionid": "session-lowercase",
                "new": True,
                "msisdn": "233241235993",
                "network": 3,
                "message": "",
                "extension": "109",
                "data": "11005",
            }
        )
        self.assertEqual(result.session_id, "session-lowercase")

    def test_string_boolean_and_local_msisdn_are_normalized(self):
        result = self._parse(
            {
                "sessionId": "session-variants",
                "new": "true",
                "msisdn": "0241235993",
                "network": "3",
                "extension": "109",
                "data": "11005",
            }
        )
        self.assertTrue(result.is_new_session)
        self.assertEqual(result.phone, "0241235993")
        self.assertEqual(result.text, "11005")

    def test_missing_required_fields_are_rejected(self):
        with self.assertRaises(USSDMalformedRequest):
            self._parse({"sessionId": "", "new": True, "msisdn": "", "message": ""})

    def test_encoded_hash_is_normalized(self):
        result = self._parse(
            {
                "sessionId": "session-3",
                "new": False,
                "msisdn": "233241235993",
                "network": 3,
                "message": "%23",
                "extension": "109",
                "data": "11005",
            }
        )
        self.assertEqual(result.text, "#")


if __name__ == "__main__":
    unittest.main()
