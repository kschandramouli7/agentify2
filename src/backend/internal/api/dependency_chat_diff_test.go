package api

import (
	"testing"
	"time"

	pgstore "github.com/chan/agentify/backend/internal/storage/postgres"
)

func msgWithDetails(intent, scopeKey string, createdAt time.Time, details map[string]interface{}) pgstore.ChatMessage {
	d := map[string]interface{}{"intent": intent, "scope_key": scopeKey}
	for k, v := range details {
		d[k] = v
	}
	return pgstore.ChatMessage{Role: "assistant", CreatedAt: createdAt, Details: d}
}

func serviceGraphDetails(edges []map[string]interface{}) map[string]interface{} {
	return map[string]interface{}{
		"service_graph": map[string]interface{}{
			"namespace": "payments", "focus": nil, "dependencies": edges,
		},
	}
}

func edge(id, from, to string, firstSeen, lastSeen time.Time) map[string]interface{} {
	// RFC3339Nano, not RFC3339 -- a second-precision format would lose
	// the sub-second component a real time.Time carries, making an
	// otherwise-identical timestamp compare unequal after the
	// marshal/unmarshal round trip extractEdges performs. Real
	// production JSON (Go's own time.Time MarshalJSON) always includes
	// fractional seconds, so this matches that, not an artificial
	// precision loss this test would otherwise introduce on its own.
	return map[string]interface{}{
		"id": id, "namespace": "payments", "from_service": from, "to_service": to,
		"evidence_count": 10, "first_seen": firstSeen.Format(time.RFC3339Nano), "last_seen": lastSeen.Format(time.RFC3339Nano),
	}
}

// ── DiffTier1Answer / diffServiceGraph ───────────────────────────────────────

func TestDiffTier1Answer_NoPriorSessionIsNil(t *testing.T) {
	if got := DiffTier1Answer(nil, "dependencies", "payments|", serviceGraphDetails(nil)); got != nil {
		t.Fatalf("want nil with no prior session, got %+v", got)
	}
}

func TestDiffTier1Answer_PriorSessionWithNoMatchingMessageIsNil(t *testing.T) {
	prior := &pgstore.ChatSession{
		ID: "s1",
		Messages: []pgstore.ChatMessage{
			msgWithDetails("dependencies", "payments|payment-api", time.Now(), serviceGraphDetails(nil)),
		},
	}
	// Different scope_key -- must not match.
	got := DiffTier1Answer(prior, "dependencies", "payments|payment-worker", serviceGraphDetails(nil))
	if got != nil {
		t.Fatalf("want nil for a non-matching scope, got %+v", got)
	}
}

func TestDiffServiceGraph_NothingChangedIsNil(t *testing.T) {
	t0 := time.Now().Add(-48 * time.Hour)
	t1 := time.Now().Add(-1 * time.Hour)
	priorEdges := []map[string]interface{}{edge("e1", "checkout-ui", "checkout-api", t0, t1)}
	prior := &pgstore.ChatSession{
		ID: "s1",
		Messages: []pgstore.ChatMessage{
			msgWithDetails("dependencies", "payments|", t0, serviceGraphDetails(priorEdges)),
		},
	}
	// Fresh fetch: same edge, same last_seen (no new evidence, but also
	// not "gone quiet" since this IS the current last_seen, not a stale
	// one relative to NOW -- wait, gone-quiet is defined as "unchanged
	// since the PRIOR snapshot", which this IS. So this edge should be
	// reported as gone quiet below. This test covers the TRULY unchanged
	// case: identical edge set, identical timestamps -- same inputs as
	// the gone-quiet test, so see that one for the actual assertion;
	// this one exists to document that an edge present with an ADVANCED
	// last_seen reports nothing.
	freshEdges := []map[string]interface{}{edge("e1", "checkout-ui", "checkout-api", t0, time.Now())}
	got := DiffTier1Answer(prior, "dependencies", "payments|", serviceGraphDetails(freshEdges))
	if got != nil {
		t.Fatalf("want nil when the only edge's last_seen simply advanced (routine evidence, not a finding), got %+v", got)
	}
}

