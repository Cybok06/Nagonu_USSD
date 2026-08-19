from __future__ import annotations

from typing import Any, Dict

from nagonu_paystack import initiate_payment, submit_otp, verify_payment
from nagonu_results_checker import (
    RESULTS_CHECKER_SERVICE_ID,
    build_public_results_checker_service,
)
from nagonu_ussd_orders import create_nagonu_guest_checker_order
from ussd_state import (
    create_pending_order,
    end_session,
    get_unfinished_session,
    get_session,
    mark_pending_order_created,
    save_session,
)


GUEST_STATES = {
    "guest_checker_type",
    "guest_checker_confirm",
    "guest_checker_payment_pending",
    "guest_checker_otp_pending",
}


def con(message: str) -> str:
    return "CON " + message


def end(message: str) -> str:
    return "END " + message


def is_guest_state(state: Any) -> bool:
    return str(state or "") in GUEST_STATES


def _service() -> Dict[str, Any] | None:
    return build_public_results_checker_service()


def _type_menu() -> str:
    service = _service()
    if not service:
        return end("Results Checker is currently unavailable. Please try again later.")
    lines = ["Select Results Checker type:"]
    for number, offer in enumerate(service.get("offers") or [], start=1):
        available = int(offer.get("inventory") or 0) > 0 and float(offer.get("amount") or 0) > 0
        suffix = f" - GHS {float(offer.get('amount') or 0):.2f}" if available else " - Unavailable"
        lines.append(f"{number}. {offer.get('value_text')}{suffix}")
    return con("\n".join(lines))


def start(session_id: str, phone: str) -> str:
    unfinished = get_unfinished_session(phone)
    if unfinished and is_guest_state(unfinished.get("state")):
        state = str(unfinished.get("state") or "")
        data = unfinished.get("data") or {}
        save_session(session_id, phone, state, data)
        if state == "guest_checker_type":
            return _type_menu()
        if state == "guest_checker_confirm":
            return con(_confirmation(data))
        if state == "guest_checker_payment_pending":
            return con("Payment is still pending.\n1. Check payment\n2. Cancel")
        if state == "guest_checker_otp_pending":
            return con("Enter the OTP/voucher code sent by your network:")
    service = _service()
    if not service:
        end_session(session_id, phone)
        return end("Results Checker is currently unavailable. Please try again later.")
    save_session(
        session_id,
        phone,
        "guest_checker_type",
        {"guest_checker": True, "recipient": phone},
    )
    return _type_menu()


def _confirmation(data: Dict[str, Any]) -> str:
    return (
        "Confirm purchase:\n"
        f"{data.get('offer_text')} Results Checker\n"
        f"GHS {float(data.get('amount') or 0):.2f}\n"
        f"SMS to: {data.get('recipient')}\n"
        "1. Pay\n"
        "2. Cancel"
    )


