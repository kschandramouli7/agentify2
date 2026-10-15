# 0041 – Splunk log connector (ROADMAP P15)

## Status

Accepted   ·   (date: 2026-10-13)   ·   Amended 2026-10-15 — live-verified against a real disposable Splunk instance; see note after Consequences.

## Context

P15 ("pull-based log-platform connector") shipped its first live connector
against Athena/Glue on 2026-07-25 — explicitly a test harness (ADR 0021),
not a customer's real log platform. Splunk and OpenSearch/Elasticsearch
have stood in the ROADMAP ever since as "the next connectors to add," for
onboarding a customer whose own logs already land somewhere else — but
neither has been designed in the detail Athena got, and no code exists for
either.

**No live Splunk instance is available to build or verify against right
now.** This ADR is a design-first pass: the query construction, schema
assumptions, auth, and wiring, written down and implemented against mocked
HTTP responses, with live verification against a real instance explicitly
deferred rather than guessed at.

### The original P15 design has already drifted once — don't repeat that mistake

P15's original text (2026-07-21) specified a **Go** `LogSource` interface
(`internal/api/adapter_client.go`, `LOG_SOURCE=k8s_adapter|opensearch`)
behind `handlePodLogs`. That substrate no longer exists: ADR 0027 (August)
retired `AdapterClient`/`handlePodLogs` outright in favor of
`live_get_pod_logs` relayed through Discovery's persistent connection. The
ROADMAP already carries a correction note about this for whoever builds
OpenSearch next.

**What actually shipped for Athena went further than that correction
anticipated**: it isn't behind a Go interface at all. It lives entirely in
the **Python agent** — `k8fy/log_platform.py`'s `query_athena_logs`, tried
first by `k8fy/log_router.py`'s `get_logs()`, falling back to
`live_get_pod_logs` on empty/error. ADR 0021 names this "accepted
architecture debt" relative to the original Go-interface plan, but it is
the *only* connector that has actually been built, tested, and run against
real data. A Splunk connector built against the original (now twice-stale)
Go design would again need porting before it could ship.

**Decision: follow the shape that is actually proven, not the shape that
was originally proposed.** Splunk is a new Python module
(`k8fy/log_platform_splunk.py`), with the identical call shape Athena's
`query_athena_logs` already has, tried as another tier inside
`log_router.py`'s `get_logs()`. If a Go-interface unification is ever
worth doing, it should cover Athena, Splunk, and OpenSearch together as one
later refactor — not be re-litigated per connector.

## Decision

### Where it lives, and the three-tier fallback order

`log_router.py`'s `get_logs()` gains a tier **before** Athena:

```
Splunk (if SPLUNK_URL configured)
  → Athena (if ATHENA_WORKGROUP/DATABASE/TABLE configured)
    → live cluster (always available, today's existing path)
```

