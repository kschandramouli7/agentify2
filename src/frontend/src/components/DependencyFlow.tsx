import { useMemo } from "react";
import { type ServiceDependency } from "../api";

// Left-to-right dataflow diagram of the mined service graph.
//
// HISTORY, BECAUSE IT EXPLAINS THE SHAPE
//
// v1 was a table with no diagram, on the argument that node-link views become
// hairballs. Reviewed against real data that was wrong — at five edges "what
// calls what" was already unreadable as rows, because it is a shape. The scale
// worry is answered by DEGRADING (focus draws a subgraph; past the caps it
// refuses to draw) rather than by declining to draw.
//
// v2 encoded evidence_count as line weight. That was also wrong, and worse,
// because it was confidently misleading — see the note on CONFIDENCE below.
//
// WHAT THE ENCODINGS MEAN NOW
//
// - Position: left-to-right is call direction. Layer = longest path from an
//   entry point, so a service sits right of its furthest upstream caller.
// - Node subtitle: in/out degree, or the role when one side is zero. This is
//   what makes entry points and terminal dependencies readable at a glance
//   without spending a colour on them.
// - Line weight: CONFIDENCE, in three absolute bands — not volume. Never a
//   continuous ramp again.
// - Colour: no categorical hues, because there are no series here. The accent
//   marks the focused subgraph; status tokens are left for status.
//
// CONFIDENCE, NOT VOLUME
//
// evidence_count is the number of scan cycles in which a miner saw the caller
// log the callee's hostname. It is therefore a function of how chatty the
// caller's logging is, how long the edge has existed, and sampling luck
// (MAX_PODS_PER_NAMESPACE=5, LOG_TAIL_LINES=200) — NOT of call volume.
//
// The payments namespace proves it: payment-batch's three edges all read
// exactly 187, because it logs all three targets in every burst, and 187 x the
// 60s scan interval is precisely its 3h age. A width ramp turned that into
// "these three dependencies are 14x more important than payment-worker's",
// which is not a claim the data supports.
//
// So the bands are ABSOLUTE, not relative to the graph's max: "seen 40 times"
// means the same thing whether or not some other edge was seen 4000 times.

const NODE_W = 158;
const NODE_H = 46;   // two lines: name + degree/role subtitle
const BEND_H = 20;   // a routing waypoint reserves less room than a real node
const COL_GAP = 84;
const ROW_GAP = 26;
const PAD = 18;

// Past this the picture stops informing and the focus view/table take over.
const MAX_NODES = 24;
const MAX_EDGES = 60;

// How consistently an edge is observed, which is what "confidence" should have
// meant all along.
//
// v3 banded the RAW COUNT on absolute thresholds. That is ambiguous, and the
// payments data proves it: payment-batch's edges read 318 over a 5.0h lifetime
// (299 scans → confirmed in ~every one) while payment-worker's read 17 over
// 5.8h (350 scans → confirmed in 1 of 20). Both landed in the same "20+,
// consistently observed" band. A count with no duration cannot distinguish
// "always seen" from "seen a lot, over a very long time".
//
// So the band is now COVERAGE — observed scans over the edge's own lifetime.
// That is scale-free and says the useful thing: is this dependency continuously
// re-confirmed, or does the miner mostly miss it?
//
// Approximate on purpose: three producers push at different cadences (60s live,
// hourly Glue, plus each diagnose), so coverage can exceed 1 and is clamped.
// It is presented as a band and a ratio, never as a precise percentage.
const SCAN_INTERVAL_MS = 60_000; // SCAN_INTERVAL_SECONDS in discovery.yaml

// Below this many scans of lifetime, coverage is noise — a brand-new edge seen
// once is 100% covered and means nothing.
const MIN_SCANS_FOR_COVERAGE = 3;

export type Confidence = {
  key: "continuous" | "intermittent" | "rare" | "new";
  width: number;
  label: string;
  scans: number | null;      // null when the lifetime is too short to judge
  coverage: number | null;
};

