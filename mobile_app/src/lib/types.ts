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

/** A message of a saved conversation (`/chats`). */
export interface ChatThreadMessage {
  role: 'user' | 'assistant' | 'system';
  content: string;
  routed_via?: string;
  model?: string;
  created_at?: string;
  /** Assistant turns: files attached to the message (Drive refs) and the tools used for the reply. */
  attachments?: { provider: string; key: string; name?: string; content_type?: string | null; size?: number | null }[];
  tools?: { name: string; summary: string; ok?: boolean; status?: 'created' | 'dismissed' | 'not-created' }[];
}

/** `GET /chats` row: a saved conversation without its messages. */
export interface ChatSummary {
  id: string;
  title: string;
  /** auto = first user message, llm = generated, user = renamed (never overwritten). */
  title_source: 'auto' | 'llm' | 'user';
  model: string | null;
  message_count: number;
  preview: string;
  pinned: boolean;
  created_at: string;
  updated_at: string;
}

export interface ChatThread extends ChatSummary {
  messages: ChatThreadMessage[];
}

export interface ChatThreadSave {
  title?: string;
  model?: string | null;
  messages: ChatThreadMessage[];
  pinned?: boolean;
}

export interface ChatThreadPatch {
  title?: string;
  model?: string | null;
  pinned?: boolean;
  append?: ChatThreadMessage[];
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

export type NoteSource = 'text' | 'voice' | 'meeting';

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
  /** HH:MM local (Africa/Casablanca) time; only with a due date. */
  due_time?: string | null;
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
  /** HH:MM; needs `due`. */
  due_time?: string;
  priority?: TaskPriority;
  note_id?: string;
}

export type TaskPatch = Partial<Omit<Task, 'id' | 'created_at'>>;

/** 'money' records expenses and incomes through `POST /money/voice`; 'auto' routes money talk there too. */
export type VoiceMode = 'note' | 'tasks' | 'both' | 'auto' | 'money';

export interface VoiceNoteResult {
  transcript: string;
  note: Note | null;
  tasks: Task[];
  audio_key: string;
  /** Money heard in an 'auto' memo ("paid 45 dh for a taxi"), recorded as expenses / incomes. */
  expenses?: Expense[];
  incomes?: Income[];
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
  /** Where the expense came from: 'email' for the Gmail import, null for manual or scanned ones. */
  source?: string | null;
  /** The source's reference: `<mailbox>#<Message-ID>` for email imports. */
  source_ref?: string | null;
  created_at: string;
}

export type ExpenseCreate = Omit<Expense, 'id' | 'created_at' | 'source' | 'source_ref'>;
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

// ------------------------------------------------------------------ Gmail receipts import

export interface InboxSyncRequest {
  days?: number;
  max?: number;
  dry_run?: boolean;
  /** Sync one mailbox only (account name or address). */
  account?: string;
}

export interface InboxSkip {
  account?: string;
  message_id: string;
  reason: string;
  subject?: string;
}

export interface InboxError {
  account?: string;
  message_id: string | null;
  error: string;
}

/** `POST /inbox/receipts/sync`. In a dry run `created` holds would-be expenses with `id: null`. */
export interface InboxSyncResult {
  scanned: number;
  matched: number;
  created: Expense[];
  skipped: InboxSkip[];
  errors: InboxError[];
  /** New messages left for the next run (time budget or `max` reached). */
  remaining: number;
  dry_run: boolean;
  /** One summary per mailbox visited (or not reached) by this call. */
  accounts: InboxAccountRun[];
}

export interface InboxAccountRun {
  name: string;
  user: string;
  scanned: number;
  created: number;
  skipped: number;
  /** null when the mailbox was not reached. */
  remaining: number | null;
  error: string | null;
  complete: boolean;
}

export interface InboxAccount {
  name: string;
  user: string;
  host: string;
  port: number;
  configured: boolean;
  last_sync_at: string | null;
  last_error: string | null;
}

export interface InboxStatus {
  configured: boolean;
  accounts: InboxAccount[];
  /** Configuration mistakes in INBOX_ACCOUNTS. */
  problems: string[];
  last_sync_at: string | null;
  last_result: Partial<InboxSyncResult> | null;
}

// ------------------------------------------------------------------ digest (summary)

export type DigestPeriod = 'week' | 'month';

