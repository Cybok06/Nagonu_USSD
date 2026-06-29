from flask import Blueprint, request, jsonify, session, render_template, abort
from bson import ObjectId
from datetime import datetime, timedelta
import os, uuid, random, string, requests, traceback, json, ast, re, threading, time
from urllib.parse import quote

from nagonu_db import db

checkout_bp = Blueprint("checkout", __name__)

# MongoDB Collections
balances_col        = db["balances"]
orders_col          = db["orders"]
transactions_col    = db["transactions"]
services_col        = db["services"]
service_profits_col = db["service_profits"]  # per-customer overrides
users_col           = db["users"]  # ✅ for invoice view
blocked_phones_col  = db["blocked_phone_numbers"]

# Mash Up order SMS alert
ARKESEL_API_KEY = "TGFhVVZvU3NOclJMZFJwWWJ5U2o"
SENDER_ID = "Nagonu"


# ===== DataConnect Provider Config (replaces old DataVerse) ===================
DATACONNECT_BASE_URL = "https://dataconnectgh.com/api/v1"
DATACONNECT_API_KEY = os.getenv(
    "DATACONNECT_API_KEY",
    "90bcf2f236b8c95547b58b531f5c597df8a061a8",  # fallback; you can remove/harden
)

# ===== DataKazina Provider Config ============================================
DATAKAZINA_BASE_URL = os.getenv(
    "DATAKAZINA_BASE_URL",
    "https://reseller.dakazinabusinessconsult.com/api/v1",
)
DATAKAZINA_API_KEY = os.getenv("DATAKAZINA_API_KEY")
DATAKAZINA_TIMEOUT = int(os.getenv("DATAKAZINA_TIMEOUT", "45"))

# ===== CodeCraft Provider Config =============================================
CODECRAFT_BASE_URL = os.getenv("CODECRAFT_BASE_URL", "https://api.codecraftnetwork.com/api")
CODECRAFT_API_KEY = os.getenv("CODECRAFT_API_KEY")

# ===== Portal-02 Provider Config =============================================
PORTAL02_BASE_URL = "https://www.portal-02.com/api/v1"
PORTAL02_API_KEY = "dk_mJmQDFQWmDId4RT_c5HrEghcgwujPAFf"
PORTAL02_WEBHOOK_URL = "https://www.portal-02.com/api/webhooks/orders"
PORTAL02_OFFER_SLUG_MTN_NORMAL = "master_beneficiary_data_bundle"
MTN_NORMAL_SERVICE_ID = "68b8b6a7eb0ced45901c68d2"

# ===== SkPlug Provider Config ================================================
SKPLUG_BASE_URL = os.getenv("SKPLUG_BASE_URL", "https://skplug.onrender.com/api/v1")
SKPLUG_API_TOKEN = os.getenv(
    "SKPLUG_API_TOKEN",
    "270103449bf5069c331eb4511845e6b43a9e9fd7d75d57d1ba317ca9342abcd3",
)
SKPLUG_TIMEOUT = int(os.getenv("SKPLUG_TIMEOUT", "45"))

SERVICE_PROVIDER_CHOICES = ("portal02", "dataconnect", "datakazina", "codecraft", "skplug")
SERVICE_PROVIDER_SET = set(SERVICE_PROVIDER_CHOICES)


# Network ID fallback (internal use)
NETWORK_ID_FALLBACK = {
    "MTN": 3,
    "VODAFONE": 2,
    "AIRTELTIGO": 1,
}

# ===== CodeCraft package cache ===============================================
_CODECRAFT_PKG_CACHE = {"ts": None, "regular": {}, "bigtime": {}}
CODECRAFT_PKG_TTL_SECONDS = 300


# ===== Tiny JSON logger =======================================================
def jlog(event: str, **kv):
    rec = {"evt": event, **kv}
    try:
        print(json.dumps(rec, ensure_ascii=False, separators=(",", ":")))
    except Exception:
        print(f"[LOG_FALLBACK] {event} {kv}")


# ===== Helpers ================================================================
def generate_order_id():
    digits = f"{random.randint(0, 99999999):08d}"
    suffix = "".join(random.choices(string.ascii_uppercase, k=2))
    return f"NAN{digits}{suffix}"


def _normalize_sms_phone(raw: str) -> str | None:
    if not raw:
        return None
    p = str(raw).strip().replace(" ", "").replace("-", "").replace("+", "")
    if p.startswith("0") and len(p) == 10:
        p = "233" + p[1:]
    if p.startswith("233") and len(p) == 12:
        return p
    return None


def _display_order_phone(raw: str | None) -> str:
    p = str(raw or "").strip().replace(" ", "").replace("-", "").replace("+", "")
    if p.startswith("233") and len(p) == 12:
        return "0" + p[3:]
    return p


def _get_order_sms_number() -> str | None:
    try:
        doc = users_col.find_one(
            {"role": "admin", "order_sms.number": {"$nin": [None, ""]}},
            {"order_sms.number": 1},
            sort=[("order_sms.updated_at", -1), ("updated_at", -1)],
        ) or {}
        return ((doc.get("order_sms") or {}).get("number") or "").strip() or None
    except Exception as exc:
        jlog("order_sms_number_lookup_error", error=str(exc))
        return None


def _send_sms(msisdn: str, message: str) -> str:
    """Best-effort SMS send via Arkesel HTTP API. Returns sent/failed/error."""
    try:
        url = (
            "https://sms.arkesel.com/sms/api?action=send-sms"
            f"&api_key={ARKESEL_API_KEY}"
            f"&to={msisdn}"
            f"&from={quote(SENDER_ID)}"
            f"&sms={quote(message)}"
        )
        resp = requests.get(url, timeout=12)
        if resp.status_code == 200 and '"code":"ok"' in resp.text:
            return "sent"
        return "failed"
    except Exception:
        return "error"


def _is_mashup_service_name(name: str | None) -> bool:
    return " ".join(str(name or "").strip().lower().split()) == "mtn mash up data"


def _format_bundle_for_sms(item: dict) -> str:
    value_obj = item.get("value_obj")
    value = item.get("value")

    volume = None
    if isinstance(value_obj, dict):
        volume = value_obj.get("volume") or value_obj.get("mb") or value_obj.get("shared_bundle")
    if volume is None and isinstance(value, dict):
        volume = value.get("volume") or value.get("mb") or value.get("shared_bundle")
    if volume is None and isinstance(value, str):
        parsed = _coerce_value_obj(value)
        if isinstance(parsed, dict):
            volume = parsed.get("volume") or parsed.get("mb") or parsed.get("shared_bundle")
        else:
            match = re.search(r"(\d+(?:\.\d+)?)\s*(gb|mb)?", value, re.I)
            if match:
                number = float(match.group(1))
                unit = (match.group(2) or "mb").lower()
                volume = number * 1000 if unit == "gb" else number

    try:
        mb = float(volume)
    except Exception:
        return ""

    if mb >= 1000:
        gb = mb / 1000.0
        return f"{int(gb)}GB" if abs(gb - int(gb)) < 1e-9 else f"{gb:.2f}GB"
    return f"{int(mb)}MB" if abs(mb - int(mb)) < 1e-9 else f"{mb:.2f}MB"


def _mashup_sms_messages(items: list[dict], created_at: datetime | None = None) -> list[str]:
    messages: list[str] = []
    placed_at = created_at or datetime.utcnow()
    date_text = placed_at.strftime("%Y-%m-%d %H:%M")
    for item in items or []:
        if not _is_mashup_service_name(item.get("serviceName")):
            continue
        phone = _display_order_phone(item.get("phone"))
        bundle = _format_bundle_for_sms(item)
        if phone and bundle:
            messages.append(f"{phone} {bundle} {date_text}")
    return messages


def _send_mashup_order_sms_async(order_id: str, created_at: datetime, items: list[dict]) -> None:
    messages = _mashup_sms_messages(items, created_at)
    if not messages:
        return

    configured_number = _get_order_sms_number()
    msisdn = _normalize_sms_phone(configured_number or "")
    if not msisdn:
        jlog("mashup_order_sms_missing_number", order_id=order_id)
        return

    def _worker():
        for message in messages:
            status = _send_sms(msisdn, message)
            jlog("mashup_order_sms", order_id=order_id, to=msisdn, message=message, status=status)

    try:
        threading.Thread(target=_worker, daemon=True).start()
    except Exception as exc:
        jlog("mashup_order_sms_spawn_error", order_id=order_id, error=str(exc))


def _money(v):
    try:
        return float(v)
    except Exception:
        return 0.0


def _clean_api_key(value) -> str:
    """
    Remove stray unicode/control characters from API keys.
    """
    if not value:
        return ""
    if not isinstance(value, str):
        value = str(value)
    cleaned = re.sub(r"[^\x20-\x7E]+", "", value)
    return cleaned.strip()


def _to_float(x, default=None):
    try:
        return float(x)
    except Exception:
        return default


def _coerce_value_obj(v):
    """
    Accepts dict, JSON string, or python-dict-like string.
    Returns a dict (possibly empty).
    """
    if isinstance(v, dict):
        return v
    if not v:
        return {}
    s = str(v).strip()
    if s.startswith("{") and s.endswith("}"):
        try:
            d = json.loads(s)
            return d if isinstance(d, dict) else {}
        except Exception:
            try:
                d = ast.literal_eval(s)
                return d if isinstance(d, dict) else {}
            except Exception:
                return {}
    return {}


def _pick_nested_response_value(payload, keys: tuple[str, ...]):
    if not isinstance(payload, dict):
        return None

    for key in keys:
        val = payload.get(key)
        if val not in (None, "", []):
            return val

    for container_key in ("data", "result", "order"):
        nested = payload.get(container_key)
        if not isinstance(nested, dict):
            continue
        for key in keys:
            val = nested.get(key)
            if val not in (None, "", []):
                return val

    return None


# ===== Ported number fields ==================================================
def _extract_ported_fields(item: dict) -> dict:
    if not isinstance(item, dict):
        return {}
    out = {}
    if "ported_confirmed" in item:
        out["ported_confirmed"] = bool(item.get("ported_confirmed"))
    for key in ("ported_expected_network", "ported_detected_network", "ported_prefix"):
        val = item.get(key)
        if val not in (None, ""):
            out[key] = str(val)
    return out


# ===== Profit helpers (absolute profit amount) ================================
def _get_service_default_profit_percent(service_doc):
    return _to_float(service_doc.get("default_profit_percent"), 0.0) or 0.0


def _get_customer_profit_override_percent(service_id, customer_id_obj):
    ov = service_profits_col.find_one({"service_id": service_id, "customer_id": customer_id_obj})
    return _to_float(ov.get("profit_percent"), None) if ov else None


def _effective_profit_percent(service_doc, customer_id_obj):
    override = _get_customer_profit_override_percent(service_doc["_id"], customer_id_obj)
    return override if override is not None else _get_service_default_profit_percent(service_doc)


def _pick_offer_base_amount_from_service(svc_doc, value_obj, raw_value):
    """
    Try to recover the base (wholesale) amount from the selected offer in svc_doc.offers.
    """
    try:
        offers = svc_doc.get("offers") or []
        vid = (value_obj or {}).get("id")
        vvol = (value_obj or {}).get("volume")
        for of in offers:
            of_val = of.get("value")
            of_amt = _to_float(of.get("amount"))
            if isinstance(of_val, str) and of_val.strip().startswith("{") and of_val.strip().endswith("}"):
                try:
                    of_val = json.loads(of_val)
                except Exception:
                    try:
                        of_val = ast.literal_eval(of_val)
                    except Exception:
                        pass
            if isinstance(of_val, dict):
                if (vid is not None and of_val.get("id") == vid) or (vvol is not None and of_val.get("volume") == vvol):
                    return of_amt
            else:
                if raw_value is not None and of_val == raw_value:
                    return of_amt
    except Exception:
        pass
    return None


