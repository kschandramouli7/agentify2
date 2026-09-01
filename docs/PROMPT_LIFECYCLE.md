# Prompt Lifecycle

How a skill's system prompt gets changed, tested, promoted, and rolled back.

> **The one thing to internalise:** prompts are **data served from Langfuse**, not
> code shipped in the container. Editing `src/agent/k8fy/prompts.py` and deploying
> **does not change production behaviour** — the Langfuse copy wins. Prompts ship
> through Langfuse; the repo copy is a fallback and a seed.
>
> This inverted on 2026-08-29 (ROADMAP P19 gap B). Before then prompts were frozen
> at process start, so a code deploy *was* how they changed.

---

## Where prompts live

| Location | Role |
|---|---|
| **Langfuse**, label `production` | What actually answers live traffic |
| `src/agent/k8fy/prompts.py` | Fallback when Langfuse is unreachable/unconfigured, and the seed content for first publish |
| `k8fy.prompts.ALL_PROMPTS` | Canonical registry of every prompt name fetched at runtime |

Eleven names are fetched at runtime: `k8fy/system`, `k8fy/health-check`,
`k8fy/cert-audit`, `k8fy/change-history`, `k8fy/restart-trend`, `k8fy/diagnose`,
`k8fy/vault-cert`, `k8fy/incident-responder`, `k8fy/deployment-guardian`,
`k8fy/chat`, `k8fy/chat-structure`.

`ALL_PROMPTS` is the single source of truth for both the startup prefetch
(`app.py`) and the seeding script. A prompt used by a skill but missing from that
registry runs on its fallback forever and can never be versioned — that is how
three prompts sat unseeded until 2026-08-29 (P19 gap A). **Add new prompts to
`ALL_PROMPTS`, not just to the skill.**

## How resolution works at runtime

`k8fy/prompt_manager.resolve()` is called **once per request**, not at import.

- The Langfuse SDK caches client-side with **stale-while-revalidate** (default TTL
  60s): a fresh cache returns with no network call; an expired one returns the
  stale value immediately and refreshes in the background. So per-request
  resolution costs effectively nothing.
- `app.py` prefetches every prompt at startup so the first request never pays a
  cold-cache fetch.
- **Negative cache:** a failed resolve is not retried for `FAILURE_COOLDOWN_SECONDS`
  (60s). `resolve()` is synchronous inside async handlers, and a failed fetch
  against an unreachable Langfuse costs 0.9–3.1s that blocks the event loop for
  *every* concurrent request. Without the cooldown, a Langfuse outage becomes a
  per-request stall.
- Per-request state lives in a `ContextVar`, never on the agent instance — skills
  are process-wide singletons via `SkillRouter`, so instance state would race
  across concurrent queries.

**Net effect: promoting the `production` label reaches live traffic within ~60s,
with no pod restart.**

---

## Changing an existing prompt

### 1. Edit the repo copy (keeps the fallback honest)

Edit the constant in `src/agent/k8fy/prompts.py` and commit. This does **not**
change production; it keeps the fallback and future seeds in step with reality.

### 2. Publish it as a candidate on `staging`

```bash
cd src/agent
export LANGFUSE_PUBLIC_KEY=pk-lf-...
export LANGFUSE_SECRET_KEY=sk-lf-...
export LANGFUSE_BASE_URL=https://us.cloud.langfuse.com   # must match settings.py

python3 scripts/migrate_prompts_to_langfuse.py --label staging k8fy/diagnose
```

`--label staging` publishes a new version **without touching live traffic**.
Naming one or more prompt names limits the run to those.

You can also edit directly in the Langfuse UI and label the new version
`staging` — equivalent, but then remember to mirror the text back into
`prompts.py` so the fallback does not drift.

### 3. Gate the candidate

GitHub → Actions → **10 · Prompt promotion gate** → Run workflow, with
`prompt_name` = the prompt you changed (e.g. `k8fy/diagnose`) and
`prompt_label` = `staging` (or `prompt_version` for an exact version).

`prompt_name` is **required**, and the pin applies only to that prompt. The
dataset spans several intents and therefore several prompts; an unscoped pin
would resolve the others at the candidate label too, 404, and fall back to their
local strings — so the run would score one candidate plus several fallbacks
instead of the production baseline.

The gate also **refuses to run if the pinned prompt does not exist**. Without
that check the agent falls back to its local string, produces good answers, and
the gate reports "candidate cleared" for a candidate that was never used. Seen
for real on 2026-08-30.

