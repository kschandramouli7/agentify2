import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { listServiceDependencies, type ServiceDependency } from "../api";

// Service-to-service dependency review (ROADMAP P18 use case #2, ADR 0029).
//
// WHY THERE IS NO NODE-LINK DIAGRAM HERE
//
// The obvious build is a boxes-and-arrows graph. It was rejected on the stated
// requirement — "must degrade sanely to hundreds of edges" — because that is
// exactly where a node-link view fails: past ~30 edges it becomes a hairball
// that answers no question, and the layout algorithm needed to delay that is a
// new dependency for a view that is worse than a table at the operator's actual
// question, which is per-service ("what breaks if I restart this?").
//
// So: a focus view (one service, its callers and callees) is primary, a sortable
// edge table is secondary, and a copyable Mermaid block gives the picture where
// pictures are actually good — a doc, a PR, an incident writeup — rendered by
// GitHub with no library shipped here.
//
// COLOR
//
// No categorical series exist in this data, so no hues are assigned by identity.
// evidence_count is a magnitude → one hue, more-is-wider, with the number always
// printed beside it. Freshness is a state → the app's reserved status tokens,
// always with a word, never colour alone. Both reuse the existing token system
// rather than importing a second palette.

const STALE_AFTER_MS = 15 * 60 * 1000; // ~30 scan cycles at the 30s burst interval

