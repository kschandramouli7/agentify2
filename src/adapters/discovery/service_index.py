"""service_index.py — which Service does a pod (or Deployment) belong to.

WHY THIS MODULE EXISTS

The Hub stores per-pod state (`phase`, `ready`, `restarts`) and per-deployment
state (`images`, `replicas_desired`), and it stores every Service's label
selector in `cluster_services.selector`. It could not join the two: the pod
event payload carried no labels, so nothing Hub-side could say "these three
pods belong to payment-api". Anything wanting per-SERVICE health or version —
ROADMAP P22's risk notes, most obviously — was blocked on data that was
already being collected.

The fix is to attribute at push time rather than ship labels the Hub would
then have to match. The collector already performs exactly this matching for
dependency mining (`main._service_for_pod`), so the logic moves here and both
callers share it.

WHY AN INDEX RATHER THAN A LOOKUP

`watch.py`'s handlers receive one object at a time from a watch stream and have
no Services list in scope. Fetching Services per pod event would be one API
call per event — unacceptable on a busy cluster. But the **Services watch is
already running**, so the same stream that pushes service events can maintain a
namespace -> Services index for free, with no additional API calls at all.

`main.py`'s scan loop also seeds the index every cycle from the `list_services`
call it already makes. That covers the startup window where the pods watch may
deliver events before the services watch has been heard from — see
RESOLUTION COVERAGE below for what happens when it doesn't.

RESOLUTION COVERAGE

Attribution can legitimately fail: a pod matched by no Service (a bare Job, a
standalone debug pod), a Service with an empty selector, or an event arriving
before its namespace is indexed. That is fine, and `service` is simply absent.

What is NOT fine is not knowing how often it fails. This module therefore
counts every attempt and outcome, and the caller logs the rate periodically.
Every field added to this pipeline needs its own capture-rate counter or it
will be trusted when it is empty (ROADMAP P27) — this codebase has shipped
that bug enough times to stop guessing.
"""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def service_for_labels(
    labels: Optional[Dict[str, str]], services: List[Dict[str, Any]]
) -> Optional[str]:
    """Which Service (by name) a set of pod labels belongs to.

    Uses the same label-selector semantics K8s itself uses to build Service
    endpoints — every selector key must match — rather than a pod-name
    heuristic. A Service with an empty selector (e.g. manually-managed
    Endpoints) never matches, because an empty selector in K8s selects
    nothing for endpoint purposes here.
    """
    if not labels:
        return None
    for svc in services:
        selector = svc.get("selector")
        if selector and all(labels.get(k) == v for k, v in selector.items()):
            return svc["name"]
    return None


class ServiceIndex:
    """namespace -> Services, maintained from the Services watch and the scan.

    Not thread-safe by design: everything in this collector runs on one
    asyncio loop, and adding a lock would imply a concurrency model that does
    not exist here.
    """

    def __init__(self) -> None:
        self._by_namespace: Dict[str, List[Dict[str, Any]]] = {}
        self.attempts = 0
        self.resolved = 0
        self.unindexed = 0  # attempts against a namespace we have never seen

    # ── maintenance ──────────────────────────────────────────────────────────

    def seed(self, namespace: str, services: List[Dict[str, Any]]) -> None:
        """Replace a namespace's Services wholesale — used by the scan loop,
        which fetches the authoritative list every cycle."""
        self._by_namespace[namespace] = list(services)

    def apply_service_event(self, event_type: str, svc: Dict[str, Any]) -> None:
        """Fold one Services-watch event into the index.

        DELETED removes; anything else upserts by name. An unknown event type
        is treated as an upsert on purpose: a MODIFIED-like event we do not
        recognise should refresh the selector rather than be dropped.
        """
        metadata = svc.get("metadata", {})
        namespace = metadata.get("namespace", "")
        name = metadata.get("name", "")
        if not namespace or not name:
            return
        entries = self._by_namespace.setdefault(namespace, [])
        # Rebuild without this name, then re-add unless deleted. Selectors
        # change on MODIFIED, so replacing beats mutating in place.
        remaining = [e for e in entries if e.get("name") != name]
        if event_type != "DELETED":
            remaining.append({"name": name, "selector": (svc.get("spec", {}) or {}).get("selector") or {}})
        self._by_namespace[namespace] = remaining

    # ── use ──────────────────────────────────────────────────────────────────

    def resolve(self, namespace: str, labels: Optional[Dict[str, str]]) -> Optional[str]:
        """The Service owning these labels, or None. Records the outcome."""
        self.attempts += 1
        services = self._by_namespace.get(namespace)
        if services is None:
            self.unindexed += 1
            return None
        name = service_for_labels(labels, services)
        if name is not None:
            self.resolved += 1
        return name

    def coverage(self) -> Dict[str, Any]:
        """Attribution stats, for the periodic log line. `rate` is None rather
        than 0.0 when nothing has been attempted, so "no data" is never
        reported as "0% resolved"."""
        return {
            "attempts": self.attempts,
            "resolved": self.resolved,
            "unindexed_namespace": self.unindexed,
            "rate": (self.resolved / self.attempts) if self.attempts else None,
            "namespaces_indexed": len(self._by_namespace),
        }


# Process-wide, because the watch handlers are module-level callbacks with no
# object to hang state off — the same reason watch.py's revision-dedup dict is
# a closure rather than an instance field.
INDEX = ServiceIndex()
