# 0029 – Glue-based service-dependency mining across the fleet

## Status

Accepted   ·   (date: 2026-08-18)

## Context

ADR 0028's "Relationship to Glue-based dependency mining" section flagged,
but explicitly deferred, a separate roadmap item (ROADMAP P15/P18 use case
#2's Glue extension): mining service-dependency edges from centralized
Athena/Glue log data, complementing `agentify-discovery`'s existing live,
per-cluster mining (`src/adapters/discovery/main.py`'s `_scan_namespace`).

Investigating this rather than assuming the roadmap's description was
accurate surfaced a bigger gap than ADR 0028's note implied:

- **The production Glue table had no cluster-identifying column at all**
  (`infra/terraform/aws/logging.tf`) — not "needs a name-to-ID mapping," but
  genuinely absent. The actual schema is `kubernetes: struct<container_name,
  host, labels:map<string,string>, namespace_name, pod_name, pod_ip, ...>` +
  `log: string` (one raw CRI line, `"<timestamp> <stream> <tag> <message>"`)
  — not the aspirational `@timestamp`/`kubernetes.cluster_name`/`message`
  schema `ROADMAP.md` describes for a different, never-built connector.
  `host` is a node hostname, not a cluster identifier.
- **`POST /api/service-dependencies` derived `cluster_id` only from the
  caller's own per-cluster `CollectorToken`** — no way to accept an explicit
  `cluster_id`. A centralized miner querying logs spanning several clusters
  in one Athena query had no caller identity to attribute edges through.
- **`from_service` attribution needs a live K8s Service-selector to
  pod-label match** (`_service_for_pod`, `main.py`) — but Glue's
  `kubernetes.labels` map (confirmed present, more than assumed) makes the
  same matching *possible* against stored data, if the selectors themselves
  are made available centrally.

Confirmed direction on all three open questions before implementing:
1. Solve cluster attribution via the log **pipeline** (tag at the source),
   not by trying to map a human-readable name after the fact.
2. A new trusted-internal-caller path on the Hub, rather than have a
   central miner hold custody of every cluster's own `CollectorToken`.
3. Reuse precise Service-selector matching (not a `labels.app` heuristic),
   by having Discovery push selectors centrally alongside its existing
   inventory push.

## Decision

Four phases, each independently shippable and testable:

**Phase 1 — tag Glue logs with `cluster_id` at the source (infra only).**
`infra/kubernetes/fargate-logging/aws-observability-configmap.yaml.tpl`
gains a second Fluent Bit filter,
`record_modifier` stamping this cluster's own Hub `Integration.ID` — never
a human-readable cluster name — onto every log record:
```
[FILTER]
    Name           record_modifier
    Match          *
    Record         cluster_id ${cluster_id}
```
`scripts/onboard_cluster_logging.sh` gains a required `<cluster-id>`
argument (the operator's own Integration.ID, not auto-looked-up — an
Integration's `Name` is a free-text label with no guaranteed
correspondence to a Terraform cluster-key, so guessing it would be more
fragile than asking). The Glue table (`logging.tf`) gains one more
top-level string column, `cluster_id`. Sidesteps the original
name-to-ID-mapping problem entirely by never introducing a name in the
first place. No application code depends on this phase; it only
back-fills going forward (rows captured before onboarding stay untagged).

**Phase 2 — Discovery pushes Service selectors centrally.**
`src/adapters/discovery/main.py`'s `_namespace_services` (renamed from
`_namespace_service_names`) now returns each Service's full `{"name":...,
"selector":{...}}` dict instead of just its name — data
`list_services(ns)` already had on hand (used for the collector's own live
`_service_for_pod` matching), previously discarded after the active/
inactive check. `inventory.py`'s `push_inventory` payload carries the
richer shape. Hub-side: `cluster_services` gains a `selector JSONB`
column (`ServiceEntry{Name, Selector}` replaces the bare `[]string` in
`UpsertClusterServices`); `namespaceInventory`'s wire type
(`serviceInventoryEntry`) accepts either the new `{"name","selector"}`
object or a bare string for backward compatibility with an older
Discovery build. New `GET /api/cluster-service-selectors?cluster_id=&namespace=`
(`ListClusterServiceSelectors`) — unlike `ResolveServiceClusters`/
`ListClusterServices`, this filters by an explicit `cluster_id`: a service
name can mean a different Service, with a different selector, in each
cluster, so there is no cross-cluster merge that would make sense here.

**Phase 3 — Hub accepts an explicit `cluster_id` from a trusted internal
caller.** `HandleServiceDependencyUpsert`'s request body gains an optional
`cluster_id` field, honored only as a fallback:
```go
if clusterID == "" && req.ClusterID != "" {
    clusterID = req.ClusterID
}
```
A real Discovery collector's own `CollectorToken`-derived `clusterID`
always resolves first and is never overridden by a stray body field. Only
a caller presenting no credential — the Agent, already on the same
trusted, unauthenticated boundary it calls every other Hub endpoint over
(same pattern `HandleLiveFetch`'s own docstring documents) — gets to
specify `cluster_id` explicitly. No new credential type, no new auth
middleware.

**Phase 4 — the miner (`src/agent/k8fy/dependency_miner.py`).** A periodic
background task (`asyncio.create_task` from `app.py`'s `startup_event`,
graceful shutdown via a matching `shutdown_event`), ticking hourly by
default (`DEPENDENCY_MINING_INTERVAL_SECONDS`, matching Glue's hour-level
partition granularity so each cycle scans exactly the newly-landed
partition). Per cycle: `GET /admin/integrations` enumerates registered
fleet clusters and their onboarded namespaces; for each
`(cluster_id, namespace)`, `GET /api/cluster-service-selectors` supplies
both the known-services set and the selector map (its keys already are
the known services — no separate fetch needed); an Athena query
(`boto3`, `start_query_execution`/poll/`get_query_results` — same
start-poll-fetch shape as `log_platform.py`'s `query_athena_logs`, a
deliberately separate copy rather than a shared import, same spirit as
`service_topology.py`'s two independent agent/discovery copies) selects
`kubernetes.pod_name, kubernetes.labels, log` filtered by the new
`cluster_id` column, `kubernetes.namespace_name`, and the current
partition window. Each row's raw CRI `log` line is parsed to its message
text (`_parse_cri_message`); `kubernetes.labels` — rendered by
Athena/Presto as `"{k=v, k2=v2}"`, not JSON — is parsed by
`_parse_athena_map` (a known simplification: good enough for flat K8s
label strings, not a general Presto-map parser). Rows are grouped by pod;
each pod's labels resolve to a `from_service` via `_service_for_labels`
(ported from `main.py`'s `_service_for_pod`, fed the stored selector map
instead of a live Service list); `extract_service_mentions` — reused
verbatim, zero changes, the actual "does this log mention service X"
logic already duplicated identically between the agent and Discovery — is
run against each pod's aggregated message text. Each discovered edge is
pushed via `POST /api/service-dependencies` with the new explicit
`cluster_id`, no bearer token, at most once per cycle (evidence
accumulates cluster-side across cycles via `evidence_count`, so a
duplicate push within one cycle is pure waste, not a correctness issue).
One `(cluster, namespace)` failing (Athena error, fetch error) never
blocks the others — same log-and-continue discipline as every other
`push_*`/`_scan_*` function in this codebase.

**The merge stays deterministic, not LLM-synthesized — same principle ADR
0028 establishes for its own fan-out.** `context-mesh/policies/
correlation.md`'s LLM-synthesis guidance governs combining a service's own
signals (health/certs/events) before a diagnostic Claude call; mining
"does log text mention another known service" is a plain extraction task,
already proven out as a pure function — no Claude call belongs anywhere in
this pipeline.

**No frontend changes.** No agent tool or UI currently consumes cross-
cluster dependency edges directly — `resolve_service_clusters`/`fetch_
service_dependencies` (already used by every Pattern-A skill's prefetch)
transparently see richer data once this miner starts pushing, with zero
changes to those call sites.

## Amendment (2026-09-01) — the frontend, and the matching rule

Two statements above were true when written and are no longer.

### "No frontend changes" is no longer true

That claim held only because nothing consumed the edges directly. A review
surface now exists: a **Dependencies** tab (`TopologyPanel.tsx`) reading the
already-shipped `GET /api/service-dependencies`, with a left-to-right layered
flow diagram (`DependencyFlow.tsx`), per-service focus lists, a table, and a
Mermaid export. No backend, storage or miner change was needed — which is the
evidence that this ADR's read path was in fact adequately factored.

Worth recording because the first version of that panel was a **table with no
diagram**, on the argument that a node-link view degenerates into a hairball.
Reviewed against real data the argument was wrong: at five edges the table was
unreadable, because "what calls what" is a shape. The scale concern was
genuine, so it is handled by *degrading* — one hop when a service is focused,
and a refusal to draw past 24 nodes / 60 edges — rather than by declining to
draw. Documented in [docs/SERVICE_DEPENDENCIES.md](../../docs/SERVICE_DEPENDENCIES.md).

### The matching rule now accepts bare service names

Phase 4 describes `extract_service_mentions` as "reused verbatim, zero
changes." It has since changed, in a way that applies to all three miners at
once (the Glue miner imports it rather than reimplementing it — the reuse this
ADR chose is what made a one-place fix possible).

The function matched only `<service>.<namespace>[.svc.cluster.local]`. Almost
nothing in a cluster logs that: Kubernetes resolves a short name through the
pod's search domain, so real callers write `http://agentify-backend:8080`.

