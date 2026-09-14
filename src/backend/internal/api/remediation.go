package api

import (
	"context"
	"crypto/subtle"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"time"

	"github.com/google/uuid"

	pgstore "github.com/chan/agentify/backend/internal/storage/postgres"
)

// RemediationConfig tunes Phase-3 remediation (ADR 0020 / spec 011 Use Cases 1+2).
// Every write action is proposed first and only executed after an explicit
// human approval — never auto-executed regardless of confidence.
type RemediationConfig struct {
	ProposalTTL time.Duration
	AuthToken   string // constant-time bearer check on approve/reject
	Env         string // "dev" | "prod" (ROADMAP OPS-3 / ADR 0020 amendment 2026-09-12); an empty AuthToken is honoured only when this is "dev"
}

// checkRemediationAuth validates the bearer token on approve/reject. This is a
// separate, narrower credential than admin-console auth so the same endpoint
// can be safely called by an authorized external service (Slack interactivity,
// PagerDuty webhook) later without depending on how the console authenticates.
// Mirrors the constant-time bearer-check pattern used for COLLECTOR_TOKEN.
//
// An unset token FAILS CLOSED outside dev (ADR 0020 amendment, 2026-09-12;
// ROADMAP OPS-3). ADR 0020 originally made an empty token mean "open," for
// consistency with COLLECTOR_TOKEN — approve/reject execute a real K8s write
// (restart/scale/rollback), a materially larger exposure than an unset
// collector credential, and remediation is one config change (flipping
// AUTONOMOUS_REMEDIATION_ENABLED/DEPLOY_GUARDIAN_ENABLED) away from live at
// any time. Mirrors checkEvalAuth's identical fix (eval_query.go), applied
// here second because reversing this posture is a decision, not a patch —
// see the ADR amendment for the tradeoff it was weighed against (a
// misconfigured deploy now makes remediation silently unusable, 401 on every
// approve/reject, rather than usable-but-insecure).
func (h *Handler) checkRemediationAuth(r *http.Request) bool {
	if h.remediationConfig.AuthToken == "" {
		env := strings.ToLower(strings.TrimSpace(h.remediationConfig.Env))
		return env == "dev" || env == "" // open for local development only; disabled anywhere else
	}
	const prefix = "Bearer "
	auth := r.Header.Get("Authorization")
	if !strings.HasPrefix(auth, prefix) {
		return false
	}
	token := strings.TrimPrefix(auth, prefix)
	return subtle.ConstantTimeCompare([]byte(token), []byte(h.remediationConfig.AuthToken)) == 1
}

// fetchNamespaceData gathers redacted k8fy signals for a namespace, the same
// way Investigator.investigateAndNotify does — reused here so proposal
// generation and post-execution verification see the same shape of data the
// diagnose path already relies on.
func (h *Handler) fetchNamespaceData(ctx context.Context, namespace string) map[string]interface{} {
	pods, err := h.queryExec.RouteToPods(ctx, "diagnose", namespace, "")
	if err != nil {
		h.logger.Warn("remediation data fetch: routing failed", "namespace", namespace, "error", err)
		return map[string]interface{}{}
	}
	data := map[string]interface{}{}
	for _, pod := range pods {
		// Internal helper, not wired to a specific incoming request's
		// resolved tenant — DefaultTenantID matches today's effectively-
		// single-tenant deployment (ROADMAP OPS-10 / ADR 0022 amendment).
		rows, ferr := h.queryExec.FetchFromPod(ctx, pgstore.DefaultTenantID, pod, nil)
		if ferr != nil {
			continue
		}
		data[pod.ID] = rows
	}
	return h.redactor.RedactFetch(data)
}

// IncidentRespondRequest is the payload for POST /api/incidents/respond.
type IncidentRespondRequest struct {
	Namespace string `json:"namespace"`
	Service   string `json:"service"`
}

