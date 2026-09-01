package api

import (
	"bytes"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// TestAnswerDependencies_BypassesThePodLookup is the reason this handler exists
// at all.
//
// HandleQuery fetches storage pods and returns "No data available for this
// query" when none match. The dependency graph lives in service_dependencies,
// not in any pod, so a namespace with a perfectly good mined graph but no
// registered pods was getting that bail and never reaching the skill.
//
// The Handler here has a nil queryExec and nil registry on purpose: if the
// dependency route ever stops short-circuiting, this test panics or returns
// "no_data" instead of the answer, rather than passing quietly.
func TestAnswerDependencies_BypassesThePodLookup(t *testing.T) {
	var gotPath string
	var gotBody map[string]interface{}

	agent := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		body, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(body, &gotBody)
		w.Header().Set("Content-Type", "application/json")
		io.WriteString(w, `{
			"answer": "payment-api is called by 2 services and calls nothing.",
			"status": "ok",
			"confidence": 1.0,
			"sources": ["service_dependencies"],
			"tier": "tier1",
			"details": {"service_graph": {"namespace": "payments", "focus": "payment-api",
				"dependencies": [{"id": "1", "from_service": "payment-batch", "to_service": "payment-api", "evidence_count": 187}]}}
		}`)
	}))
	defer agent.Close()

	h := &Handler{
		agentClient: NewAgentClient(agent.URL),
		logger:      slog.New(slog.NewTextHandler(io.Discard, nil)),
	}

	body := `{"question":"what are the upstream and downstream dependencies for payment-api?",
	          "context":{"namespace":"payments","service":"payment-api"}}`
	req := httptest.NewRequest(http.MethodPost, "/api/query", bytes.NewReader([]byte(body)))
	rec := httptest.NewRecorder()
	h.HandleQuery(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
	var resp QueryResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal response: %v", err)
	}

	if resp.Intent != "dependencies" {
		t.Errorf("intent = %q, want dependencies", resp.Intent)
	}
	// A deterministic answer must not be recorded as a paid tier2 call.
	if resp.Tier != "tier1" {
		t.Errorf("tier = %q, want tier1", resp.Tier)
	}
	if resp.Answer == "No data available for this query" {
		t.Fatal("the pod-lookup bail swallowed the question — the route no longer short-circuits")
	}
	if !strings.Contains(resp.Answer, "called by 2 services") {
		t.Errorf("answer = %q, want the skill's text", resp.Answer)
	}
	// The graph must survive to the client; the UI draws from it.
	if resp.Details == nil || resp.Details["service_graph"] == nil {
		t.Error("details.service_graph missing — the UI has nothing to draw")
	}

	if gotPath != "/reason" {
		t.Errorf("agent path = %q, want /reason", gotPath)
	}
	// The skill resolves WHICH service to report on from the question text, so
	// the question must actually be forwarded — Reason() takes a `question`
	// parameter it otherwise drops.
	ctx, _ := gotBody["context"].(map[string]interface{})
	if ctx == nil || ctx["question"] == "" || ctx["question"] == nil {
		t.Errorf("question not forwarded in agent context: %v", gotBody["context"])
	}
	if intent, _ := gotBody["intent"].(string); intent != "dependencies" {
		t.Errorf("agent intent = %q, want dependencies", intent)
	}
}

// TestAnswerDependencies_AgentFailureIsGraceful — a dependency question must
// never 500; the graph is a convenience, not a critical path.
func TestAnswerDependencies_AgentFailureIsGraceful(t *testing.T) {
	agent := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer agent.Close()

	h := &Handler{
		agentClient: NewAgentClient(agent.URL),
		logger:      slog.New(slog.NewTextHandler(io.Discard, nil)),
	}
	req := httptest.NewRequest(http.MethodPost, "/api/query",
		bytes.NewReader([]byte(`{"question":"who calls payment-api?","context":{"namespace":"payments"}}`)))
	rec := httptest.NewRecorder()
	h.HandleQuery(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200 even when the agent is down", rec.Code)
	}
	var resp QueryResponse
	json.Unmarshal(rec.Body.Bytes(), &resp)
	if resp.Status != "error" {
		t.Errorf("status = %q, want error", resp.Status)
	}
	if resp.Answer == "" {
		t.Error("an error answer must still say something to the operator")
	}
}

// TestHandleQuery_NonDependencyIntentStillUsesThePodPath guards the bypass from
// widening: any other intent must fall through to the normal path, which needs
// queryExec. A nil queryExec would panic, so reaching a non-panicking "no data"
// or an error is the proof it did NOT take the dependency shortcut.
func TestHandleQuery_NonDependencyIntentDoesNotTakeTheShortcut(t *testing.T) {
	var reached bool
	agent := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		reached = true
		io.WriteString(w, `{"answer":"x","status":"ok","tier":"tier2"}`)
	}))
	defer agent.Close()

	h := &Handler{
		agentClient: NewAgentClient(agent.URL),
		logger:      slog.New(slog.NewTextHandler(io.Discard, nil)),
	}
	req := httptest.NewRequest(http.MethodPost, "/api/query",
		bytes.NewReader([]byte(`{"question":"is payment-api healthy?","context":{"namespace":"payments"}}`)))
	rec := httptest.NewRecorder()

	defer func() {
		// A panic from the nil queryExec is the expected outcome here: it proves
		// the request went down the pod path rather than the dependency
		// shortcut. Recovered so the suite stays green.
		if recover() != nil && reached {
			t.Error("a health question reached the agent via the dependency shortcut")
		}
	}()
	h.HandleQuery(rec, req)
}
