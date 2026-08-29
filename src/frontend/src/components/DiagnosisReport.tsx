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

function EventsResult({ events, failed }: { events: EventRow[]; failed: { cluster_id: string; error: string }[] }) {
  if (events.length === 0) {
    return <div className="diag-result-empty">No recent events.<FanoutFailures failed={failed} /></div>;
  }
  return (
    <div>
      <ul className="diag-result-events">
        {events.map((e, i) => (
          <li key={i} className={`diag-result-events__item diag-result-events__item--${e.type === "Warning" ? "warn" : "muted"}`}>
            <span className="diag-result-events__reason">{e.reason ?? e.type ?? "Event"}</span>
            {e.involved_object && <span className="diag-result-table__mono diag-result-events__object"> {e.involved_object}</span>}
            {e.cluster_id && <span className="diag-result-events__cluster"> · {e.cluster_id}</span>}
            <div className="diag-result-events__message">{e.message}</div>
            {e.last_timestamp && <div className="diag-result-events__time">{e.last_timestamp}</div>}
          </li>
        ))}
      </ul>
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
                <td className="diag-result-table__mono diag-result-table__muted">{c.image ?? "–"}</td>
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
      <EventsResult events={events} failed={[]} />
    </div>
  );
}

function ActionResult({ data }: { data: Record<string, unknown> }) {
  const failed = (data.clusters_failed as { cluster_id: string; error: string }[] | undefined) ?? [];

  if (typeof data.logs === "string") return <LogsResult data={data} />;
  if (Array.isArray(data.containers)) return <DescribePodResult data={data} />;
  if (Array.isArray(data.pods)) return <PodsResult pods={data.pods as PodRow[]} failed={failed} />;
  if (Array.isArray(data.certificates)) return <CertificatesResult certs={data.certificates as CertRow[]} />;
  if (Array.isArray(data.events)) return <EventsResult events={data.events as EventRow[]} failed={failed} />;

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
