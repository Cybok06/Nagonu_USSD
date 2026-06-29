from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from bson import ObjectId

from zico_db import db
from zico_sms_sender import normalize_ghana_sms_phone, send_sms


admin_paystack_balances_col = db["admin_paystack_balances"]
admin_paystack_balance_logs_col = db["admin_paystack_balance_logs"]
admin_paystack_payout_settings_col = db["admin_paystack_payout_settings"]
admin_paystack_payout_requests_col = db["admin_paystack_payout_requests"]
admin_wallet_auto_credit_settings_col = db["admin_wallet_auto_credit_settings"]
balances_col = db["balances"]
balance_logs_col = db["balance_logs"]
transactions_col = db["transactions"]
users_col = db["users"]

PAYOUT_WITHDRAW_FEE_GHS = 1.0
MIN_PAYOUT_REQUEST_GHS = 2.0
DEFAULT_WALLET_LOW_LIMIT_GHS = 50.0
LOW_BALANCE_SMS_COOLDOWN_HOURS = 24


def _now() -> datetime:
    return datetime.utcnow()


def _to_oid(value: Any) -> Optional[ObjectId]:
    if isinstance(value, ObjectId):
        return value
    if not value:
        return None
    try:
        return ObjectId(str(value))
    except Exception:
        return None


def _r2(value: Any) -> float:
    try:
        return round(float(value or 0), 2)
    except Exception:
        return 0.0


def get_admin_paystack_balance(admin_id: Any) -> Dict[str, Any]:
    admin_oid = _to_oid(admin_id)
    if not admin_oid:
        return {
            "admin_id": None,
            "total_inflow": 0.0,
            "available_balance": 0.0,
            "pending_balance": 0.0,
            "withdrawn_balance": 0.0,
            "withdrawn_net_total": 0.0,
            "fee_total": 0.0,
        }
    doc = admin_paystack_balances_col.find_one({"admin_id": admin_oid}) or {}
    return {
        "admin_id": admin_oid,
        "total_inflow": _r2(doc.get("total_inflow")),
        "available_balance": _r2(doc.get("available_balance")),
        "pending_balance": _r2(doc.get("pending_balance")),
        "withdrawn_balance": _r2(doc.get("withdrawn_balance")),
        "withdrawn_net_total": _r2(doc.get("withdrawn_net_total")),
        "fee_total": _r2(doc.get("fee_total")),
        "updated_at": doc.get("updated_at"),
        "last_credit_at": doc.get("last_credit_at"),
        "last_request_at": doc.get("last_request_at"),
        "last_paid_at": doc.get("last_paid_at"),
    }


def get_admin_wallet_auto_credit_settings(admin_id: Any) -> Dict[str, Any]:
    admin_oid = _to_oid(admin_id)
    if not admin_oid:
        return {
            "enabled": False,
            "low_balance_limit": DEFAULT_WALLET_LOW_LIMIT_GHS,
            "topup_amount": 0.0,
        }
    doc = admin_wallet_auto_credit_settings_col.find_one({"admin_id": admin_oid}) or {}
    return {
        **doc,
        "admin_id": admin_oid,
        "enabled": bool(doc.get("enabled", False)),
        "low_balance_limit": _r2(doc.get("low_balance_limit", DEFAULT_WALLET_LOW_LIMIT_GHS)),
        "topup_amount": _r2(doc.get("topup_amount", 0)),
    }


