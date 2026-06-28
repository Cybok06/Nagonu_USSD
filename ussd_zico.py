from __future__ import annotations

from typing import Any, Dict, List

from zico_store import (
    get_service_by_id,
    is_valid_gh_phone,
    latest_order_for_phone,
    load_store_services,
    normalize_phone,
    validate_agent_code,
)
from ussd_zico_state import (
    create_pending_order,
    end_session,
    get_recent_agent_code,
    get_session,
    get_unfinished_session,
    remember_agent_code,
    save_session,
)


APP_NAME = "zico"
OFFERS_PER_PAGE = 7


def con(text: str) -> str:
    return "CON " + text


def end(text: str) -> str:
    return "END " + text


def _parts(text: str) -> List[str]:
    return [p.strip() for p in (text or "").split("*") if p.strip()]


def _start(session_id: str, phone: str) -> str:
    unfinished = get_unfinished_session(phone)
    if unfinished:
        data = unfinished.get("data") or {}
        save_session(session_id, phone, "resume_unfinished", {"resume_session_id": unfinished.get("session_id"), **data})
        return con("You have an unfinished payment session.\n1. Continue\n2. Start New")

    recent = get_recent_agent_code(phone, APP_NAME)
    if recent and recent.get("agent_code"):
        save_session(session_id, phone, "reuse_agent_code", {"recent_agent_code": recent.get("agent_code")})
        return con(f"Use agent code {recent.get('agent_code')} again?\n1. Yes\n2. No")

    save_session(session_id, phone, "enter_agent_code", {})
    return con("Welcome to AZICO USSD\nEnter agent code:")


def _load_agent(session_id: str, phone: str, code: str) -> str:
    loaded = validate_agent_code(code)
    if not loaded:
        end_session(session_id, phone)
        return end("Invalid or inactive agent code.")

    store = loaded["store"]
    agent = loaded["agent"]
    data = {
        "agent_code": loaded["code"],
        "agent_user_id": str(agent.get("_id")),
        "admin_id": str(loaded.get("admin_id") or store.get("admin_id") or ""),
        "store_slug": store.get("slug") or "",
        "store_owner_id": str(store.get("owner_id") or agent.get("_id")),
    }
    remember_agent_code(phone, APP_NAME, loaded["code"], agent.get("_id"), store.get("slug") or "")
    save_session(session_id, phone, "select_service", data)
    return _service_menu(session_id, phone, data)


def _store_doc(data: Dict[str, Any]) -> Dict[str, Any]:
    loaded = validate_agent_code(data.get("agent_code") or "")
    return (loaded or {}).get("store") or {}


def _service_menu(session_id: str, phone: str, data: Dict[str, Any]) -> str:
    store = _store_doc(data)
    services = load_store_services(store) if store else []
    if not services:
        end_session(session_id, phone)
        return end("No services are available for this agent store.")

    data["services"] = [{"id": s["id"], "name": s["name"]} for s in services]
    save_session(session_id, phone, "select_service", data)
    lines = ["Select service:"]
    for idx, service in enumerate(services, start=1):
        lines.append(f"{idx}. {service['name']}")
    lines.append("0. Check latest order")
    return con("\n".join(lines))


def _offer_menu(session_id: str, phone: str, data: Dict[str, Any], page: int = 0) -> str:
    store = _store_doc(data)
    service = get_service_by_id(store, data.get("service_id") or "") if store else None
    if not service:
        save_session(session_id, phone, "select_service", data)
        return con("Service not found.\nSelect service again:")

    offers = service.get("offers") or []
    start = page * OFFERS_PER_PAGE
    visible = offers[start:start + OFFERS_PER_PAGE]
    data["offer_page"] = page
    save_session(session_id, phone, "select_offer", data)

    lines = [f"{service['name']} packages:"]
    for idx, offer in enumerate(visible, start=1):
        lines.append(f"{idx}. {offer['value_text']} - GHS {offer['amount']:.2f}")
    next_no = len(visible) + 1
    if start + OFFERS_PER_PAGE < len(offers):
        lines.append(f"{next_no}. More")
        next_no += 1
    if page > 0:
        lines.append(f"{next_no}. Back")
    else:
        lines.append("0. Back")
    return con("\n".join(lines))


def _confirmation(data: Dict[str, Any]) -> str:
    return (
        "Confirm order:\n"
        f"{data.get('service_name')}\n"
        f"{data.get('offer_text')} - GHS {float(data.get('amount') or 0):.2f}\n"
        f"Recipient: {data.get('recipient')}\n"
        "1. Place Order\n"
        "2. Cancel"
    )


