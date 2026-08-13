# New Account Migration — Lessons Learned

> Written after migrating `infra/terraform/aws` + the K8s manifests from the
> original AWS account to a second, corporate-governed account
> (2026-08-07 to 2026-08-13). Every issue below was hit for real during that
> migration, in this order, over a single very long session. If you're doing
> this again, read the **Pre-flight checklist** first — it would have caught
> most of this before it became a debugging session.

## Pre-flight checklist

Do these *before* running `terraform apply` against the new account:

1. **Grep the whole repo for the old account ID.** Hardcoded account IDs
   are the single biggest source of pain in this migration — they don't
   fail at `terraform plan` time (Terraform doesn't know they're wrong),
   they fail hours later at apply/runtime in a way that looks unrelated.
   ```bash
   grep -rn "<OLD_ACCOUNT_ID>" --include="*.tf" --include="*.yaml" --include="*.yml" --include="*.py" .
   ```
   Every hit found this session (see "Hardcoded values" below) was a real bug.
2. **Ask whoever governs the new account about Service Control Policies**
   up front — specifically, whether EC2 instances are allowed public IPs.
   This determines whether you need `enable_nat_gateway = true` (see
   below) before you even start, rather than discovering it 20 minutes
   into an EKS node-group create.
3. **Confirm which credential mechanism you'll actually be using** (AWS
   SSO, a corporate tool like CloudBoost, static keys) and test that
   `terraform apply` — not just `aws sts get-caller-identity` — can
   authenticate with it *before* starting. See "Credential-chain gotchas"
   below; the CLI and Terraform's Go SDK do not always agree on what's a
   valid credential.
4. **Decide up front**: is this local checkout going to serve one account
   or two? If two, every account-specific value needs to come from
   `-var`/`-var-file`, never a hardcoded edit — see "Terraform
   account-portability variables" below for what's already wired up.

## Terraform account-portability variables

`infra/terraform/aws/variables.tf` has four variables added specifically
so this config can target a second account without editing shared
resources. Use a `-var-file` (see the `newaccount.tfvars` pattern below,
gitignored) rather than passing these individually every time:

