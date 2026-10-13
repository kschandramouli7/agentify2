"""log_router.py — the ONE place that decides which log platform answers a
pod's logs: Splunk, the Glue/Athena test harness (ADR 0021), or the live
cluster (ROADMAP P15, ADR 0041 adds the Splunk tier).

Deliberately not an LLM decision, in either direction this is called:
  - As the Claude-callable `get_logs` tool (tools.py) — Claude just asks for
    logs; which backend answers is invisible to it.
  - As a plain function call from DiagnoseSkill's deterministic prefetch
    (skills/diagnose.py) — still code calling code, no model in the loop.

No registry, no per-namespace configuration: each platform is tried, in
order, when it's configured, falling through to the next on an empty result
or any error. Splunk goes first when configured — it represents a
customer's real, already-populated log platform, more authoritative than
Athena's own "test harness" (ADR 0021) when both happen to be configured
(unlikely in practice, but the order is a real decision — see ADR 0041).
Falling all the way through to the live cluster is the common case today:
most namespaces were never onboarded to either platform, which is expected,
not an error condition.
"""

import logging
from typing import Any, Dict, Optional

from config.settings import get_settings
from k8fy.live_diagnostics import live_get_pod_logs
from k8fy.log_platform import query_athena_logs
from k8fy.log_platform_splunk import query_splunk_logs

logger = logging.getLogger(__name__)


async def get_logs(
    namespace: str,
    pod: str,
    container: Optional[str] = None,
    tail_lines: int = 200,
    previous: bool = False,
) -> Dict[str, Any]:
    """Fetch logs for a pod — tries Splunk, then the Athena log platform,
    whichever are configured, falling back to the live cluster on empty
    results or any error. Same output shape either way (`{"namespace",
    "pod", "logs"}` [+ "container"/"previous" for the live-cluster path]).
    """
    settings = get_settings()

    if settings.splunk_url and settings.splunk_index and settings.splunk_token:
        splunk_config = {
            "url": settings.splunk_url,
            "index": settings.splunk_index,
            "token": settings.splunk_token,
            "verify_tls": settings.splunk_verify_tls,
        }
        result = await query_splunk_logs(namespace, pod, splunk_config, tail_lines=tail_lines)
        if "error" in result:
            logger.info("splunk query failed for namespace=%s, trying next tier: %s", namespace, result["error"])
        elif result.get("logs"):
            return result
        # Empty logs (this namespace has no Splunk-ingested events) — fall through.

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