def _derive_base_profit(amount_total, base_amount_hint, eff_percent):
    a = _money(amount_total)
    if a <= 0:
        return 0.0, 0.0
    if base_amount_hint is not None and base_amount_hint > 0:
        base = float(base_amount_hint)
        profit = round(a - base, 2)
        if profit < 0:
            profit = 0.0
            base = a
        return round(base, 2), profit
    p = _to_float(eff_percent, 0.0) or 0.0
    try:
        base = round(a / (1.0 + (p / 100.0)), 2) if p > 0 else a
    except Exception:
        base = a
    profit = round(a - base, 2)
    if profit < 0:
        profit = 0.0
        base = a
    return base, profit


# ===== Field resolvers =======================================================
def _resolve_network_id(item: dict, value_obj: dict, svc_doc: dict | None):
    """
    Internal numeric network ID, used only for duplicate guards / reporting.
    Not sent to providers.
    """
    nid = (item or {}).get("network_id") or (value_obj or {}).get("network_id")
    if nid not in (None, "", []):
        try:
            return int(nid)
        except Exception:
            pass
    if svc_doc:
        try:
            if "network_id" in svc_doc and svc_doc["network_id"] not in (None, ""):
                return int(svc_doc["network_id"])
            guess = (svc_doc.get("name") or svc_doc.get("network") or "").strip().upper()
            if guess and guess in NETWORK_ID_FALLBACK:
                return int(NETWORK_ID_FALLBACK[guess])
        except Exception:
            pass
    if not svc_doc:
        name = (item.get("serviceName") or "").strip().upper()
        if name in NETWORK_ID_FALLBACK:
            return int(NETWORK_ID_FALLBACK[name])
    return None


def _resolve_dataconnect_network(svc_doc: dict | None, item: dict) -> str | None:
    """
    Resolve generic 'network' slug we also reuse:
      - 'mtn'
      - 'telecel'
      - 'airteltigo'
    Used for routing (DataConnect vs manual processing).
    """
    doc = svc_doc

    # Fallback: look up by service name if svc_doc is missing
    if not doc:
        sname = (item.get("serviceName") or "").strip()
        if sname:
            try:
                doc = services_col.find_one(
                    {"name": sname},
                    {"service_network": 1, "network": 1, "name": 1},
                )
            except Exception:
                doc = None

    candidates = []
    if doc:
        candidates.append(doc.get("service_network"))
        candidates.append(doc.get("network"))
        candidates.append(doc.get("name"))

    candidates.append(item.get("network"))
    candidates.append(item.get("network_name"))
    candidates.append(item.get("serviceName"))

    joined = " ".join(str(c) for c in candidates if c).lower()

    if "mtn" in joined:
        return "mtn"

    # Telecel / Vodafone rebrand
    if "telecel" in joined or "vodafone" in joined:
        return "telecel"

    # AirtelTigo / AT / iShare
    if (
        "airteltigo" in joined
        or "airtel tigo" in joined
        or "airtel-tigo" in joined
        or "at - ishare" in joined
        or "i share" in joined
        or "ishare" in joined
    ):
        return "airteltigo"

    return None


def _resolve_codecraft_network_name(svc_doc: dict | None, item: dict) -> str | None:
    resolved = _resolve_dataconnect_network(svc_doc, item)
    if resolved == "mtn":
        return "MTN"
    if resolved == "telecel":
        return "TELECEL"
    if resolved == "airteltigo":
        return "AT"

    name = ""
    if svc_doc:
        name = " ".join(
            str(x)
            for x in (
                svc_doc.get("service_network"),
                svc_doc.get("network"),
                svc_doc.get("name"),
            )
            if x
        )
    if not name:
        name = " ".join(
            str(x)
            for x in (item.get("serviceName"), item.get("network"), item.get("network_name"))
            if x
        )
    low = name.lower()
    if "telecel" in low or "vodafone" in low:
        return "TELECEL"
    if "mtn" in low:
        return "MTN"
    if "airteltigo" in low or "tigo" in low or "ishare" in low or "i share" in low or low.startswith("at "):
        return "AT"
    return None


def _resolve_package_size_gb(value_obj: dict, item: dict) -> int | None:
    """
    Resolve bundle size (integer GB) to use as provider "volume".
    """
    if not isinstance(value_obj, dict):
        value_obj = value_obj or {}

    # 1) explicit GB fields
    for key in ("gb", "gb_size", "package_size", "volume_gb", "size_gb"):
        val = value_obj.get(key)
        if val not in (None, "", []):
            try:
                return int(float(val))
            except Exception:
                pass

    # 2) 'volume' field (can be GB or MB)
    vol = value_obj.get("volume")
    if vol not in (None, "", []):
        try:
            vol_f = float(vol)
            if vol_f > 50:
                gb = max(1, round(vol_f / 1024.0))
            else:
                gb = vol_f
            return int(gb)
        except Exception:
            pass

    # 3) Parse from item['value'] string like '1GB', '5 GB'
    raw_val = item.get("value") or ""
    if isinstance(raw_val, str):
        m = re.search(r"(\d+(?:\.\d+)?)\s*gb", raw_val.lower())
        if m:
            try:
                return int(float(m.group(1)))
            except Exception:
                pass
        m2 = re.search(r"(\d+(?:\.\d+)?)", raw_val)
        if m2:
            try:
                return int(float(m2.group(1)))
            except Exception:
                pass

    return None


def _resolve_skplug_gb_size(value_obj: dict, item: dict) -> int | None:
    """
    Resolve the decimal GB size expected by SkPlug.

    Service offers in this codebase commonly store `volume` in decimal MB-like
    units such as 1000, 2000, 25000. For SkPlug we convert those to 1, 2, 25.
    """
    if not isinstance(value_obj, dict):
        value_obj = _coerce_value_obj(value_obj)

    for key in ("gb_size", "gb", "package_size", "volume_gb", "size_gb"):
        val = value_obj.get(key)
        if val not in (None, "", []):
            try:
                return max(1, int(round(float(val))))
            except Exception:
                pass

    vol = value_obj.get("volume") or value_obj.get("mb")
    if vol not in (None, "", []):
        try:
            vol_f = float(vol)
            if vol_f >= 100:
                return max(1, int(round(vol_f / 1000.0)))
            return max(1, int(round(vol_f)))
        except Exception:
            pass

    raw_val = item.get("value") or item.get("label") or ""
    if isinstance(raw_val, str):
        m = re.search(r"(\d+(?:\.\d+)?)\s*gb", raw_val.lower())
        if m:
            try:
                return max(1, int(round(float(m.group(1)))))
            except Exception:
                pass

    fallback = _resolve_package_size_gb(value_obj, item)
    if fallback is not None:
        try:
            return max(1, int(fallback))
        except Exception:
            return None
    return None


def _resolve_datakazina_shared_bundle(value_obj: dict, item: dict) -> int | None:
    """
    Resolve DataKazina shared_bundle identifier from selected offer/value.

    Priority:
      1) value_obj["id"] (numeric)
      2) value_obj["shared_bundle"] (numeric)
      3) value_obj["volume"] / "mb" / item["value"] fallback

    If volume appears to be MB (>= 100), convert to GB for the ID fallback.
    """
    if not isinstance(value_obj, dict):
        value_obj = _coerce_value_obj(value_obj)

    def _as_int(val):
        if val in (None, "", []):
            return None
        try:
            return int(float(val))
        except Exception:
            return None

    for key in ("id", "shared_bundle"):
        got = _as_int(value_obj.get(key))
        if got is not None:
            return got

    vol = value_obj.get("volume") or value_obj.get("mb")
    got = _as_int(vol)
    if got is not None:
        if got >= 100:
            return max(1, int(round(got / 1000.0)))
        return got

    raw_val = item.get("value") or item.get("label")
    if isinstance(raw_val, str):
        m = re.search(r"(\d+(?:\.\d+)?)\s*gb", raw_val.lower())
        if m:
            try:
                return int(float(m.group(1)))
            except Exception:
                pass
        m2 = re.search(r"(\d+(?:\.\d+)?)", raw_val)
        if m2:
            try:
                val = int(float(m2.group(1)))
                if val >= 100:
                    return max(1, int(round(val / 1000.0)))
                return val
            except Exception:
                pass
    else:
        got = _as_int(raw_val)
        if got is not None:
            return got

    return None


def _normalize_portal02_phone(phone: str) -> str:
    """
    Normalize Ghana numbers for Portal-02 only.
    - 0530xxxxxx -> 233530xxxxxx
    - 233xxxxxxxxx stays
    """
    p = re.sub(r"\s+", "", str(phone or ""))
    if p.startswith("+"):
        p = p[1:]
    if p.startswith("0") and len(p) >= 10:
        return "233" + p[1:]
    if p.startswith("233"):
        return p
    return p


def _normalize_phone_for_blocking(phone: str) -> str:
    digits = re.sub(r"\D+", "", str(phone or ""))
    if not digits:
        return ""
    if digits.startswith("233") and len(digits) == 12:
        return f"0{digits[3:]}"
    if len(digits) == 9:
        return f"0{digits}"
    return digits


def _phone_block_match_keys(phone: str) -> set[str]:
    norm = _normalize_phone_for_blocking(phone)
    if not norm:
        return set()
    keys = {norm}
    if norm.startswith("0") and len(norm) == 10:
        keys.add(f"233{norm[1:]}")
    if norm.startswith("233") and len(norm) == 12:
        keys.add(f"0{norm[3:]}")
    return keys


def _is_mtn_normal_service(service_id_raw, svc_doc) -> bool:
    try:
        if service_id_raw and str(service_id_raw) == MTN_NORMAL_SERVICE_ID:
            return True
    except Exception:
        pass
    try:
        if svc_doc and svc_doc.get("_id") and str(svc_doc.get("_id")) == MTN_NORMAL_SERVICE_ID:
            return True
    except Exception:
        pass
    return False


def _build_bundle_key(value_obj: dict, item: dict):
    """
    Build a generic bundle key for duplicate detection.
    Returns ('bundle', <normalized_value>) or None.
    """
    val = None
    if isinstance(value_obj, dict):
        for key in ("id", "volume", "code", "package_size", "gb"):
            if value_obj.get(key) not in (None, "", []):
                val = value_obj.get(key)
                break
    if val is None:
        val = item.get("value") or item.get("label")

    if val is None:
        return None

    try:
        norm = int(float(val))
    except Exception:
        norm = str(val).strip()

    return ("bundle", norm)


