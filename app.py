from __future__ import annotations

import os

import re

from flask import Flask, jsonify, request

from nagonu_store import normalize_phone
from nagonu_paystack import handle_webhook as handle_nagonu_paystack_webhook
from ussd_nagonu import handle as handle_nagonu
from ussd_state import get_recent_agent_code as get_nagonu_recent_agent_code
from ussd_state import get_session as get_nagonu_session
from ussd_state import get_unfinished_session as get_nagonu_unfinished_session
from ussd_state import log_request
from ussd_zico import handle as handle_zico
from ussd_zico_state import get_recent_agent_code as get_zico_recent_agent_code
from ussd_zico_state import get_session as get_zico_session
from ussd_zico_state import get_unfinished_session as get_zico_unfinished_session
from ussd_zico_state import log_request as log_zico_request
from zico_store import active_agent_code_exists as zico_agent_code_exists


def create_app() -> Flask:
    app = Flask(__name__)

    def _payload():
        if request.is_json:
            return request.get_json(silent=True) or {}
        return request.form if request.method == "POST" else request.args

    def _is_true(value):
        if isinstance(value, bool):
            return value
        return str(value or "").strip().lower() in {"1", "true", "yes", "y"}

    def _clean_ussd_text(value):
        return (
            str(value or "")
            .replace("＃", "#")
            .replace("＊", "*")
            .replace("%23", "#")
            .strip()
        )

    def _arkesel_text(payload):
        text = _clean_ussd_text(
            payload.get("userData")
            or payload.get("message")
            or payload.get("text")
            or payload.get("ussdString")
            or ""
        )
        if not _is_true(payload.get("newSession")):
            return text
        if not (text.startswith("*") and text.endswith("#")):
            return text
        parts = [part.strip("#") for part in text.strip("*#").split("*") if part.strip("#")]
        if len(parts) <= 1:
            return ""
        return "*".join(parts[1:])

    def _strip_leading_dial_parts(text):
        parts = [p.strip() for p in _clean_ussd_text(text).strip("*#").split("*") if p.strip()]
        for idx, part in enumerate(parts):
            if re.fullmatch(r"\d{5}", part):
                return "*".join(parts[idx:])
        return ""

    def _request_values():
        payload = _payload()
        session_id = (
            payload.get("sessionID")
            or payload.get("sessionId")
            or payload.get("session_id")
            or payload.get("session")
            or ""
        )
        session_id = str(session_id or "").strip()
        phone = normalize_phone(
            payload.get("phoneNumber")
            or payload.get("msisdn")
            or payload.get("phone")
            or ""
        )
        text = (
            _arkesel_text(payload)
            if request.is_json
            else _clean_ussd_text(payload.get("text") or payload.get("ussdString") or payload.get("userData") or "")
        )
        if not session_id:
            session_id = f"session-{phone or 'unknown'}"
        return session_id, phone, text

    def _ussd_body(response):
        if response.startswith("CON "):
            return response[4:], True
        if response.startswith("END "):
            return response[4:], False
        return response, False

    def _respond(response):
        if not request.is_json:
            return response, 200, {"Content-Type": "text/plain; charset=utf-8"}

        payload = _payload()
        message, continue_session = _ussd_body(response)
        return jsonify(
            {
                "sessionID": str(payload.get("sessionID") or payload.get("sessionId") or ""),
                "userID": str(payload.get("userID") or ""),
                "msisdn": str(payload.get("msisdn") or payload.get("phoneNumber") or payload.get("phone") or ""),
                "message": message,
                "continueSession": continue_session,
            }
        )

    @app.route("/healthz")
    def healthz():
        return "ok", 200

    @app.route("/", methods=["GET"])
    def index():
        return "USSD Runner is running", 200

    @app.route("/paystack/nagonu/webhook", methods=["POST"])
    @app.route("/paystack/webhook", methods=["POST"])
    def paystack_nagonu_webhook():
        raw_body = request.get_data() or b""
        signature = request.headers.get("x-paystack-signature") or request.headers.get("X-Paystack-Signature") or ""
        payload, status_code = handle_nagonu_paystack_webhook(raw_body, signature)
        return jsonify(payload), status_code

    @app.route("/ussd", methods=["GET", "POST"])
    def ussd_shared():
        session_id, phone, text = _request_values()
        zico_session = get_zico_session(session_id, phone)
        nagonu_session = get_nagonu_session(session_id, phone)

        if zico_session and not nagonu_session:
            response = handle_zico(session_id, phone, text)
            log_zico_request("zico", session_id, phone, text, response)
            return _respond(response)
        if nagonu_session and not zico_session:
            response = handle_nagonu(session_id, phone, text)
            log_request("nagonu", session_id, phone, text, response)
            return _respond(response)

        routed_text = _strip_leading_dial_parts(text)
        first_entry = next((p.strip() for p in routed_text.split("*") if p.strip()), "")
        if not first_entry:
            if get_zico_unfinished_session(phone) or get_zico_recent_agent_code(phone, "zico"):
                response = handle_zico(session_id, phone, "")
                log_zico_request("zico", session_id, phone, "", response)
                return _respond(response)
            if get_nagonu_unfinished_session(phone) or get_nagonu_recent_agent_code(phone, "nagonu"):
                response = handle_nagonu(session_id, phone, "")
                log_request("nagonu", session_id, phone, "", response)
                return _respond(response)
            response = "CON Enter Agent code to continue"
            return _respond(response)

        if zico_agent_code_exists(first_entry):
            response = handle_zico(session_id, phone, routed_text)
            log_zico_request("zico", session_id, phone, routed_text, response)
        else:
            response = handle_nagonu(session_id, phone, routed_text)
            log_request("nagonu", session_id, phone, routed_text, response)
        return _respond(response)

    @app.route("/ussd/zico", methods=["GET", "POST"])
    def ussd_zico():
        session_id, phone, text = _request_values()
        response = handle_zico(session_id, phone, text)
        log_zico_request("zico", session_id, phone, text, response)
        return _respond(response)

    @app.route("/ussd/nagonu", methods=["GET", "POST"])
    def ussd_nagonu():
        session_id, phone, text = _request_values()
        response = handle_nagonu(session_id, phone, text)
        log_request("nagonu", session_id, phone, text, response)
        return _respond(response)

    return app


app = create_app()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    debug = os.getenv("FLASK_DEBUG", "0").strip().lower() in {"1", "true", "yes"}
    app.run(host="0.0.0.0", port=port, debug=debug)
