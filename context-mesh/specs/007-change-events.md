# 007 – Change / deploy events (turning "when" into a candidate "why")

> Captures deployment rollouts as append-only events so diagnosis can align a
> restart onset with a preceding change — the cheapest path from temporal to
> *causal* reasoning. Builds on [spec 006](006-temporal-ingestion-and-history.md)
> (the temporal spine) and [correlation](../policies/correlation.md).

## Goal

When a service starts crashing, surface **what changed just before** — e.g.
"restarts began 14:08, ~3 min after `payment` rolled out revision 7 at 14:05."
The rollout becomes a **candidate trigger** the agent can name (and a human can
confirm), not a proven cause.

## Depends on

- [spec 006](006-temporal-ingestion-and-history.md) — restart time-series gives the
  onset to correlate against.
- [ADR 0013](../decisions/0013-temporal-data-in-postgres-events-table.md) — change
  events are append-only history → the Postgres events table (`k8fy.events`).

## Scope (v1)

**In:**
- Adapter watches **Deployments** (`AppsV1Api`) and emits a `deploy` event to
  `k8fy.events` when a deployment's revision changes (annotation
  `deployment.kubernetes.io/revision`). Payload: `deployment`, `namespace`,
  `revision`, `images`, `replicas_desired`.
- Append-only traits (`time-range-scan` / `append-only`) → events table (profile
  `k8fy.events` already registered).
- The events `Query` entity filter also matches the payload `deployment` field, so a
  service's change history is retrievable.
- Agent tool **`get_change_history`** + diagnose prompting that correlates a deploy
  timestamp with the restart onset.

**Out (stay honest):**
- ConfigMap/Secret/image-registry changes, HPA/scaling events — Deployments only in v1.
- **Proven causation.** A deploy shortly before a crash is **correlation**; the agent
  states it as a likely trigger to confirm (e.g. via logs, spec 008), never as fact.

## Behavior

- **Given** `payment` rev 7 rolled out at 14:05 **and** restarts on
  `payment-7c9-bbb` climbed starting 14:08 **when** asked "what's wrong with
  payment?" **then** the diagnosis names the rollout of rev 7 as the likely trigger
  (temporal proximity), and recommends confirming via logs/rollback.
- **Given** no deploy events in the window **then** the agent does not invent a
  change; it reports the crash onset without attributing a trigger.

## Interfaces

```
adapter: deployment revision change → event_namespace "k8fy.events"
  event_type "deploy"
  payload { deployment, namespace, revision, images[], replicas_desired }
  traits { shape: structured, access_pattern: time-range-scan, temporality: append-only }

routing: intent "change_history" → k8fy.events leaf pod
agent tool: get_change_history(namespace, deployment?, service?, since?, until?)
```

The `diagnose` fan-out already includes `k8fy.events`, so recent deploy events
appear in the agent's initial data; the tool widens the window on demand.

## Acceptance criteria

> Checked 2026-10-18. `get_change_history` is implemented and tested
> (`src/agent/tests/test_change_history.py`, `ChangeHistorySkill`,
> `handlers.go`). Shipped and tested; simply never checked off.

- [x] A deployment revision change produces one append-only `deploy` event (not latest-wins).
- [x] `get_change_history` returns deploy events filtered by deployment/service + window.
- [x] Given a deploy shortly before a restart climb, the diagnosis names it as a
      **candidate** trigger ("likely / to confirm"), not a certainty.
- [x] Given no deploy events, no trigger is fabricated.