# ===== Provider callers (used by background worker) ==========================
def _codecraft_get_packages_cached():
    now = time.time()
    ts = _CODECRAFT_PKG_CACHE.get("ts")
    if ts and (now - ts) < CODECRAFT_PKG_TTL_SECONDS:
        return _CODECRAFT_PKG_CACHE.get("regular", {}), _CODECRAFT_PKG_CACHE.get("bigtime", {})

    if not CODECRAFT_API_KEY:
        return {}, {}

    url = f"{CODECRAFT_BASE_URL.rstrip('/')}/packages.php"
    headers = {
        "Accept": "application/json",
        "x-api-key": CODECRAFT_API_KEY,
    }
    try:
        resp = requests.get(url, headers=headers, timeout=30)
        text = resp.text or ""
        try:
            payload = resp.json()
        except Exception:
            payload = {"raw": text} if text else {}

        root = payload.get("data") if isinstance(payload, dict) else {}
        if not isinstance(root, dict):
            root = {}
        if isinstance(root.get("data"), dict):
            root = root.get("data") or {}

        reg_list = root.get("regular_packages") or []
        big_list = root.get("bigtime_packages") or []

        def _pull_field(dct, keys):
            for k in keys:
                if k in dct:
                    return dct.get(k)
            return None

        regular_map = {}
        bigtime_map = {}

        for p in reg_list if isinstance(reg_list, list) else []:
            if not isinstance(p, dict):
                continue
            net = _pull_field(p, ("network", "Network", "operator", "provider"))
            gig = _pull_field(p, ("package", "gig", "Gig", "volume", "gb"))
            amt = _pull_field(p, ("amount", "price", "Amount", "cost"))
            if net is None or gig is None:
                continue
            try:
                gig_int = int(float(gig))
            except Exception:
                continue
            key = (str(net).strip().upper(), gig_int)
            regular_map[key] = _to_float(amt, None)

        for p in big_list if isinstance(big_list, list) else []:
            if not isinstance(p, dict):
                continue
            net = _pull_field(p, ("network", "Network", "operator", "provider"))
            gig = _pull_field(p, ("package", "gig", "Gig", "volume", "gb"))
            amt = _pull_field(p, ("amount", "price", "Amount", "cost"))
            if net is None or gig is None:
                continue
            try:
                gig_int = int(float(gig))
            except Exception:
                continue
            key = (str(net).strip().upper(), gig_int)
            bigtime_map[key] = _to_float(amt, None)

        _CODECRAFT_PKG_CACHE["ts"] = now
        _CODECRAFT_PKG_CACHE["regular"] = regular_map
        _CODECRAFT_PKG_CACHE["bigtime"] = bigtime_map
        jlog(
            "codecraft_packages_loaded",
            regular_count=len(regular_map),
            bigtime_count=len(bigtime_map),
            regular_keys=list(regular_map.keys())[:3],
            bigtime_keys=list(bigtime_map.keys())[:3],
        )
        return regular_map, bigtime_map
    except Exception as e:
        jlog("codecraft_packages_error", error=str(e))
        return {}, {}


def _codecraft_submit_regular(phone: str, gig: int, network: str):
    if not CODECRAFT_API_KEY:
        return False, {"success": False, "error": "CODECRAFT API key not configured", "http_status": 500}, None
    url = f"{CODECRAFT_BASE_URL.rstrip('/')}/initiate.php"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "x-api-key": CODECRAFT_API_KEY,
    }
    body = {"recipient_number": phone, "gig": str(gig), "network": network}
    masked = phone[:3] + "***" + phone[-2:] if phone and len(phone) >= 5 else "***"
    jlog(
        "codecraft_submit_request",
        mode="regular",
        network=network,
        gig=gig,
        phone=masked,
        url=url,
    )
    try:
        resp = requests.post(url, headers=headers, json=body, timeout=45)
        text = resp.text or ""
        try:
            payload = resp.json()
        except Exception:
            payload = {"raw": text} if text else {}
        reference_id = None
        if isinstance(payload, dict):
            reference_id = payload.get("reference_id") or payload.get("referenceId")
        ok = isinstance(payload, dict) and payload.get("status") == 200 and bool(reference_id)
        if isinstance(payload, dict):
            payload.setdefault("http_status", resp.status_code)
        jlog(
            "codecraft_submit_response",
            mode="regular",
            ok=ok,
            network=network,
            gig=gig,
            payload=payload,
        )
        return ok, payload, reference_id
    except requests.RequestException as e:
        return False, {"success": False, "error": str(e), "type": "NETWORK_ERROR", "http_status": 599}, None


def _codecraft_submit_bigtime(phone: str, gig: int, network: str):
    if not CODECRAFT_API_KEY:
        return False, {"success": False, "error": "CODECRAFT API key not configured", "http_status": 500}, None
    url = f"{CODECRAFT_BASE_URL.rstrip('/')}/special.php"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "x-api-key": CODECRAFT_API_KEY,
    }
    body = {"recipient_number": phone, "gig": str(gig), "network": network}
    masked = phone[:3] + "***" + phone[-2:] if phone and len(phone) >= 5 else "***"
    jlog(
        "codecraft_submit_request",
        mode="bigtime",
        network=network,
        gig=gig,
        phone=masked,
        url=url,
    )
    try:
        resp = requests.post(url, headers=headers, json=body, timeout=45)
        text = resp.text or ""
        try:
            payload = resp.json()
        except Exception:
            payload = {"raw": text} if text else {}
        reference_id = None
        if isinstance(payload, dict):
            reference_id = payload.get("reference_id") or payload.get("referenceId")
        ok = isinstance(payload, dict) and payload.get("status") == 200 and bool(reference_id)
        if isinstance(payload, dict):
            payload.setdefault("http_status", resp.status_code)
        jlog(
            "codecraft_submit_response",
            mode="bigtime",
            ok=ok,
            network=network,
            gig=gig,
            payload=payload,
        )
        return ok, payload, reference_id
    except requests.RequestException as e:
        return False, {"success": False, "error": str(e), "type": "NETWORK_ERROR", "http_status": 599}, None


def _send_dataconnect_order(
    phone: str,
    network_id: int,
    shared_bundle: int,
    external_ref: str,
    order_id: str,
    debug_events: list,
):
    """
    Sends a single bundle order to DataConnect.

    POST https://dataconnectgh.com/api/v1/buy-other-package

    Body JSON:
        {
            "recipient_msisdn": "0551053716",
            "network_id": 3,
            "shared_bundle": 1000
        }
    """
    if not DATACONNECT_API_KEY:
        err = {
            "success": False,
            "message": "DATACONNECT API key not configured",
            "http_status": 500,
        }
        jlog("dataconnect_config_error", order_id=order_id, ref=external_ref)
        return False, err

    url = f"{DATACONNECT_BASE_URL.rstrip('/')}/buy-other-package"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "x-api-key": DATACONNECT_API_KEY,
    }
    body = {
        "recipient_msisdn": phone,
        "network_id": int(network_id),
        "shared_bundle": int(shared_bundle),
    }

    masked = phone[:3] + "***" + phone[-2:] if phone and len(phone) >= 5 else "***"

    jlog(
        "dataconnect_request_body",
        order_id=order_id,
        ref=external_ref,
        url=url,
        body={
            "recipient_msisdn": masked,
            "network_id": body["network_id"],
            "shared_bundle": body["shared_bundle"],
        },
    )

    try:
        resp = requests.post(
            url,
            headers=headers,
            json=body,
            timeout=45,
        )
        text = resp.text or ""
        try:
            payload = resp.json()
        except Exception:
            payload = {"raw": text} if text else {}

        ok = (
            resp.status_code in (200, 201)
            and isinstance(payload, dict)
            and bool(payload.get("success")) is True
        )
        if isinstance(payload, dict):
            payload.setdefault("http_status", resp.status_code)

        dbg = {
            "status": resp.status_code,
            "body_len": len(text),
        }
        jlog("dataconnect_response", order_id=order_id, ref=external_ref, payload=payload)
        jlog("dataconnect_call", order_id=order_id, ref=external_ref, ok=ok, debug=dbg)

        debug_events.append(
            {
                "when": datetime.utcnow(),
                "stage": "dataconnect-buy-other-package",
                "ok": ok,
                "http_status": resp.status_code,
            }
        )
        return ok, payload

    except requests.RequestException as e:
        jlog(
            "dataconnect_network_error",
            order_id=order_id,
            ref=external_ref,
            error=str(e),
        )
        return False, {
            "success": False,
            "error": str(e),
            "type": "NETWORK_ERROR",
            "http_status": 599,
        }


def _datakazina_submit_single(
    recipient_msisdn: str,
    shared_bundle: int,
    incoming_api_ref: str,
    meta: dict | None = None,
):
    """
    Submit a single DataKazina order. Always sends network_id=3.
    """
    api_key = _clean_api_key(DATAKAZINA_API_KEY)
    if not api_key:
        payload = {
            "success": False,
            "message": "DATAKAZINA API key not configured",
        }
        return {
            "ok": False,
            "http_status": 500,
            "provider": "datakazina",
            "provider_reference": None,
            "response": payload,
            "message": payload.get("message"),
        }
    if shared_bundle is None:
        payload = {"success": False, "message": "shared_bundle missing"}
        return {
            "ok": False,
            "http_status": 400,
            "provider": "datakazina",
            "provider_reference": None,
            "response": payload,
            "message": payload.get("message"),
        }

    url = f"{DATAKAZINA_BASE_URL.rstrip('/')}/buy-data-package"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "x-api-key": api_key,
    }
    body = {
        "recipient_msisdn": recipient_msisdn,
        "network_id": 3,
        "shared_bundle": int(shared_bundle),
        "incoming_api_ref": incoming_api_ref,
    }

    masked = (
        recipient_msisdn[:3] + "***" + recipient_msisdn[-2:]
        if recipient_msisdn and len(recipient_msisdn) >= 5
        else "***"
    )
    jlog(
        "datakazina_request_prepared",
        ref=incoming_api_ref,
        phone=masked,
        shared_bundle=body["shared_bundle"],
        url=url,
        meta=meta or {},
    )
    jlog(
        "datakazina_request_body",
        ref=incoming_api_ref,
        url=url,
        body={
            "recipient_msisdn": masked,
            "network_id": body["network_id"],
            "shared_bundle": body["shared_bundle"],
            "incoming_api_ref": incoming_api_ref,
        },
        meta=meta or {},
    )

    try:
        resp = requests.post(url, headers=headers, json=body, timeout=DATAKAZINA_TIMEOUT)
        text = resp.text or ""
        try:
            payload = resp.json()
        except Exception:
            payload = {"raw": text} if text else {}

        ok = (
            resp.status_code >= 200
            and resp.status_code < 300
            and isinstance(payload, dict)
            and payload.get("success") is True
        )
        provider_ref = payload.get("transaction_code") if isinstance(payload, dict) else None
        message = payload.get("message") if isinstance(payload, dict) else None

        jlog(
            "datakazina_response",
            ref=incoming_api_ref,
            ok=ok,
            http_status=resp.status_code,
            provider_reference=provider_ref,
            payload=payload,
        )

        return {
            "ok": ok,
            "http_status": resp.status_code,
            "provider": "datakazina",
            "provider_reference": provider_ref,
            "response": payload,
            "message": message or ("OK" if ok else "DataKazina request failed"),
        }
    except requests.RequestException as e:
        err = {"success": False, "error": str(e), "type": "NETWORK_ERROR"}
        jlog("datakazina_error", ref=incoming_api_ref, error=str(e))
        return {
            "ok": False,
            "http_status": 599,
            "provider": "datakazina",
            "provider_reference": None,
            "response": err,
            "message": "Network error",
        }


