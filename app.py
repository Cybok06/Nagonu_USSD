from __future__ import annotations

import re
import json
import os
import uuid

from flask import Flask, jsonify, request

from nagonu_store import normalize_phone
from nagonu_paystack import handle_webhook as handle_nagonu_paystack_webhook
from ussd_nagonu import handle as handle_nagonu
from ussd_state import log_request
from zico_paystack import handle_webhook as handle_zico_paystack_webhook
from ussd_zico import handle as handle_zico
from ussd_zico_state import log_request as log_zico_request
from zico_store import agent_code_exists as zico_agent_code_exists
from ussd_providers.base import USSDAdapterError, normalize_menu_response
from ussd_providers.moolre import MoolreUSSDAdapter
from ussd_callback_idempotency import claim, complete, fail, find_replay
from nagonu_db import db as nagonu_db
from zico_db import db as zico_db
from ussd_guest_checker import handle as handle_guest_checker
from ussd_customer_history import should_start_guest_checker
from phone_number_registry import phone_number_exists
from ussd_session_routes import get_session_route, save_session_route


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024
    moolre_adapter = MoolreUSSDAdapter()

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

    def _last_five_digit_part(text):
        parts = [p.strip() for p in _clean_ussd_text(text).strip("*#").split("*") if p.strip()]
        for part in reversed(parts):
            if re.fullmatch(r"\d{5}", part):
                return part
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
        normalized = normalize_menu_response(response)
        return normalized.message, normalized.continue_session

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

    def _has_order_history(phone):
        return phone_number_exists(nagonu_db["phone_numbers"], phone)

    def _dispatch_shared(session_id, phone, text, is_new_session=False, routed_application=None):
        """Run the existing shared Nagonu/Zico resolver with neutral values."""
        if not is_new_session and routed_application == "nagonu_guest_checker":
            response = handle_guest_checker(session_id, phone, text)
            log_request("nagonu_guest_checker", session_id, phone, text, response)
            return response, "nagonu_guest_checker"
        if not is_new_session and routed_application == "zico":
            response = handle_zico(session_id, phone, text)
            log_zico_request("zico", session_id, phone, text, response)
            return response, "zico"
        if not is_new_session and routed_application == "nagonu":
            response = handle_nagonu(session_id, phone, text)
            log_request("nagonu", session_id, phone, text, response)
            return response, "nagonu"
        if is_new_session and should_start_guest_checker(is_new_session=True, has_session=False, has_history=_has_order_history(phone)):
            response = handle_guest_checker(session_id, phone, "")
            log_request("nagonu_guest_checker", session_id, phone, "", response)
            return response, "nagonu_guest_checker"

        routed_text = _strip_leading_dial_parts(text)
        routed_parts = [p.strip() for p in routed_text.split("*") if p.strip()]
        first_entry = routed_parts[0] if routed_parts else ""
        second_entry = routed_parts[1] if len(routed_parts) > 1 else ""
        last_code_entry = _last_five_digit_part(routed_text)
        if not first_entry:
            return "CON Enter Agent code to continue", "unknown"

        if first_entry in {"1", "2"} and second_entry:
            if zico_agent_code_exists(second_entry):
                response = handle_zico(session_id, phone, routed_text)
                log_zico_request("zico", session_id, phone, routed_text, response)
                return response, "zico"
            response = handle_nagonu(session_id, phone, routed_text)
            log_request("nagonu", session_id, phone, routed_text, response)
            return response, "nagonu"

        if last_code_entry and last_code_entry != first_entry:
            if zico_agent_code_exists(last_code_entry):
                response = handle_zico(session_id, phone, routed_text)
                log_zico_request("zico", session_id, phone, routed_text, response)
                return response, "zico"
            response = handle_nagonu(session_id, phone, routed_text)
            log_request("nagonu", session_id, phone, routed_text, response)
            return response, "nagonu"

        if zico_agent_code_exists(first_entry):
            response = handle_zico(session_id, phone, routed_text)
            log_zico_request("zico", session_id, phone, routed_text, response)
            return response, "zico"
        response = handle_nagonu(session_id, phone, routed_text)
        log_request("nagonu", session_id, phone, routed_text, response)
        return response, "nagonu"

    @app.route("/healthz")
    def healthz():
        return "ok", 200

    @app.route("/", methods=["GET"])
    def index():
        return "USSD Runner is running", 200

    @app.route("/paystack/nagonu/webhook", methods=["POST"])
    def paystack_nagonu_webhook():
        raw_body = request.get_data() or b""
        signature = request.headers.get("x-paystack-signature") or request.headers.get("X-Paystack-Signature") or ""
        payload, status_code = handle_nagonu_paystack_webhook(raw_body, signature)
        return jsonify(payload), status_code

    @app.route("/paystack/zico/webhook", methods=["POST"])
    def paystack_zico_webhook():
        raw_body = request.get_data() or b""
        signature = request.headers.get("x-paystack-signature") or request.headers.get("X-Paystack-Signature") or ""
        payload, status_code = handle_zico_paystack_webhook(raw_body, signature)
        return jsonify(payload), status_code

    @app.route("/paystack/webhook", methods=["POST"])
    def paystack_shared_webhook():
        raw_body = request.get_data() or b""
        signature = request.headers.get("x-paystack-signature") or request.headers.get("X-Paystack-Signature") or ""
        try:
            event_payload = json.loads(raw_body.decode("utf-8") or "{}")
        except Exception:
            event_payload = {}
        meta = ((event_payload.get("data") or {}).get("metadata") or {}) if isinstance(event_payload, dict) else {}
        app_name = str(meta.get("app") or "").strip().lower()
        if app_name == "zico":
            payload, status_code = handle_zico_paystack_webhook(raw_body, signature)
            return jsonify(payload), status_code
        if app_name == "nagonu":
            payload, status_code = handle_nagonu_paystack_webhook(raw_body, signature)
            return jsonify(payload), status_code
        zico_payload, zico_status = handle_zico_paystack_webhook(raw_body, signature)
        if zico_status != 401 and zico_payload.get("reason") not in {"order_not_found", "reference_missing"}:
            return jsonify(zico_payload), zico_status
        nagonu_payload, nagonu_status = handle_nagonu_paystack_webhook(raw_body, signature)
        return jsonify(nagonu_payload), nagonu_status

    @app.route("/ussd", methods=["GET", "POST"])
    def ussd_shared():
        session_id, phone, text = _request_values()
        response, _ = _dispatch_shared(session_id, phone, text)
        return _respond(response)

    @app.route("/ussd/moolre", methods=["GET", "POST"])
    def ussd_moolre():
        """Moolre JSON callback for the shared Nagonu/Zico gateway."""
        correlation_id = uuid.uuid4().hex
        claim_key = ""
        try:
            normalized = moolre_adapter.parse_request(
                method=request.method,
                content_type=request.content_type or "",
                headers=request.headers,
                body=request.get_data(cache=True) or b"",
                json_payload=request.get_json(silent=True),
                form_payload=request.form,
                query_payload=request.args,
            )
            route = get_session_route(normalized.session_id, normalized.phone)
            routed_application = str((route or {}).get("application") or "") or None
            route_turn = int((route or {}).get("turn") or 0)
            state_before = f"{routed_application or 'new'}:{route_turn}"
            replay = find_replay(
                normalized.provider,
                normalized.session_id,
                normalized.phone,
                state_before,
                normalized.text,
            )
            if replay and isinstance(replay.get("response"), dict):
                return jsonify(replay["response"]), 200

            claim_key, acquired = claim(
                normalized.provider,
                normalized.session_id,
                normalized.phone,
                state_before,
                normalized.text,
            )
            if not acquired:
                return jsonify({"message": "Request is already being processed.", "reply": False}), 200

            internal, application = _dispatch_shared(
                normalized.session_id,
                normalized.phone,
                normalized.text,
                normalized.is_new_session,
                routed_application,
            )
            neutral_response = normalize_menu_response(internal)
            body, status_code, content_type = moolre_adapter.format_response(neutral_response)
            next_turn = route_turn + 1
            save_session_route(normalized.session_id, normalized.phone, application, next_turn)
            state_after = f"{application}:{next_turn}"
            complete(claim_key, application, state_after, body)
            return jsonify(body), status_code, {"Content-Type": content_type}
        except USSDAdapterError as exc:
            app.logger.warning(
                "Moolre USSD adapter rejected callback correlation_id=%s code=%s reason=%s content_type=%s",
                correlation_id,
                exc.error_code,
                str(exc),
                request.content_type or "missing",
            )
            return jsonify({"message": "Invalid USSD request.", "reply": False}), exc.status_code
        except Exception:
            if claim_key:
                try:
                    fail(claim_key)
                except Exception:
                    pass
            app.logger.error(
                "Moolre USSD callback failed correlation_id=%s",
                correlation_id,
            )
            return jsonify({"message": "Service temporarily unavailable.", "reply": False}), 500

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
