"""
python-openhab-test-suite-backend
──────────────────────────────────
Stateless Flask proxy for the python-openhab-test-suite frontend.

IMPORTANT — why this is stateless again:
  A global `_client` variable only lives inside ONE gunicorn worker process.
  With `--workers 2` (or more), /api/connect and /api/test land on different
  worker processes at random, so the "global" client is None in the other
  workers → 401 "Not connected". This is NOT a bug in openhab-test-suite or
  python-openhab-rest-client — both correctly accept and pass through the
  client object. It is purely a backend-architecture issue.

  Fix: go back to building a fresh OpenHABClient on every request, using
  UUID(client).getUUID() as the connectivity check (the pattern that works,
  learned from python-openhab-rest-client-test-app) — but apply it per
  request instead of storing it globally.
"""

import io
import logging
import os

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
CORS(app)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _is_cloud(url: str) -> bool:
    return "myopenhab.org" in (url or "")


def _build_client(body: dict) -> OpenHABClient:
    """Build an OpenHABClient. Suppresses __login()'s print() output."""
    url      = (body.get("url") or "").rstrip("/")
    username = body.get("username") or None
    password = body.get("password") or None
    token    = body.get("token")    or None
    if not url:
        raise ValueError("url is required")

    buf = io.StringIO()
    with redirect_stdout(buf), redirect_stderr(buf):
        client = OpenHABClient(url=url, username=username, password=password, token=token)
    return client


def _verify_connection(client: OpenHABClient):
    """
    Real connectivity check using UUID(client).getUUID() —
    the pattern that actually works, instead of relying on
    the unreliable client.isLoggedIn flag.

    Returns the uuid string on success, raises on failure.
    """
    buf = io.StringIO()
    with redirect_stdout(buf), redirect_stderr(buf):
        uuid = UUID(client).getUUID()
    if isinstance(uuid, dict) and "error" in uuid:
        raise ConnectionError(uuid["error"])
    return uuid


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
    POST { url, username?, password?, token? }
    → { loggedIn: bool, isCloud: bool, uuid?: str }
    """
    body = request.get_json(force=True, silent=True) or {}
    try:
        client = _build_client(body)
        uuid   = _verify_connection(client)
        return jsonify({
            "loggedIn": True,
            "isCloud":  _is_cloud(body.get("url", "")),
            "uuid":     str(uuid),
        })
    except Exception as e:
        log.warning("connect failed: %s", e)
        return jsonify({"loggedIn": False, "error": str(e)}), 200


@app.route("/api/test", methods=["POST"])
def run_test():
    """
    POST { url, username?, password?, token?, tester, method, params }
    → { result, output }

    Credentials are sent on every request (stateless — works regardless
    of which gunicorn worker handles it).
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
        client = _build_client(body)
        _verify_connection(client)
    except Exception as e:
        return jsonify({
            "error": f"Could not connect to openHAB — {e}"
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