def _datakazina_submit_many_as_single_orders(jobs: list[dict]):
    results = []
    success_count = 0
    failed_count = 0

    for job in jobs or []:
        incoming_ref = job.get("incoming_api_ref") or job.get("provider_request_order_id")
        raw_phone = job.get("phone")
        masked = (
            raw_phone[:3] + "***" + raw_phone[-2:]
            if raw_phone and len(raw_phone) >= 5
            else "***"
        )
        jlog(
            "datakazina_worker_job",
            ref=incoming_ref,
            order_id=job.get("order_id"),
            line_index=job.get("line_index"),
            phone=masked,
            shared_bundle=job.get("shared_bundle"),
        )
        res = _datakazina_submit_single(
            recipient_msisdn=job.get("phone"),
            shared_bundle=job.get("shared_bundle"),
            incoming_api_ref=job.get("incoming_api_ref") or job.get("provider_request_order_id"),
            meta={
                "order_id": job.get("order_id"),
                "line_index": job.get("line_index"),
            },
        )
        if res.get("ok"):
            success_count += 1
        else:
            failed_count += 1
        results.append({"job": job, "result": res})

    return {
        "total": len(jobs or []),
        "success_count": success_count,
        "failed_count": failed_count,
        "results": results,
    }


def _skplug_submit_single(
    recipient: str,
    gb_size: int,
    network: str = "MTN",
    incoming_api_ref: str | None = None,
    meta: dict | None = None,
):
    token = _clean_api_key(SKPLUG_API_TOKEN)
    if not token:
        payload = {"success": False, "message": "SKPLUG API token not configured"}
        return {
            "ok": False,
            "http_status": 500,
            "provider": "skplug",
            "provider_reference": None,
            "provider_order_id": None,
            "response": payload,
            "message": payload.get("message"),
        }

    if not recipient:
        payload = {"success": False, "message": "recipient missing"}
        return {
            "ok": False,
            "http_status": 400,
            "provider": "skplug",
            "provider_reference": None,
            "provider_order_id": None,
            "response": payload,
            "message": payload.get("message"),
        }

    if gb_size in (None, "", []):
        payload = {"success": False, "message": "gb_size missing"}
        return {
            "ok": False,
            "http_status": 400,
            "provider": "skplug",
            "provider_reference": None,
            "provider_order_id": None,
            "response": payload,
            "message": payload.get("message"),
        }

    url = f"{SKPLUG_BASE_URL.rstrip('/')}/order/"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    body = {
        "recipient": str(recipient).strip(),
        "network": str(network or "MTN").strip().upper(),
        "gb_size": str(int(gb_size)),
    }

    masked = (
        recipient[:3] + "***" + recipient[-2:]
        if recipient and len(recipient) >= 5
        else "***"
    )
    jlog(
        "skplug_request_prepared",
        ref=incoming_api_ref,
        phone=masked,
        gb_size=body["gb_size"],
        network=body["network"],
        url=url,
        meta=meta or {},
    )

    try:
        resp = requests.post(url, headers=headers, json=body, timeout=SKPLUG_TIMEOUT)
        text = resp.text or ""
        try:
            payload = resp.json()
        except Exception:
            payload = {"raw": text} if text else {}

        ok = bool(resp.ok)
        if isinstance(payload, dict) and payload.get("success") is False:
            ok = False

        provider_ref = _pick_nested_response_value(
            payload,
            ("transaction_code", "reference", "order_id", "orderId", "id"),
        )
        provider_order_id = _pick_nested_response_value(
            payload,
            ("order_id", "orderId", "id", "transaction_code", "reference"),
        )
        message = (
            _pick_nested_response_value(payload, ("message", "status", "detail"))
            if isinstance(payload, dict)
            else None
        )

        jlog(
            "skplug_response",
            ref=incoming_api_ref,
            ok=ok,
            http_status=resp.status_code,
            provider_reference=provider_ref,
            provider_order_id=provider_order_id,
            payload=payload,
        )

        return {
            "ok": ok,
            "http_status": resp.status_code,
            "provider": "skplug",
            "provider_reference": provider_ref,
            "provider_order_id": provider_order_id,
            "response": payload,
            "message": message or ("OK" if ok else "SkPlug request failed"),
        }
    except requests.RequestException as e:
        err = {"success": False, "error": str(e), "type": "NETWORK_ERROR"}
        jlog("skplug_error", ref=incoming_api_ref, error=str(e))
        return {
            "ok": False,
            "http_status": 599,
            "provider": "skplug",
            "provider_reference": None,
            "provider_order_id": None,
            "response": err,
            "message": "Network error",
        }


# ===== Unavailability checker ================================================
def _service_unavailability_reason(svc_doc: dict):
    """
    Returns (is_unavailable, reason_text)
    """
    if not svc_doc:
        return True, "Closed"

    status = (svc_doc.get("status") or "").strip().upper()
    availability = (svc_doc.get("availability") or "").strip().upper()

    if availability in {"OUT_OF_STOCK", "OUT OF STOCK", "OUTOFSTOCK"}:
        return True, "Out of stock"

    if status == "CLOSED":
        return True, "Closed"

    return False, ""


# ===== Duplicate-in-processing guard =========================================
DUP_WINDOW_MINUTES = 30


def _normalize_amount_key(v):
    try:
        return float(f"{float(v):.2f}")
    except Exception:
        return 0.0


def _has_processing_conflict_strict(
    phone: str,
    service_id_raw: str | None,
    svc_name: str | None,
    network_id: int | None,
    bundle_key: tuple | None,
    amount_key: float,
) -> bool:
    if not phone or network_id is None or bundle_key is None:
        return False

    window_start = datetime.utcnow() - timedelta(minutes=DUP_WINDOW_MINUTES)
    kind, bval = bundle_key

    elem = {
        "phone": phone,
        "network_id": network_id,
        "bundle_key.kind": kind,
        "bundle_key.value": bval,
        "amount": amount_key,
    }
    if service_id_raw:
        elem["serviceId"] = service_id_raw

    q = {
        "status": {"$in": ["pending", "processing"]},
        "created_at": {"$gte": window_start},
        "items": {"$elemMatch": elem},
    }
    if orders_col.find_one(q, {"_id": 1}):
        return True

    alt = {
        "phone": phone,
        "network_id": network_id,
        "amount": amount_key,
    }
    if kind == "offer":
        alt["value_obj.id"] = bval
    else:
        alt["value_obj.volume"] = bval
    if service_id_raw:
        alt["serviceId"] = service_id_raw

    q2 = {
        "status": {"$in": ["pending", "processing"]},
        "created_at": {"$gte": window_start},
        "items": {"$elemMatch": alt},
    }
    return bool(orders_col.find_one(q2, {"_id": 1}))