func TestDiffServiceGraph_AddedEdgeReportsItsOwnFirstSeen(t *testing.T) {
	t0 := time.Now().Add(-48 * time.Hour)
	prior := &pgstore.ChatSession{
		ID: "s1",
		// An explicit empty slice, not Go nil: the prior answer
		// genuinely found zero edges, a real comparable baseline --
		// not "no service_graph was ever recorded" (which extractEdges
		// treats as nothing to compare at all, by design).
		Messages: []pgstore.ChatMessage{
			msgWithDetails("dependencies", "payments|", t0, serviceGraphDetails([]map[string]interface{}{})),
		},
	}
	newEdgeFirstSeen := time.Now().Add(-2 * time.Hour)
	freshEdges := []map[string]interface{}{edge("e-new", "checkout-ui", "billing", newEdgeFirstSeen, newEdgeFirstSeen)}

	diff := DiffTier1Answer(prior, "dependencies", "payments|", serviceGraphDetails(freshEdges))
	if diff == nil {
		t.Fatal("want a diff, got nil")
	}
	if len(diff.EdgesAdded) != 1 {
		t.Fatalf("want 1 added edge, got %d", len(diff.EdgesAdded))
	}
	if diff.EdgesAdded[0].ID != "e-new" {
		t.Errorf("want e-new, got %q", diff.EdgesAdded[0].ID)
	}
	if !diff.EdgesAdded[0].ChangedAt.Equal(newEdgeFirstSeen) {
		t.Errorf("want ChangedAt = this edge's own first_seen (%v), got %v", newEdgeFirstSeen, diff.EdgesAdded[0].ChangedAt)
	}
	if diff.PriorSessionID != "s1" {
		t.Errorf("want prior_session_id s1, got %q", diff.PriorSessionID)
	}
	if !diff.PriorAskedAt.Equal(t0) {
		t.Errorf("want prior_asked_at %v, got %v", t0, diff.PriorAskedAt)
	}
}

func TestDiffServiceGraph_GoneQuietEdgeReportsItsOwnLastSeen(t *testing.T) {
	t0 := time.Now().Add(-48 * time.Hour)
	unchangedLastSeen := time.Now().Add(-30 * time.Hour) // hasn't moved since the prior snapshot
	priorEdges := []map[string]interface{}{edge("e1", "checkout-ui", "checkout-api", t0, unchangedLastSeen)}
	prior := &pgstore.ChatSession{
		ID: "s1",
		Messages: []pgstore.ChatMessage{
			msgWithDetails("dependencies", "payments|", t0, serviceGraphDetails(priorEdges)),
		},
	}
	// Fresh fetch: SAME edge, SAME last_seen -- no new evidence arrived
	// in the entire gap, which is exactly what "gone quiet" means here.
	freshEdges := []map[string]interface{}{edge("e1", "checkout-ui", "checkout-api", t0, unchangedLastSeen)}

	diff := DiffTier1Answer(prior, "dependencies", "payments|", serviceGraphDetails(freshEdges))
	if diff == nil {
		t.Fatal("want a diff, got nil")
	}
	if len(diff.EdgesGoneQuiet) != 1 {
		t.Fatalf("want 1 gone-quiet edge, got %d", len(diff.EdgesGoneQuiet))
	}
	if !diff.EdgesGoneQuiet[0].ChangedAt.Equal(unchangedLastSeen) {
		t.Errorf("want ChangedAt = last_seen (%v), got %v", unchangedLastSeen, diff.EdgesGoneQuiet[0].ChangedAt)
	}
}

func TestDiffServiceGraph_MalformedOrMissingServiceGraphIsNil(t *testing.T) {
	prior := &pgstore.ChatSession{
		ID: "s1",
		Messages: []pgstore.ChatMessage{
			msgWithDetails("dependencies", "payments|", time.Now(), map[string]interface{}{}), // no service_graph at all
		},
	}
	got := DiffTier1Answer(prior, "dependencies", "payments|", serviceGraphDetails(nil))
	if got != nil {
		t.Fatalf("want nil when the prior message has no comparable service_graph, got %+v", got)
	}
}

