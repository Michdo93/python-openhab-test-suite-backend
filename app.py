"""
python-openhab-test-suite-backend
──────────────────────────────────
Stateless Flask proxy for the python-openhab-test-suite frontend.

Every request carries credentials; no session state is stored.

Endpoints
─────────
GET  /                 → wake-up / health check
POST /api/connect      → verify credentials → { loggedIn, isCloud }
POST /api/test         → run a tester method → { result, output }
"""

import io
import sys
import json
import logging
import os

from contextlib import redirect_stdout, redirect_stderr
from flask import Flask, request, jsonify
from flask_cors import CORS

from openhab import OpenHABClient
from openhab_test_suite import (
    ItemTester,
    ThingTester,
    RuleTester,
    ChannelTester,
    PersistenceTester,
    SitemapTester,
)

# ── Flask setup ────────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ── Helpers ────────────────────────────────────────────────────────────────────

def _client_from_body(body: dict) -> OpenHABClient:
    """Build an OpenHABClient from the request body."""
    url      = body.get("url", "").rstrip("/")
    username = body.get("username") or None
    password = body.get("password") or None
    token    = body.get("token")    or None
    if not url:
        raise ValueError("url is required")
    return OpenHABClient(url=url, username=username, password=password, token=token)


def _tester_for(name: str, client: OpenHABClient):
    """Instantiate the requested tester class."""
    mapping = {
        "ItemTester":        ItemTester,
        "ThingTester":       ThingTester,
        "RuleTester":        RuleTester,
        "ChannelTester":     ChannelTester,
        "PersistenceTester": PersistenceTester,
        "SitemapTester":     SitemapTester,
    }
    cls = mapping.get(name)
    if cls is None:
        raise ValueError(f"Unknown tester: '{name}'")
    return cls(client)


def _capture_and_call(tester, method_name: str, params: list):
    """
    Call ``tester.<method_name>(*params)`` while capturing all stdout/stderr
    output (the tester classes print diagnostic messages).

    Returns (result, captured_output).
    """
    buf = io.StringIO()
    try:
        with redirect_stdout(buf), redirect_stderr(buf):
            method = getattr(tester, method_name)
            result = method(*params)
    finally:
        output = buf.getvalue().strip()
    return result, output


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def health():
    """Wake-up / health check used by the frontend."""
    return jsonify({"status": "ok", "service": "python-openhab-test-suite-backend"}), 200


@app.route("/api/connect", methods=["POST"])
def connect():
    """
    Verify that the supplied credentials can reach the openHAB server.

    Request body (JSON):
        { url, username?, password?, token? }

    Response:
        { loggedIn: bool, isCloud: bool }
    """
    body = request.get_json(force=True, silent=True) or {}
    try:
        client = _client_from_body(body)
        client.login()
        return jsonify({"loggedIn": client.isLoggedIn, "isCloud": client.isCloud})
    except Exception as e:
        log.warning("connect failed: %s", e)
        return jsonify({"loggedIn": False, "isCloud": False, "error": str(e)}), 200


@app.route("/api/test", methods=["POST"])
def run_test():
    """
    Run a single tester method.

    Request body (JSON):
        {
            url:      string,
            username: string | null,
            password: string | null,
            token:    string | null,
            tester:   "ItemTester" | "ThingTester" | "RuleTester"
                    | "ChannelTester" | "PersistenceTester" | "SitemapTester",
            method:   string,       // e.g. "testSwitch"
            params:   array         // e.g. ["testSwitch","ON","ON",10]
        }

    Response (success):
        { result: bool | any, output: string }

    Response (error):
        HTTP 400 / 500 with { error: string }
    """
    body = request.get_json(force=True, silent=True) or {}

    tester_name = body.get("tester", "")
    method_name = body.get("method", "")
    params      = body.get("params", [])

    # ── Validate input ────────────────────────────────────────────────────────
    if not tester_name:
        return jsonify({"error": "tester is required"}), 400
    if not method_name:
        return jsonify({"error": "method is required"}), 400
    if not isinstance(params, list):
        return jsonify({"error": "params must be a JSON array"}), 400

    # ── Build client ──────────────────────────────────────────────────────────
    try:
        client = _client_from_body(body)
        client.login()
        if not client.isLoggedIn:
            return jsonify({"error": "Could not connect to openHAB — check credentials"}), 401
    except Exception as e:
        log.warning("client creation failed: %s", e)
        return jsonify({"error": f"Connection failed: {e}"}), 400

    # ── Instantiate tester ────────────────────────────────────────────────────
    try:
        tester = _tester_for(tester_name, client)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    # ── Validate method ───────────────────────────────────────────────────────
    if not hasattr(tester, method_name):
        return jsonify({"error": f"Method '{method_name}' not found on {tester_name}"}), 400

    # ── Execute ───────────────────────────────────────────────────────────────
    try:
        result, output = _capture_and_call(tester, method_name, params)
        log.info("%s.%s(%s) → %s", tester_name, method_name, params, result)
        return jsonify({"result": result, "output": output})
    except TypeError as e:
        # Wrong number of arguments
        log.warning("type error calling %s.%s: %s", tester_name, method_name, e)
        return jsonify({"error": f"Wrong arguments: {e}"}), 400
    except Exception as e:
        log.error("error in %s.%s: %s", tester_name, method_name, e, exc_info=True)
        return jsonify({"error": str(e)}), 500


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
