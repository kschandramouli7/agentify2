#!/usr/bin/env bash
# safe-kubectl-apply.sh — kubectl apply, refusing any manifest that still
# carries a CI-only placeholder.
#
# infra/kubernetes/{backend,agent,discovery,frontend}.yaml are templates:
# 02-deploy.yml checks them out fresh and sed's in the real ECR registry host,
# the commit-pinned image tag, and the Terraform-output IRSA role ARNs before
# ever running kubectl apply — the checked-in copies in this repo always keep
# the placeholders (ACCOUNT_ID.dkr.ecr..., :latest, REPLACE_WITH_TERRAFORM_
# OUTPUT_*_irsa_role_arn). Found live (2026-09-13): a plain `kubectl apply -f
# infra/kubernetes/backend.yaml` would have rolled the running image back to
# the literal, unpullable "ACCOUNT_ID..." string — caught before it ran, not
# after, but the near-miss is why this script exists instead of a comment
# alone.
#
# For a live one-off change (an env var, a resource limit, anything that
# isn't the image/registry/role-arn), use `kubectl set env` or `kubectl
# patch` against the specific field instead of re-applying the whole file —
# see ROADMAP OPS-2's fix for a worked example. To actually deploy a
# manifest change, push it through 02-deploy.yml, which resolves every
# placeholder correctly.
#
# Usage:
#   scripts/safe-kubectl-apply.sh -f <manifest.yaml> [any other kubectl apply args]
#
# Every -f/--filename argument is checked; any other kubectl apply usage
# (e.g. -k for kustomize, or reading stdin) is rejected — this script only
# has coverage for the plain-file case the footgun above actually happened on.

set -euo pipefail

PLACEHOLDER_PATTERN='ACCOUNT_ID|REPLACE_WITH_TERRAFORM_OUTPUT'

files=()
args=("$@")
i=0
while [ "$i" -lt "$#" ]; do
	arg="${args[$i]}"
	case "$arg" in
	-f | --filename)
		i=$((i + 1))
		files+=("${args[$i]}")
		;;
	-f=* | --filename=*)
		files+=("${arg#*=}")
		;;
	esac
	i=$((i + 1))
done

if [ "${#files[@]}" -eq 0 ]; then
	echo "safe-kubectl-apply.sh: no -f/--filename argument found — this script only" >&2
	echo "checks the plain-file case. Use kubectl apply directly for -k/kustomize or" >&2
	echo "stdin, after confirming by hand that nothing you're applying is templated." >&2
	exit 1
fi

for f in "${files[@]}"; do
	if [ -f "$f" ] && grep -qE "$PLACEHOLDER_PATTERN" "$f"; then
		echo "safe-kubectl-apply.sh: REFUSING to apply $f" >&2
		echo "" >&2
		echo "It still contains a CI-only placeholder (ACCOUNT_ID.dkr.ecr... or" >&2
		echo "REPLACE_WITH_TERRAFORM_OUTPUT_*). Applying it as-is would push that" >&2
		echo "literal placeholder into the live Deployment spec — including rolling" >&2
		echo "the running container image back to an unpullable string." >&2
		echo "" >&2
		echo "For a one-off live change, patch the specific field instead:" >&2
		echo "  kubectl set env deployment/<name> -n agentify SOME_VAR=value" >&2
		echo "  kubectl patch deployment/<name> -n agentify --type=strategic -p '...'" >&2
		echo "" >&2
		echo "To actually deploy a manifest change, push it through 02-deploy.yml," >&2
		echo "which resolves every placeholder before applying." >&2
		exit 1
	fi
done

exec kubectl apply "$@"