export function confidence(e: ServiceDependency): Confidence {
  const first = new Date(e.first_seen).getTime();
  const last = new Date(e.last_seen).getTime();
  const spanScans = Number.isFinite(first) && Number.isFinite(last) && last > first
    ? Math.round((last - first) / SCAN_INTERVAL_MS)
    : 0;

  if (spanScans < MIN_SCANS_FOR_COVERAGE) {
    return { key: "new", width: 1.75, label: "too new to judge", scans: null, coverage: null };
  }
  const coverage = Math.min(1, e.evidence_count / spanScans);
  if (coverage >= 0.75) {
    return { key: "continuous", width: 3.0, label: "confirmed in nearly every scan", scans: spanScans, coverage };
  }
  if (coverage >= 0.25) {
    return { key: "intermittent", width: 2.0, label: "confirmed intermittently", scans: spanScans, coverage };
  }
  return { key: "rare", width: 1.25, label: "rarely caught — the miner mostly misses this call", scans: spanScans, coverage };
}

/** Edges the miner catches in under a quarter of scans. Their existence means
 *  the graph is probably missing OTHER edges entirely, which is the actionable
 *  reading — so callers surface this rather than only styling the line. */
export function rarelyObserved(edges: ServiceDependency[]): ServiceDependency[] {
  return edges.filter(e => confidence(e).key === "rare");
}

const STALE_AFTER_MS = 15 * 60 * 1000; // matches TopologyPanel's threshold

function isStale(e: ServiceDependency): boolean {
  return Date.now() - new Date(e.last_seen).getTime() > STALE_AFTER_MS;
}

type Node = {
  id: string;
  layer: number;
  x: number;
  y: number;
  inDeg: number;
  outDeg: number;
};

type Pt = { x: number; y: number };

type Layout = {
  nodes: Map<string, Node>;
  bends: Map<string, Pt[]>;  // edge id → waypoints between its endpoints
  width: number;
  height: number;
};

// A column holds real nodes and routing waypoints in one ordering, which is the
// whole point: a waypoint that is not ordered alongside the nodes reserves no
// space, and the edge then runs underneath them.
type Slot =
  | { kind: "node"; id: string }
  | { kind: "bend"; edgeId: string };

const slotKey = (s: Slot) => (s.kind === "node" ? `n:${s.id}` : `b:${s.edgeId}`);
const slotHeight = (s: Slot) => (s.kind === "node" ? NODE_H : BEND_H);

/** In/out degree per service, over whatever edge list is given. */
export function degrees(edges: ServiceDependency[]): Map<string, { in: number; out: number }> {
  const d = new Map<string, { in: number; out: number }>();
  const at = (id: string) => d.get(id) ?? d.set(id, { in: 0, out: 0 }).get(id)!;
  for (const e of edges) {
    at(e.from_service).out += 1;
    at(e.to_service).in += 1;
  }
  return d;
}

/**
 * Layered ("Sugiyama-style") layout.
 *
 * 1. Longest-path layering — a node sits one column right of its furthest
 *    upstream caller, so column index reads as dependency depth. Cycles are
 *    broken by capping the walk; a service graph can legally contain one and
 *    the diagram must still render rather than recurse forever.
 * 2. An edge spanning more than one column gets a WAYPOINT in each column it
 *    crosses. Without this, a 0→2 edge is drawn straight through whatever
 *    occupies column 1 — which is exactly what the payments graph did, hiding
 *    two of payment-batch's three edges behind the payment-worker box.
 * 3. One barycenter pass orders each column (waypoints included) by the mean
 *    position of its predecessors, so edges cross less and the waypoints line
 *    up behind their own edge rather than zig-zagging.
 *
 * `deg` carries in/out degree from the FULL graph, not from `edges`.
 *
 * Focusing a service filters the edge list, and computing degree from the
 * filtered set made the subtitle change with the view — payment-batch read
 * "entry · calls 2" while focused and "entry · calls 3" unfocused. Degree is a
 * property of the service, so it must not depend on what is currently drawn.
 */
