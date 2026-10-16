package api

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	pgstore "github.com/chan/agentify/backend/internal/storage/postgres"
)

// fakeChatStore implements ChatStore (ROADMAP P33, ADR 0044's new
// FindPriorTier1Session method included) — just enough to drive
// HandleSendChatMessage's drift-wiring without a real database.
type fakeChatStore struct {
	session *pgstore.ChatSession
	prior   *pgstore.ChatSession // what FindPriorTier1Session returns
	updated *pgstore.ChatSession // captured from the last UpdateChatSession call
}

func (f *fakeChatStore) CreateChatSession(ctx context.Context, s *pgstore.ChatSession) error {
	return nil
}
func (f *fakeChatStore) GetChatSession(ctx context.Context, id string) (*pgstore.ChatSession, error) {
	return f.session, nil
}
func (f *fakeChatStore) UpdateChatSession(ctx context.Context, s *pgstore.ChatSession) error {
	f.updated = s
	return nil
}
func (f *fakeChatStore) ListChatSessions(ctx context.Context, limit int) ([]pgstore.ChatSession, error) {
	return nil, nil
}
func (f *fakeChatStore) DeleteChatSession(ctx context.Context, id string) error { return nil }
func (f *fakeChatStore) FindPriorTier1Session(ctx context.Context, excludeSessionID, intent, scopeKey string) (*pgstore.ChatSession, error) {
	return f.prior, nil
}