def handle(session_id: str, phone: str, text: str) -> str:
    session = get_session(session_id, phone)
    state = str((session or {}).get("state") or "")
    data: Dict[str, Any] = (session or {}).get("data") or {}
    entry = str(text or "").strip().split("*")[-1]

    if not is_guest_state(state):
        return start(session_id, phone)

    if state == "guest_checker_type":
        service = _service()
        if not service:
            end_session(session_id, phone)
            return end("Results Checker is currently unavailable. Please try again later.")
        try:
            selected = int(entry)
        except Exception:
            return _type_menu()
        offers = service.get("offers") or []
        if selected < 1 or selected > len(offers):
            return _type_menu()
        offer = offers[selected - 1]
        if int(offer.get("inventory") or 0) <= 0 or float(offer.get("amount") or 0) <= 0:
            return con(f"{offer.get('value_text')} checker is unavailable.\n" + _type_menu()[4:])
        data.update(
            {
                "guest_checker": True,
                "service_id": RESULTS_CHECKER_SERVICE_ID,
                "service_name": "Results Checker",
                "offer_index": offer.get("index"),
                "offer_text": offer.get("value_text"),
                "amount": offer.get("amount"),
                "base_amount": offer.get("base_amount"),
                "value": offer.get("value"),
                "checker_type": offer.get("checker_type"),
                "product_kind": "results_checker",
                "recipient": phone,
            }
        )
        save_session(session_id, phone, "guest_checker_confirm", data)
        return con(_confirmation(data))

    if state == "guest_checker_confirm":
        if entry == "2":
            end_session(session_id, phone)
            return end("Purchase cancelled.")
        if entry != "1":
            return con(_confirmation(data))
        pending_id = create_pending_order(
            {
                "app": "nagonu",
                "source": "guest_results_checker",
                "session_id": session_id,
                "dial_phone": phone,
                "service_id": data.get("service_id"),
                "service_name": data.get("service_name"),
                "offer_index": data.get("offer_index"),
                "offer_text": data.get("offer_text"),
                "amount": data.get("amount"),
                "base_amount": data.get("base_amount"),
                "recipient": phone,
                "value": data.get("value"),
                "product_kind": "results_checker",
                "checker_type": data.get("checker_type"),
            }
        )
        data["pending_order_id"] = pending_id
        created = create_nagonu_guest_checker_order(data, session_id, phone)
        if not created.get("success"):
            save_session(session_id, phone, "guest_checker_confirm", data)
            return con(f"{created.get('message') or 'Purchase could not be started.'}\n1. Try Again\n2. Cancel")
        data["order_id"] = created.get("order_id")
        data["charged_amount"] = created.get("charged_amount")
        data["gateway_fee"] = created.get("gateway_fee")
        mark_pending_order_created(pending_id, created.get("order_id") or "")
        payment = initiate_payment(created, data, session_id, phone)
        data["paystack_reference"] = payment.get("reference")
        if not payment.get("success"):
            save_session(session_id, phone, "guest_checker_confirm", data)
            return con(f"{payment.get('message') or 'Payment could not be started.'}\n1. Try Again\n2. Cancel")
        if payment.get("status") == "success":
            end_session(session_id, phone)
            return end(f"Payment confirmed. Results Checker sent by SMS.\nOrder ID: {created.get('order_id')}")
        if payment.get("status") == "send_otp":
            save_session(session_id, phone, "guest_checker_otp_pending", data)
            return con(f"{payment.get('message') or 'Enter OTP/voucher code'}:")
        save_session(session_id, phone, "guest_checker_payment_pending", data)
        return end(
            "Payment prompt sent to your phone.\n"
            f"Approve GHS {float(created.get('charged_amount') or 0):.2f} to receive your checker by SMS."
        )

    if state == "guest_checker_payment_pending":
        if entry == "2":
            end_session(session_id, phone)
            return end("Payment session cancelled.")
        reference = data.get("paystack_reference") or ""
        checked = verify_payment(reference) if reference else {"success": False, "status": "not_found"}
        status = checked.get("status")
        if checked.get("success") and status == "success":
            end_session(session_id, phone)
            return end(f"Payment confirmed. Results Checker sent by SMS.\nOrder ID: {data.get('order_id') or ''}")
        if status == "send_otp":
            save_session(session_id, phone, "guest_checker_otp_pending", data)
            return con(f"{checked.get('message') or 'Enter OTP/voucher code'}:")
        if status in {"failed", "abandoned", "reversed", "timeout"}:
            end_session(session_id, phone)
            return end("Payment failed or expired. Please start again.")
        save_session(session_id, phone, "guest_checker_payment_pending", data)
        return con("Payment is still pending.\n1. Check payment\n2. Cancel")

    if state == "guest_checker_otp_pending":
        result = submit_otp(data.get("paystack_reference") or "", entry)
        status = result.get("status")
        if result.get("success") and status == "success":
            end_session(session_id, phone)
            return end(f"Payment confirmed. Results Checker sent by SMS.\nOrder ID: {data.get('order_id') or ''}")
        if result.get("success") and status in {"pending", "pay_offline", "processing"}:
            save_session(session_id, phone, "guest_checker_payment_pending", data)
            return end("Payment prompt sent. Please approve on your phone.")
        save_session(session_id, phone, "guest_checker_otp_pending", data)
        return con(f"{result.get('message') or 'Invalid OTP/voucher.'}\nEnter OTP/voucher code:")

    return start(session_id, phone)
