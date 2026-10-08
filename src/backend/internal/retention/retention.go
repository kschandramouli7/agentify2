// Package retention runs a table's retention janitor: a background ticker
// that deletes rows older than a configured window. It bounds table age, not
// ingestion rate — see ADR 0015 (the events table, its original and still
// primary use) for that distinction. Generalized to more than one table as
// of ROADMAP P27 phase 4 (caller cardinality) — the metric is now passed in
// per instance rather than hardcoded, so a second Janitor for a second table
// attributes its own deletions correctly instead of silently adding to the
// first table's count.
package retention

import (
	"context"
	"log/slog"
	"time"

	"github.com/prometheus/client_golang/prometheus"
)

// Purger deletes rows older than a cutoff and reports how many were removed.
// Implemented by the Postgres client, once per purgeable table.
type Purger interface {
	PurgeOlderThan(ctx context.Context, cutoff time.Time) (int64, error)
}

// Janitor periodically purges old rows from one table.
type Janitor struct {
	purger   Purger
	window   time.Duration
	interval time.Duration
	logger   *slog.Logger
	name     string
	metric   prometheus.Counter
}

// New builds a Janitor for one table. days<=0 disables retention (returns
// nil). name is a short label for log lines ("events",
// "service_dependency_callers", ...); metric is incremented by the count
// each purge removes.
func New(purger Purger, days, intervalMinutes int, logger *slog.Logger, name string, metric prometheus.Counter) *Janitor {
	if days <= 0 || purger == nil {
		return nil
	}
	if intervalMinutes <= 0 {
		intervalMinutes = 60
	}
	return &Janitor{
		purger:   purger,
		window:   time.Duration(days) * 24 * time.Hour,
		interval: time.Duration(intervalMinutes) * time.Minute,
		logger:   logger,
		name:     name,
		metric:   metric,
	}
}

// Run blocks, purging on each tick until ctx is cancelled. Intended for a
// goroutine. A nil receiver is a no-op (retention disabled).
func (j *Janitor) Run(ctx context.Context) {
	if j == nil {
		return
	}
	j.logger.Info(j.name+" retention janitor started",
		"window", j.window.String(), "interval", j.interval.String())

	// Purge once at startup so a long-idle process doesn't wait a full interval.
	j.purgeOnce(ctx)

	ticker := time.NewTicker(j.interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			j.logger.Info(j.name + " retention janitor stopping")
			return
		case <-ticker.C:
			j.purgeOnce(ctx)
		}
	}
}

func (j *Janitor) purgeOnce(ctx context.Context) {
	cutoff := time.Now().Add(-j.window)
	n, err := j.purger.PurgeOlderThan(ctx, cutoff)
	if err != nil {
		j.logger.Error(j.name+" retention purge failed", "error", err)
		return
	}
	if j.metric != nil {
		j.metric.Add(float64(n))
	}
	if n > 0 {
		j.logger.Info(j.name+" retention purge complete", "deleted", n, "older_than", cutoff.UTC().Format(time.RFC3339))
	}
}
