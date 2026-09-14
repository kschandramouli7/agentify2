# 0007 – Egress data governance: allowlist redaction at the agent boundary

## Status

Accepted   ·   2026-06-01

## Context

The Tier-2 path sends fetched pod data to an external model API (Anthropic). The
data can carry sensitive material — namespaces, pod/service/secret names, and
(as payloads grow) annotations, env, labels. Shipping it raw is a procurement-
killing finding for any enterprise security review, and there is currently **no
redaction and no control over where the data egresses**. See
[ROADMAP §P2a](../ROADMAP.md).

Tier-1 (deterministic, ADR 0006) never egresses, so this concerns the Tier-2
agent path only — but that path has two egress points: the `/reason` request the
backend sends, and the data returned from `/api/agent/fetch` during the agent's
tool loop.

## Decision

1. **Allowlist, not denylist.** Redaction keeps only the fields the reasoning
   needs and drops everything else. A denylist of "sensitive-looking" keys leaks
   every field we failed to anticipate; an allowlist fails safe as payloads grow.
2. **Redact at the backend→agent boundary.** The backend owns the data and the
   governance policy; the agent is a conduit to the model. Both egress points
   (`/reason` input, `/api/agent/fetch` output) are redacted in the backend, so
   whatever the agent sends to the model is already minimized. Implemented in
   `internal/governance` (allowlists in code as a config table) and applied in the
   `/api/query` and `/api/agent/fetch` handlers.
3. **Pluggable egress destination — config for v1.** The model endpoint is
   overridable via `ANTHROPIC_BASE_URL` (honored by the SDK), enabling an
   in-region proxy. First-class in-region clients (Bedrock/Vertex/Foundry — Claude
   is not self-hostable) are a follow-up, not built here. See [ADR 0008](0008-multi-provider-model-routing.md).
4. **Pseudonymization is opt-in, default off.** When enabled, identifier *values*
   (pod/service/secret names, entity keys) are replaced with stable hashes.
   Default off because it degrades operator-facing answers (`id_3f9a…` vs
   `payment-svc`); turn on when a customer review requires it.

## Amendment (2026-09-14) — the operator's question is now in scope, PII coverage widens, and a real enforcement gap was found

