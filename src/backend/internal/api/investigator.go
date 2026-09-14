package api

import (
	"context"
	"fmt"
	"log/slog"
	"regexp"
	"sort"
	"strings"
	"time"

	"github.com/google/uuid"

	"github.com/chan/agentify/backend/internal/governance"
	"github.com/chan/agentify/backend/internal/models"
	"github.com/chan/agentify/backend/internal/orchestrator/evaluator"
	"github.com/chan/agentify/backend/internal/storage/registry"
	pgstore "github.com/chan/agentify/backend/internal/storage/postgres"
)

// k8sPodSuffix strips the two trailing K8s hash segments from a pod name to
// recover its deployment/service name (same technique used by
// storage/postgres.TrackedEntities).
var k8sPodSuffix = regexp.MustCompile(`-[a-z0-9]{6,12}-[a-z0-9]{5}$`)

// reasoner is the slice of the agent client the investigator needs; an interface
// so the sweep loop can be tested without a real agent.
type reasoner interface {
	Reason(question, intent string, data, context map[string]interface{}, traceID string) (*AgentResponse, error)
}

// signalFetcher is the slice of QueryExecutor the investigator needs; an interface
// so gather/investigate can be tested without real storage. *orchestrator.QueryExecutor
// satisfies it.
type signalFetcher interface {
	RouteToPods(ctx context.Context, intent, namespace, clusterID string) ([]*models.Pod, error)
	FetchFromPod(ctx context.Context, tenantID string, pod *models.Pod, query map[string]interface{}) ([]map[string]interface{}, error)
}

// InvestigationConfig tunes the proactive loop (spec 009 / ADR 0016).
type InvestigationConfig struct {
	SweepInterval   time.Duration
	MaxPerSweep     int
	Cooldown        time.Duration
	CertCriticalDays int

	// RemediationEnabled additionally proposes a remediation (ADR 0020 / spec
	// 011 Use Case 1) when a namespace diagnoses as DEGRADED/UNHEALTHY. It is
	// never auto-executed — only a pending proposal is created.
	RemediationEnabled bool
	ProposalTTL        time.Duration
}

// nsAnomaly is the deterministic trigger summary for one namespace.
type nsAnomaly struct {
	reasons      []string
	services     []string // best-effort deployment names derived from failing pods, for remediation targeting
	hasUnhealthy bool      // pod-level failure → critical; cert-only → warning
}

func (a nsAnomaly) severity() string {
	if a.hasUnhealthy {
		return "critical"
	}
	return "warning"
}

type incident struct {
	openedAt time.Time
}

// Investigator runs the periodic sweep → diagnose → notify loop. It reuses the
// deterministic evaluator to trigger and the existing diagnose path to explain.
type Investigator struct {
	registry         registry.PodStore
	queryExec        signalFetcher
	reasoner         reasoner
	redactor         *governance.Redactor
	notifier         Notifier
	remediationStore RemediationStore // nil disables remediation proposals even if cfg.RemediationEnabled
	logger           *slog.Logger
	cfg              InvestigationConfig

	open      map[string]incident  // currently-open incidents, keyed by namespace
	lastAlert map[string]time.Time // last open/close action per namespace (cooldown)
}

// NewInvestigator builds an investigator from a Handler's wiring plus a notifier.
func NewInvestigator(h *Handler, notifier Notifier, cfg InvestigationConfig, logger *slog.Logger) *Investigator {
	if cfg.MaxPerSweep <= 0 {
		cfg.MaxPerSweep = 5
	}
	return &Investigator{
		registry:         h.orch.GetPodRegistry(),
		queryExec:        h.queryExec,
		reasoner:         h.agentClient,
		redactor:         h.redactor,
		notifier:         notifier,
		remediationStore: h.remediationStore,
		logger:           logger,
		cfg:              cfg,
		open:             map[string]incident{},
		lastAlert:        map[string]time.Time{},
	}
}

