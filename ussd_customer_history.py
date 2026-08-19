from __future__ import annotations

import re
from typing import Any, List


def phone_variants(phone: Any) -> List[str]:
    digits = re.sub(r"\D+", "", str(phone or ""))
    if digits.startswith("233") and len(digits) == 12:
        local = "0" + digits[3:]
    elif len(digits) == 9:
        local = "0" + digits
    else:
        local = digits
    variants = [local]
    if re.fullmatch(r"0\d{9}", local):
        international = "233" + local[1:]
        variants.extend([international, "+" + international])
    return list(dict.fromkeys(value for value in variants if value))


def order_phone_query(phone: Any) -> dict:
    variants = phone_variants(phone)
    return {
        "$or": [
            {"items.phone": {"$in": variants}},
            {"buyer_phone": {"$in": variants}},
            {"dial_phone": {"$in": variants}},
            {"ussd.dial_phone": {"$in": variants}},
        ]
    }


def collection_has_order_history(orders, phone: Any) -> bool:
    variants = phone_variants(phone)
    if not variants:
        return False
    return bool(orders.find_one(order_phone_query(phone), {"_id": 1}))


def has_order_history(nagonu_orders, zico_orders, phone: Any) -> bool:
    return bool(
        collection_has_order_history(nagonu_orders, phone)
        or collection_has_order_history(zico_orders, phone)
    )


def should_start_guest_checker(*, is_new_session: bool, has_session: bool, has_history: bool) -> bool:
    return bool(is_new_session and not has_session and not has_history)
