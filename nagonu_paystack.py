from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Tuple

import requests

from nagonu_db import db
from nagonu_store import normalize_phone
from nagonu_ussd_orders import release_nagonu_ussd_order


PAYSTACK_BASE_URL = "https://api.paystack.co"
PAYSTACK_FEE_RATE = 0.02
OTP_MAX_ATTEMPTS = 3

payments_col = db["ussd_paystack_payments"]
pending_orders_col = db["ussd_pending_orders"]
orders_col = db["orders"]


def _load_env_file() -> None:
    env_path = Path(__file__).with_name(".env")
    if not env_path.exists():
        return
    try:
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except Exception:
        pass


_load_env_file()


def _secret_key() -> str:
    return (
        os.getenv("nagonu_secret_key")
        or os.getenv("NAGONU_SECRET_KEY")
        or os.getenv("PAYSTACK_SECRET_KEY")
        or ""
    ).strip()


def public_key() -> str:
    return (
        os.getenv("nagonu_public_key")
        or os.getenv("NAGONU_PUBLIC_KEY")
        or os.getenv("PAYSTACK_PUBLIC_KEY")
        or ""
    ).strip()


def _money(value: Any, default: float = 0.0) -> float:
    try:
        return round(float(str(value).replace(",", "").strip()), 2)
    except Exception:
        return default


def _pesewas(amount_ghs: Any) -> int:
    return int(round(_money(amount_ghs) * 100))


def _fee_for(base_amount: Any) -> float:
    return round(_money(base_amount) * PAYSTACK_FEE_RATE, 2)


def _reference(order_id: str) -> str:
    return f"USSD-{order_id}-{uuid.uuid4().hex[:8].upper()}"


def _provider_for_phone(phone: str) -> str:
    phone = normalize_phone(phone)
    prefix = phone[:3]
    if prefix in {"024", "025", "053", "054", "055", "059"}:
        return "mtn"
    if prefix in {"020", "050"}:
        return "vodafone"
    if prefix in {"026", "027", "056", "057"}:
        return "atl"
    return ""


def _headers() -> Dict[str, str]:
    secret = _secret_key()
    if not secret:
        raise RuntimeError("Nagonu Paystack secret key is not configured.")
    return {"Authorization": f"Bearer {secret}", "Content-Type": "application/json"}


def _paystack_post(path: str, body: Dict[str, Any]) -> Dict[str, Any]:
    response = requests.post(f"{PAYSTACK_BASE_URL}{path}", headers=_headers(), json=body, timeout=45)
    try:
        payload = response.json()
    except Exception:
        payload = {"status": False, "message": response.text or "Invalid Paystack response"}
    if isinstance(payload, dict):
        payload.setdefault("http_status", response.status_code)
    return payload


def _paystack_get(path: str) -> Dict[str, Any]:
    response = requests.get(f"{PAYSTACK_BASE_URL}{path}", headers=_headers(), timeout=45)
    try:
        payload = response.json()
    except Exception:
        payload = {"status": False, "message": response.text or "Invalid Paystack response"}
    if isinstance(payload, dict):
        payload.setdefault("http_status", response.status_code)
    return payload


def verify_webhook_signature(raw_body: bytes, signature: str) -> bool:
    secret = _secret_key()
    if not secret or not signature:
        return False
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(digest, str(signature).strip())


def _set_pending_order_status(pending_order_id: Any, status: str, extra: Dict[str, Any] | None = None) -> None:
    if not pending_order_id:
        return
    try:
        from bson import ObjectId

        oid = ObjectId(str(pending_order_id))
    except Exception:
        oid = None
    query = {"_id": oid} if oid else {"id": str(pending_order_id)}
    pending_orders_col.update_one(query, {"$set": {**(extra or {}), "status": status, "updated_at": datetime.utcnow()}})