| Variable | Default | Set to... |
|---|---|---|
| `create_github_oidc_provider` | `true` | `false` if the account already has a `token.actions.githubusercontent.com` OIDC provider (AWS allows only one per account — a second `apply` with this left `true` fails with `EntityAlreadyExists`). |
| `github_oidc_trusted_repos` | every repo used across every account so far | The specific repo(s) this account's CI role should trust — tightening this avoids unnecessary over-permissioning in a shared/corporate account. |
| `tfstate_bucket` | the original account's bucket | The new account's bootstrap-created bucket (`infra/terraform/bootstrap`'s `state_bucket` output). Used by the CI role's Terraform-state IAM grant (Pause/Resume's targeted `apply`) — **not** the same thing as `backend.tf`'s bucket (see next section). |
| `enable_nat_gateway` | `false` | `true` if the account's SCP denies EC2 instances a public IP (see "EKS managed node groups + a 'no public IP' SCP" below). |

`infra/terraform/aws/backend.tf`'s bucket is a *separate* thing from
`tfstate_bucket` above — it can't reference a variable (Terraform backend
blocks are evaluated before variables exist), so it's hardcoded and must
be edited directly per `docs/DEPLOYMENT.md`'s instructions, or overridden
per-`init` with `-backend-config="bucket=..."`. We hit a real outage from
this: CI workflows that ran a plain `terraform init` (no override) kept
hitting a 403 against the *old* account's bucket long after local applies
were working fine, because the override was only ever passed locally, not
baked into the file. Fix once you're fully committed to one account: just
edit `backend.tf` directly.

## Hardcoded old-account values found and fixed this migration

Every one of these was a real, silent failure — none of them errored at
`terraform plan`/`apply` time, only later:

- `infra/terraform/aws/iam.tf` — CI role's trust policy referenced the old
  account's OIDC provider ARN directly instead of deriving it.
- `infra/terraform/aws/eks.tf` — the `root` EKS access entry's
  `principal_arn` was `"arn:aws:iam::<OLD_ACCOUNT>:root"` instead of
  `"arn:aws:iam::${data.aws_caller_identity.this.account_id}:root"`.
- `infra/kubernetes/backend.yaml` / `agent.yaml` — both ServiceAccounts had
  a *real* (but old-account) IRSA `role-arn` baked in, instead of the
  `REPLACE_WITH_TERRAFORM_OUTPUT_*` placeholder the deploy workflow's `sed`
  step expects to substitute. Someone had run the substitution once and
  the resolved file got committed instead of the placeholder — a trap that
  can silently recur any time a resolved manifest gets committed by
  accident. Consider grepping for `arn:aws:iam::[0-9]+:role` in
  `infra/kubernetes/*.yaml` as a pre-deploy sanity check.
- `scripts/set_github_secrets.py` — hardcoded `OWNER`/`REPO` and the old
  account's role ARN / ECR registry.
- `.github/workflows/05-payment-test.yml` — hardcoded ALB hostname from
  the old cluster.

## K8s manifest apply-ordering race (ServiceAccount vs Deployment)

`kubectl apply -f` on a multi-document YAML file applies documents
**sequentially, in file order** — not atomically. `backend.yaml`/
`agent.yaml` originally had `Deployment` before `ServiceAccount`. A deploy
that changes both the image tag *and* the ServiceAccount's IRSA
`role-arn` (exactly what happens once per account, on the first deploy)
applies the Deployment first — Kubernetes immediately schedules a new pod
against the still-stale ServiceAccount annotation, and the IRSA mutating
webhook bakes the **wrong** IAM role ARN into that pod's env vars for its
entire lifetime, before `kubectl apply` even reaches the corrected
ServiceAccount document later in the same file.

Symptom: a pod stuck `CrashLoopBackOff` with `botocore.errorfactory.
InvalidIdentityTokenException` or `AssumeRoleWithWebIdentity` failures,
*despite* `kubectl get serviceaccount ... -o yaml` showing the correct
annotation by the time you check.

Fix (already applied): `ServiceAccount` now comes first in both files.
General rule for any future manifest: **always put ServiceAccount before
the Deployment/Pod that references it**, in every multi-document file.

## Vault Bootstrap's auto-trigger chicken-and-egg

`03-vault-bootstrap.yml` is designed to auto-fire via `workflow_run` after
a **successful** `02 · Deploy` conclusion. On a brand-new account, Deploy's
own eval-regression gate fails (there's no `payments` data yet — see
below), which makes the *whole* Deploy job conclude `failure`, even though
the actual K8s rollout succeeded. Vault Bootstrap then never auto-fires,
Vault's K8s auth roles never get created, `payments-test` pods can never
get certs, real data never gets seeded, the eval gate keeps failing —
fully circular.

**Fix for a new account: manually trigger `03 · Vault Bootstrap` at least
once** (`workflow_dispatch`) rather than waiting for the auto-trigger,
until you've had one fully-green Deploy run.

## `payments-test` image sourcing (ADR 0021 assumption doesn't hold everywhere)

The `payment-api`/`payment-service`/`payment-worker` manifests originally
pulled base images from an ECR mirror (`agentify/log-test-base/*`),
justified by ADR 0021: the `payments` namespace's Fargate profile had no
NAT gateway, so direct Docker Hub pulls timed out. That mirror only exists
when `enable_log_platform_test=true` has been applied (a whole separate,
cost-incurring P15 test harness: Fargate + Firehose + S3/Glue/Athena).

**If the new account has `enable_nat_gateway=true`** (see above — likely,
since that's usually *why* you're setting it), the original constraint no
longer holds: `payments-test`'s Fargate profile is on the same NAT-routed
private subnet, so plain Docker Hub pulls work fine. We switched
`payment-api.yaml`/`payment-service.yaml`/`payment-worker.yaml` to pull
`alpine`/`nginx` directly from Docker Hub, installing `curl`+`jq` inline
via `apk add` in the `vault-cert-init` container instead of relying on a
pre-baked ECR-mirrored tag. **Don't re-enable the P15 test harness just to
mirror two small images** — check whether NAT is already available first.

`payment-worker.yaml` also needed a `CRASH_LOOP` env var added (default
`false`) — the container had never actually crashed despite
`05-payment-test.yml`'s Phase 3 expecting real restart history to build.
Phase 3 now flips it with `kubectl set env` rather than re-applying the
static manifest, and a later step restores it to `false` unconditionally
(`if: always()`) so a crash-looping pod never lingers past the test run.

## EKS managed node groups + a "no public IP" SCP

If the account has an SCP denying `ec2:RunInstances` when
`ec2:AssociatePublicIpAddress=true`, EKS **managed** node groups (unlike
self-managed ones) don't reliably avoid tripping it just by being placed
in a private subnet with `map_public_ip_on_launch=false`. We confirmed via
`aws sts decode-authorization-message` that the denied request had
`ec2:IsLaunchTemplateResource=false` — EKS's own internal node
provisioning doesn't go through the launch template's `NetworkInterfaces`
array the way a plain ASG launch does, so the subnet-level default doesn't
reliably apply.

**Fix:** don't rely on subnet placement alone — set an explicit
`network_interfaces` block on the `eks_managed_node_groups` entry with
`associate_public_ip_address = false`. See `eks.tf`.

**General diagnostic technique for any SCP-related denial** (worth
knowing regardless of this specific issue): AWS deliberately obscures the
real reason in an `EncodedAuthorizationMessage`. Decode it:
```bash
aws sts decode-authorization-message --encoded-message "<the long string>" --query DecodedMessage --output text
```
This names the exact policy statement, principal, and condition that
denied the request — essential before guessing at IAM/SCP fixes.

## CI role missing `secretsmanager:GetResourcePolicy`

A provider-version behavior addition, not something specific to this
account: `aws_secretsmanager_secret`'s `Read` now calls
`GetResourcePolicy` during every `plan`/`refresh`, not just when actually
managing a resource policy. A least-privilege CI role written before this
provider behavior existed won't have this permission. Symptom: `terraform
plan` fails with `AccessDeniedException` on `GetResourcePolicy` for a
secret the role can otherwise read/write fine. Fixed in `iam.tf`.

## Credential-chain gotchas (CloudBoost / corporate temp credentials)

These cost the most wall-clock time this migration, almost entirely
because the failure mode looks identical ("no valid credential sources
found") regardless of which of several different underlying causes is
actually in play. Check in this order:

1. **Terraform's Go SDK cannot read a `login`-type credential** the AWS
   CLI understands fine (`aws sts get-caller-identity` works, Terraform
   still fails). Bridge it explicitly:
   ```bash
   eval "$(aws configure export-credentials --format env)"
   ```
   **Do this in the exact same shell invocation as the Terraform command**
   — shell state (env vars) does not persist between separate command
   invocations in this tooling; combine export + `terraform apply` in one
   call, or `source` it in an interactive session before running Terraform.
2. **Sessions expire — often**, and mid-long-operation (a 20-minute EKS
   node-group create can easily outlast one). If `terraform apply` dies
   partway through with an expired-token error, follow the recovery
   sequence: `terraform state push errored.tfstate` (reconciles whatever
   *did* get created) → `terraform force-unlock <id>` if the lock also
   failed to release → fresh `plan` to see the real gap → `apply` again.
   Never just re-`apply` immediately after an interrupted run without the
   state-push step first — Terraform will warn about a forked state for
   good reason.
3. **A "successful-looking" re-login can still hand back a dead token.**
   If `aws login` reports success but the very next command still fails,
   suspect a cached browser SSO session being silently reused instead of a
   genuinely fresh login (try an incognito window), or as a last resort
   clear `~/.aws/sso/cache/`, `~/.aws/cli/cache/`, and the relevant
   `~/.aws/credentials` profile entirely and start clean.
4. Rule out clock skew (`Get-Date`/`date` vs actual time) before assuming
   anything more exotic — a common, easy-to-miss cause of tokens looking
   expired immediately.

## The actual root cause of "eval gate always says no_data"

This was the deepest and most consequential bug found this migration, and
it had nothing to do with the account migration mechanics above — it's a
genuine bug in `src/adapters/discovery/`. Every `push_*` function
(`push_event`, `push_inventory`, `push_ingress`, `push_health`,
`push_dependency`) unconditionally built its request headers as
`{"Authorization": f"Bearer {collector_token}"}`. With `COLLECTOR_TOKEN`
set to an empty string (the documented, supported single-cluster mode —
see ADR 0027), this produces a header value of `"Bearer "` — which
`httpx` refuses to send at all, raising a client-side error *before the
request goes out*. Every single push had been silently failing since the
account's `agentify-discovery-secret` was created; `current_state`/
`events` were genuinely empty the entire time, which is why every query
came back `status="no_data"` with empty `intent`/`tier` regardless of how
healthy the actual pods were.

**Lesson that generalizes beyond this one bug:** when a design says "an
absent credential is tolerated," verify that claim against the actual
HTTP client's behavior for an *empty string* credential, not just the
server-side handler's tolerance for a *missing* header. Those are
different code paths, and a client library can fail closed (silently,
client-side) in a way that never even reaches the server-side tolerance
you designed and tested.

Fixed by building `headers` conditionally — omitting `Authorization`
entirely when `collector_token` is falsy — across all five `push_*`
functions. `live_relay.py`'s WebSocket connection was *not* affected (the
`websockets` library doesn't share `httpx`'s strict header validation, and
it already logs a clear warning + gets a clean 401 when unset, matching
the documented design).

## Langfuse SDK pitfalls

- `Langfuse(...)`'s host parameter is `host=`, not `base_url=` — confirmed
  by `scripts/run_evals.py`'s own working usage. Fixed in
  `09-validate-langfuse.yml`.
- `lf.create_score(...)` — still throws `'Langfuse' object has no
  attribute 'create_score'` in `scripts/run_evals.py`, even against the
  pinned `langfuse>=2.0.0,<3.0.0` range. **Not yet fixed** — it's a
  non-blocking warning (wrapped in its own try/except, doesn't affect the
  pass/fail gate), so it was deliberately left alone rather than guess at
  the correct replacement method without verifying it against the actual
  installed SDK version. If picking this up: check `pip show langfuse` in
  CI for the exact resolved version, then check that version's real API
  before changing the call.

## Quick reference — symptom → likely cause

| Symptom | Likely cause |
|---|---|
| `terraform init` 403 on an S3 bucket | `backend.tf` (or a workflow's `terraform init` call) still points at the wrong account's bucket |
| `EntityAlreadyExists` on the GitHub OIDC provider | `create_github_oidc_provider` needs to be `false` for this account |
| Pod `CrashLoopBackOff` with `InvalidIdentityTokenException` | Check the *pod's actual injected* `AWS_ROLE_ARN` (`kubectl get pod -o json`, not just the ServiceAccount) — likely the apply-ordering race above |
| EKS node group create fails, "not authorized to launch instances with this launch template" | Decode the authorization message — almost certainly the no-public-IP SCP |
| Every query returns `status="no_data"` despite healthy pods | Check `agentify-discovery`'s own logs for `push_* failed` warnings — don't assume it's a missing-test-data problem before ruling this out |
| `terraform apply` credential errors that don't match what `aws sts get-caller-identity` shows | Terraform's Go SDK vs the CLI reading credentials differently — see "Credential-chain gotchas" |