// Run blocks, sweeping on each tick until ctx is cancelled. A nil receiver is a
// no-op (loop disabled).
func (in *Investigator) Run(ctx context.Context) {
	if in == nil {
		return
	}
	in.logger.Info("investigation loop started",
		"interval", in.cfg.SweepInterval.String(), "max_per_sweep", in.cfg.MaxPerSweep)
	ticker := time.NewTicker(in.cfg.SweepInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			in.logger.Info("investigation loop stopping")
			return
		case <-ticker.C:
			in.sweep(ctx)
		}
	}
}

// sweep runs one cycle: gather anomalies, decide opens/closes, act.
func (in *Investigator) sweep(ctx context.Context) {
	anomalous, err := in.gather(ctx)
	if err != nil {
		in.logger.Error("investigation sweep gather failed", "error", err)
		return
	}
	toOpen, toClose, deferred := decideActions(anomalous, in.open, in.lastAlert, time.Now(), in.cfg.Cooldown, in.cfg.MaxPerSweep)

	now := time.Now()
	for _, ns := range toClose {
		if err := in.notifier.Send(ctx, Alert{Namespace: ns, Resolved: true}); err != nil {
			in.logger.Warn("resolve notification failed", "namespace", ns, "error", err)
		}
		delete(in.open, ns)
		in.lastAlert[ns] = now
	}
	for _, ns := range toOpen {
		in.investigateAndNotify(ctx, ns, anomalous[ns])
		in.open[ns] = incident{openedAt: now}
		in.lastAlert[ns] = now
	}
	if deferred > 0 {
		in.logger.Warn("investigation cap reached; deferred namespaces to next sweep", "deferred", deferred)
	}
}

// decideActions is the pure open/close/cooldown/cap decision (unit-tested). It
// returns namespaces to open (cap-bounded), namespaces to close, and how many
// eligible candidates were deferred by the cap.
func decideActions(
	anomalous map[string]nsAnomaly,
	open map[string]incident,
	lastAlert map[string]time.Time,
	now time.Time,
	cooldown time.Duration,
	maxPerSweep int,
) (toOpen, toClose []string, deferred int) {
	// Close incidents whose namespace is no longer anomalous.
	for ns := range open {
		if _, still := anomalous[ns]; !still {
			toClose = append(toClose, ns)
		}
	}
	sort.Strings(toClose)

	// Candidates: anomalous, not already open, past cooldown.
	var candidates []string
	for ns := range anomalous {
		if _, isOpen := open[ns]; isOpen {
			continue
		}
		if last, ok := lastAlert[ns]; ok && now.Sub(last) < cooldown {
			continue
		}
		candidates = append(candidates, ns)
	}
	sort.Strings(candidates) // deterministic selection under the cap
	if len(candidates) > maxPerSweep {
		deferred = len(candidates) - maxPerSweep
		candidates = candidates[:maxPerSweep]
	}
	return candidates, toClose, deferred
}

