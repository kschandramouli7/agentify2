package api

import (
	"context"
	"log/slog"
	"strings"
	"testing"
	"time"

	"github.com/chan/agentify/backend/internal/governance"
	"github.com/chan/agentify/backend/internal/models"
)

func TestDecideActions(t *testing.T) {
	now := time.Date(2026, 6, 6, 12, 0, 0, 0, time.UTC)
	cooldown := time.Hour

	t.Run("opens new anomalous namespace", func(t *testing.T) {
		toOpen, toClose, deferred := decideActions(
			map[string]nsAnomaly{"prod": {hasUnhealthy: true}},
			map[string]incident{}, map[string]time.Time{}, now, cooldown, 5)
		if len(toOpen) != 1 || toOpen[0] != "prod" || len(toClose) != 0 || deferred != 0 {
			t.Fatalf("got open=%v close=%v deferred=%d", toOpen, toClose, deferred)
		}
	})

	t.Run("does not reopen an already-open incident", func(t *testing.T) {
		toOpen, _, _ := decideActions(
			map[string]nsAnomaly{"prod": {}},
			map[string]incident{"prod": {openedAt: now}}, map[string]time.Time{}, now, cooldown, 5)
		if len(toOpen) != 0 {
			t.Fatalf("should not reopen, got %v", toOpen)
		}
	})

	t.Run("closes recovered namespace", func(t *testing.T) {
		_, toClose, _ := decideActions(
			map[string]nsAnomaly{}, // nothing anomalous now
			map[string]incident{"prod": {openedAt: now}}, map[string]time.Time{}, now, cooldown, 5)
		if len(toClose) != 1 || toClose[0] != "prod" {
			t.Fatalf("should close prod, got %v", toClose)
		}
	})

	t.Run("respects cooldown after recent action", func(t *testing.T) {
		toOpen, _, _ := decideActions(
			map[string]nsAnomaly{"prod": {}},
			map[string]incident{},
			map[string]time.Time{"prod": now.Add(-30 * time.Minute)}, // within 60m cooldown
			now, cooldown, 5)
		if len(toOpen) != 0 {
			t.Fatalf("cooldown should suppress reopen, got %v", toOpen)
		}
	})

	t.Run("enforces per-sweep cap and reports deferred", func(t *testing.T) {
		anom := map[string]nsAnomaly{"a": {}, "b": {}, "c": {}, "d": {}}
		toOpen, _, deferred := decideActions(anom, map[string]incident{}, map[string]time.Time{}, now, cooldown, 2)
		if len(toOpen) != 2 || deferred != 2 {
			t.Fatalf("cap not enforced: open=%v deferred=%d", toOpen, deferred)
		}
		// Deterministic selection (sorted): a, b.
		if toOpen[0] != "a" || toOpen[1] != "b" {
			t.Errorf("expected deterministic [a b], got %v", toOpen)
		}
	})
}

// --- stubs for the investigate→notify path ---

type stubFetcher struct{}

func (stubFetcher) RouteToPods(ctx context.Context, intent, ns, clusterID string) ([]*models.Pod, error) {
	return []*models.Pod{{ID: "k8fy.live-state." + ns, Kind: "leaf"}}, nil
}
func (stubFetcher) FetchFromPod(ctx context.Context, tenantID string, pod *models.Pod, q map[string]interface{}) ([]map[string]interface{}, error) {
	return []map[string]interface{}{{
		"event_namespace": "k8fy.live-state",
		"type":            "pod_modified",
		"payload": map[string]interface{}{
			"pod_id": "payment-bbb", "phase": "Running", "ready": false,
			"reason": "CrashLoopBackOff", "env": "SECRET_TOKEN=should-be-dropped",
		},
	}}, nil
}

type stubReasoner struct{ gotData map[string]interface{} }

func (s *stubReasoner) Reason(question, intent string, data, ctx map[string]interface{}, traceID string) (*AgentResponse, error) {
	s.gotData = data
	return &AgentResponse{
		Answer:  "payment crash looping; token leaked: password=hunter2",
		Sources: []string{"k8fy.live-state.prod", "k8fy.metrics"},
		Details: map[string]interface{}{
			"severity":        "critical",
			"likely_cause":    "v7 rollout",
			"recommendations": []interface{}{"roll back v7", "renew cert"},
		},
	}, nil
}

type captureNotifier struct{ last Alert }

func (c *captureNotifier) Send(ctx context.Context, a Alert) error { c.last = a; return nil }

func TestInvestigateAndNotify(t *testing.T) {
	reasoner := &stubReasoner{}
	notifier := &captureNotifier{}
	in := &Investigator{
		queryExec: stubFetcher{},
		reasoner:  reasoner,
		redactor:  governance.NewRedactor(true, false, slog.Default()),
		notifier:  notifier,
		logger:    slog.Default(),
		cfg:       InvestigationConfig{},
	}

	in.investigateAndNotify(context.Background(), "prod", nsAnomaly{reasons: []string{"pod payment-bbb: CrashLoopBackOff"}, hasUnhealthy: true})

	a := notifier.last
	if a.Namespace != "prod" || a.Severity != "critical" {
		t.Fatalf("alert basics wrong: %+v", a)
	}
	if a.Cause != "v7 rollout" || len(a.Actions) != 2 {
		t.Errorf("structured details not surfaced: cause=%q actions=%v", a.Cause, a.Actions)
	}
	// Outbound prose scrubbed (defense-in-depth on egress).
	if strings.Contains(a.Summary, "hunter2") {
		t.Errorf("secret leaked into alert summary: %q", a.Summary)
	}
	// The data the agent saw must have been allowlist-redacted (env dropped).
	rows, _ := reasoner.gotData["k8fy.live-state.prod"].([]map[string]interface{})
	if len(rows) != 1 {
		t.Fatalf("expected redacted rows, got %v", reasoner.gotData)
	}
	payload, _ := rows[0]["payload"].(map[string]interface{})
	if _, leaked := payload["env"]; leaked {
		t.Errorf("non-allowlisted field reached the agent: %v", payload)
	}
}

func TestFormatAlertText(t *testing.T) {
	open := formatAlertText(Alert{
		Namespace: "prod", Severity: "critical",
		Reasons: []string{"pod x: CrashLoopBackOff"},
		Summary: "it is broken", Cause: "v7", Actions: []string{"roll back"},
		Sources: []string{"k8fy.live-state.prod"}, TraceID: "t-1",
	})
	for _, want := range []string{"CRITICAL", "prod", "CrashLoopBackOff", "v7", "roll back", "t-1", "no action taken"} {
		if !strings.Contains(open, want) {
			t.Errorf("alert text missing %q:\n%s", want, open)
		}
	}

	resolved := formatAlertText(Alert{Namespace: "prod", Resolved: true, TraceID: "t-2"})
	if !strings.Contains(resolved, "RESOLVED") || !strings.Contains(resolved, "prod") {
		t.Errorf("resolved text wrong:\n%s", resolved)
	}
}
