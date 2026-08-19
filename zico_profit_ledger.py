from __future__ import annotations

from typing import Any, Dict, Iterable


def money(value: Any) -> float:
    try:
        return round(float(value or 0.0), 2)
    except Exception:
        return 0.0


def positive_delta(high: Any, low: Any) -> float:
    return max(0.0, round(money(high) - money(low), 2))


def normalize_profit_line(
    line: Dict[str, Any],
    *,
    selling_amount: Any = None,
    main_base_amount: Any = None,
    admin_base_amount: Any = None,
    store_owner_base_amount: Any = None,
    store_profit_amount: Any = None,
) -> Dict[str, Any]:
    """Normalize checkout line pricing before profit totals are saved.

    Examples:
    A: main=3, admin=4, selling=5 -> main profit=1, admin profit=1, platform=2.
    B: main=10, admin=10, selling=12 -> main profit=0, admin profit=2, platform=2.
    C: main missing, admin=7, selling=9 -> main profit=0, admin profit=2, platform=2.
    D: selling below admin/base -> negative deltas are clamped to 0.
    """
    src = line or {}
    out = dict(src)

    selling = money(
        selling_amount
        if selling_amount is not None
        else out.get("selling_amount")
        if out.get("selling_amount") not in (None, "")
        else out.get("amount")
    )

    admin_base = money(
        admin_base_amount
        if admin_base_amount is not None
        else out.get("admin_base_amount")
        if out.get("admin_base_amount") not in (None, "")
        else out.get("base_amount")
    )

    if admin_base <= 0 and selling > 0:
        admin_base = money(out.get("base_amount"))

    main_base = money(
        main_base_amount
        if main_base_amount is not None
        else out.get("main_base_amount")
        if out.get("main_base_amount") not in (None, "")
        else admin_base
    )

    has_store_owner = store_owner_base_amount is not None or out.get("store_owner_base_amount") not in (None, "")
    if has_store_owner:
        store_owner_base = money(
            store_owner_base_amount
            if store_owner_base_amount is not None
            else out.get("store_owner_base_amount")
        )
    else:
        store_owner_base = admin_base

    main_admin_profit = positive_delta(admin_base, main_base)
    if has_store_owner:
        admin_profit = positive_delta(store_owner_base, admin_base)
        store_profit = (
            positive_delta(selling, store_owner_base)
            if store_profit_amount is None and out.get("store_profit_amount") in (None, "")
            else money(store_profit_amount if store_profit_amount is not None else out.get("store_profit_amount"))
        )
    else:
        admin_profit = positive_delta(selling, admin_base)
        store_profit = money(out.get("store_profit_amount"))

    platform_profit = positive_delta(selling, main_base)
    profit_percent = out.get("profit_percent_used")
    if profit_percent in (None, ""):
        profit_percent = round((admin_profit / admin_base) * 100.0, 2) if admin_base > 0 else 0.0

    out["selling_amount"] = selling
    out["main_base_amount"] = main_base
    out["admin_base_amount"] = admin_base
    out["base_amount"] = admin_base
    out["amount"] = selling
    out["main_admin_profit"] = main_admin_profit
    out["admin_profit"] = admin_profit
    out["store_profit_amount"] = store_profit
    out["platform_profit_amount"] = platform_profit
    out["profit_amount"] = positive_delta(selling, admin_base)
    out["profit_percent_used"] = money(profit_percent)
    if has_store_owner:
        out["store_owner_base_amount"] = store_owner_base
    return out


def apply_profit_split(
    line: Dict[str, Any],
    *,
    selling_amount: Any = None,
    main_base_amount: Any = None,
    admin_base_amount: Any = None,
    store_owner_base_amount: Any = None,
    store_profit_amount: Any = None,
) -> Dict[str, Any]:
    return normalize_profit_line(
        line,
        selling_amount=selling_amount,
        main_base_amount=main_base_amount,
        admin_base_amount=admin_base_amount,
        store_owner_base_amount=store_owner_base_amount,
        store_profit_amount=store_profit_amount,
    )


def profit_totals(lines: Iterable[Dict[str, Any]]) -> Dict[str, float]:
    main_total = 0.0
    admin_total = 0.0
    store_total = 0.0
    for line in lines or []:
        main_total += money((line or {}).get("main_admin_profit"))
        admin_total += money((line or {}).get("admin_profit"))
        store_total += money((line or {}).get("store_profit_amount"))
    return {
        "main_admin_profit_total": round(main_total, 2),
        "admin_profit_total": round(admin_total, 2),
        "store_profit_total": round(store_total, 2),
        "profit_amount_total": round(main_total + admin_total + store_total, 2),
    }
