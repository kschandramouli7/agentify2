import { useMemo } from "react";
import { type ServiceDependency } from "../api";

/**
 * An edge on the diagram. `kind` separates two categorically different claims
 * that must never be conflated:
 *
 *   observed — mined from a log line. Evidence, therefore a LOWER BOUND, with
 *              a confidence band and a sighting count.
 *   declared — read from a Kubernetes object (an Ingress / HTTPRoute / Route).
 *              A fact. No confidence, no count, and its absence means the
 *              object does not exist rather than "we saw nothing".
 *
 * Drawing them identically would repeat the mistake this panel already made
 * once, when a sighting count was rendered as though it were traffic volume.
 */
export type FlowEdge = ServiceDependency & { kind?: "observed" | "declared" };

/** Extra facts about a node, from inventory rather than from the edges. */
export type NodeMeta = {
  /** "service" (a real Kubernetes Service) or "ingress" (an entry point). */
  kind?: "service" | "ingress";
  /** Fraction of scans in which this service's pods were readable, if known. */
  coverage?: number | null;
  /** Pods attributed to it / pods actually sampled, if known. */
  podsSeen?: number;
  podsSampled?: number;
  /** True when inventory lists it but no edge references it. */
  unobserved?: boolean;
  /** For an ingress node: the host it serves. */
  host?: string;
  /** What to draw in the box. The id must stay unique and collision-proof
   *  (an ingress id is prefixed), which makes it the wrong thing to display. */
  label?: string;
  /** Declarative facts from the Kubernetes objects — workload kind, scale,
   *  exposure. Distinct in kind from everything else here: a profile is a
   *  FACT, where coverage and edges are evidence. */
  workloadKind?: string;
  replicasReady?: number | null;
  replicasDesired?: number | null;
  serviceType?: string;
  ports?: { name?: string; port: number; protocol?: string }[];
  image?: string;
  schedule?: string;
  /** LIVE state, from the pod watch — as opposed to the declared spec above.
   *  `pods`/`podsReady` are what is actually running; `replicasDesired` is
   *  what was asked for. When they disagree, the disagreement is the finding. */
  podsRunning?: number;
  podsReadyNow?: number;
  restarts?: number;
  phases?: string[];
};

/** The middle line of a node: what this service IS.
 *
 *  Ordered by how much it classifies the box — kind first (a CronJob is a
 *  categorically different thing from a Deployment), then scale, then how it
 *  is exposed. Omitted entirely when the collector reported no profile, rather
 *  than padded with "unknown": an empty line reads as "not collected", a line
 *  of unknowns reads as a finding. */
function profileText(m?: NodeMeta): string | null {
  if (!m) return null;
  const bits: string[] = [];
  // Kind only — a raw cron expression ("*/30 * * * *") is 12 characters of
  // noise that pushed the port off the line. "CronJob" already says "batch";
  // the schedule itself lives in the tooltip.
  if (m.workloadKind) bits.push(m.workloadKind);
  // Live readiness beats the declared spec when both exist: the spec says what
  // was asked for, the watch says what is true. 0/2 is a real and urgent
  // finding, so a zero must print — only null is unknown.
  const ready = m.podsReadyNow ?? m.replicasReady;
  const total = m.replicasDesired ?? m.podsRunning;
  if (ready != null && total != null) bits.push(`${ready}/${total}`);
  if (m.ports?.length) {
    const p = m.ports[0];
    bits.push(m.ports.length > 1 ? `${p.port} +${m.ports.length - 1}` : String(p.port));
  } else if (m.serviceType === "Headless") {
    bits.push("headless");
  }
  return bits.length ? bits.join(" · ") : null;
}

/** The one line that says something is WRONG, or nothing at all.
 *
 *  Deliberately absent when healthy: a diagram where every box carries a
 *  status line trains the eye to skip it. Restarts are only surfaced past a
 *  threshold, because a handful over a pod's lifetime is normal and flagging
 *  it would cry wolf. */
