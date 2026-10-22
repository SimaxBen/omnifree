// The AI assistant (`/assistant`): data-aware chat with tools, confirmations, memory, attachments
// and voice helpers. Kept out of api.ts: every call goes through HubClient.request.
import type { HubClient } from './api';
import type { ChatMessage } from './types';

export type DataMode = 'auto' | 'always' | 'never';
export const DATA_MODES: { value: DataMode; label: string }[] = [
  { value: 'auto', label: 'Auto' },
  { value: 'always', label: 'Always' },
  { value: 'never', label: 'Never' },
];

/** Writes the owner confirms; `remember` too when the turn read third-party text (a file, notes, Drive). */
export type WriteTool = 'tasks_add' | 'expense_add' | 'notes_create' | 'income_add' | 'calendar_add_event' | 'remember';

/** Final state of a proposed write, kept with the saved chat: created, dismissed or never confirmed. */
export type ProposedStatus = 'created' | 'dismissed' | 'not-created';

export interface UsedTool {
  name: string;
  args?: Record<string, unknown>;
  summary: string;
  ok?: boolean;
  /** A write only proposed (waiting for Confirm), tied to its card by `action_id`. */
  pending?: boolean;
  action_id?: string;
  status?: ProposedStatus;
}

export interface PendingAction {
  id: string;
  tool: WriteTool;
  args: Record<string, unknown>;
  summary: string;
}

/** A Drive object attached to a chat message (uploaded under chat/YYYY/MM/ first). */
export interface AttachmentRef {
  provider: string;
  key: string;
  name?: string;
  content_type?: string | null;
  size?: number | null;
  mode?: 'image' | 'text' | 'native';
  pages?: number;
  truncated?: boolean;
}

export type MemoryKind = 'fact' | 'preference';

export interface MemoryFact {
  id: string;
  fact: string;
  pinned: boolean;
  /** preference: how to answer (sent with every chat); fact: only with questions about your data. */
  kind?: MemoryKind;
  source_chat: string | null;
  created_at: string;
  updated_at: string;
}

/** A history message; `data` marks an earlier answer drawn from the owner's data (tools or facts). */
export interface AssistantMessage extends ChatMessage {
  data?: boolean;
}

/** What replaces a data answer in the history of a general question (no personal data leaves then). */
export const WITHHELD_ANSWER = "[Earlier answer drawn from the owner's personal data: withheld from this general question.]";

/** History for a plain (general) chat: earlier data answers are withheld, the `data` flags dropped. */
export function generalHistory(messages: readonly AssistantMessage[]): ChatMessage[] {
  return messages.map((m) => ({ role: m.role, content: m.data && m.role === 'assistant' ? WITHHELD_ANSWER : m.content }));
}

export interface AssistantRequest {
  messages: AssistantMessage[];
  mode: DataMode;
  auto_confirm?: boolean;
  chat_id?: string;
  attachments?: AttachmentRef[];
  model?: string | null;
  provider?: string | null;
  allow_fallback?: boolean;
  /** How the previous reply was routed: a short follow-up of a data turn stays on data. */
  previous_route?: 'data' | 'general';
}

export interface AssistantReply {
  content: string;
  routed: 'data' | 'general';
  route_reason: string;
  used_tools: UsedTool[];
  pending_actions: PendingAction[];
  remembered: MemoryFact[];
  memories_used: number;
  /** The final answer failed after tools ran: `content` lists what was read, created or proposed. */
  partial?: boolean;
  attachments: AttachmentRef[];
  language: SpeechLang;
  routed_via: string;
  model: string;
  latency_ms: number;
}

export interface RouteResult {
  route: 'data' | 'general' | 'ambiguous';
  reason: string;
  /** Answer-style preferences a general turn may carry (sent as a system message when streaming). */
  preferences: string[];
}

/** What ChatPanel.send gives the voice assistant: the reply, or why there is none. */
export type AskResult = { ok: true; reply: AssistantReply } | { ok: false; error: string; busy?: boolean; aborted?: boolean };

export interface ConfirmResult {
  ok: boolean;
  tool: WriteTool;
  summary: string;
  result: Record<string, unknown>;
}

// ------------------------------------------------------------------ hub calls

