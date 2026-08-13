import { useState } from "react";
import {
  Integration,
  IntegrationInput,
  listIntegrations,
  getIntegration,
  createIntegration,
  updateIntegration,
  deleteIntegration,
} from "../api";

// ── Helpers ───────────────────────────────────────────────────────────────────

async function fetchTrackedNamespaces(): Promise<string[]> {
  const res = await fetch("/admin/tracked");
  if (!res.ok) return [];
  const data = (await res.json()) as string[] | null;
  const entries = data ?? [];
  // Each entry is "namespace/service" — extract unique namespaces.
  const seen = new Set<string>();
  for (const entry of entries) {
    const ns = entry.split("/")[0];
    if (ns) seen.add(ns);
  }
  return [...seen].sort();
}

// ── Status badge ──────────────────────────────────────────────────────────────

function StatusBadge({ status }: { status: string }) {
  const cls =
    status === "active"   ? "int-badge int-badge--active"
    : status === "error"  ? "int-badge int-badge--error"
    : "int-badge int-badge--inactive";
  return <span className={cls}>{status}</span>;
}

// ── Empty state ───────────────────────────────────────────────────────────────

function EmptyState({ onAdd }: { onAdd: () => void }) {
  return (
    <div className="int-empty">
      <p>No integrations configured yet.</p>
      <button className="int-btn int-btn--primary" onClick={onAdd}>
        Add first integration
      </button>
    </div>
  );
}

// ── Namespaces field (checkboxes over discovered + manual add) ───────────────

function NamespacesField({
  value,
  discovered,
  onChange,
}: {
  value: string[];
  discovered: string[];
  onChange: (namespaces: string[]) => void;
}) {
  const [manualEntry, setManualEntry] = useState("");
  const known = [...new Set([...discovered, ...value])].sort();

  function toggle(ns: string) {
    onChange(value.includes(ns) ? value.filter(n => n !== ns) : [...value, ns]);
  }

  function addManual() {
    const ns = manualEntry.trim();
    if (ns && !value.includes(ns)) onChange([...value, ns]);
    setManualEntry("");
  }

  return (
    <label className="int-field">
      <span>Namespaces</span>
      <div className="int-ns-checkboxes">
        {known.length === 0 && <em>No namespaces discovered yet — add one manually below.</em>}
        {known.map(ns => (
          <label key={ns} className="int-ns-checkbox">
            <input type="checkbox" checked={value.includes(ns)} onChange={() => toggle(ns)} />
            {ns}
          </label>
        ))}
      </div>
      <div className="int-ns-manual">
        <input
          type="text"
          value={manualEntry}
          onChange={e => setManualEntry(e.target.value)}
          placeholder="add namespace not yet discovered"
          onKeyDown={e => { if (e.key === "Enter") { e.preventDefault(); addManual(); } }}
        />
        <button type="button" className="int-btn int-btn--sm" onClick={addManual}>Add</button>
      </div>
      <em>
        Namespaces this integration is responsible for. If a fleet collector
        (agentify-discovery) is reporting for this cluster, it refreshes this
        list automatically on its own schedule — saving this form afterward
        will overwrite whatever it last pushed with your selection here.
      </em>
    </label>
  );
}

// ── Integration form (create + edit) ─────────────────────────────────────────

const BLANK: IntegrationInput = { name: "", namespaces: [], token: "", status: "inactive" };

function IntegrationForm({
  initial,
  discoveredNamespaces,
  onSave,
  onCancel,
  saving,
}: {
  initial?: Integration;
  discoveredNamespaces: string[];
  onSave: (input: IntegrationInput) => void;
  onCancel: () => void;
  saving: boolean;
}) {
  const [form, setForm] = useState<IntegrationInput>(
    initial
      ? {
          name: initial.name, namespaces: initial.namespaces, token: "",
          status: initial.status,
        }
      : BLANK
  );

  function set<K extends keyof IntegrationInput>(k: K, v: IntegrationInput[K]) {
    setForm(f => ({ ...f, [k]: v }));
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    onSave(form);
  }

  return (
    <form className="int-form" onSubmit={handleSubmit}>
      <label className="int-field">
        <span>Name *</span>
        <input
          type="text"
          value={form.name}
          onChange={e => set("name", e.target.value)}
          placeholder="Production cluster"
          required
        />
      </label>
      <NamespacesField
        value={form.namespaces}
        discovered={discoveredNamespaces}
        onChange={ns => set("namespaces", ns)}
      />
      <label className="int-field">
        <span>Bearer token {initial?.has_token && <em>(leave blank to keep existing)</em>}</span>
        <input
          type="password"
          value={form.token}
          onChange={e => set("token", e.target.value)}
          placeholder={initial?.has_token ? "••••••••" : "optional"}
          autoComplete="new-password"
        />
      </label>
      {initial && (
        <label className="int-field">
          <span>Status</span>
          <select value={form.status} onChange={e => set("status", e.target.value)}>
            <option value="inactive">inactive</option>
            <option value="active">active</option>
            <option value="error">error</option>
          </select>
        </label>
      )}
      <div className="int-form__actions">
        <button type="submit" className="int-btn int-btn--primary" disabled={saving}>
          {saving ? "Saving…" : initial ? "Save changes" : "Create"}
        </button>
        <button type="button" className="int-btn" onClick={onCancel} disabled={saving}>
          Cancel
        </button>
      </div>
    </form>
  );
}

// ── Integration row ───────────────────────────────────────────────────────────

