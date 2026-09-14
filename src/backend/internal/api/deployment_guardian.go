package api

import (
	"context"
	"fmt"
	"log/slog"
	"time"

	"github.com/google/uuid"

	pgstore "github.com/chan/agentify/backend/internal/storage/postgres"
)

// DeploymentGuardianConfig tunes the deploy-regression poller (spec 011 Use Case 2).
type DeploymentGuardianConfig struct {
	PollInterval   time.Duration
	SettleAfter    time.Duration // only evaluate deploys at least this old, so post-deploy metrics have accumulated
	ProposalTTL    time.Duration
	LookbackWindow time.Duration // how far back to scan for unevaluated deploy events; defaults to 2h
}

// deployEvent is one k8fy.events row of type "deploy" (spec 007).
type deployEvent struct {
	ID         string
	Namespace  string
	Deployment string
	Timestamp  time.Time
}

// DeploymentGuardian watches deploy events and proposes a rollback when the
// post-deploy signal looks worse than pre-deploy — never auto-executes
// (ADR 0020). Same shape as Investigator: an in-process ticker that calls
// the agent directly, reusing existing storage/agent plumbing rather than
// looping back through its own HTTP surface.
type DeploymentGuardian struct {
	queryExec        signalFetcher
	reasoner         reasoner
	remediationStore RemediationStore // nil disables the guardian even if the ticker runs
	logger           *slog.Logger
	cfg              DeploymentGuardianConfig
}

// NewDeploymentGuardian builds a guardian from a Handler's wiring.
func NewDeploymentGuardian(h *Handler, cfg DeploymentGuardianConfig, logger *slog.Logger) *DeploymentGuardian {
	if cfg.LookbackWindow <= 0 {
		cfg.LookbackWindow = 2 * time.Hour
	}
	return &DeploymentGuardian{
		queryExec:        h.queryExec,
		reasoner:         h.agentClient,
		remediationStore: h.remediationStore,
		logger:           logger,
		cfg:              cfg,
	}
}

// Run blocks, polling on each tick until ctx is cancelled. A nil receiver is a no-op.
func (dg *DeploymentGuardian) Run(ctx context.Context) {
	if dg == nil {
		return
	}
	dg.logger.Info("deployment guardian started", "poll_interval", dg.cfg.PollInterval.String())
	ticker := time.NewTicker(dg.cfg.PollInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			dg.logger.Info("deployment guardian stopping")
			return
		case <-ticker.C:
			dg.sweep(ctx)
		}
	}
}

// sweep scans recent deploy events and evaluates any that have settled and
// haven't already been checked.
func (dg *DeploymentGuardian) sweep(ctx context.Context) {
	if dg.remediationStore == nil {
		return
	}
	events, err := dg.fetchDeployEvents(ctx)
	if err != nil {
		dg.logger.Error("deployment guardian: fetch deploy events failed", "error", err)
		return
	}
	now := time.Now()
	for _, ev := range events {
		if ev.Namespace == "" || ev.Deployment == "" {
			continue
		}
		if now.Sub(ev.Timestamp) < dg.cfg.SettleAfter {
			continue // too recent — post-deploy metrics haven't accumulated yet
		}
		exists, err := dg.remediationStore.ProposalExistsForEvent(ctx, ev.ID)
		if err != nil {
			dg.logger.Warn("deployment guardian: dedup check failed", "event_id", ev.ID, "error", err)
			continue
		}
		if exists {
			continue
		}
		dg.checkDeploy(ctx, ev)
	}
}

