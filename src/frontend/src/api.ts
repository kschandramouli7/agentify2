// Typed client for the agentify backend. Calls are same-origin (/api, /admin) and
// proxied to the Go backend by Vite in dev (see vite.config.ts).

export interface CertDetail {
  name: string;
  namespace: string;
  should_renew: boolean;
  days: number;
  expires_at: string;
  reason: string;
  urgency: "ok" | "warn" | "crit";
  dns_names?: string[];
}

export interface PodDetail {
  name: string;
  status: string;       // healthy | degraded | unhealthy | completed | unknown
  reason: string;
  phase: string;
  ready: boolean;
  restarts: number;
  completed: boolean;   // true = Succeeded/old pod, excluded from health score
}

export interface ToolCall {
  name: string;
  arguments: Record<string, unknown>;
}

export interface FindingDetail {
  resource: string;
  status: "HEALTHY" | "DEGRADED" | "UNHEALTHY";
  reason: string;
}

export interface ServiceHealthDetail {
  service: string;
  ready_replicas: number;
  total_replicas: number;
  ready_percent: number;
  endpoints: number;
}

export interface QueryResponse {
  answer: string;
  status: string;
  confidence: number; // 0.0–1.0
  sources: string[];
  trace_id?: string;
  tool_calls?: ToolCall[];
  details?: {
    // Cert check (Tier-1)
    certs_checked?: number;
    certs_needing_renewal?: number;
    renewal_threshold_days?: number;
    certificates?: CertDetail[];
    // Health check (Tier-1)
    healthy?: number;
    total_active?: number;
    total_completed?: number;
    ratio?: number;
    service_status?: string;
    pods?: PodDetail[];
    // Diagnosis (Tier-2) — old format
    severity?: string;
    likely_cause?: string;
    findings?: (string | FindingDetail)[];
    recommendations?: string[];
    // Diagnosis (Tier-2) — new k8fy/health-check format
    headline?: string;
    summary?: string;
    service_health?: ServiceHealthDetail;
    // Diagnosis (Tier-2) — new k8fy/diagnose format
    incident_summary?: string;
    timeline?: string[];
    // Service dependencies (intent "dependencies", answered deterministically —
    // tier1, no model call). Same shape the chat path attaches, so the same
    // component draws it.
    service_graph?: {
      namespace: string;
      focus?: string | null;
      dependencies: ServiceDependency[];
    };
  };
  // Which tier answered: "tier1" means deterministic, no model call.
  tier?: string;
  intent?: string;
}

export interface Pod {
  id: string;
  kind: string;
  summary: string;
  namespace: string;
  store_type: string;
  lifecycle: string;
  event_count: number;
  freshness: string;
  tags: string[] | null;
}

// ── Chat ─────────────────────────────────────────────────────────────────────

export interface RecommendedAction {
  label: string;
  tool: string;
  arguments: Record<string, unknown>;
}

export interface ChatMessageDetails {
  status?: string;
  severity?: string;
  incident_summary?: string;
  timeline?: string[];
  findings?: unknown[];
  likely_cause?: string | null;
  recommendations?: string[];
  recommended_actions?: RecommendedAction[];
  /**
   * The mined call graph, attached by the agent when the question was about
   * dependencies (or when it consulted the graph itself). Present so the chat
   * answer can DRAW the graph rather than paraphrase it — the same edges the
   * Dependencies tab renders, and the same component draws them.
   */
  service_graph?: {
    namespace: string;
    focus?: string | null;
    dependencies: ServiceDependency[];
  };
}

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  created_at: string;
  details?: ChatMessageDetails;
}

export interface ChatSession {
  id: string;
  title: string;
  namespace: string;
  service: string;
  messages: ChatMessage[] | null;
  created_at: string;
  last_active: string;
}

export async function createChatSession(init?: { namespace?: string; service?: string }): Promise<ChatSession> {
  const res = await fetch("/api/chat/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(init ?? {}),
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json() as Promise<ChatSession>;
}

export async function listChatSessions(): Promise<ChatSession[]> {
  const res = await fetch("/api/chat/sessions");
  if (!res.ok) return [];
  return res.json() as Promise<ChatSession[]>;
}

export async function getChatSession(id: string): Promise<ChatSession> {
  const res = await fetch(`/api/chat/sessions/${encodeURIComponent(id)}`);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json() as Promise<ChatSession>;
}

export async function sendChatMessage(
  sessionId: string,
  content: string,
): Promise<{ message: ChatMessage; session: ChatSession }> {
  const res = await fetch(`/api/chat/sessions/${encodeURIComponent(sessionId)}/messages`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content }),
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json() as Promise<{ message: ChatMessage; session: ChatSession }>;
}

// Directly invokes one of the agent's live-diagnostics tools (no LLM call) —
// used by a recommended action's "Run" button. `tool` must be one of the
// live-diagnostics tool names; the backend rejects anything else.
export async function runLiveTool(
  tool: string,
  args: Record<string, unknown>,
): Promise<{ tool: string; data: Record<string, unknown> }> {
  const res = await fetch("/api/live-query", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tool, arguments: args }),
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json() as Promise<{ tool: string; data: Record<string, unknown> }>;
}

