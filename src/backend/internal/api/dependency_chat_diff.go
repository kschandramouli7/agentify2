package api

import (
	"encoding/json"
	"time"

	pgstore "github.com/chan/agentify/backend/internal/storage/postgres"
)

// ROADMAP P33 (ADR 0044) — Tier A: a deterministic, zero-LLM-cost diff
// against the most recent past chat session that answered this exact
// same route+scope. Structural change only in v1 (edges/cold-services
// added or removed, not evidence_count magnitude — see ADR 0044
// Decision #2/Consequences for why that's deferred, not omitted by
// oversight). Every finding carries its OWN timestamp, never one blanket
// "since we last asked" (Decision #4).

// EdgeDiffEntry pairs an edge with the one timestamp that explains why
// it's in the diff: first_seen for an added edge, last_seen for one gone
// quiet.
type EdgeDiffEntry struct {
	pgstore.ServiceDependency
	ChangedAt time.Time `json:"changed_at"`
}

// ColdServiceDiffEntry is cold_services' own diff unit — a service name
// plus, for a newly-cold entry, the last time any evidence for it
// arrived at all (mirrors fetch_cold_services' own row shape).
type ColdServiceDiffEntry struct {
	Service  string     `json:"service"`
	LastSeen *time.Time `json:"last_seen,omitempty"`
}

// GraphDiff is Tier A's result. Which pair of fields is populated
// depends entirely on which route produced it (the edge fields for
// `dependencies`/`label_scoped_dependencies`, the cold-service fields
// for `cold_services`) — never both, since the two families compare
// structurally different data.
type GraphDiff struct {
	PriorSessionID string    `json:"prior_session_id"`
	PriorAskedAt   time.Time `json:"prior_asked_at"`

	EdgesAdded     []EdgeDiffEntry `json:"edges_added,omitempty"`
	EdgesGoneQuiet []EdgeDiffEntry `json:"edges_gone_quiet,omitempty"`

	ServicesNewlyCold    []ColdServiceDiffEntry `json:"services_newly_cold,omitempty"`
	ServicesNoLongerCold []string               `json:"services_no_longer_cold,omitempty"`
}

// DiffTier1Answer compares a freshly-computed Tier-1 answer against the
// same-scope answer from `prior` (already resolved by
// FindPriorTier1Session), returning nil when there's nothing to report
// — no prior session, the prior session's last matching message carried
// nothing comparable, or nothing actually changed. Silence, never a
// fabricated "nothing changed" entry (spec 013's own stated behavior).
func DiffTier1Answer(prior *pgstore.ChatSession, intent, scopeKey string, freshDetails map[string]interface{}) *GraphDiff {
	if prior == nil {
		return nil
	}
	priorMsg := lastMatchingMessage(prior.Messages, intent, scopeKey)
	if priorMsg == nil {
		return nil
	}

	switch intent {
	case "dependencies", "label_scoped_dependencies":
		return diffServiceGraph(prior.ID, priorMsg, freshDetails)
	case "cold_services":
		return diffColdServices(prior.ID, priorMsg, freshDetails)
	default:
		return nil
	}
}

func diffServiceGraph(priorSessionID string, priorMsg *pgstore.ChatMessage, freshDetails map[string]interface{}) *GraphDiff {
	priorEdges := extractEdges(priorMsg.Details)
	freshEdges := extractEdges(freshDetails)
	if priorEdges == nil || freshEdges == nil {
		return nil
	}

	priorByID := make(map[string]pgstore.ServiceDependency, len(priorEdges))
	for _, e := range priorEdges {
		priorByID[e.ID] = e
	}

	diff := &GraphDiff{PriorSessionID: priorSessionID, PriorAskedAt: priorMsg.CreatedAt}
	for _, e := range freshEdges {
		p, existed := priorByID[e.ID]
		if !existed {
			diff.EdgesAdded = append(diff.EdgesAdded, EdgeDiffEntry{ServiceDependency: e, ChangedAt: e.FirstSeen})
			continue
		}
		// Gone quiet: existed before, and no new evidence has arrived
		// anywhere in the gap between the two conversations, however
		// long that gap was — deliberately NOT the live diagram's
		// 15-minute real-time staleness marker (DependencyFlow.tsx's
		// isStale), a different time scale for a different purpose.
		if e.LastSeen.Equal(p.LastSeen) {
			diff.EdgesGoneQuiet = append(diff.EdgesGoneQuiet, EdgeDiffEntry{ServiceDependency: e, ChangedAt: e.LastSeen})
		}
	}

	if len(diff.EdgesAdded) == 0 && len(diff.EdgesGoneQuiet) == 0 {
		return nil
	}
	return diff
}