**The failure this hid is the point.** `payments` reported five edges and
looked like a working subsystem — but those existed *only* because its test
workloads were written to log FQDNs deliberately. The miner was being
validated against a fixture built to satisfy it. `agentify` and `vault`, which
call each other constantly, reported zero. Same shape as this project's
tracing and embedding bugs: a component reporting success while observing
nothing, found by running it against reality rather than by reading it.

A bare name now counts, but **only in a hostname context** — immediately after
`//`, or immediately before `:<port>`. Without that restriction a Service
named `payment` would match the word "payment" in log prose. Validation
against the live Service list remains the second guard, and a bare name is
checked against the *scanned namespace's own* list, which is precisely what
the search domain does. The boundary is pinned by a test that must keep
failing for the loose version.

Redaction was verified to run before extraction without destroying the
signal: URL userinfo is rewritten (`//user:pass@host` → `//user:***@host`) but
host and port survive, so the edge is still found.

**Cost accepted:** `name:port` is a slightly weaker signal than an FQDN — a
log line like `payment:8080` in prose would match if `payment` is a real
Service. Judged acceptable against the alternative, which was a graph blind to
the form nearly every real caller uses.

## Consequences

- **Positive:** service-dependency edges are no longer limited to what a
  live per-cluster scan happened to sample that cycle — a cluster that
  ships logs to the shared Glue destination but isn't (yet, or ever)
  onboarded for live Discovery scanning still contributes edges.
  `resolve_service_clusters` (the lookup ADR 0028's fan-out mechanism
  depends on) gets more complete, automatically, with zero changes to that
  ADR's own code.
