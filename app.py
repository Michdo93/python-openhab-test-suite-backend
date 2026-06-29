"""
python-openhab-test-suite-backend
──────────────────────────────────
Stateless Flask proxy for the python-openhab-test-suite frontend.

Key facts about python-openhab-rest-client:
  1. OpenHABClient.__init__ calls __login() automatically — no public login()
  2. __login() uses print() for errors (400/401/timeout) → goes to stdout
  3. isCloud is set only when url == "https://myopenhab.org" (exact match)
  4. isLoggedIn is True only on HTTP 2xx from /rest

Strategy:
  - Wrap client construction in redirect_stdout/redirect_stderr so that
    OpenHABClient's print() calls go into a buffer, not into Render logs
  - Derive isCloud from the URL ourselves (contains "myopenhab.org")
  - Return loggedIn=False on any failed login — that is correct behaviour
"""

import io
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

# ── Flask ──────────────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _build_client_silent(body: dict) -> tuple:
    """
    Build an OpenHABClient, suppressing all print() output from __login().

    Returns (client, suppressed_output).
    The constructor calls __login() automatically — no separate login() exists.
    """
    url      = (body.get("url") or "").rstrip("/")
    username = body.get("username") or None
    password = body.get("password") or None
    token    = body.get("token")    or None

    if not url:
        raise ValueError("url is required")

    buf = io.StringIO()
    # Suppress the print() calls inside OpenHABClient.__login()
    with redirect_stdout(buf), redirect_stderr(buf):
        client = OpenHABClient(
            url=url, username=username, password=password, token=token
        )
    suppressed = buf.getvalue().strip()
    return client, suppressed


def _is_cloud(url: str) -> bool:
    """More robust cloud detection than the library's exact-string comparison."""
    return "myopenhab.org" in (url or "")


def _tester_for(name: str, client: OpenHABClient):
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
            f"Unknown tester '{name}'. Valid: ItemTester, ThingTester, "
            "RuleTester, ChannelTester, PersistenceTester, SitemapTester"
        )
    return cls(client)


def _capture_and_call(tester, method_name: str, params: list):
    """Call tester.method(*params) while capturing stdout/stderr."""
    buf = io.StringIO()
    try:
        with redirect_stdout(buf), redirect_stderr(buf):
            result = getattr(tester, method_name)(*params)
    finally:
        output = buf.getvalue().strip()
    return result, output


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "python-openhab-test-suite-backend"}), 200


@app.route("/api/connect", methods=["POST"])
def connect():
    """
    POST { url, username?, password?, token? }
    → { loggedIn: bool, isCloud: bool }
    """
    body = request.get_json(force=True, silent=True) or {}
    try:
        client, _ = _build_client_silent(body)
        return jsonify({
            "loggedIn": bool(client.isLoggedIn),
            "isCloud":  _is_cloud(body.get("url", "")),
        })
    except Exception as e:
        log.warning("connect error: %s", e)
        return jsonify({"loggedIn": False, "isCloud": False, "error": str(e)}), 200


@app.route("/api/test", methods=["POST"])
def run_test():
    """
    POST { url, username?, password?, token?,
           tester, method, params }
    → { result, output }
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

    try:
        client, _ = _build_client_silent(body)
    except Exception as e:
        return jsonify({"error": f"Connection failed: {e}"}), 400

    if not client.isLoggedIn:
        return jsonify({
            "error": "Could not connect to openHAB — check URL and credentials"
        }), 401

    try:
        tester = _tester_for(tester_name, client)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    if not hasattr(tester, method_name):
        return jsonify({
            "error": f"Method '{method_name}' not found on {tester_name}"
        }), 400

    try:
        result, output = _capture_and_call(tester, method_name, params)
        log.info("%s.%s(%s) → %s", tester_name, method_name, params, result)
        return jsonify({"result": result, "output": output})
    except TypeError as e:
        return jsonify({"error": f"Wrong arguments: {e}"}), 400
    except Exception as e:
        log.error("%s.%s failed: %s", tester_name, method_name, e, exc_info=True)
        return jsonify({"error": str(e)}), 500


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)