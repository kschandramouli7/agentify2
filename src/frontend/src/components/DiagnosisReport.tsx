import { useState } from "react";
import { runLiveTool, type ChatMessageDetails, type RecommendedAction } from "../api";

function statusIcon(status: string): string {
  if (status === "healthy") return "✓";
  if (status === "degraded") return "⚠";
  if (["unhealthy", "critical", "error"].includes(status)) return "✗";
  return "–";
}

function statusClass(status: string): string {
  if (status === "healthy") return "ok";
  if (status === "degraded") return "warn";
  if (["unhealthy", "critical", "error"].includes(status)) return "crit";
  return "muted";
}

// ── Collapsible section ──────────────────────────────────────────────────────

function Section({
  title, items, defaultOpen = true,
}: {
  title: string;
  items: string[];
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  if (items.length === 0) return null;
  return (
    <div className="diag-section">
      <button className="diag-section__toggle" type="button" onClick={() => setOpen(o => !o)}>
        {open ? "▾" : "▸"} {title}
      </button>
      {open && (
        <ul className="diag-section__list">
          {items.map((item, i) => <li key={i}>{item}</li>)}
        </ul>
      )}
    </div>
  );
}

// ── Runnable recommended action ──────────────────────────────────────────────

type RunState = "idle" | "running" | "ok" | "error";

// ── Human-readable rendering of a live-tool-call result ──────────────────────
// The Hub/Agent always return JSON; this maps each known live_* tool's shape
// (live_list_pods/live_get_events/live_get_pod_logs/live_describe_pod/
// live_get_certificates, plus ADR 0028's fan-out merge of the first three)
// to a small table/list instead of a raw JSON dump. Anything unrecognized
// falls back to pretty-printed JSON so a new/changed tool shape never shows
// nothing.

// ── What actually ran ────────────────────────────────────────────────────────
// These tools call the Kubernetes API directly (see live_tools.py) — they do NOT
// shell out to kubectl. Both are shown on purpose: the API path is the truth,
// and the kubectl line is what an operator would type to reproduce it. Labelling
// a kubectl command as "the query" would be a small lie that costs trust the
// first time someone runs it and sees different output.
type ToolExplain = { api: string; kubectl: string; why: string };

function explainTool(tool: string, args: Record<string, unknown>): ToolExplain | null {
  const ns = String(args.namespace ?? "");
  const pod = String(args.pod ?? "");
  const nsPath = encodeURIComponent(ns);
  switch (tool) {
    case "live_list_pods":
      return {
        api: `GET /api/v1/namespaces/${nsPath}/pods`,
        kubectl: `kubectl get pods -n ${ns}`,
        why: "Lists every pod in the namespace with its phase, readiness and restart count.",
      };
    case "live_get_events":
      return {
        api: `GET /api/v1/namespaces/${nsPath}/events` +
             (pod ? `?fieldSelector=involvedObject.name=${encodeURIComponent(pod)}` : ""),
        kubectl: pod
          ? `kubectl get events -n ${ns} --field-selector involvedObject.name=${pod}`
          : `kubectl get events -n ${ns}`,
        why: pod
          ? "Recent Kubernetes events for this pod — scheduling, image pulls and restarts appear here first."
          : "Recent Kubernetes events across the namespace, newest first.",
      };
    case "live_describe_pod":
      return {
        api: `GET /api/v1/namespaces/${nsPath}/pods/${encodeURIComponent(pod)}`,
        kubectl: `kubectl describe pod -n ${ns} ${pod}`,
        why: "Container images, per-container state and pod conditions — why a pod is not Ready.",
      };
    case "live_get_pod_logs": {
      const container = args.container ? ` -c ${String(args.container)}` : "";
      const previous = args.previous ? " --previous" : "";
      const tail = args.tail_lines ? ` --tail=${String(args.tail_lines)}` : "";
      return {
        api: `GET /api/v1/namespaces/${nsPath}/pods/${encodeURIComponent(pod)}/log`,
        kubectl: `kubectl logs -n ${ns} ${pod}${container}${previous}${tail}`,
        why: args.previous
          ? "Logs from the PREVIOUS container instance — what it printed before it died."
          : "A bounded tail of the pod's current logs. Secrets are redacted before leaving the cluster.",
      };
    }
    case "live_get_certificates":
      return {
        api: `GET /api/v1/namespaces/${nsPath}/secrets (type=kubernetes.io/tls)`,
        kubectl: `kubectl get secrets -n ${ns} --field-selector type=kubernetes.io/tls`,
        why: "TLS secrets with expiry dates. Days-until-expiry is computed here, never by the model.",
      };
    default:
      return null;
  }
}

// A registry host plus a deep path makes every row unreadable and pushes the tag
// — the part that matters — off the end. Keep the last two segments.
function shortImage(image?: string): string {
  if (!image) return "–";
  const parts = image.split("/");
  return parts.length <= 2 ? image : `…/${parts.slice(-2).join("/")}`;
}

// Kubernetes repeats an event every back-off cycle. Collapse identical
// reason+message pairs into one row with a count, keeping the newest timestamp,
// so an ImagePullBackOff loop reads as one line rather than a dozen.
function dedupeEvents(events: EventRow[]): (EventRow & { occurrences: number })[] {
  const out: (EventRow & { occurrences: number })[] = [];
  const index = new Map<string, number>();
  for (const e of events) {
    const key = `${e.reason ?? ""}|${e.message ?? ""}|${e.involved_object ?? ""}`;
    const at = index.get(key);
    if (at === undefined) {
      index.set(key, out.length);
      out.push({ ...e, occurrences: 1 });
    } else {
      out[at].occurrences += 1;
    }
  }
  return out;
}

type PodRow = { name: string; node?: string; phase?: string; ready?: boolean; restart_count?: number; cluster_id?: string };
type EventRow = { type?: string; reason?: string; message?: string; last_timestamp?: string; involved_object?: string; cluster_id?: string };
type CertRow = { name: string; common_name?: string; expiry_date?: string; days_until_expiry?: number; cluster_id?: string };
type ContainerRow = { name: string; image?: string; ready?: boolean; restart_count?: number; state?: string; last_state?: string | null };
type ConditionRow = { type?: string; status?: string; reason?: string };

function FanoutFailures({ failed }: { failed: { cluster_id: string; error: string }[] }) {
  if (failed.length === 0) return null;
  return (
    <div className="diag-result-fanout-warn">
      {failed.length} cluster{failed.length > 1 ? "s" : ""} didn't answer:{" "}
      {failed.map((f, i) => (
        <span key={f.cluster_id}>
          {i > 0 && ", "}
          <code>{f.cluster_id}</code> ({f.error})
        </span>
      ))}
    </div>
  );
}

function PodsResult({ pods, failed }: { pods: PodRow[]; failed: { cluster_id: string; error: string }[] }) {
  const showCluster = pods.some(p => p.cluster_id);
  if (pods.length === 0) {
    return <div className="diag-result-empty">No pods found.<FanoutFailures failed={failed} /></div>;
  }
  return (
    <div>
      <table className="diag-result-table">
        <thead>
          <tr>
            <th>Pod</th><th>Node</th><th>Status</th><th>Restarts</th>
            {showCluster && <th>Cluster</th>}
          </tr>
        </thead>
        <tbody>
          {pods.map(p => (
            <tr key={`${p.cluster_id ?? ""}/${p.name}`}>
              <td className="diag-result-table__mono">{p.name}</td>
              <td className="diag-result-table__mono diag-result-table__muted">{p.node ?? "–"}</td>
              <td>
                <span className={`diag-pod-status diag-pod-status--${p.ready ? "ok" : "warn"}`}>
                  {p.phase ?? "Unknown"}
                </span>
              </td>
              <td className={p.restart_count ? "diag-result-table__warn" : undefined}>{p.restart_count ?? 0}</td>
              {showCluster && <td className="diag-result-table__mono">{p.cluster_id}</td>}
            </tr>
          ))}
        </tbody>
      </table>
      <FanoutFailures failed={failed} />
    </div>
  );
}

function truncate(text: string | undefined, max: number): string {
  if (!text) return "";
  const flat = text.replace(/\s+/g, " ").trim();
  return flat.length <= max ? flat : `${flat.slice(0, max)}…`;
}

function EventsResult({
  events, failed, limit = 5, showObject = false,
}: {
  events: EventRow[];
  failed: { cluster_id: string; error: string }[];
  limit?: number;
  // The pod name is already in the action's title and the describe header, so
  // repeating involved_object on every row is noise. Only useful for a
  // namespace-wide event list, where the rows really are different objects.
  showObject?: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  if (events.length === 0) {
    return <div className="diag-result-empty">No recent events.<FanoutFailures failed={failed} /></div>;
  }
  const deduped = dedupeEvents(events);
  const shown = expanded ? deduped : deduped.slice(0, limit);
  return (
    <div>
      <ul className="diag-result-events">
        {shown.map((e, i) => (
          <li key={i} className={`diag-result-events__item diag-result-events__item--${e.type === "Warning" ? "warn" : "muted"}`}>
            <span className="diag-result-events__reason">{e.reason ?? e.type ?? "Event"}</span>
            {e.occurrences > 1 && <span className="diag-result-events__cluster"> ×{e.occurrences}</span>}
            {showObject && e.involved_object && <span className="diag-result-table__mono diag-result-events__object"> {e.involved_object}</span>}
            {e.cluster_id && <span className="diag-result-events__cluster"> · {e.cluster_id}</span>}
            {/* Full text on hover — an image-pull error repeats the whole
                registry path three times and buries the one word that matters. */}
            <div className="diag-result-events__message" title={e.message}>{truncate(e.message, 180)}</div>
            {e.last_timestamp && <div className="diag-result-events__time">{e.last_timestamp}</div>}
          </li>
        ))}
      </ul>
      {deduped.length > shown.length && (
        <button type="button" className="diag-result-more" onClick={() => setExpanded(true)}>
          show {deduped.length - shown.length} more
        </button>
      )}
      <FanoutFailures failed={failed} />
    </div>
  );
}

function LogsResult({ data }: { data: Record<string, unknown> }) {
  const pod = typeof data.pod === "string" ? data.pod : null;
  const container = typeof data.container === "string" ? data.container : null;
  const previous = data.previous === true;
  const logs = typeof data.logs === "string" ? data.logs : "";
  return (
    <div>
      <div className="diag-result-meta">
        {pod ?? "pod"}{container ? ` / ${container}` : ""}{previous ? " (previous instance)" : ""}
      </div>
      <pre className="diag-action__output diag-action__output--ok">{logs || "(no log output)"}</pre>
    </div>
  );
}

function CertificatesResult({ certs }: { certs: CertRow[] }) {
  if (certs.length === 0) return <div className="diag-result-empty">No TLS certificates found.</div>;
  return (
    <table className="diag-result-table">
      <thead><tr><th>Secret</th><th>Common name</th><th>Expires</th><th>Days left</th></tr></thead>
      <tbody>
        {certs.map(c => (
          <tr key={c.name}>
            <td className="diag-result-table__mono">{c.name}</td>
            <td>{c.common_name ?? "–"}</td>
            <td className="diag-result-table__mono">{c.expiry_date ?? "–"}</td>
            <td className={c.days_until_expiry !== undefined && c.days_until_expiry < 14 ? "diag-result-table__warn" : undefined}>
              {c.days_until_expiry ?? "–"}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function DescribePodResult({ data }: { data: Record<string, unknown> }) {
  const containers = (data.containers as ContainerRow[] | undefined) ?? [];
  const conditions = (data.conditions as ConditionRow[] | undefined) ?? [];
  const events = (data.events as EventRow[] | undefined) ?? [];
  return (
    <div className="diag-result-describe">
      <div className="diag-result-meta">
        {String(data.pod ?? "pod")} — {String(data.phase ?? "Unknown")} on <span className="diag-result-table__mono">{String(data.node ?? "–")}</span>
      </div>
      {containers.length > 0 && (
        <table className="diag-result-table">
          <thead><tr><th>Container</th><th>Image</th><th>Ready</th><th>Restarts</th><th>State</th></tr></thead>
          <tbody>
            {containers.map(c => (
              <tr key={c.name}>
                <td className="diag-result-table__mono">{c.name}</td>
                <td className="diag-result-table__mono diag-result-table__muted" title={c.image}>{shortImage(c.image)}</td>
                <td>{c.ready ? "✓" : "✗"}</td>
                <td className={c.restart_count ? "diag-result-table__warn" : undefined}>{c.restart_count ?? 0}</td>
                <td>{c.last_state && c.last_state !== "unknown" ? `${c.state} (was ${c.last_state})` : c.state}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {conditions.length > 0 && (
        <ul className="diag-result-events">
          {conditions.map((c, i) => (
            <li key={i} className={`diag-result-events__item diag-result-events__item--${c.status === "True" ? "muted" : "warn"}`}>
              <span className="diag-result-events__reason">{c.type}</span>: {c.status}{c.reason ? ` (${c.reason})` : ""}
            </li>
          ))}
        </ul>
      )}
      {/* Tighter cap here: the "check events" action already renders the full
          list, so repeating it in full made the two actions near-identical. */}
      <EventsResult events={events} failed={[]} limit={3} />
    </div>
  );
}

function ActionResult({ data }: { data: Record<string, unknown> }) {
  const failed = (data.clusters_failed as { cluster_id: string; error: string }[] | undefined) ?? [];

  if (typeof data.logs === "string") return <LogsResult data={data} />;
  if (Array.isArray(data.containers)) return <DescribePodResult data={data} />;
  if (Array.isArray(data.pods)) return <PodsResult pods={data.pods as PodRow[]} failed={failed} />;
  if (Array.isArray(data.certificates)) return <CertificatesResult certs={data.certificates as CertRow[]} />;
  if (Array.isArray(data.events)) return <EventsResult events={data.events as EventRow[]} failed={failed} showObject />;

  // Unrecognized shape — pretty JSON, never silently show nothing.
  return <pre className="diag-action__output diag-action__output--ok">{JSON.stringify(data, null, 2)}</pre>;
}

function ActionRow({ action }: { action: RecommendedAction }) {
  const [state, setState] = useState<RunState>("idle");
  const [result, setResult] = useState<Record<string, unknown> | null>(null);
  const [errorText, setErrorText] = useState<string>("");

  async function handleRun() {
    setState("running");
    try {
      const runResult = await runLiveTool(action.tool, action.arguments);
      const data = runResult.data as Record<string, unknown>;
      if (data && typeof data.error === "string") {
        setState("error");
        setErrorText(data.error);
      } else {
        setState("ok");
        setResult(data);
      }
    } catch (e) {
      setState("error");
      setErrorText(e instanceof Error ? e.message : "Run failed.");
    }
  }

  const explain = explainTool(action.tool, action.arguments ?? {});

  return (
    <div className="diag-action">
      <div className="diag-action__row">
        <span className={`diag-action__dot diag-action__dot--${state}`} />
        <span className="diag-action__label">{action.label}</span>
        <button
          className="diag-action__run"
          type="button"
          onClick={handleRun}
          disabled={state === "running"}
        >
          {state === "running" ? "Running…" : "Run"}
        </button>
      </div>
      {/* Shown BEFORE running, not after: an operator should be able to see what
          a button will do before pressing it. The API path is what actually
          executes; the kubectl line is the reproducible equivalent, labelled as
          such rather than pretending kubectl ran. */}
      {explain && (
        <div className="diag-action__explain">
          <div className="diag-action__why">{explain.why}</div>
          <code className="diag-action__cmd" title="Equivalent kubectl command — for you to reproduce it">
            {explain.kubectl}
          </code>
          <code className="diag-action__api" title="The Kubernetes API request this actually issues">
            {explain.api}
          </code>
        </div>
      )}
      {state === "error" && errorText && (
        <pre className="diag-action__output diag-action__output--error">{errorText}</pre>
      )}
      {state === "ok" && result && <ActionResult data={result} />}
    </div>
  );
}

// ── Main report ───────────────────────────────────────────────────────────────

export function DiagnosisReport({ details }: { details: ChatMessageDetails }) {
  const sev = statusClass(details.severity ?? details.status ?? "info");
  const findingsAsText = (details.findings ?? []).map(f =>
    typeof f === "string" ? f : JSON.stringify(f),
  );

  return (
    <div className={`diag-report diag-report--${sev}`}>
      {(details.incident_summary || details.status) && (
        <div className={`diag-banner diag-banner--${sev}`}>
          <span className="diag-banner__icon">{statusIcon(details.status ?? "")}</span>
          <span className="diag-banner__text">{details.incident_summary || details.status}</span>
        </div>
      )}

      <Section title="What happened" items={details.timeline ?? []} />
      <Section title="Findings" items={findingsAsText} />

      {details.likely_cause && (
        <div className="diag-callout">
          <span className="diag-callout__label">Likely cause</span>
          <span className="diag-callout__text">{details.likely_cause}</span>
        </div>
      )}

      <Section title="Recommendations" items={details.recommendations ?? []} defaultOpen={false} />

      {(details.recommended_actions ?? []).length > 0 && (
        <div className="diag-section">
          <div className="diag-section__title">Recommended actions</div>
          {(details.recommended_actions ?? []).map((a, i) => <ActionRow key={i} action={a} />)}
        </div>
      )}
    </div>
  );
}
