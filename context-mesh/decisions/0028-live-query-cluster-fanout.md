# 0028 – Cluster-aware recommended actions: deterministic fan-out + merge across fleet clusters

## Status

Accepted   ·   (date: 2026-08-18)   ·   Amended 2026-08-18 — see note below

**Amendment (2026-08-18):** the two open gaps this ADR's "Relationship to
Glue-based dependency mining" section named (no cluster-identifying column
in the Glue schema; no way for a centralized caller to attribute an edge to
an explicit `cluster_id`) are resolved by
[ADR 0029](0029-glue-based-dependency-mining.md), which builds the
Glue-based miner this section only speculated about. This ADR's own design
(the fan-out mechanism itself) is unchanged — only the two named gaps in
its Glue-mining side note are no longer open.

## Context

Diagram 7 (`docs/SEQUENCE_FLOWS.md`) documents the Chat UI's "Run" button on
a recommended action: Frontend → Hub (thin proxy) → Agent → the Kubernetes
API of whatever cluster **the agent pod itself** runs in. That local branch
is only correct when the service being discussed actually lives in the
agent's own cluster — true for a single-cluster deployment, false for a
real multi-tenant fleet (ADR 0022) where most clusters are reachable only
through their own Discovery collector, over the persistent relay Diagram 6
describes.

Two confirmed, structural gaps meant this was never routed correctly:

- `CHAT_REASONING_SCHEMA` (`src/agent/k8fy/agent.py`) — the schema Claude is
  constrained to when building `recommended_actions` — has
  `additionalProperties: false` on `arguments`, allowing only `namespace`,
  `pod`, `container`, `tail_lines`, `previous`. `cluster_id` cannot appear;
  Claude structurally cannot supply it.
- `_structure_chat_answer` never called `resolve_service_clusters` (ADR
  0023) — every Pattern-A skill (`HealthSkill`, `CertAuditSkill`,
  `DiagnoseSkill`, etc.) resolves a service's fleet cluster(s) during its
  own prefetch fan-out; the chat-structuring step that actually produces
  Run-button actions never did.

Net effect: every Run-button click took the local branch regardless of
where the service actually lived — at best a 404, at worst a silent wrong
answer if the namespace name happened to collide with something in the
agent's own cluster.

The fix needed to go further than "route to the one right cluster,"
though: a service can have dependencies spanning more than one fleet
cluster, and `resolve_service_clusters` already returns a list, not a
single ID (other skills already fan out per resolved cluster during
prefetch). A single Run button can only carry one HTTP call's worth of
routing — so when resolution finds more than one cluster, the request
needs to go to *each* of them and the results need to come back combined,
not routed to a guess.

## Decision

**Extend the existing "never trust the model, override deterministically
from context" pattern (already used for `namespace`, `agent.py`) to cluster
routing, and add a deterministic multi-cluster merge for the case where
more than one cluster resolves.**

1. **Resolution happens once per chat response, not once per action.**
   `_structure_chat_answer` calls `resolve_service_clusters(namespace,
   service, backend_url)` when both are present in `context`, and reuses
   the result across every `recommended_action` in that response.
   Degrades to skipping resolution entirely when `service` is absent
   (nothing to look up), same as `resolve_service_clusters` itself
   degrading to `[]` on any backend/network failure — both leave
   `arguments` untouched, preserving today's local-execution behavior for
   a single-cluster deployment with no registered fleet clusters.
2. **Zero clusters resolved → unchanged.** No fleet cluster is registered
   for this service; local execution is already correct.