function IntegrationRow({
  integration,
  discoveredNamespaces,
  onEdit,
  onDelete,
}: {
  integration: Integration;
  discoveredNamespaces: string[];
  onEdit: (i: Integration) => void;
  onDelete: (id: string) => void;
}) {
  return (
    <div className="int-row">
      <div className="int-row__main">
        <span className="int-row__name">{integration.name}</span>
        <StatusBadge status={integration.status} />
        {integration.has_token && (
          <span className="int-row__token-dot" title="bearer token configured">🔑</span>
        )}
      </div>
      <div className="int-row__watching">
        <span className="int-row__watching-label">
          {integration.namespaces.length > 0 ? "Namespaces:" : "No namespaces assigned yet"}
        </span>
        {integration.namespaces.map(ns => (
          <code key={ns} className="int-ns-chip">{ns}</code>
        ))}
      </div>
      {discoveredNamespaces.length > 0 && (
        <div className="int-row__watching int-row__watching--discovered">
          <span className="int-row__watching-label">Discovered by agentify-discovery:</span>
          {discoveredNamespaces.map(ns => (
            <code key={ns} className="int-ns-chip int-ns-chip--muted">{ns}</code>
          ))}
        </div>
      )}
      <div className="int-row__actions">
        <button className="int-btn int-btn--sm" onClick={() => onEdit(integration)}>Edit</button>
        <button className="int-btn int-btn--sm int-btn--danger" onClick={() => onDelete(integration.id)}>Delete</button>
      </div>
    </div>
  );
}

// ── Main panel ────────────────────────────────────────────────────────────────

type Mode = "list" | "create" | { editing: Integration };

export function IntegrationsPanel() {
  const [integrations, setIntegrations] = useState<Integration[] | null>(null);
  const [discoveredNamespaces, setDiscoveredNamespaces] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [mode, setMode] = useState<Mode>("list");

  // Load integrations + discovered namespaces together on first render.
  if (integrations === null && !loading && !error) {
    setLoading(true);
    Promise.all([listIntegrations(), fetchTrackedNamespaces()])
      .then(([data, ns]) => {
        setIntegrations(data);
        setDiscoveredNamespaces(ns);
        setLoading(false);
      })
      .catch(e => { setError(String(e)); setLoading(false); });
  }

  // Re-fetch this one integration right before editing rather than reusing
  // the (possibly stale) copy from the last list load — a fleet collector's
  // background inventory push (ROADMAP P18 use case #1) can update
  // Namespaces at any time, and the edit form's full-row PUT would otherwise
  // silently overwrite whatever it last pushed with a stale snapshot. This
  // narrows the race window; it doesn't eliminate it (the admin can still
  // take a while to fill out the form) — see the NamespacesField note.
  async function handleEdit(item: Integration) {
    setError(null);
    try {
      const fresh = await getIntegration(item.id);
      setIntegrations(prev => prev?.map(i => i.id === fresh.id ? fresh : i) ?? prev);
      setMode({ editing: fresh });
    } catch {
      // Refetch failed (e.g. transient network blip) — fall back to the
      // list's cached copy rather than blocking the edit entirely.
      setMode({ editing: item });
    }
  }

  async function handleSave(input: IntegrationInput) {
    setSaving(true);
    setError(null);
    try {
      if (mode === "create") {
        const created = await createIntegration(input);
        setIntegrations(prev => [...(prev ?? []), created]);
      } else if (typeof mode === "object") {
        const updated = await updateIntegration(mode.editing.id, input);
        setIntegrations(prev => prev?.map(i => i.id === updated.id ? updated : i) ?? []);
      }
      setMode("list");
    } catch (e) {
      setError(`Save failed: ${e}`);
    } finally {
      setSaving(false);
    }
  }

  async function handleDelete(id: string) {
    if (!confirm("Delete this integration? This cannot be undone.")) return;
    setError(null);
    try {
      await deleteIntegration(id);
      setIntegrations(prev => prev?.filter(i => i.id !== id) ?? []);
    } catch (e) {
      setError(`Delete failed: ${e}`);
    }
  }

  const list = integrations ?? [];

  return (
    <div className="int-panel">
      <div className="int-panel__header">
        <h2>Integrations</h2>
        <p className="int-panel__desc">
          Each integration connects agentify to an agentify-discovery collector. Namespaces
          watched by it are discovered automatically — assign the ones this integration is
          responsible for below.
        </p>
        {mode === "list" && (
          <button className="int-btn int-btn--primary" onClick={() => setMode("create")}>
            + Add integration
          </button>
        )}
      </div>

      {error && <div className="int-error">{error}</div>}

      {(mode === "create" || typeof mode === "object") && (
        <div className="int-form-wrap">
          <h3>{mode === "create" ? "New integration" : `Edit: ${(mode as { editing: Integration }).editing.name}`}</h3>
          <IntegrationForm
            initial={typeof mode === "object" ? mode.editing : undefined}
            discoveredNamespaces={discoveredNamespaces}
            onSave={handleSave}
            onCancel={() => setMode("list")}
            saving={saving}
          />
        </div>
      )}

      {mode === "list" && (
        <>
          {loading && <p className="int-loading">Loading…</p>}
          {!loading && list.length === 0 && (
            <EmptyState onAdd={() => setMode("create")} />
          )}
          {list.length > 0 && (
            <div className="int-list">
              {list.map(i => (
                <IntegrationRow
                  key={i.id}
                  integration={i}
                  discoveredNamespaces={discoveredNamespaces}
                  onEdit={handleEdit}
                  onDelete={handleDelete}
                />
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}
