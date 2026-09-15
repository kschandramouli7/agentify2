import type { CallTrace, TraceHop } from "../api";

// Simpler layout than DependencyFlow.tsx on purpose: order is already given
// by the data (chronological hop order from trace_search.py), so this needs
// N vertical lifelines + one row per hop, not a Sugiyama layering pass.
const COL_W = 170;
const ROW_H = 46;
const PAD_X = 90;
const PAD_TOP = 40;
const PAD_BOTTOM = 24;

function laneLabel(hop: TraceHop): string {
  return hop.service ?? `${hop.pod_name} (unattributed)`;
}

function outcomeTone(outcome: TraceHop["outcome"]): "ok" | "warn" | "crit" | "muted" {
  if (outcome === "success") return "ok";
  if (outcome === "timeout") return "warn";
  if (outcome === "failure") return "crit";
  return "muted";
}

function elapsedLabel(hop: TraceHop, first: TraceHop): string {
  const ms = new Date(hop.timestamp).getTime() - new Date(first.timestamp).getTime();
  if (!Number.isFinite(ms) || ms < 0) return "";
  if (ms < 1000) return `+${ms}ms`;
  return `+${(ms / 1000).toFixed(2)}s`;
}

export function SequenceDiagram({ trace }: { trace: CallTrace }) {
  const { hops } = trace;
  if (hops.length === 0) return null;

  const lanes: string[] = [];
  for (const h of hops) {
    const label = laneLabel(h);
    if (!lanes.includes(label)) lanes.push(label);
  }
  const laneX = new Map(lanes.map((l, i) => [l, PAD_X + i * COL_W]));

  const width = PAD_X * 2 + Math.max(0, lanes.length - 1) * COL_W;
  const height = PAD_TOP + hops.length * ROW_H + PAD_BOTTOM;
  const first = hops[0];

  return (
    <div className="seq">
      <p className="seq__caveat">
        Order reflects when each service's own logs mentioned this call — the closest
        approximation available without a propagated trace header, not a confirmed
        caller/callee edge.
      </p>
      <div className="seq__scroll">
        <svg
          className="seq__svg"
          viewBox={`0 0 ${width} ${height}`}
          width={width}
          height={height}
          role="img"
          aria-label={
            `Sequence diagram for "${trace.query}": ${hops.length} log line` +
            `${hops.length === 1 ? "" : "s"} across ${lanes.length} service` +
            `${lanes.length === 1 ? "" : "s"}, in chronological order. ` +
            `The message above lists the same hops as text.`
          }
        >
          {/* Lifelines, drawn first so ticks/arrows paint over them. */}
          {lanes.map(l => {
            const x = laneX.get(l)!;
            return (
              <g key={l} className="seq__lane">
                <text x={x} y={20} textAnchor="middle" className="seq__lane-label">{l}</text>
                <line x1={x} y1={PAD_TOP - 8} x2={x} y2={height - PAD_BOTTOM + 4} className="seq__lifeline" />
              </g>
            );
          })}

          {/* One row per hop: an activation tick, plus an arrow from the
              previous hop when the lane changed. Same-lane consecutive hops
              get successive ticks with no arrow between them. */}
          {hops.map((h, i) => {
            const y = PAD_TOP + i * ROW_H + ROW_H / 2;
            const x = laneX.get(laneLabel(h))!;
            const prev = i > 0 ? hops[i - 1] : null;
            const prevX = prev ? laneX.get(laneLabel(prev))! : null;
            const tone = outcomeTone(h.outcome);
            return (
              <g key={h.seq} className={`seq__hop seq__hop--${tone}`}>
                <title>{`${h.seq}. ${h.timestamp}\n${laneLabel(h)}${h.outcome ? ` [${h.outcome}]` : ""}\n${h.log_excerpt}`}</title>
                {prev && prevX !== null && prevX !== x && (
                  <>
                    <line x1={prevX} y1={y} x2={x} y2={y} className={`seq__arrow seq__arrow--${tone}`}
                          markerEnd="url(#seq-arrow)" />
                    <text x={(prevX + x) / 2} y={y - 6} textAnchor="middle" className="seq__arrow-label">
                      {h.outcome ?? ""} {elapsedLabel(h, first)}
                    </text>
                  </>
                )}
                <circle cx={x} cy={y} r={5} className={`seq__tick seq__tick--${tone}`} />
                <text x={x + 10} y={y + 4} className="seq__tick-label">{h.seq}</text>
              </g>
            );
          })}

          <defs>
            <marker id="seq-arrow" viewBox="0 0 8 8" refX="7" refY="4"
                    markerWidth="7" markerHeight="7" orient="auto-start-reverse">
              <path d="M 0 0 L 8 4 L 0 8 z" className="seq__arrowhead" />
            </marker>
          </defs>
        </svg>
      </div>
    </div>
  );
}
