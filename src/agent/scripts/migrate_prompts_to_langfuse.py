"""Seed the K8fy skill prompts into Langfuse.

Run after setting credentials:

    export LANGFUSE_PUBLIC_KEY=pk-lf-...
    export LANGFUSE_SECRET_KEY=sk-lf-...
    export LANGFUSE_BASE_URL=https://us.cloud.langfuse.com  # MUST match the agent's

    cd src/agent
    python scripts/migrate_prompts_to_langfuse.py            # seed only what's missing
    python scripts/migrate_prompts_to_langfuse.py --force     # re-push every prompt
    python scripts/migrate_prompts_to_langfuse.py k8fy/diagnose  # only these names

**Default is seed-only-if-absent.** Prompts already present in Langfuse are left
untouched, because the copy there is the live one and may carry edits made in the
UI that do not exist in prompts.py. The previous version of this script always
created a new version of *every* prompt and moved the "production" label to it,
which silently reverted any such edit — dangerous once prompts are actually being
maintained in Langfuse.

Use --force only when you intend prompts.py to overwrite Langfuse.

The set seeded is k8fy.prompts.ALL_PROMPTS — the same registry the runtime
prefetches from, so a prompt cannot be fetched at runtime yet missed here
(which is how three prompts sat on permanent silent fallback until 2026-08-29;
ROADMAP P19 gap A).
"""

import argparse
import logging
import os
import sys

# Ensure the agent src root is on the path so we can import local modules.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from langfuse import Langfuse  # noqa: E402 — after sys.path manipulation

from k8fy.prompt_manager import PRODUCTION_LABEL  # noqa: E402
from k8fy.prompts import ALL_PROMPTS  # noqa: E402

# Seeded set = every prompt the runtime fetches. See k8fy/prompts.py.
PROMPTS = ALL_PROMPTS


def _connect(public_key: str, secret_key: str, base_url: str) -> Langfuse:
    """Build a client, tolerating the v2 (`host=`) / v3+ (`base_url=`) rename."""
    creds = {"public_key": public_key, "secret_key": secret_key}
    try:
        return Langfuse(base_url=base_url, **creds)
    except TypeError:
        return Langfuse(host=base_url, **creds)


class SeedError(RuntimeError):
    """A failure that must abort the run rather than be treated as 'absent'."""


