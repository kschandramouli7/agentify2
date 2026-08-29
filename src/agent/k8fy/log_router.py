"""log_router.py — the ONE place that decides whether a pod's logs come from
the live cluster or the Glue/Athena log-platform test harness (ADR 0021).

Deliberately not an LLM decision, in either direction this is called:
  - As the Claude-callable `get_logs` tool (tools.py) — Claude just asks for
    logs; which backend answers is invisible to it.
  - As a plain function call from DiagnoseSkill's deterministic prefetch
    (skills/diagnose.py) — still code calling code, no model in the loop.

No registry, no per-namespace configuration: if the log platform is
configured (ATHENA_WORKGROUP/DATABASE/TABLE — see config/settings.py and
infra/kubernetes/agent.yaml), it's tried first for every namespace, since it's
the durable historical store when data exists there. Falls back to the live
cluster when the platform is unconfigured, errors, or simply has no rows for
this namespace/pod (most namespaces were never onboarded to the Firehose
pipeline, so this is the common case, not an error condition).
"""

import logging
from typing import Any, Dict, Optional

from config.settings import get_settings
from k8fy.live_diagnostics import live_get_pod_logs
from k8fy.log_platform import query_athena_logs

logger = logging.getLogger(__name__)


async def get_logs(
    namespace: str,
    pod: str,
    container: Optional[str] = None,
    tail_lines: int = 200,
    previous: bool = False,
) -> Dict[str, Any]:
    """Fetch logs for a pod — tries the log platform first when configured,
    falls back to the live cluster on empty results or any error. Same output
    shape either way (`{"namespace", "pod", "logs"}` [+ "container"/"previous"
    for the live-cluster path]).
    """
    settings = get_settings()
    if settings.athena_workgroup and settings.athena_database and settings.athena_table:
        athena_config = {
            "workgroup": settings.athena_workgroup,
            "database": settings.athena_database,
            "table": settings.athena_table,
            "region": settings.aws_region,
        }
        result = await query_athena_logs(namespace, pod, athena_config, tail_lines=tail_lines)
        if "error" in result:
            logger.info("log platform query failed for namespace=%s, falling back to cluster: %s", namespace, result["error"])
        elif result.get("logs"):
            return result
        # Empty logs (namespace never onboarded to the Firehose pipeline) — fall through.

    return await live_get_pod_logs(
        namespace, pod, container=container, tail_lines=tail_lines, previous=previous,
    )
