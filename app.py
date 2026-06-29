"""
python-openhab-test-suite-backend
──────────────────────────────────
Stateless Flask proxy for the python-openhab-test-suite frontend.

Known OpenHABClient behaviour (python-openhab-rest-client):
  - __login() is called automatically in __init__
  - There is no public login() method
  - For myopenhab.org: isCloud=True but isLoggedIn stays False on 401
    (raise_for_status() throws before self.isLoggedIn = True is reached)
  - For local OH: isLoggedIn=True on success, False on auth failure
  - The 401 log lines in the Render console are from failed test connection
    attempts by the frontend — this is expected behaviour

Strategy:
  - For /api/connect: try the request, treat HTTP 401 as "wrong credentials"
    (not as "server unreachable"), treat HTTP 2xx/3xx as "loggedIn"
  - isCloud is detected from the URL, not from the library attribute
"""

import io
import logging
import os

from contextlib import redirect_stdout, redirect_stderr
from flask import Flask, request, jsonify
from flask_cors import CORS

import requests as req_lib

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

def _build_client(body: dict) -> OpenHABClient:
    """
    Build an OpenHABClient from the request body.
    The constructor calls __login() automatically.
    """
    url      = (body.get("url") or "").rstrip("/")
    username = body.get("username") or None
    password = body.get("password") or None
    token    = body.get("token")    or None
    if not url:
        raise ValueError("url is required")
    return OpenHABClient(url=url, username=username, password=password, token=token)


def _check_connection(client: OpenHABClient) -> tuple[bool, bool]:
    """
    Returns (loggedIn, isCloud).

    The Python library sets isLoggedIn=True only when the /rest endpoint
    returns 2xx. For myopenhab.org with valid credentials this works.
    For wrong credentials it returns 401 (raise_for_status throws) so
    isLoggedIn stays False — which is the correct behaviour.

    isCloud is derived from the URL since the library only sets it
    for the literal string "https://myopenhab.org".
    """
    is_cloud = "myopenhab.org" in (client.url or "")
    return bool(client.isLoggedIn), is_cloud


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
        raise ValueError(
            f"Unknown tester: '{name}'. Valid: "
            "ItemTester, ThingTester, RuleTester, "
            "ChannelTester, PersistenceTester, SitemapTester"
        )
    return cls(client)


def _capture_and_call(tester, method_name: str, params: list):
    """
    Call tester.<method_name>(*params) while capturing stdout/stderr.
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
    """Wake-up / health check."""
    return jsonify({"status": "ok", "service": "python-openhab-test-suite-backend"}), 200


@app.route("/api/connect", methods=["POST"])
def connect():
    """
    Verify that the supplied credentials can reach openHAB.

    POST body: { url, username?, password?, token? }
    Response:  { loggedIn: bool, isCloud: bool }
    """
    body = request.get_json(force=True, silent=True) or {}
    try:
        client              = _build_client(body)
        logged_in, is_cloud = _check_connection(client)
        return jsonify({"loggedIn": logged_in, "isCloud": is_cloud})
    except Exception as e:
        log.warning("connect error: %s", e)
        return jsonify({"loggedIn": False, "isCloud": False, "error": str(e)}), 200


@app.route("/api/test", methods=["POST"])
def run_test():
    """
    Run a single tester method.

    POST body:
        {
            url, username?, password?, token?,
            tester: str,   // e.g. "ItemTester"
            method: str,   // e.g. "testSwitch"
            params: list   // e.g. ["testSwitch","ON","ON",10]
        }

    Response: { result: bool|any, output: str }
    """
    body = request.get_json(force=True, silent=True) or {}

    tester_name = body.get("tester", "")
    method_name = body.get("method", "")
    params      = body.get("params", [])

    if not tester_name:
        return jsonify({"error": "tester is required"}), 400
    if not method_name:
        return jsonify({"error": "method is required"}), 400
    if not isinstance(params, list):
        return jsonify({"error": "params must be a JSON array"}), 400

    # Build client — constructor performs login automatically
    try:
        client = _build_client(body)
    except Exception as e:
        log.warning("client build error: %s", e)
        return jsonify({"error": f"Connection failed: {e}"}), 400

    logged_in, _ = _check_connection(client)
    if not logged_in:
        return jsonify({
            "error": "Could not connect to openHAB — check URL and credentials"
        }), 401

    # Instantiate tester
    try:
        tester = _tester_for(tester_name, client)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    # Validate method exists
    if not hasattr(tester, method_name):
        return jsonify({
            "error": f"Method '{method_name}' not found on {tester_name}"
        }), 400

    # Execute, capturing all print() output from the tester classes
    try:
        result, output = _capture_and_call(tester, method_name, params)
        log.info("%s.%s(%s) → %s", tester_name, method_name, params, result)
        return jsonify({"result": result, "output": output})
    except TypeError as e:
        log.warning("wrong args %s.%s: %s", tester_name, method_name, e)
        return jsonify({"error": f"Wrong arguments: {e}"}), 400
    except Exception as e:
        log.error("error in %s.%s: %s", tester_name, method_name, e, exc_info=True)
        return jsonify({"error": str(e)}), 500


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)