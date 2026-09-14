# Policy: Data Governance (egress)

> **Question this answers:** What data is allowed to leave our boundary to an
> external model, and how is it minimized? See [ADR 0007](../decisions/0007-egress-data-governance.md).

## Principle

**Send the model the least data that lets it answer.** Default to dropping; keep
only what an answer needs. Data leaving the boundary is redacted at one auditable
choke point — the backend, before anything reaches the agent/model.

## Where the gate sits

```
Tier-1 (deterministic) ─────────────────────────────► answer   (no egress)

Tier-2:  store data ──▶ [REDACT: allowlist] ──▶ agent ──▶ [REDACT: denylist] ──▶ model
         /api/agent/fetch ──▶ [REDACT: allowlist] ──▶ agent ──▶ [REDACT: denylist] ──▶ model
         operator's question ──▶ [REDACT: denylist] ──▶ agent ──▶ [REDACT: denylist] ──▶ model
                                                                        │
                                                                        └─▶ Langfuse trace
```

The two backend→agent egress points are redacted (`internal/governance.Redactor`,
allowlist). The operator's own free-text question is redacted separately — it
can't be allowlisted (free text has no enumerable "fields"), so it gets the same
denylist pass as log text, applied where the question is assembled into a prompt
or chat turn (`agent.py`'s `_build_user_message`/`_traced_chat`), before either
the model call or the Langfuse trace sees it. A second denylist pass sits at the
Anthropic client itself (`config/claude_client.py`) as a backstop — defense in
depth for a future call site that forgets to redact upstream, not the primary
control.

## The allowlist (config table, not hardcoded policy)

Only allowlisted keys survive; everything else is dropped.

- **Record level:** `entity_key`, `event_namespace`, `type`, `timestamp`, `source`, `payload`.
- **Payload level (K8fy):** `pod_id`, `namespace`, `phase`, `ready`, `restarts`,
  `reason`, `message`, `service`, `endpoints`, `ready_endpoints`, `ready_ratio`,
  `container`, `secret`, `expires_at`, `days_until_expiry`, `should_renew`.

Dropped by default (not allowlisted): annotations, labels, env, raw `conditions`,
and any field a future adapter adds until it is reviewed and added here.

**Rule:** expand the allowlist one field at a time, with review. Never replace it
with "send everything."

## Freeform text (logs, chat, and the operator's question) — a weaker, denylist guarantee (ADR 0014)

On-demand pod logs ([spec 008](../specs/008-on-demand-pod-logs.md)), chat messages,
and the operator's own free-text question are all **freeform text**, so the
allowlist above cannot apply — you can't enumerate the safe "fields" of a log line
or a sentence in advance. They pass through `Redactor.RedactText` (Go) /
`redact_log_text` (Python, two hand-synced copies), a **best-effort denylist**
that masks known secret shapes: bearer tokens, AWS keys, JWTs, `key=secret` pairs,
connection-string passwords, long hex/base64 blobs, GitHub/Slack/Google/Stripe/
generic `sk-`-prefixed keys, and PII — email, phone numbers, IPv4/IPv6 addresses,
and Luhn-checked credit card numbers (checksum-validated, not just digit-shape,
to keep the false-positive rate against ordinary numeric IDs sane) — then
truncates the tail.

⚠️ **This is explicitly weaker than the allowlist.** It will miss novel secret
formats; a log line *can* still carry sensitive data to the model. That weaker
guarantee is the reason logs are **fetched on-demand and never persisted**
([ADR 0014](../decisions/0014-on-demand-ephemeral-log-fetch.md)) — the blast radius
of a miss is one transient prompt, not a permanent store. Do not route logs into
any persistent store under this policy.

## Optional pseudonymization (default off)

When `REDACTION_PSEUDONYMIZE=true`, identifier *values* (`pod_id`, `namespace`,
`service`, `secret`, `entity_key`) become stable hashes (`id_<10hex>`), so the
model can still correlate entities without seeing real names. Off by default
because it degrades operator-facing answers. Known residual: namespace names
embedded in pod-registry IDs are not pseudonymized.

## Egress destination

The model endpoint is overridable via `ANTHROPIC_BASE_URL` for an in-region proxy.
First-class in-region clients (Bedrock/Vertex/Foundry — Claude is not self-hostable)
are future work; see [ADR 0008](../decisions/0008-multi-provider-model-routing.md).

## Non-goals

- Per-tenant classification (depends on multi-tenancy, ROADMAP P3a).
- Encryption/DLP beyond field minimization.
- An entropy-based catch-all secret detector — named patterns only (see the
  denylist section above); a generic high-entropy detector has a real
  false-positive cost against legitimate hashes/IDs already flowing through
  this system (pod hashes, trace UUIDs).

## How this adapts over time

New integrations register their needed fields into the allowlist (reviewed). The
[refinement-loop](refinement-loop.md) could later flag fields that are sent but
never improve answers, to trim the allowlist.