// HandleIncidentRespond runs IncidentResponderSkill (propose-only — this call
// makes no infrastructure writes) and persists the result as a `pending`
// remediation proposal (ADR 0020). Execution requires a separate, explicit
// POST /admin/remediation/{id}/approve call.
func (h *Handler) HandleIncidentRespond(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if h.remediationStore == nil {
		http.Error(w, "remediation store not available", http.StatusServiceUnavailable)
		return
	}
	var req IncidentRespondRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "invalid JSON", http.StatusBadRequest)
		return
	}
	if req.Namespace == "" || req.Service == "" {
		http.Error(w, "namespace and service are required", http.StatusBadRequest)
		return
	}

	traceID := uuid.New().String()
	data := h.fetchNamespaceData(r.Context(), req.Namespace)
	agentCtx := map[string]interface{}{"namespace": req.Namespace, "service": req.Service}
	question := fmt.Sprintf("propose a remediation for %s/%s", req.Namespace, req.Service)

	resp, err := h.agentClient.Reason(question, "incident_respond", data, agentCtx, traceID)
	if err != nil {
		h.logger.Warn("incident responder call failed", "error", err)
		http.Error(w, "agent call failed: "+err.Error(), http.StatusInternalServerError)
		return
	}

	proposal, err := h.createProposalFromResponse(r.Context(), "incident_responder", req.Namespace, req.Service, traceID, "", resp)
	if err != nil {
		h.logger.Warn("persist remediation proposal failed", "error", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}
	writeJSON(w, http.StatusOK, remediationToResponse(*proposal))
}

// createProposalFromResponse maps a skill's structured decision (packed into
// AgentResponse.Details by REMEDIATION_REASONING_SCHEMA) into a persisted
// pending proposal.
func (h *Handler) createProposalFromResponse(
	ctx context.Context, useCase, namespace, service, traceID, sourceEventID string, resp *AgentResponse,
) (*pgstore.RemediationProposal, error) {
	action, _ := resp.Details["proposed_action"].(string)
	if action == "" {
		action = "human_escalation"
	}
	params, _ := resp.Details["action_params"].(map[string]interface{})
	if params == nil {
		params = map[string]interface{}{}
	}
	analysis := map[string]interface{}{
		"reasoning":    resp.Details["reasoning"],
		"blast_radius": resp.Details["blast_radius"],
		"evidence":     resp.Details["evidence"],
		"confidence":   resp.Confidence,
	}

	p := &pgstore.RemediationProposal{
		ID:             uuid.New().String(),
		TraceID:        traceID,
		UseCase:        useCase,
		Namespace:      namespace,
		Service:        service,
		ProposedAction: action,
		ActionParams:   params,
		Analysis:       analysis,
		SourceEventID:  sourceEventID,
		ExpiresAt:      time.Now().Add(h.remediationConfig.ProposalTTL),
	}
	if err := h.remediationStore.CreateRemediationProposal(ctx, p); err != nil {
		return nil, err
	}
	return p, nil
}

