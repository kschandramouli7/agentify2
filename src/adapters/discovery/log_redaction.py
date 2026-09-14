"""log_redaction.py — best-effort secret scrubbing for raw log text.

Copied verbatim from src/agent/k8fy/log_redaction.py (ADR 0022 Decision #6:
"redaction runs at the collector, before anything leaves the cluster" — this
package deliberately does not import from src/agent, which carries a much
heavier dependency set; see the "Layout" section of the agentify-discovery
plan). Keep this in sync by hand if the original ever changes shape.
"""

import re

_MAX_LOG_CHARS = 16384

_LOG_SCRUBBERS = [
    (re.compile(r"(?i)(authorization\s*[:=]\s*)(bearer\s+)?[A-Za-z0-9._\-]{12,}"), r"\1\2***"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "***"),
    (re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"), "***"),
    (re.compile(r"(?i)(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|client[_-]?secret)(\"?\s*[:=]\s*\"?)[^\s\"',;}]+"), r"\1\2***"),
    (re.compile(r"(://[^:/\s]+:)[^@/\s]+(@)"), r"\1***\2"),
    (re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"), "***"),
    (re.compile(r"\b[A-Fa-f0-9]{32,}\b"), "***"),
    (re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"), "***"),
    # --- ADR 0007, 2026-09-14 amendment: PII coverage widens past email-only.
    # Mirrors redact.go's additions exactly — see that file's comments for
    # why each pattern is shaped the way it is (phone requires a separator to
    # avoid colliding with concatenated-digit IDs; IPv6 uses the standard
    # multi-alternative form, not a naive repeated group, because the naive
    # form mis-splits "::" and fails to match compressed addresses).
    (re.compile(r"\b(?:\+?\d{1,2}[-.\s])?\(?\d{3}\)?[-.\s]\d{3}[-.\s]?\d{4}\b"), "***"),  # phone
    (re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b"), "***"),  # IPv4
    (re.compile(
        r"\b(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}|(?:[0-9A-Fa-f]{1,4}:){1,7}:|"
        r"(?:[0-9A-Fa-f]{1,4}:){1,6}:[0-9A-Fa-f]{1,4}|(?:[0-9A-Fa-f]{1,4}:){1,5}(?::[0-9A-Fa-f]{1,4}){1,2}|"
        r"(?:[0-9A-Fa-f]{1,4}:){1,4}(?::[0-9A-Fa-f]{1,4}){1,3}|(?:[0-9A-Fa-f]{1,4}:){1,3}(?::[0-9A-Fa-f]{1,4}){1,4}|"
        r"(?:[0-9A-Fa-f]{1,4}:){1,2}(?::[0-9A-Fa-f]{1,4}){1,5}|[0-9A-Fa-f]{1,4}:(?::[0-9A-Fa-f]{1,4}){1,6}|"
        r":(?:(?::[0-9A-Fa-f]{1,4}){1,7}|:)\b",
    ), "***"),  # IPv6
    (re.compile(r"\bghp_[A-Za-z0-9]{36}\b"), "***"),               # GitHub classic PAT
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), "***"),      # GitHub fine-grained PAT
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "***"),      # Slack token
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), "***"),            # Google API key
    (re.compile(r"\bsk_(?:live|test)_[A-Za-z0-9]{16,}\b"), "***"), # Stripe key
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), "***"),               # generic sk- prefix
]

# Credit-card candidates (13-19 digits, optionally grouped with spaces/dashes)
# are matched separately and masked only when they also pass the Luhn check
# digit — an ordinary numeric ID (trace ID, pod hash fragment) is exactly
# this shape without being Luhn-valid, so digit-shape alone would be far too
# noisy. Mirrors redact.go's redactCreditCards.
_CREDIT_CARD_CANDIDATE = re.compile(r"\b\d(?:[ -]?\d){12,18}\b")


def _luhn_valid(digits: str) -> bool:
    total = 0
    alt = False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def _redact_credit_cards(s: str) -> str:
    def _mask(m: "re.Match[str]") -> str:
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn_valid(digits):
            return "***"
        return m.group(0)

    return _CREDIT_CARD_CANDIDATE.sub(_mask, s)


def redact_log_text(s: str) -> str:
    s = _redact_credit_cards(s)
    for pattern, replacement in _LOG_SCRUBBERS:
        s = pattern.sub(replacement, s)
    if len(s) > _MAX_LOG_CHARS:
        s = s[:_MAX_LOG_CHARS] + "\n…[truncated]"
    return s