export interface DigestStats {
  expenses: ExpenseSummary & {
    top_merchants: { merchant: string; currency: string; total: number; count: number }[];
  };
  tasks: { done: number; open: number; overdue: number };
  notes: { created: number; voice: number };
  drive: { uploaded_files: number | null; uploaded_bytes: number | null };
  llm: { requests_by_provider: Record<string, number> };
  /** Google Calendar events in the period; null/absent when Google Calendar is not set up. */
  calendar?: { events: number | null; all_day?: number } | null;
}

export interface Digest {
  period: DigestPeriod;
  from: string;
  to: string;
  generated_at: string;
  markdown: string;
  stats: DigestStats;
  highlights: string[];
  suggestions: string[];
  /** 'llm' or 'template' (no LLM answered). */
  source: string;
  cached: boolean;
}

// ------------------------------------------------------------------ money (/money)

export const INCOME_CATEGORIES = ['salary', 'freelance', 'refund', 'gift', 'other'] as const;
export type IncomeCategory = (typeof INCOME_CATEGORIES)[number];

export const ACCOUNT_KINDS = ['cash', 'bank', 'savings', 'card', 'other'] as const;
export type AccountKind = (typeof ACCOUNT_KINDS)[number];

export interface Income {
  id: string;
  /** YYYY-MM-DD; for a monthly income, its first month and pay day. */
  date: string;
  amount: number;
  currency: string;
  source: string;
  category: IncomeCategory;
  note: string | null;
  /** Counts every month from the month of `date` until `end_month` (inclusive, open when null). */
  recurring: boolean;
  end_month: string | null;
  /** 'manual' or 'voice'. */
  origin: string | null;
  audio_key: string | null;
  created_at: string;
}

export interface IncomeCreate {
  date?: string | null;
  amount: number;
  currency?: string;
  source?: string;
  category?: IncomeCategory;
  note?: string | null;
  recurring?: boolean;
  end_month?: string | null;
}
export type IncomePatch = Partial<IncomeCreate>;

export interface RecurringExpense {
  id: string;
  name: string;
  amount: number;
  currency: string;
  category: ExpenseCategory;
  /** Day of the month it is paid (clamped to short months). */
  day: number;
  start_month: string;
  end_month: string | null;
  note: string | null;
  created_at: string;
}

export interface RecurringCreate {
  name: string;
  amount: number;
  currency?: string;
  category?: ExpenseCategory;
  day?: number;
  start_month?: string | null;
  end_month?: string | null;
  note?: string | null;
}
export type RecurringPatch = Partial<RecurringCreate>;

export interface MoneyAccount {
  id: string;
  name: string;
  kind: AccountKind;
  balance: number;
  currency: string;
  note: string | null;
  updated_at: string;
  created_at: string;
}

export interface AccountCreate {
  name: string;
  kind?: AccountKind;
  balance?: number;
  currency?: string;
  note?: string | null;
}
export type AccountPatch = Partial<AccountCreate>;

export interface CategoryBudget {
  category: ExpenseCategory;
  amount: number;
  currency: string;
  updated_at: string;
}

export interface MoneyCategoryLine {
  category: string;
  /** MAD */
  spent: number;
  /** Monthly limit converted to MAD, null without a budget. */
  budget: number | null;
  budget_original: { amount: number; currency: string } | null;
  fraction: number | null;
}

export interface MoneyUpcoming {
  kind: 'expense' | 'income';
  id: string;
  name: string;
  amount: number;
  currency: string;
  amount_mad: number;
  date: string;
}

export type FxSource = 'live' | 'cache' | 'stale' | 'fallback';

/** `GET /money/overview?month=`: every total in MAD. */
export interface MoneyOverview {
  month: string;
  currency: string;
  income: { total: number; items: (Income & { amount_mad: number; on: string })[] };
  spent: { total: number; expenses: number; recurring: number; count: number };
  net: number;
  savings_rate: number | null;
  categories: MoneyCategoryLine[];
  series: { month: string; income: number; spent: number }[];
  /** `paid`: an expense of the month already paid this charge (it counts once, through the expense). */
  recurring: (RecurringExpense & { amount_mad: number; on: string; upcoming: boolean; paid?: boolean })[];
  upcoming: MoneyUpcoming[];
  accounts: { total: number; items: (MoneyAccount & { balance_mad: number })[] };
  fx: { base: string; source: FxSource; fetched_at: string | null; unconverted: string[] };
  forecast: MoneyForecast | null;
}