def _exists(lf: Langfuse, name: str) -> bool:
    """True if *name* already has a version carrying the production label.

    Only a genuine not-found counts as absent. Any other failure (auth, wrong
    host, network) is raised: treating it as "absent" would make the run fall
    through to create_prompt and move the production label — creating a
    duplicate version and clobbering the Langfuse-side copy, which is exactly
    what seed-only-if-absent exists to prevent. Seen live 2026-08-30: a 401 from
    a wrong-region host was read as "absent" for all 11 prompts.
    """
    # A miss is the expected case when seeding, but the SDK logs it at ERROR
    # ("Error while fetching prompt ... 404"), which makes a successful seed read
    # like a failure. Quieten the SDK for the probe only; genuine problems are
    # still raised below and reported by the caller.
    lf_log = logging.getLogger("langfuse")
    prior = lf_log.level
    lf_log.setLevel(logging.CRITICAL)
    try:
        lf.get_prompt(name, label=PRODUCTION_LABEL, cache_ttl_seconds=0)
        return True
    except Exception as exc:
        text = str(exc).lower()
        if "not found" in text or "404" in text:
            return False
        raise SeedError(f"could not determine whether {name!r} exists: {exc}") from exc
    finally:
        lf_log.setLevel(prior)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("names", nargs="*", help="only seed these prompt names (default: all)")
    ap.add_argument(
        "--label",
        default=PRODUCTION_LABEL,
        help=f"label for the created version (default: {PRODUCTION_LABEL}). Use "
             "'staging' to publish a CANDIDATE for the promotion gate without "
             "touching live traffic.",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="re-push prompts that already exist, creating a new version and moving "
             "the production label (overwrites Langfuse-side edits)",
    )
    args = ap.parse_args()

    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY", "")
    # Default must track config/settings.py's langfuse_base_url. They disagreed
    # until 2026-08-30 (this script defaulted to the EU host, the agent to US),
    # which seeds prompts into a different region than the agent reads from and
    # surfaces as a confusing 401 rather than an obvious misconfiguration.
    base_url   = os.environ.get("LANGFUSE_BASE_URL", "https://us.cloud.langfuse.com")

    if not public_key or not secret_key:
        print(
            "ERROR: LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY must be set.\n"
            "       Find them in Langfuse UI → Settings → API Keys.",
            file=sys.stderr,
        )
        sys.exit(1)

    # A candidate label is always a NEW version by definition, so
    # seed-only-if-absent must not apply: the prompt almost certainly already
    # exists on `production`, and skipping would mean never being able to publish
    # a candidate at all.
    publishing_candidate = args.label != PRODUCTION_LABEL

    selected = [(n, c) for n, c in PROMPTS if not args.names or n in set(args.names)]
    if args.names:
        unknown = set(args.names) - {n for n, _ in PROMPTS}
        if unknown:
            print(f"ERROR: unknown prompt name(s): {', '.join(sorted(unknown))}", file=sys.stderr)
            sys.exit(1)

    lf = _connect(public_key, secret_key, base_url)

    # Verify before touching anything: the common failure is valid keys sent to
    # the wrong data region, which every subsequent call reports as a 401.
    try:
        ok = lf.auth_check()
    except Exception as exc:
        ok = False
        print(f"  auth_check raised: {exc}", file=sys.stderr)
    if not ok:
        print(
            f"ERROR: Langfuse rejected these credentials at {base_url}\n"
            "       The keys are usually right and the HOST wrong — projects are\n"
            "       region-scoped. Set LANGFUSE_BASE_URL to the region holding your\n"
            "       project, and make sure it matches the agent's\n"
            "       config/settings.py langfuse_base_url:\n"
            "         US  https://us.cloud.langfuse.com\n"
            "         EU  https://cloud.langfuse.com\n"
            "         or your self-hosted URL",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Connected to Langfuse at {base_url}")
    if publishing_candidate:
        mode = f"publish CANDIDATE on label '{args.label}' (live traffic untouched)"
    elif args.force:
        mode = "FORCE re-push (moves the production label)"
    else:
        mode = "seed only if absent"
    print(f"Mode: {mode}\n")

    created = skipped = failed = 0
    created_names: list = []
    for name, content in selected:
        if not args.force and not publishing_candidate:
            try:
                present = _exists(lf, name)
            except SeedError as exc:
                print(f"\nABORTED: {exc}", file=sys.stderr)
                print("         Nothing was created. Fix the connection and re-run.", file=sys.stderr)
                sys.exit(1)
            if present:
                print(f"  SKIP {name}  (already in Langfuse)")
                skipped += 1
                continue
        try:
            prompt = lf.create_prompt(
                name=name,
                type="text",
                prompt=content,
                labels=[args.label],
            )
            print(f"  OK   {name}  (version {prompt.version})")
            created += 1
            created_names.append((name, prompt.version))
        except Exception as exc:
            print(f"  ERR  {name}  FAILED: {exc}", file=sys.stderr)
            failed += 1

    print(f"\nCreated {created}, skipped {skipped}, failed {failed}.")
    print("Verify in Langfuse UI → Prompts (filter by \"k8fy/\").")
    if publishing_candidate and created:
        print(f"\nCandidate published on '{args.label}'. It is NOT serving traffic.")
        print("Next: GitHub Actions → '10 · Prompt promotion gate' → Run workflow with")
        for name, version in created_names:
            # prompt_name is REQUIRED: the eval dataset spans several prompts, and an
            # unscoped pin would resolve the others at this label, 404, and fall back
            # to their local strings — scoring one candidate plus several fallbacks.
            print(f"    prompt_name={name}  prompt_label={args.label}   (version {version})")
        print("Then move the 'production' label yourself if it passes — the gate never promotes.")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