// coldServiceRow mirrors fetch_cold_services'/ColdServicesSkill's own
// per-service shape ({"service": ..., "last_seen": ... | null}) closely
// enough to decode details["cold_services"] generically.
type coldServiceRow struct {
	Service  string  `json:"service"`
	LastSeen *string `json:"last_seen"`
}

func diffColdServices(priorSessionID string, priorMsg *pgstore.ChatMessage, freshDetails map[string]interface{}) *GraphDiff {
	priorCold := extractColdServices(priorMsg.Details)
	freshCold := extractColdServices(freshDetails)
	if priorCold == nil || freshCold == nil {
		return nil
	}

	priorNames := make(map[string]bool, len(priorCold))
	for _, s := range priorCold {
		priorNames[s.Service] = true
	}
	freshNames := make(map[string]bool, len(freshCold))

	diff := &GraphDiff{PriorSessionID: priorSessionID, PriorAskedAt: priorMsg.CreatedAt}
	for _, s := range freshCold {
		freshNames[s.Service] = true
		if priorNames[s.Service] {
			continue
		}
		var lastSeen *time.Time
		if s.LastSeen != nil {
			if t, err := time.Parse(time.RFC3339, *s.LastSeen); err == nil {
				lastSeen = &t
			}
		}
		diff.ServicesNewlyCold = append(diff.ServicesNewlyCold, ColdServiceDiffEntry{Service: s.Service, LastSeen: lastSeen})
	}
	for _, s := range priorCold {
		if !freshNames[s.Service] {
			diff.ServicesNoLongerCold = append(diff.ServicesNoLongerCold, s.Service)
		}
	}

	if len(diff.ServicesNewlyCold) == 0 && len(diff.ServicesNoLongerCold) == 0 {
		return nil
	}
	return diff
}

// lastMatchingMessage finds the LAST (most recent) assistant message in
// a session tagged with this exact intent+scopeKey — a session can ask
// about the same scope more than once across its own history, and the
// most recent one is the correct "before" snapshot.
func lastMatchingMessage(messages []pgstore.ChatMessage, intent, scopeKey string) *pgstore.ChatMessage {
	for i := len(messages) - 1; i >= 0; i-- {
		m := messages[i]
		if m.Details == nil {
			continue
		}
		if gotIntent, _ := m.Details["intent"].(string); gotIntent != intent {
			continue
		}
		if gotScope, _ := m.Details["scope_key"].(string); gotScope != scopeKey {
			continue
		}
		return &m
	}
	return nil
}

// extractEdges reads details["service_graph"]["dependencies"] into typed
// ServiceDependency structs. details arrives as map[string]interface{}
// (the agent's JSON response decoded generically), so a marshal/
// unmarshal round trip is the simplest safe way to re-type it — same
// convention used wherever else in this codebase a generic details map
// needs to become something stronger again. Returns nil (distinct from
// a real, legitimately empty slice) when the field is absent or
// malformed, so callers can tell "nothing to compare" from "compared,
// zero edges on both sides."
func extractEdges(details map[string]interface{}) []pgstore.ServiceDependency {
	if details == nil {
		return nil
	}
	graph, ok := details["service_graph"].(map[string]interface{})
	if !ok {
		return nil
	}
	raw, ok := graph["dependencies"]
	if !ok {
		return nil
	}
	b, err := json.Marshal(raw)
	if err != nil {
		return nil
	}
	var edges []pgstore.ServiceDependency
	if err := json.Unmarshal(b, &edges); err != nil {
		return nil
	}
	return edges
}

// extractColdServices reads details["cold_services"] the same way
// extractEdges reads the service graph — nil (not an empty slice) means
// "nothing to compare," not "compared, found none."
func extractColdServices(details map[string]interface{}) []coldServiceRow {
	if details == nil {
		return nil
	}
	raw, ok := details["cold_services"]
	if !ok {
		return nil
	}
	b, err := json.Marshal(raw)
	if err != nil {
		return nil
	}
	var rows []coldServiceRow
	if err := json.Unmarshal(b, &rows); err != nil {
		return nil
	}
	return rows
}
