# 0032 – Expected-failure annotation for dependency health

## Status

Accepted   ·   (date: 2026-09-13)

## Context

The health-weighted graph (built on ADR 0031's outcome capture) surfaced a
real finding: `payment-batch → payment-worker` at 100% failure, banner-flagged
as unhealthy. Investigation confirmed the finding is correct — `payment-worker`
is a synthetic test fixture (`infra/kubernetes/payments-test/payment-worker.yaml`)
that never listens on any port at all (`ss -tln` inside the live pod shows zero
listening sockets); every call to it genuinely fails. Its own inline comment
explains this is deliberate: "an unreachable dependency is still a dependency,
and that is exactly the case an operator most wants surfaced" — it exists to
give the mining/health pipeline a guaranteed real failing edge to detect.

The follow-up question was general, not about that fixture: when a *real*
deployment has a destination that is legitimately expected to fail or be
unreachable by design, there was no way to say so. An Explore agent confirmed
nothing in the codebase does this: `kind: "declared"` (`DependencyFlow.tsx`,
the only existing edge-exclusion mechanism) means "no observation data
exists," not "data exists and is expected to be bad" — repurposing it would
misrepresent real failure evidence as an edge with none. There is no DB
column, no admin endpoint, and no K8s annotation read anywhere in the mining
pipeline for this.

## Decision

**A Kubernetes annotation on the destination Service:**
`agentify.io/expected-failure: "<free-text reason>"`. Non-empty means "calls
to this service are expected to fail/be unreachable by design — exclude
edges targeting it from the unhealthy banner." Chosen over an admin-editable
DB flag (would move the source of truth off the K8s manifest, cutting against
this repo's "config externalized" convention) and over a namespace-level
exclusion (too coarse — would hide a genuine incident in that namespace too,
not just the one expected failure).

**It rides the existing `cluster_services` inventory pipe — no new one.**
`src/adapters/discovery`'s scan loop already calls `k8s_client.list_services()`
once per namespace per cycle and pushes a per-service profile
(`_service_profiles()` in `main.py`) via `POST /api/cluster-inventory`,
stored as `cluster_services` (`ServiceEntry`/`UpsertClusterServices` in
`postgres.go`) — the same pipe already carrying `image`, `workload_kind`,
`replicas_ready`, etc. Reading the annotation here rather than building a new
path means:

- **Single writer.** Only the discovery adapter's live scan ever sets this
  field. The Glue-based miner (`dependency_miner.py`) and the diagnose-skill
  miner (`service_topology.py`) needed **zero changes** — neither has live
  K8s API access, and neither needs it for this.
- **Self-reconciling, no staleness logic to design.** `UpsertClusterServices`
  already does a full delete-then-insert per `(tenant, cluster)` on every
  push. Annotate a Service → it appears on the next scan cycle. Remove the
  annotation → it disappears on the next scan cycle. No "empty means unknown
  vs. empty means cleared" ambiguity, because there is exactly one source of
  truth per cycle.
- **No `service_dependencies` schema change, no new upsert parameter.** The
  reason is looked up by `to_service` name at *read* time
  (`ListServiceDependencies`'s `LEFT JOIN cluster_services ... ON
  cs.tenant_id = sd.tenant_id AND cs.cluster_id = sd.cluster_id AND
  cs.namespace = sd.namespace AND cs.service = sd.to_service`), never
  duplicated onto every edge row at write time. One value per service,
  however many edges target it, always current.

**The frontend only suppresses the alert, never the data.**
`unhealthyEdges()` (`DependencyFlow.tsx`) additionally filters out edges
whose `expected_failure_reason` is set. `edgeHealth()` itself is unchanged —
the edge still renders failing/degraded in its real color, the table
(`TopologyPanel.tsx`'s `HealthCell`) still shows the real `N/M failed` counts
plus a `(known issue)` marker, and the tooltip explains the reason
(`"146/146 failed — known issue: <reason>"`). An operator inspecting the
specific edge is never left wondering why it's colored but missing from the
banner.

**`payment-worker` is deliberately NOT annotated.** Its whole purpose is to
demonstrate genuine failure detection — annotating it away would defeat that
fixture's test. A future reader should not "fix" this by adding the
annotation there.

**Accepted scope limit: per-service, not per-edge.** A Service can only be
marked "expected to fail" for *all* callers, not for one specific
caller→callee edge. `payment-worker` itself illustrates why this is
sometimes exactly wrong to apply broadly (it's fine as a genuinely-failing
destination for the one fixture caller that's testing it) — but per-edge
granularity would require a materially different design (the annotation
would need to name the caller too, which doesn't fit cleanly on a Service
object). Deferred until a real case demonstrates the coarser grain is
insufficient.

## Consequences

- **Positive:** operators can now say "this failure is intentional" without
  lying about the underlying evidence — the banner (the thing meant to
  demand attention) respects it, while the graph, table, and raw counts
  (the thing meant to be trustworthy) never do.
- **Positive:** zero changes needed to either of the two miners without live
  K8s access (Glue-based and diagnose-skill) — the design concentrates all
  new logic in the one component (`discovery`'s live scan) that already both
  reads Service annotations and owns the relevant registry table.
- **Negative / cost accepted:** the annotation only takes effect on the next
  discovery scan cycle, not immediately — the same staleness bound every
  other `cluster_services`-derived fact already carries (matches, e.g., how
  a `replicas_ready` change is not instant either).
- **Negative / cost accepted:** per-service, not per-edge (see above) — a
  service that is a mostly-fine destination for most callers but genuinely
  broken for one specific caller cannot distinguish that case from this
  annotation alone.
- **Revisit if:** a real (non-fixture) case needs per-edge granularity, or
  the annotation's free-text reason needs to become structured (e.g. an
  expiry date, a ticket reference field) rather than a single string.
