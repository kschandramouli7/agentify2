package api

import (
	"bytes"
	"context"
	"crypto/subtle"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/google/uuid"

	pgstore "github.com/chan/agentify/backend/internal/storage/postgres"
)

// SecurityEngagementConfig tunes ROADMAP P30 phases 2-4 (ADR 0033). Every
// active-verification technique is proposed first and only dispatched after
// an explicit human approval — same shape as RemediationConfig, deliberately:
// this is a strictly higher-consequence action than remediation's own write
// (it can affect systems this platform doesn't even own if scope enforcement
// has any gap), so it gets no looser a gate than remediation has.
type SecurityEngagementConfig struct {
	ProposalTTL time.Duration
	AuthToken   string
	Env         string // "dev" | "prod"; an empty AuthToken is honoured only when this is "dev"
}

// checkSecurityEngagementAuth validates the bearer token on approve/reject —
// identical fail-closed-outside-dev logic to checkRemediationAuth
// (remediation.go), applied here from day one rather than starting open and
// needing an amendment later the way REMEDIATION_AUTH_TOKEN/EVAL_AUTH_TOKEN
// both did (ROADMAP OPS-2/OPS-3).
func (h *Handler) checkSecurityEngagementAuth(r *http.Request) bool {
	if h.securityEngagementConfig.AuthToken == "" {
		env := strings.ToLower(strings.TrimSpace(h.securityEngagementConfig.Env))
		return env == "dev" || env == ""
	}
	const prefix = "Bearer "
	auth := r.Header.Get("Authorization")
	if !strings.HasPrefix(auth, prefix) {
		return false
	}
	token := strings.TrimPrefix(auth, prefix)
	return subtle.ConstantTimeCompare([]byte(token), []byte(h.securityEngagementConfig.AuthToken)) == 1
}

// checkIDToTechnique maps a Phase 1 check to the Phase 2+ technique that can
// verify it — only checks with an entry here can have an engagement
// requested against them. Every technique listed is phase 2 today; a future
// phase 3/4 technique gets its own entries plus its own phase number where
// dispatchSecurityEngagement branches on it.
var checkIDToTechnique = map[string]string{
	"ingress-missing-tls": "confirm-http-reachable",
}

const securityEngagementPhase2 = 2

// SecurityVerifierClient talks to the agentify-security-verifier service —
// the new network-isolated executor for active-verification techniques.
// Deliberately NOT AgentClient: dispatch here never goes through
// agentify-agent at all, not even the deterministic intent-routed hop
// remediation's own dispatch uses, per ADR 0033 ("a prompt-injected 'please
// verify this endpoint is exploitable' is a strictly worse outcome... it can
// affect systems this platform doesn't even own").
type SecurityVerifierClient struct {
	baseURL string
	token   string
	client  *http.Client
}

func NewSecurityVerifierClient(baseURL, token string) *SecurityVerifierClient {
	return &SecurityVerifierClient{
		baseURL: baseURL,
		token:   token,
		client:  &http.Client{Timeout: 15 * time.Second}, // phase 2's one technique is a single HTTP GET
	}
}

// VerifyResult is the verifier's response to one dispatched technique.
// Confirmed is a pointer so "the check ran but couldn't reach a definitive
// answer" (nil) stays distinguishable from a definitive true/false —
// CompleteSecurityEngagement relies on this to avoid marking a finding
// confirmed-live/refuted on an inconclusive result.
type VerifyResult struct {
	Confirmed *bool  `json:"confirmed"`
	Evidence  string `json:"evidence"`
}

// Verify dispatches one technique against one target host.
func (vc *SecurityVerifierClient) Verify(engagementID, technique, targetHost string) (*VerifyResult, error) {
	body, err := json.Marshal(map[string]interface{}{
		"engagement_id": engagementID,
		"technique":     technique,
		"target":        map[string]string{"host": targetHost},
	})
	if err != nil {
		return nil, fmt.Errorf("marshal verify request: %w", err)
	}
	req, err := http.NewRequest(http.MethodPost, vc.baseURL+"/verify", bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("build verify request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	if vc.token != "" {
		req.Header.Set("Authorization", "Bearer "+vc.token)
	}
	resp, err := vc.client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("verify request failed: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		b, _ := io.ReadAll(resp.Body)
		return nil, fmt.Errorf("security-verifier returned %d: %s", resp.StatusCode, string(b))
	}
	var out VerifyResult
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return nil, fmt.Errorf("decode verify response: %w", err)
	}
	return &out, nil
}

// securityEngagementCreateRequest is the payload for POST
// /admin/security-engagements — an operator, from the Security Posture
// panel, requesting verification of one specific open finding. No
// checkSecurityEngagementAuth gate here, same asymmetry
// HandleIncidentRespond/decideRemediation already have: producing a request
// makes no network calls, so it carries the console's own session trust;
// only approval (which dispatches) needs the stronger gate.
type securityEngagementCreateRequest struct {
	Namespace    string `json:"namespace"`
	CheckID      string `json:"check_id"`
	ResourceKind string `json:"resource_kind"`
	ResourceName string `json:"resource_name"`
}