export function assistantChat(client: HubClient, req: AssistantRequest, signal?: AbortSignal): Promise<AssistantReply> {
  return client.request<AssistantReply>('POST', '/assistant/chat', { json: req, signal });
}

/** Keyword routing only (no LLM): lets the PWA stream general answers in Auto mode. */
export function assistantRoute(
  client: HubClient,
  body: { text: string; previous?: string; previous_route?: 'data' | 'general'; mode: DataMode },
  signal?: AbortSignal,
): Promise<RouteResult> {
  return client.request<RouteResult>('POST', '/assistant/route', { json: body, signal });
}

export function assistantConfirm(client: HubClient, action: Pick<PendingAction, 'tool' | 'args'>): Promise<ConfirmResult> {
  return client.request<ConfirmResult>('POST', '/assistant/confirm', { json: { tool: action.tool, args: action.args } });
}

export async function memoryList(client: HubClient, q?: string, signal?: AbortSignal): Promise<MemoryFact[]> {
  const body = await client.request<{ memories?: MemoryFact[] }>('GET', '/assistant/memory', { query: { q }, signal });
  return Array.isArray(body?.memories) ? body.memories : [];
}

export function memoryCreate(client: HubClient, fact: string, pinned = false): Promise<MemoryFact> {
  return client.request<MemoryFact>('POST', '/assistant/memory', { json: { fact, pinned } });
}

export function memoryUpdate(client: HubClient, id: string, patch: { fact?: string; pinned?: boolean; kind?: MemoryKind }): Promise<MemoryFact> {
  return client.request<MemoryFact>('PATCH', `/assistant/memory/${encodeURIComponent(id)}`, { json: patch });
}

export function memoryDelete(client: HubClient, id: string): Promise<void> {
  return client.request<void>('DELETE', `/assistant/memory/${encodeURIComponent(id)}`);
}

export function memoryForgetAll(client: HubClient): Promise<{ deleted: number }> {
  return client.request<{ deleted: number }>('DELETE', '/assistant/memory', { query: { confirm: 'true' } });
}

// ------------------------------------------------------------------ pure helpers

const TOOL_LABELS: Record<string, string> = {
  expenses_month: 'Expenses',
  money_overview: 'Money',
  tasks_list: 'Tasks',
  notes_search: 'Notes',
  notes_get: 'Note',
  ask_drive: 'Ask my Drive',
  calendar_events: 'Agenda',
  budget_status: 'Free-tier budget',
  drive_search: 'Drive search',
  tasks_add: 'New task',
  expense_add: 'New expense',
  notes_create: 'New note',
  income_add: 'New income',
  calendar_add_event: 'New event',
  remember: 'Memory',
};

/** Short chip label for a tool name. */
export function toolLabel(name: string): string {
  return TOOL_LABELS[name] ?? name.replace(/_/g, ' ');
}

/** Whether a message goes through /assistant/chat (data modes, or files attached) rather than the plain pool chat. */
export function usesAssistant(mode: DataMode, attachmentCount: number): boolean {
  return mode !== 'never' || attachmentCount > 0;
}

/**
 * How a typed message is sent: `assistant` (/assistant/chat), `plain` (pool chat, streamed or not),
 * or `probe` (Auto with streaming on: ask /assistant/route first and stream when it says general).
 */
export function sendPath(mode: DataMode, attachmentCount: number, streaming: boolean): 'assistant' | 'plain' | 'probe' {
  if (!usesAssistant(mode, attachmentCount)) return 'plain';
  return mode === 'auto' && attachmentCount === 0 && streaming ? 'probe' : 'assistant';
}

/** The system message carrying answer-style preferences on a streamed general turn (null when none). */
export function preferenceSystem(preferences: readonly string[]): string | null {
  const items = preferences.map((p) => p.trim()).filter(Boolean);
  return items.length ? ['How the owner wants answers (saved preferences, follow them):', ...items.map((p) => `- ${p}`)].join('\n') : null;
}

export type ChipState = 'ok' | 'error' | 'proposed' | 'created' | 'dismissed' | 'not-created';

interface ActionLike {
  id: string;
  status: string;
}