def initiate_payment(order: Dict[str, Any], session_data: Dict[str, Any], session_id: str, payer_phone: str) -> Dict[str, Any]:
    order_id = str(order.get("order_id") or "")
    if not order_id:
        return {"success": False, "message": "Order ID missing."}

    existing = payments_col.find_one(
        {"order_id": order_id, "payment_status": {"$in": ["pending", "otp_required", "paid"]}},
        sort=[("created_at", -1)],
    )
    if existing:
        status = existing.get("payment_status") or "pending"
        return {
            "success": True,
            "status": "success" if status == "paid" else ("send_otp" if status == "otp_required" else "pending"),
            "reference": existing.get("paystack_reference"),
            "message": "Existing payment session found.",
        }

    base_amount = _money(order.get("total_amount") or session_data.get("amount"))
    gateway_fee = _fee_for(base_amount)
    charge_amount = round(base_amount + gateway_fee, 2)
    expected_pesewas = _pesewas(charge_amount)
    reference = _reference(order_id)
    payer_phone = normalize_phone(payer_phone)
    provider = _provider_for_phone(payer_phone)
    now = datetime.utcnow()

    if not provider:
        return {"success": False, "message": "Unsupported mobile money network for payment."}

    payment_doc = {
        "order_id": order_id,
        "payment_reference": reference,
        "paystack_reference": reference,
        "amount": charge_amount,
        "base_amount": base_amount,
        "gateway_fee": gateway_fee,
        "amount_pesewas": expected_pesewas,
        "currency": "GHS",
        "channel": "ussd",
        "payment_provider": "paystack",
        "payment_channel": "mobile_money",
        "payment_status": "pending",
        "status": "awaiting_payment",
        "payer_phone": payer_phone,
        "recipient_phone": normalize_phone(session_data.get("recipient")),
        "momo_provider": provider,
        "agent_code": session_data.get("agent_code"),
        "agent_user_id": session_data.get("agent_user_id"),
        "agent_display_name": session_data.get("agent_display_name") or "",
        "session_id": session_id,
        "pending_order_id": session_data.get("pending_order_id"),
        "created_at": now,
        "updated_at": now,
    }
    payments_col.insert_one(payment_doc)
    orders_col.update_one(
        {"order_id": order_id},
        {
            "$set": {
                "payment_reference": reference,
                "paystack_reference": reference,
                "payment_status": "pending",
                "status": "awaiting_payment",
                "payment_provider": "paystack",
                "payment_channel": "mobile_money",
                "charged_amount": charge_amount,
                "paystack_charged_amount": charge_amount,
                "paystack_fee_amount": gateway_fee,
                "debug.paystack_expected_ghs": charge_amount,
                "debug.paystack_fee_ghs": gateway_fee,
                "updated_at": now,
            }
        },
    )
    _set_pending_order_status(
        session_data.get("pending_order_id"),
        "payment_pending",
        {
            "order_id": order_id,
            "payment_reference": reference,
            "paystack_reference": reference,
            "amount": charge_amount,
            "base_amount": base_amount,
            "gateway_fee": gateway_fee,
        },
    )

    metadata = {
        "order_id": order_id,
        "channel": "arkesel_ussd",
        "app": "nagonu",
        "agent_code": session_data.get("agent_code"),
        "agent_user_id": session_data.get("agent_user_id"),
        "agent_display_name": session_data.get("agent_display_name") or "",
        "session_id": session_id,
    }
    body = {
        "email": f"ussd-{payer_phone}@nagonu.local",
        "amount": expected_pesewas,
        "currency": "GHS",
        "reference": reference,
        "mobile_money": {"phone": payer_phone, "provider": provider},
        "metadata": metadata,
    }

    try:
        payload = _paystack_post("/charge", body)
    except Exception as exc:
        payments_col.update_one(
            {"paystack_reference": reference},
            {"$set": {"payment_status": "failed", "status": "charge_request_failed", "error": str(exc), "updated_at": datetime.utcnow()}},
        )
        _set_pending_order_status(session_data.get("pending_order_id"), "payment_failed", {"payment_error": str(exc)})
        return {"success": False, "status": "failed", "reference": reference, "message": str(exc)}

    data = payload.get("data") if isinstance(payload, dict) else {}
    paystack_status = str((data or {}).get("status") or "").strip().lower()
    payments_col.update_one(
        {"paystack_reference": reference},
        {
            "$set": {
                "paystack_initial_response": payload,
                "paystack_status": paystack_status,
                "updated_at": datetime.utcnow(),
            }
        },
    )
    return _handle_paystack_status(reference, payload, session_data.get("pending_order_id"))


def _handle_paystack_status(reference: str, payload: Dict[str, Any], pending_order_id: Any = None) -> Dict[str, Any]:
    data = payload.get("data") if isinstance(payload, dict) else {}
    status = str((data or {}).get("status") or "").strip().lower()
    if status == "success":
        complete = complete_payment(reference, data, source="charge_response")
        return {"success": True, "status": "success", "reference": reference, **complete}
    if status == "send_otp":
        payments_col.update_one(
            {"paystack_reference": reference},
            {"$set": {"payment_status": "otp_required", "status": "otp_required", "updated_at": datetime.utcnow()}},
        )
        _set_pending_order_status(pending_order_id, "otp_required")
        return {
            "success": True,
            "status": "send_otp",
            "reference": reference,
            "message": (data or {}).get("display_text") or "Enter the OTP or voucher code sent by your network.",
        }
    if status in {"pending", "pay_offline", "processing"}:
        payments_col.update_one(
            {"paystack_reference": reference},
            {"$set": {"payment_status": "pending", "status": "awaiting_payment", "updated_at": datetime.utcnow()}},
        )
        _set_pending_order_status(pending_order_id, "payment_pending")
        return {"success": True, "status": status or "pending", "reference": reference}

    fail_status = status or "failed"
    payments_col.update_one(
        {"paystack_reference": reference},
        {"$set": {"payment_status": "failed", "status": fail_status, "updated_at": datetime.utcnow()}},
    )
    _set_pending_order_status(pending_order_id, "payment_failed")
    return {"success": False, "status": fail_status, "reference": reference, "message": "Payment could not be started."}