The gate scores the candidate against the `k8fy-regression` Langfuse dataset via
`POST /admin/eval/query` (ADR 0030) and fails below ADR 0019's 0.85 threshold.
Because that endpoint delegates to the real `HandleQuery`, the candidate is
measured through the actual production path — routing, tiering, redaction and the
backend↔agent boundary all included.

The gate **never promotes anything**. A pass is evidence for a human.

**The overall mean is not the verdict.** The dataset spans ten intents, so a mean
across all of them can clear 0.85 while the single item that exercises the
candidate fails outright — observed on `prompt-gate-33305278654` (mean 0.935
PASS, while the `diagnose` item returned `status='error'`). The gate therefore
also requires **every item whose intent maps to the gated prompt** to pass, and
fails if the dataset contains **no** such item — gating a prompt the dataset
never exercises tests nothing. The intent→prompt map lives in
`INTENT_TO_PROMPT` in `run_evals.py` and mirrors `SkillRouter`.

**When a gated item fails, the gate re-runs just that item unpinned** and reports
candidate vs production side by side, because the two outcomes demand opposite
actions:

| Result | Verdict |
|---|---|
| candidate fails, production passes | **REGRESSION** — the candidate caused it. Fail, do not promote. |
| candidate fails, production fails too | **Pre-existing** — the candidate is not at fault. Pass with a warning, and fix the underlying failure separately. |

Without that comparison a long-standing failure would block every prompt change
forever, and a genuine regression would look identical to it.

**Where to read the result:**

| Where | What you get |
|---|---|
| The workflow run's **step log** ("Score the candidate against k8fy-regression") | The authoritative result. Per-item scores, the mean, and a `Gating: <prompt> <label> (resolved to version N)` line proving which version was actually measured. The gate decision is computed here, locally — not in Langfuse. |
| The run's **Job Summary** | Prompt, label, version and threshold at a glance, no expanding |
| Langfuse → **Experiments** | The run named `prompt-gate-<run_id>`, with per-item scores that persist so candidates can be compared across runs. Note: this is under **Experiments**, not under the dataset — Langfuse v4 surfaces what the SDK calls a "dataset run" as an experiment. |
| Red tick + annotation | `Candidate prompt scored below the gate. Do NOT promote it to production.` |

Langfuse reporting is **best-effort**: it is wrapped in a `try/except` that prints
`WARN Langfuse reporting skipped` and continues, so a Langfuse outage cannot fail
the gate. That also means the step log — not the Experiments view — is the source
of truth for whether a candidate passed.

### 4. Promote

**There is no version to specify anywhere.** Not in `prompts.py`, not in a
manifest, not in a deploy. The agent resolves `label="production"` on every
request, so promoting *is* moving that label — the deployed image is irrelevant
and no restart is involved.

A label points to exactly one version, so assigning `production` to v6 moves it
off v5 in the same action.

**Via the UI (recommended for a first promotion — unambiguous):**
Langfuse → Prompts → `k8fy/diagnose` → the version you gated → set label
`production`.

**Scripted:**

```python
from langfuse import Langfuse
lf = Langfuse(public_key=..., secret_key=..., base_url="https://us.cloud.langfuse.com")
lf.update_prompt(name="k8fy/diagnose", version=6, new_labels=["production"])
```

`new_labels` sets the labels *for that version*, so include any you want to keep
(e.g. `["production", "staging"]` to leave it labelled as the current candidate
too).

**Then confirm it actually took effect** — this is the only step that proves the
whole mechanism, and it needs no deploy:

```bash
# wait ~60s for the SDK's stale-while-revalidate cache, then (port-forward first —
# it needs a moment to establish, see CLOUDSHELL_RUNBOOK.md):
curl -sS -X POST localhost:18080/api/query -H 'Content-Type: application/json' \
  -d '{"question":"why is payment-worker crashing?","context":{"namespace":"payments","service":"payment-worker"}}'
curl -sS localhost:18080/admin/traces | python3 -m json.tool | grep -m2 prompt_version
```

`prompt_version: 6` on a Tier-2 trace means live traffic is on the new version.

**Expect to need one extra request.** The SDK is stale-while-revalidate: the
first call after the 60 s TTL expires still returns the *stale* version and only
then refreshes in the background. So a query immediately after promoting can
legitimately still report the old version — query again before concluding the
promotion failed. Observed 2026-09-01: `5`, then `6` on the next request.
The next deploy's **Verify prompt provenance** step reports the same thing.

Two consequences worth expecting:

- Gating `--prompt-label production` from then on tests v6, so the baseline the
  gate compares against has moved.
- Rolling back is the same operation in reverse (step 5) — no redeploy.

### 5. Roll back

Move `production` back to the previous version. Same ~60s, no redeploy. This is
the fastest rollback in the system — faster than any code path.

