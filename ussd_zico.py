from __future__ import annotations

from typing import Any, Dict, List

from zico_store import (
    agent_code_status,
    get_service_by_id,
    is_mtn_service_id,
    is_phone_eligible_for_mtn,
    is_valid_gh_phone,
    latest_order_for_phone,
    load_store_services,
    normalize_phone,
    validate_agent_code,
)
from ussd_zico_state import (
    create_pending_order,
    end_session,
    end_active_sessions_for_phone,
    get_recent_agent_code,
    get_session,
    get_unfinished_session,
    remember_agent_code,
    save_session,
    mark_pending_order_created,
)
from zico_ussd_orders import create_zico_ussd_order
from zico_paystack import initiate_payment, submit_otp, verify_payment
from zico_results_checker import (
    RESULTS_CHECKER_SERVICE_NAME,
    build_results_checker_service,
    is_results_checker_service_id,
)
from zico_ussd_complaints import (
    parse_payment_date_ddmmyyyy,
    parse_payment_time,
    submit_ussd_complaint,
    valid_phone as valid_complaint_phone,
)


APP_NAME = "zico"
OFFERS_PER_PAGE = 6
COMPLAINT_SERVICE_ID = "ussd_complaint"
COMPLAINT_SERVICE_NAME = "Complaint"


def con(text: str) -> str:
    return "CON " + text


def end(text: str) -> str:
    return "END " + text


def _parts(text: str) -> List[str]:
    return [p.strip() for p in (text or "").split("*") if p.strip()]


def _complaint_service() -> Dict[str, Any]:
    return {"id": COMPLAINT_SERVICE_ID, "name": COMPLAINT_SERVICE_NAME, "kind": "complaint", "offers": []}


def _runtime_services(store: Dict[str, Any]) -> List[Dict[str, Any]]:
    services = load_store_services(store or {})
    checker_service = build_results_checker_service(store or {})
    if checker_service:
        services = [*services, checker_service]
    return [*services, _complaint_service()]


def _runtime_service_by_id(store: Dict[str, Any], service_id: str) -> Dict[str, Any] | None:
    for service in _runtime_services(store or {}):
        if service.get("id") == service_id:
            return service
    return None


