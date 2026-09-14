# 0022 – Multi-tenant Fleet Hub: push-based cluster reporting, reverses ADR 0009

## Status

Accepted · (date: 2026-08-02) — supersedes [ADR 0009](0009-tenancy-single-tenant-per-deployment.md) · Amended 2026-08-03 — see note after Decision #7; two wording corrections found while implementing the first real collector, agentify-discovery.

## Context

[ADR 0009](0009-tenancy-single-tenant-per-deployment.md) chose single-tenant-
per-deployment, but named its own reversal trigger explicitly: *"Revisit — and
supersede this ADR — if the go-to-market shifts to a shared multi-tenant SaaS
(many orgs, one deployment)... adopt row-level isolation — and do it before
the storage schema (P2b) hardens, because that is the expensive retrofit P3a
warned about."*

That trigger has now been pulled (2026-08-02): the target is a genuinely
multi-tenant **Fleet Hub** — many clusters, potentially belonging to
different customers/orgs, reporting into one shared backend. **Not a
separate/adjacent system:** since Decision #2 retrofits every existing
per-customer table, not just new ones, "the Hub" *is* the existing agentify
backend — collectors and the existing chat/diagnose/remediation UI are just
different API consumers of the same tenant-scoped Postgres, not two
different systems that happen to share a database.

