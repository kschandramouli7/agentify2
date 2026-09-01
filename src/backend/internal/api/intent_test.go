package api

import "testing"

// TestInferIntent_DiagnoseWinsFirst locks spec 005's routing fix: diagnostic
// phrasing must resolve to "diagnose" (Tier-2 fan-out) even when it also contains
// health/cert keywords, so it never drops into the single-signal Tier-1 path.
func TestInferIntent_DiagnoseWinsFirst(t *testing.T) {
	cases := []struct {
		question string
		want     string
	}{
		{"why is payment unhealthy?", "diagnose"},
		{"what's wrong with the checkout service?", "diagnose"},
		{"root cause of the api outage", "diagnose"},
		{"investigate the cert errors", "diagnose"},
		{"diagnose payment", "diagnose"},
		{"the service is broken", "diagnose"},
		// Plain single-signal questions stay on their fast paths.
		{"is payment healthy?", "health_check"},
		{"when does the cert expire?", "cert_check"},
		{"show cpu metrics", "metrics_query"},
		{"list the pods", "general_query"},
	}
	for _, c := range cases {
		if got := inferIntent(c.question); got != c.want {
			t.Errorf("inferIntent(%q) = %q, want %q", c.question, got, c.want)
		}
	}
}

// TestInferIntent_Dependencies locks the routing rule for ROADMAP P18 use
// case #2's deterministic path (docs/SERVICE_DEPENDENCIES.md §1b).
//
// The rule has two halves, and the second is the one a well-meaning edit will
// break: a question routes to "dependencies" only when it asks about the call
// graph AND about nothing else. The deterministic answer holds no health, log,
// cert, metric or change data, so a mixed question answered from the graph
// alone would silently drop half of what was asked.
func TestInferIntent_Dependencies(t *testing.T) {
	cases := []struct {
		question string
		want     string
	}{
		// Pure graph questions — deterministic, tier1.
		{"what are the upstream and downstream dependencies for payment-api?", "dependencies"},
		{"who calls payment-api?", "dependencies"},
		{"what calls into payment?", "dependencies"},
		{"show me the call graph for payments", "dependencies"},
		{"which services depend on payment?", "dependencies"},
		{"list the callers and callees of payment", "dependencies"},
		{"what's the blast radius of payment-api going down", "dependencies"},
		{"what is impacted if payment-api stops", "dependencies"},

		// Regression: substring matching on "log" excluded "topo(log)y", so
		// these silently fell through. Word-boundary matching fixes it.
		{"service topology for payments", "dependencies"},
		{"what is the call graph topology of payments", "dependencies"},

		// Diagnostic phrasing wins, exactly as it does over health and cert.
		// The graph is context for a diagnosis, never the answer to one.
		{"why is payment-api slow, does it depend on vault?", "diagnose"},
		{"what's wrong with payment-api's dependencies?", "diagnose"},
		{"investigate what payment-api depends on", "diagnose"},

		// Mixed questions go to the branch owning the other domain, or fall
		// through to general_query — never to the graph-only answer.
		{"what are payment-api's dependencies and is it healthy?", "health_check"},
		{"do payment-api's dependencies have expiring certs?", "cert_check"},
		{"what changed in payment-api's dependencies recently?", "change_history"},
		{"dependencies of payment-api - also show me its logs", "general_query"},
		{"what depends on payment-api and should I restart it?", "general_query"},
		{"is payment-api's dependency on payment causing the timeout?", "general_query"},
		{"show cpu for payment-api's upstream services", "metrics_query"},

		// Not graph questions at all.
		{"is payment healthy?", "health_check"},
		{"list the pods", "general_query"},
	}
	for _, c := range cases {
		if got := inferIntent(c.question); got != c.want {
			t.Errorf("inferIntent(%q) = %q, want %q", c.question, got, c.want)
		}
	}
}

// TestIsDependencyQuestion_RequiresBothHalves guards the helper directly, so a
// change that drops either half fails here with a clear message rather than as
// a confusing intent mismatch.
func TestIsDependencyQuestion_RequiresBothHalves(t *testing.T) {
	if isDependencyQuestion("how many pods are running?") {
		t.Error("a question with no graph keyword must not be a dependency question")
	}
	if !isDependencyQuestion("what are the dependencies of payment-api") {
		t.Error("a pure graph question must be a dependency question")
	}
	if isDependencyQuestion("dependencies of payment-api and its cpu usage") {
		t.Error("a graph question mixed with another domain must be excluded")
	}
	// The stem must be anchored at a word boundary: "log" inside "topology" is
	// not a question about logs.
	if !isDependencyQuestion("service topology for payments") {
		t.Error(`"topology" must not be excluded by the "log" stem`)
	}
	if isDependencyQuestion("dependencies of payment-api, show its logs") {
		t.Error(`"logs" as its own word must still exclude`)
	}
}
