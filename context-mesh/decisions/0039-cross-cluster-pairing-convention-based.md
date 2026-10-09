# 0039 – Cross-cluster pairing, convention-based (ROADMAP P31 phase 3)

## Status

Accepted   ·   (date: 2026-10-09)

## Context

ADR 0038's cold-services view can say "this service's evidence has gone
quiet," but during a migration that alone can't distinguish two very
different situations: the service was safely replaced by its equivalent
in the new cluster, or it's simply broken/abandoned with nothing picking
up its work. Telling those apart needs a notion of "this service in
cluster A is the same logical app as that service in cluster B."

Two ways to build that were considered up front: an explicit
`migration_mappings` table with pod-label-based confirmation and a
propose/confirm workflow (robust, but substantial new surface — a table,
an RLS policy, new admin endpoints, a confirmation UI), or a cheap
read-only query pairing same-named services across `cluster_id`s with no
schema change at all. **Decision, made explicitly before implementation:
convention-based only for this pass.** Ship the cheap version, validate
it against real migration data, and treat the label-confirmation/explicit-
mapping machinery as a deliberate, deferred follow-up rather than
over-building before the simpler approach has been checked against
reality.

## Decision

Pair services purely on **identical `(namespace, service)` name appearing
under more than one `cluster_id`** for a tenant, using `cluster_services`
(the fleet inventory registry, refreshed every scan cycle) as the ground
truth for "does this service exist in this cluster" — not
`service_dependencies`, which only proves something was called and is
exactly the lagging/leading signal this feature layers cold/warm
*evidence* on top of, not what decides membership.

Each side's cold/warm state reuses ADR 0038's exact predicate
(`coldServiceWhereClause`), not a re-derived one — the two queries'
definitions of "cold" are now structurally incapable of drifting apart
independently, since they're the same Go constant.

### No new chat intent

Unlike cold services (ADR 0038, which got its own Tier-1 intent),
`ListCrossClusterPairs`/`GET /api/cross-cluster-pairs` is **store-only for
this pass** — the same deliberate scope boundary `ClusterIngressStore`'s
own comment already states for P18 use case #3 ("no agent tool consumes
this yet"). Its only consumer is the cold-services UI toggle: when active,
the panel also calls this endpoint and annotates a cold service with
"⇄ N clusters" (tooltip: each cluster's cold/warm state) when a same-named
match exists elsewhere in the fleet. No new keyword routing, no new
skill — explicitly deferred to avoid building a chat-facing surface for a
convention-based signal that hasn't yet been validated against a real
migration's naming consistency.

## Consequences

- **Positive:** a cold service's evidence can now be read alongside "is
  there a same-named replacement elsewhere, and is it warm" — the
  distinguishing signal decommissioning readiness actually needs, built
  with zero new schema.
- **Positive:** sharing `coldServiceWhereClause` with ADR 0038 means this
  feature's notion of "cold" is identical to the cold-services view's by
  construction, not by discipline.
- **Negative / cost accepted, explicitly deferred (not forgotten):**
  name-only pairing is a convention, not a confirmation — two unrelated
  services that happen to share a name in two different clusters would be
  reported as a "pair" with no way to tell the difference. Per the locked
  decision, no label-based confirmation, structural fingerprinting,
  explicit mapping table, or propose/confirm workflow was built this pass.
  This is the same caution this codebase already applies elsewhere (the
  2026-09-05 trace-UUID incident is the standing reminder that a naming/
  shape convention alone is never sufficient to *act* on, only to
  surface as a candidate).
- **Negative / cost accepted:** no chat-facing answer exists yet for "is
  X the same as Y" — an operator has to notice the UI annotation; nothing
  proactively tells them.
- **Revisit if:** convention-based pairing proves noisy in practice (too
  many false "pairs" from coincidental same-naming across unrelated
  namespaces/services) — the fix is the deferred label/structural
  confirmation layer, not abandoning pairing; or if an operator workflow
  genuinely needs to *act* on a pairing (not just see it), which is
  exactly when the explicit `migration_mappings` table and a confirm gate
  earn their cost.