// gather scans current_state and returns the anomalous namespaces (spec 009).
func (in *Investigator) gather(ctx context.Context) (map[string]nsAnomaly, error) {
	integration := "k8fy"
	pods, err := in.registry.ListPods(ctx, &models.PodFilter{Namespace: &integration})
	if err != nil {
		return nil, err
	}
	out := map[string]nsAnomaly{}
	add := func(ns, reason, service string, unhealthy bool) {
		if ns == "" {
			return
		}
		a := out[ns]
		a.reasons = append(a.reasons, reason)
		a.hasUnhealthy = a.hasUnhealthy || unhealthy
		if service != "" && !containsString(a.services, service) {
			a.services = append(a.services, service)
		}
		out[ns] = a
	}

	for _, pod := range pods {
		if pod.Kind == "index" {
			continue
		}
		switch {
		case strings.HasPrefix(pod.ID, "k8fy.live-state."):
			ns := strings.TrimPrefix(pod.ID, "k8fy.live-state.")
			// No inbound HTTP request to resolve a tenant from — a background
			// sweep, not a per-request handler. DefaultTenantID matches every
			// other unauthenticated call in this effectively-single-tenant
			// deployment today (ROADMAP OPS-10 / ADR 0022 amendment); a real
			// per-tenant investigation loop is a separate, larger feature.
			rows, err := in.queryExec.FetchFromPod(ctx, pgstore.DefaultTenantID, pod, nil)
			if err != nil {
				in.logger.Warn("sweep fetch failed", "pod_id", pod.ID, "error", err)
				continue
			}
			for _, row := range rows {
				p := payloadOf(row)
				if p == nil || p["phase"] == nil { // skip non-pod rows (e.g. services)
					continue
				}
				if status, reason := evaluator.PodHealth(p); status == evaluator.StatusUnhealthy {
					podID := asString(p["pod_id"])
					service := k8sPodSuffix.ReplaceAllString(podID, "")
					add(ns, fmt.Sprintf("pod %s: %s", podID, reason), service, true)
				}
			}
		case pod.ID == "k8fy.certificates":
			// No inbound HTTP request to resolve a tenant from — a background
			// sweep, not a per-request handler. DefaultTenantID matches every
			// other unauthenticated call in this effectively-single-tenant
			// deployment today (ROADMAP OPS-10 / ADR 0022 amendment); a real
			// per-tenant investigation loop is a separate, larger feature.
			rows, err := in.queryExec.FetchFromPod(ctx, pgstore.DefaultTenantID, pod, nil)
			if err != nil {
				in.logger.Warn("sweep fetch failed", "pod_id", pod.ID, "error", err)
				continue
			}
			for _, row := range rows {
				p := payloadOf(row)
				if p == nil {
					continue
				}
				_, days, _ := evaluator.CertRenewal(p)
				if days < in.cfg.CertCriticalDays {
					add(asString(p["namespace"]), fmt.Sprintf("cert %s expires in %dd", asString(p["secret"]), days), "", false)
				}
			}
		}
	}
	return out, nil
}

// investigateAndNotify runs the diagnose path for a namespace and sends one alert.
func (in *Investigator) investigateAndNotify(ctx context.Context, ns string, an nsAnomaly) {
	traceID := uuid.New().String()

	// Reuse the diagnose fan-out: all k8fy leaves for the namespace, redacted.
	pods, err := in.queryExec.RouteToPods(ctx, "diagnose", ns, "")
	if err != nil {
		in.logger.Error("investigation routing failed", "namespace", ns, "error", err)
		return
	}
	data := map[string]interface{}{}
	for _, pod := range pods {
		// Background sweep, no inbound request — see gather()'s comment above.
		rows, err := in.queryExec.FetchFromPod(ctx, pgstore.DefaultTenantID, pod, nil)
		if err != nil {
			continue
		}
		data[pod.ID] = rows
	}
	agentData := in.redactor.RedactFetch(data)

	question := fmt.Sprintf("Proactive check: namespace %s tripped a deterministic alert (%s). Diagnose it.", ns, strings.Join(an.reasons, "; "))
	resp, err := in.reasoner.Reason(question, "diagnose", agentData, map[string]interface{}{"namespace": ns}, traceID)
	if err != nil {
		in.logger.Error("investigation reasoning failed", "namespace", ns, "trace_id", traceID, "error", err)
		// Still notify with the deterministic trigger so a failure is visible.
		_ = in.notifier.Send(ctx, Alert{Namespace: ns, Severity: an.severity(), Reasons: an.reasons,
			Summary: "Automated diagnosis failed; raised from deterministic trigger only.", TraceID: traceID})
		return
	}

	alert := Alert{
		Namespace: ns,
		Severity:  severityFrom(resp, an),
		Reasons:   an.reasons,
		Summary:   in.redactor.RedactText(resp.Answer), // defense-in-depth on prose egress
		Cause:     in.redactor.RedactText(detailString(resp, "likely_cause")),
		Actions:   detailStrings(resp, "recommendations"),
		Sources:   resp.Sources,
		TraceID:   traceID,
	}

	// Phase-3 remediation (ADR 0020 / spec 011 Use Case 1) — opt-in. A
	// DEGRADED/UNHEALTHY diagnosis additionally produces a pending proposal;
	// it is never auto-executed. Requires an identified service to target.
	if in.cfg.RemediationEnabled && in.remediationStore != nil &&
		(resp.Status == "degraded" || resp.Status == "unhealthy") && len(an.services) > 0 {
		alert.ProposalID = in.proposeRemediation(ctx, ns, an.services[0], traceID, agentData)
	}

	if err := in.notifier.Send(ctx, alert); err != nil {
		in.logger.Warn("alert notification failed", "namespace", ns, "trace_id", traceID, "error", err)
		return
	}
	in.logger.Info("investigation alert sent", "namespace", ns, "severity", alert.Severity, "trace_id", traceID)
}