// HandleSecurityEngagementCreate handles POST /admin/security-engagements.
func (h *Handler) HandleSecurityEngagementCreate(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if h.securityEngagementStore == nil || h.securityFindingsStore == nil {
		http.Error(w, "security engagement store not available", http.StatusServiceUnavailable)
		return
	}
	tenantID, clusterID, err := h.resolveTenantContext(r)
	if err != nil {
		h.logger.Warn("tenant resolution failed", "error", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}
	var req securityEngagementCreateRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "invalid JSON", http.StatusBadRequest)
		return
	}
	if req.Namespace == "" || req.CheckID == "" || req.ResourceKind == "" || req.ResourceName == "" {
		http.Error(w, "namespace, check_id, resource_kind, and resource_name are required", http.StatusBadRequest)
		return
	}
	technique, ok := checkIDToTechnique[req.CheckID]
	if !ok {
		http.Error(w, fmt.Sprintf("no active-verification technique mapped to check_id %q", req.CheckID), http.StatusBadRequest)
		return
	}
	finding, err := h.securityFindingsStore.GetSecurityFinding(r.Context(), tenantID, req.Namespace, req.CheckID, req.ResourceKind, req.ResourceName)
	if err != nil {
		http.Error(w, "target finding not found", http.StatusNotFound)
		return
	}
	if finding.Status == "resolved" {
		http.Error(w, "target finding is already resolved — nothing to verify", http.StatusConflict)
		return
	}
	if finding.TargetHost == "" {
		http.Error(w, "target finding has no target_host recorded — cannot dispatch this technique", http.StatusUnprocessableEntity)
		return
	}

	e := &pgstore.SecurityEngagement{
		ID: uuid.New().String(), TenantID: tenantID, ClusterID: clusterID,
		Phase: securityEngagementPhase2, Technique: technique,
		TargetNamespace: req.Namespace, TargetCheckID: req.CheckID,
		TargetResourceKind: req.ResourceKind, TargetResourceName: req.ResourceName,
		Scope: map[string]interface{}{"target_host": finding.TargetHost},
		RequestedBy: "admin-console", ExpiresAt: time.Now().Add(h.securityEngagementConfig.ProposalTTL),
	}
	if err := h.securityEngagementStore.CreateSecurityEngagement(r.Context(), e); err != nil {
		h.logger.Warn("failed to create security engagement", "error", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}
	got, err := h.securityEngagementStore.GetSecurityEngagement(r.Context(), tenantID, e.ID)
	if err != nil {
		writeJSON(w, http.StatusOK, map[string]string{"id": e.ID, "status": "pending"})
		return
	}
	writeJSON(w, http.StatusOK, securityEngagementToResponse(*got))
}

