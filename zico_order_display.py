import ast
import json
import re
from typing import Any, Dict, List


NETWORK_ID_LABELS = {
    1: "AT",
    2: "Telecel",
    3: "MTN",
}


def _jsonish_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if value in (None, "", []):
        return {}
    if not isinstance(value, str):
        return {}

    text = value.strip()
    if not (text.startswith("{") and text.endswith("}")):
        return {}

    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        pass

    try:
        parsed = ast.literal_eval(text)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _fmt_num(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def order_item_network_label(item: Dict[str, Any]) -> str:
    if not isinstance(item, dict):
        return ""

    candidates = [
        item.get("ported_expected_network"),
        item.get("ported_detected_network"),
        item.get("network_name"),
        item.get("network"),
        item.get("service_network"),
        item.get("serviceName"),
    ]
    joined = " ".join(str(c) for c in candidates if c).strip().lower()

    if "mtn" in joined:
        return "MTN"
    if "telecel" in joined or "vodafone" in joined:
        return "Telecel"
    if (
        "airteltigo" in joined
        or "airtel tigo" in joined
        or "airtel-tigo" in joined
        or "at - ishare" in joined
        or "at - bigtime" in joined
        or "bigtime" in joined
        or "i share" in joined
        or "ishare" in joined
        or re.search(r"\bat\b", joined)
    ):
        return "AT"

    try:
        return NETWORK_ID_LABELS.get(int(item.get("network_id") or 0), "")
    except Exception:
        return ""


def order_item_size_label(item: Dict[str, Any]) -> str:
    if not isinstance(item, dict):
        return ""

    raw_value = item.get("value")
    if isinstance(raw_value, str):
        value_text = raw_value.strip()
        if value_text and not (value_text.startswith("{") and value_text.endswith("}")):
            return value_text

    value_obj = _jsonish_dict(item.get("value_obj")) or _jsonish_dict(raw_value)

    for key in ("label", "name", "display", "size"):
        val = value_obj.get(key)
        if val not in (None, ""):
            return str(val).strip()

    for key in ("gb", "gb_size", "package_size", "volume_gb", "size_gb"):
        val = value_obj.get(key)
        if val not in (None, ""):
            try:
                return f"{_fmt_num(float(val))}GB"
            except Exception:
                return f"{str(val).strip()}GB"

    vol = value_obj.get("volume")
    if vol not in (None, ""):
        try:
            vol_num = float(vol)
        except Exception:
            vol_num = None

        unit = str(value_obj.get("unit") or "").strip().upper()
        if unit in {"GB", "G"} and vol_num is not None:
            return f"{_fmt_num(vol_num)}GB"
        if unit in {"MB", "M", "MBS"} and vol_num is not None:
            return f"{_fmt_num(vol_num)}MB"
        if vol_num is not None:
            if vol_num > 50:
                return f"{_fmt_num(vol_num / 1024.0)}GB"
            return f"{_fmt_num(vol_num)}GB"
        return str(vol).strip()

    return ""


def build_order_display_items(items: List[Dict[str, Any]] | None) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue

        service_type = str(item.get("service_type") or item.get("kind") or "").strip().upper()
        network_label = order_item_network_label(item)
        raw_service_name = str(item.get("serviceName") or "").strip()
        value_text = str(item.get("value") or "").strip()
        phone = str(item.get("phone") or "").strip()
        target_link = str(item.get("target_link") or "").strip()
        quantity = item.get("quantity")
        value_obj = item.get("value_obj") if isinstance(item.get("value_obj"), dict) else _jsonish_dict(item.get("value_obj"))
        sender_name = str((value_obj or {}).get("sender_name") or item.get("sender_name") or "").strip()
        message_body = str((value_obj or {}).get("message_body") or item.get("message_body") or "").strip()
        recipient_count = (value_obj or {}).get("recipient_count") or item.get("recipient_count")
        checker_type = str((value_obj or {}).get("checker_type") or item.get("checker_type") or "").strip().upper()
        comments_count = (value_obj or {}).get("comments_count") or item.get("comments_count")
        service_name = raw_service_name or network_label or "Service"
        title = service_name
        meta_parts: List[str] = []
        detail_lines: List[str] = []
        accent = network_label or service_type.replace("_", " ").title()

        if service_type == "RESULTS_CHECKER":
            title = raw_service_name or f"{checker_type or value_text or 'Results'} Results Checker"
            if phone:
                meta_parts.append(phone)
            detail_lines.append("Delivered by SMS")
        elif service_type == "BULK_SMS":
            title = raw_service_name or "Bulk SMS"
            if recipient_count:
                meta_parts.append(f"{recipient_count} recipients")
            if sender_name:
                detail_lines.append(f"Sender: {sender_name}")
            if message_body:
                compact = re.sub(r"\s+", " ", message_body).strip()
                detail_lines.append(compact[:72] + ("..." if len(compact) > 72 else ""))
        elif "BOOST" in service_type or (value_obj or {}).get("social_boosting"):
            title = raw_service_name or "Social Boosting"
            if quantity not in (None, ""):
                meta_parts.append(f"Qty {quantity}")
            if target_link or phone:
                detail_lines.append(target_link or phone)
            if comments_count:
                detail_lines.append(f"{comments_count} custom comments")
        elif service_type == "AFA":
            title = raw_service_name or "AFA Registration"
            if phone:
                meta_parts.append(phone)
            if value_text:
                detail_lines.append(value_text)
        else:
            size_label = order_item_size_label(item)
            if phone:
                meta_parts.append(phone)
            if size_label:
                meta_parts.append(size_label)

        out.append(
            {
                "network_label": network_label,
                "service_name": title,
                "phone": phone,
                "size_label": order_item_size_label(item),
                "amount": item.get("amount"),
                "service_type": service_type,
                "accent_label": accent,
                "meta_text": " - ".join(part for part in meta_parts if part),
                "detail_lines": [line for line in detail_lines if line],
                "target_link": target_link,
                "quantity": quantity,
                "sender_name": sender_name,
                "recipient_count": recipient_count,
                "checker_type": checker_type,
            }
        )
    return out


def build_order_report_message(order_id: str, display_items: List[Dict[str, Any]] | None) -> str:
    blocks = []
    for item in display_items or []:
        block = [
            str(item.get("network_label") or "").strip(),
            str(item.get("phone") or "").strip(),
            str(item.get("size_label") or "").strip(),
        ]
        block = [line for line in block if line]
        if block:
            blocks.append("\n".join(block))

    parts = [part for part in [str(order_id or "").strip(), "\n\n".join(blocks), "NOT RECEIVED"] if part]
    return "\n".join(parts)
