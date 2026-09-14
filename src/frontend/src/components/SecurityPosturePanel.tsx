import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  listSecurityFindings, createSecurityEngagement, listSecurityEngagements,
  approveSecurityEngagement, rejectSecurityEngagement,
  type SecurityFinding, type SecurityEngagement,
} from "../api";

// Deployment Security Posture (ROADMAP P30 phase 1, ADR 0033).
//
// Read-only, evidence-first, same boundary as every other panel here: a
// finding names what was observed and why it matters, never an auto-fix
// button. Phase 1 ships three checks (NetworkPolicy coverage, pod
// securityContext, Ingress TLS) — deliberately not the whole security
// audit that motivated this, since two of those checks needed the smallest
// possible new RBAC grant and one needed none at all, proving the pattern
// before asking for broader cluster access. Phases 2-4 (active
// verification, exploitability checks, pentest orchestration) are a named
// future direction, not built here — see ADR 0033.
//
// Findings are config-state, not accumulated evidence, so "resolved" is a
// real, meaningful status here (the issue stopped reproducing), not a
// terminal state that hides the row — same "never hide the data" principle
// the Dependencies panel's health work already established.

// /admin/tracked returns "namespace/service" pairs — same source the
// Dependencies panel's namespace picker uses. Namespaces are its distinct
// prefixes. Duplicated here rather than imported from TopologyPanel,
// matching this codebase's own convention of small pure-function
// duplication over cross-component coupling.
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

function StatCard({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="adm-stat">
      <div className="adm-stat__value">{value}</div>
      <div className="adm-stat__label">{label}</div>
      {sub && <div className="adm-stat__sub">{sub}</div>}
    </div>
  );
}

// critical/high -> crit, medium -> warn, low -> ok — reusing the app's
// existing three-tone status vocabulary rather than inventing a fourth
// shade for "low", same "don't grow the palette" discipline as the
// Dependencies panel's edge-health colouring.
function severityTone(sev: SecurityFinding["severity"]): "crit" | "warn" | "ok" {
  if (sev === "critical" || sev === "high") return "crit";
  if (sev === "medium") return "warn";
  return "ok";
}

function SeverityBadge({ severity }: { severity: SecurityFinding["severity"] }) {
  return <span className={`adm-badge adm-badge--${severityTone(severity)}`}>{severity}</span>;
}

function StatusCell({ status }: { status: SecurityFinding["status"] }) {
  if (status === "resolved") {
    return <span className="adm-muted">resolved</span>;
  }
  if (status === "acknowledged") {
    return <span className="adm-badge adm-badge--warn">acknowledged</span>;
  }
  return <span className="adm-badge adm-badge--crit">open</span>;
}

const ENGAGEMENT_STATUS_CLS: Record<string, string> = {
  pending:   "adm-badge adm-badge--warn",
  approved:  "adm-badge adm-badge--tier2",
  active:    "adm-badge adm-badge--tier2",
  completed: "adm-badge adm-badge--ok",
  rejected:  "adm-badge adm-badge--muted",
  expired:   "adm-badge adm-badge--muted",
  failed:    "adm-badge adm-badge--crit",
};

