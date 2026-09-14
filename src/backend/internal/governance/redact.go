// Package governance enforces egress data governance: it minimizes the data the
// backend sends toward the agent/model by applying an allowlist (drop everything
// not explicitly needed), with optional identifier pseudonymization.
//
// See context-mesh/policies/data-governance.md and ADR 0007.
package governance

import (
	"crypto/sha256"
	"encoding/hex"
	"log/slog"
	"regexp"
	"strings"
)

// Redactor applies the egress allowlist to data leaving the backend boundary.
type Redactor struct {
	enabled      bool
	pseudonymize bool
	logger       *slog.Logger

	recordAllow  map[string]struct{} // top-level keys kept on each record
	payloadAllow map[string]struct{} // keys kept inside a record's payload
	identifiers  map[string]struct{} // fields pseudonymized when enabled
}

// NewRedactor builds a Redactor with the default K8fy allowlist.
func NewRedactor(enabled, pseudonymize bool, logger *slog.Logger) *Redactor {
	set := func(keys ...string) map[string]struct{} {
		m := make(map[string]struct{}, len(keys))
		for _, k := range keys {
			m[k] = struct{}{}
		}
		return m
	}
	return &Redactor{
		enabled:      enabled,
		pseudonymize: pseudonymize,
		logger:       logger,
		recordAllow:  set("entity_key", "event_namespace", "type", "timestamp", "source", "payload"),
		payloadAllow: set(
			"pod_id", "namespace", "phase", "ready", "restarts", "reason", "message",
			"service", "endpoints", "ready_endpoints", "ready_ratio", "container",
			"secret", "expires_at", "days_until_expiry", "should_renew",
		),
		identifiers: set("pod_id", "namespace", "service", "secret", "entity_key"),
	}
}

// RedactPodData redacts the /api/query shape: map[podID] -> {data: []rows, ...}.
// Returns a new map; the input is left untouched (Tier-1 still needs the raw data).
func (r *Redactor) RedactPodData(podData map[string]interface{}) map[string]interface{} {
	if !r.enabled || podData == nil {
		return podData
	}
	out := make(map[string]interface{}, len(podData))
	for podID, v := range podData {
		entry, ok := v.(map[string]interface{})
		if !ok {
			out[podID] = v
			continue
		}
		cp := make(map[string]interface{}, len(entry))
		for k, val := range entry {
			cp[k] = val
		}
		if rows, ok := entry["data"].([]map[string]interface{}); ok {
			cp["data"] = r.redactRows(rows)
		}
		out[podID] = cp
	}
	return out
}

// RedactFetch redacts the /api/agent/fetch shape: map[podID] -> []rows.
func (r *Redactor) RedactFetch(data map[string]interface{}) map[string]interface{} {
	if !r.enabled || data == nil {
		return data
	}
	out := make(map[string]interface{}, len(data))
	for podID, v := range data {
		if rows, ok := v.([]map[string]interface{}); ok {
			out[podID] = r.redactRows(rows)
		} else {
			out[podID] = v
		}
	}
	return out
}

func (r *Redactor) redactRows(rows []map[string]interface{}) []map[string]interface{} {
	out := make([]map[string]interface{}, 0, len(rows))
	for _, row := range rows {
		out = append(out, r.redactRecord(row))
	}
	return out
}

func (r *Redactor) redactRecord(row map[string]interface{}) map[string]interface{} {
	out := make(map[string]interface{})
	for k, v := range row {
		if _, ok := r.recordAllow[k]; !ok {
			continue // drop anything not explicitly allowed
		}
		if k == "payload" {
			if p, ok := v.(map[string]interface{}); ok {
				out[k] = r.redactPayload(p)
				continue
			}
		}
		out[k] = r.maybePseudonymize(k, v)
	}
	return out
}

func (r *Redactor) redactPayload(p map[string]interface{}) map[string]interface{} {
	out := make(map[string]interface{})
	for k, v := range p {
		if _, ok := r.payloadAllow[k]; !ok {
			continue
		}
		out[k] = r.maybePseudonymize(k, v)
	}
	return out
}