3. **Exactly one cluster resolved → singular `cluster_id` attached.**
   Routes through Diagram 6's existing single-cluster relay
   (`_dispatch_live_diagnostic`'s pre-existing branch), unchanged.
4. **Two or more clusters resolved, and the action has no `pod`
   argument → plural `cluster_ids` attached.** This is new:
   `_dispatch_live_diagnostic` (`src/agent/k8fy/tools.py`) gained a first
   branch that, on seeing `cluster_ids`, calls the existing
   `_remote_live_fetch` once per cluster **concurrently**
   (`asyncio.gather(..., return_exceptions=True)`) and merges the results
   with a new `_merge_fanout_results` helper: each item in the tool's
   list-shaped field (`pods` / `events` / `certificates`) is tagged with
   its source `cluster_id` and concatenated; a cluster that errored or
   timed out is recorded in `clusters_failed` rather than silently
   dropped. `_remote_live_fetch` needed **zero changes** — it already
   strips `cluster_id` before relaying and is safe to call concurrently.
5. **Two or more clusters resolved, but the action has a `pod`
   argument → best-effort singular `cluster_id` (first resolved).** A
   pod name is already cluster-specific — fan-out cannot disambiguate
   which cluster it belongs to, and there is no mechanism today to resolve
   that. Rather than fall back to local execution (the exact
   possibly-wrong-cluster behavior this ADR fixes), a specific cluster is
   guessed deterministically (first of the resolved list, not an LLM
   guess) and the limitation is named here rather than silently
   "handled."
6. **The merge is deterministic, not LLM-synthesized.** `context-mesh/
   policies/correlation.md` already governs fan-out-and-correlate, but for
   a different shape of problem: combining a service's *own* signals
   (health/certs/events) within one cluster, before a diagnostic Claude
   call that can reason over them. Diagram 7's Run-button path is
   deliberately no-LLM — a quick, deterministic re-check of what a
   diagnosis already found — so extending it to span clusters must stay
   deterministic too. Concatenating three clusters' pod lists and tagging
   each with its origin is a plain merge, not a synthesis task; adding a
   second Claude call here would contradict the reason this path exists.
7. **No Hub (Go) or frontend changes.** `HandleLiveToolCall`
   (`handlers.go`) already forwards `arguments` as an opaque
   `map[string]interface{}`; `runLiveTool`/`ActionRow`
   (`api.ts`/`DiagnosisReport.tsx`) already render whatever JSON comes
   back generically. The entire feature is scoped to `src/agent/`, reusing
   Diagram 6's existing per-cluster relay and its existing bounded-failure
   guarantee (502 "cluster not connected" / 504 timeout, now per fanned-out
   cluster instead of once).

### Relationship to Glue-based dependency mining (ROADMAP P15 / P18 use case #2)

A separate, not-yet-built roadmap item proposes mining service-dependency
edges from centralized Athena/Glue log data instead of (or alongside)
Discovery's current live, per-cluster log mining. Worth recording
explicitly how the two relate, since they are complementary, not
overlapping, and touch adjacent code:

- **This ADR's fan-out exists because each fleet cluster is only reachable
  *live*, through its own Discovery collector** — there is no
  already-aggregated view of current cluster state, so reaching N clusters
  means N separate relayed calls, merged after the fact.
- **Glue/Athena is the opposite shape.** A single shared S3/Glue
  destination already aggregates logs across every onboarded cluster at
  ingest time (Firehose). A dependency miner built against it would issue
  **one** query spanning every cluster, not a fan-out — the correlation
  already happened upstream, at ingest, not at query time.
- **They connect through data, not code.** Both would be producers into
  the same `service_dependencies`/`cluster_services` tables via the same
  Hub ingest endpoints (`POST /api/service-dependencies`,
  `POST /api/cluster-inventory`) that Discovery's live mining already uses,
  reusing `extract_service_mentions`'s existing log-source-agnostic
  extraction logic (`ROADMAP.md`'s P18 use case #2 section) — just fed
  Athena query rows instead of live pod-log lines. A Glue-based miner
  would most naturally live in the Agent process alongside
  `log_router.py`'s existing Athena connector, not inside Discovery, since
  Discovery's reason to exist is per-cluster live access and Athena
  querying needs no cluster connectivity at all.
- **Net effect for this ADR:** `resolve_service_clusters` — the lookup the
  fan-out mechanism depends on — would get more complete data over time if
  a Glue-based miner ships later (it might surface a dependency edge live
  per-cluster sampling missed). Nothing in this ADR's design needs to
  change for that to happen; it is a strictly additive data source
  underneath an unchanged read path.
- **Two open gaps for whoever picks up the Glue-mining item, recorded here
  so they aren't rediscovered from scratch, not solved by this ADR:**
  (1) Glue log rows carry `kubernetes.cluster_name` (human-readable) while
  `Integration.ID`/`cluster_id` is not necessarily the same string — needs
  a mapping. (2) Glue rows carry `kubernetes.labels.app` but not a live
  Service-selector match, so `from_service` attribution would be coarser
  than today's live `_service_for_pod` (`src/adapters/discovery/main.py`)
  selector-based matching.

## Consequences

- **Positive:** Run-button actions now route to the cluster(s) the
  discussed service actually lives in, closing a real correctness gap
  (silent wrong-cluster reads in a genuine multi-cluster fleet). A
  dependency spanning multiple clusters now gets one combined,
  cluster-attributed answer instead of a guess at a single cluster.
- **Positive:** zero Hub or frontend changes — the fix is fully contained
  in the Agent process, reusing every existing relay/allow-list/
  bounded-failure mechanism as-is.
- **Negative / cost accepted:** pod-specific tools (`live_get_pod_logs`,
  `live_describe_pod`) still can't be routed with full confidence when a
  service resolves to multiple clusters — the best-effort first-cluster
  guess is a known, named gap, not a solved one.
- **Negative / cost accepted:** `_structure_chat_answer` now makes one
  additional network round trip (`resolve_service_clusters`, ~5s worst-case
  timeout) whenever a chat response's context carries both `namespace` and
  `service` and produced at least one recommended action — bounded by the
  same graceful-degradation contract (`[]` on any failure) every other
  caller of that function already relies on.
- **Revisit if:** a mechanism for resolving "which cluster is pod X in"
  is ever built (would remove the pod-specific best-effort limitation), or
  the Glue-based dependency miner in ROADMAP P15/P18 use case #2 is picked
  up (would need the cluster-name mapping and coarser-attribution gaps
  above addressed).