// proposeRemediation calls IncidentResponderSkill (propose-only — no
// infrastructure calls) and persists the result as a pending proposal.
// Returns "" (and logs) on any failure — a failed proposal must never block
// or degrade the read-only alert this sweep already produces.
func (in *Investigator) proposeRemediation(ctx context.Context, ns, service, traceID string, agentData map[string]interface{}) string {
	question := fmt.Sprintf("propose a remediation for %s/%s", ns, service)
	resp, err := in.reasoner.Reason(question, "incident_respond", agentData, map[string]interface{}{"namespace": ns, "service": service}, traceID)
	if err != nil {
		in.logger.Warn("incident responder proposal failed", "namespace", ns, "service", service, "error", err)
		return ""
	}

	action, _ := resp.Details["proposed_action"].(string)
	if action == "" {
		action = "human_escalation"
	}
	params, _ := resp.Details["action_params"].(map[string]interface{})
	if params == nil {
		params = map[string]interface{}{}
	}
	p := &pgstore.RemediationProposal{
		ID:             uuid.New().String(),
		TraceID:        traceID,
		UseCase:        "incident_responder",
		Namespace:      ns,
		Service:        service,
		ProposedAction: action,
		ActionParams:   params,
		Analysis: map[string]interface{}{
			"reasoning":    resp.Details["reasoning"],
			"blast_radius": resp.Details["blast_radius"],
			"evidence":     resp.Details["evidence"],
			"confidence":   resp.Confidence,
		},
		ExpiresAt: time.Now().Add(in.cfg.ProposalTTL),
	}
	if err := in.remediationStore.CreateRemediationProposal(ctx, p); err != nil {
		in.logger.Warn("persist remediation proposal failed", "namespace", ns, "service", service, "error", err)
		return ""
	}
	return p.ID
}

// containsString reports whether s is already present in list.
func containsString(list []string, s string) bool {
	for _, v := range list {
		if v == s {
			return true
		}
	}
	return false
}

// --- helpers ---

func payloadOf(row map[string]interface{}) map[string]interface{} {
	if p, ok := row["payload"].(map[string]interface{}); ok {
		return p
	}
	return nil
}

func asString(v interface{}) string {
	if s, ok := v.(string); ok {
		return s
	}
	return ""
}

// severityFrom prefers the agent's structured severity, falling back to the
// deterministic trigger severity.
func severityFrom(resp *AgentResponse, an nsAnomaly) string {
	if s := detailString(resp, "severity"); s != "" {
		return s
	}
	return an.severity()
}

func detailString(resp *AgentResponse, key string) string {
	if resp.Details == nil {
		return ""
	}
	return asString(resp.Details[key])
}

func detailStrings(resp *AgentResponse, key string) []string {
	if resp.Details == nil {
		return nil
	}
	raw, ok := resp.Details[key].([]interface{})
	if !ok {
		return nil
	}
	out := make([]string, 0, len(raw))
	for _, v := range raw {
		if s, ok := v.(string); ok {
			out = append(out, s)
		}
	}
	return out
}