# ===== BACKGROUND WORKER =====================================================
def _background_process_providers(order_id: str, api_jobs: list[dict]):
    """
    Runs in a separate thread AFTER the HTTP response is sent.
    It picks queued lines and calls configured providers, then updates the order doc.
    """
    jlog("checkout_bg_worker_start", order_id=order_id, jobs=len(api_jobs))
    local_debug = []

    datakazina_jobs = [job for job in (api_jobs or []) if job.get("provider") == "datakazina"]
    if datakazina_jobs:
        summary = _datakazina_submit_many_as_single_orders(datakazina_jobs)
        jlog(
            "datakazina_batch_complete",
            order_id=order_id,
            total=summary.get("total"),
            success_count=summary.get("success_count"),
            failed_count=summary.get("failed_count"),
        )

        for entry in summary.get("results", []):
            job = entry.get("job") or {}
            result = entry.get("result") or {}
            line_ref = job.get("provider_request_order_id")
            job_order_id = job.get("order_id") or order_id
            if not line_ref or not job_order_id:
                continue

            ok = bool(result.get("ok"))
            payload = result.get("response")
            provider_ref = result.get("provider_reference")

            try:
                orders_col.update_one(
                    {
                        "order_id": job_order_id,
                        "items.provider_request_order_id": line_ref,
                    },
                    {
                        "$set": {
                            "items.$.api_status": "success" if ok else "processing",
                            "items.$.line_status": "processing",
                            "items.$.api_response": payload,
                            "items.$.provider_reference": provider_ref,
                            "items.$.provider_order_id": provider_ref,
                            "items.$.provider": "datakazina",
                            "status": "processing",
                            "updated_at": datetime.utcnow(),
                        }
                    },
                )
            except Exception as e:
                jlog("datakazina_update_error", order_id=job_order_id, ref=line_ref, error=str(e))

    for job in api_jobs:
        line_ref = job.get("provider_request_order_id")
        try:
            phone = job.get("phone")
            provider = job.get("provider")
            job_order_id = job.get("order_id") or order_id
            if not job_order_id:
                continue

            if provider == "datakazina":
                continue

            if provider == "skplug":
                skplug_network = (job.get("skplug_network") or "MTN").strip().upper()
                skplug_gb_size = job.get("skplug_gb_size")
                result = _skplug_submit_single(
                    recipient=phone,
                    gb_size=skplug_gb_size,
                    network=skplug_network,
                    incoming_api_ref=line_ref,
                    meta={
                        "order_id": job_order_id,
                        "line_index": job.get("line_index"),
                    },
                )
                ok = bool(result.get("ok"))
                payload = result.get("response")
                provider_ref = result.get("provider_reference")
                provider_order_id = result.get("provider_order_id") or provider_ref

                orders_col.update_one(
                    {
                        "order_id": job_order_id,
                        "items.provider_request_order_id": line_ref,
                    },
                    {
                        "$set": {
                            "items.$.api_status": "success" if ok else "processing",
                            "items.$.line_status": "processing",
                            "items.$.api_response": payload,
                            "items.$.provider_reference": provider_ref,
                            "items.$.provider_order_id": provider_order_id,
                            "items.$.provider_network": skplug_network,
                            "items.$.provider_gb_size": skplug_gb_size,
                            "items.$.provider": "skplug",
                            "status": "processing",
                            "updated_at": datetime.utcnow(),
                        }
                    },
                )
                continue

            if provider == "dataconnect":
                dataconnect_network_id = job.get("network_id")
                dataconnect_shared_bundle = job.get("shared_bundle")

                ok, payload = _send_dataconnect_order(
                    phone=phone,
                    network_id=dataconnect_network_id,
                    shared_bundle=dataconnect_shared_bundle,
                    external_ref=line_ref,
                    order_id=job_order_id,
                    debug_events=local_debug,
                )

                provider_ref = None
                provider_order_id = None
                if isinstance(payload, dict):
                    provider_ref = (
                        payload.get("transaction_code")
                        or payload.get("reference")
                        or payload.get("order_reference")
                    )
                    provider_order_id = (
                        payload.get("orderId")
                        or payload.get("order_id")
                        or payload.get("transaction_code")
                    )

                # Update this specific line inside the order items
                orders_col.update_one(
                    {
                        "order_id": job_order_id,
                        "items.provider_request_order_id": line_ref,
                    },
                    {
                        "$set": {
                            "items.$.api_status": "success" if ok else "processing",
                            "items.$.line_status": "processing",
                            "items.$.api_response": payload,
                            "items.$.provider_reference": provider_ref,
                            "items.$.provider_order_id": provider_order_id,
                            "status": "processing",
                            "updated_at": datetime.utcnow(),
                        }
                    },
                )
                continue

            if provider == "codecraft":
                provider_network = job.get("provider_network")
                provider_gig = job.get("provider_gig")
                provider_mode = job.get("provider_mode")
                provider_amount = job.get("provider_amount")

                if provider_network == "TELECEL" and provider_mode == "bigtime":
                    provider_mode = "regular"

                if provider_mode == "bigtime":
                    ok, payload, reference_id = _codecraft_submit_bigtime(
                        phone=phone,
                        gig=provider_gig,
                        network=provider_network,
                    )
                else:
                    ok, payload, reference_id = _codecraft_submit_regular(
                        phone=phone,
                        gig=provider_gig,
                        network=provider_network,
                    )

                orders_col.update_one(
                    {
                        "order_id": job_order_id,
                        "items.provider_request_order_id": line_ref,
                    },
                    {
                        "$set": {
                            "items.$.api_status": "success" if ok else "processing",
                            "items.$.line_status": "processing",
                            "items.$.api_response": payload,
                            "items.$.provider_reference": reference_id,
                            "items.$.provider_order_id": reference_id,
                            "items.$.provider_mode": provider_mode,
                            "items.$.provider_network": provider_network,
                            "items.$.provider_gig": provider_gig,
                            "items.$.provider_package_amount": provider_amount,
                            "items.$.provider": "codecraft",
                            "status": "processing",
                            "updated_at": datetime.utcnow(),
                        }
                    },
                )
                continue

            if provider == "portal02":
                if not PORTAL02_API_KEY:
                    ok = False
                    payload = {"success": False, "error": "PORTAL02 API key not configured", "http_status": 500}
                else:
                    network_slug = (job.get("portal02_network_slug") or "mtn").strip().lower()
                    offer_slug = job.get("portal02_offer_slug") or PORTAL02_OFFER_SLUG_MTN_NORMAL
                    package_size_gb = job.get("package_size_gb")
                    norm_phone = _normalize_portal02_phone(phone)

                    url = f"{PORTAL02_BASE_URL.rstrip('/')}/order/{network_slug}"
                    headers = {
                        "x-api-key": PORTAL02_API_KEY,
                        "Content-Type": "application/json",
                    }
                    body = {
                        "type": "single",
                        "volume": int(package_size_gb) if package_size_gb is not None else None,
                        "phone": norm_phone,
                        "offerSlug": offer_slug,
                        "webhookUrl": PORTAL02_WEBHOOK_URL,
                    }

                    try:
                        resp = requests.post(url, headers=headers, json=body, timeout=45)
                        text = resp.text or ""
                        try:
                            payload = resp.json()
                        except Exception:
                            payload = {"raw": text} if text else {}
                        if isinstance(payload, dict):
                            payload.setdefault("http_status", resp.status_code)
                        ok = bool(resp.ok)
                    except requests.RequestException as e:
                        ok = False
                        payload = {"success": False, "error": str(e), "type": "NETWORK_ERROR", "http_status": 599}

                provider_ref = None
                provider_order_id = None
                if isinstance(payload, dict):
                    provider_ref = payload.get("reference") or payload.get("transaction_code")
                    provider_order_id = (
                        payload.get("orderId")
                        or payload.get("order_id")
                        or payload.get("transaction_code")
                        or payload.get("reference")
                    )

                orders_col.update_one(
                    {
                        "order_id": job_order_id,
                        "items.provider_request_order_id": line_ref,
                    },
                    {
                        "$set": {
                            "items.$.api_status": "success" if ok else "failed",
                            "items.$.line_status": "processing" if ok else "failed",
                            "items.$.api_response": payload,
                            "items.$.provider_reference": provider_ref,
                            "items.$.provider_order_id": provider_order_id,
                            "items.$.provider": "portal02",
                            "status": "processing" if ok else "failed",
                            "updated_at": datetime.utcnow(),
                        }
                    },
                )
                continue
            else:
                jlog("provider_skipped", order_id=job_order_id, ref=line_ref, provider=provider)
                api_status = "not_applicable_unknown_provider"
                api_note = "Unknown provider; queued for manual processing."

            orders_col.update_one(
                {
                    "order_id": job_order_id,
                    "items.provider_request_order_id": line_ref,
                },
                {
                    "$set": {
                        "items.$.api_status": api_status,
                        "items.$.line_status": "processing",
                        "items.$.api_response": {"note": api_note},
                        "status": "processing",
                        "updated_at": datetime.utcnow(),
                    }
                },
            )
        except Exception as e:
            jlog("checkout_bg_worker_line_error", order_id=job_order_id, error=str(e))
            if line_ref:
                try:
                    provider = job.get("provider")
                    err_type = "CODECRAFT_EXCEPTION" if provider == "codecraft" else "PROVIDER_EXCEPTION"
                    orders_col.update_one(
                        {
                            "order_id": job_order_id,
                            "items.provider_request_order_id": line_ref,
                        },
                        {
                            "$set": {
                                "items.$.api_status": "failed",
                                "items.$.line_status": "failed",
                                "items.$.api_response": {"error": str(e), "type": err_type},
                                "status": "failed",
                                "updated_at": datetime.utcnow(),
                            }
                        },
                    )
                except Exception:
                    pass

    if local_debug:
        # append debug entries
        try:
            orders_col.update_one(
                {"order_id": order_id},
                {"$push": {"debug.events": {"$each": local_debug}}},
            )
        except Exception:
            pass

    jlog("checkout_bg_worker_end", order_id=order_id, jobs=len(api_jobs))


