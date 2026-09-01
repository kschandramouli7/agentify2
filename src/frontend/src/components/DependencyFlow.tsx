import { useMemo } from "react";
import { type ServiceDependency } from "../api";

// Left-to-right dataflow diagram of the mined service graph.
//
// This replaces an earlier judgement of mine. I argued a table beat a node-link
// view because a diagram becomes a hairball at scale — true, but it answered the
// wrong question: at five edges the table was unreadable, and "what calls what"
// is a shape, not a list. The scale worry is handled by degrading (see
// MAX_NODES/MAX_EDGES below) rather than by refusing to draw.
//
// Hand-rolled SVG rather than a graph library: layered layout for a graph this
// size is ~40 lines, and a layout dependency would be the largest thing in the
// bundle for one panel.
//
// Colour, per the same rules as the rest of the panel: no categorical hues —
// there are no series here. Edge weight encodes evidence (one channel, plus the
// printed number), the accent hue marks the focused subgraph, and everything
// else is recessive ink.

const NODE_W = 150;
const NODE_H = 38;
const COL_GAP = 76;   // horizontal space between layers
const ROW_GAP = 22;   // vertical space between nodes in a layer
const PAD = 16;

// Past this the picture stops informing and the focus view/table take over.
const MAX_NODES = 24;
const MAX_EDGES = 60;

type Node = { id: string; layer: number; row: number; x: number; y: number };

type Layout = {
  nodes: Map<string, Node>;
  width: number;
  height: number;
  layers: string[][];
};

/**
 * Longest-path layering: a node sits one column right of its furthest upstream
 * caller. Cycles are broken by capping the walk — a service graph can legally
 * contain one (A calls B, B calls A) and the diagram must still render rather
 * than recurse forever.
 */
function layout(edges: ServiceDependency[]): Layout {
  const nodesIn = new Map<string, string[]>();
  const ids = new Set<string>();
  for (const e of edges) {
    ids.add(e.from_service);
    ids.add(e.to_service);
    (nodesIn.get(e.to_service) ?? nodesIn.set(e.to_service, []).get(e.to_service)!).push(e.from_service);
  }

  const depth = new Map<string, number>();
  const resolve = (id: string, seen: Set<string>): number => {
    const cached = depth.get(id);
    if (cached !== undefined) return cached;
    if (seen.has(id)) return 0; // cycle — treat as a source rather than looping
    seen.add(id);
    const parents = nodesIn.get(id) ?? [];
    const d = parents.length === 0 ? 0 : Math.max(...parents.map(p => resolve(p, seen))) + 1;
    seen.delete(id);
    depth.set(id, d);
    return d;
  };
  for (const id of ids) resolve(id, new Set());

  // Group into columns, then order each column by the average row of its
  // upstream callers (one barycenter pass) so edges cross less often.
  const maxLayer = Math.max(0, ...[...depth.values()]);
  const layers: string[][] = Array.from({ length: maxLayer + 1 }, () => []);
  for (const id of [...ids].sort()) layers[depth.get(id) ?? 0].push(id);

  const rowOf = new Map<string, number>();
  layers.forEach((layerIds, li) => {
    if (li > 0) {
      layerIds.sort((a, b) => {
        const bary = (n: string) => {
          const parents = (nodesIn.get(n) ?? []).map(p => rowOf.get(p)).filter((v): v is number => v !== undefined);
          return parents.length ? parents.reduce((s, v) => s + v, 0) / parents.length : 0;
        };
        return bary(a) - bary(b) || a.localeCompare(b);
      });
    }
    layerIds.forEach((id, ri) => rowOf.set(id, ri));
  });

  const tallest = Math.max(1, ...layers.map(l => l.length));
  const nodes = new Map<string, Node>();
  layers.forEach((layerIds, li) => {
    // Centre each column vertically so short columns don't hug the top.
    const colHeight = layerIds.length * NODE_H + (layerIds.length - 1) * ROW_GAP;
    const fullHeight = tallest * NODE_H + (tallest - 1) * ROW_GAP;
    const yOffset = (fullHeight - colHeight) / 2;
    layerIds.forEach((id, ri) => {
      nodes.set(id, {
        id,
        layer: li,
        row: ri,
        x: PAD + li * (NODE_W + COL_GAP),
        y: PAD + yOffset + ri * (NODE_H + ROW_GAP),
      });
    });
  });

  return {
    nodes,
    layers,
    width: PAD * 2 + layers.length * NODE_W + Math.max(0, layers.length - 1) * COL_GAP,
    height: PAD * 2 + tallest * NODE_H + Math.max(0, tallest - 1) * ROW_GAP,
  };
}