/** What a tool chip shows: a proposed write follows its Confirm card (live) or its saved status. */
export function chipState(tool: UsedTool, actions?: readonly ActionLike[]): ChipState {
  if (tool.status) return tool.status;
  if (tool.ok === false) return 'error';
  if (!tool.pending) return 'ok';
  const action = actions?.find((a) => a.id === tool.action_id);
  if (action?.status === 'done') return 'created';
  if (action?.status === 'dismissed') return 'dismissed';
  return 'proposed';
}

/** Tool records kept with a saved chat: proposed writes carry their final status (a card cannot come back). */
export function savedTools(tools: readonly UsedTool[], actions?: readonly ActionLike[]): { name: string; summary: string; ok?: boolean; status?: ProposedStatus }[] {
  return tools.map((t) => {
    const state = chipState(t, actions);
    const status: ProposedStatus | undefined = !t.pending && !t.status ? undefined : state === 'created' || state === 'dismissed' ? state : 'not-created';
    return { name: t.name, summary: t.summary, ...(t.ok !== undefined ? { ok: t.ok } : {}), ...(status ? { status } : {}) };
  });
}

/** Drive key of a chat attachment: chat/YYYY/MM/<yyyymmdd-hhmmss>-<safe name>. */
export function attachmentKey(name: string, now: Date = new Date()): string {
  const p = (n: number) => String(n).padStart(2, '0');
  const dot = name.lastIndexOf('.');
  const ext = dot > 0 ? name.slice(dot + 1).toLowerCase().replace(/[^a-z0-9]/g, '').slice(0, 8) : '';
  const stem = (dot > 0 ? name.slice(0, dot) : name)
    .normalize('NFKD')
    .replace(/[̀-ͯ]/g, '')
    .replace(/[^A-Za-z0-9_-]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 60) || 'file';
  const stamp = `${now.getFullYear()}${p(now.getMonth() + 1)}${p(now.getDate())}-${p(now.getHours())}${p(now.getMinutes())}${p(now.getSeconds())}`;
  return `chat/${now.getFullYear()}/${p(now.getMonth() + 1)}/${stamp}-${stem}${ext ? `.${ext}` : ''}`;
}

export type AttachKind = 'image' | 'pdf' | 'text';
export const MAX_ATTACHMENTS = 4;
const LIMITS: Record<AttachKind, number> = { image: 8 * 1024 * 1024, pdf: 20 * 1024 * 1024, text: 1024 * 1024 };

/** What the assistant can do with a file, or an error message for the user. */
export function attachmentKind(name: string, type: string | null | undefined, size: number): { kind: AttachKind } | { error: string } {
  const t = (type ?? '').toLowerCase();
  const lower = name.toLowerCase();
  let kind: AttachKind | null = null;
  if (/^image\/(jpeg|png|webp|gif|heic|heif)$/.test(t) || /\.(jpe?g|png|webp|gif|heic|heif)$/.test(lower)) kind = 'image';
  else if (t === 'application/pdf' || lower.endsWith('.pdf')) kind = 'pdf';
  else if (t.startsWith('text/') || t === 'application/json' || /\.(txt|md|csv|json)$/.test(lower)) kind = 'text';
  if (!kind) return { error: `${name}: only images, PDF and text files can be attached` };
  if (size > LIMITS[kind]) return { error: `${name} is too large (max ${Math.round(LIMITS[kind] / (1024 * 1024))} MB for ${kind === 'pdf' ? 'PDF' : kind}s)` };
  return { kind };
}

export type SpeechLang = 'ar' | 'fr' | 'en';

const FR_WORDS = new Set(
  "le la les des est je tu vous mon ma mes quoi combien pour avec une un du et pas que qui sur dans ce cette ajoute j'ai bonjour merci oui non quand comment pourquoi wach chhal dyal bghit 3ndi khassni kifach daba".split(' '),
);
const EN_WORDS = new Set('the is are what my how you and to of for with a an in on it this that i me do did when why add please hello thanks yes no'.split(' '));