# ===== Core checkout logic (reused by Agent API) =============================
def _process_checkout_core(
    user_id: ObjectId,
    data: dict,
    api_reference_id: str | None = None,
    api_mode: str | None = None,
    api_source: str | None = None,
    client_request_id_override: str | None = None,
):
    try:
        cart = data.get("cart", [])
        method = data.get("method", "wallet")
        jlog("checkout_incoming", payload={"cart_count": len(cart) if isinstance(cart, list) else 0, "method": method, "source": api_source})

        if not cart or not isinstance(cart, list):
            return jsonify({"success": False, "message": "Cart is empty or invalid"}), 400

        # Total requested (customer-facing)
        total_requested = sum(_money(item.get("amount")) for item in cart)
        if total_requested <= 0:
            return jsonify({"success": False, "message": "Total amount must be greater than zero"}), 400

        client_request_id = (client_request_id_override or data.get("client_request_id") or "").strip()
        if client_request_id:
            existing = orders_col.find_one(
                {"user_id": user_id, "client_request_id": client_request_id},
                {"order_id": 1, "status": 1, "charged_amount": 1, "profit_amount_total": 1, "items": 1, "api_reference_id": 1},
            )
            if existing:
                existing_status = existing.get("status") or "pending"
                payload = {
                    "success": True,
                    "message": "Order already received.",
                    "order_id": existing.get("order_id"),
                    "redirect_url": f"/invoice/{existing.get('order_id')}",
                    "status": existing_status,
                    "charged_amount": _money(existing.get("charged_amount")),
                    "profit_amount_total": _money(existing.get("profit_amount_total")),
                    "items": existing.get("items") or [],
                }
                if existing.get("api_reference_id"):
                    payload["api_reference_id"] = existing.get("api_reference_id")
                return jsonify(payload), 200

        order_id = generate_order_id()

        # Balance check
        bal_doc = balances_col.find_one({"user_id": user_id}) or {}
        current_balance = _money(bal_doc.get("amount", 0))
        jlog("checkout_balance", order_id=order_id, balance=current_balance, total=total_requested)
        if current_balance < total_requested:
            return jsonify({"success": False, "message": "❌ Insufficient wallet balance"}), 400

        results = []
        debug_events = []

        total_delivered_api_amount = 0.0  # stays 0.0 (we don't mark delivered immediately)
        total_processing_amount = 0.0
        api_requested_total = 0.0
        has_processing = False
        profit_amount_total = 0.0

        seen_keys = set()
        api_jobs = []  # lines to be sent to providers in the background worker
        codecraft_regular_map = None
        codecraft_bigtime_map = None
        blocked_keys_in_cart = set()

        for cart_item in cart:
            phone_candidate = cart_item.get("phone")
            blocked_keys_in_cart.update(_phone_block_match_keys(phone_candidate))

        active_blocked_keys = set()
        if blocked_keys_in_cart:
            try:
                blocked_docs = blocked_phones_col.find(
                    {
                        "is_active": True,
                        "normalized_phone": {"$in": list(blocked_keys_in_cart)},
                    },
                    {"normalized_phone": 1, "_id": 0},
                )
                active_blocked_keys = {
                    d.get("normalized_phone")
                    for d in blocked_docs
                    if d.get("normalized_phone")
                }
            except Exception as e:
                jlog("blocked_phone_lookup_error", error=str(e))
                active_blocked_keys = set()

        for idx, item in enumerate(cart, start=1):
            phone = (item.get("phone") or "").strip()
            value_obj = _coerce_value_obj(item.get("value_obj") or item.get("value"))
            amt_total = _money(item.get("amount"))
            amount_key = _normalize_amount_key(amt_total)
            ported_fields = _extract_ported_fields(item)

            service_id_raw = item.get("serviceId")
            svc_doc = None
            svc_type = None
            svc_name = item.get("serviceName") or None
            svc_provider = ""

            if service_id_raw:
                try:
                    svc_doc = services_col.find_one(
                        {"_id": ObjectId(service_id_raw)},
                        {
                            "type": 1,
                            "network_id": 1,
                            "name": 1,
                            "network": 1,
                            "offers": 1,
                            "provider": 1,
                            "default_profit_percent": 1,
                            "service_category": 1,
                            "status": 1,
                            "availability": 1,
                            "service_network": 1,
                        },
                    )
                    if svc_doc:
                        st = svc_doc.get("type")
                        svc_type = (st.strip().upper() if isinstance(st, str) else st)
                        svc_name = svc_doc.get("name") or svc_doc.get("network") or svc_name
                except Exception:
                    svc_doc = None
                    svc_type = None

            if svc_doc and svc_doc.get("provider"):
                svc_provider = str(svc_doc.get("provider") or "").strip().lower()
            elif item.get("provider"):
                svc_provider = str(item.get("provider") or "").strip().lower()

            # HARD GATE: availability
            is_unavail, reason_text = _service_unavailability_reason(svc_doc)
            if is_unavail:
                return jsonify(
                    {
                        "success": False,
                        "message": reason_text,
                        "unavailable": {
                            "serviceId": service_id_raw,
                            "serviceName": svc_name,
                            "reason": reason_text,
                        },
                    }
                ), 400

            # Duplicate guards
            network_id = _resolve_network_id(item, value_obj, svc_doc)
            bundle_key = _build_bundle_key(value_obj, item)
            base_hint = _to_float(item.get("base_amount"))
            base_amount = round(float(base_hint if base_hint is not None else 0.0), 2)
            profit_amount = max(0.0, round(amt_total - base_amount, 2))
            profit_percent_used = round((profit_amount / base_amount) * 100.0, 2) if base_amount > 0 else 0.0

            phone_match_keys = _phone_block_match_keys(phone)
            if phone_match_keys and active_blocked_keys.intersection(phone_match_keys):
                has_processing = True
                total_processing_amount += amt_total
                profit_amount_total += profit_amount
                results.append(
                    {
                        "phone": phone,
                        "base_amount": base_amount,
                        "amount": amt_total,
                        "profit_amount": profit_amount,
                        "profit_percent_used": profit_percent_used,
                        **ported_fields,
                        "value": item.get("value"),
                        "value_obj": value_obj,
                        "serviceId": service_id_raw,
                        "serviceName": svc_name,
                        "service_type": svc_type if svc_type else ("unknown" if not svc_doc else None),
                        "network_id": network_id,
                        "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                        "line_amount_key": amount_key,
                        "line_status": "processing",
                        "api_status": "not_applicable_blocked_phone",
                        "api_response": {
                            "note": "Phone number is blocked from API checkout; order recorded for manual processing."
                        },
                    }
                )
                continue

            if phone and (network_id is not None) and (bundle_key is not None):
                cart_key = (phone, int(network_id), bundle_key[1], bundle_key[0], amount_key)
                if cart_key in seen_keys:
                    results.append(
                        {
                            "phone": phone,
                            "base_amount": 0.0,
                        "amount": 0.0,
                        "originally_requested_amount": amt_total,
                        "profit_amount": 0.0,
                        "profit_percent_used": 0.0,
                        **ported_fields,
                        "value": item.get("value"),
                            "value_obj": value_obj,
                            "serviceId": service_id_raw,
                            "serviceName": svc_name,
                            "service_type": svc_type if svc_type else ("unknown" if not svc_doc else None),
                            "network_id": network_id,
                            "bundle_key": {"kind": bundle_key[0], "value": bundle_key[1]},
                            "line_amount_key": amount_key,
                            "line_status": "skipped_duplicate_in_cart",
                            "api_status": "skipped",
                            "api_response": {
                                "note": "Duplicate line in this cart (same number, network, bundle, amount)"
                            },
                        }
                    )
                    continue
                seen_keys.add(cart_key)

            is_dup_strict = _has_processing_conflict_strict(
                phone, service_id_raw, svc_name, network_id, bundle_key, amount_key
            )
            if is_dup_strict:
                results.append(
                    {
                        "phone": phone,
                        "base_amount": 0.0,
                        "amount": 0.0,
                        "originally_requested_amount": amt_total,
                        "profit_amount": 0.0,
                        "profit_percent_used": 0.0,
                        **ported_fields,
                        "value": item.get("value"),
                        "value_obj": value_obj,
                        "serviceId": service_id_raw,
                        "serviceName": svc_name,
                        "service_type": svc_type if svc_type else ("unknown" if not svc_doc else None),
                        "network_id": network_id,
                        "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                        "line_amount_key": amount_key,
                        "line_status": "skipped_duplicate_processing",
                        "api_status": "skipped",
                        "api_response": {
                            "note": "Same number + same network + same bundle + same amount already processing; skipping."
                        },
                    }
                )
                continue

            profit_amount_total += profit_amount

            svc_name_norm = (svc_name or "").strip().lower()
            is_mtn_normal = (svc_name_norm == "mtn normal") or _is_mtn_normal_service(service_id_raw, svc_doc)
            is_mtn_express = (svc_name_norm == "mtn express")

            if str(item.get("provider") or "").strip().lower() == "portal02" and not (
                is_mtn_normal or is_mtn_express
            ):
                jlog(
                    "portal02" + "_blocked",
                    order_id=order_id,
                    idx=idx,
                    serviceId=service_id_raw,
                    serviceName=svc_name,
                )
                has_processing = True
                total_processing_amount += amt_total
                results.append(
                    {
                        "phone": phone,
                        "base_amount": base_amount,
                        "amount": amt_total,
                        "profit_amount": profit_amount,
                        "profit_percent_used": profit_percent_used,
                        **ported_fields,
                        "value": item.get("value"),
                        "value_obj": value_obj,
                        "serviceId": service_id_raw,
                        "serviceName": svc_name,
                        "service_type": svc_type if svc_type else "unknown",
                        "network_id": network_id,
                        "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                        "line_amount_key": amount_key,
                        "line_status": "processing",
                        "api_status": "not_applicable_portal_blocked",
                        "api_response": {"note": "Portal provider disabled; queued for manual processing."},
                    }
                )
                continue

            # No service doc → manual processing
            if not svc_doc:
                has_processing = True
                total_processing_amount += amt_total
                results.append(
                    {
                        "phone": phone,
                        "base_amount": base_amount,
                        "amount": amt_total,
                        "profit_amount": profit_amount,
                        "profit_percent_used": profit_percent_used,
                        **ported_fields,
                        "value": item.get("value"),
                        "value_obj": value_obj,
                        "serviceId": service_id_raw,
                        "serviceName": svc_name,
                        "service_type": svc_type if svc_type else "unknown",
                        "network_id": network_id,
                        "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                        "line_amount_key": amount_key,
                        "line_status": "processing",
                        "api_status": "not_applicable",
                        "api_response": {"note": "Service not found; queued for processing"},
                    }
                )
                continue

            # Provider selection
            resolved_network = _resolve_dataconnect_network(svc_doc, item)

            svc_type_flag = (svc_type or "").strip().upper() if isinstance(svc_type, str) else ""
            type_allows_api = svc_type_flag in ("ON", "API")
            api_allowed = type_allows_api
            if svc_type_flag == "OFF":
                api_allowed = False

            # MTN NORMAL / MTN EXPRESS provider selection
            chosen_mtn_normal_provider = None
            chosen_mtn_express_provider = None
            use_portal02 = False

            if is_mtn_normal:
                chosen_mtn_normal_provider = (svc_provider or "").strip().lower()
                if chosen_mtn_normal_provider not in SERVICE_PROVIDER_SET:
                    chosen_mtn_normal_provider = "portal02"
                if api_allowed and chosen_mtn_normal_provider == "portal02":
                    use_portal02 = True

            if is_mtn_express:
                chosen_mtn_express_provider = (svc_provider or "").strip().lower()
                if chosen_mtn_express_provider not in SERVICE_PROVIDER_SET:
                    chosen_mtn_express_provider = "dataconnect"
                if api_allowed and chosen_mtn_express_provider == "portal02":
                    use_portal02 = True

            use_codecraft = bool(
                api_allowed
                and (
                    (is_mtn_normal and chosen_mtn_normal_provider == "codecraft")
                    or (is_mtn_express and chosen_mtn_express_provider == "codecraft")
                    or ((not is_mtn_normal and not is_mtn_express) and svc_provider == "codecraft")
                )
            )
            codecraft_network = _resolve_codecraft_network_name(svc_doc, item) if use_codecraft else None

            # DataConnect: MTN Express rule unchanged + MTN NORMAL override
            use_dataconnect_express = (
                resolved_network == "mtn"
                and is_mtn_express
                and chosen_mtn_express_provider == "dataconnect"
                and api_allowed
            )
            use_dataconnect_mtn_normal = (
                is_mtn_normal and chosen_mtn_normal_provider == "dataconnect" and api_allowed
            )
            use_dataconnect = (use_dataconnect_express or use_dataconnect_mtn_normal) and not use_codecraft

            use_datakazina_express = (
                resolved_network == "mtn"
                and is_mtn_express
                and chosen_mtn_express_provider == "datakazina"
                and api_allowed
            )
            use_datakazina_mtn_normal = (
                is_mtn_normal and chosen_mtn_normal_provider == "datakazina" and api_allowed
            )
            use_datakazina = (use_datakazina_express or use_datakazina_mtn_normal) and not use_codecraft and not use_portal02

            use_skplug_express = (
                resolved_network == "mtn"
                and is_mtn_express
                and chosen_mtn_express_provider == "skplug"
                and api_allowed
            )
            use_skplug_mtn_normal = (
                is_mtn_normal and chosen_mtn_normal_provider == "skplug" and api_allowed
            )
            use_skplug = (use_skplug_express or use_skplug_mtn_normal) and not use_codecraft and not use_portal02

            jlog(
                "checkout_line_routing",
                order_id=order_id,
                idx=idx,
                serviceId=service_id_raw,
                svc_name=svc_name,
                resolved_network=resolved_network,
                svc_type_flag=svc_type_flag,
                is_mtn_express=is_mtn_express,
                is_mtn_normal=is_mtn_normal,
                mtn_normal_provider=chosen_mtn_normal_provider,
                mtn_express_provider=chosen_mtn_express_provider,
                api_allowed=api_allowed,
                use_portal02=use_portal02,
                use_dataconnect=use_dataconnect,
                use_datakazina=use_datakazina,
                use_skplug=use_skplug,
                svc_provider=svc_provider,
                use_codecraft=use_codecraft,
                codecraft_network=codecraft_network,
            )

            if use_datakazina:
                jlog(
                    "datakazina_routing_selected",
                    order_id=order_id,
                    idx=idx,
                    serviceId=service_id_raw,
                    serviceName=svc_name,
                    provider=svc_provider,
                )

            # HARD GATE: never call any provider if service type is OFF
            if not api_allowed:
                jlog(
                    "api_gate_blocked_type_off",
                    order_id=order_id,
                    idx=idx,
                    serviceId=service_id_raw,
                    serviceName=svc_name,
                    provider=svc_provider,
                    svc_type_flag=svc_type_flag,
                )
                has_processing = True
                total_processing_amount += amt_total
                results.append(
                    {
                        "phone": phone,
                        "base_amount": base_amount,
                        "amount": amt_total,
                        "profit_amount": profit_amount,
                        "profit_percent_used": profit_percent_used,
                        **ported_fields,
                        "value": item.get("value"),
                        "value_obj": value_obj,
                        "serviceId": service_id_raw,
                        "serviceName": svc_name,
                        "service_type": svc_type,
                        "network_id": network_id,
                        "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                        "line_amount_key": amount_key,
                        "line_status": "processing",
                        "api_status": "not_applicable_type_off",
                        "api_response": {
                            "note": "API calls disabled for this service (type OFF); queued for manual processing."
                        },
                    }
                )
                continue

            if not use_dataconnect and not use_datakazina and not use_skplug and not use_codecraft and not use_portal02:
                has_processing = True
                total_processing_amount += amt_total

                if not api_allowed:
                    note = (
                        "API calls disabled for this service (type OFF); queued for manual processing."
                    )
                    api_status = "not_applicable_type_off"
                else:
                    note = (
                        "API is only used for MTN NORMAL/MTN EXPRESS (Portal02/DataConnect/DataKazina/SkPlug/CodeCraft) or CodeCraft-routed services; queued for manual processing."
                    )
                    api_status = "not_applicable_network"

                results.append(
                    {
                        "phone": phone,
                        "base_amount": base_amount,
                        "amount": amt_total,
                        "profit_amount": profit_amount,
                        "profit_percent_used": profit_percent_used,
                        **ported_fields,
                        "value": item.get("value"),
                        "value_obj": value_obj,
                        "serviceId": service_id_raw,
                        "serviceName": svc_name,
                        "service_type": svc_type,
                        "network_id": network_id,
                        "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                        "line_amount_key": amount_key,
                        "line_status": "processing",
                        "api_status": api_status,
                        "api_response": {
                            "note": note,
                            "resolved_network": resolved_network,
                            "serviceName": svc_name,
                            "service_type_flag": svc_type_flag,
                        },
                    }
                )
                continue

            if use_portal02:
                api_requested_total += amt_total

                package_size_gb = _resolve_package_size_gb(value_obj, item)

                if not phone or package_size_gb is None:
                    has_processing = True
                    total_processing_amount += amt_total
                    results.append(
                        {
                            "phone": phone,
                            "base_amount": base_amount,
                            "amount": amt_total,
                            "profit_amount": profit_amount,
                            "profit_percent_used": profit_percent_used,
                            **ported_fields,
                            "value": item.get("value"),
                            "value_obj": value_obj,
                            "serviceId": service_id_raw,
                            "serviceName": svc_name,
                            "service_type": svc_type,
                            "network_id": network_id,
                            "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                            "line_amount_key": amount_key,
                            "line_status": "processing",
                            "api_status": "skipped_missing_fields",
                            "api_response": {
                                "note": "API fields missing; queued for processing",
                                "got": {
                                    "phone": bool(phone),
                                    "package_size_gb": package_size_gb,
                                },
                            },
                        }
                    )
                    continue

                external_ref = f"{order_id}_{idx}_{uuid.uuid4().hex[:6]}"

                has_processing = True
                total_processing_amount += amt_total

                line_record = {
                    "phone": phone,
                    "base_amount": base_amount,
                    "amount": amt_total,
                    "profit_amount": profit_amount,
                    "profit_percent_used": profit_percent_used,
                    **ported_fields,
                    "value": item.get("value"),
                    "value_obj": value_obj,
                    "serviceId": service_id_raw,
                    "serviceName": svc_name,
                    "service_type": svc_type,
                    "provider": "portal02",
                    "provider_reference": None,
                    "provider_order_id": None,
                    "provider_request_order_id": external_ref,
                    "network_id": network_id,
                    "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                    "line_amount_key": amount_key,
                    "line_status": "pending",
                    "api_status": "queued",
                    "api_response": {"note": "Queued for background API call"},
                }

                results.append(line_record)

                job_payload = {
                    "provider_request_order_id": external_ref,
                    "phone": phone,
                    "provider": "portal02",
                    "portal02_network_slug": "mtn",
                    "package_size_gb": package_size_gb,
                    "portal02_offer_slug": PORTAL02_OFFER_SLUG_MTN_NORMAL,
                    "service_id": svc_doc["_id"],
                    "raw_item": item,
                    "line_index": idx,
                }

                api_jobs.append(job_payload)
                continue

            if use_codecraft:
                api_requested_total += amt_total

                volume_mb = None
                if isinstance(value_obj, dict):
                    vol_raw = value_obj.get("volume")
                    if vol_raw not in (None, "", []):
                        try:
                            volume_mb = int(float(vol_raw))
                        except Exception:
                            volume_mb = None
                if volume_mb is None:
                    gb_fallback = _resolve_package_size_gb(value_obj, item)
                    if gb_fallback is not None:
                        volume_mb = int(gb_fallback * 1000)

                provider_gig = None
                if volume_mb is not None:
                    try:
                        provider_gig = max(1, int(float(volume_mb) / 1000))
                    except Exception:
                        provider_gig = None

                if not phone or not provider_gig or not codecraft_network:
                    has_processing = True
                    total_processing_amount += amt_total
                    results.append(
                        {
                            "phone": phone,
                            "base_amount": base_amount,
                            "amount": amt_total,
                            "profit_amount": profit_amount,
                            "profit_percent_used": profit_percent_used,
                            **ported_fields,
                            "value": item.get("value"),
                            "value_obj": value_obj,
                            "serviceId": service_id_raw,
                            "serviceName": svc_name,
                            "service_type": svc_type,
                            "network_id": network_id,
                            "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                            "line_amount_key": amount_key,
                            "line_status": "processing",
                            "api_status": "skipped_missing_fields",
                            "api_response": {
                                "note": "API fields missing; queued for processing",
                                "got": {
                                    "phone": bool(phone),
                                    "provider_network": codecraft_network,
                                    "provider_gig": provider_gig,
                                },
                            },
                        }
                    )
                    continue

                if codecraft_regular_map is None or codecraft_bigtime_map is None:
                    codecraft_regular_map, codecraft_bigtime_map = _codecraft_get_packages_cached()

                key = (codecraft_network, provider_gig)
                provider_mode = None
                provider_amount = None
                if codecraft_network == "TELECEL":
                    if codecraft_regular_map and key in codecraft_regular_map:
                        provider_mode = "regular"
                        provider_amount = codecraft_regular_map.get(key)
                else:
                    if codecraft_bigtime_map and key in codecraft_bigtime_map:
                        provider_mode = "bigtime"
                        provider_amount = codecraft_bigtime_map.get(key)
                    elif codecraft_regular_map and key in codecraft_regular_map:
                        provider_mode = "regular"
                        provider_amount = codecraft_regular_map.get(key)

                jlog(
                    "codecraft_mode_selected",
                    order_id=order_id,
                    idx=idx,
                    codecraft_network=codecraft_network,
                    provider_mode=provider_mode,
                )

                if not provider_mode:
                    has_processing = True
                    total_processing_amount += amt_total
                    results.append(
                        {
                            "phone": phone,
                            "base_amount": base_amount,
                            "amount": amt_total,
                            "profit_amount": profit_amount,
                            "profit_percent_used": profit_percent_used,
                            **ported_fields,
                            "value": item.get("value"),
                            "value_obj": value_obj,
                            "serviceId": service_id_raw,
                            "serviceName": svc_name,
                            "service_type": svc_type,
                            "network_id": network_id,
                            "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                            "line_amount_key": amount_key,
                            "line_status": "processing",
                            "api_status": "skipped_package_not_found",
                            "api_response": {
                                "note": "Package not found in CodeCraft; queued for processing",
                                "provider_network": codecraft_network,
                                "provider_gig": provider_gig,
                            },
                        }
                    )
                    continue

                external_ref = f"{order_id}_{idx}_{uuid.uuid4().hex[:6]}"

                has_processing = True
                total_processing_amount += amt_total

                line_record = {
                    "phone": phone,
                    "base_amount": base_amount,
                    "amount": amt_total,
                    "profit_amount": profit_amount,
                    "profit_percent_used": profit_percent_used,
                    **ported_fields,
                    "value": item.get("value"),
                    "value_obj": value_obj,
                    "serviceId": service_id_raw,
                    "serviceName": svc_name,
                    "service_type": svc_type,
                    "provider": "codecraft",
                    "provider_reference": None,
                    "provider_order_id": None,
                    "provider_request_order_id": external_ref,
                    "provider_mode": provider_mode,
                    "provider_network": codecraft_network,
                    "provider_gig": provider_gig,
                    "provider_package_amount": provider_amount,
                    "network_id": network_id,
                    "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                    "line_amount_key": amount_key,
                    "line_status": "pending",
                    "api_status": "queued",
                    "api_response": {"note": "Queued for background API call"},
                }

                results.append(line_record)

                job_payload = {
                    "provider_request_order_id": external_ref,
                    "phone": phone,
                    "provider": "codecraft",
                    "provider_network": codecraft_network,
                    "provider_gig": provider_gig,
                    "provider_mode": provider_mode,
                    "provider_amount": provider_amount,
                    "service_id": svc_doc["_id"],
                    "line_index": idx,
                }

                api_jobs.append(job_payload)
                continue

            if use_skplug:
                api_requested_total += amt_total

                package_size_gb = _resolve_skplug_gb_size(value_obj, item)
                skplug_network = "MTN"

                if not phone or package_size_gb is None:
                    has_processing = True
                    total_processing_amount += amt_total
                    results.append(
                        {
                            "phone": phone,
                            "base_amount": base_amount,
                            "amount": amt_total,
                            "profit_amount": profit_amount,
                            "profit_percent_used": profit_percent_used,
                            **ported_fields,
                            "value": item.get("value"),
                            "value_obj": value_obj,
                            "serviceId": service_id_raw,
                            "serviceName": svc_name,
                            "service_type": svc_type,
                            "network_id": network_id,
                            "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                            "line_amount_key": amount_key,
                            "line_status": "processing",
                            "api_status": "skipped_missing_fields",
                            "api_response": {
                                "note": "API fields missing; queued for processing",
                                "got": {
                                    "phone": bool(phone),
                                    "network": skplug_network,
                                    "gb_size": package_size_gb,
                                },
                            },
                        }
                    )
                    continue

                external_ref = f"{order_id}_{idx}_{uuid.uuid4().hex[:6]}"

                has_processing = True
                total_processing_amount += amt_total

                line_record = {
                    "phone": phone,
                    "base_amount": base_amount,
                    "amount": amt_total,
                    "profit_amount": profit_amount,
                    "profit_percent_used": profit_percent_used,
                    **ported_fields,
                    "value": item.get("value"),
                    "value_obj": value_obj,
                    "serviceId": service_id_raw,
                    "serviceName": svc_name,
                    "service_type": svc_type,
                    "provider": "skplug",
                    "provider_reference": None,
                    "provider_order_id": None,
                    "provider_request_order_id": external_ref,
                    "provider_network": skplug_network,
                    "provider_gb_size": package_size_gb,
                    "network_id": network_id,
                    "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                    "line_amount_key": amount_key,
                    "line_status": "pending",
                    "api_status": "queued",
                    "api_response": {"note": "Queued for background API call"},
                }

                results.append(line_record)

                job_payload = {
                    "provider_request_order_id": external_ref,
                    "phone": phone,
                    "provider": "skplug",
                    "skplug_network": skplug_network,
                    "skplug_gb_size": package_size_gb,
                    "service_id": svc_doc["_id"],
                    "line_index": idx,
                }

                api_jobs.append(job_payload)
                continue

            if use_datakazina:
                api_requested_total += amt_total

                shared_bundle = _resolve_datakazina_shared_bundle(value_obj, item)

                if shared_bundle is None:
                    jlog(
                        "datakazina_shared_bundle_resolution_failed",
                        order_id=order_id,
                        idx=idx,
                        serviceName=svc_name,
                        value=item.get("value"),
                        value_obj=value_obj,
                    )
                    has_processing = True
                    total_processing_amount += amt_total
                    results.append(
                        {
                            "phone": phone,
                            "base_amount": base_amount,
                            "amount": amt_total,
                            "profit_amount": profit_amount,
                            "profit_percent_used": profit_percent_used,
                            **ported_fields,
                            "value": item.get("value"),
                            "value_obj": value_obj,
                            "serviceId": service_id_raw,
                            "serviceName": svc_name,
                            "service_type": svc_type,
                            "network_id": network_id,
                            "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                            "line_amount_key": amount_key,
                            "line_status": "processing",
                            "api_status": "datakazina_bundle_resolution_failed",
                            "api_response": {
                                "note": "DataKazina shared_bundle resolution failed; queued for manual processing",
                                "serviceName": svc_name,
                                "value": item.get("value"),
                                "value_obj": value_obj,
                            },
                        }
                    )
                    continue
                jlog(
                    "datakazina_shared_bundle_resolved",
                    order_id=order_id,
                    idx=idx,
                    serviceName=svc_name,
                    value=item.get("value"),
                    value_obj=value_obj,
                    shared_bundle=shared_bundle,
                )

                if not phone:
                    has_processing = True
                    total_processing_amount += amt_total
                    results.append(
                        {
                            "phone": phone,
                            "base_amount": base_amount,
                            "amount": amt_total,
                            "profit_amount": profit_amount,
                            "profit_percent_used": profit_percent_used,
                            **ported_fields,
                            "value": item.get("value"),
                            "value_obj": value_obj,
                            "serviceId": service_id_raw,
                            "serviceName": svc_name,
                            "service_type": svc_type,
                            "network_id": network_id,
                            "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                            "line_amount_key": amount_key,
                            "line_status": "processing",
                            "api_status": "skipped_missing_fields",
                            "api_response": {
                                "note": "API fields missing; queued for processing",
                                "got": {
                                    "phone": bool(phone),
                                    "shared_bundle": shared_bundle,
                                },
                            },
                        }
                    )
                    continue

                external_ref = f"{order_id}_{idx}_{uuid.uuid4().hex[:6]}"

                has_processing = True
                total_processing_amount += amt_total

                line_record = {
                    "phone": phone,
                    "base_amount": base_amount,
                    "amount": amt_total,
                    "profit_amount": profit_amount,
                    "profit_percent_used": profit_percent_used,
                    **ported_fields,
                    "value": item.get("value"),
                    "value_obj": value_obj,
                    "serviceId": service_id_raw,
                    "serviceName": svc_name,
                    "service_type": svc_type,
                    "provider": "datakazina",
                    "provider_reference": None,
                    "provider_order_id": None,
                    "provider_request_order_id": external_ref,
                    "network_id": network_id,
                    "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                    "line_amount_key": amount_key,
                    "line_status": "pending",
                    "api_status": "queued",
                    "api_response": {"note": "Queued for background API call"},
                    "shared_bundle": shared_bundle,
                }

                results.append(line_record)

                job_payload = {
                    "provider_request_order_id": external_ref,
                    "incoming_api_ref": external_ref,
                    "phone": phone,
                    "provider": "datakazina",
                    "shared_bundle": shared_bundle,
                    "network_id": 3,
                    "service_id": svc_doc["_id"],
                    "line_index": idx,
                }

                api_jobs.append(job_payload)
                masked_phone = (
                    phone[:3] + "***" + phone[-2:] if phone and len(phone) >= 5 else "***"
                )
                job_payload_log = dict(job_payload)
                job_payload_log["phone"] = masked_phone
                jlog(
                    "datakazina_job_payload_queued",
                    order_id=order_id,
                    idx=idx,
                    serviceName=svc_name,
                    value=item.get("value"),
                    value_obj=value_obj,
                    shared_bundle=shared_bundle,
                    job_payload=job_payload_log,
                )
                continue

            if not use_dataconnect:
                continue

            # From here: API-eligible line → we will send it via BACKGROUND worker
            api_requested_total += amt_total

            package_size_gb = _resolve_package_size_gb(value_obj, item)

            # Resolve shared_bundle for DataConnect from your stored offer structure
            shared_bundle = None
            if isinstance(value_obj, dict):
                sb = value_obj.get("volume") or value_obj.get("shared_bundle") or value_obj.get("mb")
                if sb not in (None, "", []):
                    try:
                        shared_bundle = int(float(sb))
                    except Exception:
                        shared_bundle = None
            if shared_bundle is None and package_size_gb is not None:
                shared_bundle = int(package_size_gb * 1000)

            if not phone or package_size_gb is None:
                has_processing = True
                total_processing_amount += amt_total
                results.append(
                    {
                        "phone": phone,
                        "base_amount": base_amount,
                        "amount": amt_total,
                        "profit_amount": profit_amount,
                        "profit_percent_used": profit_percent_used,
                        **ported_fields,
                        "value": item.get("value"),
                        "value_obj": value_obj,
                        "serviceId": service_id_raw,
                        "serviceName": svc_name,
                        "service_type": svc_type,
                        "network_id": network_id,
                        "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                        "line_amount_key": amount_key,
                        "line_status": "processing",
                        "api_status": "skipped_missing_fields",
                        "api_response": {
                            "note": "API fields missing; queued for processing",
                            "got": {
                                "phone": bool(phone),
                                "resolved_network": resolved_network,
                                "package_size_gb": package_size_gb,
                            },
                        },
                    }
                )
                continue

            # Prepare background job meta
            external_ref = f"{order_id}_{idx}_{uuid.uuid4().hex[:6]}"

            provider_name = "dataconnect"

            has_processing = True
            total_processing_amount += amt_total

            # store line with "queued" status; background worker will update
            line_record = {
                "phone": phone,
                "base_amount": base_amount,
                "amount": amt_total,
                "profit_amount": profit_amount,
                "profit_percent_used": profit_percent_used,
                **ported_fields,
                "value": item.get("value"),
                "value_obj": value_obj,
                "serviceId": service_id_raw,
                "serviceName": svc_name,
                "service_type": svc_type,
                "provider": provider_name,
                "provider_reference": None,
                "provider_order_id": None,
                "provider_request_order_id": external_ref,
                "network_id": network_id,
                "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                "line_amount_key": amount_key,
                "line_status": "pending",
                "api_status": "queued",      # <--- queued for background call
                "api_response": {"note": "Queued for background API call"},
            }

            # For transparency/debug you can store shared_bundle on the line as well
            if use_dataconnect:
                line_record["shared_bundle"] = shared_bundle

            results.append(line_record)

            job_payload = {
                "provider_request_order_id": external_ref,
                "phone": phone,
                "provider": provider_name,
                "service_id": svc_doc["_id"],
                "line_index": idx,
            }

            if provider_name == "dataconnect":
                job_payload["network_id"] = network_id
                job_payload["shared_bundle"] = shared_bundle

            api_jobs.append(job_payload)

        if len(debug_events) > 10:
            debug_events = debug_events[-10:]

        total_to_charge_now = round(total_delivered_api_amount + total_processing_amount, 2)

        # If nothing to charge (all skipped)
        if total_to_charge_now <= 0:
            created_now = datetime.utcnow()

            order_doc = {
                "user_id": user_id,
                "order_id": order_id,
                "items": results,
                "total_amount": 0.0,
                "charged_amount": 0.0,
                "profit_amount_total": 0.0,
                "status": "skipped",
                "paid_from": method,
                "created_at": created_now,
                "updated_at": created_now,
                "debug": {"events": debug_events},
            }
            if client_request_id:
                order_doc["client_request_id"] = client_request_id
            if api_reference_id:
                order_doc["api_reference_id"] = api_reference_id
            if api_mode:
                order_doc["api_mode"] = api_mode
            if api_source:
                order_doc["api_source"] = api_source

            orders_col.insert_one(order_doc)
            skipped_count = sum(
                1
                for it in results
                if it.get("line_status") in ("skipped_duplicate_processing", "skipped_duplicate_in_cart")
            )
            return (
                jsonify(
                    {
                        "success": True,
                        "message": (
                            "No charge taken. {n} item(s) were skipped because the same phone, network, bundle, "
                            "and amount already has an order in processing or duplicated in cart."
                        ).format(n=skipped_count),
                        "order_id": order_id,
                        "redirect_url": f"/invoice/{order_id}",
                        "status": "skipped",
                        "charged_amount": 0.0,
                        "profit_amount_total": 0.0,
                        "skipped_count": skipped_count,
                        "items": results,
                    }
                ),
                200,
            )

        # Deduct balance NOW
        balances_col.update_one(
            {"user_id": user_id},
            {"$inc": {"amount": -total_to_charge_now}, "$set": {"updated_at": datetime.utcnow()}},
            upsert=True,
        )

        status = "pending"
        created_now = datetime.utcnow()

        order_doc = {
            "user_id": user_id,
            "order_id": order_id,
            "items": results,
            "total_amount": total_requested,
            "charged_amount": total_to_charge_now,
            "profit_amount_total": round(profit_amount_total, 2),
            "status": status,
            "paid_from": method,
            "created_at": created_now,
            "updated_at": created_now,
            "debug": {"events": debug_events},
        }
        if client_request_id:
            order_doc["client_request_id"] = client_request_id
        if api_reference_id:
            order_doc["api_reference_id"] = api_reference_id
        if api_mode:
            order_doc["api_mode"] = api_mode
        if api_source:
            order_doc["api_source"] = api_source

        orders_col.insert_one(order_doc)
        _send_mashup_order_sms_async(order_id, created_now, results)

        # Record transaction
        providers_used = sorted(
            {it.get("provider") for it in results if it.get("provider")}
        )
        provider_request_ids = [
            it.get("provider_request_order_id")
            for it in results
            if it.get("provider_request_order_id")
        ]
        transactions_col.insert_one(
            {
                "user_id": user_id,
                "amount": total_to_charge_now,
                "reference": order_id,
                "status": "success",
                "type": "purchase",
                "gateway": "Wallet",
                "currency": "GHS",
                "created_at": datetime.utcnow(),
                "verified_at": datetime.utcnow(),
                "meta": {
                    "order_status": status,
                    "api_delivered_amount": round(total_delivered_api_amount, 2),
                    "processing_amount": round(total_processing_amount, 2),
                    "profit_amount_total": round(profit_amount_total, 2),
                    "providers_used": providers_used,
                    "provider_request_ids": provider_request_ids,
                },
            }
        )

        skipped_count = sum(
            1
            for it in results
            if it.get("line_status") in ("skipped_duplicate_processing", "skipped_duplicate_in_cart")
        )
        processing_count = sum(1 for it in results if it.get("line_status") == "processing")

        for job in api_jobs:
            job["order_id"] = order_id

        # 🔥 Spawn background worker for provider calls (does not block response)
        if api_jobs:
            try:
                t = threading.Thread(
                    target=_background_process_providers,
                    args=(order_id, api_jobs),
                    daemon=True,
                )
                t.start()
            except Exception as e:
                jlog("checkout_bg_spawn_error", order_id=order_id, error=str(e))

        msg = (
            "📝 Order received and is processing. "
            "We’ve charged your wallet. Order ID: {oid}"
        ).format(oid=order_id)

        return (
            jsonify(
                {
                    "success": True,
                    "message": msg,
                    "order_id": order_id,
                    "redirect_url": f"/invoice/{order_id}",  # frontend already uses this
                    "status": status,
                    "charged_amount": total_to_charge_now,
                    "profit_amount_total": round(profit_amount_total, 2),
                    "processing_count": processing_count,
                    "skipped_count": skipped_count,
                    "items": results,
                }
            ),
            200,
        )

    except Exception:
        jlog("checkout_uncaught", error=traceback.format_exc())
        return jsonify({"success": False, "message": "Server error"}), 500


