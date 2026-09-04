import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  listServiceDependencies, listClusterIngress, listScanCoverage, listServiceProfiles,
  type ServiceDependency,
} from "../api";
import { DependencyFlow, confidence, rarelyObserved, type FlowEdge, type NodeMeta } from "./DependencyFlow";

// Service-to-service dependency review (ROADMAP P18 use case #2, ADR 0029).
//
// LAYOUT: DIAGRAM FIRST, THEN DETAIL
//
// The first version of this panel led with the table and deliberately had no
// diagram, on the argument that a node-link view becomes a hairball at scale.
// Reviewed against the real data, that was wrong: at five edges the table was
// unreadable, because "what calls what" is a shape and a list is a bad way to
// show a shape. The scale concern was real but is handled by DEGRADING —
// DependencyFlow draws one hop when a service is focused and refuses to draw at
// all past its node/edge caps — not by declining to draw.
//
// So: the flow diagram is primary, the focus lists and the sortable table are the
// detail and the text alternative, and the Mermaid block remains for docs and
// PRs.
//
// COLOR
//
// No categorical series exist in this data, so no hues are assigned by identity.
// evidence_count is a magnitude → one hue, more-is-wider, with the number always
// printed beside it. Freshness is a state → the app's reserved status tokens,
// always with a word, never colour alone. Both reuse the existing token system
// rather than importing a second palette.

const STALE_AFTER_MS = 15 * 60 * 1000; // ~15 cycles at the miner's 60s SCAN_INTERVAL_SECONDS,
                                       // so a single missed cycle never reads as stale

// /admin/tracked returns "namespace/service" pairs — the same source the
// observability search box uses. Namespaces are its distinct prefixes.
async function fetchNamespaces(): Promise<string[]> {
  const res = await fetch("/admin/tracked");
  if (!res.ok) return [];
  const pairs = (await res.json()) as string[] | null;
  return [...new Set((pairs ?? []).map(p => p.split("/")[0]).filter(Boolean))].sort();
}

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
  entries: string[];    // nothing observed calling them — where work enters
  terminals: string[];  // observed calling nothing — dependencies of last resort
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
  const all = [...services].sort();
  return {
    edges,
    services: all,
    callees,
    callers,
    maxEvidence,
    staleCount,
    entries: all.filter(s => !callers.has(s)),
    terminals: all.filter(s => !callees.has(s)),
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

// Matches MetricsPanel's markup exactly, which is what .adm-stat's CSS was
// written for: block-level children in value -> label -> sub order (the label's
// `margin-top` only makes sense under the number).
//
// This shipped with <span> children in the reverse order, so all three ran
// together inline — "Services4", "Stale edges0all fresh". The CSS was never
// wrong; the markup was.
// A stat card's sub-line is one short line, not a list — naming 20 services
// there makes every card in the row as tall as the longest one.
function summarise(names: string[], emptyText: string): string {
  if (names.length === 0) return emptyText;
  if (names.length <= 2) return names.join(", ");
  return `${names.slice(0, 2).join(", ")} +${names.length - 2} more`;
}

function StatCard({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="adm-stat">
      <div className="adm-stat__value">{value}</div>
      <div className="adm-stat__label">{label}</div>
      {sub && <div className="adm-stat__sub">{sub}</div>}
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

// One hue, more-is-wider — but note what the quantity IS: the number of scan
// cycles that observed this call, which tracks the caller's log verbosity and
// the edge's age, not its traffic. Read it as confidence. The bar is only an
// in-table scan aid; the number is always printed, and the header names it.
function EvidenceBar({ edge }: { edge: ServiceDependency }) {
  const c = confidence(edge);
  // The bar is coverage, not the count relative to other edges. A raw count is
  // ambiguous without a duration — 318 over 299 scans and 17 over 350 scans are
  // both "large", and only one of them means the call is consistently confirmed.
  const pct = c.coverage === null ? 0 : Math.max(4, Math.round(c.coverage * 100));
  return (
    <span
      className={`topo-evidence topo-evidence--${c.key}`}
      title={
        c.scans === null
          ? `Seen ${edge.evidence_count}x, but too new to judge how consistently.`
          : `Seen in ${edge.evidence_count} of ~${c.scans} scans since first observed ` +
            `(${Math.round((c.coverage ?? 0) * 100)}%) — ${c.label}. ` +
            `Sightings in logs, not requests.`
      }
    >
      <span className="topo-evidence__track">
        <span className="topo-evidence__fill" style={{ width: `${pct}%` }} />
      </span>
      <span className="topo-evidence__num">
        {c.scans === null ? `${edge.evidence_count}` : `${Math.round((c.coverage ?? 0) * 100)}%`}
      </span>
    </span>
  );
}

function EdgeList({
  edges, direction, onSelect,
}: {
  edges: ServiceDependency[];
  direction: "out" | "in";
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
              <EvidenceBar edge={e} />
              <Freshness lastSeen={e.last_seen} />
            </li>
          );
        })}
    </ul>
  );
}

