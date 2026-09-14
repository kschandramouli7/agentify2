"""verify.py — pure active-verification technique tests (ROADMAP P30 phase
2). No server involved; confirm_http_reachable is tested by mocking
urllib.request.urlopen, same style as test_k8s_client.py mocks the K8s API
in the sibling discovery package.
"""
import sys
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from security_verifier import verify  # noqa: E402


def test_confirm_http_reachable_true_on_200():
    resp = MagicMock()
    resp.status = 200
    resp.__enter__.return_value = resp
    with patch("urllib.request.urlopen", return_value=resp):
        result = verify.confirm_http_reachable("shop.example.com")
    assert result["confirmed"] is True
    assert "200" in result["evidence"]


def test_confirm_http_reachable_true_on_http_error_status():
    """Even an error status (404, 500) means a plaintext listener answered
    — the technique confirms reachability, not correctness."""
    err = urllib.error.HTTPError("http://shop.example.com/", 404, "Not Found", {}, None)
    with patch("urllib.request.urlopen", side_effect=err):
        result = verify.confirm_http_reachable("shop.example.com")
    assert result["confirmed"] is True
    assert "404" in result["evidence"]


def test_confirm_http_reachable_inconclusive_on_connection_error():
    """A connection-level failure is inconclusive (None), never a confident
    'refuted' — this technique cannot tell 'genuinely not listening' apart
    from 'transient network hiccup' from one attempt."""
    with patch("urllib.request.urlopen", side_effect=ConnectionRefusedError("refused")):
        result = verify.confirm_http_reachable("shop.example.com")
    assert result["confirmed"] is None
    assert "shop.example.com" in result["evidence"]


def test_confirm_http_reachable_inconclusive_on_timeout():
    with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
        result = verify.confirm_http_reachable("shop.example.com")
    assert result["confirmed"] is None


def test_techniques_table_has_confirm_http_reachable():
    assert verify.TECHNIQUES["confirm-http-reachable"] is verify.confirm_http_reachable