export async function deleteChatSession(id: string): Promise<void> {
  await fetch(`/api/chat/sessions/${encodeURIComponent(id)}`, { method: "DELETE" });
}

export interface ModelPricing {
  model_id: string;
  display_name: string;
  input_per_mtok: number;
  output_per_mtok: number;
  cache_write_per_mtok: number;
  cache_read_per_mtok: number;
  updated_at: string;
}

export async function listPricing(): Promise<ModelPricing[]> {
  const res = await fetch("/admin/pricing");
  if (!res.ok) return [];
  return res.json() as Promise<ModelPricing[]>;
}

export async function upsertPricing(p: Omit<ModelPricing, "updated_at">): Promise<ModelPricing> {
  const res = await fetch("/admin/pricing", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(p),
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json() as Promise<ModelPricing>;
}

export interface ServiceContext {
  service: string;
  namespace: string;
  dns?: string;
}

async function postJSON<T>(url: string, body: unknown): Promise<T> {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(190_000), // 190s covers Opus (~90s) + headroom
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json() as Promise<T>;
}

async function getJSON<T>(url: string): Promise<T> {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json() as Promise<T>;
}

export interface CertRenewResponse {
  status: "ok" | "error";
  message: string;
  serial?: string;
  ttl?: string;
  k8s_secret_updated?: boolean;
  k8s_secret?: string;
  expires_at?: string;
  days_until_expiry?: number;
  dns_names?: string[];
}

// On-demand cert renewal — calls Vault PKI + updates K8s TLS Secret
export function renewCert(ctx: ServiceContext): Promise<CertRenewResponse> {
  return postJSON<CertRenewResponse>("/admin/certs/renew", {
    namespace: ctx.namespace,
    service:   ctx.service,
  });
}

// Tier-1: deterministic health check — no LLM, <10ms
export function checkHealth(ctx: ServiceContext): Promise<QueryResponse> {
  return postJSON<QueryResponse>("/api/query", {
    question: `is the ${ctx.service} service healthy?`,
    context: { namespace: ctx.namespace, service: ctx.service },
  });
}

// Tier-1: deterministic cert check — no LLM, <10ms.
// Certs are namespace-scoped in K8s; the backend returns all certs in the
// namespace and tier1Cert filters to secrets matching the service name first,
// falling back to all namespace certs. service is passed for name-matching only.
export function checkCerts(ctx: ServiceContext): Promise<QueryResponse> {
  return postJSON<QueryResponse>("/api/query", {
    question: `does ${ctx.service} have any certificates expiring soon?`,
    context: { namespace: ctx.namespace, service_hint: ctx.service },
  });
}

// Tier-2: full correlated diagnosis — Claude Opus, fires only when Tier-1 finds issues
export function diagnoseService(ctx: ServiceContext): Promise<QueryResponse> {
  return postJSON<QueryResponse>("/api/query", {
    question: `why is ${ctx.service} having issues? diagnose crashes, cert expiry, recent deploys, and restart trends`,
    context: { namespace: ctx.namespace, service: ctx.service },
  });
}

// Tier-2: what changed recently? (deploy events)
export function checkChangeHistory(ctx: ServiceContext): Promise<QueryResponse> {
  return postJSON<QueryResponse>("/api/query", {
    question: `what changed recently for ${ctx.service}? show recent deployments and rollouts`,
    context: { namespace: ctx.namespace, deployment: ctx.service },
  });
}

// Tier-2: restart trend over time
export function checkRestartTrend(ctx: ServiceContext): Promise<QueryResponse> {
  return postJSON<QueryResponse>("/api/query", {
    question: `show the restart trend for ${ctx.service} — when did restarts start and how many?`,
    context: { namespace: ctx.namespace, service: ctx.service },
  });
}

// Legacy free-text query (kept for backward compat)
export function askQuery(question: string, context: Record<string, string>): Promise<QueryResponse> {
  return postJSON<QueryResponse>("/api/query", { question, context });
}

export function listPods(): Promise<Pod[]> {
  return getJSON<Pod[]>("/admin/pods");
}

// ── Integrations ─────────────────────────────────────────────────────────────

export interface Integration {
  id: string;
  name: string;
  namespaces: string[];
  status: "active" | "inactive" | "error";
  has_token: boolean;
  created_at: string;
  updated_at: string;
}

export interface IntegrationInput {
  name: string;
  namespaces: string[];
  token?: string;
  status?: string;
}

export function listIntegrations(): Promise<Integration[]> {
  return getJSON<Integration[]>("/admin/integrations");
}

export function getIntegration(id: string): Promise<Integration> {
  return getJSON<Integration>(`/admin/integrations/${id}`);
}

export function createIntegration(input: IntegrationInput): Promise<Integration> {
  return postJSON<Integration>("/admin/integrations", input);
}

export async function updateIntegration(id: string, input: IntegrationInput): Promise<Integration> {
  const res = await fetch(`/admin/integrations/${id}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json() as Promise<Integration>;
}

export async function deleteIntegration(id: string): Promise<void> {
  const res = await fetch(`/admin/integrations/${id}`, { method: "DELETE" });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
}

// ── Query History (traces) ────────────────────────────────────────────────────

export interface TraceRecord {
  id: string;
  trace_id: string;
  question: string;
  intent: string;
  namespace: string;
  tier: string;
  status: string;
  confidence: number;
  sources: string[];
  tool_calls: string[];
  latency_ms: number;
  started_at: string;
  created_at: string;
  input_tokens: number;
  output_tokens: number;
  cache_creation_input_tokens: number;
  cache_read_input_tokens: number;
  estimated_cost_usd: number;
}

// ── Service dependencies (ROADMAP P18 use case #2, ADR 0029) ────────────────
// Edges are MINED FROM LOG TEXT, not observed on the network: an edge exists
// only where a caller logged the callee's DNS name. So absence means "no
// evidence", never "no dependency" — the UI has to say so, or a reader will
// treat a sparse graph as a complete one.
export type ServiceDependency = {
  id: string;
  namespace: string;
  from_service: string;
  to_service: string;
  evidence_count: number;
  first_seen: string;
  last_seen: string;
  tenant_id: string;
  cluster_id?: string;
};

export async function listServiceDependencies(namespace: string): Promise<ServiceDependency[]> {
  const res = await fetch(`/api/service-dependencies?namespace=${encodeURIComponent(namespace)}`);
  if (!res.ok) throw new Error(`Failed to load service dependencies (${res.status})`);
  return res.json();
}

export async function listTraces(): Promise<TraceRecord[]> {
  const res = await fetch("/admin/traces");
  if (res.status === 404) return [];
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json() as Promise<TraceRecord[]>;
}

export async function getTrace(id: string): Promise<TraceRecord> {
  const res = await fetch(`/admin/traces/${encodeURIComponent(id)}`);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json() as Promise<TraceRecord>;
}

// ── Metrics summary ───────────────────────────────────────────────────────────

export interface MetricsSummary {
  total_queries: number;
  last_24h_count: number;
  queries_by_tier: Record<string, number>;
  queries_by_status: Record<string, number>;
  queries_by_intent: Record<string, number>;
  avg_agent_latency_ms: number;
  p95_agent_latency_ms: number;
  collected_at: string;
}

export async function getMetricsSummary(): Promise<MetricsSummary> {
  const res = await fetch("/admin/metrics/summary");
  if (res.status === 404) return {
    total_queries: 0, last_24h_count: 0,
    queries_by_tier: {}, queries_by_status: {}, queries_by_intent: {},
    avg_agent_latency_ms: 0, p95_agent_latency_ms: 0,
    collected_at: new Date().toISOString(),
  };
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json() as Promise<MetricsSummary>;
}

export interface SyncResult {
  namespaces: { namespace: string; services: string[]; service_count: number }[];
  suggestions: string[];
  total: number;
}

// Trigger a sync from the cluster_services registry (populated by
// agentify-discovery's periodic inventory push, ADR 0027) — discovers all
// K8s namespaces/services.
export function syncNamespaces(): Promise<SyncResult> {
  return postJSON<SyncResult>("/admin/sync", {});
}

// ── Remediation proposals (ADR 0020 / spec 011 Use Cases 1+2) ────────────────
// Every proposal here is propose-only until explicitly approved — nothing in
// this file executes an infrastructure change on its own.

export interface RemediationProposal {
  id: string;
  trace_id?: string;
  use_case: "incident_responder" | "deployment_guardian";
  namespace: string;
  service: string;
  proposed_action: "restart_deployment" | "scale_deployment" | "rollback_deployment" | "rotate_cert" | "human_escalation";
  action_params: Record<string, unknown>;
  analysis: {
    reasoning?: string;
    blast_radius?: string;
    evidence?: string[];
    confidence?: number;
  };
  status: "pending" | "approved" | "rejected" | "executed" | "failed" | "expired";
  created_at: string;
  expires_at: string;
  decided_at?: string;
  decided_by?: string;
  executed_at?: string;
  result?: Record<string, unknown>;
  error?: string;
}

export async function listRemediationProposals(status?: string): Promise<RemediationProposal[]> {
  const qs = status ? `?status=${encodeURIComponent(status)}` : "";
  const res = await fetch(`/admin/remediation${qs}`);
  if (res.status === 404) return [];
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json() as Promise<RemediationProposal[]>;
}

export function approveRemediation(id: string): Promise<RemediationProposal> {
  return postJSON<RemediationProposal>(`/admin/remediation/${encodeURIComponent(id)}/approve`, {});
}

export function rejectRemediation(id: string): Promise<RemediationProposal> {
  return postJSON<RemediationProposal>(`/admin/remediation/${encodeURIComponent(id)}/reject`, {});
}