A 2026-09-14 audit (prompted by a request to screen "any data... from chat,
logs, or anywhere" before it reaches an LLM) re-examined this ADR's own
non-goals against what's actually shipped. Two findings changed the
decision; a third is a bug against the *existing* policy, not a new one.

**1. The operator's free-text question is no longer exempt.** Point 4's
non-goal ("a determined operator can still put a secret in the prompt") was
an accepted risk in 2026-06 when Tier-2 was new and low-volume. It no longer
holds: the question flows unredacted into the Claude prompt (`agent.py`'s
five call sites — `reason`, `reason_chat`, the executor/advisor and
Pattern-A paths — none apply redaction) **and** into Langfuse tracing
(`agent.yaml`'s tracing config ships the full question and chat history to a
third-party, US-region service). **Decision: the question is now redacted
the same way K8s log text already is** — the existing denylist
(`log_redaction.py`, both copies) applied to the question string before it
reaches *any* egress point (LLM prompt, Langfuse trace, and any future
persistence), not just before storage. This is denylist-based like log
redaction, not allowlist-based like structured data — free text can't be
allowlisted, only screened for known-dangerous shapes — so the residual risk
(a novel secret format the denylist doesn't recognize) is the same accepted
tradeoff `policies/data-governance.md` already documents for log text.

**2. PII coverage widens beyond email.** The only PII class either
`RedactText` or `log_redaction.py` currently recognizes is email. Add phone
numbers, credit-card numbers (Luhn-checked, not just digit-shape, to keep
the false-positive rate sane), and IPv4/IPv6 addresses. Also add key shapes
beyond AWS/JWT: GitHub (`ghp_`, `github_pat_`), Slack (`xox[baprs]-`), Google
(`AIza`), Stripe (`sk_live_`/`sk_test_`), and a generic `sk-` prefix (covers
OpenAI-shaped and several others). No entropy-based catch-all for now — a
generic high-entropy detector has a real false-positive cost against
legitimate hashes/IDs already flowing through this system (pod hashes,
trace UUIDs), and this ADR's own Decision #1 principle (fail toward
minimizing, not toward guessing) argues for named patterns over a heuristic
that would need its own tuning pass.

**3. Found live, not decided here: `incident_embeddings.summary` is written
unredacted — a bug against this ADR's existing policy, not a new gap.**
`HandleQuery`'s diagnose path builds a summary from the model's own prose
and posts it to `/embed` for permanent storage in the pgvector index
(`handlers.go`, around the `/embed` call) with no `RedactText` call — while
`investigator.go`'s webhook path applies `RedactText` to the *identically-
shaped* field before sending it out. `policies/data-governance.md` already
says persistent stores must not receive unredacted log-derived content; this
is that rule being violated by an oversight, not a case this ADR needs to
re-decide. Tracked as ROADMAP OPS-13 — a bug fix, not a design question.

**4. Enforcement moves from "every call site remembers" to a backstop at
the model client itself.** All four points above are still applied
upstream, at the point where data is assembled — that stays, since it's
cheaper (redacting a small assembled payload beats redacting everything a
tool could return) and keeps the allowlist's minimization benefit. But
nothing currently stops a *new* call site from forgetting, the way
`incident_embeddings` did. **Decision: add a defense-in-depth pass in
`claude_client.py` itself**, redacting the outbound prompt text (denylist
patterns only — the allowlist step already ran further upstream and can't
be redone generically at this layer) immediately before the API call. This
does not replace the upstream allowlist/denylist calls; it catches what they
miss, the same "defense in depth, not the primary control" framing
`investigator.go`'s existing double-redaction already established for the
webhook path.

**Shipped (same day).** All four decisions above are implemented, not just
decided:

1. `agent.py` gained `_redact_context_question` (applied inside
   `_build_user_message`, covering `_reason_single`/`_reason_advisor_executor`/
   `_reason_pattern_a` in one place) and `_redact_chat_messages` (applied
   inside `_traced_chat`, which wraps `reason_chat`). The latter redacts
   *before* opening the Langfuse span, not after — `_traced_chat`'s
   `tracing.observe(...)` block runs before the wrapped method body ever
   executes, so redacting inside `reason_chat`'s own body would have been in
   time for the prompt but too late for the trace's `input=`.
2. `redact.go`'s `logScrubbers` and both `log_redaction.py` copies gained
   phone, IPv4, IPv6, GitHub/Slack/Google/Stripe/generic-`sk-` key patterns,
   plus a separate Luhn-validated credit-card pass (candidates are matched by
   digit-shape, then only masked if they also pass the Luhn checksum — kept
   as its own function since Luhn validation is a computed check, not a
   static regex).
3. Shipped as ROADMAP OPS-13, see that item and this ADR's own 2026-09-14
   entry above.
4. `config/claude_client.py`'s module-level `client` is now a
   `RedactingAnthropicClient` wrapping the real `AsyncAnthropic` — its
   `.messages.create`/`.beta.messages.create` redact `messages`/`system`
   immediately before delegating to the real call, recursively covering
   nested content blocks (including a `tool_result` block's JSON-string
   `content`). `K8fyAgent.__init__` picks this up automatically via
   `get_claude_client()`, no call-site changes needed.

`policies/data-governance.md` is updated to match — the operator's question
redaction and the client-level backstop are now documented there instead of
being listed under Non-goals, which had gone stale relative to this
amendment's own decisions.

## Consequences

- **Positive:** removes the raw-egress finding; minimizes tokens sent to the
  model; gives one auditable choke point; in-region routing is a config flag.
- **Negative / cost accepted:** an allowlist can **starve the open-ended Tier-2
  agent** of context as questions broaden — the allowlist must be expanded
  *deliberately* and never widened to "send everything." The operator's free-text
  **question is not redacted** (v1 non-goal) — a determined operator can still put
  a secret in the prompt. Pseudonymization does not cover namespace names embedded
  in pod-registry IDs (a known residual when enabled).
- **Revisit if:** Tier-2 needs richer context (expand the allowlist per field,
  with review), or when in-region model clients are built (Bedrock/Vertex/Foundry —
  [ADR 0008](0008-multi-provider-model-routing.md) — supersedes the
  `ANTHROPIC_BASE_URL`-only step), or multi-tenancy (ROADMAP P3a) requires
  per-tenant classification.
- **Negative / cost accepted (2026-09-14 amendment):** denylist-redacting the
  operator's own question means a legitimate question that happens to match a
  secret-shaped pattern (e.g. quoting a real error message containing a
  connection string) gets truncated — the same false-positive tradeoff
  `policies/data-governance.md` already accepts for log text, now also paid
  on the question path. The `claude_client.py` backstop only catches
  denylist-shaped leaks, not a genuinely novel secret format — it raises the
  floor, it does not close the residual risk Decision #1 already accepted.
- **Positive (shipped 2026-09-14):** all four amendment decisions above are
  now code, not just decided — see the "Shipped" note in the amendment
  itself for exactly what changed and where. The original v1 Consequences
  bullet above ("the operator's free-text question is not redacted") is
  superseded by this; left in place as the historical record of what v1
  actually shipped with, per this file's own append-only convention.

See [policies/data-governance.md](../policies/data-governance.md) for the living rules.