export function layout(edges: ServiceDependency[], deg: Map<string, { in: number; out: number }>): Layout {
  const parents = new Map<string, string[]>();
  const ids = new Set<string>();
  for (const e of edges) {
    ids.add(e.from_service);
    ids.add(e.to_service);
    (parents.get(e.to_service) ?? parents.set(e.to_service, []).get(e.to_service)!).push(e.from_service);
  }

  const depth = new Map<string, number>();
  const resolve = (id: string, seen: Set<string>): number => {
    const cached = depth.get(id);
    if (cached !== undefined) return cached;
    if (seen.has(id)) return 0;
    seen.add(id);
    const ps = parents.get(id) ?? [];
    const d = ps.length === 0 ? 0 : Math.max(...ps.map(p => resolve(p, seen))) + 1;
    seen.delete(id);
    depth.set(id, d);
    return d;
  };
  for (const id of ids) resolve(id, new Set());

  const maxLayer = Math.max(0, ...[...depth.values()]);
  const layers: Slot[][] = Array.from({ length: maxLayer + 1 }, () => []);
  for (const id of [...ids].sort()) layers[depth.get(id) ?? 0].push({ kind: "node", id });

  // Waypoints for column-skipping edges, plus the predecessor map the ordering
  // pass needs. A waypoint's predecessor is the previous waypoint of the same
  // edge, or the edge's source when this is the first one.
  const predOf = new Map<string, string[]>();
  const addPred = (k: string, p: string) =>
    (predOf.get(k) ?? predOf.set(k, []).get(k)!).push(p);
  for (const id of ids) for (const p of parents.get(id) ?? []) addPred(`n:${id}`, `n:${p}`);

  const bendLayers = new Map<string, number[]>();
  for (const e of edges) {
    const a = depth.get(e.from_service) ?? 0;
    const b = depth.get(e.to_service) ?? 0;
    if (b - a <= 1) continue; // adjacent columns, or a back edge — no routing needed
    const crossed: number[] = [];
    let prev = `n:${e.from_service}`;
    for (let li = a + 1; li < b; li++) {
      layers[li].push({ kind: "bend", edgeId: e.id });
      crossed.push(li);
      addPred(`b:${e.id}`, prev);
      prev = `b:${e.id}`;
    }
    bendLayers.set(e.id, crossed);
  }

  const posOf = new Map<string, number>(); // slot key → row centre, for barycentring
  layers.forEach((slots, li) => {
    if (li > 0) {
      slots.sort((s1, s2) => {
        const bary = (s: Slot) => {
          const ps = (predOf.get(slotKey(s)) ?? [])
            .map(k => posOf.get(k))
            .filter((v): v is number => v !== undefined);
          return ps.length ? ps.reduce((acc, v) => acc + v, 0) / ps.length : 0;
        };
        return bary(s1) - bary(s2) || slotKey(s1).localeCompare(slotKey(s2));
      });
    }
    // Provisional centres so the next column has something to barycentre on.
    let y = 0;
    for (const s of slots) {
      posOf.set(slotKey(s), y + slotHeight(s) / 2);
      y += slotHeight(s) + ROW_GAP;
    }
  });

  const colHeight = (slots: Slot[]) =>
    slots.reduce((h, s) => h + slotHeight(s), 0) + Math.max(0, slots.length - 1) * ROW_GAP;
  const fullHeight = Math.max(NODE_H, ...layers.map(colHeight));

  const nodes = new Map<string, Node>();
  const bends = new Map<string, Pt[]>();
  layers.forEach((slots, li) => {
    const x = PAD + li * (NODE_W + COL_GAP);
    // Centre each column vertically so short columns don't hug the top.
    let y = PAD + (fullHeight - colHeight(slots)) / 2;
    for (const s of slots) {
      if (s.kind === "node") {
        nodes.set(s.id, {
          id: s.id, layer: li, x, y,
          inDeg: deg.get(s.id)?.in ?? 0,
          outDeg: deg.get(s.id)?.out ?? 0,
        });
      } else {
        // Waypoint sits mid-column, so a routed edge reads as passing between
        // the boxes rather than through them.
        (bends.get(s.edgeId) ?? bends.set(s.edgeId, []).get(s.edgeId)!)
          .push({ x: x + NODE_W / 2, y: y + BEND_H / 2 });
      }
      y += slotHeight(s) + ROW_GAP;
    }
  });

  return {
    nodes,
    bends,
    width: PAD * 2 + layers.length * NODE_W + Math.max(0, layers.length - 1) * COL_GAP,
    height: PAD * 2 + fullHeight,
  };
}