// Mirrors RemediationPanel.tsx's ProposalCard exactly — same propose/
// approve-reject shape (ADR 0033 deliberately copies ADR 0020's gate), same
// "no confirmation dialog, the button title says what it does" convention.
function EngagementCard({ e, onDecided }: { e: SecurityEngagement; onDecided: () => void }) {
  const [busy, setBusy] = useState<"approve" | "reject" | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const isPending = e.status === "pending";

  async function decide(action: "approve" | "reject") {
    setBusy(action);
    setErr(null);
    try {
      if (action === "approve") await approveSecurityEngagement(e.id);
      else await rejectSecurityEngagement(e.id);
      onDecided();
    } catch (err2) {
      setErr(err2 instanceof Error ? err2.message : `${action} failed`);
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className={`check-card check-card--${e.status === "pending" ? "warn" : e.status === "completed" ? "ok" : e.status === "failed" ? "crit" : "muted"}`}>
      <div className="check-card__header">
        <span className="check-card__label">{e.target_resource_kind}/{e.target_resource_name}</span>
        <span className="adm-badge adm-badge--muted">{e.technique}</span>
        <span className={ENGAGEMENT_STATUS_CLS[e.status] ?? "adm-badge adm-badge--muted"} style={{ marginLeft: "auto" }}>
          {e.status}
        </span>
      </div>
      <p className="check-card__answer">
        Verifying <strong>{e.target_check_id}</strong> against {e.target_namespace}/{e.target_resource_name}
      </p>
      <div className="remediation-meta muted small">
        requested {relTime(e.created_at)}
        {isPending && ` · expires ${relTime(e.expires_at)}`}
        {e.approved_by && ` · decided by ${e.approved_by}`}
      </div>
      {e.result && Object.keys(e.result).length > 0 && (
        <details className="remediation-result">
          <summary>Verification result</summary>
          <pre>{JSON.stringify(e.result, null, 2)}</pre>
        </details>
      )}
      {e.error && <p className="adm-error">{e.error}</p>}
      {isPending && (
        <div className="remediation-actions">
          <button
            className="adm-btn adm-btn--primary"
            disabled={busy !== null}
            onClick={() => decide("approve")}
            title="Dispatch this check now — no further confirmation"
          >
            {busy === "approve" ? "Approving…" : "✓ Approve & verify"}
          </button>
          <button className="adm-btn adm-btn--ghost" disabled={busy !== null} onClick={() => decide("reject")}>
            {busy === "reject" ? "Rejecting…" : "✕ Reject"}
          </button>
        </div>
      )}
      {err && <p className="adm-error">{err}</p>}
    </div>
  );
}

function RequestVerificationButton({ finding, onRequested }: { finding: SecurityFinding; onRequested: () => void }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  if (!finding.target_host || finding.status === "resolved") {
    return <span className="adm-muted">—</span>;
  }

  async function request() {
    setBusy(true);
    setErr(null);
    try {
      await createSecurityEngagement(finding);
      onRequested();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "request failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <button className="adm-btn adm-btn--ghost" type="button" disabled={busy} onClick={request}>
        {busy ? "Requesting…" : "Request verification"}
      </button>
      {err && <div className="adm-error">{err}</div>}
    </>
  );
}

export function SecurityPosturePanel() {
  const [applied, setApplied] = useState("");
  const [typed, setTyped] = useState("");
  const [showResolved, setShowResolved] = useState(false);
  const queryClient = useQueryClient();

  const { data: namespaces = [] } = useQuery({
    queryKey: ["tracked-namespaces"],
    queryFn: fetchNamespaces,
    refetchInterval: (q) => ((q.state.data ?? []).length === 0 ? 3000 : 30000),
  });

  useEffect(() => {
    if (applied || namespaces.length === 0) return;
    setApplied(namespaces.includes("payments") ? "payments" : namespaces[0]);
  }, [namespaces, applied]);

  const { data: findings = [], isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: ["security-findings", applied],
    queryFn: () => listSecurityFindings(applied),
    enabled: applied.length > 0,
  });

  const { data: engagements = [] } = useQuery({
    queryKey: ["security-engagements"],
    queryFn: () => listSecurityEngagements(),
    enabled: applied.length > 0,
    refetchInterval: 10000,
  });

  function refreshEngagements() {
    queryClient.invalidateQueries({ queryKey: ["security-engagements"] });
    queryClient.invalidateQueries({ queryKey: ["security-findings", applied] });
  }

  const nsEngagements = engagements.filter(e => e.target_namespace === applied);
  const pendingEngagements = nsEngagements.filter(e => e.status === "pending");
  const historyEngagements = nsEngagements.filter(e => e.status !== "pending");

  const visible = showResolved ? findings : findings.filter(f => f.status !== "resolved");
  const openCount = findings.filter(f => f.status === "open").length;
  const criticalCount = findings.filter(f => f.status !== "resolved" && f.severity === "critical").length;
  const resolvedCount = findings.filter(f => f.status === "resolved").length;

  return (
    <div className="adm-panel">
      <div className="adm-panel__header">
        <div>
          <h2>Security Posture</h2>
          <p className="adm-panel__desc">
            Deployment-security checks against this namespace's live Kubernetes config —
            <strong> not code vulnerabilities</strong>, how things are deployed and exposed:
            NetworkPolicy coverage, pod <code>securityContext</code>, and Ingress TLS. Every
            finding names the exact resource and evidence observed; nothing here is auto-fixed
            — this reports, a human decides. A namespace that passes every check shows no open
            findings, not a blank panel.
          </p>
        </div>
        <div className="adm-filters">
          {namespaces.length > 0 ? (
            <select
              className="adm-date-input"
              value={applied}
              onChange={e => setApplied(e.target.value)}
              aria-label="Namespace"
            >
              {!namespaces.includes(applied) && applied && <option value={applied}>{applied}</option>}
              {namespaces.map(ns => <option key={ns} value={ns}>{ns}</option>)}
            </select>
          ) : (
            <>
              <input
                className="adm-date-input"
                value={typed}
                onChange={e => setTyped(e.target.value)}
                onKeyDown={e => { if (e.key === "Enter" && typed) setApplied(typed); }}
                placeholder="namespace"
                aria-label="Namespace"
              />
              <button
                className="adm-btn adm-btn--ghost"
                type="button"
                disabled={!typed}
                onClick={() => setApplied(typed)}
              >
                Load
              </button>
            </>
          )}
          <button className="adm-btn adm-btn--ghost" type="button" onClick={() => refetch()}>
            {isFetching ? "Refreshing…" : "Refresh"}
          </button>
          <label>
            <input type="checkbox" checked={showResolved} onChange={e => setShowResolved(e.target.checked)} />
            {" "}Show resolved
          </label>
        </div>
      </div>

      {isLoading && <p className="adm-loading">Loading…</p>}
      {isError && (
        <p className="adm-error">{error instanceof Error ? error.message : "Failed to load security findings."}</p>
      )}

      {!isLoading && !isError && findings.length === 0 && (
        <div className="adm-empty">
          <p>
            No findings recorded yet for <strong>{applied}</strong>.
          </p>
          <p className="adm-muted">
            Either this namespace has not been scanned yet (Discovery's next cycle will report
            it, whether clean or not — no findings is not yet the same as "checked and clean"
            until at least one push has happened), or it has passed every Phase 1 check.
          </p>
        </div>
      )}

      {findings.length > 0 && (
        <>
          <div className="adm-stats-row">
            <StatCard label="Open findings" value={String(openCount)} sub={openCount === 0 ? "none open" : undefined} />
            <StatCard
              label="Critical"
              value={String(criticalCount)}
              sub={criticalCount > 0 ? "needs attention" : "none open"}
            />
            <StatCard label="Resolved" value={String(resolvedCount)} sub="no longer reproducing" />
          </div>

          {criticalCount > 0 && (
            <p className="topo-gap">
              <span className="adm-badge adm-badge--crit">critical</span>{" "}
              {criticalCount === 1 ? "1 finding needs" : `${criticalCount} findings need`} attention in{" "}
              <strong>{applied}</strong>.
            </p>
          )}

          <div className="adm-table-wrap">
            <table className="adm-table">
              <thead>
                <tr>
                  <th>Severity</th><th>Check</th><th>Resource</th><th>Evidence</th>
                  <th>Status</th><th>First seen</th><th>Last seen</th><th>Verification</th>
                </tr>
              </thead>
              <tbody>
                {visible.map(f => (
                  <tr key={`${f.check_id}/${f.resource_kind}/${f.resource_name}`}>
                    <td><SeverityBadge severity={f.severity} /></td>
                    <td><code>{f.check_id}</code></td>
                    <td>{f.resource_kind}/{f.resource_name}</td>
                    <td>{f.evidence}</td>
                    <td><StatusCell status={f.status} /></td>
                    <td title={f.first_seen}>{relTime(f.first_seen)}</td>
                    <td title={f.last_seen}>{relTime(f.last_seen)}</td>
                    <td>
                      {f.confidence === "confirmed-live" ? (
                        <span className="adm-badge adm-badge--ok">confirmed live</span>
                      ) : f.confidence === "refuted" ? (
                        <span className="adm-badge adm-badge--muted">not reachable</span>
                      ) : (
                        <RequestVerificationButton finding={f} onRequested={refreshEngagements} />
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {visible.length === 0 && (
            <p className="adm-muted">Every finding in {applied} is resolved. Check "Show resolved" to see history.</p>
          )}
        </>
      )}

      {(pendingEngagements.length > 0 || historyEngagements.length > 0) && (
        <div className="adm-panel__section">
          <h3>Active verification (ROADMAP P30 phase 2)</h3>
          <p className="adm-panel__desc">
            Each engagement below authorizes one network check — a plaintext HTTP request to the
            finding's own host — dispatched to an isolated verifier service that Claude has no
            path to at all. Approving does not fix anything; it only confirms whether the
            config-level finding is reachable in practice.
          </p>
          {pendingEngagements.length > 0 && (
            <div className="check-card-list">
              {pendingEngagements.map(e => (
                <EngagementCard key={e.id} e={e} onDecided={refreshEngagements} />
              ))}
            </div>
          )}
          {historyEngagements.length > 0 && (
            <details className="remediation-history">
              <summary>History ({historyEngagements.length})</summary>
              <div className="check-card-list">
                {historyEngagements.map(e => (
                  <EngagementCard key={e.id} e={e} onDecided={refreshEngagements} />
                ))}
              </div>
            </details>
          )}
        </div>
      )}
    </div>
  );
}