---

## Quick reference — verify a prompt change end to end

### 0. Get credentials without pasting them anywhere

The keys already live in AWS Secrets Manager (`agentify/dev/langfuse`), put there
by `04-bootstrap-langfuse-secret.yml`. Source them from there rather than copying
them into a scratch file — a secret key in an editor buffer or shell history is
one screen-share away from being leaked, and rotating it means touching the
GitHub secret, Secrets Manager and every developer.

```bash
export AWS_PROFILE=agentify-dev            # aws sso login --profile agentify-dev first

eval "$(aws secretsmanager get-secret-value \
  --secret-id agentify/dev/langfuse \
  --query SecretString --output text \
  | python3 -c 'import json,sys
d=json.load(sys.stdin)
for k in ("LANGFUSE_PUBLIC_KEY","LANGFUSE_SECRET_KEY","LANGFUSE_BASE_URL"):
    if d.get(k): print(f"export {k}={d[k]}")')"
```

If you must set them by hand, use placeholders and mind the space after `export`
(`exportLANGFUSE_BASE_URL=...` fails silently as a single unknown command):

```bash
export LANGFUSE_PUBLIC_KEY=pk-lf-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
export LANGFUSE_SECRET_KEY=sk-lf-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
export LANGFUSE_BASE_URL=https://us.cloud.langfuse.com
```

`LANGFUSE_BASE_URL` must match `config/settings.py` — Langfuse projects are
region-scoped, and the wrong region returns `401 Invalid credentials` even when
the keys are correct.

### 1. Publish the candidate

```bash
cd src/agent
python3 scripts/migrate_prompts_to_langfuse.py --label staging k8fy/diagnose
```

Expect `Created 1, skipped 0, failed 0` and a reminder that it is not serving
traffic. `Created 11` means you are pointed at the wrong project — stop.

### 2. Confirm it exists before gating

```bash
python3 - <<'EOF'
import os
from langfuse import Langfuse
lf = Langfuse(public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
              secret_key=os.environ["LANGFUSE_SECRET_KEY"],
              base_url=os.environ["LANGFUSE_BASE_URL"])
p = lf.get_prompt("k8fy/diagnose", label="staging", cache_ttl_seconds=0)
print("staging version:", p.version)
EOF
```

The gate performs this check itself and refuses to run without it, so this step
is only for a faster answer.

### 3. Gate it

GitHub → Actions → **10 · Prompt promotion gate** → Run workflow:
`prompt_name=k8fy/diagnose`, `prompt_label=staging`.

Read the step log for `Gating : k8fy/diagnose label 'staging' (resolved to
version N)` — that line proves the candidate was measured rather than a
fallback.

### 4. Promote, then verify it took effect

Move the `production` label in Langfuse, wait ~60s, then check a Tier-2 trace
shows the new version:

```bash
kubectl port-forward -n agentify svc/agentify-backend 18080:8080 &

curl -sS -X POST localhost:18080/api/query -H 'Content-Type: application/json' \
  -d '{"question":"why is payment-worker crashing?","context":{"namespace":"payments","service":"payment-worker"}}'

curl -sS localhost:18080/admin/traces | python3 -m json.tool \
  | grep -E 'prompt_name|prompt_version|tier' | head
```

A `diagnose` question is used deliberately: a healthy service takes the Tier-1
fast path, makes no LLM call, and correctly records no prompt (ADR 0006).

### 5. If it misbehaves

Move `production` back to the previous version. ~60s, no redeploy.

---

## Adding a new prompt

1. Add the constant to `prompts.py`.
2. **Add `(name, constant)` to `ALL_PROMPTS`.**
3. Have the skill pass `prompt_name=` / `prompt_fallback=` to `K8fyAgent.__init__`
   (never a pre-resolved string — that pins it and skips Langfuse).
4. Seed it: `python3 scripts/migrate_prompts_to_langfuse.py` (default mode is
   seed-only-if-absent, so it will not disturb existing prompts).

---

## Verifying a prompt change took effect

`traces.prompt_name` and `traces.prompt_version` record which prompt produced
each answer.

```bash
curl -sS localhost:18080/admin/traces | python3 -m json.tool \
  | grep -E 'prompt_name|prompt_version|tier'
```

| Observation | Meaning |
|---|---|
| `prompt_name` set, `prompt_version` a number | Resolved from Langfuse — working |
| `prompt_version: null` on a **tier2** trace | **Fell back to the local string** — Langfuse is not serving this prompt |
| `prompt_version: null` on a **tier1** trace | Correct: no LLM call, so no prompt (ADR 0006) |
| `prompt_name` empty on tier2 | Provenance plumbing is broken |

