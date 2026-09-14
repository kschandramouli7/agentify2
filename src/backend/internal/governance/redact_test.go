package governance

import (
	"log/slog"
	"strings"
	"testing"
)

func sampleFetch() map[string]interface{} {
	return map[string]interface{}{
		"k8fy.live-state.prod": []map[string]interface{}{
			{
				"id":              "evt-1",
				"event_id":        "evt-1",
				"entity_key":      "payment-svc-abc",
				"event_namespace": "k8fy.live-state",
				"type":            "pod_modified",
				"timestamp":       "2026-06-01T00:00:00Z",
				"source":          "kubernetes-api",
				"payload": map[string]interface{}{
					"pod_id":    "payment-svc-abc",
					"namespace": "prod",
					"phase":     "Running",
					"ready":     false,
					"restarts":  float64(7),
					"reason":    "CrashLoopBackOff",
					// sensitive / non-allowlisted fields that must be dropped:
					"annotations": map[string]interface{}{"vault-token": "s.SECRET"},
					"env":         []string{"DB_PASSWORD=hunter2"},
				},
			},
		},
	}
}

func TestRedactDropsNonAllowlisted(t *testing.T) {
	r := NewRedactor(true, false, slog.Default())
	out := r.RedactFetch(sampleFetch())

	rows := out["k8fy.live-state.prod"].([]map[string]interface{})
	rec := rows[0]

	// dropped top-level keys
	for _, k := range []string{"id", "event_id"} {
		if _, ok := rec[k]; ok {
			t.Errorf("expected top-level %q to be dropped", k)
		}
	}
	// kept top-level keys
	for _, k := range []string{"entity_key", "event_namespace", "type", "payload"} {
		if _, ok := rec[k]; !ok {
			t.Errorf("expected top-level %q to be kept", k)
		}
	}

	payload := rec["payload"].(map[string]interface{})
	// sensitive fields dropped
	for _, k := range []string{"annotations", "env"} {
		if _, ok := payload[k]; ok {
			t.Errorf("expected sensitive payload field %q to be dropped", k)
		}
	}
	// reasoning fields kept
	for _, k := range []string{"pod_id", "phase", "ready", "restarts", "reason"} {
		if _, ok := payload[k]; !ok {
			t.Errorf("expected payload field %q to be kept", k)
		}
	}
}

func TestPseudonymizeIdentifiers(t *testing.T) {
	r := NewRedactor(true, true, slog.Default())
	out := r.RedactFetch(sampleFetch())
	rec := out["k8fy.live-state.prod"].([]map[string]interface{})[0]
	payload := rec["payload"].(map[string]interface{})

	if got := payload["pod_id"].(string); got == "payment-svc-abc" {
		t.Error("pod_id should be pseudonymized when enabled")
	} else if len(got) < 4 || got[:3] != "id_" {
		t.Errorf("pseudonym should be id_<hash>, got %q", got)
	}
	// non-identifier fields must NOT be altered
	if payload["phase"].(string) != "Running" {
		t.Error("non-identifier field phase must not be pseudonymized")
	}
}

func TestRedactDisabledIsPassthrough(t *testing.T) {
	r := NewRedactor(false, false, slog.Default())
	in := sampleFetch()
	out := r.RedactFetch(in)
	rec := out["k8fy.live-state.prod"].([]map[string]interface{})[0]
	if _, ok := rec["payload"].(map[string]interface{})["env"]; !ok {
		t.Error("disabled redactor should pass data through unchanged")
	}
}

func TestRedactTextScrubsSecrets(t *testing.T) {
	r := NewRedactor(true, false, slog.Default())
	in := `level=error connecting db
postgres://app:hunter2@db.internal:5432/payments failed
Authorization: Bearer abcdef0123456789ABCDEF
AWS_KEY=AKIAIOSFODNN7EXAMPLE password=supersecret123
contact ops@example.com token: eyJhbGciOi.JzdWIiOiI.SflKxwRJSM
digest 0123456789abcdef0123456789abcdef`

	out := r.RedactText(in)

	for _, s := range []string{"hunter2", "supersecret123", "abcdef0123456789ABCDEF", "AKIAIOSFODNN7EXAMPLE", "ops@example.com", "eyJhbGciOi.JzdWIiOiI.SflKxwRJSM", "0123456789abcdef0123456789abcdef"} {
		if strings.Contains(out, s) {
			t.Errorf("RedactText leaked %q\n--- output ---\n%s", s, out)
		}
	}
	// Non-secret context must survive so the log stays useful.
	if !strings.Contains(out, "connecting db") || !strings.Contains(out, "failed") {
		t.Errorf("RedactText over-scrubbed useful context:\n%s", out)
	}
}