function edgePath(from: Node, to: Node): string {
  const x1 = from.x + NODE_W;
  const y1 = from.y + NODE_H / 2;
  const x2 = to.x;
  const y2 = to.y + NODE_H / 2;
  if (x2 <= x1) {
    // Back edge (a cycle, or same column): bow underneath so it stays readable
    // instead of hiding behind the nodes.
    const dip = Math.max(Math.abs(y2 - y1), NODE_H) + 26;
    return `M ${x1} ${y1} C ${x1 + 40} ${y1 + dip}, ${x2 - 40} ${y2 + dip}, ${x2} ${y2}`;
  }
  const c = (x2 - x1) * 0.5;
  return `M ${x1} ${y1} C ${x1 + c} ${y1}, ${x2 - c} ${y2}, ${x2} ${y2}`;
}

export function DependencyFlow({
  edges, focus, onFocus, maxEvidence,
}: {
  edges: ServiceDependency[];
  focus: string | null;
  onFocus: (s: string | null) => void;
  maxEvidence: number;
}) {
  // When focused, draw only that service's immediate neighbourhood. This is what
  // makes a large graph usable: one hop is legible at any fleet size.
  const visible = useMemo(() => {
    if (!focus) return edges;
    return edges.filter(e => e.from_service === focus || e.to_service === focus);
  }, [edges, focus]);

  const nodeCount = useMemo(
    () => new Set(visible.flatMap(e => [e.from_service, e.to_service])).size,
    [visible],
  );

  const tooBig = nodeCount > MAX_NODES || visible.length > MAX_EDGES;
  const l = useMemo(() => layout(tooBig ? [] : visible), [visible, tooBig]);

  if (tooBig) {
    return (
      <div className="adm-empty flow-toobig">
        <p>
          {nodeCount} services and {visible.length} edges — too dense to draw usefully.
        </p>
        <p className="adm-muted">
          Pick a service below to see just its callers and callees, or read the table.
          A diagram of this many edges is a hairball, not an answer.
        </p>
      </div>
    );
  }

  if (visible.length === 0) return null;

  const strokeFor = (count: number) =>
    maxEvidence > 0 ? 1.25 + (count / maxEvidence) * 2.25 : 1.5;

  return (
    <div className="flow">
      <div className="flow__scroll">
        <svg
          className="flow__svg"
          viewBox={`0 0 ${l.width} ${l.height}`}
          width={l.width}
          height={l.height}
          role="img"
          aria-label={`Service dependency flow: ${visible.length} calls between ${nodeCount} services. The table below carries the same data as text.`}
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
            const on = !focus || e.from_service === focus || e.to_service === focus;
            const d = edgePath(a, b);
            return (
              <g key={e.id} className={`flow__edge${on ? " flow__edge--on" : ""}`}>
                <title>{`${e.from_service} → ${e.to_service} · ${e.evidence_count} observations · last seen ${new Date(e.last_seen).toLocaleString()}`}</title>
                <path
                  d={d}
                  className="flow__line"
                  strokeWidth={strokeFor(e.evidence_count)}
                  markerEnd={`url(#${on ? "flow-arrow-on" : "flow-arrow"})`}
                />
                {/* The number is printed, not implied by thickness alone. */}
                <text className="flow__count" dy="-4">
                  <textPath href={`#${e.id}`} startOffset="50%" textAnchor="middle">
                    {e.evidence_count}
                  </textPath>
                </text>
                <path id={e.id} d={d} className="flow__hidden-path" />
              </g>
            );
          })}

          {[...l.nodes.values()].map(n => {
            const on = !focus || n.id === focus;
            return (
              <g
                key={n.id}
                className={`flow__node${n.id === focus ? " flow__node--focus" : ""}${on ? "" : " flow__node--off"}`}
                onClick={() => onFocus(n.id === focus ? null : n.id)}
                role="button"
                tabIndex={0}
                onKeyDown={ev => { if (ev.key === "Enter" || ev.key === " ") onFocus(n.id === focus ? null : n.id); }}
              >
                <title>{`${n.id} — click to ${n.id === focus ? "clear focus" : "show only its callers and callees"}`}</title>
                <rect x={n.x} y={n.y} width={NODE_W} height={NODE_H} rx="7" className="flow__box" />
                <text x={n.x + NODE_W / 2} y={n.y + NODE_H / 2 + 4} textAnchor="middle" className="flow__label">
                  {n.id.length > 20 ? `${n.id.slice(0, 19)}…` : n.id}
                </text>
              </g>
            );
          })}
        </svg>
      </div>
      <p className="flow__legend adm-muted">
        Left to right = call direction. Line weight and the number on each arrow are
        observation counts. Click a service to see only its immediate callers and callees.
        {focus && <> · <button type="button" className="topo-link" onClick={() => onFocus(null)}>show everything</button></>}
      </p>
    </div>
  );
}
