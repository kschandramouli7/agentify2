# 002 – K8fy Health Queries

> First operational use case. Lets operators ask natural-language questions about
> Kubernetes health and certificates. This spec is the worked example for the
> whole context-mesh approach — start here to see the pattern end to end.

## Goal

Let an operator ask, in natural language, about the live health of services/pods
and the status of certificates — and get an accurate, current answer (including
"should we renew this cert?").

## Depends on

- Policies: [storage-strategy](../policies/storage-strategy.md) (K8fy event profiles),
  [correlation](../policies/correlation.md) (the renewal judgment),
  [pod-formation](../policies/pod-formation.md) (K8fy's 4 starter pods).
- Other specs: 001-event-ingestion (how K8s signals get into the mesh).

## Context / constraints

- Kubernetes API is the **system-of-record** — K8fy stores mirrors/indexes, not masters.
- Live health must be **fresh** (query live; don't serve stale cached state).
- K8s events expire (~1h) — must be captured to answer historical "why" questions.
- **Non-goals (v1):** taking *actions* (actually renewing certs, restarting pods);
  multi-cluster; RBAC/permissions model (assume read-only access for now).

## Health model and signals (the definition of "healthy")

Health is **not** a single boolean — it's multi-dimensional. We answer at three
levels of confidence:

### Pod-level health

A pod is considered **Healthy** if:
- **Phase** is `Running` (not Pending, Failed, Unknown, etc.)
- **Ready condition** is `True` (containers are past their readiness probes)
- **Restart count** < 3 in the last 1h (a few restarts = normal; thrashing = bad)

A pod is **Degraded** if:
- Phase is Running but Ready is False (starts but not ready; may recover)
- Restart count >= 3 in the last 1h
- Recent event is a warning (e.g. `FailedScheduling`, `Unhealthy`)

A pod is **Unhealthy** if:
- Phase is Failed or Unknown
- Recent events show `CrashLoopBackOff`, `OOMKilled`, `DeadlineExceeded`

### Service-level health

A service's health is derived from its **endpoints**:
- **Healthy**: >= 1 endpoint and >= 75% of endpoints Ready
- **Degraded**: >= 1 endpoint but < 75% Ready (some replicas working, some not)
- **Unhealthy**: 0 endpoints OR all endpoints NotReady

### Thresholds (tunable; v1 defaults shown)

- Restart threshold: **3 restarts in 1h** → degraded
- Endpoint threshold: **75% Ready** → healthy
- Event lookback: **1h**

These are defaults; can be overridden per-namespace or service (future work).

### Why this matters for storage

- **`k8fy.live-state`** must capture pod phase + Ready condition + restart count + recent events.
- **`k8fy.metrics`** must track restart trends so we can spot patterns (not just one spike).
- **`k8fy.events`** stores the "why" (CrashLoopBackOff, etc.) so we answer *"why did it restart?"*

### Future refinement (refinement-loop)

- If users ignore Degraded and only act on Unhealthy, adjust thresholds.
- If certain events are noisy, filter them.
- If restarts have no correlation with user impact, learn a better signal.

## The questions in scope

1. "What's the health status of service `X`?"
2. "What's the health status of pod `Y`?"
3. "Are the certificates for service `X` healthy? expired? expiring soon?"
4. "Do we need to renew the certificate for `X`?"
5. _(near-future)_ "Why did pod `Y` restart?" → backed by `k8fy.events`.

## How each is answered (routing)

| Question | Pod(s) used | How |
|----------|-------------|-----|
| 1, 2 health now | `k8fy.live-state` | query K8s API live; summarize phase/readiness/restarts |
| 3 cert status | `k8fy.certificates` | point-lookup by service / filter "expiring < N days" |
| 4 should renew? | `k8fy.certificates` + renewal rule | data + threshold → judgment ([correlation](../policies/correlation.md)) |
| 5 why restart? | `k8fy.events` | time-range scan of captured events |

## The renewal rule (knowledge, not data)

- Default: **renew if a cert expires within 30 days**.
- **Resolved, as of [spec 003](003-tier1-deterministic-queries.md)'s
  implementation:** shipped as a single global constant in
  `internal/orchestrator/evaluator`'s `CertRenewal`, not a per-service
  override or a learned value. Whether it should become either of those is
  still open — moved to Open Questions below instead of left as an inline
  placeholder once the v1 answer was settled.

## Behavior

- **Given** service `X` exists **when** asked for its health **then** return a current
  summary (overall status + contributing pods/endpoints), freshness ≤ a few seconds.
- **Given** a cert expires in 12 days **and** the threshold is 30 days **when** asked
  "should we renew?" **then** answer "Yes — expires in 12 days (within 30-day policy)."
- **Given** the K8s API is unreachable **when** asked a live-state question **then**
  say so explicitly rather than returning stale/guessed data.

## Interfaces

```
ask(question, context) -> Answer { text, sources: [pod_id], freshness, confidence }
```

## Open questions

- [ ] Should thresholds (3 restarts in 1h, 75% endpoint Ready) be configurable per-namespace or global-only for v1?
- [ ] Should the 30-day cert-renewal threshold become per-service/configurable, or learned from past renewals? (v1 shipped as one global constant — see "The renewal rule" above.)
- [ ] When an endpoint is in transition (Ready → NotReady), which do we report?
- [x] Read-only now — when do we add *actions* (renew/restart) and how is that gated? Resolved: [ADR 0003](../decisions/0003-read-only-to-actions-boundary.md) + its 2026-10-18 amendment, and [ADR 0020](../decisions/0020-phase-3-remediation-with-approval-gate.md) — cert renewal is live (`VaultCertSkill._renew()`), other actions require human approval.
- [x] Single cluster v1 → how does multi-cluster change pod partitioning? Resolved: [ADR 0024](../decisions/0024-ingested-data-cluster-scoping.md) — an optional `cluster_id` segment in the pod ID (`models.PodID`), not a repartition.

## Acceptance criteria

> Checked 2026-10-18 against `internal/orchestrator/evaluator/{evaluator.go,evaluator_test.go}`.
> Items 2/3/4/6 have direct unit-test evidence (`TestPodHealth`,
> `TestServiceStatus`, `TestCertRenewal`). Items 1/5/7/8 are live-query/
> handler-level properties this test file doesn't exercise — left
> unchecked pending that verification, rather than assumed.

- [ ] Health query for a known service returns current status (Healthy | Degraded | Unhealthy) with ≤ few-seconds freshness.
- [x] Pod-level health is determined by Phase + Ready condition + restart count; service-level by endpoint count/readiness. (`TestPodHealth`, `TestServiceStatus`)
- [x] Restart count is measured over the last 1h; >= 3 → degraded. (`TestPodHealth`'s "too-many-restarts" case)
- [x] Endpoint thresholds: Healthy if >= 1 and >= 75% Ready; Degraded if >= 1 but < 75%; Unhealthy if 0 or all NotReady. (`TestServiceStatus`)
- [ ] Cert query can both look up one service and list "expiring within N days".
- [x] "Should we renew?" applies the 30-day renewal threshold and explains its reasoning. (`TestCertRenewal`)
- [ ] When the K8s API is down, the answer states that instead of guessing.
- [ ] Every answer cites which pod(s) it came from (e.g. "`k8fy.live-state` shard for namespace X").

