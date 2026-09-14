package storage

import (
	"context"
	"fmt"
)

// Backend is the interface for storing and querying data in different store types.
// Implementations handle Postgres (relational), Redis (KV), Weaviate (vector), etc.
type Backend interface {
	// Store saves event data in the backend.
	Store(ctx context.Context, podID string, data map[string]interface{}) (string, error)

	// Query retrieves data matching the criteria. tenantID is server-resolved
	// (never taken from the client-supplied query map — see ROADMAP OPS-10 /
	// ADR 0022 amendment) and, on the kv (current_state) backend, sets the
	// RLS session variable a caller cannot spoof its way around. The
	// relational (events) backend accepts it for interface compliance but
	// does not yet enforce it — events has no RLS policy, a separate item.
	Query(ctx context.Context, tenantID, podID string, query map[string]interface{}) ([]map[string]interface{}, error)

	// HealthCheck verifies the backend is accessible.
	HealthCheck(ctx context.Context) error

	// Close closes the connection.
	Close() error
}

// BackendFactory maps store families to their backing store. For the MVP both
// "relational" and "kv" are Postgres-backed (ADR 0010); "vector" is unprovisioned.
type BackendFactory struct {
	relational Backend
	kv         Backend
	vector     Backend
}

// NewBackendFactory creates a new backend factory from per-family backends.
// Any may be nil (unprovisioned); GetBackend then errors for that family.
func NewBackendFactory(relational, kv, vector Backend) *BackendFactory {
	return &BackendFactory{
		relational: relational,
		kv:         kv,
		vector:     vector,
	}
}

// GetBackend returns a backend by store type.
// Returns an error if the store type is unknown or if the backend for a known
// store type was not connected (e.g. the dependency was unavailable at startup).
func (bf *BackendFactory) GetBackend(storeType string) (Backend, error) {
	var b Backend
	switch storeType {
	// "timeseries" and "logs" are the trait-derived store types for temporal data
	// (metric samples, event history). Per ADR 0013 they land in the same Postgres
	// events table as "relational" for v1 — no separate TSDB/log index yet.
	case "relational", "timeseries", "logs":
		b = bf.relational
	case "kv":
		b = bf.kv
	case "vector":
		b = bf.vector
	default:
		return nil, fmt.Errorf("unsupported store type: %s", storeType)
	}

	if b == nil {
		return nil, fmt.Errorf("backend for store type %q is not configured", storeType)
	}
	return b, nil
}

// Close closes the connected backends, returning the first error encountered.
// (relational and kv may share one connection pool — kv's Close is a no-op then.)
func (bf *BackendFactory) Close() error {
	var firstErr error
	for _, b := range []Backend{bf.relational, bf.kv, bf.vector} {
		if b == nil {
			continue
		}
		if err := b.Close(); err != nil && firstErr == nil {
			firstErr = err
		}
	}
	return firstErr
}
