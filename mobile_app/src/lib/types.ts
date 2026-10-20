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

// ------------------------------------------------------------------ drive: direct transfers

/** `GET /drive/presign/{provider}/{key}`. */
export interface PresignedUrl {
  url: string;
  provider: string;
  key?: string;
  expires_s?: number;
  method?: string;
}

export interface UploadUrlRequest {
  filename: string;
  size: number;
  content_type: string;
  prefix?: string;
  /** Full target key (overrides prefix + filename). */
  key?: string;
  provider?: string;
}

/** `POST /drive/upload-url`: where and how to send the bytes, bypassing the hub. */
export interface DirectUploadTarget {
  provider: string;
  key: string;
  method: string;
  url: string;
  headers: Record<string, string>;
  expires_at?: string | null;
}

// ------------------------------------------------------------------ cron (core/interfaces.py CronJob)

export interface CronProvider {
  name: string;
  enabled: boolean;
  healthy?: boolean | null;
  description?: string | null;
}

export interface CronJob {
  id: string;
  name: string;
  /** 5-field cron expression, UTC. */
  schedule: string;
  /** URL (QStash) or workflow file name (GitHub Actions). */
  target: string;
  method: string;
  headers: Record<string, string>;
  body?: string | null;
  enabled: boolean;
  provider: string;
  last_run_at?: string | null;
  next_run_at?: string | null;
}

export interface CronJobCreate {
  provider: string;
  name: string;
  schedule: string;
  target: string;
  method?: string;
  headers?: Record<string, string>;
  body?: string;
}

// ------------------------------------------------------------------ LLM audio & rerank

export interface TranscriptionResult {
  text: string;
  model?: string;
  language?: string;
  duration?: number;
}

export interface RerankRequest {
  query: string;
  documents: string[];
  top_n?: number;
}

export interface RerankResult {
  index: number;
  /** Hub field name (core.interfaces.RerankResult). */
  relevance_score: number;
  document?: string;
}

// ------------------------------------------------------------------ apps (round 3 contract)

export type NoteSource = 'text' | 'voice';

export interface Note {
  id: string;
  title: string;
  /** Markdown. */
  body: string;
  notebook: string;
  tags: string[];
  source: NoteSource;
  audio_key: string | null;
  pinned: boolean;
  created_at: string;
  updated_at: string;
}

export interface NotebookCount {
  name: string;
  count: number;
}

export interface NoteListing {
  notes: Note[];
  notebooks: NotebookCount[];
}

export interface NoteCreate {
  title?: string;
  body: string;
  notebook?: string;
  tags?: string[];
  pinned?: boolean;
}

export type NotePatch = Partial<Omit<Note, 'id' | 'created_at'>>;

export type TaskPriority = 'low' | 'normal' | 'high';

export interface Task {
  id: string;
  title: string;
  details: string | null;
  /** YYYY-MM-DD */
  due: string | null;
  done: boolean;
  priority: TaskPriority;
  note_id: string | null;
  created_at: string;
  done_at: string | null;
}

export interface TaskCreate {
  title: string;
  details?: string;
  due?: string;
  priority?: TaskPriority;
  note_id?: string;
}

export type TaskPatch = Partial<Omit<Task, 'id' | 'created_at'>>;

export type VoiceMode = 'note' | 'tasks' | 'both' | 'auto';

export interface VoiceNoteResult {
  transcript: string;
  note: Note | null;
  tasks: Task[];
  audio_key: string;
}

export const EXPENSE_CATEGORIES = [
  'food',
  'groceries',
  'transport',
  'housing',
  'bills',
  'health',
  'shopping',
  'entertainment',
  'education',
  'travel',
  'other',
] as const;

export type ExpenseCategory = (typeof EXPENSE_CATEGORIES)[number];

export interface ExpenseItem {
  name: string;
  qty: number | null;
  price: number | null;
}

export interface Expense {
  id: string;
  /** YYYY-MM-DD */
  date: string;
  merchant: string;
  total: number;
  /** ISO 4217 */
  currency: string;
  category: ExpenseCategory;
  items: ExpenseItem[];
  note: string | null;
  receipt_key: string | null;
  created_at: string;
}

export type ExpenseCreate = Omit<Expense, 'id' | 'created_at'>;
export type ExpensePatch = Partial<ExpenseCreate>;

export interface ExpenseSummary {
  total_by_currency: Record<string, number>;
  by_category: Record<string, Record<string, number>>;
  count: number;
}

export interface ExpenseListing {
  expenses: Expense[];
  summary: ExpenseSummary;
}

export interface ReceiptResult {
  expense: Expense;
  confidence: 'high' | 'medium' | 'low';
}

export interface ShortLink {
  code: string;
  url: string;
  title: string | null;
  clicks: number;
  created_at: string;
  expires_at: string | null;
  short_url: string;
}

export interface ShortLinkCreate {
  url: string;
  code?: string;
  title?: string;
  expires_in_days?: number;
}

export interface Drop {
  code: string;
  provider: string;
  key: string;
  filename: string;
  size: number | null;
  created_at: string;
  expires_at: string;
  max_downloads: number | null;
  downloads: number;
  has_password: boolean;
  url: string;
}

export interface DropCreate {
  provider: string;
  key: string;
  expires_in_hours?: number;
  max_downloads?: number;
  password?: string;
}

export interface RagDoc {
  provider: string;
  key: string;
  title: string;
  mime: string;
  size: number | null;
  chunks: number;
  indexed_at: string;
  error: string | null;
}

export interface RagDocListing {
  docs: RagDoc[];
  embedding_model: string;
}

export interface RagSource {
  n: number;
  provider: string;
  key: string;
  title: string;
  page: number | null;
  snippet: string;
  score: number;
}

export interface RagAnswer {
  /** Markdown with [n] citation markers. */
  answer: string;
  sources: RagSource[];
  routed_via: string;
}

export interface RagAskRequest {
  question: string;
  top_k?: number;
  keys?: string[];
}

export type BudgetStatus = 'ok' | 'warn' | 'over' | 'unknown';

export interface BudgetItem {
  provider: string;
  kind: string;
  group: string;
  metric: string;
  window: string;
  used: number | null;
  limit: number | null;
  unit: string;
  fraction: number | null;
  status: BudgetStatus;
  reset_at: string | null;
  source: 'provider' | 'router' | 'config';
}

export interface BudgetReport {
  generated_at: string;
  items: BudgetItem[];
  summary: Record<BudgetStatus, number>;
}
