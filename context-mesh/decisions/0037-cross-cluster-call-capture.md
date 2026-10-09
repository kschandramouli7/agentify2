# 0037 – Cross-cluster call capture (ROADMAP P31 phase 1)

## Status

Accepted   ·   (date: 2026-10-09)

## Context

A CPaaS-to-CPaaS datacenter migration moves applications from one
OpenShift cluster to another inside the same fleet. Operators cannot
confidently decommission a migrated app's old-cluster footprint today
partly because agentify has no way to see genuine cross-cluster traffic at
all: a call from one cluster to another gets checked against nothing (the
caller's own Service list and namespace registry both live in a different
cluster) and, lacking any validated classification, falls into the
`external` bucket — which has been disabled by default since 2026-09-05
for fabricating dependencies from access-log noise (a scanner's
User-Agent, a third-party error body's own docs URL). Decommissioning
decisions built on top of a graph with this gap would be blind exactly
where a migration needs it most.

## Decision

A **new `target_kind = "cross_cluster"`**, distinct from `external`,
validated against a ground truth `external` never had: `cluster_ingress_endpoints`
— the fleet-wide table of every onboarded cluster's own OpenShift
Route/Ingress/HTTPRoute host mappings (already mined for ROADMAP P18 use
case #3). A hostname mentioned in one cluster's logs that matches a REAL
Route/Ingress entry in a *different* cluster in the same tenant's fleet is
not a shape guess — it is checked against a real object, the same bar this
codebase has enforced since the 2026-09-05 incident where a trace UUID
followed by a real namespace passed as a service call. That bar is exactly
why `cross_cluster` can stay on unconditionally (like `cross_namespace`
already does) while plain `external` stays gated behind
`MINE_EXTERNAL_EGRESS`.

### Precision tiering

OpenShift Route entries are an exact 1:1 host→backend mapping
(`k8s_client.py`'s `list_routes` reads `.spec.host`/`.spec.to.name`
directly). Ingress/HTTPRoute entries are a known-lossy N×M cross product
(`ingress.py`'s own docstring: hosts and backends are deduped per object,
then paired by cross product, not true per-rule pairing). Rather than
inherit that lossiness into a brand-new trust tier, a new config knob
(`CROSS_CLUSTER_INGRESS_KINDS`, default `route` only) gates which entry-point
`kind`s are trusted — Ingress/HTTPRoute-sourced matches can be opted in
later once that lossiness is addressed, without blocking this feature.

### Two gaps found while designing, fixed as part of this

`IngressEndpoint` (the Go struct) and `ListClusterIngress`'s SELECT carried
no `cluster_id` at all — a latent gap in the P18 #3 surface, invisible
until "which cluster owns this host" became the actual question being
asked. Fixed by adding the column to both. The only existing index
(`idx_cluster_ingress_lookup`) was built for the opposite lookup direction
(`tenant_id, namespace, backend_service` → finding routes for a known
service); a reverse "what does this hostname resolve to" lookup needed its
own new index on `(tenant_id, host)`.

### Schema

```sql
ALTER TABLE service_dependencies ADD COLUMN IF NOT EXISTS target_cluster_id TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_cluster_ingress_host_lookup ON cluster_ingress_endpoints(tenant_id, host) WHERE host != '';
```
`target_cluster_id` is evidence *about* the edge (like the provenance
counters, ADR 0036) — no row-splitting, since it's a per-observation fact,
not a materially different edge. It is distinct from the existing
`cluster_id` column, which means "the cluster that OBSERVED/pushed this
row" (the caller's side) — conflating the two would have been wrong, not
just imprecise.

### Ambiguity: reported, never guessed

During a migration overlap, the same hostname can legitimately front two
clusters at once (a GSLB-style name mid-cutover). `ResolveIngressHost`
(Go) returns every distinct matching `cluster_id`, never picks one.
Discovery's extraction loop treats `len == 1` as resolved and `len > 1` as
"still promoted to `cross_cluster` (it IS validated), but
`target_cluster_id` stays `""`" — unattributed, not fabricated. The same
"never downgrade a correction" rule `target_kind` itself already follows
applies here too: a later ambiguous sighting cannot erase an earlier
resolved `target_cluster_id`.

### Prerequisite refactor: `UpsertServiceDependency` becomes a struct param

This feature's `target_cluster_id` would have been the Go upsert
function's 15th positional parameter (14 after ADR 0036's provenance
work). Since every call site — production and ~60 test call sites across
two files — needed touching regardless for the new field, this was the
point to refactor to a `ServiceDependencyUpsert` struct instead of
appending yet another positional string. The mechanical rewrite used
`gofmt -r` (a true AST-based rewrite, not sed) with the method's receiver
included as a pattern variable (`x.UpsertServiceDependency(...)`) — the
first attempt without a receiver variable matched nothing, since a bare
function-call pattern cannot match a method call on a selector expression.

### API surface

`serviceDependencyUpsertRequest` gains `TargetClusterID` (`json:
"target_cluster_id"`), same explicit-never-omitted sentinel convention as
every other optional field on this request. A new fleet-wide, tenant-only
endpoint, `GET /admin/ingress-lookup?host=&kinds=`, follows the exact
precedent `GET /admin/tracked`/`HandleTrackedEntities` already set
(resolves tenant only, answers across every cluster that tenant owns) —
and excludes the calling cluster's own id from the result, since a cluster
calling its own public ingress hostname is a self-loop, not a migration
signal.

### Python (Discovery only)

Only Discovery's live collector (`src/adapters/discovery/main.py`) ever
sets a non-default `target_kind` today — the Glue miner and the
opportunistic agent-skill miner always implicitly push `"service"`. This
feature therefore only needed wiring into Discovery's extraction loop; the
new `resolve_cross_cluster_target` function and config knob are mirrored
into `src/agent/k8fy/service_topology.py` per ADR 0029's duplication
convention, but not wired into that module's own `mine_service_dependencies`
— kept in sync for consistency, should that producer gain this capability
later. A per-scan-cycle cache (keyed by hostname, shared across every
namespace in one cycle) memoizes the lookup, so N pods across N namespaces
mentioning the same migrating host cost one `/admin/ingress-lookup` call,
not N.

### A real behavior/cost change, named explicitly

Today, with `MINE_EXTERNAL_EGRESS=false` (the default), an
external-shaped mention costs zero network calls — it's simply skipped.
After this change, every distinct external-shaped host per scan cycle
costs one memoized `GET /admin/ingress-lookup` call, **regardless of that
flag** — because whether a mention is genuinely cross-cluster can only be
known *after* the lookup. This is bounded (by distinct external-shaped
hosts per cycle, typically small) and best-effort (degrades to `[]` on any
failure, same as every other Discovery network call), but it is a real
cost-shape change, not a purely additive one.

### UI

`target_kind`'s TypeScript union widens to include `"cross_cluster"`
(`api.ts`), alongside a new `target_cluster_id?` field. `DependencyFlow.tsx`
gets a `cross_cluster` node/edge treatment mirroring `cross_namespace`'s
dashed-but-filled styling (not `external`'s hollow/dim one, since this
tier is validated, not guessed) and an edge tooltip naming the resolved
cluster or explicitly saying "unresolved" rather than staying silent.
`TopologyPanel.tsx`'s classification block trusts `cross_cluster` only
when the backend says so — deliberately **no shape-based fallback** the
way `cross_namespace`/`external` each have, since a cross-cluster hostname
is shape-indistinguishable from a plain external one; only the backend's
fleet-wide ingress lookup can tell them apart.

## Consequences

- **Positive:** genuine cross-cluster traffic is now visible and
  trustworthy, closing the exact gap that made "is anything still calling
  the old cluster" unanswerable for a migrating service.
- **Positive:** `cluster_ingress_endpoints`'s two pre-existing gaps
  (missing `cluster_id` on read, no host-reverse index) are fixed as a
  byproduct, benefiting any future consumer of that table, not just this
  feature.
- **Positive:** the `UpsertServiceDependency` struct refactor stops the
  function's positional parameter list from growing indefinitely — a
  future field is now one struct field, not a 16th positional argument
  and another full call-site sweep.
- **Negative / cost accepted:** Discovery's outbound call volume increases
  by one memoized lookup per distinct external-shaped host per cycle,
  unconditionally — a real, if bounded, behavior change from today's
  zero-cost skip when the egress flag is off.
- **Negative / cost accepted:** Ingress/HTTPRoute-sourced ingress entries
  are excluded from validation by default (`CROSS_CLUSTER_INGRESS_KINDS=route`)
  because of their existing lossy cross-product pairing — real
  cross-cluster traffic fronted only by an Ingress/HTTPRoute (no Route)
  will not be captured until that's opted in or the pairing fidelity is
  improved.
- **Revisit if:** a deployment has no OpenShift Routes at all (a
  vanilla-Kubernetes or pure Gateway-API shop) and needs Ingress/HTTPRoute
  trusted by default — the knob exists for exactly that, but the lossy
  pairing underneath it should be fixed first, not just trusted anyway.

## Amendment (2026-10-09) — widened the default to `route,ingress,httproute`

Exactly the "revisit if" condition above, reached sooner than expected: the
client's fleet is not OpenShift-only — business services deploy across
OpenShift, AWS EKS, and GCP GKE. OpenShift Route is an OpenShift-only CRD
(`route.openshift.io`); EKS and GKE clusters never produce one, under any
configuration. With the original Route-only default, a call crossing into
or out of either platform was structurally incapable of validating as
`cross_cluster` — not a tuning gap, a hard platform ceiling.

`cross_cluster_ingress_kinds`'s default (`src/adapters/discovery/config.py`)
widened to `["route", "ingress", "httproute"]`, accepting the
Ingress/HTTPRoute lossy N×M pairing fleet-wide by default rather than
per-operator opt-in. The lossy-pairing cost named above is unchanged and
still real; the fix did not improve that fidelity, it just decided the
alternative (zero cross-cluster visibility into two of the fleet's three
platforms) was worse. Revisit if the Ingress/HTTPRoute cross-product
pairing's false-positive rate in practice turns out to outweigh that
tradeoff — the fix then is improving `ingress.py`'s host/backend pairing
precision, not reverting the default.

Landed alongside platform-labeling (OpenShift/EKS/GKE detection and a
visual distinction on the diagram) — see `context-mesh/ROADMAP.md`'s P31
entry for that work's own detail. Platform-labeling's own `ListServiceDependencies`
JOINs to `cluster_health_snapshots` (RLS-enabled) surfaced the same class
of gap ADR 0035/0036 each already hit once: `TestServiceDependencyTenantIsolation`'s
restricted role had no `GRANT` on the newly-joined table, caught immediately
by running the real test suite rather than trusting a piped exit code —
fixed by granting `SELECT` and extending the test to seed distinct
platforms per tenant's cluster and assert neither leaks into the other's
read, not just that the grant unblocks the query.
