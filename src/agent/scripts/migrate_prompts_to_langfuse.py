"""Seed the K8fy skill prompts into Langfuse.

Run after setting credentials:

    export LANGFUSE_PUBLIC_KEY=pk-lf-...
    export LANGFUSE_SECRET_KEY=sk-lf-...
    export LANGFUSE_BASE_URL=https://cloud.langfuse.com   # or your self-hosted URL

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


def _exists(lf: Langfuse, name: str) -> bool:
    """True if *name* already has a version carrying the production label."""
    try:
        lf.get_prompt(name, label=PRODUCTION_LABEL, cache_ttl_seconds=0)
        return True
    except Exception:
        return False


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("names", nargs="*", help="only seed these prompt names (default: all)")
    ap.add_argument(
        "--force",
        action="store_true",
        help="re-push prompts that already exist, creating a new version and moving "
             "the production label (overwrites Langfuse-side edits)",
    )
    args = ap.parse_args()

    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY", "")
    base_url   = os.environ.get("LANGFUSE_BASE_URL", "https://cloud.langfuse.com")

    if not public_key or not secret_key:
        print(
            "ERROR: LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY must be set.\n"
            "       Find them in Langfuse UI → Settings → API Keys.",
            file=sys.stderr,
        )
        sys.exit(1)

    selected = [(n, c) for n, c in PROMPTS if not args.names or n in set(args.names)]
    if args.names:
        unknown = set(args.names) - {n for n, _ in PROMPTS}
        if unknown:
            print(f"ERROR: unknown prompt name(s): {', '.join(sorted(unknown))}", file=sys.stderr)
            sys.exit(1)

    lf = _connect(public_key, secret_key, base_url)
    print(f"Connected to Langfuse at {base_url}")
    print(f"Mode: {'FORCE re-push' if args.force else 'seed only if absent'}\n")

    created = skipped = failed = 0
    for name, content in selected:
        if not args.force and _exists(lf, name):
            print(f"  SKIP {name}  (already in Langfuse)")
            skipped += 1
            continue
        try:
            prompt = lf.create_prompt(
                name=name,
                type="text",
                prompt=content,
                labels=[PRODUCTION_LABEL],
            )
            print(f"  OK   {name}  (version {prompt.version})")
            created += 1
        except Exception as exc:
            print(f"  ERR  {name}  FAILED: {exc}", file=sys.stderr)
            failed += 1

    print(f"\nCreated {created}, skipped {skipped}, failed {failed}.")
    print("Verify in Langfuse UI → Prompts (filter by \"k8fy/\").")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
