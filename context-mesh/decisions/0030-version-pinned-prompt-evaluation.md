# 0030 – Version-pinned prompt evaluation via a dedicated authenticated endpoint

## Status

Proposed   ·   (date: 2026-08-30)

## Context

[ADR 0019](0019-eval-harness-as-ci-gate.md) made the eval harness a CI gate:
`scripts/run_evals.py` POSTs each dataset item to `{backend_url}/api/query` and
fails the deploy if the mean score drops below a threshold. That design was
deliberate — it exercises the **real deployed path**, not a reconstruction of it.

Two things have changed since.

**1. Prompt changes now bypass the gate entirely.** `02-deploy.yml` triggers on
`workflow_dispatch` and on pushes to `main` under `src/**`,
`infra/kubernetes/**`, or the workflow file. Moving the `production` label in the
Langfuse UI touches no git object, so no workflow runs. Since prompts are now
resolved per request (ROADMAP P19 gap B, 2026-08-29), a label move reaches live
traffic within ~60 s — where it previously needed a pod restart that tended to
coincide with a deploy. Making promotion work as documented also made an ungated
promotion take effect immediately.

**2. The harness cannot evaluate a candidate.** `run_evals.py` measures whatever
`production` currently points at; it has no concept of a prompt version. So
"validate this candidate before promoting it" — the shape Langfuse's own
prompt-CI/CD guidance recommends, and the shape ROADMAP P19 gap D was reframed
to on 2026-08-30 — is **not expressible today**. This is the blocker, and it is
a product change, not CI plumbing.

Relevant constraints discovered while scoping this:

- **`/api/query` is unauthenticated.** `QueryRequest` is `{question, context}`
  and no bearer check runs on it (`internal/api/router.go`).
- **A `/admin/` prefix grants nothing.** Every route — `/admin/*` included —
  does pass through one global middleware (`NewMiddleware`, wired at
  `internal/api/router.go:122`), but it does request logging and Prometheus
  metrics only, and carries an explicit `// TODO: add auth middleware`. There is
  no authentication at the middleware or route layer anywhere. The only
  authenticated surfaces do an explicit in-handler bearer check:
  `checkRemediationAuth` (ADR 0020) and the collector token. Auth here must
  therefore be written, not inherited — and adding it to `NewMiddleware`
  instead would apply it to `/api/query` and the ingest path too, which is a
  larger decision than this ADR should make.
- **[ADR 0020](0020-phase-3-remediation-with-approval-gate.md) rule 5** keeps
  any capability that reshapes agent behaviour off Claude-facing and
  unauthenticated surfaces, on the grounds that prompt injection or a
  misinterpreted free-form request must not be a route to it.

A prompt-version override is exactly such a capability: "make the agent answer
using arbitrary prompt text" is a behaviour-substitution lever, weaker than a
pod restart but far from inert.

## Decision

Add version-pinned evaluation as a **separate, explicitly bearer-authenticated
endpoint that reuses the real deployed code path**. Do not add the override to
`/api/query`.

1. **New route: `POST /admin/eval/query`.** Body is the existing
   `QueryRequest` plus an optional prompt pin (`prompt_label` **or**
   `prompt_version`, per prompt name). It runs the same handler logic as
   `HandleQuery` — same routing, same tiering, same agent call — differing only
   in which prompt version the agent resolves. This preserves ADR 0019's
   "test the deployed path" property.

2. **Auth is an explicit in-handler bearer check**, mirroring
   `checkRemediationAuth`: constant-time compare against a configured token,
   and an unset token means open (dev-only), the same posture already used for
   `COLLECTOR_TOKEN` and the remediation gate. A route prefix is not treated as
   protection.

3. **`/api/query` is unchanged.** The unauthenticated public path gains no
   prompt-override field, no header, and no way to reach one. This is the whole
   reason for a second endpoint rather than a flag.

4. **The pin flows as request state, not process state.** The backend forwards
   the pin to the agent's reason request; the agent honours it inside the
   existing per-request `ContextVar` established for gap B
   (`k8fy/agent.py:_with_system_prompt`). No instance or module state is
   involved, so a pinned eval request cannot leak into a concurrent production
   request served by the same singleton skill.

5. **Pinned fetches bypass both caches.** A pinned resolve uses
   `cache_ttl_seconds=0` (the point is an exact version, not a fast one) and
   keys its failure cooldown separately from the unpinned `production` entry,
   so an eval against a broken candidate cannot suppress production's own
   resolution — or vice versa.

6. **Eval traffic is marked and excluded from P19's sampling.** Traces produced
   through this endpoint are flagged (a dedicated column or trace field, decided
   at implementation). The Evaluator Agent's sampler **must** filter them out.
   Without this, P19 would judge its own synthetic eval traffic as if it were
   production, and the improvement loop would partly be learning from its own
   test set — a silent correctness failure, not a cosmetic one.

7. **The gate that consumes this is D2, and is out of scope here.** This ADR
   provides the capability only. Wiring it (Langfuse webhook → GitHub
   `repository_dispatch` → `langfuse/experiment-action` against the existing
   `k8fy-regression` dataset) is a separate change, as is any decision about
   protected labels.

### Rejected alternatives

- **A prompt-version field on `/api/query`.** Simplest diff, and rejected: it
  places a behaviour-substitution lever on an unauthenticated endpoint, which
  is precisely the class of exposure ADR 0020 rule 5 exists to prevent. A
  `non-prod-only` guard was considered as mitigation and rejected as too easy to
  misconfigure into production — the separate authenticated route achieves the
  same thing structurally.
- **An out-of-band harness that constructs skills in-process against the
  candidate prompt.** Adds no production surface, and rejected because it stops
  exercising the deployed path — routing, tiering, governance/redaction, and the
  backend↔agent boundary all drop out of coverage. That is the property ADR 0019
  was built to have, and a gate that passes while the real path is broken is
  worse than no gate.
- **Promote-then-revert** (run evals after the label moves; revert on a low
  score), which is what ROADMAP gap D said before 2026-08-30. Rejected: a bad
  prompt still reaches 100 % of live traffic for the duration of the eval run.
  Gate before promote; keep revert as a backstop.

## Consequences

- **Positive:** makes "validate the candidate, then promote" possible at all,
  which is the prerequisite for closing P19 gap D. The capability is useful
  independently of P19 — prompt A/B tests, canary rollouts, and per-version
  quality comparison all need exactly this pin.
- **Positive:** keeps the new lever off the unauthenticated surface, so the
  blast radius of the eval capability is bounded by a token rather than by
  network position.
- **Negative / cost accepted:** a second endpoint that must stay behaviourally
  in step with `HandleQuery`. If they drift, the gate silently stops testing
  what production does. Mitigation: share the handler body and let the pin be
  the only branch — not a parallel implementation.
- **Negative / cost accepted:** eval traffic now writes traces that look
  production-shaped and must be filtered by every future consumer of the
  `traces` table, not just P19's sampler. The flag is easy; remembering to
  honour it is the ongoing cost.
- **Negative / cost accepted:** an unset token means open, matching the existing
  collector/remediation posture. That is a deliberate consistency choice, and it
  means a deployment that forgets to set the token exposes the override.
- **Revisit if:** a general per-tenant or per-request model/prompt routing
  capability lands ([ADR 0008](0008-multi-provider-model-routing.md) territory).
  At that point the pin stops being an eval-only concern and should be folded
  into that mechanism rather than kept as a parallel path.
