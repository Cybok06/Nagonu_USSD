from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Iterable, List, Tuple

from bson import ObjectId

from zico_profit_ledger import money

ADMIN_WALLET_NEGATIVE_LIMIT = -50.0
WALLET_OVERDRAFT_LIMIT_MESSAGE = "Cannot Place order, Contact admin.Thank You"


def _oid(value: Any) -> ObjectId | None:
    if isinstance(value, ObjectId):
        return value
    try:
        return ObjectId(str(value))
    except Exception:
        return None


def _wallet_key(value: ObjectId) -> str:
    return str(value)


def grouped_wallet_debits(debits: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped: Dict[str, Dict[str, Any]] = {}
    for debit in debits or []:
        user_id = _oid((debit or {}).get("user_id"))
        amount = money((debit or {}).get("amount"))
        if not user_id or amount <= 0:
            continue
        key = _wallet_key(user_id)
        row = grouped.setdefault(
            key,
            {
                "user_id": user_id,
                "amount": 0.0,
                "labels": [],
                "items": [],
            },
        )
        row["amount"] = money(row["amount"] + amount)
        label = str((debit or {}).get("label") or "wallet_debit").strip()
        row["labels"].append(label)
        row["items"].append(dict(debit or {}, user_id=user_id, amount=amount, label=label))
    return list(grouped.values())


def debit_wallets_for_order(
    *,
    balances_col,
    balance_logs_col,
    transactions_col,
    debits: Iterable[Dict[str, Any]],
    order_id: str,
    admin_id: Any,
    source: str,
    note: str,
    meta: Dict[str, Any] | None = None,
    allow_negative: bool = False,
    negative_balance_limit: float | None = ADMIN_WALLET_NEGATIVE_LIMIT,
) -> Tuple[bool, str, List[Dict[str, Any]]]:
    grouped = grouped_wallet_debits(debits)
    if not grouped:
        return True, "", []

    admin_oid = _oid(admin_id)
    now = datetime.utcnow()
    applied: List[Dict[str, Any]] = []
    meta = dict(meta or {})

    if not allow_negative:
        for row in grouped:
            bal_doc = balances_col.find_one({"user_id": row["user_id"]}, {"amount": 1})
            current = money((bal_doc or {}).get("amount"))
            if current < money(row["amount"]):
                return False, f"Insufficient wallet balance for {', '.join(row['labels'])}.", []
    elif negative_balance_limit is not None:
        floor = money(negative_balance_limit)
        for row in grouped:
            amount = money(row["amount"])
            bal_doc = balances_col.find_one({"user_id": row["user_id"]}, {"amount": 1})
            current = money((bal_doc or {}).get("amount"))
            if money(current - amount) < floor:
                return False, WALLET_OVERDRAFT_LIMIT_MESSAGE, []

    for row in grouped:
        amount = money(row["amount"])
        before_doc = balances_col.find_one({"user_id": row["user_id"]}, {"amount": 1})
        before = money((before_doc or {}).get("amount"))
        update_filter = {"user_id": row["user_id"]}
        update_doc = {
            "$inc": {"amount": -amount},
            "$set": {"updated_at": now, "admin_id": admin_oid or row["user_id"]},
        }
        if allow_negative:
            update_doc["$setOnInsert"] = {
                "user_id": row["user_id"],
                "currency": "GHS",
                "created_at": now,
            }
        else:
            update_filter["amount"] = {"$gte": amount}

        result = balances_col.update_one(update_filter, update_doc, upsert=allow_negative)
        if not (result.modified_count or getattr(result, "upserted_id", None)):
            for done in applied:
                balances_col.update_one(
                    {"user_id": done["user_id"]},
                    {"$inc": {"amount": done["amount"]}, "$set": {"updated_at": datetime.utcnow()}},
                )
            return False, f"Could not debit wallet for {', '.join(row['labels'])}. Please try again.", applied

        after = money(before - amount)
        log_doc = {
            "user_id": row["user_id"],
            "admin_id": admin_oid,
            "action": "purchase_debit",
            "delta": -amount,
            "amount_before": before,
            "amount_after": after,
            "currency": "GHS",
            "note": note,
            "order_id": order_id,
            "source": source,
            "labels": row["labels"],
            "created_at": now,
            "meta": meta,
        }
        try:
            balance_logs_col.insert_one(log_doc)
        except Exception:
            pass
        try:
            transactions_col.insert_one(
                {
                    "user_id": row["user_id"],
                    "admin_id": admin_oid,
                    "amount": amount,
                    "reference": order_id,
                    "status": "success",
                    "type": "purchase_debit",
                    "source": source,
                    "gateway": "Wallet",
                    "currency": "GHS",
                    "created_at": now,
                    "verified_at": now,
                    "meta": {
                        **meta,
                        "labels": row["labels"],
                        "wallet_debit_items": row["items"],
                        "amount_before": before,
                        "amount_after": after,
                    },
                }
            )
        except Exception:
            pass
        applied.append({"user_id": row["user_id"], "amount": amount, "labels": row["labels"]})

    return True, "", applied