/**
 * Smooth path through the endpoints and any waypoints, with horizontal tangents
 * at every joint so the segments meet without a visible kink.
 */
export function edgePath(from: Node, to: Node, via: Pt[]): { d: string; mid: Pt } {
  const start: Pt = { x: from.x + NODE_W, y: from.y + NODE_H / 2 };
  const end: Pt = { x: to.x, y: to.y + NODE_H / 2 };

  if (via.length === 0 && end.x <= start.x) {
    // Back edge (a cycle, or same column): bow underneath so it stays readable
    // instead of hiding behind the nodes.
    const dip = Math.max(Math.abs(end.y - start.y), NODE_H) + 26;
    return {
      d: `M ${start.x} ${start.y} C ${start.x + 40} ${start.y + dip}, ${end.x - 40} ${end.y + dip}, ${end.x} ${end.y}`,
      mid: { x: (start.x + end.x) / 2, y: (start.y + end.y) / 2 + dip * 0.75 },
    };
  }

  const pts = [start, ...via, end];
  let d = `M ${start.x} ${start.y}`;
  for (let i = 1; i < pts.length; i++) {
    const p = pts[i - 1];
    const q = pts[i];
    const c = (q.x - p.x) * 0.5;
    d += ` C ${p.x + c} ${p.y}, ${q.x - c} ${q.y}, ${q.x} ${q.y}`;
  }

  // Label anchor: a waypoint when there is one (it is on the drawn curve by
  // construction), else the midpoint of the single segment.
  const mid = via.length
    ? via[(via.length - 1) >> 1]
    : { x: (start.x + end.x) / 2, y: (start.y + end.y) / 2 };
  return { d, mid };
}

/**
 * Transitive reachability from `start`, following edges forwards
 * (`dir: "down"` — what it depends on) or backwards (`dir: "up"` — what
 * depends on it, i.e. the blast radius if it fails). Returns hop distance per
 * service, which is what lets the diagram distinguish "calls this directly"
 * from "affected two hops away".
 *
 * BFS, so a service reachable by both a short and a long path gets the short
 * distance — the honest reading of "how close is this failure".
 */
export function reach(
  edges: ServiceDependency[], start: string, dir: "up" | "down",
): Map<string, number> {
  const next = new Map<string, string[]>();
  for (const e of edges) {
    const [k, v] = dir === "down" ? [e.from_service, e.to_service] : [e.to_service, e.from_service];
    (next.get(k) ?? next.set(k, []).get(k)!).push(v);
  }
  const dist = new Map<string, number>([[start, 0]]);
  let frontier = [start];
  while (frontier.length) {
    const following: string[] = [];
    for (const id of frontier) {
      for (const n of next.get(id) ?? []) {
        if (dist.has(n)) continue; // already reached at an equal-or-shorter distance
        dist.set(n, (dist.get(id) ?? 0) + 1);
        following.push(n);
      }
    }
    frontier = following;
  }
  dist.delete(start);
  return dist;
}

function roleText(n: Node): string {
  if (n.inDeg === 0) return `entry · calls ${n.outDeg}`;
  if (n.outDeg === 0) return `terminal · ${n.inDeg} caller${n.inDeg === 1 ? "" : "s"}`;
  return `${n.inDeg} in · ${n.outDeg} out`;
}