Splunk goes first when both are configured: Athena is explicitly a test
harness (ADR 0021's own words), while Splunk represents a customer's real,
already-populated log platform — more authoritative when both exist. In
practice the two are unlikely to be configured simultaneously (a given
tenant has one "additional" log platform, not two), but the order is
still a real, documented decision, not an accident of code layout — the
same reasoning `get_logs()`'s own module docstring already uses to justify
Athena-before-live-cluster.

Same output contract as both existing tiers: `{"namespace", "pod", "logs"}`
on success, `{"error": "..."}` on failure — callers (the `get_logs` tool,
`DiagnoseSkill`'s prefetch) never need to know which tier answered.

### Query construction (Splunk REST API, not a dashboard/app)

Splunk's **search-jobs REST API** (`/services/search/jobs`), not SPL typed
into a saved search or dashboard — matches the "bounded, time-windowed read
at diagnosis time" posture ADR 0014 already established, same as Athena's
one-shot query-per-request shape.

```spl
search index=<configured index> kubernetes.namespace_name="<namespace>" [kubernetes.pod_name="<pod>"]
| head <limit>
```
submitted with `earliest_time`/`latest_time` as separate REST parameters
(a bounded time range — never an open-ended search, same non-negotiable
Athena's partition predicate already enforces), not inline in the SPL
string.

Three-call flow, same start→poll→fetch shape `_run_query_sync` already
uses for Athena (same `_POLL_INTERVAL_SECONDS`/`_MAX_POLL_SECONDS`
constants, reused not reinvented):

1. `POST {splunk_url}/services/search/jobs` — form-encoded `search`,
   `earliest_time`, `latest_time`, `output_mode=json` → `{"sid": "..."}`.
2. Poll `GET {splunk_url}/services/search/jobs/{sid}?output_mode=json`
   until `entry[0].content.dispatchState == "DONE"` (or `FAILED`, or the
   poll budget is exhausted — same three-way outcome Athena's poll loop
   already returns).
3. `GET {splunk_url}/services/search/jobs/{sid}/results?output_mode=json&count=<limit>`
   → parse `results[]`.

### Schema — assumed, not discovered, and named as such

Unlike Athena's schema (verified 2026-07-25 against a real captured
record, per `logging.tf`'s own comment), **no real Splunk index exists to
verify field names against.** This design assumes a Kubernetes-filter-style
field layout close to what Fluent Bit's own Splunk output plugin produces
— `kubernetes.namespace_name`, `kubernetes.pod_name`,
`kubernetes.container_name`, with the raw log line in Splunk's own `_raw`
field — the same assumption OpenSearch's still-unbuilt design already
makes for the same reason (modeled on Fluent Bit's standard output, not a
bespoke convention). **This is the single highest-risk unverified
assumption in this design** — a real customer Splunk index populated by
a different shipper (Splunk's own Universal Forwarder, a CIM-compliant
app, etc.) will very likely use different field names. The index name
itself is a required config value (`SPLUNK_INDEX`) precisely because it
can't be assumed or discovered; the *field names inside* events are not
similarly configurable in this first pass, and should become so the first
time this is pointed at a real, differently-shaped index — flagged here
rather than silently assumed correct.

### Auth: Splunk token, Secrets-Manager-backed, optional like Langfuse's — not fail-fast like Anthropic's

Two existing patterns already exist in `config/settings.py` for a
Secrets-Manager-backed credential, and they're deliberately different:
Anthropic's key fetch **raises** on failure (the service cannot run at
all without it); Langfuse's fetch **logs a warning and returns an empty
dict** (the service runs fine without prompt management, just degraded).
Splunk is an *additional, optional* integration the same way Langfuse is
— a missing/invalid Splunk token must degrade to "this tier unavailable,
fall through," never block startup. New fields, mirroring Langfuse's
exact shape:

```python
splunk_url: str = Field(default="", validation_alias=AliasChoices("SPLUNK_URL"))
splunk_index: str = Field(default="", validation_alias=AliasChoices("SPLUNK_INDEX"))
splunk_token: str = Field(default="", validation_alias=AliasChoices("SPLUNK_TOKEN"))
splunk_secret_name: str = "agentify/dev/splunk"
splunk_verify_tls: bool = True
```
`splunk_token` is fetched from Secrets Manager (`splunk_secret_name`) in
the same `model_validator(mode="after")` Langfuse already uses, only when
`SPLUNK_TOKEN` wasn't set directly — same "env var wins, Secrets Manager
is the fallback" precedence Langfuse already has. Sent as
`Authorization: Bearer <token>` — Splunk's own token-auth mechanism
(Splunk Enterprise 7.3+), avoiding a username/password + session-key
dance that would need its own credential and its own expiry handling.

`splunk_verify_tls` exists because enterprise Splunk deployments
self-signed-cert their management port often enough that this needs to be
a real, named config knob (default `True` — fail safe, never silently
skip verification) rather than a hardcoded `verify=True` a future reader
has to go find and flip.

### Injection safety — same discipline as Athena, not weaker

Athena's `_validate_k8s_name` rejects (never escapes) a namespace/pod
value that isn't a valid RFC 1123 Kubernetes name before it's interpolated
into SQL text, because this SDK path has no native query parameterization.
SPL has the same shape of problem (its own quoting rules around
field-value search terms) and the same fix applies: reuse
`_validate_k8s_name` unchanged (it's already namespace/pod-agnostic, not
Athena-specific) rather than inventing a second validator or attempting
SPL-specific escaping.

### Result shape

Same line-join convention Athena's `query_athena_logs` already uses
(`[{pod_name}/{container_name}] {log}` per line, joined, then run through
the *same* `redact_log_text` scrubber) — one shared formatting/redaction
path for every log-platform tier, not a per-connector reimplementation.

## Consequences

- **Positive:** closes the one-line gap in P15's connector lineup with an
  actual design — query construction, auth, schema risk, and wiring order
  are now written down and implementable, not a single unexpanded bullet.
- **Positive:** correctly follows the connector shape that's actually
  proven (Athena's Python module, not the original Go interface that's
  already been superseded once) — avoids building against a design that
  would need porting before it could ship, the same mistake the original
  P15 OpenSearch bullet already had to be corrected for once.
- **Negative / cost accepted:** the field-mapping schema is an assumption,
  not a verified fact, because no real Splunk index exists to check it
  against — named as the highest-risk part of this design rather than
  hidden. The first real deployment against a customer's actual index
  should expect to find field-name mismatches.
- **Negative / cost accepted:** unit-testable (mocked HTTP) but not
  integration-verified — this ADR documents a design ready to implement,
  not a live-confirmed connector. Treat it with the same confidence level
  as unverified code, not as proven as Athena's path.
- **Revisit if:** a real customer Splunk instance becomes available —
  verify the schema assumption first, before trusting any result this
  connector returns; or if OpenSearch gets built before Splunk does, in
  which case confirm the three-tier ordering above still makes sense with
  a fourth tier added, rather than assuming this ADR's two-platform
  ordering generalizes unchanged.

## Amendment (2026-10-15) — live verification against a real instance

The field-mapping schema assumption named above as the highest risk is
**confirmed correct**: tested against a disposable self-hosted Splunk
instance (ADR 0021's "self-hosted disposable pod" pattern, see ROADMAP.md
P15), `kubernetes.pod_name`, `kubernetes.container_name`, and
`kubernetes.namespace_name` all land as flat, independently-searchable
fields exactly as `_build_spl`/the row-parsing code assume.

Getting a clean result surfaced one real bug in this module, now fixed:
`_build_spl` never named `kubernetes.container_name` (and, when no `pod`
filter is given, `kubernetes.pod_name`) anywhere in its SPL. Splunk's JSON
results API only returns fields that are either a fixed "default" set or
explicitly named in the query — so both fields silently vanished from
every result row despite being genuinely indexed, with no error of any
kind. Fixed by appending `| table *` to the query, with a regression test
(`test_build_spl_requests_all_fields`) pinning it. The infra-level bugs
that blocked reaching this point at all (a missing security-group rule,
and a test-fixture `sourcetype` misconfiguration that silently dropped
every event before it could even be searched) are infrastructure/test-
harness issues, not connector-code bugs — see ROADMAP.md P15 for the full
account.