func TestRedactTextTruncates(t *testing.T) {
	r := NewRedactor(true, false, slog.Default())
	out := r.RedactText(strings.Repeat("x", maxLogChars+500))
	if len(out) > maxLogChars+len("\n…[truncated]") {
		t.Errorf("RedactText did not truncate: len=%d", len(out))
	}
}

func TestRedactTextDisabledIsPassthrough(t *testing.T) {
	r := NewRedactor(false, false, slog.Default())
	in := "password=hunter2"
	if got := r.RedactText(in); got != in {
		t.Errorf("disabled RedactText should pass through, got %q", got)
	}
}

// TestRedactTextWidenedPII is ADR 0007's 2026-09-14 amendment's Decision #2:
// PII coverage widens past email-only.
func TestRedactTextWidenedPII(t *testing.T) {
	r := NewRedactor(true, false, slog.Default())
	// Slack/Stripe test values are split across concatenated literals so the
	// realistic-enough fake shape doesn't appear contiguously in this file —
	// GitHub's own secret-scanning push protection otherwise flags it despite
	// being a synthetic test fixture, not a real credential.
	slackToken := "xoxb-123456789012-" + "abcdefghijklmnopqrstuvwx"
	stripeKey := "sk_live_" + "ABCDEFGHIJKLMNOPQRSTUVWX"
	in := "call the customer at +1 415-555-0132 or (415) 555-0199\n" +
		"card on file: 4111 1111 1111 1111\n" +
		"internal host 10.0.4.17, external peer 2001:0db8:85a3:0000:0000:8a2e:0370:7334\n" +
		"compressed form 2001:db8::1 and loopback ::1\n" +
		"leaked keys: ghp_abcdefghijklmnopqrstuvwxyz0123456789 github_pat_ABCDEFGHIJKLMNOPQRSTUVWX\n" +
		slackToken + "\n" +
		"AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ0123456\n" +
		stripeKey + "\n" +
		"sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"

	out := r.RedactText(in)

	leaks := []string{
		"415-555-0132", "415) 555-0199",
		"4111 1111 1111 1111",
		"10.0.4.17",
		"2001:0db8:85a3:0000:0000:8a2e:0370:7334", "2001:db8::1", "::1",
		"ghp_abcdefghijklmnopqrstuvwxyz0123456789",
		"github_pat_ABCDEFGHIJKLMNOPQRSTUVWX",
		slackToken,
		"AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ0123456",
		stripeKey,
		"sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345",
	}
	for _, s := range leaks {
		if strings.Contains(out, s) {
			t.Errorf("RedactText leaked %q\n--- output ---\n%s", s, out)
		}
	}
}

// TestRedactTextCreditCardRequiresLuhn proves the credit-card scrubber only
// masks a candidate that passes the Luhn checksum — per the ADR's own
// "Luhn-checked, not just digit-shape" requirement, an ordinary 16-digit
// numeric ID (trace ID, pod hash fragment) must survive untouched.
func TestRedactTextCreditCardRequiresLuhn(t *testing.T) {
	r := NewRedactor(true, false, slog.Default())

	valid := "4111111111111111" // well-known Luhn-valid test Visa number
	if out := r.RedactText("card: " + valid); strings.Contains(out, valid) {
		t.Errorf("Luhn-valid card number was not redacted: %s", out)
	}

	invalid := "4111111111111112" // same shape, fails Luhn (last digit flipped)
	if out := r.RedactText("trace id: " + invalid); !strings.Contains(out, invalid) {
		t.Errorf("a Luhn-INVALID 16-digit number must survive (likely a trace ID/numeric ID, not a card), got: %s", out)
	}
}

// TestRedactTextIPv6DoesNotCatchClockStrings guards the false-positive this
// pattern is most at risk of: an HH:MM:SS timestamp is 3 hex-shaped groups
// (2 colons) and must never be mistaken for a compressed IPv6 address.
func TestRedactTextIPv6DoesNotCatchClockStrings(t *testing.T) {
	r := NewRedactor(true, false, slog.Default())
	in := "request completed at 14:23:05 in 00:00:12"
	out := r.RedactText(in)
	if !strings.Contains(out, "14:23:05") || !strings.Contains(out, "00:00:12") {
		t.Errorf("RedactText over-scrubbed clock-shaped text as IPv6:\n%s", out)
	}
}