def save_admin_wallet_auto_credit_settings(
    admin_id: Any,
    enabled: Any,
    low_balance_limit: Any,
    topup_amount: Any,
) -> Dict[str, Any]:
    admin_oid = _to_oid(admin_id)
    limit_f = _r2(low_balance_limit)
    topup_f = _r2(topup_amount)
    enabled_bool = str(enabled).strip().lower() in {"1", "true", "yes", "on", "enabled"}
    if not admin_oid:
        return {"ok": False, "message": "Invalid admin account."}
    if limit_f <= 0:
        return {"ok": False, "message": "Enter a valid low balance limit."}
    if enabled_bool and topup_f <= 0:
        return {"ok": False, "message": "Enter the amount to auto-credit when balance is low."}

    now = _now()
    admin_wallet_auto_credit_settings_col.update_one(
        {"admin_id": admin_oid},
        {
            "$set": {
                "admin_id": admin_oid,
                "enabled": enabled_bool,
                "low_balance_limit": limit_f,
                "topup_amount": topup_f,
                "updated_at": now,
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )
    return {"ok": True, "message": "Auto-credit settings saved."}


def _get_admin_wallet_balance(admin_oid: ObjectId) -> float:
    doc = balances_col.find_one({"user_id": admin_oid}, {"amount": 1}) or {}
    return _r2(doc.get("amount", 0))


def _send_low_balance_sms_if_due(admin_oid: ObjectId, balance: float, limit: float) -> bool:
    settings = get_admin_wallet_auto_credit_settings(admin_oid)
    last_sent = settings.get("last_low_sms_at")
    if isinstance(last_sent, datetime) and last_sent > (_now() - timedelta(hours=LOW_BALANCE_SMS_COOLDOWN_HOURS)):
        return False

    user = users_col.find_one({"_id": admin_oid}, {"phone": 1, "username": 1, "first_name": 1}) or {}
    msisdn = normalize_ghana_sms_phone(user.get("phone") or "")
    if not msisdn:
        return False

    name = (user.get("first_name") or user.get("username") or "Admin").strip()
    message = (
        f"Hi {name}, your AZICO admin wallet balance is GHS {balance:.2f}, "
        f"which is at or below your low balance limit of GHS {limit:.2f}."
    )
    try:
        result = send_sms(msisdn, message)
    except Exception as exc:
        result = f"error:{exc}"

    admin_wallet_auto_credit_settings_col.update_one(
        {"admin_id": admin_oid},
        {
            "$set": {
                "last_low_sms_at": _now(),
                "last_low_sms_balance": balance,
                "last_low_sms_result": str(result)[:500],
            },
            "$setOnInsert": {"created_at": _now()},
        },
        upsert=True,
    )
    return True


def _auto_credit_admin_wallet(admin_oid: ObjectId, settings: Dict[str, Any], balance: float) -> Dict[str, Any]:
    if not settings.get("enabled"):
        return {"ok": False, "skipped": True, "message": "Auto-credit is off."}

    requested = _r2(settings.get("topup_amount"))
    if requested <= 0:
        return {"ok": False, "skipped": True, "message": "Auto-credit amount is not set."}

    available = _r2(get_admin_paystack_balance(admin_oid).get("available_balance"))
    transfer_amount = _r2(min(requested, available))
    if transfer_amount <= 0:
        return {"ok": False, "skipped": True, "message": "No Paystack payout balance available."}

    now = _now()
    req_doc = {
        "admin_id": admin_oid,
        "gross_amount": transfer_amount,
        "fee_amount": 0.0,
        "net_amount": transfer_amount,
        "method": "wallet_auto_credit",
        "status": "paid",
        "note": "Automatic low-balance wallet credit",
        "created_at": now,
        "updated_at": now,
        "paid_at": now,
        "processed_at": now,
        "processed_by": admin_oid,
        "process_note": "Auto-credit from Paystack payout balance",
    }
    insert_res = admin_paystack_payout_requests_col.insert_one(req_doc)

    admin_paystack_balances_col.update_one(
        {"admin_id": admin_oid},
        {
            "$inc": {
                "available_balance": -transfer_amount,
                "withdrawn_balance": transfer_amount,
                "withdrawn_net_total": transfer_amount,
            },
            "$set": {"updated_at": now, "last_paid_at": now, "currency": "GHS"},
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )
    _credit_admin_wallet(
        admin_oid,
        transfer_amount,
        insert_res.inserted_id,
        admin_oid,
        actor_name="Wallet Auto Credit",
        source="admin_wallet_auto_credit",
        gateway="paystack_payout_auto_credit",
        note="Auto-credit from Paystack payout balance",
        reference_prefix="AUTO-CREDIT",
    )
    admin_paystack_balance_logs_col.insert_one(
        {
            "admin_id": admin_oid,
            "type": "auto_wallet_credit",
            "request_id": insert_res.inserted_id,
            "amount": transfer_amount,
            "fee_amount": 0.0,
            "net_amount": transfer_amount,
            "method": "wallet_auto_credit",
            "status": "paid",
            "actor_id": admin_oid,
            "note": "Auto-credit from Paystack payout balance",
            "created_at": now,
        }
    )
    admin_wallet_auto_credit_settings_col.update_one(
        {"admin_id": admin_oid},
        {
            "$set": {
                "last_auto_credit_at": now,
                "last_auto_credit_amount": transfer_amount,
                "last_auto_credit_wallet_before": balance,
                "last_auto_credit_wallet_after": _r2(balance + transfer_amount),
            }
        },
    )
    return {
        "ok": True,
        "credited": transfer_amount,
        "requested": requested,
        "available_before": available,
        "message": f"Auto-credited GHS {transfer_amount:.2f} from Paystack payouts.",
    }


def evaluate_admin_wallet_low_balance(admin_id: Any, *, send_alert: bool = True, run_auto_credit: bool = True) -> Dict[str, Any]:
    admin_oid = _to_oid(admin_id)
    if not admin_oid:
        return {"ok": False, "balance": 0.0, "low": False, "limit": DEFAULT_WALLET_LOW_LIMIT_GHS}

    settings = get_admin_wallet_auto_credit_settings(admin_oid)
    limit = _r2(settings.get("low_balance_limit", DEFAULT_WALLET_LOW_LIMIT_GHS)) or DEFAULT_WALLET_LOW_LIMIT_GHS
    balance = _get_admin_wallet_balance(admin_oid)
    low = balance <= limit
    sms_sent = False
    auto_credit = {"skipped": True}

    if low and send_alert:
        sms_sent = _send_low_balance_sms_if_due(admin_oid, balance, limit)

    if low and run_auto_credit:
        auto_credit = _auto_credit_admin_wallet(admin_oid, settings, balance)
        if auto_credit.get("ok"):
            balance = _get_admin_wallet_balance(admin_oid)
            low = balance <= limit

    return {
        "ok": True,
        "balance": balance,
        "low": low,
        "limit": limit,
        "settings": settings,
        "sms_sent": sms_sent,
        "auto_credit": auto_credit,
        "paystack_balance": get_admin_paystack_balance(admin_oid),
    }


def get_admin_paystack_payout_settings(admin_id: Any) -> Dict[str, Any]:
    admin_oid = _to_oid(admin_id)
    if not admin_oid:
        return {}
    return admin_paystack_payout_settings_col.find_one({"admin_id": admin_oid}) or {}


def save_admin_paystack_payout_settings(admin_id: Any, recipient_name: str, msisdn: str, network: str) -> None:
    admin_oid = _to_oid(admin_id)
    if not admin_oid:
        return
    now = _now()
    admin_paystack_payout_settings_col.update_one(
        {"admin_id": admin_oid},
        {
            "$set": {
                "admin_id": admin_oid,
                "recipient_name": (recipient_name or "").strip(),
                "msisdn": (msisdn or "").strip(),
                "network": (network or "").strip().upper(),
                "updated_at": now,
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )


def clear_admin_paystack_payout_settings(admin_id: Any) -> None:
    admin_oid = _to_oid(admin_id)
    if not admin_oid:
        return
    admin_paystack_payout_settings_col.delete_one({"admin_id": admin_oid})


def record_admin_paystack_credit(
    admin_id: Any,
    amount: Any,
    profile: str,
    reference: str,
    transaction_id: Any | None = None,
    meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    admin_oid = _to_oid(admin_id)
    amount_f = _r2(amount)
    if not admin_oid or amount_f <= 0:
        return {"ok": False, "message": "Invalid admin or amount"}

    profile_key = (profile or "unknown").strip().lower()
    reference_key = (reference or "").strip()
    txn_oid = _to_oid(transaction_id)
    dedupe_key = f"{profile_key}:{reference_key}:{str(txn_oid) if txn_oid else ''}"

    existing = admin_paystack_balance_logs_col.find_one(
        {"admin_id": admin_oid, "type": "credit", "dedupe_key": dedupe_key}
    )
    if existing:
        return {"ok": True, "message": "Already credited", "duplicate": True}

    now = _now()
    admin_paystack_balances_col.update_one(
        {"admin_id": admin_oid},
        {
            "$inc": {"total_inflow": amount_f, "available_balance": amount_f},
            "$set": {"updated_at": now, "last_credit_at": now, "currency": "GHS"},
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )
    admin_paystack_balance_logs_col.insert_one(
        {
            "admin_id": admin_oid,
            "type": "credit",
            "amount": amount_f,
            "profile": profile_key,
            "source": (dict(meta or {}).get("source") or profile_key),
            "reference": reference_key,
            "transaction_id": txn_oid,
            "dedupe_key": dedupe_key,
            "meta": dict(meta or {}),
            "created_at": now,
        }
    )
    return {"ok": True, "credited": amount_f}


def create_admin_paystack_payout_request(admin_id: Any, amount: Any, method: str, note: str = "") -> Dict[str, Any]:
    admin_oid = _to_oid(admin_id)
    amount_f = _r2(amount)
    method_key = (method or "").strip().lower()
    if not admin_oid:
        return {"ok": False, "message": "Invalid admin"}
    if method_key not in {"momo", "wallet"}:
        return {"ok": False, "message": "Select a valid payout method"}
    if amount_f < MIN_PAYOUT_REQUEST_GHS:
        return {"ok": False, "message": f"Minimum payout request is GHS {MIN_PAYOUT_REQUEST_GHS:.2f}"}
    if amount_f <= PAYOUT_WITHDRAW_FEE_GHS:
        return {"ok": False, "message": "Amount must be greater than the payout fee"}

    bal = get_admin_paystack_balance(admin_oid)
    if amount_f > _r2(bal.get("available_balance")):
        return {"ok": False, "message": "Insufficient Paystack payout balance"}

    payout_snapshot = None
    if method_key == "momo":
        payout = get_admin_paystack_payout_settings(admin_oid)
        if not (payout.get("recipient_name") and payout.get("msisdn")):
            return {"ok": False, "message": "Set your payout MoMo details in profile first"}
        payout_snapshot = {
            "recipient_name": payout.get("recipient_name"),
            "msisdn": payout.get("msisdn"),
            "network": payout.get("network"),
        }

    now = _now()
    fee_amount = _r2(PAYOUT_WITHDRAW_FEE_GHS)
    net_amount = _r2(amount_f - fee_amount)
    is_wallet_auto = method_key == "wallet"

    req_doc = {
        "admin_id": admin_oid,
        "gross_amount": amount_f,
        "fee_amount": fee_amount,
        "net_amount": net_amount,
        "method": method_key,
        "status": "paid" if is_wallet_auto else "pending",
        "note": (note or "").strip()[:240],
        "payout_snapshot": payout_snapshot,
        "created_at": now,
        "updated_at": now,
    }
    if is_wallet_auto:
        req_doc.update(
            {
                "paid_at": now,
                "processed_at": now,
                "processed_by": admin_oid,
                "process_note": "Automatic wallet payout",
            }
        )
    insert_res = admin_paystack_payout_requests_col.insert_one(req_doc)

    if is_wallet_auto:
        admin_paystack_balances_col.update_one(
            {"admin_id": admin_oid},
            {
                "$inc": {
                    "available_balance": -amount_f,
                    "withdrawn_balance": amount_f,
                    "withdrawn_net_total": net_amount,
                    "fee_total": fee_amount,
                },
                "$set": {
                    "updated_at": now,
                    "last_request_at": now,
                    "last_paid_at": now,
                    "currency": "GHS",
                },
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )
        _credit_admin_wallet(
            admin_oid,
            net_amount,
            insert_res.inserted_id,
            admin_oid,
            actor_name="Automatic Wallet Payout",
        )
        admin_paystack_balance_logs_col.insert_one(
            {
                "admin_id": admin_oid,
                "type": "paid",
                "request_id": insert_res.inserted_id,
                "amount": amount_f,
                "fee_amount": fee_amount,
                "net_amount": net_amount,
                "method": method_key,
                "status": "paid",
                "actor_id": admin_oid,
                "note": "Automatic wallet payout",
                "created_at": now,
            }
        )
        return {
            "ok": True,
            "request_id": insert_res.inserted_id,
            "gross_amount": amount_f,
            "net_amount": net_amount,
            "message": f"Wallet payout completed. GHS {net_amount:.2f} credited to your wallet.",
        }

    admin_paystack_balances_col.update_one(
        {"admin_id": admin_oid},
        {
            "$inc": {"available_balance": -amount_f, "pending_balance": amount_f},
            "$set": {"updated_at": now, "last_request_at": now, "currency": "GHS"},
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )
    admin_paystack_balance_logs_col.insert_one(
        {
            "admin_id": admin_oid,
            "type": "request",
            "request_id": insert_res.inserted_id,
            "amount": amount_f,
            "fee_amount": fee_amount,
            "net_amount": net_amount,
            "method": method_key,
            "status": "pending",
            "note": (note or "").strip()[:240],
            "created_at": now,
        }
    )
    return {"ok": True, "request_id": insert_res.inserted_id, "gross_amount": amount_f, "net_amount": net_amount}


def _credit_admin_wallet(
    admin_oid: ObjectId,
    amount: float,
    request_oid: ObjectId,
    actor_oid: Optional[ObjectId],
    actor_name: str = "Main Admin",
    source: str = "admin_paystack_payout",
    gateway: str = "admin_paystack_payout",
    note: str = "Paystack payout request settled to admin wallet",
    reference_prefix: str = "PAYOUT",
) -> None:
    now = _now()
    bal_doc = balances_col.find_one({"user_id": admin_oid}) or {}
    before = _r2(bal_doc.get("amount"))
    after = _r2(before + amount)
    balances_col.update_one(
        {"user_id": admin_oid},
        {
            "$inc": {"amount": amount},
            "$set": {"updated_at": now},
            "$setOnInsert": {"created_at": now, "currency": "GHS", "admin_id": admin_oid},
        },
        upsert=True,
    )
    log_res = balance_logs_col.insert_one(
        {
            "user_id": admin_oid,
            "admin_id": actor_oid,
            "action": "deposit",
            "delta": amount,
            "amount_before": before,
            "amount_after": after,
            "currency": "GHS",
            "note": f"{note} (request {str(request_oid)})",
            "actor_id": actor_oid,
            "actor_name": actor_name,
            "created_at": now,
        }
    )
    transactions_col.insert_one(
        {
            "reference": f"{reference_prefix}-{str(request_oid)[-8:].upper()}",
            "user_id": admin_oid,
            "admin_id": admin_oid,
            "amount": amount,
            "currency": "GHS",
            "type": "deposit",
            "status": "success",
            "source": source,
            "gateway": gateway,
            "created_at": now,
            "verified_at": now,
            "actor_id": actor_oid,
            "note": note,
            "balance_log_id": log_res.inserted_id,
            "meta": {
                "request_id": request_oid,
                "paystack_payout": True,
                "paystack_profile": "payout_wallet",
            },
        }
    )


def process_admin_paystack_payout_request(request_id: Any, action: str, actor_id: Any | None = None, note: str = "") -> Dict[str, Any]:
    req_oid = _to_oid(request_id)
    actor_oid = _to_oid(actor_id)
    action_key = (action or "").strip().lower()
    if not req_oid:
        return {"ok": False, "message": "Invalid request"}
    if action_key not in {"paid", "rejected"}:
        return {"ok": False, "message": "Invalid action"}

    req = admin_paystack_payout_requests_col.find_one({"_id": req_oid})
    if not req:
        return {"ok": False, "message": "Request not found"}
    if (req.get("status") or "").strip().lower() != "pending":
        return {"ok": False, "message": "Only pending requests can be updated"}

    admin_oid = _to_oid(req.get("admin_id"))
    gross_amount = _r2(req.get("gross_amount"))
    fee_amount = _r2(req.get("fee_amount"))
    net_amount = _r2(req.get("net_amount"))
    method_key = (req.get("method") or "").strip().lower()
    now = _now()

    if action_key == "paid":
        admin_paystack_balances_col.update_one(
            {"admin_id": admin_oid},
            {
                "$inc": {
                    "pending_balance": -gross_amount,
                    "withdrawn_balance": gross_amount,
                    "withdrawn_net_total": net_amount,
                    "fee_total": fee_amount,
                },
                "$set": {"updated_at": now, "last_paid_at": now},
            },
            upsert=True,
        )
        if method_key == "wallet":
            _credit_admin_wallet(admin_oid, net_amount, req_oid, actor_oid)
        status_note = (note or "").strip()[:240]
        admin_paystack_payout_requests_col.update_one(
            {"_id": req_oid},
            {
                "$set": {
                    "status": "paid",
                    "paid_at": now,
                    "processed_at": now,
                    "processed_by": actor_oid,
                    "process_note": status_note,
                    "updated_at": now,
                }
            },
        )
        admin_paystack_balance_logs_col.insert_one(
            {
                "admin_id": admin_oid,
                "type": "paid",
                "request_id": req_oid,
                "amount": gross_amount,
                "fee_amount": fee_amount,
                "net_amount": net_amount,
                "method": method_key,
                "status": "paid",
                "actor_id": actor_oid,
                "note": status_note,
                "created_at": now,
            }
        )
        return {"ok": True, "message": "Payout marked as paid"}

    admin_paystack_balances_col.update_one(
        {"admin_id": admin_oid},
        {
            "$inc": {"pending_balance": -gross_amount, "available_balance": gross_amount},
            "$set": {"updated_at": now},
        },
        upsert=True,
    )
    status_note = (note or "").strip()[:240]
    admin_paystack_payout_requests_col.update_one(
        {"_id": req_oid},
        {
            "$set": {
                "status": "rejected",
                "processed_at": now,
                "processed_by": actor_oid,
                "process_note": status_note,
                "updated_at": now,
            }
        },
    )
    admin_paystack_balance_logs_col.insert_one(
        {
            "admin_id": admin_oid,
            "type": "rejected",
            "request_id": req_oid,
            "amount": gross_amount,
            "fee_amount": fee_amount,
            "net_amount": net_amount,
            "method": method_key,
            "status": "rejected",
            "actor_id": actor_oid,
            "note": status_note,
            "created_at": now,
        }
    )
    return {"ok": True, "message": "Payout request rejected"}