### Per-item failures are reported, not gated

Both eval gates threshold the **mean** (ADR 0019). A single failing item
disappears into the average — `diagnose-payment-crash-001` returned
`status='error'` on production while every deploy passed at mean 0.935.

`run_evals.py` therefore emits a `::warning::` annotation per failing item, plus
one stating the gate passed despite them, whenever it runs in GitHub Actions.
**Report-only on purpose:** a hard per-item gate on the deploy path would block
every deploy until that pre-existing failure is fixed. The *prompt* gate is
stricter — it does fail when the gated prompt's own items fail — because a
candidate has no business being promoted on a diluted average.

Tightening the deploy gate to per-item is a policy decision to take once the
dataset is clean.

`02-deploy.yml`'s **Verify prompt provenance on traces** step asserts this on
every deploy: a missing `prompt_name` fails the deploy, a null `prompt_version`
warns. It warns rather than fails on purpose — coupling deploy success to
Langfuse's availability would be a worse failure mode than the one it guards.

`traces.is_eval` marks traffic produced by `/admin/eval/query`. Eval runs use the
real path, so their traces look production-shaped; **every consumer of the traces
table must filter them out**, or quality analysis grades the system's own test
traffic.

---

## Do not

- **Do not** expect a code deploy to change prompt behaviour. It changes the
  fallback only.
- **Do not** use `--force` casually. It re-pushes every prompt *and moves the
  `production` label*, silently reverting any Langfuse-side edit. Use it only when
  you intend `prompts.py` to overwrite Langfuse.
- **Do not** promote straight to `production` without gating. Prompt changes have
  a wider blast radius than a pod restart — they reshape every future query for
  every tenant, indefinitely (ADR 0020's reasoning, applied to prompts).
- **Do not** pass `system_prompt=` from a skill. That pins an exact string and
  bypasses Langfuse entirely; it exists for tests.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `401 Invalid credentials. Confirm that you've configured the correct host.` | Langfuse projects are **region-scoped**. The keys are usually right and the host wrong. Set `LANGFUSE_BASE_URL` to the region holding your project, and make it match `config/settings.py`. |
| Seeding reports `Created 11` when you expected `Created 3` | You are pointed at the wrong project — the existing prompts were not found, and their `production` labels have just been moved. |
| Candidate publish reports `SKIP … already in Langfuse` | Only happens without `--label`; a candidate label bypasses the seed-only-if-absent guard. |
| `prompt_version` null in production traces | Langfuse not resolving. Check pod logs for `Langfuse resolve('…') failed`, and note the 60s negative cache means one log line per prompt per minute, not one per request. |
| Gate returns 401 | `EVAL_AUTH_TOKEN` differs between the backend deployment and the repo's Actions secret. Empty in both means the endpoint is open (dev only). |
| Langfuse UI edit seems to have no effect | Check the label. Only the version carrying `production` serves traffic. |

---

## Configuration

| Variable | Where | Notes |
|---|---|---|
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | AWS Secrets Manager `agentify/dev/langfuse`, bootstrapped by `04-bootstrap-langfuse-secret.yml` | Absent → all prompts use fallbacks, cleanly |
| `LANGFUSE_BASE_URL` | same | Defaults to `https://us.cloud.langfuse.com` |
| `LANGFUSE_TRACING_ENABLED` | `infra/kubernetes/agent.yaml` | Emits observations for reasoning calls (P19 gap E). Code default is off |
| `EVAL_AUTH_TOKEN` | `infra/kubernetes/backend.yaml` + Actions secret | Guards `/admin/eval/query`. Empty **disables** the endpoint (503) unless `ENV=dev` — revised 2026-09-01, see ADR 0030's amendment. The promotion gate needs it set. |
| `langfuse` SDK | `src/agent/requirements.txt` | Pinned `>=4.14.0,<5.0.0`. CI's eval scripts install `<3.0.0` separately — they use the v2 dataset API |

## References

- [ADR 0030](../context-mesh/decisions/0030-version-pinned-prompt-evaluation.md) — version-pinned evaluation endpoint
- [ADR 0019](../context-mesh/decisions/0019-eval-harness-as-ci-gate.md) — eval harness as CI gate, plus its 2026-08-30 correction
- [ADR 0020](../context-mesh/decisions/0020-phase-3-remediation-with-approval-gate.md) — the never-auto-apply precedent
- [ROADMAP P19](../context-mesh/ROADMAP.md) — gaps A–F and the self-improving-agent design
- `.github/workflows/10-prompt-gate.yml` — the gate, and why a Langfuse webhook cannot trigger it directly