def submit_otp(reference: str, otp: str) -> Dict[str, Any]:
    payment = payments_col.find_one({"paystack_reference": reference})
    if not payment:
        return {"success": False, "status": "not_found", "message": "Payment session not found."}
    attempts = int(payment.get("otp_attempts") or 0)
    if attempts >= OTP_MAX_ATTEMPTS:
        return {"success": False, "status": "failed", "message": "OTP attempts exceeded."}

    payments_col.update_one({"paystack_reference": reference}, {"$inc": {"otp_attempts": 1}, "$set": {"updated_at": datetime.utcnow()}})
    try:
        payload = _paystack_post("/charge/submit_otp", {"otp": str(otp or "").strip(), "reference": reference})
    except Exception as exc:
        return {"success": False, "status": "failed", "message": str(exc)}

    payments_col.update_one(
        {"paystack_reference": reference},
        {"$set": {"paystack_otp_response": payload, "updated_at": datetime.utcnow()}},
    )
    return _handle_paystack_status(reference, payload, payment.get("pending_order_id"))


def verify_payment(reference: str) -> Dict[str, Any]:
    payment = payments_col.find_one({"paystack_reference": reference})
    if not payment:
        return {"success": False, "status": "not_found", "message": "Payment session not found."}
    try:
        payload = _paystack_get(f"/charge/{reference}")
    except Exception as exc:
        return {"success": False, "status": "failed", "message": str(exc)}

    payments_col.update_one(
        {"paystack_reference": reference},
        {"$set": {"paystack_verify_response": payload, "updated_at": datetime.utcnow()}},
    )
    return _handle_paystack_status(reference, payload, payment.get("pending_order_id"))


def complete_payment(reference: str, paystack_data: Dict[str, Any], source: str = "webhook") -> Dict[str, Any]:
    payment = payments_col.find_one({"paystack_reference": reference})
    if not payment:
        return {"success": True, "ignored": True, "reason": "order_not_found"}

    currency = str(paystack_data.get("currency") or payment.get("currency") or "").upper()
    amount = int(paystack_data.get("amount") or 0)
    expected_amount = int(payment.get("amount_pesewas") or _pesewas(payment.get("amount")))
    if currency != "GHS" or amount != expected_amount:
        payments_col.update_one(
            {"paystack_reference": reference},
            {
                "$set": {
                    "payment_status": "mismatch",
                    "status": "payment_mismatch",
                    "mismatch": {"currency": currency, "amount": amount, "expected_amount": expected_amount},
                    "updated_at": datetime.utcnow(),
                }
            },
        )
        return {"success": False, "ignored": True, "reason": "amount_or_currency_mismatch"}

    now = datetime.utcnow()
    payments_col.update_one(
        {"paystack_reference": reference},
        {
            "$set": {
                "payment_status": "paid",
                "status": "paid",
                "paid_at": now,
                "paystack_success_data": paystack_data,
                "completed_by": source,
                "updated_at": now,
            }
        },
    )
    _set_pending_order_status(
        payment.get("pending_order_id"),
        "paid",
        {"paid_at": now, "payment_reference": reference, "paystack_reference": reference},
    )
    released = release_nagonu_ussd_order(str(payment.get("order_id") or ""), {**payment, "paystack_reference": reference}, paystack_data)
    return {"success": True, "released_provider_processing": bool(released.get("released")), **released}


def handle_webhook(raw_body: bytes, signature: str) -> Tuple[Dict[str, Any], int]:
    if not verify_webhook_signature(raw_body, signature):
        return {"success": False, "error": "invalid_signature"}, 401
    try:
        payload = json.loads(raw_body.decode("utf-8") or "{}")
    except Exception:
        return {"success": False, "error": "invalid_json"}, 400
    if payload.get("event") != "charge.success":
        return {"success": True, "ignored": True, "reason": "event_not_supported"}, 200
    data = payload.get("data") or {}
    reference = data.get("reference") or data.get("payment_reference")
    if not reference:
        return {"success": True, "ignored": True, "reason": "reference_missing"}, 200
    result = complete_payment(str(reference), data, source="webhook")
    return result, 200
