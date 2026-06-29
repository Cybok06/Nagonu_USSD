from __future__ import annotations

import re
import json
from typing import Any, Dict, Iterable, List

import requests
from bson import ObjectId

from zico_db import db


ARKESEL_API_KEY = "TGFhVVZvU3NOclJMZFJwWWJ5U2o"
ARKESEL_SMS_V2_URL = "https://sms.arkesel.com/api/v2/sms/send"
DEFAULT_SITE_SENDER_NAME = "Azico"
ADMIN_SYSTEM_SENDER_NAME = "Zishop"

settings_col = db["settings"]
users_col = db["users"]
auth_pages_col = db["auth_pages"]


def _to_oid(value: Any) -> ObjectId | None:
    if isinstance(value, ObjectId):
        return value
    if not value:
        return None
    try:
        return ObjectId(str(value))
    except Exception:
        return None


def get_sms_settings() -> Dict[str, Any]:
    return (
        settings_col.find_one({"key": "sms_settings", "admin_id": {"$exists": False}})
        or settings_col.find_one({"key": "sms_settings", "admin_id": None})
        or {}
    )


def get_arkesel_api_key() -> str:
    settings_doc = get_sms_settings()
    return str(settings_doc.get("arkesel_api_key") or ARKESEL_API_KEY).strip()


def get_arkesel_api_key_source() -> str:
    settings_doc = get_sms_settings()
    return "settings" if (settings_doc.get("arkesel_api_key") or "").strip() else "fallback"


def get_site_sms_sender_name() -> str:
    settings_doc = get_sms_settings()
    raw = str(settings_doc.get("site_sender_name") or DEFAULT_SITE_SENDER_NAME).strip()
    return normalize_sms_sender_id(raw, fallback=DEFAULT_SITE_SENDER_NAME)


def resolve_admin_sender_name(admin_id: Any = None) -> str:
    admin_oid = _to_oid(admin_id)
    if not admin_oid:
        return get_site_sms_sender_name()
    admin_doc = users_col.find_one({"_id": admin_oid}, {"business_name": 1, "username": 1}) or {}
    auth_doc = auth_pages_col.find_one({"admin_id": admin_oid}, {"business_name": 1}) or {}
    brand_name = (
        (auth_doc or {}).get("business_name")
        or (admin_doc or {}).get("business_name")
        or (admin_doc or {}).get("username")
        or get_site_sms_sender_name()
    )
    return normalize_sms_sender_id(brand_name, fallback=get_site_sms_sender_name())


def resolve_system_sender_id(admin_id: Any = None, recipient_role: str | None = None, recipient_user_id: Any = None) -> str:
    role = str(recipient_role or "").strip().lower()
    if not role and recipient_user_id:
        recipient_doc = users_col.find_one({"_id": _to_oid(recipient_user_id)}, {"role": 1}) or {}
        role = str(recipient_doc.get("role") or "").strip().lower()
    if role in {"admin", "main_admin"}:
        return normalize_sms_sender_id(ADMIN_SYSTEM_SENDER_NAME, fallback=get_site_sms_sender_name())
    return resolve_admin_sender_name(admin_id)


def normalize_ghana_sms_phone(raw: str) -> str | None:
    if not raw:
        return None
    p = str(raw).strip().replace(" ", "").replace("-", "").replace("+", "")
    if p.startswith("0") and len(p) == 10:
        p = "233" + p[1:]
    if p.startswith("233") and len(p) == 12:
        return p
    return None


def normalize_sms_sender_id(raw: str, fallback: str = "Zico") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]", "", str(raw or "")).strip()
    if len(cleaned) < 3:
        cleaned = re.sub(r"[^A-Za-z0-9]", "", str(fallback or "Zico")).strip() or "Zico"
    return cleaned[:11] or "Zico"


def _coerce_recipients(recipients: Iterable[str]) -> List[str]:
    cleaned: List[str] = []
    seen = set()
    for raw in recipients or []:
        msisdn = normalize_ghana_sms_phone(str(raw or ""))
        if not msisdn or msisdn in seen:
            continue
        seen.add(msisdn)
        cleaned.append(msisdn)
    return cleaned


def _response_json(resp: requests.Response) -> Any:
    try:
        return resp.json()
    except Exception:
        return None


def _extract_provider_message(data: Any) -> str:
    if isinstance(data, dict):
        for key in ("message", "detail", "description", "status", "code"):
            value = data.get(key)
            if value not in (None, ""):
                return str(value)
        nested = data.get("data")
        if isinstance(nested, dict):
            for key in ("message", "detail", "description", "status", "code"):
                value = nested.get(key)
                if value not in (None, ""):
                    return str(value)
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict):
            return _extract_provider_message(first)
    return ""


def _extract_provider_status(data: Any) -> str:
    if isinstance(data, dict):
        for key in ("status", "code", "state"):
            value = data.get(key)
            if value not in (None, ""):
                return str(value)
        nested = data.get("data")
        if isinstance(nested, dict):
            for key in ("status", "code", "state"):
                value = nested.get(key)
                if value not in (None, ""):
                    return str(value)
    return ""