def handle(session_id: str, phone: str, text: str) -> str:
    phone = normalize_phone(phone)
    parts = _parts(text)

    if not parts:
        return _start(session_id, phone)

    session = get_session(session_id, phone)
    data: Dict[str, Any] = (session or {}).get("data") or {}
    state = (session or {}).get("state") or "enter_agent_code"
    entry = parts[-1]

    if state == "resume_unfinished":
        if entry == "1":
            save_session(session_id, phone, "payment_pending", data)
            return con("Payment is still pending.\n1. Check payment\n2. Cancel")
        end_session(session_id, phone)
        save_session(session_id, phone, "enter_agent_code", {})
        return con("Enter agent code:")

    if state == "reuse_agent_code":
        if entry == "1":
            return _load_agent(session_id, phone, data.get("recent_agent_code") or "")
        save_session(session_id, phone, "enter_agent_code", {})
        return con("Enter agent code:")

    if state == "enter_agent_code":
        return _load_agent(session_id, phone, entry)

    if state == "select_service":
        if entry == "0":
            latest = latest_order_for_phone(phone)
            save_session(session_id, phone, "latest_order", data)
            if not latest:
                return con("No recent order found.\n0. Back")
            item = (latest.get("items") or [{}])[0]
            return con(
                f"Latest order {latest.get('order_id')}\n"
                f"{item.get('serviceName') or ''} {item.get('value') or ''}\n"
                f"Status: {latest.get('status') or 'Pending'}\n"
                "0. Back"
            )
        try:
            selected = int(entry)
        except Exception:
            return _service_menu(session_id, phone, data)
        store = _store_doc(data)
        services = load_store_services(store) if store else []
        if selected < 1 or selected > len(services):
            return _service_menu(session_id, phone, data)
        service = services[selected - 1]
        data.update({"service_id": service["id"], "service_name": service["name"]})
        return _offer_menu(session_id, phone, data, 0)

    if state == "select_offer":
        store = _store_doc(data)
        service = get_service_by_id(store, data.get("service_id") or "") if store else None
        if not service:
            return _service_menu(session_id, phone, data)
        page = int(data.get("offer_page") or 0)
        offers = service.get("offers") or []
        start = page * OFFERS_PER_PAGE
        visible = offers[start:start + OFFERS_PER_PAGE]
        try:
            selected = int(entry)
        except Exception:
            return _offer_menu(session_id, phone, data, page)
        more_option = len(visible) + 1 if start + OFFERS_PER_PAGE < len(offers) else None
        back_option = (more_option + 1) if (more_option and page > 0) else (len(visible) + 1 if page > 0 else 0)
        if more_option and selected == more_option:
            return _offer_menu(session_id, phone, data, page + 1)
        if selected == back_option:
            if page > 0:
                return _offer_menu(session_id, phone, data, page - 1)
            return _service_menu(session_id, phone, data)
        if selected < 1 or selected > len(visible):
            return _offer_menu(session_id, phone, data, page)
        offer = visible[selected - 1]
        data.update(
            {
                "offer_index": offer["index"],
                "offer_text": offer["value_text"],
                "amount": offer["amount"],
                "base_amount": offer["base_amount"],
                "value": offer["value"],
            }
        )
        save_session(session_id, phone, "recipient_choice", data)
        return con("Who receives the bundle?\n1. Self\n2. Other")

    if state == "recipient_choice":
        if entry == "1":
            data["recipient"] = phone
            save_session(session_id, phone, "confirm_order", data)
            return con(_confirmation(data))
        if entry == "2":
            save_session(session_id, phone, "enter_recipient", data)
            return con("Enter recipient phone number:")
        return con("Who receives the bundle?\n1. Self\n2. Other")

    if state == "enter_recipient":
        candidate = normalize_phone(entry)
        if not is_valid_gh_phone(candidate):
            return con("Invalid phone number.\nEnter recipient phone number:")
        data["recipient_candidate"] = candidate
        save_session(session_id, phone, "confirm_recipient", data)
        return con("Enter recipient number again to confirm:")

    if state == "confirm_recipient":
        candidate = normalize_phone(entry)
        if candidate != data.get("recipient_candidate"):
            save_session(session_id, phone, "enter_recipient", data)
            return con("Numbers do not match.\nEnter recipient phone number again:")
        data["recipient"] = candidate
        save_session(session_id, phone, "confirm_order", data)
        return con(_confirmation(data))

    if state == "confirm_order":
        if entry == "2":
            end_session(session_id, phone)
            return end("Order cancelled.")
        if entry != "1":
            return con(_confirmation(data))
        pending_id = create_pending_order(
            {
                "app": APP_NAME,
                "session_id": session_id,
                "dial_phone": phone,
                "agent_code": data.get("agent_code"),
                "agent_user_id": data.get("agent_user_id"),
                "admin_id": data.get("admin_id"),
                "store_slug": data.get("store_slug"),
                "service_id": data.get("service_id"),
                "service_name": data.get("service_name"),
                "offer_index": data.get("offer_index"),
                "offer_text": data.get("offer_text"),
                "amount": data.get("amount"),
                "base_amount": data.get("base_amount"),
                "recipient": data.get("recipient"),
                "value": data.get("value"),
            }
        )
        data["pending_order_id"] = pending_id
        save_session(session_id, phone, "payment_pending", data)
        return end("Order saved. Mobile money payment will be added next.")

    if state == "latest_order":
        return _service_menu(session_id, phone, data)

    if state == "payment_pending":
        if entry == "2":
            end_session(session_id, phone)
            return end("Payment session cancelled.")
        return con("Payment is still pending.\n1. Check payment\n2. Cancel")

    return _start(session_id, phone)
