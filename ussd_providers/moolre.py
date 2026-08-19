from __future__ import annotations

import json
import re
from typing import Any, Mapping

from .base import (
    NormalizedUSSDRequest,
    NormalizedUSSDResponse,
    USSDMalformedRequest,
    USSDProviderAdapter,
    USSDUnsupportedMediaType,
)


MOOLRE_USSD_GUIDE_URL = "https://docs.moolre.com/guides/ussd"


class MoolreUSSDAdapter(USSDProviderAdapter):
    """Adapter for Moolre's confirmed JSON callback contract."""

    provider = "moolre"

    @staticmethod
    def _clean_input(value: Any) -> str:
        return str(value or "").replace("%23", "#").strip()

    @staticmethod
    def _value(payload: Mapping[str, Any], name: str) -> Any:
        """Read Moolre fields case-insensitively.

        The official JSON example uses ``sessionId`` while the parameter table
        documents ``sessionid``. Treating field names case-insensitively keeps
        the callback compatible with both representations.
        """
        wanted = name.casefold()
        for key, value in payload.items():
            if str(key).casefold() == wanted:
                return value
        return None

    @staticmethod
    def _boolean(value: Any) -> bool | None:
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().casefold()
            if normalized in {"true", "1"}:
                return True
            if normalized in {"false", "0"}:
                return False
        return None

    def parse_request(
        self,
        *,
        method: str,
        content_type: str,
        headers: Mapping[str, str],
        body: bytes,
        json_payload: Any,
        form_payload: Mapping[str, Any],
        query_payload: Mapping[str, Any],
    ) -> NormalizedUSSDRequest:
        payload = json_payload
        if not isinstance(payload, dict) and body:
            try:
                payload = json.loads(body.decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = None

        if not isinstance(payload, dict):
            if "application/json" not in str(content_type or "").lower():
                raise USSDUnsupportedMediaType("Moolre callbacks must contain a JSON body.")
            raise USSDMalformedRequest("A JSON object is required.")

        session_id = str(self._value(payload, "sessionId") or "").strip()
        if not session_id or len(session_id) > 128:
            raise USSDMalformedRequest("A valid sessionId is required.")
        is_new = self._boolean(self._value(payload, "new"))
        if is_new is None:
            raise USSDMalformedRequest("The new field must be a boolean.")

        raw_phone = re.sub(r"\D+", "", str(self._value(payload, "msisdn") or ""))
        if re.fullmatch(r"0\d{9}", raw_phone):
            phone = raw_phone
        elif re.fullmatch(r"233\d{9}", raw_phone):
            phone = "0" + raw_phone[3:]
        else:
            raise USSDMalformedRequest("A valid Ghana msisdn is required.")

        message = self._clean_input(self._value(payload, "message"))
        data = self._clean_input(self._value(payload, "data"))
        # Moolre sends the extra dialed data (the agent code in
        # *203*extension*agent-code#) separately on the first callback. Later
        # callbacks carry only the current user selection in message. This is
        # exactly the latest-input model consumed by the existing state machine.
        text = data if is_new and data else message
        if len(text) > 512:
            raise USSDMalformedRequest("USSD input is too long.")

        extension = str(self._value(payload, "extension") or "").strip() or None
        network_raw = self._value(payload, "network")
        network = str(network_raw).strip() if network_raw is not None else None
        return NormalizedUSSDRequest(
            provider=self.provider,
            session_id=session_id,
            phone=phone,
            text=text,
            is_new_session=is_new,
            shortcode=extension,
            network=network,
            raw_event_id=None,
        )

    def format_response(self, response: NormalizedUSSDResponse) -> tuple[Any, int, str]:
        return {
            "message": str(response.message or ""),
            "reply": bool(response.continue_session),
        }, 200, "application/json"