function relTime(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime();
  if (!Number.isFinite(diff)) return "—";
  const m = Math.round(diff / 60000);
  if (m < 1) return "just now";
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.round(h / 24)}d ago`;
}

function absTime(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

type Graph = {
  edges: ServiceDependency[];
  services: string[];
  callees: Map<string, ServiceDependency[]>; // from → edges out
  callers: Map<string, ServiceDependency[]>; // to   → edges in
  maxEvidence: number;
  staleCount: number;
};

function buildGraph(edges: ServiceDependency[]): Graph {
  const callees = new Map<string, ServiceDependency[]>();
  const callers = new Map<string, ServiceDependency[]>();
  const services = new Set<string>();
  let maxEvidence = 0;
  let staleCount = 0;
  const now = Date.now();

  for (const e of edges) {
    services.add(e.from_service);
    services.add(e.to_service);
    (callees.get(e.from_service) ?? callees.set(e.from_service, []).get(e.from_service)!).push(e);
    (callers.get(e.to_service) ?? callers.set(e.to_service, []).get(e.to_service)!).push(e);
    maxEvidence = Math.max(maxEvidence, e.evidence_count);
    if (now - new Date(e.last_seen).getTime() > STALE_AFTER_MS) staleCount += 1;
  }
  return {
    edges,
    services: [...services].sort(),
    callees,
    callers,
    maxEvidence,
    staleCount,
  };
}

// Mermaid rather than an inline diagram: it renders in GitHub, docs and PRs,
// where a dependency picture is genuinely useful, and costs no runtime library.
function toMermaid(g: Graph, namespace: string): string {
  const id = (s: string) => s.replace(/[^a-zA-Z0-9_]/g, "_");
  const lines = [`graph LR`, `  %% ${namespace} — mined service dependencies`];
  for (const s of g.services) lines.push(`  ${id(s)}["${s}"]`);
  for (const e of g.edges) {
    lines.push(`  ${id(e.from_service)} -->|${e.evidence_count}| ${id(e.to_service)}`);
  }
  return lines.join("\n");
}

function StatCard({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="adm-stat">
      <span className="adm-stat__label">{label}</span>
      <span className="adm-stat__value">{value}</span>
      {sub && <span className="adm-stat__sub">{sub}</span>}
    </div>
  );
}

function Freshness({ lastSeen }: { lastSeen: string }) {
  const stale = Date.now() - new Date(lastSeen).getTime() > STALE_AFTER_MS;
  // Word + colour, never colour alone.
  return (
    <span className={`adm-badge adm-badge--${stale ? "warn" : "ok"}`} title={absTime(lastSeen)}>
      {stale ? "stale" : "fresh"} · {relTime(lastSeen)}
    </span>
  );
}

// One hue, more-is-wider. The number is always printed — the bar is a scan aid,
// not the value.
function EvidenceBar({ count, max }: { count: number; max: number }) {
  const pct = max > 0 ? Math.max(4, Math.round((count / max) * 100)) : 0;
  return (
    <span className="topo-evidence" title={`${count} log observations`}>
      <span className="topo-evidence__track">
        <span className="topo-evidence__fill" style={{ width: `${pct}%` }} />
      </span>
      <span className="topo-evidence__num">{count}</span>
    </span>
  );
}

function EdgeList({
  edges, direction, max, onSelect,
}: {
  edges: ServiceDependency[];
  direction: "out" | "in";
  max: number;
  onSelect: (s: string) => void;
}) {
  if (edges.length === 0) {
    return <p className="adm-muted topo-empty">No evidence of {direction === "out" ? "outbound" : "inbound"} calls.</p>;
  }
  return (
    <ul className="topo-edges">
      {edges
        .slice()
        .sort((a, b) => b.evidence_count - a.evidence_count)
        .map(e => {
          const other = direction === "out" ? e.to_service : e.from_service;
          return (
            <li key={e.id} className="topo-edges__item">
              <span className="topo-edges__arrow">{direction === "out" ? "→" : "←"}</span>
              <button type="button" className="topo-link" onClick={() => onSelect(other)}>{other}</button>
              <EvidenceBar count={e.evidence_count} max={max} />
              <Freshness lastSeen={e.last_seen} />
            </li>
          );
        })}
    </ul>
  );
}

export function TopologyPanel() {
  const [namespace, setNamespace] = useState("payments");
  const [applied, setApplied] = useState("payments");
  const [focus, setFocus] = useState<string | null>(null);
  const [showMermaid, setShowMermaid] = useState(false);

  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: ["service-dependencies", applied],
    queryFn: () => listServiceDependencies(applied),
    enabled: applied.length > 0,
  });

  const graph = useMemo(() => buildGraph(data ?? []), [data]);
  const selected = focus && graph.services.includes(focus) ? focus : null;

  return (
    <div className="adm-panel">
      <div className="adm-panel__header">
        <div>
          <h2>Service Dependencies</h2>
          <p className="adm-panel__desc">
            Mined from pod logs, not observed on the network — an edge exists only where a
            caller logged the callee's DNS name. A missing edge means <em>no evidence</em>,
            not <em>no dependency</em>.
          </p>
        </div>
        <div className="adm-filters">
          <input
            className="adm-date-input"
            value={namespace}
            onChange={e => setNamespace(e.target.value)}
            onKeyDown={e => { if (e.key === "Enter") { setApplied(namespace); setFocus(null); } }}
            placeholder="namespace"
            aria-label="Namespace"
          />
          <button
            className="adm-btn adm-btn--ghost"
            type="button"
            onClick={() => { setApplied(namespace); setFocus(null); }}
          >
            Load
          </button>
          <button className="adm-btn adm-btn--ghost" type="button" onClick={() => refetch()}>
            {isFetching ? "Refreshing…" : "Refresh"}
          </button>
        </div>
      </div>

      {isLoading && <p className="adm-loading">Loading…</p>}
      {isError && (
        <p className="adm-error">{error instanceof Error ? error.message : "Failed to load dependencies."}</p>
      )}

      {!isLoading && !isError && graph.edges.length === 0 && (
        <div className="adm-empty">
          <p>No dependency evidence for <strong>{applied}</strong>.</p>
          <p className="adm-muted">
            Expected when nothing in the namespace logs a callee's DNS name. The miner needs
            the caller to have a Service (to attribute <code>from_service</code>) and to print
            the target hostname — see <code>docs/…</code> P18 use case #2.
          </p>
        </div>
      )}

      {graph.edges.length > 0 && (
        <>
          <div className="adm-stats-row">
            <StatCard label="Services" value={String(graph.services.length)} />
            <StatCard label="Edges" value={String(graph.edges.length)} />
            <StatCard
              label="Observations"
              value={graph.edges.reduce((n, e) => n + e.evidence_count, 0).toLocaleString()}
              sub="log mentions counted"
            />
            <StatCard
              label="Stale edges"
              value={String(graph.staleCount)}
              sub={graph.staleCount > 0 ? "no evidence in 15m" : "all fresh"}
            />
          </div>

          <div className="topo-focus">
            <div className="topo-focus__services">
              <span className="adm-muted topo-focus__hint">Focus a service:</span>
              {graph.services.map(s => (
                <button
                  key={s}
                  type="button"
                  className={`topo-chip${selected === s ? " topo-chip--active" : ""}`}
                  onClick={() => setFocus(selected === s ? null : s)}
                >
                  {s}
                </button>
              ))}
            </div>

            {selected && (
              <div className="topo-focus__detail">
                <div className="topo-focus__col">
                  <h4>{selected} calls</h4>
                  <EdgeList
                    edges={graph.callees.get(selected) ?? []}
                    direction="out" max={graph.maxEvidence} onSelect={setFocus}
                  />
                </div>
                <div className="topo-focus__col">
                  <h4>Called by {selected}</h4>
                  <EdgeList
                    edges={graph.callers.get(selected) ?? []}
                    direction="in" max={graph.maxEvidence} onSelect={setFocus}
                  />
                </div>
              </div>
            )}
          </div>

          <div className="adm-table-wrap">
            <table className="adm-table">
              <thead>
                <tr>
                  <th>From</th><th></th><th>To</th><th>Evidence</th><th>Last seen</th><th>First seen</th>
                </tr>
              </thead>
              <tbody>
                {graph.edges
                  .slice()
                  .sort((a, b) => b.evidence_count - a.evidence_count)
                  .map(e => (
                    <tr key={e.id}>
                      <td><button type="button" className="topo-link" onClick={() => setFocus(e.from_service)}>{e.from_service}</button></td>
                      <td className="adm-muted">→</td>
                      <td><button type="button" className="topo-link" onClick={() => setFocus(e.to_service)}>{e.to_service}</button></td>
                      <td><EvidenceBar count={e.evidence_count} max={graph.maxEvidence} /></td>
                      <td><Freshness lastSeen={e.last_seen} /></td>
                      <td className="adm-muted" title={absTime(e.first_seen)}>{relTime(e.first_seen)}</td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>

          <div className="topo-export">
            <button className="adm-btn adm-btn--ghost" type="button" onClick={() => setShowMermaid(v => !v)}>
              {showMermaid ? "Hide" : "Show"} Mermaid diagram
            </button>
            {showMermaid && (
              <>
                <p className="adm-muted topo-export__hint">
                  Paste into a Markdown file, PR or incident writeup — GitHub renders it. Edge
                  labels are observation counts.
                </p>
                <pre className="topo-export__code">{toMermaid(graph, applied)}</pre>
              </>
            )}
          </div>
        </>
      )}
    </div>
  );
}
