from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class NormalizedUSSDRequest:
    provider: str
    session_id: str
    phone: str
    text: str
    is_new_session: bool
    shortcode: Optional[str] = None
    network: Optional[str] = None
    raw_event_id: Optional[str] = None


@dataclass(frozen=True)
class NormalizedUSSDResponse:
    message: str
    continue_session: bool


class USSDAdapterError(Exception):
    """A safe error that may be returned at the callback boundary."""

    status_code = 400
    error_code = "invalid_ussd_callback"


class USSDMethodNotAllowed(USSDAdapterError):
    status_code = 405
    error_code = "method_not_allowed"


class USSDUnsupportedMediaType(USSDAdapterError):
    status_code = 415
    error_code = "unsupported_media_type"


class USSDMalformedRequest(USSDAdapterError):
    status_code = 400
    error_code = "malformed_request"


class USSDContractUnavailable(USSDAdapterError):
    """Raised when an adapter cannot be implemented without guessing."""

    status_code = 503
    error_code = "provider_contract_unavailable"


def normalize_menu_response(response: Any) -> NormalizedUSSDResponse:
    """Convert the existing internal CON/END convention into neutral data."""
    value = str(response or "")
    if value.startswith("CON "):
        return NormalizedUSSDResponse(value[4:], True)
    if value.startswith("END "):
        return NormalizedUSSDResponse(value[4:], False)
    return NormalizedUSSDResponse(value, False)


class USSDProviderAdapter:
    provider = "unknown"

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
        raise NotImplementedError

    def format_response(self, response: NormalizedUSSDResponse) -> tuple[Any, int, str]:
        raise NotImplementedError