function troubleText(m?: NodeMeta): string | null {
  if (!m) return null;
  const bad = (m.phases ?? []).filter(p => p && p !== "Running" && p !== "Succeeded");
  if (bad.length) return fit(bad.join(", "), 24);
  if (m.podsReadyNow != null && m.podsRunning != null && m.podsReadyNow < m.podsRunning) {
    return `${m.podsRunning - m.podsReadyNow} not ready`;
  }
  // PER POD, not total. A 3-pod StatefulSet with 6 lifetime restarts is two
  // each — normal churn. Flagging that in red is how a status colour gets
  // trained out of a reader, so the threshold scales with the pod count.
  const perPod = (m.restarts ?? 0) / Math.max(1, m.podsRunning ?? 1);
  if (perPod >= 5) return `${m.restarts} restarts`;
  return null;
}

/** Fit a string to the node box. Hostnames are the reason this exists —
 *  "agentify-dev.elb.amazonaws.com" is twice the width of the box and spilled
 *  outside it. */
function fit(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

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
const NODE_H = 60;   // three lines: name, what it is, and its role in the graph
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

export function confidence(e: FlowEdge): Confidence {
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
export function rarelyObserved(edges: FlowEdge[]): FlowEdge[] {
  // Declared edges have no evidence to be thin — they are facts, not sightings.
  return edges.filter(e => e.kind !== "declared" && confidence(e).key === "rare");
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
export function degrees(edges: FlowEdge[]): Map<string, { in: number; out: number }> {
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
export function layout(
  edges: FlowEdge[],
  deg: Map<string, { in: number; out: number }>,
  standalone: string[] = [],
): Layout {
  const parents = new Map<string, string[]>();
  const ids = new Set<string>();
  for (const e of edges) {
    ids.add(e.from_service);
    ids.add(e.to_service);
    (parents.get(e.to_service) ?? parents.set(e.to_service, []).get(e.to_service)!).push(e.from_service);
  }
  // Services the inventory knows about but no edge references. Drawing them is
  // the single biggest legibility win: the agentify namespace runs four
  // services and the mined graph referenced two, so half the architecture was
  // simply absent from the picture with nothing to indicate it.
  for (const id of standalone) ids.add(id);

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
  // A back edge (a cycle, or a same-column pair) routes below every box on a
  // return track. Without reserving room the path is drawn outside the viewBox
  // and silently clipped — which looked like a missing edge, the worst possible
  // failure for a diagram whose claim is that these are observed facts.
  const hasBackEdge = edges.some(
    e => (depth.get(e.to_service) ?? 0) <= (depth.get(e.from_service) ?? 0),
  );

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
    height: PAD * 2 + fullHeight + (hasBackEdge ? BACK_EDGE_ROOM : 0),
  };
}

const CORNER = 5;    // corner rounding; small enough to still read as square
const PORT_GAP = 9;  // vertical spacing between an edge's attachment points
const TRACK_GAP = 14; // horizontal spacing between edges' vertical runs
const BACK_EDGE_ROOM = 46; // room below the boxes for a back edge's return track

/**
 * Where each edge attaches to its endpoints ("ports").
 *
 * With curved edges, several lines leaving one node could share a single
 * attachment point and still be told apart, because they diverged immediately.
 * Orthogonal lines do not diverge — they run along the same horizontal for a
 * stretch and read as one thick line. So exits are spread down the source's
 * right edge and entries up the target's left edge.
 *
 * Ordering matters as much as spacing: exits are sorted by where their target
 * sits vertically, and entries by where their source sits, which is what stops
 * the fan from crossing itself on the way out.
 */
export function assignPorts(
  edges: FlowEdge[], nodes: Map<string, Node>,
): Map<string, { from: Pt; to: Pt; track: number }> {
  const out = new Map<string, ServiceDependency[]>();
  const inn = new Map<string, ServiceDependency[]>();
  for (const e of edges) {
    if (!nodes.has(e.from_service) || !nodes.has(e.to_service)) continue;
    (out.get(e.from_service) ?? out.set(e.from_service, []).get(e.from_service)!).push(e);
    (inn.get(e.to_service) ?? inn.set(e.to_service, []).get(e.to_service)!).push(e);
  }

  const centreY = (id: string) => {
    const n = nodes.get(id)!;
    return n.y + NODE_H / 2;
  };
  // Spread n ports around the centre of a node side, clamped inside the box so
  // a high-degree node never sprouts lines from outside its own outline.
  const spread = (n: Node, count: number, i: number) => {
    const usable = NODE_H - 12;
    const step = Math.min(PORT_GAP, count > 1 ? usable / (count - 1) : 0);
    return n.y + NODE_H / 2 + (i - (count - 1) / 2) * step;
  };

  const ports = new Map<string, { from: Pt; to: Pt; track: number }>();
  for (const [id, list] of out) {
    const n = nodes.get(id)!;
    list.sort((a, b) => centreY(a.to_service) - centreY(b.to_service) || a.id.localeCompare(b.id));
    list.forEach((e, i) => {
      const prev = ports.get(e.id);
      ports.set(e.id, {
        from: { x: n.x + NODE_W, y: spread(n, list.length, i) },
        to: prev?.to ?? { x: 0, y: 0 },
        // Each edge out of a node gets its OWN vertical track. Without this,
        // two edges whose elbows land on the same x share that segment and the
        // pair renders as a closed rectangle — one shape, not two edges.
        track: (i - (list.length - 1) / 2) * TRACK_GAP,
      });
    });
  }
  for (const [id, list] of inn) {
    const n = nodes.get(id)!;
    list.sort((a, b) => centreY(a.from_service) - centreY(b.from_service) || a.id.localeCompare(b.id));
    list.forEach((e, i) => {
      const prev = ports.get(e.id);
      ports.set(e.id, {
        from: prev?.from ?? { x: 0, y: 0 },
        to: { x: n.x, y: spread(n, list.length, i) },
        track: prev?.track ?? 0,
      });
    });
  }
  return ports;
}

/** One rounded right-angle turn at `corner`, arriving from `prev` and leaving
 *  toward `next`. Returns the SVG segment; the arc is skipped when a leg is too
 *  short for it, which would otherwise overshoot and kink. */
function turn(prev: Pt, corner: Pt, next: Pt): string {
  const inLen = Math.hypot(corner.x - prev.x, corner.y - prev.y);
  const outLen = Math.hypot(next.x - corner.x, next.y - corner.y);
  const r = Math.min(CORNER, inLen / 2, outLen / 2);
  if (r < 1) return ` L ${corner.x} ${corner.y}`;
  const ux = Math.sign(corner.x - prev.x), uy = Math.sign(corner.y - prev.y);
  const vx = Math.sign(next.x - corner.x), vy = Math.sign(next.y - corner.y);
  return (
    ` L ${corner.x - ux * r} ${corner.y - uy * r}` +
    ` Q ${corner.x} ${corner.y} ${corner.x + vx * r} ${corner.y + vy * r}`
  );
}

/** Orthogonal path through a list of points, rounding each corner. */
function orthPath(pts: Pt[]): string {
  let d = `M ${pts[0].x} ${pts[0].y}`;
  for (let i = 1; i < pts.length - 1; i++) d += turn(pts[i - 1], pts[i], pts[i + 1]);
  const last = pts[pts.length - 1];
  return d + ` L ${last.x} ${last.y}`;
}

/**
 * Orthogonal ("squared") route from one node to another.
 *
 * Replaces a bezier. The curves read as organic and made every edge look
 * approximate, which is the wrong impression for a diagram whose whole claim is
 * that these are observed facts. Right angles also make it obvious when two
 * edges share a track, where a curve just looked like a slightly different
 * curve.
 *
 * Shape: a horizontal stub out of the source, one vertical run at the midpoint
 * of the gap, then a horizontal stub into the target — the classic Z route,
 * degenerating to a straight line when both ports share a y.
 */
export function edgePath(
  from: Node, to: Node, via: Pt[], port?: { from: Pt; to: Pt; track?: number },
): { d: string; mid: Pt } {
  const start: Pt = port?.from ?? { x: from.x + NODE_W, y: from.y + NODE_H / 2 };
  const end: Pt = port?.to ?? { x: to.x, y: to.y + NODE_H / 2 };

  if (via.length === 0 && end.x <= start.x) {
    // Back edge (a cycle, or same column): drop below both boxes and run back
    // along a horizontal track, rather than hiding behind the nodes. The dip
    // lands inside the BACK_EDGE_ROOM layout() reserves for exactly this.
    const dip = Math.max(from.y + NODE_H, to.y + NODE_H) + BACK_EDGE_ROOM / 2;
    const pts = [start, { x: start.x + 18, y: start.y }, { x: start.x + 18, y: dip },
                 { x: end.x - 18, y: dip }, { x: end.x - 18, y: end.y }, end];
    return { d: orthPath(pts), mid: { x: (start.x + end.x) / 2, y: dip } };
  }

  // Straight through when the ports line up — no elbow to draw.
  if (via.length === 0 && Math.abs(start.y - end.y) < 0.5) {
    return {
      d: `M ${start.x} ${start.y} L ${end.x} ${end.y}`,
      mid: { x: (start.x + end.x) / 2, y: start.y },
    };
  }

  const track = port?.track ?? 0;

  if (via.length > 0) {
    // A column-skipping edge takes ONE bypass channel rather than an elbow per
    // leg. The waypoint already reserves a row between the boxes it passes, so
    // running the whole horizontal at that row is both correct and legible:
    // the edge visibly goes around the intervening node.
    //
    // Elbowing per leg instead produced a staircase whose segments closed into
    // a rectangle, which read as a container rather than as two edges.
    const channelY = via[0].y;
    const sx = start.x + 18 + track;
    const ex = end.x - 18 - track;
    const pts: Pt[] = [start];
    if (Math.abs(start.y - channelY) >= 0.5) pts.push({ x: sx, y: start.y }, { x: sx, y: channelY });
    if (Math.abs(end.y - channelY) >= 0.5) pts.push({ x: ex, y: channelY }, { x: ex, y: end.y });
    pts.push(end);
    return { d: orthPath(pts), mid: { x: (sx + ex) / 2, y: channelY } };
  }

  // Adjacent columns: the classic Z, with the vertical run on this edge's own
  // track and clamped inside the gap so it never doubles back.
  const lo = Math.min(start.x, end.x) + 12;
  const hi = Math.max(start.x, end.x) - 12;
  const xm = Math.max(lo, Math.min(hi, (start.x + end.x) / 2 + track));
  const pts: Pt[] = [start, { x: xm, y: start.y }, { x: xm, y: end.y }, end];

  // Label on the first vertical run when there is one — a vertical segment has
  // clear space either side of it, where a horizontal run is where the lines
  // bunch together.
  const vertical = pts.find((q, i) => i > 0 && Math.abs(q.x - pts[i - 1].x) < 0.5 && Math.abs(q.y - pts[i - 1].y) > 8);
  const vi = vertical ? pts.indexOf(vertical) : -1;
  const mid = vi > 0
    ? { x: pts[vi].x, y: (pts[vi].y + pts[vi - 1].y) / 2 }
    : { x: (start.x + end.x) / 2, y: (start.y + end.y) / 2 };
  return { d: orthPath(pts), mid };
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
  edges: FlowEdge[], start: string, dir: "up" | "down",
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

function roleText(n: Node, m?: NodeMeta): string {
  // Just "entry point": the host is already the box label, and repeating it
  // here both duplicated it and overflowed the box on a real ALB hostname.
  if (m?.kind === "ingress") return "entry point";
  // in=0 AND out=0 is NOT an entry point — it is a service we have no evidence
  // about in either direction. Calling it "entry · calls 0" implied a finding
  // where there is only an absence.
  if (n.inDeg === 0 && n.outDeg === 0) return "no observed calls";
  if (n.inDeg === 0) return `entry · calls ${n.outDeg}`;
  if (n.outDeg === 0) return fit(`terminal · ${n.inDeg} caller${n.inDeg === 1 ? "" : "s"}`, 30);
  return `${n.inDeg} in · ${n.outDeg} out`;
}

export function DependencyFlow({
  edges, focus, onFocus, standalone = [], meta,
}: {
  edges: FlowEdge[];
  focus: string | null;
  onFocus: (s: string | null) => void;
  /** Inventory services with no edge — drawn so the picture is the whole
   *  namespace rather than only its observed half. */
  standalone?: string[];
  /** Per-node facts from inventory: node kind, coverage, ingress host. */
  meta?: Map<string, NodeMeta>;
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

  // Standalone nodes are dropped while a service is focused: focus answers
  // "what does this reach", and a node with no edges reaches nothing.
  const visibleStandalone = focus ? [] : standalone;

  const nodeCount = useMemo(
    () => new Set([...visible.flatMap(e => [e.from_service, e.to_service]), ...visibleStandalone]).size,
    [visible, visibleStandalone],
  );

  // From the full edge list on purpose — see layout()'s note on `deg`.
  const deg = useMemo(() => degrees(edges), [edges]);

  const tooBig = nodeCount > MAX_NODES || visible.length > MAX_EDGES;
  const l = useMemo(
    () => layout(tooBig ? [] : visible, deg, tooBig ? [] : visibleStandalone),
    [visible, tooBig, deg, visibleStandalone],
  );
  // Attachment points, computed once per layout: orthogonal lines that share a
  // port read as a single thick line rather than as several edges.
  const ports = useMemo(() => assignPorts(tooBig ? [] : visible, l.nodes), [visible, tooBig, l]);

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

  if (visible.length === 0 && visibleStandalone.length === 0) return null;

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
            const declared = e.kind === "declared";
            const c = confidence(e);
            const stale = !declared && isStale(e);
            const { d, mid } = edgePath(a, b, l.bends.get(e.id) ?? [], ports.get(e.id));
            return (
              <g key={e.id} className={`flow__edge${on ? " flow__edge--on" : ""}`}>
                <title>
                  {declared
                    ? `${e.from_service} → ${e.to_service}\n` +
                      "DECLARED route, read from the Kubernetes object — a fact, not evidence. " +
                      "No sighting count applies."
                    : `${e.from_service} → ${e.to_service}\n` +
                      (c.scans === null
                        ? `Seen ${e.evidence_count}x; too new to judge how consistently.\n`
                        : `Seen in ${e.evidence_count} of ~${c.scans} scans since it was first ` +
                          `observed (${Math.round((c.coverage ?? 0) * 100)}%) — ${c.label}.\n`) +
                      `This counts sightings in logs, not requests: confidence, not traffic volume.\n` +
                      (stale ? "STALE: no new evidence in over 15 minutes.\n" : "") +
                      `Last seen ${new Date(e.last_seen).toLocaleString()}`}
                </title>
                <path d={d}
                      className={
                        "flow__line" +
                        (declared ? " flow__line--declared" : "") +
                        (stale ? " flow__line--stale" : "")
                      }
                      strokeWidth={declared ? 2 : c.width}
                      markerEnd={`url(#${on ? "flow-arrow-on" : "flow-arrow"})`} />
                {/* Printed, never implied by thickness alone — and set
                    horizontally rather than on a textPath, which rotated the
                    digits along the curve and made them unreadable. */}
                {!declared && (
                  <text x={mid.x} y={mid.y - 6} textAnchor="middle" className="flow__count">
                    {e.evidence_count}
                  </text>
                )}
              </g>
            );
          })}

          {[...l.nodes.values()].map(n => {
            const hop = hopOf(n.id);
            const m = meta?.get(n.id);
            const profile = profileText(m);
            const trouble = troubleText(m);
            const unobserved = n.inDeg === 0 && n.outDeg === 0 && m?.kind !== "ingress";
            const cls = [
              "flow__node",
              m?.kind === "ingress" ? "flow__node--ingress" : "",
              // Dashed outline, not dimmed: an unobserved service is present in
              // the cluster and absent from the evidence. Dimming would read as
              // "less important" rather than "not seen".
              unobserved ? "flow__node--unobserved" : "",
              // Status is colour + a word, never colour alone — the same rule
              // the rest of the panel follows.
              trouble ? "flow__node--trouble" : "",
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
                  {`${n.id} — ${roleText(n, m)}` +
                   (profile ? `\n${profile}` : "") +
                   (trouble ? `\n⚠ ${trouble}` : "") +
                   (m?.restarts != null && m.restarts > 0 ? `\nrestarts: ${m.restarts}` : "") +
                   (m?.phases?.length ? `\npod phases: ${m.phases.join(", ")}` : "") +
                   (m?.schedule ? `\nschedule: ${m.schedule}` : "") +
                   (m?.image ? `\nimage: ${m.image}` : "") +
                   (m?.ports?.length
                     ? `\nports: ${m.ports.map(p => `${p.name ? p.name + " " : ""}${p.port}/${p.protocol ?? "TCP"}`).join(", ")}`
                     : "") +
                   (m?.serviceType ? `\nexposure: ${m.serviceType}` : "") +
                   (m?.coverage != null
                     ? `\nObserved in ${Math.round(m.coverage * 100)}% of scans` +
                       (m.podsSeen != null ? ` (${m.podsSampled ?? 0} of ${m.podsSeen} pods sampled)` : "")
                     : "") +
                   (unobserved
                     ? "\nIn the inventory but referenced by no observed call — either it makes " +
                       "and receives none, or nothing it does is visible to the miner."
                     : "") +
                   (focus && hop !== null && hop > 0
                     ? `\n${hop === 1 ? "Directly" : `${hop} hops`} ${upstream.has(n.id) ? "upstream of" : "downstream of"} ${focus}`
                     : "") +
                   `\nClick to ${n.id === focus ? "clear focus" : "trace what it reaches"}`}
                </title>
                <rect x={n.x} y={n.y} width={NODE_W} height={NODE_H} rx="8" className="flow__box" />
                <text x={n.x + NODE_W / 2} y={n.y + 19} textAnchor="middle" className="flow__label">
                  {fit(m?.label ?? n.id, 21)}
                </text>
                {profile && (
                  <text x={n.x + NODE_W / 2} y={n.y + 34} textAnchor="middle" className="flow__what">
                    {fit(profile, 26)}
                  </text>
                )}
                {/* When something is wrong, that displaces the graph role on the
                    third line: "2 not ready" matters more than "terminal · 3
                    callers", and the role is still in the tooltip. */}
                <text
                  x={n.x + NODE_W / 2}
                  y={profile ? n.y + 49 : n.y + 38}
                  textAnchor="middle"
                  className={trouble ? "flow__trouble" : "flow__sub"}
                >
                  {trouble ?? roleText(n, m)}
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
          <span className="flow__key-band">
            <svg width="26" height="8" aria-hidden="true">
              <line x1="0" y1="4" x2="26" y2="4" className="flow__line flow__line--declared" strokeWidth={2} />
            </svg>
            declared route
          </span>
        </span>
        <span>
          <strong>Two different claims are drawn here.</strong> A solid arrow is{" "}
          <em>observed</em> — mined from a log line, so its weight is how consistently it was
          seen and the number is a sighting count, never traffic. A{" "}
          <span className="flow__inline-declared">declared route</span> comes from a
          Kubernetes Ingress object: a fact, with no count to give. A box with a dashed
          outline is in the inventory but referenced by no observed call.
          {focus && <> · <button type="button" className="topo-link" onClick={() => onFocus(null)}>show everything</button></>}
        </span>
      </div>
    </div>
  );
}