// HandleSecurityEngagementList handles GET /admin/security-engagements?status=pending
func (h *Handler) HandleSecurityEngagementList(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if h.securityEngagementStore == nil {
		writeJSON(w, http.StatusOK, []SecurityEngagementResponse{})
		return
	}
	tenantID, _, err := h.resolveTenantContext(r)
	if err != nil {
		h.logger.Warn("tenant resolution failed", "error", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}
	status := r.URL.Query().Get("status")
	engagements, err := h.securityEngagementStore.ListSecurityEngagements(r.Context(), tenantID, status, 100)
	if err != nil {
		h.logger.Warn("list security engagements failed", "error", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}
	out := make([]SecurityEngagementResponse, 0, len(engagements))
	for _, e := range engagements {
		out = append(out, securityEngagementToResponse(e))
	}
	writeJSON(w, http.StatusOK, out)
}

// HandleSecurityEngagementGet handles GET /admin/security-engagements/{id}
func (h *Handler) HandleSecurityEngagementGet(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if h.securityEngagementStore == nil {
		http.Error(w, "security engagement store not available", http.StatusServiceUnavailable)
		return
	}
	tenantID, _, err := h.resolveTenantContext(r)
	if err != nil {
		h.logger.Warn("tenant resolution failed", "error", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}
	id := r.PathValue("id")
	e, err := h.securityEngagementStore.GetSecurityEngagement(r.Context(), tenantID, id)
	if err != nil {
		http.Error(w, "not found", http.StatusNotFound)
		return
	}
	writeJSON(w, http.StatusOK, securityEngagementToResponse(*e))
}

// HandleSecurityEngagementApprove handles POST
// /admin/security-engagements/{id}/approve — the explicit human-confirmation
// step. No active check runs without this call.
func (h *Handler) HandleSecurityEngagementApprove(w http.ResponseWriter, r *http.Request) {
	h.decideSecurityEngagement(w, r, "approved")
}

// HandleSecurityEngagementReject handles POST
// /admin/security-engagements/{id}/reject. Rejecting is terminal and never
// dispatches the technique.
func (h *Handler) HandleSecurityEngagementReject(w http.ResponseWriter, r *http.Request) {
	h.decideSecurityEngagement(w, r, "rejected")
}

func (h *Handler) decideSecurityEngagement(w http.ResponseWriter, r *http.Request, decision string) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if !h.checkSecurityEngagementAuth(r) {
		http.Error(w, "unauthorized", http.StatusUnauthorized)
		return
	}
	if h.securityEngagementStore == nil {
		http.Error(w, "security engagement store not available", http.StatusServiceUnavailable)
		return
	}
	tenantID, _, err := h.resolveTenantContext(r)
	if err != nil {
		h.logger.Warn("tenant resolution failed", "error", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}
	id := r.PathValue("id")

	e, err := h.securityEngagementStore.GetSecurityEngagement(r.Context(), tenantID, id)
	if err != nil {
		http.Error(w, "not found", http.StatusNotFound)
		return
	}
	if e.Status != "pending" {
		writeJSON(w, http.StatusConflict, map[string]string{
			"status": e.Status, "message": "engagement already decided",
		})
		return
	}
	if time.Now().After(e.ExpiresAt) {
		_, _ = h.securityEngagementStore.DecideSecurityEngagement(r.Context(), tenantID, id, "expired", "")
		writeJSON(w, http.StatusGone, map[string]string{
			"status": "expired", "message": "engagement expired — request verification again against current findings before approving",
		})
		return
	}

	approvedBy := r.Header.Get("X-Remediation-Actor")
	if approvedBy == "" {
		approvedBy = "admin-console"
	}
	ok, err := h.securityEngagementStore.DecideSecurityEngagement(r.Context(), tenantID, id, decision, approvedBy)
	if err != nil {
		h.logger.Warn("decide security engagement failed", "id", id, "error", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}
	if !ok {
		writeJSON(w, http.StatusConflict, map[string]string{"message": "already decided"})
		return
	}
	if decision == "rejected" {
		writeJSON(w, http.StatusOK, map[string]string{"status": "rejected"})
		return
	}

	h.dispatchSecurityEngagement(r.Context(), tenantID, e)
	updated, gerr := h.securityEngagementStore.GetSecurityEngagement(r.Context(), tenantID, id)
	if gerr == nil && updated != nil {
		writeJSON(w, http.StatusOK, securityEngagementToResponse(*updated))
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "completed"})
}

// dispatchSecurityEngagement calls the isolated verifier service directly —
// agentify-agent has no role in this path at all. Never retries, never
// chains a second technique automatically, same "verify, don't chain"
// discipline ADR 0020 established for remediation.
func (h *Handler) dispatchSecurityEngagement(ctx context.Context, tenantID string, e *pgstore.SecurityEngagement) {
	if h.securityVerifier == nil {
		_ = h.securityEngagementStore.CompleteSecurityEngagement(ctx, tenantID, e.ID, "failed",
			map[string]interface{}{}, "security-verifier not configured", nil)
		return
	}
	targetHost, _ := e.Scope["target_host"].(string)
	result, err := h.securityVerifier.Verify(e.ID, e.Technique, targetHost)
	if err != nil {
		_ = h.securityEngagementStore.CompleteSecurityEngagement(ctx, tenantID, e.ID, "failed",
			map[string]interface{}{"error": err.Error()}, err.Error(), nil)
		return
	}
	resultMap := map[string]interface{}{"evidence": result.Evidence}
	if result.Confirmed != nil {
		resultMap["confirmed"] = *result.Confirmed
	}
	_ = h.securityEngagementStore.CompleteSecurityEngagement(ctx, tenantID, e.ID, "completed", resultMap, "", result.Confirmed)
}

// SecurityEngagementResponse is the API shape of a security engagement.
type SecurityEngagementResponse struct {
	ID                 string                 `json:"id"`
	Phase              int                    `json:"phase"`
	Technique          string                 `json:"technique"`
	TargetNamespace    string                 `json:"target_namespace"`
	TargetCheckID      string                 `json:"target_check_id"`
	TargetResourceKind string                 `json:"target_resource_kind"`
	TargetResourceName string                 `json:"target_resource_name"`
	Status             string                 `json:"status"`
	RequestedBy        string                 `json:"requested_by,omitempty"`
	ApprovedBy         string                 `json:"approved_by,omitempty"`
	CreatedAt          time.Time              `json:"created_at"`
	ExpiresAt          time.Time              `json:"expires_at"`
	DecidedAt          *time.Time             `json:"decided_at,omitempty"`
	CompletedAt        *time.Time             `json:"completed_at,omitempty"`
	Result             map[string]interface{} `json:"result,omitempty"`
	Error              string                 `json:"error,omitempty"`
}

func securityEngagementToResponse(e pgstore.SecurityEngagement) SecurityEngagementResponse {
	result := e.Result
	if result == nil {
		result = map[string]interface{}{}
	}
	return SecurityEngagementResponse{
		ID: e.ID, Phase: e.Phase, Technique: e.Technique,
		TargetNamespace: e.TargetNamespace, TargetCheckID: e.TargetCheckID,
		TargetResourceKind: e.TargetResourceKind, TargetResourceName: e.TargetResourceName,
		Status: e.Status, RequestedBy: e.RequestedBy, ApprovedBy: e.ApprovedBy,
		CreatedAt: e.CreatedAt, ExpiresAt: e.ExpiresAt,
		DecidedAt: e.DecidedAt, CompletedAt: e.CompletedAt,
		Result: result, Error: e.Error,
	}
}