/** Arabic script -> ar; else French (or Latin-script Darija) vs English by common words. Mirrors the hub. */
export function detectLanguage(text: string): SpeechLang {
  const letters = [...(text || '')].filter((c) => /\p{L}/u.test(c));
  if (!letters.length) return 'en';
  const arabic = letters.filter((c) => c >= '؀' && c <= 'ۿ').length;
  if (arabic / letters.length > 0.3) return 'ar';
  if (/[éèêàçùûôîœ]/i.test(text)) return 'fr';
  const words = text.toLowerCase().match(/[a-z0-9']+/g) ?? [];
  const fr = words.filter((w) => FR_WORDS.has(w)).length;
  const en = words.filter((w) => EN_WORDS.has(w)).length;
  return fr > en ? 'fr' : 'en';
}

/** Spoken when the assistant could not answer (hub error, timeout), in the language of the question. */
export function failureLine(lang: SpeechLang): string {
  return { fr: "Désolé, ça n'a pas marché. Réessaie dans un instant.", en: "Sorry, that didn't work. Try again in a moment.", ar: 'سمح ليا، ما خدمش. عاود من بعد شوية.' }[lang];
}

/** Spoken when a previous request is still running. */
export function busyLine(lang: SpeechLang): string {
  return { fr: 'Un instant, je termine la demande précédente.', en: 'One moment, I am still on the previous request.', ar: 'لحظة، مازال كنكمل الطلب اللي قبل.' }[lang];
}

export const SPEECH_TAGS: Record<SpeechLang, string> = { fr: 'fr-FR', ar: 'ar-MA', en: 'en-US' };

interface VoiceLike {
  lang: string;
  name: string;
  localService?: boolean;
}

/** Best installed voice for a language: exact tag, then same language (Moroccan Arabic falls back to any Arabic). */
export function pickVoice<V extends VoiceLike>(voices: readonly V[], lang: SpeechLang): V | null {
  const tag = SPEECH_TAGS[lang].toLowerCase();
  const norm = (v: V) => v.lang.replace('_', '-').toLowerCase();
  return (
    voices.find((v) => norm(v) === tag) ??
    voices.find((v) => norm(v).startsWith(`${lang}-`) && v.localService) ??
    voices.find((v) => norm(v).startsWith(`${lang}-`) || norm(v) === lang) ??
    null
  );
}

/** Markdown reduced to what should be read aloud (no code blocks, links, tables or emphasis marks). */
export function speakableText(markdown: string, max = 1200): string {
  const text = markdown
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/`([^`]*)`/g, '$1')
    .replace(/!\[[^\]]*]\([^)]*\)/g, ' ')
    .replace(/\[([^\]]+)]\([^)]*\)/g, '$1')
    .replace(/^\s*\|.*\|\s*$/gm, ' ')
    .replace(/^\s{0,3}#{1,6}\s+/gm, '')
    .replace(/^\s*[-*+]\s+/gm, '')
    .replace(/[*_~>#]+/g, '')
    .replace(/\s+/g, ' ')
    .trim();
  if (text.length <= max) return text;
  const cut = text.slice(0, max);
  const end = Math.max(cut.lastIndexOf('. '), cut.lastIndexOf('? '), cut.lastIndexOf('! '));
  return (end > max / 2 ? cut.slice(0, end + 1) : cut).trim();
}

// Word ends that also work after Arabic letters (`\b` does not: they are not ASCII word characters).
// Short words that are also common ("la facture ?", "ah bon") only count as the whole utterance.
const SPOKEN_YES = /^(?:oui|ouais|yes|yeah|yep|ok|okay|d['’]accord|confirme[rsz]?|confirm|vas-y|go|wakha|iyeh|نعم|واخا|إيه|ايه)(?![\p{L}\p{N}])|^(?:ah|اه|آه)[\s.,!?؟،]*$/iu;
const SPOKEN_NO = /^(?:non|no|nope|annule[rsz]?|cancel|stop|لا|لالا)(?![\p{L}\p{N}])|^(?:la|lla)[\s.,!?؟،]*$/iu;
const SPOKEN_NEGATION = /(?:^|[^\p{L}\p{N}])(?:non|no|nope|annule[rsz]?|cancel|stop|لا)(?![\p{L}\p{N}])/iu;

/** Short French/English/Darija confirmation phrases for hands-free use ("oui", "confirme", "yes", "wakha", "واخا"). */
export function isSpokenYes(text: string): boolean {
  const t = text.trim();
  return SPOKEN_YES.test(t) && !SPOKEN_NEGATION.test(t); // "ok non attends" is not a yes
}

export function isSpokenNo(text: string): boolean {
  return SPOKEN_NO.test(text.trim());
}