func (r *Redactor) maybePseudonymize(key string, v interface{}) interface{} {
	if !r.pseudonymize {
		return v
	}
	if _, ok := r.identifiers[key]; !ok {
		return v
	}
	if s, ok := v.(string); ok && s != "" {
		return pseudonym(s)
	}
	return v
}

// pseudonym returns a stable, non-reversible token for an identifier value.
func pseudonym(s string) string {
	h := sha256.Sum256([]byte(s))
	return "id_" + hex.EncodeToString(h[:])[:10]
}

// --- Best-effort text scrubbing for freeform logs (ADR 0014) ---
//
// IMPORTANT: this is a DENYLIST, not the allowlist guarantee the structured
// redactor above provides. It masks known secret shapes in log text but WILL miss
// novel ones. It is the reason logs are fetched on-demand and never persisted
// (ADR 0014): the blast radius of a miss is a transient leak to the model, not a
// permanent one in storage.

// maxLogChars caps scrubbed log text so a tail can't blow up token cost.
const maxLogChars = 16384

var logScrubbers = []*regexp.Regexp{
	// Authorization: Bearer <token> / "bearer <token>"
	regexp.MustCompile(`(?i)(authorization\s*[:=]\s*)(bearer\s+)?[A-Za-z0-9._\-]{12,}`),
	// AWS access key IDs
	regexp.MustCompile(`AKIA[0-9A-Z]{16}`),
	// JWT-ish: three base64url segments
	regexp.MustCompile(`eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+`),
	// key=secret / "password": "..." style assignments
	regexp.MustCompile(`(?i)(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|client[_-]?secret)("?\s*[:=]\s*"?)[^\s"',;}]+`),
	// connection-string password: proto://user:pass@host  → mask the pass
	regexp.MustCompile(`(://[^:/\s]+:)[^@/\s]+(@)`),
	// emails
	regexp.MustCompile(`[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}`),
	// long hex / base64 blobs (likely keys/hashes)
	regexp.MustCompile(`\b[A-Fa-f0-9]{32,}\b`),
	regexp.MustCompile(`\b[A-Za-z0-9+/]{40,}={0,2}\b`),
	// --- ADR 0007, 2026-09-14 amendment: PII coverage widens past email-only ---
	// phone numbers — requires a separator after the area-code-shaped group so
	// this doesn't collide with arbitrary concatenated-digit IDs/trace UUIDs,
	// which this system has plenty of and none of which use separators.
	regexp.MustCompile(`\b(?:\+?\d{1,2}[-.\s])?\(?\d{3}\)?[-.\s]\d{3}[-.\s]?\d{4}\b`),
	// IPv4 — validated octets (0-255), not just dot-separated digit shape.
	regexp.MustCompile(`\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b`),
	// IPv6 — the standard multi-alternative pattern (each alternative pins how
	// many groups fall before/after a "::" compression), not a naive repeated
	// group: the naive form mis-splits the double colon and simply fails to
	// match compressed addresses like "2001:db8::1". Requires 4+ hex groups
	// (3+ colons) minimum so an HH:MM:SS clock string (2 colons) never matches.
	regexp.MustCompile(`\b(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}|(?:[0-9A-Fa-f]{1,4}:){1,7}:|(?:[0-9A-Fa-f]{1,4}:){1,6}:[0-9A-Fa-f]{1,4}|(?:[0-9A-Fa-f]{1,4}:){1,5}(?::[0-9A-Fa-f]{1,4}){1,2}|(?:[0-9A-Fa-f]{1,4}:){1,4}(?::[0-9A-Fa-f]{1,4}){1,3}|(?:[0-9A-Fa-f]{1,4}:){1,3}(?::[0-9A-Fa-f]{1,4}){1,4}|(?:[0-9A-Fa-f]{1,4}:){1,2}(?::[0-9A-Fa-f]{1,4}){1,5}|[0-9A-Fa-f]{1,4}:(?::[0-9A-Fa-f]{1,4}){1,6}|:(?:(?::[0-9A-Fa-f]{1,4}){1,7}|:)\b`),
	// key shapes beyond AWS/JWT
	regexp.MustCompile(`\bghp_[A-Za-z0-9]{36}\b`),           // GitHub classic PAT
	regexp.MustCompile(`\bgithub_pat_[A-Za-z0-9_]{20,}\b`),  // GitHub fine-grained PAT
	regexp.MustCompile(`\bxox[baprs]-[A-Za-z0-9-]{10,}\b`),  // Slack token
	regexp.MustCompile(`\bAIza[0-9A-Za-z_\-]{35}\b`),        // Google API key
	regexp.MustCompile(`\bsk_(?:live|test)_[A-Za-z0-9]{16,}\b`), // Stripe key
	regexp.MustCompile(`\bsk-[A-Za-z0-9]{20,}\b`),           // generic sk- prefix (OpenAI-shaped and others)
}