// HandleRemediationList handles GET /admin/remediation?status=pending
func (h *Handler) HandleRemediationList(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if h.remediationStore == nil {
		writeJSON(w, http.StatusOK, []RemediationProposalResponse{})
		return
	}
	status := r.URL.Query().Get("status")
	proposals, err := h.remediationStore.ListRemediationProposals(r.Context(), status, 100)
	if err != nil {
		h.logger.Warn("list remediation proposals failed", "error", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}
	out := make([]RemediationProposalResponse, 0, len(proposals))
	for _, p := range proposals {
		out = append(out, remediationToResponse(p))
	}
	writeJSON(w, http.StatusOK, out)
}

// HandleRemediationGet handles GET /admin/remediation/{id}
func (h *Handler) HandleRemediationGet(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if h.remediationStore == nil {
		http.Error(w, "remediation store not available", http.StatusServiceUnavailable)
		return
	}
	id := r.PathValue("id")
	p, err := h.remediationStore.GetRemediationProposal(r.Context(), id)
	if err != nil {
		http.Error(w, "not found", http.StatusNotFound)
		return
	}
	writeJSON(w, http.StatusOK, remediationToResponse(*p))
}

// HandleRemediationApprove handles POST /admin/remediation/{id}/approve — the
// explicit human-confirmation step. No action executes without this call.
func (h *Handler) HandleRemediationApprove(w http.ResponseWriter, r *http.Request) {
	h.decideRemediation(w, r, "approved")
}

// HandleRemediationReject handles POST /admin/remediation/{id}/reject.
// Rejecting is terminal and never executes the proposed action.
func (h *Handler) HandleRemediationReject(w http.ResponseWriter, r *http.Request) {
	h.decideRemediation(w, r, "rejected")
}

func (h *Handler) decideRemediation(w http.ResponseWriter, r *http.Request, decision string) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if !h.checkRemediationAuth(r) {
		http.Error(w, "unauthorized", http.StatusUnauthorized)
		return
	}
	if h.remediationStore == nil {
		http.Error(w, "remediation store not available", http.StatusServiceUnavailable)
		return
	}
	id := r.PathValue("id")

	p, err := h.remediationStore.GetRemediationProposal(r.Context(), id)
	if err != nil {
		http.Error(w, "not found", http.StatusNotFound)
		return
	}
	if p.Status != "pending" {
		// Idempotent: a duplicate click or webhook retry after the first
		// decision must not re-decide or re-execute.
		writeJSON(w, http.StatusConflict, map[string]string{
			"status": p.Status, "message": "proposal already decided",
		})
		return
	}
	if time.Now().After(p.ExpiresAt) {
		_, _ = h.remediationStore.DecideRemediationProposal(r.Context(), id, "expired", "")
		writeJSON(w, http.StatusGone, map[string]string{
			"status": "expired", "message": "proposal expired — re-run the propose step against current evidence before approving",
		})
		return
	}

	decidedBy := r.Header.Get("X-Remediation-Actor")
	if decidedBy == "" {
		decidedBy = "admin-console"
	}
	ok, err := h.remediationStore.DecideRemediationProposal(r.Context(), id, decision, decidedBy)
	if err != nil {
		h.logger.Warn("decide remediation proposal failed", "id", id, "error", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}
	if !ok {
		// Lost the race to a concurrent decision — idempotent no-op, not an error.
		writeJSON(w, http.StatusConflict, map[string]string{"message": "already decided"})
		return
	}

	if decision == "rejected" {
		writeJSON(w, http.StatusOK, map[string]string{"status": "rejected"})
		return
	}

	h.executeRemediation(r.Context(), p)
	updated, gerr := h.remediationStore.GetRemediationProposal(r.Context(), id)
	if gerr == nil && updated != nil {
		writeJSON(w, http.StatusOK, remediationToResponse(*updated))
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "executed"})
}

// executeRemediation calls the agent's deterministic execute-only path — no
// Claude call is made to decide anything here, the decision was already made
// and approved; RemediationExecutorSkill just dispatches the named action.
// For incident_responder proposals it then re-runs diagnose once to verify
// the outcome and capture postmortem material. It never retries or chains
// another remediation automatically (ADR 0020).
func (h *Handler) executeRemediation(ctx context.Context, p *pgstore.RemediationProposal) {
	execCtx := map[string]interface{}{
		"namespace": p.Namespace,
		"service":   p.Service,
		"action":    p.ProposedAction,
	}
	for k, v := range p.ActionParams {
		execCtx[k] = v
	}
	question := fmt.Sprintf("execute approved remediation %s for %s/%s", p.ProposedAction, p.Namespace, p.Service)
	resp, err := h.agentClient.Reason(question, "execute_remediation", map[string]interface{}{}, execCtx, p.TraceID)
	if err != nil {
		_ = h.remediationStore.CompleteRemediationProposal(ctx, p.ID, "failed", map[string]interface{}{}, err.Error())
		return
	}

	result := map[string]interface{}{"execution": resp.Details, "execution_answer": resp.Answer}
	if resp.Status == "error" {
		_ = h.remediationStore.CompleteRemediationProposal(ctx, p.ID, "failed", result, resp.Answer)
		return
	}

	if p.UseCase == "incident_responder" {
		data := h.fetchNamespaceData(ctx, p.Namespace)
		verifyCtx := map[string]interface{}{"namespace": p.Namespace, "service": p.Service}
		verifyQ := fmt.Sprintf("Post-remediation check: verify %s/%s after %s.", p.Namespace, p.Service, p.ProposedAction)
		if verifyResp, verr := h.agentClient.Reason(verifyQ, "diagnose", data, verifyCtx, p.TraceID); verr == nil {
			result["verify_status"] = verifyResp.Status
			result["postmortem"] = verifyResp.Details
		} else {
			result["verify_error"] = verr.Error()
		}
	}

	_ = h.remediationStore.CompleteRemediationProposal(ctx, p.ID, "executed", result, "")
}
