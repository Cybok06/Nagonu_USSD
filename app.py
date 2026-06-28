from __future__ import annotations

import os

from flask import Flask, jsonify, request

from nagonu_store import normalize_phone
from ussd_nagonu import handle as handle_nagonu
from ussd_state import log_request
from ussd_zico import handle as handle_zico
from ussd_zico_state import log_request as log_zico_request
from zico_store import active_agent_code_exists as zico_agent_code_exists


def create_app() -> Flask:
    app = Flask(__name__)

    def _payload():
        if request.is_json:
            return request.get_json(silent=True) or {}
        return request.form if request.method == "POST" else request.args

    def _arkesel_text(payload):
        text = str(payload.get("userData") or "").strip()
        if not bool(payload.get("newSession")):
            return text
        if not (text.startswith("*") and text.endswith("#")):
            return text
        parts = [part.strip("#") for part in text.strip("*#").split("*") if part.strip("#")]
        if len(parts) <= 1:
            return ""
        return "*".join(parts[1:])

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
            else str(payload.get("text") or payload.get("ussdString") or "").strip()
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

    @app.route("/ussd", methods=["GET", "POST"])
    def ussd_shared():
        session_id, phone, text = _request_values()
        first_entry = next((p.strip() for p in text.split("*") if p.strip()), "")
        if not first_entry:
            response = "CON Welcome to DataWeb USSD\nEnter agent code:"
            return _respond(response)

        if zico_agent_code_exists(first_entry):
            response = handle_zico(session_id, phone, text)
            log_zico_request("zico", session_id, phone, text, response)
        else:
            response = handle_nagonu(session_id, phone, text)
            log_request("nagonu", session_id, phone, text, response)
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
    app.run(host="0.0.0.0", port=port, debug=True)