export function TopologyPanel() {
  const [applied, setApplied] = useState("");
  const [typed, setTyped] = useState("");
  const [focus, setFocus] = useState<string | null>(null);
  const [showMermaid, setShowMermaid] = useState(false);

  // Poll while empty (discovery may not have pushed inventory yet), then back
  // off — the same pattern SearchInput uses against this endpoint.
  const { data: namespaces = [] } = useQuery({
    queryKey: ["tracked-namespaces"],
    queryFn: fetchNamespaces,
    refetchInterval: (q) => ((q.state.data ?? []).length === 0 ? 3000 : 30000),
  });

  // Pick a default once, without fighting a later choice: prefer the namespace
  // that actually has test traffic, else the first tracked one.
  useEffect(() => {
    if (applied || namespaces.length === 0) return;
    setApplied(namespaces.includes("payments") ? "payments" : namespaces[0]);
  }, [namespaces, applied]);

  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: ["service-dependencies", applied],
    queryFn: () => listServiceDependencies(applied),
    enabled: applied.length > 0,
  });

  // The three sources that turn a call graph into an architecture view. All
  // best-effort: each degrades to empty rather than blanking the panel, because
  // the mined graph on its own is still worth showing.
  const { data: ingress = [] } = useQuery({
    queryKey: ["cluster-ingress", applied],
    queryFn: () => listClusterIngress(applied),
    enabled: applied.length > 0,
  });
  const { data: coverage = [] } = useQuery({
    queryKey: ["scan-coverage", applied],
    queryFn: () => listScanCoverage(applied),
    enabled: applied.length > 0,
  });
  // What each service IS — workload kind, scale, exposure. Declarative facts
  // read from the Kubernetes objects, so no evidence caveat applies to them.
  const { data: profiles = [] } = useQuery({
    queryKey: ["service-profiles", applied],
    queryFn: () => listServiceProfiles(applied),
    enabled: applied.length > 0,
  });
  // Full service inventory for this namespace, from the same /admin/tracked
  // the namespace picker already reads. This is what makes services with no
  // observed edges appear at all.
  const { data: inventory = [] } = useQuery({
    queryKey: ["tracked-services", applied],
    queryFn: async () => {
      const res = await fetch("/admin/tracked");
      if (!res.ok) return [] as string[];
      const pairs = (await res.json()) as string[] | null;
      return (pairs ?? [])
        .filter(p => p.startsWith(`${applied}/`))
        .map(p => p.slice(applied.length + 1))
        .filter(Boolean);
    },
    enabled: applied.length > 0,
  });

  const graph = useMemo(() => buildGraph(data ?? []), [data]);

  // Declared entry points become synthetic nodes and edges. Their ids are
  // prefixed so they can never collide with a real service name.
  const arch = useMemo(() => {
    const edges: FlowEdge[] = [...(data ?? [])];
    const meta = new Map<string, NodeMeta>();
    const now = new Date().toISOString();

    for (const svc of inventory) meta.set(svc, { kind: "service" });

    // Profiles double as an inventory source: they come from cluster_services,
    // which is the registry itself, so a service present there but missing
    // from /admin/tracked still gets drawn.
    for (const p of profiles) {
      meta.set(p.service, {
        ...(meta.get(p.service) ?? { kind: "service" as const }),
        kind: "service",
        workloadKind: p.workload_kind || undefined,
        replicasReady: p.replicas_ready,
        replicasDesired: p.replicas_desired,
        serviceType: p.service_type || undefined,
        ports: p.ports?.length ? p.ports : undefined,
        image: p.image || undefined,
        schedule: p.schedule || undefined,
      });
    }

    for (const e of ingress) {
      if (!e.backend_service) continue;                 // nothing to point at
      // Prefixed so an entry point can never collide with a service name;
      // `label` is what actually gets drawn.
      const shown = e.host || e.name;
      const id = `ingress:${shown}`;
      meta.set(id, { kind: "ingress", host: shown, label: shown });
      edges.push({
        id: `ingress:${e.kind}:${e.name}:${e.host}:${e.backend_service}`,
        namespace: applied,
        from_service: id,
        to_service: e.backend_service,
        evidence_count: 0,
        // A declared route has no observation window; these satisfy the type
        // and are never read, because `kind: "declared"` short-circuits every
        // confidence and freshness path.
        first_seen: now,
        last_seen: now,
        tenant_id: "",
        kind: "declared",
      });
    }

    for (const c of coverage) {
      const prev = meta.get(c.service) ?? { kind: "service" as const };
      meta.set(c.service, {
        ...prev,
        coverage: c.pods_seen > 0 ? c.pods_sampled / c.pods_seen : null,
        podsSeen: c.pods_seen,
        podsSampled: c.pods_sampled,
      });
    }

    // Services in the inventory that no edge (observed or declared) mentions.
    const referenced = new Set(edges.flatMap(e => [e.from_service, e.to_service]));
    const known = new Set<string>([...inventory, ...profiles.map(p => p.service)]);
    const standalone = [...known].filter(s => !referenced.has(s)).sort();
    return { edges, meta, standalone, known };
  }, [data, ingress, coverage, inventory, profiles, applied]);
  // Surfaced, not just styled: an edge the miner rarely catches implies the
  // graph is missing edges it never catches at all.
  const rare = useMemo(() => rarelyObserved(graph.edges), [graph.edges]);
  const selected = focus && graph.services.includes(focus) ? focus : null;

  return (
    <div className="adm-panel">
      <div className="adm-panel__header">
        <div>
          <h2>Service Dependencies</h2>
          <p className="adm-panel__desc">
            Every service in the namespace, its declared entry points, and the calls actually
            observed between them. <strong>Solid arrows are mined from pod logs</strong> — an
            edge exists only where a caller logged the callee's hostname, so a missing one
            means <em>no evidence</em>, not <em>no dependency</em>, and the counts are scan
            cycles, measuring <em>confidence, not traffic</em>. <strong>Dashed purple arrows
            are declared routes</strong> read from Kubernetes Ingress objects — facts, with no
            count. A dashed box is a service the inventory knows about that no observed call
            mentions.
          </p>
        </div>
        <div className="adm-filters">
          {namespaces.length > 0 ? (
            <select
              className="adm-date-input"
              value={applied}
              onChange={e => { setApplied(e.target.value); setFocus(null); }}
              aria-label="Namespace"
            >
              {!namespaces.includes(applied) && applied && <option value={applied}>{applied}</option>}
              {namespaces.map(ns => <option key={ns} value={ns}>{ns}</option>)}
            </select>
          ) : (
            // Free text until discovery has pushed inventory — otherwise an
            // empty dropdown would make the panel unusable rather than just
            // unpopulated.
            <>
              <input
                className="adm-date-input"
                value={typed}
                onChange={e => setTyped(e.target.value)}
                onKeyDown={e => { if (e.key === "Enter" && typed) { setApplied(typed); setFocus(null); } }}
                placeholder="namespace"
                aria-label="Namespace"
              />
              <button
                className="adm-btn adm-btn--ghost"
                type="button"
                disabled={!typed}
                onClick={() => { setApplied(typed); setFocus(null); }}
              >
                Load
              </button>
            </>
          )}
          <button className="adm-btn adm-btn--ghost" type="button" onClick={() => refetch()}>
            {isFetching ? "Refreshing…" : "Refresh"}
          </button>
        </div>
      </div>

      {isLoading && <p className="adm-loading">Loading…</p>}
      {isError && (
        <p className="adm-error">{error instanceof Error ? error.message : "Failed to load dependencies."}</p>
      )}

      {!isLoading && !isError && graph.edges.length === 0 && arch.standalone.length === 0 && (
        <div className="adm-empty">
          <p>
            No dependency evidence for <strong>{applied}</strong>
            {inventory.length > 0 && <> — and no services in the inventory either</>}.
          </p>
          <p className="adm-muted">
            Expected when nothing in the namespace logs a callee's hostname. The miner needs
            the caller to have a Service (to attribute <code>from_service</code>) and to print
            the target host — either <code>svc.ns</code> or a bare <code>//svc</code> /
            <code>svc:port</code>. <code>docs/SERVICE_DEPENDENCIES.md</code> §4 lists the eight
            reasons a namespace comes back empty, in order of likelihood.
          </p>
        </div>
      )}

      {(graph.edges.length > 0 || arch.standalone.length > 0) && (
        <>
          <div className="adm-stats-row">
            <StatCard
              label="Services"
              value={String(Math.max(arch.known.size, graph.services.length))}
              sub={
                arch.standalone.length > 0
                  ? `${arch.standalone.length} with no observed calls`
                  : "all appear in the graph"
              }
            />
            <StatCard
              label="Observed calls"
              value={String(graph.edges.length)}
              sub={ingress.length > 0 ? `+ ${ingress.length} declared route${ingress.length === 1 ? "" : "s"}` : undefined}
            />
            <StatCard
              label="Entry points"
              value={String(graph.entries.length)}
              sub={summarise(graph.entries, "every service has a caller")}
            />
            <StatCard
              label="Terminal"
              value={String(graph.terminals.length)}
              sub={summarise(graph.terminals, "none observed")}
            />
            <StatCard
              label="Stale edges"
              value={String(graph.staleCount)}
              sub={graph.staleCount > 0 ? "no evidence in 15m" : "all fresh"}
            />
          </div>

          {rare.length > 0 && (
            <p className="topo-gap">
              <span className="adm-badge adm-badge--warn">incomplete</span>{" "}
              {rare.length === 1 ? "1 edge is" : `${rare.length} edges are`} caught in under a
              quarter of scans ({rare.map(e => `${e.from_service}→${e.to_service}`).join(", ")}).
              The miner samples only 5 pods per namespace and the last 200 log lines, so edges
              it rarely catches are a sign it is <strong>missing others entirely</strong> — treat
              this graph as more incomplete than the counts suggest.
            </p>
          )}

          <DependencyFlow
            edges={arch.edges}
            standalone={arch.standalone}
            meta={arch.meta}
            focus={selected}
            onFocus={setFocus}
          />

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
                    direction="out" onSelect={setFocus}
                  />
                </div>
                <div className="topo-focus__col">
                  <h4>Called by {selected}</h4>
                  <EdgeList
                    edges={graph.callers.get(selected) ?? []}
                    direction="in" onSelect={setFocus}
                  />
                </div>
              </div>
            )}
          </div>

          <div className="adm-table-wrap">
            <table className="adm-table">
              <thead>
                <tr>
                  <th>From</th><th></th><th>To</th><th>Seen in</th><th>Last seen</th><th>First seen</th>
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
                      <td><EvidenceBar edge={e} /></td>
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
