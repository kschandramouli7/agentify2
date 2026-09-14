"""main.py — the /verify dispatch server (ROADMAP P30 phase 2).

Spins up the real stdlib ThreadingHTTPServer on an ephemeral port and hits
it with real HTTP requests — BaseHTTPRequestHandler subclasses aren't
practical to unit-test any other way, and this exercises the exact code
path the Go backend's SecurityVerifierClient calls in production.
"""
import json
import sys
import threading
import urllib.request
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from security_verifier.main import _make_handler  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402

TOKEN = "test-token"


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(TOKEN))
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield port
    srv.shutdown()
    thread.join(timeout=2)


def _post(port, path, body=None, token=TOKEN):
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body or {}).encode()
    conn.request("POST", path, body=data, headers=headers)
    resp = conn.getresponse()
    return resp.status, json.loads(resp.read())


def test_health_returns_ok(server):
    with urllib.request.urlopen(f"http://127.0.0.1:{server}/health") as resp:
        assert resp.status == 200
        assert json.loads(resp.read()) == {"status": "ok"}


def test_verify_rejects_missing_token(server):
    status, body = _post(server, "/verify", {"technique": "confirm-http-reachable", "target": {"host": "x"}}, token=None)
    assert status == 401


def test_verify_rejects_wrong_token(server):
    status, body = _post(server, "/verify", {"technique": "confirm-http-reachable", "target": {"host": "x"}}, token="wrong")
    assert status == 401


def test_verify_rejects_unknown_technique(server):
    status, body = _post(server, "/verify", {"technique": "not-a-real-technique", "target": {"host": "x"}})
    assert status == 400
    assert "unknown technique" in body["error"]


def test_verify_requires_target_host(server):
    status, body = _post(server, "/verify", {"technique": "confirm-http-reachable", "target": {}})
    assert status == 400
    assert "target.host" in body["error"]


def test_verify_dispatches_to_the_technique(server):
    # Patch the TECHNIQUES dict entry itself, not the function name — main.py
    # looks up verify.TECHNIQUES.get(technique) at call time, but the dict
    # was built once at import time holding a direct reference to the
    # original function, so patching confirm_http_reachable's own name would
    # not affect the already-captured dict entry.
    from security_verifier import verify
    with patch.dict(verify.TECHNIQUES, {"confirm-http-reachable": lambda host: {"confirmed": True, "evidence": "stub"}}):
        status, body = _post(server, "/verify", {
            "engagement_id": "eng-1", "technique": "confirm-http-reachable", "target": {"host": "shop.example.com"},
        })
    assert status == 200
    assert body == {"confirmed": True, "evidence": "stub"}


def test_unknown_path_is_404(server):
    status, body = _post(server, "/not-a-route", {})
    assert status == 404
