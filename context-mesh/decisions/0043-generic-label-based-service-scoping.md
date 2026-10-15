# 0043 – Generic label-based service scoping for dependency views (ROADMAP P32)

## Status

Accepted   ·   (date: 2026-10-15)   ·   Implemented 2026-10-15 for the Glue and Splunk miners, the single-shot and chat entry points, and the LLM-tool fallback — Discovery's live-scan capture and the frontend in/out-of-scope visual treatment are explicitly NOT done yet; see the amendment after Consequences.

## Context

Ops/Support asked for an APM-number-driven view: key in an APM number in
chat, see the dependency flow for every microservice tagged with it —
including downstream systems that have a *different* (or no) APM number,
since "what does this app actually touch" is the whole point of asking.
APM numbers here are **pod labels**, not Service-level metadata or an
external CMDB lookup (confirmed with the user) — e.g. a pod carries
`apm-id: APM123456` alongside its other labels.

A second, related ask followed: don't hardcode "apm" anywhere — if a
different team tags pods with `team`, `cost-center`, or anything else,
the same lookup should work for that label key too, with no code change.

**What already exists, and what's missing:** every mining path that
resolves `from_service` (Discovery's live scan, the Glue miner, and the
newly-verified Splunk miner — ADR 0029's `_service_for_labels`/
`_fetch_selectors`) already computes a pod's **full label dict** in memory,
purely to match it against a Service's `spec.selector`. That dict is
discarded immediately after resolving the service name. Nothing in this
codebase persists Service-level or pod-level labels for any OTHER purpose
today (confirmed by survey: `cluster_services` stores only `selector`
JSONB for matching, plus one single hardcoded annotation key,
`agentify.io/expected-failure` — no generic labels/annotations capture
exists anywhere). Adding this is additive to data already in hand, not a
new scan or a new network call to any source.

A second, separate design question came up alongside this: how much of
this flow needs an LLM call, and where. This codebase already draws a
hard line between a fast, deterministic Tier-1 path and a full agentic
Tier-2 one ([ADR 0006](0006-two-tier-query-path.md)) — this feature should
slot into that existing split, not invent a third tier or default to an
LLM call for every lookup.

