# 0033 – Deployment security posture, and a staged path toward active verification

## Status

Accepted   ·   (date: 2026-09-14)

## Context

[ROADMAP P30](../ROADMAP.md#p30--deployment-security-posture-staged-toward-active-verification-proposed-2026-09-14-phased-2026-09-14)
proposes the platform assess its own deployment's security posture —
NetworkPolicy coverage, RBAC surface, pod `securityContext`, TLS/exposure —
promoting [ADR 0022](0022-multi-tenant-fleet-hub.md)'s use case #8, deferred
since 2026-08-02. A 2026-09-14 audit found concrete findings (zero
NetworkPolicies anywhere in the repo, a cluster-wide Secrets-read ClusterRole,
a dev-mode Vault with a root token on a public ALB) proving the category is
real, not hypothetical.

The request that followed asked for two things **designed together, not
built together**: a panel that runs these checks and reports outcomes now,
and a deliberate path for this to grow into active security verification —
eventually a "penetration testing" territory in its own right. That second
half changes the shape of the schema and API this ADR has to specify, even
though only the first half is being built yet: retrofitting an authorization
model onto an already-shipped read-only scanner is exactly the kind of
rework a little foresight avoids (the same lesson OPS-10 relearned the hard
way for tenant scoping — decide the boundary before the first table exists,
not after).

**The core problem this ADR solves:** "posture scanning" (read config, infer
risk) and "penetration testing" (actively probe, verify, occasionally
exploit) are not two sizes of the same feature — they are different risk
categories requiring different authorization models, and conflating them in
one always-on scanner is how a config-reading tool ends up sending live
exploit traffic because a flag got flipped without anyone re-examining what
that flag actually does.

## Decision

**A four-phase ladder, one roadmap item (P30), each phase a materially
different trust boundary — mirroring how P27 already uses phases for one
initiative built incrementally:**

1. **Posture** (this pass, buildable now) — reads configuration state only:
   K8s API objects (NetworkPolicy, RBAC, pod specs, Ingress/TLS), IAM policy
   documents where already-granted access allows it. Zero network side
   effects. No approval gate — same trust level as the existing dependency
   miner, which already reads just as broadly.
2. **Active verification** (future) — narrowly-scoped, non-destructive live
   checks that confirm a *specific* Phase 1 finding is actually live, not
   just theoretically bad ("config says anonymous access is on — does an
   unauthenticated GET against the live endpoint actually succeed?").
3. **Exploitability verification** (future) — matches a detected version
   against known CVEs and attempts a scoped, non-destructive proof-of-concept
   to confirm exploitability rather than trusting a CVE number applies here.
4. **Full pentest orchestration** (future, the named long-term territory) —
   scheduled/on-demand campaigns, credential testing, lateral-movement
   simulation, with real rules of engagement.

**Phase 1 needs no new authorization model — it rides the existing
read-only boundary ([ADR 0003](0003-read-only-to-actions-boundary.md))
exactly. Phases 2 onward each require an explicit engagement record before
anything network-active runs, modeled directly on
[ADR 0020](0020-phase-3-remediation-with-approval-gate.md)'s
remediation-approval gate** — chosen over a simpler admin-only toggle
because active verification is a strictly higher-consequence action than
config-reading (it touches live systems, however carefully) and deserves
the same per-action, auditable, expiring authorization remediation already
has, not a standing "mode" that's easy to forget is on.

### Schema (Phase 1 buildable now; Phase 2+ shape decided now, not built)

**`security_findings`** — one row per (check, resource), accumulate-on-rescan
like `scan_coverage`, tenant/cluster-scoped with RLS **enabled from the
table's first migration** (unlike `current_state`'s original gap — ADR 0022's
Decision #2 pattern applied correctly this time, not retrofitted):

- `check_id` (e.g. `namespace-has-networkpolicy`, `clusterrole-secrets-scope`)
- `severity` (`critical`/`high`/`medium`/`low`)
- `resource` (namespace/kind/name the finding is about)
- `evidence` (what was actually observed — never a bare verdict)
- `status` (`open`/`acknowledged`/`resolved`)
- `verified_by_engagement_id` (nullable FK, populated once a Phase 2+
  engagement confirms — or refutes — the finding; see below)
- `confidence` (`config-only` until verified, `confirmed-live` /
  `refuted` after) — the same trust-tiering idea `service_dependencies`
  already uses for declared/observed/external edges, applied here to "how
  sure are we this is a real, exploitable issue" instead
- `first_seen`/`last_seen`

**`security_engagements`** (Phase 2+, schema specified now so Phase 1's
`verified_by_engagement_id` column has something real to point at later,
not a guess) — directly modeled on `remediation_proposals`:

- `id`, `tenant_id`, `cluster_id`
- `scope` (which namespaces/hosts/resources are in bounds — an engagement
  against a finding it wasn't scoped for must be rejected, not silently
  widened)
- `techniques_allowed` (which check classes this engagement authorizes —
  Phase 2's "confirm live" is a different, lower-risk grant than Phase 3's
  "attempt exploitation," and an engagement approves one, not "verification
  in general")
- `requested_by`, `approved_by` (two distinct identities — same
  propose/approve split ADR 0020 Decision #1 already established, for the
  same reason: the person who wants a check run should not be the only
  signature needed to run it)
- `status` (`pending`/`approved`/`active`/`expired`/`completed`/`rejected`)
- `expires_at` — a TTL bounding how stale an approval's context can be
  before execution, same reasoning as `REMEDIATION_PROPOSAL_TTL_MINUTES`
- `results` (what actually happened, linked back to the finding(s) it
  targeted)

**A finding can reference an engagement; an engagement always references
which finding(s) it targets.** There is no path for an engagement to run
untargeted "general" active checks — every Phase 2+ action traces back to a
specific Phase 1 finding it exists to verify.

### Execution boundary

**Phase 2+ execution must be a separate, network-isolated service, not a
mode inside the existing backend or agent process** — same reasoning
[ROADMAP P14a](../ROADMAP.md#p14a--remediation-executor-as-its-own-network-isolated-agent)
already used for the remediation executor: isolation-by-convention (a
function nobody calls yet) is a code-review guarantee, not an
infrastructure one, and active-verification code (things that open
connections to live systems, attempt authentication, run PoC exploit code)
is exactly the class of code most likely to itself carry a vulnerability or
be misused if the process running it is ever compromised. Least-privilege
separation between "reasons over data" and "holds active-testing
capability" is the same standard boundary P14a draws between "reasons over
data" and "holds write credentials."

**Phase 2+ dispatch must be invisible to every Claude-facing tool list**,
identical to ADR 0020 Decision #5's rule for remediation write tools —
reachable only via a deterministic, non-LLM path triggered by an approved
engagement, never as something a chat turn or a misinterpreted free-form
question could invoke. This matters more here than for remediation: a
prompt-injected "please verify this endpoint is exploitable" is a strictly
worse outcome than a prompt-injected restart, because it can affect systems
this platform doesn't even own if scope enforcement has any gap.

### What Phase 1 alone gets to skip

No engagement record, no separate service, no TTL — it reads config the
same way the dependency miner already does, through Discovery's existing
RBAC, with no new grant. The panel described in the roadmap item (a
"Security Posture" tab, findings table, "Run scan now" action) is entirely
Phase 1 scope.

## Amendment (2026-09-14) — Phase 1 shipped

Filled in the two things this ADR deliberately left open, rather than
another ADR: the concrete check list and how findings surface.

**Three checks, not the whole audit list that motivated P30.** Chosen
because two need zero new RBAC and one needs the smallest possible new
grant — proving the pattern (K8s read → check → finding → panel) without
asking for broad new cluster access in the same feature that flags other
ClusterRoles as too broad:

- `namespace-has-networkpolicy` (severity `high`) — the one check needing
  new RBAC: `networkpolicies` list/get in `networking.k8s.io`, added to
  `agentify-discovery`'s ClusterRole as its own narrowly-scoped rule.
- `pod-security-context` (severity `medium`) — flags a container missing
  `runAsNonRoot`, `readOnlyRootFilesystem`, `allowPrivilegeEscalation:
  false`, or a capability drop. Reuses the existing `pods` read; a real bug
  surfaced writing this check's own tests — `allowPrivilegeEscalation`'s
  *secure* value is `false`, not `true` like the other two fields, and the
  first implementation checked all three uniformly, flagging correctly-
  configured containers as findings. Caught before merge, not after.
- `ingress-missing-tls` (severity `critical`) — reuses the existing
  `ingresses` read, widened with one more field (`has_tls`) rather than a
  new call, since it's the same object already fetched.

**Explicitly deferred, not part of Phase 1:** the RBAC-surface scan
(Roles/ClusterRoles/Bindings) — the most sensitive new grant of anything in
the original audit — and anything CI/CD- or Terraform-visible (mutable ECR
tags, missing scan gate, EKS admin exposure), which aren't K8s-API-visible
and need a mechanism this phase doesn't build.

**Upsert semantics, decided in the implementation rather than left
ambiguous:** `UpsertSecurityFindings` treats a push as this namespace's
*complete* current truth. A finding already on record has `evidence`/
`severity`/`last_seen` updated in place; `first_seen` never resets across
cycles. A finding missing from a push resolves (`status='resolved'`, never
deleted — history survives a fixed issue). A `resolved` finding that
reappears flips back to `open` (a regression gets fresh attention); one
already `open` or `acknowledged` is left as-is, so acknowledging a
still-present issue survives the very next scan cycle finding it again. A
namespace whose check run partially fails is not pushed at all that
cycle — a partial finding set would look to the Hub like "these issues are
now fixed," silently resolving findings from a check that simply didn't
run. Same "no edges rather than unvalidated ones" discipline the
cross-namespace miner already uses.

**Schema shipped exactly as sketched above** — `security_findings` with
RLS enabled from its first migration (no OPS-10-shaped gap this time),
`confidence`/`verified_by_engagement_id` present and inert.

**The panel** (`SecurityPosturePanel.tsx`) ships as scoped: namespace
picker, a stat row (open/critical/resolved counts), and a findings table.
No "run scan now" button in this pass — findings arrive from Discovery's
normal scan cycle, same as every other panel; an on-demand trigger is a
fast-follow, not part of this slice.

## Amendment (2026-09-14) — Phase 2 shipped: `ingress-missing-tls` active verification

Built the engagement infrastructure sized for phases 2-4 as decided above,
but wired up only one technique end to end — confirming
`ingress-missing-tls` is actually reachable over plaintext HTTP — per the
explicit scoping choice made when starting this work.

**`security_engagements` shipped exactly as sketched, with two
simplifications the implementation earned the right to make:**

- **One `technique` string per engagement, not `techniques_allowed`.**
  Mirrors `remediation_proposals`' "one proposal, one action" shape rather
  than a multi-technique grant list — a richer scope model is deferred to
  whichever phase 3/4 technique first actually needs it, not designed
  speculatively now.
- **`target_check_id`/`target_resource_kind`/`target_resource_name`/
  `target_namespace` columns instead of a free-form `scope` object for
  "which finding this targets"** — the one-finding-per-engagement rule this
  ADR already committed to is enforced by foreign lookup
  (`GetSecurityFinding`) rather than interpreting an opaque JSON scope
  blob. `scope JSONB` still exists on the table for whatever a future
  technique needs beyond "which finding," currently holding just
  `target_host` (see below).

**RLS enabled from `security_engagements`' first migration**, same as
`security_findings` — confirmed by
`TestSecurityEngagementsTenantIsolation`, which proves a second tenant can
neither list, `GET` by ID, nor decide an engagement it doesn't own, even
when it knows the exact UUID.

**One schema addition this ADR didn't anticipate:** `security_findings`
gained a `target_host TEXT NOT NULL DEFAULT ''` column. Phase 1 had no
reason to record a checkable network address — it only ever read config.
Phase 2 needs to know *what host* an engagement should check, and the
finding itself (the thing that already knows which Ingress/Service is
implicated) is the natural place to carry that, rather than re-deriving it
at engagement-creation time or asking the operator to type it in. A finding
with no `target_host` (any check without a phase-2 technique mapped, or a
future check that simply doesn't resolve to a single host) cannot have an
engagement requested against it — `HandleSecurityEngagementCreate` returns
422, not a best-effort guess.

**Dispatch confirmed genuinely isolated, not just isolated-by-convention.**
`agentify-security-verifier` ([P14a](../ROADMAP.md#p14a--remediation-executor-as-its-own-network-isolated-agent)'s
pattern, but the *first* time it's actually been built — P14a itself is
still unbuilt for remediation) is a separate Deployment/Service with **no
RBAC grant at all** for phase 2's one technique (a plaintext HTTP GET needs
no K8s API access), reachable only via `ClusterIP` with no Ingress, and
`agentify-agent` has no network route to it — `SecurityVerifierClient`
lives entirely in the Go backend and is called directly, never through the
agent's HTTP surface, even as the deterministic non-LLM hop remediation's
own dispatch still uses. This is measurably stronger isolation than
remediation has today, not just a documented intention.

**Approve/reject share `checkSecurityEngagementAuth`'s fail-closed-outside-
dev logic from the first line of code**, not an amendment after shipping
open — the exact history `REMEDIATION_AUTH_TOKEN`/`EVAL_AUTH_TOKEN` both
have (ROADMAP OPS-2/OPS-3). An expired engagement (approved after
`expires_at`) returns 410 Gone and flips the record to `expired` rather
than silently dispatching a stale approval.

**Frontend**: `SecurityPosturePanel.tsx` gained a "Request verification"
column (shown only when a finding has a non-empty `target_host` and isn't
already `resolved`) and an engagement list section below the findings
table — pending cards with approve/reject, a collapsed history — mirroring
`RemediationPanel.tsx`'s card shape rather than inventing a new one.

**Explicitly not built:** phases 3 (exploitability/CVE verification) and 4
(full pentest orchestration) remain schema-ready, named future directions
only — `phase` and `technique` are already general enough to carry them,
but no technique, RBAC grant, or UI exists for either yet.

## Consequences

- **Positive:** the schema and authorization model for the whole ladder are
  decided once, before Phase 1 ships, so Phase 2+ is an additive migration
  (`security_engagements` + a `verified_by_engagement_id` FK) rather than a
  retrofit onto a Phase-1 schema that never anticipated verification —
  avoiding a second OPS-10-shaped fix later.
- **Positive:** the confidence-tiering on findings (`config-only` vs
  `confirmed-live`/`refuted`) gives Phase 1 a real payoff even before Phase
  2 is built — once it exists, a finding's confidence label is already the
  right shape to display, it just never upgrades past `config-only` until
  an engagement runs.
- **Negative / cost accepted:** Phase 1 ships with columns
  (`verified_by_engagement_id`, `confidence`) that do nothing until Phase 2
  exists — deliberate, not scope creep, but it does mean Phase 1's schema
  is slightly bigger than a standalone posture-only feature would need.
- **Negative / cost accepted:** this ADR commits to a real design cost
  before Phase 2 is scheduled — if the pentest direction is later
  deprioritized entirely, the `security_engagements` shape and the
  isolated-service requirement were designed for nothing built. Judged
  worth it given the explicit stated intent to grow this into its own
  focus area, not a hedge against an unlikely future.
- **Revisit if:** Phase 2 turns out to need per-check scope finer than
  "which resource" (e.g. rate limits, blast-radius caps distinct from
  remediation's) — extend `security_engagements`, don't redesign it; or if
  the pentest direction is dropped, in which case Phase 1 stands alone fine
  and the unused columns can be left inert rather than migrated away.
- **Positive (Phase 2 shipped):** `agentify-security-verifier` is the first
  genuinely network-isolated executor in this codebase — proof that the
  isolation P14a described for remediation is buildable, and a concrete
  template if P14a itself is ever picked up.
- **Negative / cost accepted (Phase 2 shipped):** `security_findings`
  needed one unplanned column (`target_host`) discovered mid-implementation
  — a small, backward-compatible addition, but evidence this ADR's schema
  sketch, however deliberate, still missed a real requirement until
  implementation surfaced it.
