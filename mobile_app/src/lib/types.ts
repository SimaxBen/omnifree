// Mirrors the pydantic models in core/interfaces.py and core/router.py (JSON mode: datetimes are ISO strings).

export interface HealthStatus {
  provider: string;
  ok: boolean;
  latency_ms?: number | null;
  detail?: string;
  checked_at?: string;
}

export interface QuotaInfo {
  provider: string;
  used: number;
  limit?: number | null;
  unit: string;
  window?: string | null;
  reset_at?: string | null;
  source: 'provider' | 'router' | 'config';
  extra?: Record<string, unknown>;
}

export interface ChatMessage {
  role: 'system' | 'user' | 'assistant' | 'tool';
  content: string | null;
}

export interface ChatRequest {
  messages: ChatMessage[];
  model?: string | null;
  temperature?: number | null;
  max_tokens?: number | null;
  provider?: string | null;
  allow_fallback?: boolean;
  stream?: boolean;
}

export interface Usage {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
}

export interface ChatResponse {
  id: string;
  model: string;
  content?: string | null;
  role: string;
  finish_reason?: string | null;
  usage: Usage;
  routed_via: string;
  attempts: string[];
  latency_ms?: number | null;
  created_at?: string;
}

export interface ChatChunk {
  delta: string;
  model: string;
  finish_reason?: string | null;
  usage?: Usage | null;
  routed_via: string;
}

export interface ModelInfo {
  id: string;
  provider: string;
  owned_by?: string | null;
  context_window?: number | null;
  capabilities?: string[];
}

export interface StoredObject {
  key: string;
  size: number;
  content_type?: string | null;
  etag?: string | null;
  last_modified?: string | null;
  backend: string;
  url?: string | null;
  metadata?: Record<string, string>;
}

export interface ColumnInfo {
  name: string;
  type: string;
  nullable: boolean;
  primary_key: boolean;
  default?: string | null;
}

export interface TableInfo {
  name: string;
  schema_name?: string | null;
  kind: 'table' | 'view' | 'collection' | 'keyspace' | 'topic' | 'index';
  row_count?: number | null;
  size_bytes?: number | null;
  columns: ColumnInfo[];
}

export interface QueryResult {
  columns: string[];
  rows: unknown[][];
  row_count: number;
  affected_rows?: number | null;
  elapsed_ms?: number | null;
  backend: string;
  truncated: boolean;
}

export type VMState =
  | 'running' | 'stopped' | 'starting' | 'stopping' | 'provisioning' | 'terminated'
  | 'sleeping' | 'deploying' | 'error' | 'online' | 'offline' | 'unknown';

export interface VMInstance {
  id: string;
  name: string;
  provider: string;
  state: VMState;
  region?: string | null;
  shape?: string | null;
  public_ip?: string | null;
  private_ip?: string | null;
  tailscale_ip?: string | null;
  url?: string | null;
  tags?: Record<string, string>;
  meta?: Record<string, unknown>;
}

export interface DNSStatus {
  provider: string;
  blocking_enabled: boolean;
  blocking_resumes_at?: string | null;
  queries_today?: number | null;
  blocked_today?: number | null;
  percent_blocked?: number | null;
  domains_on_blocklist?: number | null;
}

export interface DNSQueryLogEntry {
  timestamp: string;
  domain: string;
  client?: string | null;
  query_type?: string | null;
  status?: string | null;
  blocked: boolean;
}

export interface ProviderStatus {
  name: string;
  kind: string;
  group: string;
  enabled: boolean;
  priority: number;
  breaker: string;
  exhausted_until?: string | null;
  last_error?: string | null;
  last_latency_ms?: number | null;
  avg_latency_ms?: number | null;
  successes: number;
  failures: number;
  missing_secrets: string[];
  load_error?: string | null;
  last_health?: HealthStatus | null;
  usage: QuotaInfo[];
  /** Optional enrichments the hub may merge in from registry.describe()/class_meta(). */
  description?: string;
  display_name?: string;
  capabilities?: string[];
}

export interface DBProvider {
  name: string;
  enabled: boolean;
  display_name?: string;
  description?: string;
  capabilities?: string[];
  missing_secrets?: string[];
}

export type AgentTaskStatus = 'queued' | 'running' | 'completed' | 'failed' | 'cancelled';

export interface AgentTask {
  id: string;
  goal: string;
  status: AgentTaskStatus;
  start_url?: string | null;
  model?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  result?: string | null;
  error?: string | null;
  /** Instinct StepRecord objects (GET /agent/tasks/{id}); list entries carry step_count instead. */
  steps?: unknown[];
  step_count?: number;
  last_screenshot_b64?: string | null;
}

export interface AgentTaskCreate {
  goal: string;
  start_url?: string;
  model?: string;
  max_steps?: number;
}

/** core/events.py Event, sent as event.model_dump(mode="json") over /ws. */
export interface HubEvent {
  id: string;
  type: string;
  ts: string;
  provider?: string | null;
  kind?: string | null;
  data: Record<string, unknown>;
}