**Explicitly out of scope for this ADR:** the other half of the original
ask — a user naming a **Splunk index** in chat and getting an ad-hoc,
one-shot dependency-mining pass against it. That is a materially
different mechanism (a parameterized one-shot version of
`splunk_dependency_miner.py`'s `_mine_namespace`, not a label lookup) and
deserves its own ADR once this one ships — bundling both would conflate
two different designs the way P31's own phasing explicitly avoided doing.

## Decision

**1. Capture is allowlisted, not denylisted, and configured once,
deployment-wide.** A new setting, `TRACKED_LABEL_KEYS` (comma-separated,
parsed the same way `config.py`'s existing `_list_env` already parses
`CROSS_CLUSTER_INGRESS_KINDS`) — only pod label keys named in this list
are ever persisted. Rejected the alternative (capture every label minus a
denylist of known K8s-generated noise like `pod-template-hash`): an
allowlist is smaller, has no risk of an unbounded/noisy label space, and
makes "is this a label type we track?" a simple, fast membership check at
query time rather than a fuzzy judgment call. The cost accepted: a brand
new label dimension needs a config change (not a code change or redeploy
of mining logic) before it's queryable — acceptable, since this is one
settings edit, the same posture every other operator-configured knob in
this codebase already has (`SPLUNK_INDEX`, `ATHENA_TABLE`, etc.).

**2. Storage is generic key/value, at service granularity, not a named
column per label type.** New table:

```sql
CREATE TABLE IF NOT EXISTS service_labels (
    tenant_id   TEXT NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001',
    cluster_id  TEXT NOT NULL,
    namespace   TEXT NOT NULL,
    service     TEXT NOT NULL,
    label_key   TEXT NOT NULL,
    label_value TEXT NOT NULL,
    last_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, cluster_id, namespace, service, label_key)
);
-- RLS enabled/forced, same convention as service_dependencies.
```

One row per (service, label_key) — not per pod. Every pod behind one
Service's selector is assumed to share the same business-meaningful label
value, the same atomicity assumption `_service_for_labels` already makes
when resolving one service name from one pod's labels. A later pod with a
different value for the same key (e.g. mid-rollout) overwrites via
`last_seen`-ordered upsert — last-known-wins, the same convention
`service_dependencies`' own outcome/match_kind merge already uses. No
`apm_id` column, no label-specific schema anywhere — `label_key` is
free-form text drawn from `TRACKED_LABEL_KEYS` at write time, validated
against that same list at read time.

**3. Capture is wired into existing mining cycles, not a new scan.** Every
miner that already calls `_service_for_labels` successfully gains one
more call once `from_service` resolves — filter that pod's label dict down
to keys in `TRACKED_LABEL_KEYS`, push each surviving pair. Symmetric to
`_push_edge`; no new interval, no new source query.

**4. One generic read endpoint, parameterized, never hardcoded.**
`GET /api/service-labels?label_key=X&label_value=Y` → `[{cluster_id,
namespace, service}, ...]`. Returns 400 (not a silent empty list) when
`label_key` isn't in `TRACKED_LABEL_KEYS`, so a typo or an untracked
dimension is distinguishable from "no services match."

**5. Graph scoping is a union over either endpoint, reusing
`service_dependencies` as-is.** Given the matched service set from (4), a
new query returns every row where `(cluster_id, namespace, from_service)`
**or** `(target_cluster_id, namespace, to_service)` is in that set — not
an intersection. This is what gives "downstream systems with a different
or no APM number" for free: an edge qualifies if *either* side is in
scope, regardless of the other side's tag (or lack of one). No schema
change to `service_dependencies` itself.

**6. Tier-1 fast path for the common case; full tool-use as fallback —
not a third tier.** Two deterministic, zero-LLM-call recognizers run
before any chat/tool-use turn: explicit `key=value` syntax, and a small,
operator-editable alias table (e.g. `{"apm": "apm-id"}`) combined with a
fixed-format regex per alias (e.g. `APM\d+`) for well-known shorthand.
Either match resolves `(label_key, label_value)` deterministically, same
spirit as every other Tier-1 recognizer in this codebase. Free-form
phrasing ("what talks to the checkout team's stuff") falls back to the
agent's existing tool-use turn — the LLM fills `(label_key, label_value)`
as arguments on the SAME call it already makes to pick any tool, not an
additional call; one more existing call synthesizes the final answer.
Rejected: always resolving via an LLM call, which would make the common,
recognizable case (an ops person pasting a known APM number) pay full
agentic latency/cost for a lookup that's really just a dictionary read.

## Consequences

- **Positive:** fully generic once built — a new business label dimension
  (cost-center, team, release-train, anything) is queryable the moment
  it's added to `TRACKED_LABEL_KEYS`, with zero code change to capture,
  storage, the read endpoint, or the graph-scoping query.
- **Positive:** the "downstream system with a different/no tag still
  shows up" requirement falls out of the OR-based scoping query with no
  special-casing — not a separate rule to maintain.
- **Positive:** stays cheap for the expected common case (a known-format
  identifier or explicit syntax) by design, consistent with this
  codebase's existing Tier-1/Tier-2 split rather than introducing new cost
  behavior for one feature.
- **Negative / cost accepted:** `TRACKED_LABEL_KEYS` is a single,
  deployment-wide list in v1, not per-tenant. Revisit if a second tenant
  on the same Hub needs a different tracked set — ADR 0022's multi-tenant
  model would need this setting to move from env var to a per-tenant
  config row.
- **Negative / cost accepted:** service-granularity storage means a
  canary/mixed-value rollout briefly shows one service under one value at
  a time (whichever pod reported most recently), never a split view —
  acceptable for a dependency-scoping lookup, would NOT be acceptable if
  this table were ever reused for per-pod analytics.
- **Revisit if:** the alias table for conversational shorthand grows past
  a handful of entries and starts needing its own admin UI rather than a
  config edit; or if a tracked label key's value needs fuzzy/partial
  matching (not exact `label_value` equality) — out of scope here, exact
  match only.

## Amendment (2026-10-15) — implementation status

Shipped, with full test coverage at every layer (Go: `postgres_test.go`'s
new `service_labels`/`ListServiceDependenciesForServices` subtests,
`service_label_test.go`'s handler tests, `intent_test.go`'s
`TestInferIntent_LabelScopedDependencies`; Python: `test_settings.py`,
the `_push_service_labels` tests in `test_dependency_miner.py`/
`test_splunk_dependency_miner.py`, `test_chat_label_scoped.py`,
`test_label_scoped_dependency_skill.py`, `test_lookup_services_by_label_tool.py`):

- `service_labels` table, RLS policy, `UpsertServiceLabel`/
  `ListServicesByLabel`/`ListServiceDependenciesForServices`, and the
  three new endpoints (`POST`/`GET /api/service-labels`,
  `GET /api/service-dependencies-for-services`).
- `TRACKED_LABEL_KEYS`/`LABEL_KEY_ALIASES` settings; `_push_service_labels`
  wired into **the Glue and Splunk miners** (`dependency_miner.py`,
  `splunk_dependency_miner.py`).
- The Tier-1 fast path (`labelScopedDependencyRE` in Go's `inferIntent`,
  mirrored by `_resolve_label_query`/`_chat_route` in `agent.py`), the
  deterministic single-shot path (`LabelScopedDependencySkill`, reached
  via the same `answerDependencies` bypass `dependencies` already uses —
  it turned out to be fully generic, not `dependencies`-specific, so no
  new Go handler was needed), the chat-page path (`reason_chat`'s new
  `route == "label_scoped_dependencies"` branch), and the conversational
  fallback (`lookup_services_by_label`, a new Claude tool in `tools.py`).

**Explicitly NOT done, flagged rather than silently skipped:**

- **Discovery's own live per-cluster scan** (`src/adapters/discovery/`)
  does not yet call `_push_service_labels` — only the two Hub-side Python
  miners do. Discovery is a separate deployable with its own config
  system (dataclass + `_list_env`, not pydantic `Settings`), and wiring it
  in properly deserves its own pass rather than a rushed addition here.
  Until that lands, a service whose ONLY mining evidence comes from
  Discovery's live scan (no Glue/Splunk coverage) will never appear in a
  label-scoped lookup even if correctly tagged.
- **Frontend visual distinction between in-scope and out-of-scope nodes**
  (spec 012's open question #3) was not built. The existing
  `ServiceGraphSection`/`service_graph` rendering path is reused as-is
  (confirmed safe: `namespace` is display text only, never used for a
  further API call), so the graph renders correctly today — just without
  a badge distinguishing which nodes were in the tagged set versus pulled
  in only because they're connected to one.
