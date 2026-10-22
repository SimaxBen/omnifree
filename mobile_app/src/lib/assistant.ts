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

export type WriteTool = 'tasks_add' | 'expense_add' | 'notes_create' | 'income_add' | 'calendar_add_event';

export interface UsedTool {
  name: string;
  args?: Record<string, unknown>;
  summary: string;
  ok?: boolean;
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

export interface MemoryFact {
  id: string;
  fact: string;
  pinned: boolean;
  source_chat: string | null;
  created_at: string;
  updated_at: string;
}

export interface AssistantRequest {
  messages: ChatMessage[];
  mode: DataMode;
  auto_confirm?: boolean;
  chat_id?: string;
  attachments?: AttachmentRef[];
  model?: string | null;
  provider?: string | null;
  allow_fallback?: boolean;
}

export interface AssistantReply {
  content: string;
  routed: 'data' | 'general';
  route_reason: string;
  used_tools: UsedTool[];
  pending_actions: PendingAction[];
  remembered: MemoryFact[];
  memories_used: number;
  attachments: AttachmentRef[];
  language: SpeechLang;
  routed_via: string;
  model: string;
  latency_ms: number;
}

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

export function memoryUpdate(client: HubClient, id: string, patch: { fact?: string; pinned?: boolean }): Promise<MemoryFact> {
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
  remember: 'Remembered',
};

/** Short chip label for a tool name. */
export function toolLabel(name: string): string {
  return TOOL_LABELS[name] ?? name.replace(/_/g, ' ');
}

/** Whether a message goes through /assistant/chat (data modes, or files attached) rather than the plain pool chat. */
export function usesAssistant(mode: DataMode, attachmentCount: number): boolean {
  return mode !== 'never' || attachmentCount > 0;
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

/** Short French/English confirmation phrases for hands-free use ("oui", "confirme", "yes", "wakha"). */
export function isSpokenYes(text: string): boolean {
  return /^\s*(oui|ouais|yes|yeah|yep|ok|okay|d'accord|confirme[rsz]?|confirm|vas-y|go|wakha|iyeh|ah|نعم|واخا|اه)\b/i.test(text.trim());
}

export function isSpokenNo(text: string): boolean {
  return /^\s*(non|no|nope|annule[rsz]?|cancel|stop|la|lla|لا)\b/i.test(text.trim());
}