- **Positive:** the cluster-identity gap this ADR fixes was solved
  structurally (tag at the source) rather than by building a fragile
  name-to-ID lookup after the fact — there is no name to reconcile.
- **Negative / cost accepted:** `_parse_athena_map`'s map-string parsing is
  a simplification — a label value containing a literal `"="` or `", "`
  would be parsed incorrectly. Acceptable for K8s labels in practice (RFC
  1123-constrained values), not acceptable as a general Presto-map parser
  if reused elsewhere.
- **Negative / cost accepted:** rows captured before a cluster is
  onboarded to Phase 1's tagging filter carry no `cluster_id` and are
  invisible to this miner (not an error — they simply never match any
  `cluster_id = '...'` filter). Same "reflects state going forward, no
  backfill guarantee" convention as this project's other inventory-style
  data.
- **Negative / cost accepted:** an hourly cadence means a genuinely new
  dependency can take up to an hour to first appear via this path — live
  Discovery mining (same-cycle, ~60s default) remains the faster signal
  for freshly-observed edges; this miner's value is breadth (every
  onboarded cluster shipping to Glue) and resilience (survives a cluster's
  live scan being paused or misconfigured), not latency.
- **Revisit if:** Glue query volume/cost grows enough that per-namespace
  querying (one Athena query per `(cluster, namespace)` per cycle) needs
  consolidating into fewer, broader queries; or a mechanism for resolving
  "which cluster is pod X in" is ever built for ADR 0028's pod-specific
  fan-out limitation, which could also simplify parts of this miner's
  selector-matching need.