// replacements pairs each scrubber with how to rewrite a match. Patterns that have
// a "prefix to keep" use a capture-group template; the rest mask wholesale.
var logReplacements = []string{
	`${1}${2}***`, // authorization
	`***`,         // aws key
	`***`,         // jwt
	`${1}${2}***`, // key=secret (keep field name + delimiter)
	`${1}***${2}`, // conn-string password
	`***`,         // email
	`***`,         // hex blob
	`***`,         // base64 blob
	`***`,         // phone
	`***`,         // ipv4
	`***`,         // ipv6
	`***`,         // github classic PAT
	`***`,         // github fine-grained PAT
	`***`,         // slack token
	`***`,         // google api key
	`***`,         // stripe key
	`***`,         // generic sk- prefix
}

// creditCardCandidate finds digit runs shaped like a card number (13-19
// digits, optionally grouped with spaces/dashes). This alone is far too
// loose — ordinary numeric IDs are exactly this shape — so redactCreditCards
// only masks a candidate that also passes the Luhn check digit, keeping the
// false-positive rate sane per ADR 0007's 2026-09-14 amendment ("Luhn-checked,
// not just digit-shape").
var creditCardCandidate = regexp.MustCompile(`\b\d(?:[ -]?\d){12,18}\b`)

// luhnValid reports whether digits (a string of only '0'-'9') passes the
// Luhn checksum used by every major card network.
func luhnValid(digits string) bool {
	sum := 0
	alt := false
	for i := len(digits) - 1; i >= 0; i-- {
		d := int(digits[i] - '0')
		if alt {
			d *= 2
			if d > 9 {
				d -= 9
			}
		}
		sum += d
		alt = !alt
	}
	return sum%10 == 0
}

// redactCreditCards masks only candidates that pass Luhn — a candidate that
// fails is left untouched, since at 13-19 digits it's far more likely to be
// a trace ID, pod hash fragment, or other ordinary numeric identifier this
// system already has plenty of.
func redactCreditCards(s string) string {
	return creditCardCandidate.ReplaceAllStringFunc(s, func(m string) string {
		digits := strings.Map(func(r rune) rune {
			if r >= '0' && r <= '9' {
				return r
			}
			return -1
		}, m)
		if len(digits) >= 13 && len(digits) <= 19 && luhnValid(digits) {
			return "***"
		}
		return m
	})
}

// RedactText masks common secret shapes in freeform text (logs) and truncates it.
// A no-op when redaction is disabled. See ADR 0014 for the (weaker) guarantee.
func (r *Redactor) RedactText(s string) string {
	if !r.enabled {
		return s
	}
	s = redactCreditCards(s)
	for i, re := range logScrubbers {
		s = re.ReplaceAllString(s, logReplacements[i])
	}
	if len(s) > maxLogChars {
		s = s[:maxLogChars] + "\n…[truncated]"
	}
	return s
}