This also reverses the hub/spoke topology [P16](../ROADMAP.md#p16--multi-cluster-connector-proposed-2026-07-21)/
[P17](../ROADMAP.md#p17--multi-cluster-access-for-the-live-diagnostics-tools-proposed-2026-07-25-superseded-2026-08-02)
were heading toward — one central agent **pulling** from N clusters (P17
exists specifically because that's hard: STS `AssumeRole` per cluster, since
an in-cluster ServiceAccount token only works for the cluster the agent pod
physically runs in). The new model inverts it: each cluster runs its own
lightweight, **deterministic, non-agentic** collector that **pushes** status
to the shared Hub. This sidesteps P17's hardest problem entirely — every
collector uses only its own cluster's already-built local RBAC
(`agent-live-diagnostics`-style Role); no cross-cluster credentials, no STS
role-per-cluster, no central credential sprawl.

**Honest cost callout, not glossed over:** ADR 0009 named the cheap window as
"before P2b hardens." That window is closed. [P2b](../ROADMAP.md) (Postgres
single-store, [ADR 0010](0010-postgres-single-store.md)) has been live since
2026-06-02, and eight tables have been added since: `integrations`,
`chat_sessions`, `remediation_proposals`, `incident_embeddings`, `traces`,
`current_state`, `events`, `service_dependencies`. This is a real retrofit
against an already-hardened schema, not the green-field version ADR 0009
anticipated — scope and time this accordingly, not as a quick flag flip.

## Decision

1. **Row-level tenant isolation: `tenant_id` + Postgres RLS.** This is the
   option ADR 0009 itself named as the answer for shared SaaS at real scale
   ("the rejected options above are the starting menu for that decision").
   RLS (`SET app.current_tenant_id = ...` per connection) is defense in
   depth — a query that forgets a `WHERE tenant_id = $1` clause still can't
   cross tenants, because the database enforces it, not just the query.

2. **`tenant_id` added to every table holding per-customer operational
   data:** `integrations`, `chat_sessions`, `remediation_proposals`,
   `incident_embeddings`, `traces`, `current_state`, `events`,
   `service_dependencies`. **Explicitly NOT added to `model_pricing`** —
   Anthropic's list prices don't vary per tenant; it stays shared reference
   data, same for every tenant.

3. **Tenant ≠ cluster.** A tenant (customer/org) can own a fleet of multiple
   clusters. `Integration` (`internal/models/integration.go` /
   `postgres.Integration`) gains `tenant_id` alongside its existing
   `Namespaces` — P16's cluster-level concept nests *under* the tenant
   boundary, it doesn't replace it. Two tenants can each have a cluster/
   namespace called `payments` without collision, because rows are
   disambiguated by `tenant_id` first.

   **`cluster_id` must propagate past `Integration`, into the operational
   tables too — `tenant_id` alone is not a sufficient disambiguator.** A
   single tenant's own fleet can easily have a namespace called `payments`
   in *both* its staging and prod clusters; `(tenant_id, namespace, ...)`
   would silently conflate those two clusters' rows into one. Every table
   in Decision #2 gains `cluster_id` alongside `tenant_id` (nullable only
   where no specific cluster applies) — the real isolation/uniqueness key
   is `(tenant_id, cluster_id, namespace, ...)`, not `(tenant_id,
   namespace, ...)`. `service_dependencies` needs this most urgently: its
   current unique key (`namespace, from_service, to_service`) would merge
   two different clusters' dependency graphs into one namespace's worth of
   evidence otherwise.

4. **Migration path for existing data:** `ALTER TABLE ... ADD COLUMN
   tenant_id TEXT NOT NULL DEFAULT '<default-tenant-uuid>'` on every table
   listed in #2 — same pattern as every other migration already in
   `postgres.go`. Today's single deployment's data becomes "tenant #1"
   automatically; the `DEFAULT` is dropped once every write path passes a
   real `tenant_id` explicitly (no write path should silently rely on the
   default past initial rollout).

5. **Tenant identity is resolved once, in middleware — never per-handler.**
   Every request — frontend session, chat, and the new spoke-collector push
   endpoints — resolves `tenant_id` from its credential (session/JWT for the
   frontend and chat UI; a per-tenant-per-cluster bearer credential for
   spoke collectors, extending the existing `Integration.Token` pattern) via
   one shared middleware, before any handler logic runs, then sets it as the
   RLS session variable for that DB connection. A handler that forgets to
   filter by tenant is still safe (RLS catches it); a handler that never
   resolves a tenant can't run at all.

   **Credential *mechanism* vs. credential *storage* — kept separate,
   deliberately.** The mechanism (a bearer token or mTLS client cert held by
   the collector) is plain and cloud-agnostic by construction — it has to
   be, so the same collector build works on GKE/AKS/on-prem/k3s, not just
   EKS. Where the Hub keeps its own copy for issuance/rotation (AWS Secrets
   Manager, HashiCorp Vault, whatever the Hub's own deployment already uses)
   is a Hub-side operational detail, not a portability requirement on the
   collector — don't let "the Hub happens to run on AWS" leak into "the
   collector requires AWS." This credential now genuinely crosses a trust
   boundary between organizations (unlike today's single dev-cluster bearer
   token), so on the Hub side it should move off plaintext Postgres storage
   onto a proper secret store regardless of which one — P16 already flagged
   this exact gap for the pre-multi-tenant case; it's materially more
   important now.

6. **The per-cluster collector is deterministic, not agentic — and built
   for genuine cross-distribution portability from the start, not EKS
   first with portability bolted on later.** Replaces P17's "central agent
   + STS `AssumeRole` per cluster" with a lightweight K8s-native component
   (one long-running Deployment per cluster — no Claude/LLM calls) that
   uses that cluster's own local RBAC to mine/collect data and push it to
   the Hub's tenant-scoped ingest API. See the concrete collector/ingest
   design in [ROADMAP P18](../ROADMAP.md#p18--deterministic-per-cluster-fleet-collector--multi-tenant-hub-ingest-proposed-2026-08-02-revised-2026-08-02-replaces-p17).

   **Default log source is the standard K8s pod-logs API**
   (`GET /api/v1/namespaces/{ns}/pods/{pod}/log` — the same call
   `live_diagnostics.py` already makes), not the Athena/Glue pipeline.
   Athena is explicitly EKS/Fargate-specific test scaffolding per
   [ADR 0021](0021-log-platform-test-infra.md) — it doesn't exist on GKE,
   AKS, on-prem, or even EKS-on-EC2. It stays available as an *optional*
   enhancement for whichever specific fleet members happen to have that
   AWS-native log-shipping infrastructure, never the required path.
   `service_topology.py`'s existing extraction logic
   (`extract_service_mentions`) is reused as-is against whichever log
   source the collector actually has — the mining logic doesn't get
   rebuilt, just re-hosted and given a portable default input.

   **Portability requirements to bake in from day one, not retrofit
   later:**
   - **API-capability discovery at startup** — query `/version` and
     `/apis`, never assume a fixed surface. OpenShift has `Route`/SCCs
     instead of Ingress/PSPs; Fargate has no DaemonSets; older clusters
     lack newer API groups.
   - **RBAC-only for in-cluster reads, no cloud-IAM assumption** — the
     existing `agent-live-diagnostics`-style Role is already portable;
     it stays the *only* thing required to run. Cloud-specific credentials
     belong only to the outbound Hub connection (Decision #7), never to
     reading the local cluster.
   - **Ingress/Gateway-API/Route-agnostic entry-point mapping** — check
     for `Ingress`, `Gateway`/`HTTPRoute`, and OpenShift `Route`; degrade
     gracefully if a given CRD isn't installed, rather than assuming one.
   - **`metrics-server` is optional, not assumed** — capacity signals
     degrade to "unavailable" rather than erroring if it isn't installed.
   - **Service-mesh awareness is opportunistic only** — if Istio/Linkerd
     CRDs are present, use their richer graph as a bonus higher-fidelity
     signal; never require a mesh to function.
   - **Cluster identity is injected at onboarding, never auto-detected** —
     there is no portable "what cluster am I" K8s API; `cluster_id` (and
     `tenant_id`) are set via ConfigMap/env at deploy time, not inferred.
   - **Redaction runs at the collector, before anything leaves the
     cluster** — reuse `log_redaction.py` as-is; don't rely on the Hub to
     clean up after ingest, especially now that fleet members may belong
     to different, less-trusted tenants.
   - **API version-skew tolerance** — try the modern API group first,
     fall back to deprecated ones, the way any client meant to run across
     a multi-year-old-to-brand-new fleet must.

7. **On-demand live drill-down into a remote tenant's cluster is NOT
   automatically solved by push alone — named explicitly, not silently
   dropped. And it must be outbound-only, not an inbound callback.**
   Periodic push gives the Hub whatever was last reported; an operator
   asking chat "show me live pod status in cluster B right now" needs
   something more. The original framing of this decision had the Hub
   dialing *into* the collector on demand — that only works when the Hub
   can open a new inbound connection to the cluster, true for EKS behind a
   public ALB, false for most real clusters (on-prem, NAT'd, private VPC,
   behind a corporate firewall). **Corrected resolution:** the collector
   initiates and holds open a persistent *outbound* connection to the Hub
   (a long-lived HTTP/2 connection or gRPC bidi-stream) — the same pattern
   every SaaS-facing K8s agent actually uses (Datadog's cluster agent,
   Teleport, etc.) for exactly this reason. Both the periodic push *and*
   the Hub's on-demand "fetch X now" requests flow over that one
   already-established outbound connection. The Hub never dials into a
   cluster; it only ever receives connections and sends messages over ones
   already open. This still reintroduces a *little* of P17's original
   shape — the Hub can ask a specific collector for something on demand —
   but only as "the Hub sends a message over a connection one specific
   tenant's one specific collector already opened," never "one central
   agent holds standing credentials for every cluster in the fleet."

**Amendment (2026-08-03), found while building the first real collector,
[agentify-discovery](../../src/adapters/discovery/):**

- **Decision #6's "`cluster_id` (and `tenant_id`) are set via ConfigMap/env
  at deploy time" is corrected — no separate `CLUSTER_ID`/`TENANT_ID` env is
  needed.** By the time this was actually implemented, phase 2 of the
  multi-tenancy migration had already built `resolveTenantContext`
  (`src/backend/internal/api/handlers.go`), which derives `clusterID`
  server-side from *which* `Integration` row the presented
  `COLLECTOR_TOKEN` matches (`integ.ID`). The collector only needs its own
  token — a second, separately-injected cluster identity would be
  redundant and could drift from the Hub's own record. "Injected at
  onboarding, never auto-detected" still holds; it happens at the moment an
  admin creates the `Integration` row and mints its `CollectorToken`, not
  via a second config value on the collector itself.
- **`service_topology.py`'s original `get_known_services()` calls the Hub's
  `GET /admin/tracked` — confirmed to have no tenant scoping.** Reusing it
  unchanged from a multi-tenant collector would leak every tenant's known
  service names into another tenant's extraction-validation set (a
  cross-tenant information leak, not just an isolation gap — the leaked
  names never get stored, but a collector could use them to fabricate
  false-positive edges). `agentify-discovery` instead lists K8s `Service`
  objects directly from its own cluster
  (`GET /api/v1/namespaces/{ns}/services`) to build its known-services set
  — more correct, and also more portable (no dependency on the k8fy-adapter
  having run first to populate `/admin/tracked`).

8. **Coupled decisions this ADR does NOT resolve — flagged, not silently
   punted:**
   - [ADR 0008](0008-multi-provider-model-routing.md) (multi-provider
     routing) treats provider/region/credentials as per-deployment config.
     Under a shared Hub, does each tenant need its own Anthropic key/BYOK,
     or does the Hub use one shared key with usage attributed per-tenant for
     cost allocation? **Not resolved here** — required follow-up before this
     ships to real multi-tenant customers.
   - [ADR 0007](0007-egress-data-governance.md) (redaction) stays a
     Hub-wide policy for now, not per-tenant-configurable. Revisit if a
     tenant contractually requires custom redaction rules.

9. **Relationship to the existing k8fy-adapter — flagged as a
   consolidation opportunity, not resolved here.** The adapter
   (pull-configured via `Integration.AdapterURL`, continuously watches K8s
   and POSTs events to populate `current_state`/`events` for Tier-1
   deterministic health/cert checks) and the new fleet collector have
   overlapping concerns: both run per-cluster, both need K8s RBAC read
   access, both report state to the central backend. Ending up with two
   separate per-cluster components with adjacent responsibilities is a
   real smell — the natural target is one per-cluster deployable doing
   both jobs, one RBAC ServiceAccount, one thing to operate per cluster.
   Not resolved here: merging them means migrating every existing adapter
   deployment, which is its own decision with its own migration plan, not
   something to fold in casually alongside this ADR.

**Amendment (2026-08-03), found while building Decision #7's on-demand
drill-down (ROADMAP P18 use case #9, `agentify-discovery`'s
`live_relay.py`/`live_tools.py` + the Hub's `CollectorHub`):**

- **Periodic push does NOT move onto the persistent connection.** Decision
  #7's text describes "periodic push and the Hub's on-demand requests" both
  flowing over the one outbound connection. In practice, use case #9 was
  built as a **second, separate** WebSocket purely for on-demand
  request/response traffic — `inventory.push_inventory` and
  `service_topology.push_dependency` (use cases #1/#2) stay exactly as
  shipped, plain HTTP POST. Migrating already-working, already-tested push
  code onto the new channel bought nothing for this slice and would have
  been pure churn; the two concerns (fire-and-forget periodic reporting vs.
  request/response drill-down) have different failure modes and don't need
  to share a transport just because the ADR originally imagined they would.
  Revisit only if running two connections per cluster becomes an actual
  operational cost worth collapsing.
- **`cluster_id` is supplied by the caller, not resolved automatically.**
  The Hub's `POST /api/live-fetch` and the agent's `live_*` tool schemas
  require an explicit `cluster_id` (an `Integration.ID`, listed via the
  existing `GET /admin/integrations`). This item does not build "which
  cluster is service X in" resolution — that's [P16](../ROADMAP.md#p16--multi-cluster-connector-proposed-2026-07-21)
  (multi-cluster connector), separately proposed and not started. Chat can
  ask "show me pods in cluster `cluster-42`" today; it cannot yet ask "show
  me pods for `payment-api`" and have that resolve to the right cluster on
  its own.

## Amendment (2026-09-13) — closing the current_state RLS gap, and what it actually took

Decision #2 above lists `current_state` among the tables that should get
`tenant_id` + RLS. It got the column; the RLS half was left as a "query-
retrofit-phase decision" and was never finished (flagged live as ROADMAP
OPS-10, filed as a routine chore alongside two unrelated one-line fixes).

**Why this needed its own amendment, not just a migration.** Postgres RLS
with `FORCE` applies per-role to *every* query against a table, regardless
of which Go method issues it — there is no way to enable it "partially."
Four code paths touched `current_state`, and none of them set the
`app.current_tenant_id` session variable the policy checks: `CurrentState
.Store` (had the tenant on hand, just never used it), `Client
.ListServiceHealth` (already filtered by tenant manually, but not via RLS),
`CurrentState.TrackedEntities` (no tenant filtering at all, not even the
manual-WHERE-clause kind), and — the one that changed the scope of this fix
— `CurrentState.Query`, reached from **`HandleQuery` (`POST /api/query`, the
primary chat/ask endpoint)**, whose own code comment said outright it was
"not cluster-scoped." Turning on `FORCE ROW LEVEL SECURITY` without fixing
that path first would have made every pod/service-state answer through the
main query path start silently returning empty results — the core product
feature breaking, not just a security posture improving.

**Decision: do the full retrofit, including `/api/query`.** Confirmed
explicitly rather than assumed, given the size difference from what OPS-10
implied.

- `storage.Backend.Query` (`internal/storage/backend.go`) gained a
  `tenantID string` parameter, server-resolved and passed explicitly — never
  read from the query body, since (unlike `Store`, whose `data` map is
  populated server-side by the ingester) `Query`'s `podQuery` is built
  directly from client-supplied `req.Context`, and putting `tenant_id`
  there would let a caller spoof it. `Store`'s signature was untouched for
  exactly this reason — it already carried a trusted value.
- `HandleQuery` and `HandleAgentFetch` now call the existing
  `resolveTenantContext(r)` helper every other handler already used — no
  `Authorization` header required (unchanged), only a garbage bearer token
  is newly rejected. Since no real multi-tenant frontend auth exists yet,
  every unauthenticated call already resolves to the same `DefaultTenantID`
  today, so this is **behaviorally a no-op for the current deployment**,
  not a functional change.
- `CurrentState.TrackedEntities` (`/admin/tracked`) gained both a
  `tenantID` parameter and an explicit `tenant_id = $1` predicate — it had
  neither before, despite aggregating the whole table with no `pod_id` key
  at all, the widest blast radius of any current_state reader.
- Background sweeps with no inbound request to resolve a tenant from
  (`DeploymentGuardian`, `Investigator`, remediation's post-execution
  verification) now pass `pgstore.DefaultTenantID` explicitly, matching
  today's effectively-single-tenant reality rather than inventing a
  per-tenant background-sweep design this pass didn't need.
- `Client.Query` (events) accepts the same new parameter for interface
  compliance but does not enforce it — events has the identical missing-RLS
  gap current_state just had, but closing it is a separate, not-yet-decided
  item, not a side effect of this one.

**A real finding from testing this, not just implementing it:**
`current_state`'s `PRIMARY KEY (pod_id, entity_key)` does not include
`tenant_id`. Two different tenants writing the exact same `(pod_id,
entity_key)` pair collide at the schema level — the second write hits
`ON CONFLICT DO UPDATE` against a row RLS won't let it see, and errors
rather than coexisting. This is not fixed here: ADR 0024's `PodID` helper
already embeds `cluster_id` into every `pod_id`, and two different tenants
never share a `cluster_id` by construction, so this collision cannot occur
in practice — pod_id-based scoping was always current_state's real
isolation mechanism (see Store's own comment); RLS adds a backstop for the
aggregate reads (`ListServiceHealth`, `TrackedEntities`) that key off
namespace *names*, which genuinely can collide across two tenants'
clusters, rather than off pod_id, which cannot.

## Consequences

- **Positive:** unlocks the actual target architecture (one shared Hub, many
  tenants' clusters reporting in) instead of one-deployment-per-customer;
  RLS gives a real, database-enforced isolation guarantee instead of hoping
  every query remembers a `WHERE` clause; per-cluster collectors are cheap
  (no LLM cost per collection cycle) and need zero cross-cluster credentials,
  which was P17's whole problem to begin with; the collector's portability
  requirements (Decision #6) mean the fleet isn't accidentally locked to
  EKS the way the original Athena-first, STS-based design was.
- **Negative / cost accepted:** a real retrofit against an already-hardened,
  8-table schema, not the cheap early version ADR 0009 anticipated — every
  existing query across the codebase needs a tenant-*and-cluster*-scoping
  audit (Decision #3), not just new code going forward; the spoke↔hub
  credential model is new work (per-tenant-per-cluster tokens, rotated via
  whatever secret store the Hub's own deployment uses, per Decision #5);
  the outbound persistent-connection model (Decision #7) is real
  infrastructure to build and operate — connection lifecycle, reconnect/
  backoff, multiplexing push and on-demand traffic on one stream — not a
  simple request/response API; ADR 0008/0007's per-deployment assumptions
  need their own follow-up decisions, not automatically resolved by
  adopting this ADR; the k8fy-adapter/collector consolidation (Decision #9)
  is a real migration if and when it happens, not free.
- **Revisit if:** RLS session-variable overhead becomes measurable at real
  scale (worth benchmarking before assuming it's free); a tenant requires
  isolation stronger than row-level (e.g. a contractual demand for physical
  separation) — schema-per-tenant or a dedicated deployment becomes an
  exception path for that one tenant, not a change to the default.
- **Negative / cost accepted (2026-09-13 amendment):** `events` (`Client
  .Query`/`Store`) has the same missing-RLS gap `current_state` just had —
  deliberately not closed as a side effect of this fix, so it remains open
  until its own decision is made. `current_state`'s primary key still has
  no `tenant_id` component (see the amendment) — accepted because pod_id
  already makes the collision it would guard against impossible today, not
  because the schema is airtight on its own.

## Coupled decisions (supersedes ADR 0009's own list)

- **P2b (storage consolidation):** reversed — the Postgres spine is now
  multi-tenant: `tenant_id` + RLS on every per-customer table.
- **P17:** superseded by this ADR's push-based, deterministic-collector,
  tenant-scoped model — see its revised ROADMAP entry and the new
  [P18](../ROADMAP.md#p18--deterministic-per-cluster-fleet-collector--multi-tenant-hub-ingest-proposed-2026-08-02-revised-2026-08-02-replaces-p17).
- **P16:** not superseded, only revised — its namespace→cluster routing
  concern is unchanged in shape, now additionally scoped by `tenant_id`.
- **ADR 0008 / ADR 0007:** flagged as needing their own follow-up decisions
  (Decision #8), not resolved by this ADR.
