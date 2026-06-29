"""
python-openhab-test-suite-backend
──────────────────────────────────
Stateless Flask proxy for the python-openhab-test-suite frontend.

Login pattern learned from python-openhab-rest-client-test-app:
  1. OpenHABClient(url, username, password, token)  — no isLoggedIn check
  2. UUID(client).getUUID()                         — real API call as verification
  3. If getUUID() raises → not connected
  4. If getUUID() returns a string → connected
  isLoggedIn / isCloud are NOT used — they are unreliable.
"""

import io
import logging
import os
import traceback

from contextlib import redirect_stdout, redirect_stderr
from flask import Flask, request, jsonify
from flask_cors import CORS

from openhab import OpenHABClient, UUID
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
CORS(app, origins=["https://michdo93.github.io", "http://localhost",
                   "http://127.0.0.1", "null", "*"])
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ── Global client (stateful, same as working test app) ────────────────────────
_client: OpenHABClient | None = None


def _get_client() -> OpenHABClient:
    if _client is None:
        raise RuntimeError("Not connected. Call /api/connect first.")
    return _client


# ── Helpers ────────────────────────────────────────────────────────────────────

def _is_cloud(url: str) -> bool:
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
    """Call tester.method(*params) while capturing all print() output."""
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
    Pattern from python-openhab-rest-client-test-app:
      1. Create OpenHABClient
      2. Call UUID(client).getUUID() as real connectivity verification
      3. Success → return { loggedIn: true, isCloud, uuid }
      4. Exception → return { loggedIn: false, error }

    We do NOT check client.isLoggedIn — it is set by __login() which
    prints 400/401 errors to stdout and is unreliable for cloud connections.
    """
    global _client

    body     = request.get_json(force=True, silent=True) or {}
    url      = (body.get("url") or "").rstrip("/")
    username = body.get("username") or None
    password = body.get("password") or None
    token    = body.get("token")    or None

    if not url:
        return jsonify({"loggedIn": False, "error": "url is required"}), 200

    try:
        # Suppress the print() calls from OpenHABClient.__login()
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(buf):
            _client = OpenHABClient(
                url=url, username=username, password=password, token=token
            )

        # Real connectivity check — same as working test app
        uuid = UUID(_client).getUUID()

        # getUUID returns a string on success, a dict with "error" on failure
        if isinstance(uuid, dict) and "error" in uuid:
            _client = None
            return jsonify({
                "loggedIn": False,
                "error":    uuid["error"],
            }), 200

        return jsonify({
            "loggedIn": True,
            "isCloud":  _is_cloud(url),
            "uuid":     str(uuid),
        })

    except Exception as e:
        _client = None
        log.warning("connect failed: %s", e)
        return jsonify({"loggedIn": False, "error": str(e)}), 200


@app.route("/api/test", methods=["POST"])
def run_test():
    """
    POST { tester, method, params }
    → { result, output }

    Credentials are NOT sent per-request — the global _client is reused.
    Frontend must call /api/connect first.
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
        client = _get_client()
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 401

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
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)