def _extract_message_id(data: Any) -> str:
    if isinstance(data, dict):
        for key in ("message_id", "messageId", "sms_id", "id"):
            value = data.get(key)
            if value not in (None, ""):
                return str(value)
        nested = data.get("data")
        if isinstance(nested, dict):
            for key in ("message_id", "messageId", "sms_id", "id"):
                value = nested.get(key)
                if value not in (None, ""):
                    return str(value)
    return ""


def _looks_successful(http_status: int | None, data: Any, text: str) -> bool:
    if http_status is None or http_status < 200 or http_status >= 300:
        return False

    combined = " ".join(
        part for part in [
            _extract_provider_status(data),
            _extract_provider_message(data),
            str(text or ""),
        ] if part
    ).lower()

    if any(flag in combined for flag in ("error", "failed", "invalid", "unauthorized", "forbidden")):
        return False
    if any(flag in combined for flag in ("ok", "success", "sent", "queued", "accepted")):
        return True
    return True


def _mask_recipient(msisdn: str) -> str:
    raw = str(msisdn or "")
    if len(raw) <= 5:
        return "***"
    return raw[:3] + "***" + raw[-2:]


def _safe_sms_log(event: str, **payload: Any) -> None:
    try:
        print(json.dumps({"evt": event, **payload}, ensure_ascii=False, default=str, separators=(",", ":")))
    except Exception:
        print(f"[SMS_LOG_FALLBACK] {event} {payload}")


def _log_failed_sms_result(result: Dict[str, Any]) -> None:
    req = result.get("request_payload") if isinstance(result.get("request_payload"), dict) else {}
    recipients = req.get("recipients") if isinstance(req.get("recipients"), list) else []
    _safe_sms_log(
        "arkesel_sms_failed",
        provider=result.get("provider"),
        http_status=result.get("http_status"),
        provider_status=result.get("provider_status"),
        provider_message=result.get("provider_message"),
        provider_message_id=result.get("provider_message_id"),
        error=result.get("error"),
        api_key_source=result.get("api_key_source"),
        sender=req.get("sender"),
        recipient_count=len(recipients),
        recipients=[_mask_recipient(r) for r in recipients[:5]],
        message_length=len(str(req.get("message") or "")),
        response_json=result.get("response_json"),
        response_text=(result.get("response_text") or "")[:1500],
    )


def send_bulk_sms(recipients: Iterable[str], message: str, sender_id: str = "Zico", timeout: int = 20) -> Dict[str, Any]:
    site_sender = get_site_sms_sender_name()
    sender = normalize_sms_sender_id(sender_id, fallback=site_sender)
    recipient_list = _coerce_recipients(recipients)
    payload = {
        "sender": sender,
        "message": str(message or ""),
        "recipients": recipient_list,
    }

    if not recipient_list:
        result = {
            "success": False,
            "status": "failed",
            "provider": "arkesel_v2",
            "http_status": None,
            "provider_status": "invalid_recipients",
            "provider_message": "No valid recipient numbers were provided.",
            "provider_message_id": "",
            "api_key_source": get_arkesel_api_key_source(),
            "request_payload": payload,
            "response_json": None,
            "response_text": "",
            "error": "invalid_recipients",
        }
        _log_failed_sms_result(result)
        return result

    try:
        resp = requests.post(
            ARKESEL_SMS_V2_URL,
            headers={
                "api-key": get_arkesel_api_key(),
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout,
        )
        data = _response_json(resp)
        text = (resp.text or "")[:1500]
        success = _looks_successful(resp.status_code, data, text)
        result = {
            "success": success,
            "status": "sent" if success else "failed",
            "provider": "arkesel_v2",
            "http_status": resp.status_code,
            "provider_status": _extract_provider_status(data) or ("success" if success else "failed"),
            "provider_message": _extract_provider_message(data) or ("Accepted by provider." if success else "Provider rejected the SMS request."),
            "provider_message_id": _extract_message_id(data),
            "api_key_source": get_arkesel_api_key_source(),
            "request_payload": payload,
            "response_json": data,
            "response_text": text,
            "error": "",
        }
        if not success:
            _log_failed_sms_result(result)
        return result
    except Exception as exc:
        result = {
            "success": False,
            "status": "error",
            "provider": "arkesel_v2",
            "http_status": None,
            "provider_status": "error",
            "provider_message": str(exc),
            "provider_message_id": "",
            "api_key_source": get_arkesel_api_key_source(),
            "request_payload": payload,
            "response_json": None,
            "response_text": "",
            "error": str(exc),
        }
        _log_failed_sms_result(result)
        return result


def send_sms(msisdn: str, message: str, sender_id: str = "Zico") -> str:
    result = send_bulk_sms([msisdn], message, sender_id=sender_id)
    if result.get("status") == "sent":
        return "sent"
    if result.get("status") == "failed":
        return "failed"
    return "error"
