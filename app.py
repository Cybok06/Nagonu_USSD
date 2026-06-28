from __future__ import annotations

import os

from flask import Flask, request

from nagonu_store import normalize_phone
from ussd_nagonu import handle as handle_nagonu
from ussd_state import log_request
from ussd_zico import handle as handle_zico
from ussd_zico_state import log_request as log_zico_request
from zico_store import active_agent_code_exists as zico_agent_code_exists


def create_app() -> Flask:
    app = Flask(__name__)

    def _payload():
        return request.form if request.method == "POST" else request.args

    def _request_values():
        payload = _payload()
        session_id = (
            payload.get("sessionId")
            or payload.get("session_id")
            or payload.get("session")
            or ""
        ).strip()
        phone = normalize_phone(
            payload.get("phoneNumber")
            or payload.get("msisdn")
            or payload.get("phone")
            or ""
        )
        text = (payload.get("text") or payload.get("ussdString") or "").strip()
        if not session_id:
            session_id = f"session-{phone or 'unknown'}"
        return session_id, phone, text

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
            return response, 200, {"Content-Type": "text/plain; charset=utf-8"}

        if zico_agent_code_exists(first_entry):
            response = handle_zico(session_id, phone, text)
            log_zico_request("zico", session_id, phone, text, response)
        else:
            response = handle_nagonu(session_id, phone, text)
            log_request("nagonu", session_id, phone, text, response)
        return response, 200, {"Content-Type": "text/plain; charset=utf-8"}

    @app.route("/ussd/zico", methods=["GET", "POST"])
    def ussd_zico():
        session_id, phone, text = _request_values()
        response = handle_zico(session_id, phone, text)
        log_zico_request("zico", session_id, phone, text, response)
        return response, 200, {"Content-Type": "text/plain; charset=utf-8"}

    @app.route("/ussd/nagonu", methods=["GET", "POST"])
    def ussd_nagonu():
        session_id, phone, text = _request_values()
        response = handle_nagonu(session_id, phone, text)
        log_request("nagonu", session_id, phone, text, response)
        return response, 200, {"Content-Type": "text/plain; charset=utf-8"}

    return app


app = create_app()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