# ===== Route (FAST RESPONSE, PROVIDERS IN BACKGROUND) ========================
@checkout_bp.route("/checkout", methods=["POST"])
def process_checkout():
    try:
        # Auth
        if "user_id" not in session or session.get("role") != "customer":
            jlog("checkout_auth_fail", session_keys=list(session.keys()))
            return jsonify({"success": False, "message": "Not authorized"}), 401

        try:
            user_id = ObjectId(session["user_id"])
        except Exception:
            return jsonify({"success": False, "message": "Invalid user ID"}), 400

        data = request.get_json(silent=True) or {}
        return _process_checkout_core(user_id, data)

    except Exception:
        jlog("checkout_uncaught", error=traceback.format_exc())
        return jsonify({"success": False, "message": "Server error"}), 500


# ===== Invoice view (same blueprint) =========================================
@checkout_bp.route("/invoice/<order_id>")
def invoice_view(order_id):
    """
    Render a single invoice by Nagonu Order ID (e.g. NAN12345)
    Uses invoice.html template you already created.
    """
    order = orders_col.find_one({"order_id": order_id})
    if not order:
        abort(404)

    user = {}
    try:
        uid = order.get("user_id")
        if uid:
            user = users_col.find_one({"_id": uid}) or {}
    except Exception:
        user = {}

    customer_name = (
        user.get("name")
        or user.get("full_name")
        or user.get("username")
        or "Customer"
    )

    return render_template(
        "invoice.html",
        order=order,
        user=user,
        customer=customer_name,
    )