func TestDiffServiceGraph_UsesTheLastMatchingMessageNotTheFirst(t *testing.T) {
	// The same session asked about this scope twice; the SECOND (more
	// recent) answer is the correct "before" snapshot, not the first.
	t0 := time.Now().Add(-72 * time.Hour)
	t1 := time.Now().Add(-24 * time.Hour)
	firstAnswerEdges := []map[string]interface{}{edge("e-old", "a", "b", t0, t0)}
	secondAnswerEdges := []map[string]interface{}{edge("e-newer", "a", "c", t1, t1)}
	prior := &pgstore.ChatSession{
		ID: "s1",
		Messages: []pgstore.ChatMessage{
			msgWithDetails("dependencies", "payments|", t0, serviceGraphDetails(firstAnswerEdges)),
			msgWithDetails("dependencies", "payments|", t1, serviceGraphDetails(secondAnswerEdges)),
		},
	}
	// Fresh has BOTH e-old and e-newer -- against the correct (second)
	// baseline, only e-old should report as "added" (e-newer already existed).
	freshEdges := []map[string]interface{}{
		edge("e-old", "a", "b", t0, time.Now()),
		edge("e-newer", "a", "c", t1, t1),
	}
	diff := DiffTier1Answer(prior, "dependencies", "payments|", serviceGraphDetails(freshEdges))
	if diff == nil {
		t.Fatal("want a diff (e-old is new relative to the second answer), got nil")
	}
	if len(diff.EdgesAdded) != 1 || diff.EdgesAdded[0].ID != "e-old" {
		t.Errorf("want e-old reported as added (baseline = second answer), got %+v", diff.EdgesAdded)
	}
	if !diff.PriorAskedAt.Equal(t1) {
		t.Errorf("want PriorAskedAt = the SECOND message's timestamp (%v), got %v", t1, diff.PriorAskedAt)
	}
}

// ── diffColdServices ──────────────────────────────────────────────────────────

func coldServicesDetails(services []map[string]interface{}) map[string]interface{} {
	return map[string]interface{}{"namespace": "payments", "cold_services": services}
}

func TestDiffColdServices_NewlyColdAndNoLongerCold(t *testing.T) {
	lastSeen := time.Now().Add(-10 * 24 * time.Hour).Format(time.RFC3339)
	priorCold := []map[string]interface{}{{"service": "payment-legacy", "last_seen": lastSeen}}
	prior := &pgstore.ChatSession{
		ID: "s1",
		Messages: []pgstore.ChatMessage{
			msgWithDetails("cold_services", "payments", time.Now().Add(-48*time.Hour), coldServicesDetails(priorCold)),
		},
	}
	// payment-legacy warmed back up (gone); payment-v2 is newly cold.
	freshCold := []map[string]interface{}{{"service": "payment-v2", "last_seen": lastSeen}}

	diff := DiffTier1Answer(prior, "cold_services", "payments", coldServicesDetails(freshCold))
	if diff == nil {
		t.Fatal("want a diff, got nil")
	}
	if len(diff.ServicesNewlyCold) != 1 || diff.ServicesNewlyCold[0].Service != "payment-v2" {
		t.Errorf("want payment-v2 newly cold, got %+v", diff.ServicesNewlyCold)
	}
	if len(diff.ServicesNoLongerCold) != 1 || diff.ServicesNoLongerCold[0] != "payment-legacy" {
		t.Errorf("want payment-legacy no-longer-cold, got %+v", diff.ServicesNoLongerCold)
	}
}

func TestDiffColdServices_SameSetIsNil(t *testing.T) {
	cold := []map[string]interface{}{{"service": "payment-legacy", "last_seen": nil}}
	prior := &pgstore.ChatSession{
		ID: "s1",
		Messages: []pgstore.ChatMessage{
			msgWithDetails("cold_services", "payments", time.Now().Add(-48*time.Hour), coldServicesDetails(cold)),
		},
	}
	got := DiffTier1Answer(prior, "cold_services", "payments", coldServicesDetails(cold))
	if got != nil {
		t.Fatalf("want nil when the cold set is unchanged, got %+v", got)
	}
}
