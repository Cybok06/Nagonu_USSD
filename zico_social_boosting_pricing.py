from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

from bson import ObjectId


SOCIAL_BOOSTING_SERVICE_ID_STR = "69dfade8c9890c62a77db55d"
SOCIAL_BOOSTING_SERVICE_ID = ObjectId(SOCIAL_BOOSTING_SERVICE_ID_STR)
SOCIAL_BOOSTING_NAME = "Social Media Boosting"
SOCIAL_BOOSTING_IMAGE_URL = "/images/boosting_logo.png"
SOCIAL_BOOSTING_PROVIDER = "exosupplier"
SOCIAL_BOOSTING_PROVIDER_CURRENCY = "USD"
SOCIAL_BOOSTING_DISPLAY_CURRENCY = "GHS"
SOCIAL_BOOSTING_USD_TO_GHS_RATE = Decimal("11.01")

ADMIN_PERCENT_FIELDS = {
    "admin": "admin_profit_percent",
    "super_admin": "super_admin_profit_percent",
    "super_professional": "super_professional_profit_percent",
}

AGENT_PERCENT_FIELDS = {
    "normal_agent": "normal_agent_profit_percent",
    "elite_agent": "elite_agent_profit_percent",
    "premium": "premium_profit_percent",
}


def money(value: Any) -> float:
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        dec = Decimal("0")
    return float(dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def rate_money(value: Any) -> float:
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        dec = Decimal("0")
    return float(dec.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def usd_to_ghs(value: Any) -> float:
    return money(to_decimal(value, "0") * SOCIAL_BOOSTING_USD_TO_GHS_RATE)


def usd_to_ghs_rate(value: Any) -> float:
    return rate_money(to_decimal(value, "0") * SOCIAL_BOOSTING_USD_TO_GHS_RATE)


def to_decimal(value: Any, default: str = "0") -> Decimal:
    try:
        if value is None or value == "":
            return Decimal(default)
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def percent_value(value: Any) -> float:
    pct = to_decimal(value, "0")
    if pct < 0:
        pct = Decimal("0")
    return float(pct.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def normalize_admin_level(raw: Any) -> str:
    lvl = str(raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    if lvl in {"super_admin", "superadmin"}:
        return "super_admin"
    if lvl in {"super_professional", "professional_admin", "professional", "pro_admin"}:
        return "super_professional"
    return "admin"


def normalize_customer_stage(raw: Any) -> str:
    stage = str(raw or "").strip().lower().replace("-", " ").replace("_", " ")
    if stage in {"elite", "elite agent"}:
        return "elite_agent"
    if stage in {"premium", "premium agent"}:
        return "premium"
    return "normal_agent"


def is_social_boosting_service(value: Any) -> bool:
    if isinstance(value, dict):
        raw_id = value.get("_id") or value.get("serviceId") or value.get("service_id")
        name = str(value.get("name") or value.get("serviceName") or "").strip().lower()
        if name == SOCIAL_BOOSTING_NAME.lower():
            return True
        value = raw_id

    if isinstance(value, ObjectId):
        return value == SOCIAL_BOOSTING_SERVICE_ID

    text = str(value or "").strip()
    if text == SOCIAL_BOOSTING_SERVICE_ID_STR:
        return True
    return text.lower() == SOCIAL_BOOSTING_NAME.lower()


def offer_service_id(offer: Dict[str, Any]) -> Optional[int]:
    for key in ("service", "offer_id", "provider_service_id"):
        raw = offer.get(key)
        if raw not in (None, ""):
            try:
                return int(float(raw))
            except Exception:
                pass
    return None


def offer_requires_custom_comments(offer: Dict[str, Any]) -> bool:
    offer_type = str(
        (offer or {}).get("type")
        or (offer or {}).get("offer_type")
        or ""
    ).strip().lower().replace("_", " ")
    return offer_type == "custom comments"


def normalize_custom_comments(raw: Any) -> List[str]:
    source = raw
    if isinstance(source, dict):
        if source.get("comments") not in (None, "", []):
            source = source.get("comments")
        elif source.get("comments_list") not in (None, "", []):
            source = source.get("comments_list")
        else:
            source = source.get("comments_text")

    if isinstance(source, list):
        parts = source
    else:
        text = str(source or "").replace("\r\n", "\n").replace("\r", "\n")
        parts = text.split("\n")

    out: List[str] = []
    for part in parts:
        line = str(part or "").strip()
        if line:
            out.append(line)
    return out


def custom_comments_text(raw: Any) -> str:
    return "\n".join(normalize_custom_comments(raw))


def find_offer(offers: Any, provider_service_id: Any) -> Optional[Dict[str, Any]]:
    try:
        target = int(float(provider_service_id))
    except Exception:
        return None

    for offer in offers if isinstance(offers, list) else []:
        if not isinstance(offer, dict):
            continue
        if offer_service_id(offer) == target:
            return offer
    return None


def service_rate_per_1000(offer: Dict[str, Any]) -> Decimal:
    return to_decimal(offer.get("rate"), "0")


def admin_profit_percent(offer: Dict[str, Any], admin_level: Any) -> float:
    key = normalize_admin_level(admin_level)
    field = ADMIN_PERCENT_FIELDS.get(key, ADMIN_PERCENT_FIELDS["admin"])
    return percent_value(offer.get(field))


def admin_rate_per_1000(offer: Dict[str, Any], admin_level: Any) -> float:
    rate = service_rate_per_1000(offer)
    pct = to_decimal(admin_profit_percent(offer, admin_level), "0")
    final = rate * (Decimal("1") + (pct / Decimal("100")))
    return rate_money(final)


def agent_profit_percent(offer: Dict[str, Any], admin_id: Any, stage_label: Any) -> float:
    stage_key = normalize_customer_stage(stage_label)
    field = AGENT_PERCENT_FIELDS.get(stage_key, AGENT_PERCENT_FIELDS["normal_agent"])
    by_admin = offer.get("agent_profit_percentages_by_admin")

    if isinstance(by_admin, dict) and admin_id:
        admin_key = str(admin_id)
        row = by_admin.get(admin_key)
        if isinstance(row, dict) and field in row:
            return percent_value(row.get(field))

    return percent_value(offer.get(field))


def customer_rate_per_1000(
    offer: Dict[str, Any],
    admin_level: Any,
    admin_id: Any,
    stage_label: Any,
) -> float:
    admin_rate = to_decimal(admin_rate_per_1000(offer, admin_level), "0")
    pct = to_decimal(agent_profit_percent(offer, admin_id, stage_label), "0")
    final = admin_rate * (Decimal("1") + (pct / Decimal("100")))
    return rate_money(final)


def total_for_quantity(rate_per_1000: Any, quantity: Any) -> float:
    rate = to_decimal(rate_per_1000, "0")
    qty = to_decimal(quantity, "0")
    if qty < 0:
        qty = Decimal("0")
    return money((rate * qty) / Decimal("1000"))


def total_for_quantity_ghs(rate_usd_per_1000: Any, quantity: Any) -> float:
    rate = to_decimal(rate_usd_per_1000, "0")
    qty = to_decimal(quantity, "0")
    if qty < 0:
        qty = Decimal("0")
    return money(((rate * qty) / Decimal("1000")) * SOCIAL_BOOSTING_USD_TO_GHS_RATE)


def apply_default_offer_fields(offer: Dict[str, Any]) -> Dict[str, Any]:
    for field in ADMIN_PERCENT_FIELDS.values():
        offer.setdefault(field, 0)
    for field in AGENT_PERCENT_FIELDS.values():
        offer.setdefault(field, 0)
    if not isinstance(offer.get("agent_profit_percentages_by_admin"), dict):
        offer["agent_profit_percentages_by_admin"] = {}
    offer.setdefault("currency", SOCIAL_BOOSTING_PROVIDER_CURRENCY)
    offer.setdefault("display_currency", SOCIAL_BOOSTING_DISPLAY_CURRENCY)
    offer.setdefault("usd_to_ghs_rate", float(SOCIAL_BOOSTING_USD_TO_GHS_RATE))
    return offer
