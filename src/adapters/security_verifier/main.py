"""main.py — agentify-security-verifier (ROADMAP P30 phases 2-4, ADR 0033).

The first network-isolated executor in this repo (ROADMAP P14a's remediation
executor was agreed but never split out of agentify-agent — this establishes
the pattern, not copies it). Dispatched ONLY by the Go backend, after a
human has approved a security_engagements row; agentify-agent has no network
path to this service at all, not even a deterministic intent hop the way
remediation's own dispatch still goes through the agent process. A prompt-
injected "please verify this endpoint is exploitable" must have nowhere to
go — this service is simply unreachable from anywhere an LLM call runs.

A stdlib HTTP server is used deliberately, same reasoning
discovery/health.py already states for its one route: two internal routes
do not justify pulling an ASGI framework into this image. No K8s API calls
either — phase 2's one technique (confirm_http_reachable) is a plain
outbound HTTP GET, so this service's RBAC footprint is genuinely empty.
"""

import json
import logging
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import verify

logger = logging.getLogger("agentify.security-verifier")


def _configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter('{"level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}')
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)


def _make_handler(token: str) -> type:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:  # noqa: D401 — quiet default stderr logging
            return

        def _write_json(self, status: int, body: dict) -> None:
            data = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self) -> bool:
            # Defense in depth, not the primary boundary — this Service is
            # ClusterIP-only with no Ingress, so the network topology is the
            # real isolation; an empty token means "not configured," which
            # fails closed (never authorized) rather than open, same posture
            # every other auth check in this codebase takes for an unset
            # production credential.
            if not token:
                return False
            return self.headers.get("Authorization", "") == f"Bearer {token}"

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                self._write_json(200, {"status": "ok"})
                return
            self._write_json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/verify":
                self._write_json(404, {"error": "not found"})
                return
            if not self._authorized():
                self._write_json(401, {"error": "unauthorized"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                self._write_json(400, {"error": "invalid JSON"})
                return

            technique = payload.get("technique", "")
            target = payload.get("target") or {}
            host = target.get("host", "")
            engagement_id = payload.get("engagement_id", "")

            fn = verify.TECHNIQUES.get(technique)
            if fn is None:
                self._write_json(400, {"error": f"unknown technique {technique!r}"})
                return
            if not host:
                self._write_json(400, {"error": "target.host is required"})
                return

            logger.info("dispatching technique=%s engagement_id=%s host=%s", technique, engagement_id, host)
            result = fn(host)
            self._write_json(200, result)

    return Handler


def main() -> None:
    _configure_logging()
    port = int(os.environ.get("PORT", "8400"))
    token = os.environ.get("SECURITY_VERIFIER_TOKEN", "")
    if not token:
        logger.warning("SECURITY_VERIFIER_TOKEN not set — every /verify call will be rejected (fails closed)")
    server = ThreadingHTTPServer(("0.0.0.0", port), _make_handler(token))
    logger.info("agentify-security-verifier listening on port %d", port)
    server.serve_forever()


if __name__ == "__main__":
    main()