// TestHandleSendChatMessage_AddsDriftWhenAPriorMatchingSessionExists pins
// ROADMAP P33 (ADR 0044)'s Tier A wiring end to end: the agent answers
// deterministically (intent+scope_key tagged), a prior session answered
// the exact same scope with one fewer edge, and the new message's
// details must carry a "drift" section reporting the added edge.
func TestHandleSendChatMessage_AddsDriftWhenAPriorMatchingSessionExists(t *testing.T) {
	t0 := time.Now().Add(-48 * time.Hour).Format(time.RFC3339Nano)
	tNow := time.Now().Format(time.RFC3339Nano)

	priorEdge := map[string]interface{}{
		"id": "e1", "namespace": "payments", "from_service": "checkout-ui", "to_service": "checkout-api",
		"evidence_count": 5, "first_seen": t0, "last_seen": t0,
	}
	prior := &pgstore.ChatSession{
		ID: "prior-session",
		Messages: []pgstore.ChatMessage{
			{
				Role: "assistant", CreatedAt: time.Now().Add(-48 * time.Hour),
				Details: map[string]interface{}{
					"intent": "dependencies", "scope_key": "payments|",
					"service_graph": map[string]interface{}{
						"namespace": "payments", "focus": nil,
						"dependencies": []interface{}{priorEdge},
					},
				},
			},
		},
	}

	// e1 still exists but has fresh evidence (last_seen advanced) -- it
	// must report NEITHER added nor gone-quiet, only e2 (genuinely new)
	// should show up.
	freshE1 := map[string]interface{}{
		"id": "e1", "namespace": "payments", "from_service": "checkout-ui", "to_service": "checkout-api",
		"evidence_count": 9, "first_seen": t0, "last_seen": tNow,
	}
	newEdge := map[string]interface{}{
		"id": "e2", "namespace": "payments", "from_service": "checkout-api", "to_service": "billing",
		"evidence_count": 1, "first_seen": tNow, "last_seen": tNow,
	}
	agent := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		resp := map[string]interface{}{
			"answer": "checkout-ui depends on checkout-api, which depends on billing.",
			"status": "ok", "confidence": 1.0, "sources": []string{"service_dependencies"},
			"tier": "tier1",
			"details": map[string]interface{}{
				"intent": "dependencies", "scope_key": "payments|",
				"service_graph": map[string]interface{}{
					"namespace": "payments", "focus": nil,
					"dependencies": []interface{}{freshE1, newEdge},
				},
			},
		}
		json.NewEncoder(w).Encode(resp)
	}))
	defer agent.Close()

	store := &fakeChatStore{
		session: &pgstore.ChatSession{ID: "current-session", Namespace: "payments", Messages: nil},
		prior:   prior,
	}
	h := &Handler{
		agentClient: NewAgentClient(agent.URL),
		chatStore:   store,
		logger:      slog.New(slog.NewTextHandler(io.Discard, nil)),
	}

	body := `{"content":"what does checkout-ui depend on?"}`
	req := httptest.NewRequest(http.MethodPost, "/api/chat/sessions/current-session/messages", bytes.NewReader([]byte(body)))
	req.SetPathValue("id", "current-session")
	rec := httptest.NewRecorder()
	h.HandleSendChatMessage(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, body = %s", rec.Code, rec.Body.String())
	}
	var resp struct {
		Message pgstore.ChatMessage `json:"message"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal response: %v", err)
	}

	drift, ok := resp.Message.Details["drift"].(map[string]interface{})
	if !ok {
		t.Fatalf("want details.drift present, got %+v", resp.Message.Details)
	}
	if drift["prior_session_id"] != "prior-session" {
		t.Errorf("want prior_session_id = prior-session, got %v", drift["prior_session_id"])
	}
	added, _ := drift["edges_added"].([]interface{})
	if len(added) != 1 {
		t.Fatalf("want 1 added edge, got %+v", drift["edges_added"])
	}
	addedEdge, _ := added[0].(map[string]interface{})
	if addedEdge["id"] != "e2" {
		t.Errorf("want e2 reported as added, got %v", addedEdge["id"])
	}
	if _, goneQuiet := drift["edges_gone_quiet"]; goneQuiet {
		t.Errorf("e1's last_seen advanced (new evidence arrived), must not be reported gone-quiet: %+v", drift)
	}

	// Also confirm it's what actually got PERSISTED, not just echoed in
	// the HTTP response.
	if store.updated == nil || len(store.updated.Messages) == 0 {
		t.Fatal("UpdateChatSession was never called with the assistant message")
	}
	lastStored := store.updated.Messages[len(store.updated.Messages)-1]
	if _, ok := lastStored.Details["drift"]; !ok {
		t.Error("the stored message must carry the drift too, not just the HTTP response")
	}
}

// TestHandleSendChatMessage_NoDriftWhenAnswerIsNotTier1Tagged confirms the
// drift lookup is skipped entirely (not called with empty strings) for a
// model-answered turn, which never carries intent/scope_key.
func TestHandleSendChatMessage_NoDriftWhenAnswerIsNotTier1Tagged(t *testing.T) {
	agent := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]interface{}{
			"answer": "payment-api looks healthy.", "status": "ok", "confidence": 0.9,
			"sources": []string{}, "tier": "tier2", "details": map[string]interface{}{},
		})
	}))
	defer agent.Close()

	store := &fakeChatStore{
		session: &pgstore.ChatSession{ID: "current-session", Namespace: "payments"},
		prior:   nil,
	}
	h := &Handler{
		agentClient: NewAgentClient(agent.URL),
		chatStore:   store,
		logger:      slog.New(slog.NewTextHandler(io.Discard, nil)),
	}

	body := `{"content":"is payment-api healthy?"}`
	req := httptest.NewRequest(http.MethodPost, "/api/chat/sessions/current-session/messages", bytes.NewReader([]byte(body)))
	req.SetPathValue("id", "current-session")
	rec := httptest.NewRecorder()
	h.HandleSendChatMessage(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, body = %s", rec.Code, rec.Body.String())
	}
	var resp struct {
		Message pgstore.ChatMessage `json:"message"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal response: %v", err)
	}
	if _, ok := resp.Message.Details["drift"]; ok {
		t.Errorf("want no drift key on a non-Tier-1 answer, got %+v", resp.Message.Details)
	}
}