def _complaint_target_services(store: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [svc for svc in _runtime_services(store or {}) if svc.get("id") != COMPLAINT_SERVICE_ID]


def _start(session_id: str, phone: str) -> str:
    unfinished = get_unfinished_session(phone)
    if unfinished:
        data = unfinished.get("data") or {}
        state = unfinished.get("state") or ""
        save_session(session_id, phone, state, data)
        if state == "confirm_order":
            return con(_confirmation(data))
        if state == "otp_pending":
            return con("Enter the OTP/voucher code sent by your network:")
        if state == "payment_pending":
            return con("Payment is still pending.\n1. Check payment\n2. Cancel")
        if state == "complaint_phone":
            return con("Complaint\nEnter phone number used (0xxxxxxxxx):")
        if state == "complaint_ref":
            return con("Enter MOMO TRANSACTION ID:")
        if state == "complaint_date":
            return con("Enter payment date (dd/mm/yyyy):")
        if state == "complaint_time":
            return con("Enter payment time (HH:MM):")
        if state == "complaint_service":
            return _complaint_service_menu(session_id, phone, data)
        if state == "complaint_offer":
            return _complaint_offer_menu(session_id, phone, data, int(data.get("complaint_offer_page") or 0))
        if state == "complaint_message":
            return con("Enter complaint details or 0 to skip:")

    recent = get_recent_agent_code(phone, APP_NAME)
    if recent and recent.get("agent_code"):
        save_session(session_id, phone, "reuse_agent_code", {"recent_agent_code": recent.get("agent_code")})
        return con(f"Do you want to continue with your recent agent code ({recent.get('agent_code')})?\n1. Yes\n2. No")

    save_session(session_id, phone, "enter_agent_code", {})
    return con("Welcome to AZICO USSD\nEnter agent code:")


def _load_agent(session_id: str, phone: str, code: str) -> str:
    loaded = validate_agent_code(code)
    if not loaded:
        end_session(session_id, phone)
        if agent_code_status(code) == "inactive":
            return end("Unavailable at the moment,Please try again later")
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
    services = _runtime_services(store) if store else []
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


def _complaint_service_menu(session_id: str, phone: str, data: Dict[str, Any]) -> str:
    store = _store_doc(data)
    services = _complaint_target_services(store) if store else []
    if not services:
        end_session(session_id, phone)
        return end("No services are available for complaint selection.")

    data["complaint_services"] = [{"id": s["id"], "name": s["name"]} for s in services]
    save_session(session_id, phone, "complaint_service", data)
    lines = ["Select service for complaint:"]
    for idx, service in enumerate(services, start=1):
        lines.append(f"{idx}. {service['name']}")
    lines.append("0. Cancel")
    return con("\n".join(lines))


def _complaint_offer_menu(session_id: str, phone: str, data: Dict[str, Any], page: int = 0) -> str:
    store = _store_doc(data)
    service = _runtime_service_by_id(store, data.get("complaint_service_id") or "") if store else None
    if not service:
        return _complaint_service_menu(session_id, phone, data)

    offers = service.get("offers") or []
    start = page * OFFERS_PER_PAGE
    visible = offers[start:start + OFFERS_PER_PAGE]
    data["complaint_offer_page"] = page
    save_session(session_id, phone, "complaint_offer", data)

    lines = [f"{service['name']} packages:"]
    for idx, offer in enumerate(visible, start=1):
        lines.append(f"{idx}. {offer['value_text']} - GHS {offer['amount']:.2f}")
    next_no = len(visible) + 1
    if start + OFFERS_PER_PAGE < len(offers):
        lines.append(f"{next_no}.More")
        next_no += 1
    if page > 0:
        lines.append(f"{next_no}. Back")
    else:
        lines.append("0. Back")
    return con("\n".join(lines))


def _submit_complaint(session_id: str, phone: str, data: Dict[str, Any]) -> str:
    payload = {
        "phone": data.get("complaint_phone"),
        "paystack_reference": data.get("complaint_ref"),
        "payment_date_user": data.get("complaint_date"),
        "payment_time": data.get("complaint_time"),
        "service_name": data.get("complaint_service_name"),
        "offer": data.get("complaint_offer_text"),
        "message": data.get("complaint_message") or "",
        "order_id": data.get("order_id") or "",
        "cart": [
            {
                "serviceName": data.get("complaint_service_name"),
                "value": data.get("complaint_offer_text"),
                "amount": data.get("complaint_amount") or 0,
            }
        ],
    }
    result = submit_ussd_complaint(data.get("store_slug") or "", payload)
    end_session(session_id, phone)
    if result.get("success"):
        return end("Complaint sent successfully. Your issue will be resolved shortly, Thank You")
    return end(result.get("message") or "Complaint could not be submitted.")


def _offer_menu(session_id: str, phone: str, data: Dict[str, Any], page: int = 0) -> str:
    store = _store_doc(data)
    service = _runtime_service_by_id(store, data.get("service_id") or "") if store else None
    if not service:
        save_session(session_id, phone, "select_service", data)
        return con("Service not found.\nSelect service again:")

    offers = service.get("offers") or []
    start = page * OFFERS_PER_PAGE
    visible = offers[start:start + OFFERS_PER_PAGE]
    data["offer_page"] = page
    save_session(session_id, phone, "select_offer", data)

    lines = ["Select checker type:" if is_results_checker_service_id(service.get("id")) else f"{service['name']} packages:"]
    for idx, offer in enumerate(visible, start=1):
        lines.append(f"{idx}. {offer['value_text']} - GHS {offer['amount']:.2f}")
    next_no = len(visible) + 1
    if start + OFFERS_PER_PAGE < len(offers):
        lines.append(f"{next_no}.More")
        next_no += 1
    if page > 0:
        lines.append(f"{next_no}. Back")
    else:
        lines.append("0. Back")
    return con("\n".join(lines))


def _confirmation(data: Dict[str, Any]) -> str:
    if data.get("product_kind") == "results_checker":
        return (
            "Confirm order:\n"
            f"{RESULTS_CHECKER_SERVICE_NAME}\n"
            f"{data.get('offer_text')} - GHS {float(data.get('amount') or 0):.2f}\n"
            f"SMS to: {data.get('recipient')}\n"
            "1. Place Order\n"
            "2. Cancel"
        )
    return (
        "Confirm order:\n"
        f"{data.get('service_name')}\n"
        f"{data.get('offer_text')} - GHS {float(data.get('amount') or 0):.2f}\n"
        f"Recipient: {data.get('recipient')}\n"
        "1. Place Order\n"
        "2. Cancel"
    )


def _reject_ineligible_mtn(session_id: str, phone: str, data: Dict[str, Any], recipient: str) -> str | None:
    if not is_mtn_service_id(data.get("service_id")):
        return None
    if is_phone_eligible_for_mtn(recipient):
        return None
    data.pop("recipient", None)
    save_session(session_id, phone, "enter_recipient", data)
    return con("Ineligible number.\nEnter recipient phone number:")


def handle(session_id: str, phone: str, text: str) -> str:
    phone = normalize_phone(phone)
    parts = _parts(text)

    if not parts:
        return _start(session_id, phone)

    session = get_session(session_id, phone)
    data: Dict[str, Any] = (session or {}).get("data") or {}
    state = (session or {}).get("state") or "enter_agent_code"
    entry = parts[-1]

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
        services = _runtime_services(store) if store else []
        if selected < 1 or selected > len(services):
            return _service_menu(session_id, phone, data)
        service = services[selected - 1]
        if service.get("id") == COMPLAINT_SERVICE_ID:
            save_session(session_id, phone, "complaint_phone", data)
            return con("Complaint\nEnter phone number used (0xxxxxxxxx):")
        data.update({"service_id": service["id"], "service_name": service["name"]})
        return _offer_menu(session_id, phone, data, 0)

    if state == "select_offer":
        store = _store_doc(data)
        service = _runtime_service_by_id(store, data.get("service_id") or "") if store else None
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
        if is_results_checker_service_id(service.get("id")):
            data["product_kind"] = "results_checker"
            data["checker_type"] = offer.get("checker_type") or offer.get("value")
            data["recipient"] = phone
            save_session(session_id, phone, "confirm_order", data)
            return con(_confirmation(data))
        else:
            data.pop("product_kind", None)
            data.pop("checker_type", None)
        save_session(session_id, phone, "recipient_choice", data)
        return con("Who receives the bundle?\n1. Self\n2. Other")

    if state == "recipient_choice":
        if entry == "1":
            rejection = _reject_ineligible_mtn(session_id, phone, data, phone)
            if rejection:
                return rejection
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
        rejection = _reject_ineligible_mtn(session_id, phone, data, candidate)
        if rejection:
            data.pop("recipient_candidate", None)
            return rejection
        data["recipient"] = candidate
        data.pop("recipient_candidate", None)
        save_session(session_id, phone, "confirm_order", data)
        return con(_confirmation(data))

    if state == "confirm_order":
        if entry == "2":
            end_active_sessions_for_phone(phone)
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
                "product_kind": data.get("product_kind"),
                "checker_type": data.get("checker_type"),
            }
        )
        data["pending_order_id"] = pending_id
        created = create_zico_ussd_order(data, session_id, phone)
        if not created.get("success"):
            save_session(session_id, phone, "confirm_order", data)
            return con(f"{created.get('message') or 'Order could not be placed.'}\n1. Try Again\n2. Cancel")
        data["order_id"] = created.get("order_id")
        data["charged_amount"] = created.get("charged_amount")
        data["gateway_fee"] = created.get("gateway_fee")
        mark_pending_order_created(pending_id, created.get("order_id") or "")
        payment = initiate_payment(created, data, session_id, phone)
        data["paystack_reference"] = payment.get("reference")
        data["payment_status"] = payment.get("status")

        if not payment.get("success"):
            save_session(session_id, phone, "confirm_order", data)
            return con(f"{payment.get('message') or 'Payment could not be started.'}\n1. Try Again\n2. Cancel")

        if payment.get("status") == "success":
            if not payment.get("released_provider_processing") and payment.get("reason") not in {"already_released", ""}:
                end_session(session_id, phone)
                return end(payment.get("message") or "Payment received but order could not be released. Please contact support.")
            end_session(session_id, phone)
            if data.get("product_kind") == "results_checker":
                return end(f"Payment confirmed. Checker sent by SMS.\nOrder ID: {created.get('order_id')}")
            return end(f"Payment received. Order processing started.\nOrder ID: {created.get('order_id')}")

        if payment.get("status") == "send_otp":
            data["paystack_requires_otp"] = True
            save_session(session_id, phone, "otp_pending", data)
            return con(f"{payment.get('message') or 'Enter OTP/voucher code'}:")

        save_session(session_id, phone, "payment_pending", data)
        return end(
            "Payment prompt sent to your phone.\n"
            f"Approve GHS {float(created.get('charged_amount') or 0):.2f} to "
            f"{'receive your checker by SMS' if data.get('product_kind') == 'results_checker' else 'process order'} {created.get('order_id')}."
        )

    if state == "latest_order":
        return _service_menu(session_id, phone, data)

    if state == "complaint_phone":
        if not valid_complaint_phone(entry):
            save_session(session_id, phone, "complaint_phone", data)
            return con("Invalid phone number.\nEnter phone number used (0xxxxxxxxx):")
        data["complaint_phone"] = entry.strip()
        save_session(session_id, phone, "complaint_ref", data)
        return con("Enter MOMO TRANSACTION ID:")

    if state == "complaint_ref":
        ref = str(entry or "").strip()
        if ref and ref == str(data.get("complaint_phone") or "").strip():
            save_session(session_id, phone, "complaint_ref", data)
            return con("Transaction ID cannot match the phone number.\nEnter MOMO TRANSACTION ID:")
        data["complaint_ref"] = ref
        save_session(session_id, phone, "complaint_date", data)
        return con("Enter payment date (dd/mm/yyyy):")

    if state == "complaint_date":
        if str(entry or "").strip() == str(data.get("complaint_ref") or "").strip():
            save_session(session_id, phone, "complaint_date", data)
            return con("Date cannot match the transaction ID.\nEnter payment date (dd/mm/yyyy):")
        ok_date, _, _ = parse_payment_date_ddmmyyyy(str(entry).strip())
        if not ok_date:
            save_session(session_id, phone, "complaint_date", data)
            return con("Invalid date format.\nEnter payment date (dd/mm/yyyy):")
        data["complaint_date"] = str(entry).strip()
        save_session(session_id, phone, "complaint_time", data)
        return con("Enter payment time (HH:MM):")

    if state == "complaint_time":
        if str(entry or "").strip() == str(data.get("complaint_date") or "").strip():
            save_session(session_id, phone, "complaint_time", data)
            return con("Time cannot match the payment date.\nEnter payment time (HH:MM):")
        ok_time, _ = parse_payment_time(str(entry).strip())
        if not ok_time:
            save_session(session_id, phone, "complaint_time", data)
            return con("Invalid time format.\nEnter payment time (HH:MM):")
        data["complaint_time"] = str(entry).strip()
        return _complaint_service_menu(session_id, phone, data)

    if state == "complaint_service":
        if entry == "0":
            end_session(session_id, phone)
            return end("Complaint cancelled.")
        try:
            selected = int(entry)
        except Exception:
            return _complaint_service_menu(session_id, phone, data)
        store = _store_doc(data)
        services = _complaint_target_services(store) if store else []
        if selected < 1 or selected > len(services):
            return _complaint_service_menu(session_id, phone, data)
        service = services[selected - 1]
        data["complaint_service_id"] = service.get("id")
        data["complaint_service_name"] = service.get("name")
        return _complaint_offer_menu(session_id, phone, data, 0)

    if state == "complaint_offer":
        store = _store_doc(data)
        service = _runtime_service_by_id(store, data.get("complaint_service_id") or "") if store else None
        if not service:
            return _complaint_service_menu(session_id, phone, data)
        page = int(data.get("complaint_offer_page") or 0)
        offers = service.get("offers") or []
        start = page * OFFERS_PER_PAGE
        visible = offers[start:start + OFFERS_PER_PAGE]
        try:
            selected = int(entry)
        except Exception:
            return _complaint_offer_menu(session_id, phone, data, page)
        more_option = len(visible) + 1 if start + OFFERS_PER_PAGE < len(offers) else None
        back_option = (more_option + 1) if (more_option and page > 0) else (len(visible) + 1 if page > 0 else 0)
        if more_option and selected == more_option:
            return _complaint_offer_menu(session_id, phone, data, page + 1)
        if selected == back_option:
            if page > 0:
                return _complaint_offer_menu(session_id, phone, data, page - 1)
            return _complaint_service_menu(session_id, phone, data)
        if selected < 1 or selected > len(visible):
            return _complaint_offer_menu(session_id, phone, data, page)
        offer = visible[selected - 1]
        data["complaint_offer_text"] = offer.get("value_text")
        data["complaint_amount"] = offer.get("amount")
        save_session(session_id, phone, "complaint_message", data)
        return con("Enter complaint details or 0 to skip:")

    if state == "complaint_message":
        data["complaint_message"] = "" if str(entry).strip() == "0" else str(entry).strip()
        return _submit_complaint(session_id, phone, data)

    if state == "payment_pending":
        if entry == "2":
            end_session(session_id, phone)
            return end("Payment session cancelled.")
        reference = data.get("paystack_reference") or ""
        checked = verify_payment(reference) if reference else {"success": False, "status": "not_found"}
        status = checked.get("status")
        if checked.get("success") and checked.get("released_provider_processing"):
            end_session(session_id, phone)
            if data.get("product_kind") == "results_checker":
                return end(f"Payment confirmed. Checker sent by SMS.\nOrder ID: {data.get('order_id') or ''}")
            return end(f"Payment confirmed. Order processing started.\nOrder ID: {data.get('order_id') or ''}")
        if status == "success":
            if checked.get("reason") not in {"already_released", "", None} and not checked.get("released_provider_processing"):
                end_session(session_id, phone)
                return end(checked.get("message") or "Payment received but order could not be released. Please contact support.")
            end_session(session_id, phone)
            if data.get("product_kind") == "results_checker":
                return end(f"Payment confirmed. Checker sent by SMS.\nOrder ID: {data.get('order_id') or ''}")
            return end(f"Payment confirmed.\nOrder ID: {data.get('order_id') or ''}")
        if status == "send_otp":
            data["paystack_requires_otp"] = True
            save_session(session_id, phone, "otp_pending", data)
            return con(f"{checked.get('message') or 'Enter OTP/voucher code'}:")
        if status in {"failed", "abandoned", "reversed", "timeout"}:
            end_session(session_id, phone)
            return end("Payment failed or expired. Please start again.")
        save_session(session_id, phone, "payment_pending", data)
        return con("Payment is still pending.\n1. Check payment\n2. Cancel")

    if state == "otp_pending":
        reference = data.get("paystack_reference") or ""
        result = submit_otp(reference, entry)
        status = result.get("status")
        if result.get("success") and status == "success":
            if not result.get("released_provider_processing") and result.get("reason") not in {"already_released", "", None}:
                end_session(session_id, phone)
                return end(result.get("message") or "Payment received but order could not be released. Please contact support.")
            end_session(session_id, phone)
            if data.get("product_kind") == "results_checker":
                return end(f"Payment confirmed. Checker sent by SMS.\nOrder ID: {data.get('order_id') or ''}")
            return end(f"Payment confirmed. Order processing started.\nOrder ID: {data.get('order_id') or ''}")
        if result.get("success") and status in {"pending", "pay_offline", "processing"}:
            data["paystack_requires_otp"] = False
            save_session(session_id, phone, "payment_pending", data)
            return end("Payment prompt sent. Please approve on your phone.")
        if result.get("success") and status == "send_otp":
            save_session(session_id, phone, "otp_pending", data)
            return con(f"{result.get('message') or 'Enter OTP/voucher code'}:")
        save_session(session_id, phone, "otp_pending", data)
        return con(f"{result.get('message') or 'Invalid OTP/voucher.'}\nEnter OTP/voucher code:")

    return _start(session_id, phone)
