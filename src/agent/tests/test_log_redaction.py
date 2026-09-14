"""Tests for log_redaction.py — mirrors
src/backend/internal/governance/redact_test.go's widened-PII coverage
(ADR 0007, 2026-09-14 amendment) so both the Go and Python scrubbers are
held to the same guarantee.
"""

from k8fy.log_redaction import redact_log_text


def test_redact_scrubs_known_secret_shapes():
    text = (
        "level=error connecting db\n"
        "postgres://app:hunter2@db.internal:5432/payments failed\n"
        "Authorization: Bearer abcdef0123456789ABCDEF\n"
        "AWS_KEY=AKIAIOSFODNN7EXAMPLE password=supersecret123\n"
        "contact ops@example.com token: eyJhbGciOi.JzdWIiOiI.SflKxwRJSM\n"
        "digest 0123456789abcdef0123456789abcdef"
    )
    out = redact_log_text(text)
    for leaked in [
        "hunter2", "supersecret123", "abcdef0123456789ABCDEF",
        "AKIAIOSFODNN7EXAMPLE", "ops@example.com",
        "eyJhbGciOi.JzdWIiOiI.SflKxwRJSM", "0123456789abcdef0123456789abcdef",
    ]:
        assert leaked not in out, f"leaked {leaked!r} in {out!r}"
    assert "connecting db" in out and "failed" in out


def test_redact_widened_pii():
    # Slack/Stripe test values are split across concatenated literals so the
    # realistic-enough fake shape doesn't appear contiguously in this file —
    # GitHub's own secret-scanning push protection otherwise flags it despite
    # being a synthetic test fixture, not a real credential.
    slack_token = "xoxb-123456789012-" + "abcdefghijklmnopqrstuvwx"
    stripe_key = "sk_live_" + "ABCDEFGHIJKLMNOPQRSTUVWX"
    text = (
        "call the customer at +1 415-555-0132 or (415) 555-0199\n"
        "card on file: 4111 1111 1111 1111\n"
        "internal host 10.0.4.17, external peer 2001:0db8:85a3:0000:0000:8a2e:0370:7334\n"
        "compressed form 2001:db8::1 and loopback ::1\n"
        "leaked keys: ghp_abcdefghijklmnopqrstuvwxyz0123456789 github_pat_ABCDEFGHIJKLMNOPQRSTUVWX\n"
        f"{slack_token}\n"
        "AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ0123456\n"
        f"{stripe_key}\n"
        "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
    )
    out = redact_log_text(text)
    for leaked in [
        "415-555-0132", "415) 555-0199",
        "4111 1111 1111 1111",
        "10.0.4.17",
        "2001:0db8:85a3:0000:0000:8a2e:0370:7334", "2001:db8::1", "::1",
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "github_pat_ABCDEFGHIJKLMNOPQRSTUVWX",
        slack_token,
        "AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ0123456",
        stripe_key,
        "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345",
    ]:
        assert leaked not in out, f"leaked {leaked!r} in {out!r}"


def test_redact_credit_card_requires_luhn():
    valid = "4111111111111111"  # well-known Luhn-valid test Visa number
    out = redact_log_text(f"card: {valid}")
    assert valid not in out

    invalid = "4111111111111112"  # same shape, fails Luhn
    out = redact_log_text(f"trace id: {invalid}")
    assert invalid in out, "a Luhn-invalid 16-digit number must survive (likely a trace/numeric ID)"


def test_redact_ipv6_does_not_catch_clock_strings():
    out = redact_log_text("request completed at 14:23:05 in 00:00:12")
    assert "14:23:05" in out and "00:00:12" in out


def test_redact_truncates_long_text():
    out = redact_log_text("x" * 20000)
    assert len(out) <= 16384 + len("\n…[truncated]")