export function DependencyFlow({
  edges, focus, onFocus,
}: {
  edges: ServiceDependency[];
  focus: string | null;
  onFocus: (s: string | null) => void;
}) {
  // Focus shows the TRANSITIVE neighbourhood, not one hop. One hop answers
  // "who calls this"; the operator's actual question during an incident is
  // "who is affected if this breaks", which is the upstream closure.
  const { visible, upstream, downstream } = useMemo(() => {
    if (!focus) return { visible: edges, upstream: new Map(), downstream: new Map() };
    const up = reach(edges, focus, "up");
    const down = reach(edges, focus, "down");
    const keep = new Set<string>([focus, ...up.keys(), ...down.keys()]);
    return {
      // An edge is drawn when both ends are in the closure, so the paths that
      // carry the impact are visible, not just the endpoints.
      visible: edges.filter(e => keep.has(e.from_service) && keep.has(e.to_service)),
      upstream: up,
      downstream: down,
    };
  }, [edges, focus]);

  const nodeCount = useMemo(
    () => new Set(visible.flatMap(e => [e.from_service, e.to_service])).size,
    [visible],
  );

  // From the full edge list on purpose — see layout()'s note on `deg`.
  const deg = useMemo(() => degrees(edges), [edges]);

  const tooBig = nodeCount > MAX_NODES || visible.length > MAX_EDGES;
  const l = useMemo(() => layout(tooBig ? [] : visible, deg), [visible, tooBig, deg]);

  if (tooBig) {
    return (
      <div className="adm-empty flow-toobig">
        <p>{nodeCount} services and {visible.length} edges — too dense to draw usefully.</p>
        <p className="adm-muted">
          Pick a service below to see only what it reaches, or read the table. A diagram of
          this many edges is a hairball, not an answer.
        </p>
      </div>
    );
  }

  if (visible.length === 0) return null;

  const hopOf = (id: string): number | null =>
    id === focus ? 0 : upstream.get(id) ?? downstream.get(id) ?? null;

  return (
    <div className="flow">
      {focus && (
        <p className="flow__radius">
          If <strong>{focus}</strong> fails,{" "}
          {upstream.size === 0 ? (
            <>nothing here is affected — no service was observed calling it.</>
          ) : (
            <>
              <strong>{upstream.size}</strong> service{upstream.size === 1 ? "" : "s"} upstream
              {" "}{upstream.size === 1 ? "is" : "are"} affected:{" "}
              {[...upstream.entries()]
                .sort((a, b) => a[1] - b[1] || a[0].localeCompare(b[0]))
                .map(([s, d]) => `${s} (${d === 1 ? "direct" : `${d} hops`})`)
                .join(", ")}
              .
            </>
          )}{" "}
          It depends on <strong>{downstream.size}</strong>.
        </p>
      )}

      <div className="flow__scroll">
        <svg
          className="flow__svg"
          viewBox={`0 0 ${l.width} ${l.height}`}
          width={l.width}
          height={l.height}
          role="img"
          aria-label={
            `Service dependency flow: ${visible.length} observed calls between ${nodeCount} services, ` +
            `drawn left to right in call direction. The table below carries the same data as text.`
          }
        >
          <defs>
            <marker id="flow-arrow" viewBox="0 0 8 8" refX="7" refY="4"
                    markerWidth="7" markerHeight="7" orient="auto-start-reverse">
              <path d="M 0 0 L 8 4 L 0 8 z" className="flow__arrowhead" />
            </marker>
            <marker id="flow-arrow-on" viewBox="0 0 8 8" refX="7" refY="4"
                    markerWidth="7" markerHeight="7" orient="auto-start-reverse">
              <path d="M 0 0 L 8 4 L 0 8 z" className="flow__arrowhead flow__arrowhead--on" />
            </marker>
          </defs>

          {/* Edges first so nodes paint over their endpoints. */}
          {visible.map(e => {
            const a = l.nodes.get(e.from_service);
            const b = l.nodes.get(e.to_service);
            if (!a || !b) return null;
            // Emphasise the edges that actually touch the focused service; the
            // rest are the paths carrying impact onward and stay recessive.
            const on = !focus || e.from_service === focus || e.to_service === focus;
            const c = confidence(e);
            const stale = isStale(e);
            const { d, mid } = edgePath(a, b, l.bends.get(e.id) ?? []);
            return (
              <g key={e.id} className={`flow__edge${on ? " flow__edge--on" : ""}`}>
                <title>
                  {`${e.from_service} → ${e.to_service}\n` +
                   (c.scans === null
                     ? `Seen ${e.evidence_count}x; too new to judge how consistently.\n`
                     : `Seen in ${e.evidence_count} of ~${c.scans} scans since it was first ` +
                       `observed (${Math.round((c.coverage ?? 0) * 100)}%) — ${c.label}.\n`) +
                   `This counts sightings in logs, not requests: confidence, not traffic volume.\n` +
                   (stale ? "STALE: no new evidence in over 15 minutes.\n" : "") +
                   `Last seen ${new Date(e.last_seen).toLocaleString()}`}
                </title>
                <path d={d}
                      className={`flow__line${stale ? " flow__line--stale" : ""}`}
                      strokeWidth={c.width}
                      markerEnd={`url(#${on ? "flow-arrow-on" : "flow-arrow"})`} />
                {/* Printed, never implied by thickness alone — and set
                    horizontally rather than on a textPath, which rotated the
                    digits along the curve and made them unreadable. */}
                <text x={mid.x} y={mid.y - 6} textAnchor="middle" className="flow__count">
                  {e.evidence_count}
                </text>
              </g>
            );
          })}

          {[...l.nodes.values()].map(n => {
            const hop = hopOf(n.id);
            const cls = [
              "flow__node",
              n.id === focus ? "flow__node--focus" : "",
              // Direct neighbours of the focus read stronger than distant ones,
              // so hop distance is visible without a second colour channel.
              focus && hop !== null && hop > 1 ? "flow__node--distant" : "",
              focus && hop === null ? "flow__node--off" : "",
            ].filter(Boolean).join(" ");
            return (
              <g key={n.id} className={cls}
                 onClick={() => onFocus(n.id === focus ? null : n.id)}
                 role="button" tabIndex={0}
                 onKeyDown={ev => {
                   if (ev.key === "Enter" || ev.key === " ") {
                     ev.preventDefault();
                     onFocus(n.id === focus ? null : n.id);
                   }
                 }}>
                <title>
                  {`${n.id} — ${roleText(n)}` +
                   (focus && hop !== null && hop > 0
                     ? `\n${hop === 1 ? "Directly" : `${hop} hops`} ${upstream.has(n.id) ? "upstream of" : "downstream of"} ${focus}`
                     : "") +
                   `\nClick to ${n.id === focus ? "clear focus" : "trace what it reaches"}`}
                </title>
                <rect x={n.x} y={n.y} width={NODE_W} height={NODE_H} rx="8" className="flow__box" />
                <text x={n.x + NODE_W / 2} y={n.y + 19} textAnchor="middle" className="flow__label">
                  {n.id.length > 21 ? `${n.id.slice(0, 20)}…` : n.id}
                </text>
                <text x={n.x + NODE_W / 2} y={n.y + 34} textAnchor="middle" className="flow__sub">
                  {roleText(n)}
                </text>
              </g>
            );
          })}
        </svg>
      </div>

      <div className="flow__legend adm-muted">
        <span><span className="flow__key flow__key--dir" aria-hidden="true" /> left to right = call direction</span>
        <span className="flow__legend-group" aria-hidden="true">
          {[
            { w: 3.0, t: "every scan" },
            { w: 2.0, t: "intermittent" },
            { w: 1.25, t: "rarely caught" },
          ].map(b => (
            <span key={b.t} className="flow__key-band">
              <svg width="26" height="8" aria-hidden="true">
                <line x1="0" y1="4" x2="26" y2="4" className="flow__line" strokeWidth={b.w} />
              </svg>
              {b.t}
            </span>
          ))}
          <span className="flow__key-band">
            <svg width="26" height="8" aria-hidden="true">
              <line x1="0" y1="4" x2="26" y2="4" className="flow__line flow__line--stale" strokeWidth={2} />
            </svg>
            going stale
          </span>
        </span>
        <span>
          Line weight is how <strong>consistently</strong> the call is observed across the
          edge's own lifetime — not how much traffic it carries. The number is the raw
          sighting count. Click a service to trace what it reaches and what breaks with it.
          {focus && <> · <button type="button" className="topo-link" onClick={() => onFocus(null)}>show everything</button></>}
        </span>
      </div>
    </div>
  );
}
