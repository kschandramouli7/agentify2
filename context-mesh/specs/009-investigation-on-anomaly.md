# 009 – Investigation-on-anomaly loop

> The system notices a namespace going bad, runs the existing `diagnose`, and posts
> a redacted summary to a webhook — human-in-the-loop, **no auto-remediation**.
> Realizes [ADR 0016](../decisions/0016-proactive-investigation-loop.md); respects
> [ADR 0003](../decisions/0003-read-only-to-actions-boundary.md).

## Goal

Catch hard failures without someone watching a dashboard: when a namespace's pods
go `UNHEALTHY` or a cert is about to expire, automatically diagnose it (spec 005)
and push a prioritized summary to on-call — once per incident, cost-bounded.

## Depends on

- [spec 005](005-root-cause-correlation.md) — the diagnose path the loop reuses.
- Evaluator (`PodHealth`/`CertRenewal`) — the deterministic trigger.
- [ADR 0007](../decisions/0007-egress-data-governance.md) — redaction of outbound data.
- [ADR 0003](../decisions/0003-read-only-to-actions-boundary.md) — notify only, never act.

## Scope (v1)

**In:**
- Periodic sweep of `current_state`; **namespace** is the incident key.
- Anomaly = any pod `UNHEALTHY` **or** any cert within `*_CERT_CRITICAL_DAYS`.
- Investigate on transition **into** anomalous; notify again on **resolve**; no
  repeat while still bad. Per-sweep cap + per-namespace cooldown bound LLM spend.
- Generic Slack-compatible webhook (`{"text": …}`), summary scrubbed before send.
- **Opt-in** (`INVESTIGATION_ENABLED=false` by default).

**Out (stay honest):**
- Statistical anomaly detection / subtle regressions (needs P4b-ML) — UNHEALTHY/cert
  thresholds only.
- Auto-remediation of any kind (ADR 0003).
- Provider-specific paging (PagerDuty), Block Kit formatting, sub-sweep flap capture,
  shared incident state across replicas.

## Behavior

- **Given** namespace `prod` has a pod in CrashLoopBackOff and no open incident
  **when** the sweep runs **then** it diagnoses `prod`, posts a summary (severity,
  likely cause, recommendations, sources, trace_id) to the webhook, and marks the
  incident open — and does **not** alert again next sweep while it stays bad.
- **Given** an open incident's namespace is healthy again **then** it posts a
  "resolved" note and closes the incident.
- **Given** more anomalous namespaces than the per-sweep cap **then** it investigates
  up to the cap and logs that it deferred the rest (no silent drop).
- **Given** `INVESTIGATION_ENABLED=false` or no webhook URL **then** the loop does
  nothing.

## Interfaces

```
config:
  INVESTIGATION_ENABLED                 (default false)
  INVESTIGATION_WEBHOOK_URL             (Slack-compatible incoming webhook)
  INVESTIGATION_SWEEP_INTERVAL_MINUTES  (default 5)
  INVESTIGATION_MAX_PER_SWEEP           (default 5)
  INVESTIGATION_COOLDOWN_MINUTES        (default 60)
  INVESTIGATION_CERT_CRITICAL_DAYS      (default 7)

sweep → per namespace: gather signals → evaluator → anomalous?
  → (transition in)  diagnose (Opus) → RedactText(summary) → POST webhook → open
  → (transition out) POST "resolved" → close
webhook payload: { "text": "<formatted: severity, ns, reasons, cause, actions, sources, trace_id>" }
```

## Acceptance criteria

> Checked 2026-10-18. `internal/api/investigator.go` has `sweep()` and the
> pure `decideActions()` function; `investigator_test.go` has
> `TestDecideActions`, `TestInvestigateAndNotify`, `TestFormatAlertText`.
> Shipped and tested; simply never checked off.

- [x] Disabled by default; no webhook URL ⇒ no-op.
- [x] One alert per incident (no repeat while still anomalous); a resolve note on recovery.
- [x] Per-sweep cap enforced; deferrals logged, not dropped silently.
- [x] Outbound summary passes the egress scrubber; no action is ever taken (ADR 0003).
- [x] The open/close/cooldown decision is a pure function with unit tests.