// fetchDeployEvents returns type=deploy rows from k8fy.events within the
// lookback window, newest first.
func (dg *DeploymentGuardian) fetchDeployEvents(ctx context.Context) ([]deployEvent, error) {
	pods, err := dg.queryExec.RouteToPods(ctx, "change_history", "", "")
	if err != nil {
		return nil, err
	}
	cutoff := time.Now().Add(-dg.cfg.LookbackWindow).UTC().Format(time.RFC3339)

	var out []deployEvent
	for _, pod := range pods {
		// Background sweep, no inbound request to resolve a tenant from;
		// also the events (relational) backend, which doesn't enforce
		// tenantID yet anyway (ROADMAP OPS-10 / ADR 0022 amendment).
		rows, err := dg.queryExec.FetchFromPod(ctx, pgstore.DefaultTenantID, pod, map[string]interface{}{
			"since": cutoff, "order": "desc", "limit": 200,
		})
		if err != nil {
			dg.logger.Warn("deployment guardian: fetch events failed", "pod_id", pod.ID, "error", err)
			continue
		}
		for _, row := range rows {
			if asString(row["type"]) != "deploy" {
				continue
			}
			p := payloadOf(row)
			if p == nil {
				continue
			}
			ts, terr := time.Parse(time.RFC3339, asString(row["timestamp"]))
			if terr != nil {
				continue
			}
			out = append(out, deployEvent{
				ID:         asString(row["id"]),
				Namespace:  asString(p["namespace"]),
				Deployment: asString(p["deployment"]),
				Timestamp:  ts,
			})
		}
	}
	return out, nil
}

// checkDeploy compares pre/post restart-count snapshots via
// DeploymentGuardianSkill and persists a pending proposal only when the
// response marks the deploy as degraded — it never auto-executes.
func (dg *DeploymentGuardian) checkDeploy(ctx context.Context, ev deployEvent) {
	traceID := uuid.New().String()

	data := map[string]interface{}{
		"pre_snapshot":  dg.metricsSnapshot(ctx, ev.Namespace, ev.Deployment, "", ev.Timestamp.UTC().Format(time.RFC3339)),
		"post_snapshot": dg.metricsSnapshot(ctx, ev.Namespace, ev.Deployment, ev.Timestamp.UTC().Format(time.RFC3339), ""),
	}
	agentCtx := map[string]interface{}{"namespace": ev.Namespace, "service": ev.Deployment, "deployment": ev.Deployment}
	question := fmt.Sprintf("compare pre/post deploy health for %s/%s", ev.Namespace, ev.Deployment)

	resp, err := dg.reasoner.Reason(question, "deploy_guardian_check", data, agentCtx, traceID)
	if err != nil {
		dg.logger.Warn("deployment guardian: skill call failed", "namespace", ev.Namespace, "deployment", ev.Deployment, "error", err)
		return
	}

	degraded, _ := resp.Details["degraded"].(bool)
	if !degraded {
		return
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
		UseCase:        "deployment_guardian",
		Namespace:      ev.Namespace,
		Service:        ev.Deployment,
		ProposedAction: action,
		ActionParams:   params,
		Analysis: map[string]interface{}{
			"reasoning":    resp.Details["reasoning"],
			"blast_radius": resp.Details["blast_radius"],
			"evidence":     resp.Details["evidence"],
			"confidence":   resp.Confidence,
		},
		SourceEventID: ev.ID,
		ExpiresAt:     time.Now().Add(dg.cfg.ProposalTTL),
	}
	if err := dg.remediationStore.CreateRemediationProposal(ctx, p); err != nil {
		dg.logger.Warn("deployment guardian: persist proposal failed", "event_id", ev.ID, "error", err)
		return
	}
	dg.logger.Info("deployment guardian: remediation proposal created",
		"namespace", ev.Namespace, "deployment", ev.Deployment, "proposal_id", p.ID, "action", action)
}

// metricsSnapshot returns the most recent restart-count sample in the given
// [since,until) window (empty bound = unbounded) for a deployment, or a note
// when none exist — the caller reasons over these numbers, never guesses.
func (dg *DeploymentGuardian) metricsSnapshot(ctx context.Context, namespace, deployment, since, until string) map[string]interface{} {
	pods, err := dg.queryExec.RouteToPods(ctx, "metrics_history", namespace, "")
	if err != nil {
		return map[string]interface{}{"note": "metrics unavailable"}
	}
	query := map[string]interface{}{"deployment": deployment, "order": "desc", "limit": 1}
	if since != "" {
		query["since"] = since
	}
	if until != "" {
		query["until"] = until
	}
	for _, pod := range pods {
		rows, err := dg.queryExec.FetchFromPod(ctx, pgstore.DefaultTenantID, pod, query)
		if err != nil || len(rows) == 0 {
			continue
		}
		return map[string]interface{}{"sample": payloadOf(rows[0]), "timestamp": rows[0]["timestamp"]}
	}
	return map[string]interface{}{"note": "no restart-count sample in this window"}
}
