"""verify.py — active-verification techniques (ROADMAP P30 phase 2, ADR 0033).

Pure functions, no server framework — same "a single/few internal routes
does not justify pulling in a framework" reasoning discovery/health.py
already states, applied here to the outbound check logic too: each
technique takes plain arguments and returns a plain dict, so it's testable
without spinning up the HTTP server in main.py.

Only one technique exists today: `confirm_http_reachable`, dispatched for
the `ingress-missing-tls` finding. Adding a technique means adding a
function here and one entry in main.py's TECHNIQUES dispatch table — never
a new endpoint.
"""

import urllib.error
import urllib.request
from typing import Optional, TypedDict


class VerifyResult(TypedDict):
    confirmed: Optional[bool]
    evidence: str


def confirm_http_reachable(host: str, timeout: float = 5.0) -> VerifyResult:
    """Confirm `host` actually answers a plaintext HTTP GET — read-only,
    non-destructive, no auth attempted. This is Phase 2's most conservative
    possible check: it only ever answers "confirmed" or "inconclusive," never
    "refuted."

    `confirmed=True` on ANY HTTP response, including a redirect or an error
    status — the point is that a plaintext listener answered at all, not
    what it said. `confirmed=None` (never False) on a connection-level
    failure: a timeout, refused connection, or DNS failure could mean "this
    genuinely doesn't listen on plaintext HTTP" just as easily as "transient
    in-cluster network hiccup," and this technique cannot tell those apart
    from one failed attempt — the same "an honest unknown beats a wrong
    guess" principle ADR 0031's outcome classifier already established for
    log-based inference. A future, more thorough technique could earn a real
    `confirmed=False`; this one does not claim more certainty than it has.
    """
    url = f"http://{host}/"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 — deliberate plaintext HTTP check
            return {"confirmed": True, "evidence": f"HTTP {resp.status} from {url}"}
    except urllib.error.HTTPError as e:
        # A response was received — even an error status confirms a
        # plaintext listener exists and answered.
        return {"confirmed": True, "evidence": f"HTTP {e.code} from {url}"}
    except Exception as e:  # noqa: BLE001 — any connection-level failure is inconclusive, not "confirmed False"
        return {"confirmed": None, "evidence": f"{url} did not respond ({e.__class__.__name__}: {e})"}


# check_id -> technique name -> function, so main.py's dispatch is one lookup,
# and so the mapping lives beside the functions it maps to (ADR 0033's
# checkIDToTechnique on the Go side names the SAME technique strings —
# keep them in sync if either side ever adds one).
TECHNIQUES = {
    "confirm-http-reachable": confirm_http_reachable,
}
