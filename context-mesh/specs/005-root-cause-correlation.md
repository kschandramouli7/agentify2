# 005 – Root-cause correlation (multi-signal diagnosis)

> The defensible capability: stitch multiple signals about a service into one
> prioritized, causal narrative — where an LLM beats a dashboard. Builds on
> [correlation](../policies/correlation.md), [ADR 0006](../decisions/0006-two-tier-query-path.md)
> (this is a Tier-2 capability), and [spec 002](002-k8fy-health-queries.md) (the per-signal rules).

## Goal

For a diagnostic question ("what's wrong with payment?", "why is X unhealthy?"),
fan out to **all** signals for the service/namespace, and have the agent return a
correlated diagnosis: the **active incident**, **latent risks**, a **likely cause**,
and a **prioritized** set of actions — citing which signals support each.

## Depends on

- Policies: [correlation](../policies/correlation.md) (fan-out + combine),
  [data-governance](../policies/data-governance.md) (the multi-signal data is redacted before egress).
- Decisions: ADR 0006 (correlation is Tier-2 — synthesis needs the LLM).

## Context / constraints

- **Tier-2 only.** Correlation is inherently multi-signal synthesis → the agent,
  never the Tier-1 fast path.
- **Routing fix:** diagnostic phrasing must NOT fall into the single-signal Tier-1
  health path. `inferIntent` recognizes a **`diagnose`** intent *before* health/cert,
  so "why is payment unhealthy?" fans out instead of returning a one-pod verdict.
  (This resolves Known-limitation #3 in [spec 003](003-tier1-deterministic-queries.md).)
- **Fan-out:** `diagnose` routes to all `k8fy` leaf pods for the namespace
  (live-state shard + certificates + events if present) so the agent's initial data
  spans signals; it may still call tools for more.
- **⚠️ Bounded by available signals — bound narrowed by [spec 006](006-temporal-ingestion-and-history.md) (2026-06-05).**
  We ingest **pod/service health + cert metadata**, and now a **restart-count
  time-series** (`k8fy.metrics`, append-only). So diagnosis can correlate
  current-state health + certs **and reference *when* restarts began climbing**
  (e.g. "restarts went 0→17 starting 14:08; cert expires in 5d → fix the crash
  first"). Bound further narrowed by [spec 007](007-change-events.md) (deploy
  events) and [spec 008](008-on-demand-pod-logs.md) (on-demand logs): diagnosis can
  now name a **candidate trigger** ("restarts began ~3 min after revision 7 rolled
  out — likely cause, confirm via logs") and fetch the crash reason from
  previous-container logs. Still **out:** CPU/mem samples (need metrics-server),
  full lifecycle-event history, and config/secret/registry change sources. Causation
  remains stated as a hypothesis to confirm, never asserted from correlation alone.
- **Non-goals (v1):** temporal/causal RCA from event history; auto-remediation
  (read-only — ADR 0003); a structured findings API surfaced to the UI (the
  correlation lands in the answer narrative; structured `findings`/`likely_cause`
  ride along in the agent response `details` for future consumers).

## Behavior

- **Given** payment in `prod` is DEGRADED (1/2 pods CrashLoopBackOff) **and** its
  cert expires in 5 days **when** asked "what's wrong with payment?" **then** the
  agent returns: active incident = the CrashLoop; latent risk = the cert; a likely
  cause; and a prioritized action list (investigate crash first), citing both signals.
- **Given** only health data is available **then** it still diagnoses health and
  notes the absence of other signals rather than inventing a cause.

## Interfaces

```
intent "diagnose"  → RouteToPods fans out to all k8fy leaf pods for the namespace → Tier 2
agent structured output (additions): {
  answer,              # the correlated narrative (surfaced to the UI today)
  status, confidence,
  findings: string[],  # one per signal considered
  likely_cause,        # hypothesis (may be null when signals are insufficient)
  severity,            # info | warning | critical
  recommendations: string[]   # prioritized
}
```

## Open questions

- [ ] When does `diagnose` win over `health_check`/`cert_check`? (v1: any diagnostic
      phrasing — "why", "what's wrong", "root cause", "investigate".)
- [ ] Surface structured `findings`/`likely_cause` through the backend to the UI, or
      keep them in the narrative for v1? (v1: narrative + `details`.)
- [ ] Confidence/disagreement handling when signals conflict (correlation.md).

## Acceptance criteria

> Checked 2026-10-18. `src/agent/models/response.py` has `findings`,
> `likely_cause`, `severity`, `recommendations` exactly as specified.
> `diagnose` intent and the fan-out logic are live and relied on by later
> specs (006/007/008/010) as already-working infrastructure. Shipped;
> simply never checked off.

- [x] Diagnostic phrasing routes to `diagnose` (Tier-2 fan-out), not Tier-1.
- [x] Given multi-signal data, the agent's answer names the active incident, the
      latent risk(s), a likely cause, and a prioritized action order.
- [x] With only one signal present, it diagnoses that signal and does not fabricate
      a cause for absent signals.
- [x] Multi-signal data is redacted before egress (ADR 0007) — unchanged.