// ------------------------------------------------------------------ money intelligence (forecast, subscriptions, goals)

export type ForecastConfidence = 'low' | 'medium' | 'high';

/** `overview.forecast`: the end of the month in MAD. */
export interface MoneyForecast {
  projected_spent: number;
  projected_left: number;
  /** MAD per day of variable spend (expenses that pay no recurring charge). */
  daily_avg: number;
  remaining_recurring: number;
  spent_so_far: number;
  variable_spent: number;
  remaining_days: number;
  month_end: string;
  history_months: number;
  history_daily_avg: number | null;
  confidence: ForecastConfidence;
}

export type SubscriptionCadence = 'weekly' | 'monthly' | 'yearly';

/** `GET /money/subscriptions/suggestions`: a merchant charged again and again. */
export interface SubscriptionSuggestion {
  key: string;
  name: string;
  cadence: SubscriptionCadence;
  avg_amount: number;
  currency: string;
  avg_amount_mad: number;
  monthly_mad: number;
  occurrences: number;
  first_date: string;
  last_date: string;
  next_date: string;
  day: number | null;
  category: ExpenseCategory;
  /** 0..1 */
  confidence: number;
  level: ForecastConfidence;
  sources: string[];
  expense_ids: string[];
}

/** `GET /money/subscriptions`: accepted (weekly/yearly ones stay here, monthly ones become recurring) or dismissed. */
export interface TrackedSubscription {
  id: string;
  key: string;
  name: string;
  status: 'accepted' | 'dismissed';
  cadence: SubscriptionCadence;
  amount: number;
  currency: string;
  category: string;
  anchor_date: string | null;
  next_date: string | null;
  active: boolean;
  recurring_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface SubscriptionAccept {
  key: string;
  name?: string;
  amount?: number;
  currency?: string;
  category?: ExpenseCategory;
  day?: number;
}

export interface SavingsGoal {
  id: string;
  name: string;
  target: number;
  currency: string;
  saved: number;
  remaining: number;
  /** 0..1 */
  progress: number;
  done: boolean;
  deadline: string | null;
  months_left: number | null;
  overdue: boolean;
  /** Null without a deadline. */
  per_month_needed: number | null;
  saved_mad: number | null;
  target_mad: number | null;
  account_id: string | null;
  account: { id: string; name: string } | null;
  emoji: string | null;
  color: string | null;
  note: string | null;
  created_at: string;
  updated_at: string;
}

export interface GoalContribution {
  id: string;
  amount: number;
  note: string | null;
  date: string;
  created_at: string;
}

export interface GoalCreate {
  name: string;
  target: number;
  currency?: string;
  deadline?: string | null;
  saved?: number;
  account_id?: string | null;
  emoji?: string | null;
  color?: string | null;
  note?: string | null;
}
export type GoalPatch = Partial<Omit<GoalCreate, 'saved'>>;

export interface GoalsListing {
  goals: SavingsGoal[];
  total_saved_mad: number;
  total_target_mad: number;
}

export type VoiceMoneyKind = 'auto' | 'expense' | 'income';

/** `POST /money/voice`. */
export interface VoiceMoneyResult {
  transcript: string;
  audio_key: string;
  expenses: Expense[];
  incomes: Income[];
}

// ------------------------------------------------------------------ notifications (ntfy) and calendar feed

export type NotifyKind = 'morning' | 'evening' | 'weekly' | 'budget' | 'month' | 'reminders' | 'bills' | 'summary' | 'debts' | 'inbox' | 'spending';
export type NotifyRunKind = Exclude<NotifyKind, 'inbox' | 'spending'>;
export type NotifyChannel = 'ntfy' | 'webpush';

/** Per-channel outcome of one push (`app_notify_log.channels`). */
export interface NotifyChannelResults {
  ntfy?: { ok: boolean; id?: string | null; error?: string };
  webpush?: { ok: boolean; sent: number; failed: number; removed: number; errors: string[] };
}

export interface NotifyLogEntry {
  kind: string;
  title: string | null;
  /** 'sent' | 'failed' | 'sending' */
  status: string;
  error: string | null;
  channels?: NotifyChannelResults | null;
  sent_at: string;
}

export interface NotifyStatus {
  configured: boolean;
  server: string;
  /** The secret ntfy topic (returned to the authenticated PWA only). */
  topic: string | null;
  subscribe_url: string | null;
  token_set: boolean;
  prefs: Record<NotifyKind, boolean>;
  channels: Record<NotifyChannel, boolean>;
  webpush: { configured: boolean; error: string | null; devices: number };
  recent: NotifyLogEntry[];
}

// ------------------------------------------------------------------ native web push (/push)

export interface PushConfig {
  configured: boolean;
  /** base64url VAPID public key (applicationServerKey); null when not configured. */
  public_key: string | null;
  error: string | null;
}

/** One subscribed browser, as the hub lists it (never the keys). */
export interface PushDevice {
  /** SHA-256 hex of the endpoint. */
  id: string;
  label: string | null;
  browser: string | null;
  push_service: string | null;
  created_at: string | null;
  last_success_at: string | null;
  last_failure_at: string | null;
  last_error: string | null;
}

export interface PushTestResult {
  id: string;
  label: string | null;
  browser: string | null;
  ok: boolean;
  status: number | null;
  error: string | null;
  removed: boolean;
}

export interface PushTestResponse {
  sent: number;
  failed: number;
  removed: number;
  results: PushTestResult[];
}

export interface NotifyResult {
  kind: string;
  sent: boolean;
  skipped: string | null;
  title: string | null;
  message: string | null;
  channels?: NotifyChannelResults;
}

export interface CalendarInfo {
  configured: boolean;
  url: string | null;
  token_key: string;
}

// ------------------------------------------------------------------ Google Calendar (/gcal)

/** One agenda event read from Google Calendar (OmniFree's own task events are left out). */
export interface GCalEvent {
  id: string;
  /** Calendar name (or id). */
  calendar: string;
  calendar_id: string;
  title: string;
  /** All day: YYYY-MM-DD. Timed: ISO date-time with the Africa/Casablanca offset. */
  start: string;
  /** All day: the exclusive end date. */
  end: string;
  all_day: boolean;
  location: string | null;
  link: string | null;
}

export interface GCalEvents {
  configured: boolean;
  from: string;
  to: string;
  events: GCalEvent[];
  errors: { calendar: string; error: string }[];
}

export interface GCalCalendar {
  id: string;
  access_role: string | null;
  summary: string | null;
  error: string | null;
  write: boolean;
  read: boolean;
  ok: boolean;
}

export interface GCalSyncResult {
  configured: boolean;
  upserted: number;
  unchanged: number;
  deleted: number;
  failed: number;
  remaining: number;
  orphans_checked: boolean;
  errors: string[];
  synced_at?: string;
  skipped?: string;
}

export interface GCalStatus {
  configured: boolean;
  writable: boolean;
  sa_email: string | null;
  calendar_id: string | null;
  read_ids: string[];
  error: string | null;
  keys: { sa_json: string; calendar_id: string; read_ids: string };
  calendars: GCalCalendar[];
  last_sync: Omit<GCalSyncResult, 'configured'> | null;
  last_push_at: string | null;
  errors: { task_id: string; error: string | null; synced_at: string }[];
  counts: Record<string, number>;
}

// ------------------------------------------------------------------ bank SMS (/money/sms)

/** `GET /money/sms/setup`. */
export interface SmsSetup {
  configured: boolean;
  token: string | null;
  token_key: string;
  header: string;
  query_param: string;
  path: string;
  public_base_url: string | null;
}

export type SmsKind = 'expense' | 'income' | 'withdrawal' | 'ignored' | 'unknown';

export interface SmsParsed {
  kind: SmsKind;
  bank: string | null;
  amount: number | null;
  currency: string;
  merchant: string;
  date: string | null;
  card: string | null;
  balance: number | null;
  balance_currency: string | null;
  category: string;
  reason: string | null;
  parser: 'regex' | 'llm';
}

/** `POST /money/sms` (also with `dry_run`). */
export interface SmsResult {
  status: 'created' | 'ignored' | 'error';
  ignored?: string;
  dry_run: boolean;
  parsed: SmsParsed | null;
  expense: Expense | null;
  income: Income | null;
  account: MoneyAccount | null;
  cash_account: MoneyAccount | null;
}

/** One row of `GET /money/sms/log` (card numbers masked, OTP texts not stored). */
export interface SmsLogEntry {
  id: string;
  sender: string | null;
  bank: string | null;
  text: string | null;
  status: 'created' | 'ignored' | 'error';
  reason: string | null;
  kind: SmsKind | null;
  amount: number | null;
  currency: string | null;
  parser: string | null;
  expense_id: string | null;
  income_id: string | null;
  account_id: string | null;
  received_at: string | null;
  created_at: string;
}

// ------------------------------------------------------------------ tasks: recurrence, checklist, time blocks
// (merged into Task / TaskCreate / VoiceNoteResult above by interface declaration merging)

/** 0 = Monday ... 6 = Sunday. `interval` = every N days from the due date; `until` = last possible day. */
export interface Recurrence {
  freq: 'daily' | 'weekly' | 'monthly' | 'interval' | 'yearly';
  days?: number[];
  day?: number;
  interval?: number;
  month?: number;
  until?: string | null;
}

export interface ChecklistItem {
  id: string;
  text: string;
  done: boolean;
}

/** A Google Calendar time block booked for a task (local YYYY-MM-DDTHH:MM bounds). */
export interface TaskBlock {
  event_id: string;
  start: string | null;
  end: string | null;
}

export interface Task {
  recurrence?: Recurrence | null;
  recurrence_label?: string | null;
  series_id?: string | null;
  /** The occurrence created when this one was completed. */
  next_id?: string | null;
  checklist?: ChecklistItem[];
  /** Minutes. */
  estimate?: number | null;
  block?: TaskBlock | null;
  /** Only in the PATCH answer that completed a recurring task: its new occurrence. */
  next?: Task;
  /** Only in the PATCH answer that reopened a recurring task: the untouched next occurrence it deleted. */
  removed_next?: string;
}

export interface TaskCreate {
  /** A rule, or a phrase the hub parses ("every monday", "le 1er de chaque mois"). */
  recurrence?: Recurrence | string | null;
  checklist?: { id?: string; text: string; done?: boolean }[];
  estimate?: number | null;
}

/** `GET /tasks/{id}/slots`. */
export interface TaskSlots {
  /** Google Calendar can take a block (POST /tasks/{id}/block). */
  writable: boolean;
  minutes: number;
  slots: { start: string; end: string }[];
  errors: { calendar: string; error: string }[];
  work_hours: [string, string];
}

export interface TaskBlockResult {
  task: Task;
  event: GCalEvent;
}

export interface VoiceNoteResult {
  /** Appointments heard in the memo, booked in the OmniFree Google Calendar (undo: DELETE /gcal/events/{id}). */
  events?: GCalEvent[];
}

// ------------------------------------------------------------------ global search (GET /search)

export type SearchResultType = 'notes' | 'tasks' | 'expenses' | 'incomes' | 'files' | 'chats' | 'links';

export interface SearchResult {
  type: SearchResultType;
  /** Note/task/expense/income/chat id; `backend:key` for files; `link:CODE` / `drop:CODE` for links. */
  id: string;
  title: string;
  snippet: string;
  date: string | null;
  score: number;
  /** Per type: the full expense / income row, the StoredObject of a file, `{kind, code, url}` of a link... */
  data: Record<string, unknown>;
}

export interface SearchGroup {
  type: SearchResultType;
  label: string;
  /** Matches before the per-type limit. */
  count: number;
  items: SearchResult[];
}

export interface SearchResponse {
  query: string;
  terms: string[];
  total: number;
  groups: SearchGroup[];
  /** Sources that failed or timed out (e.g. a storage backend), by type. */
  errors: Record<string, string>;
}

// ------------------------------------------------------------------ folder drops (offline & sharing)

/** A drop as `GET /drops` lists it since folder drops: `kind` 'file' or 'folder' (key = the folder prefix). */
export interface FolderDrop extends Drop {
  kind?: 'file' | 'folder';
  /** Files under the folder when the drop was made (folder drops only). */
  file_count?: number | null;
}

export interface FolderDropCreate {
  /** Drive folder, e.g. "photos/trip". */
  prefix: string;
  /** Storage backend; omitted: the folder across every backend. */
  provider?: string;
  expires_in_hours?: number;
  max_downloads?: number;
  password?: string;
}
