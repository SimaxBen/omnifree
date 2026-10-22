// App icon shortcuts (long press on the OmniFree icon): the web manifest lists them (vite.config.ts,
// manifest.shortcuts) with URLs like `/?shortcut=voice`; on load the app reads the parameter, removes it from the
// address and starts the matching capture once the app lock (if any) is open.

export type ShortcutId = 'voice' | 'receipt' | 'note' | 'task' | 'money';

export interface Shortcut {
  id: ShortcutId;
  name: string;
  short_name: string;
  description: string;
}

export const SHORTCUTS: Shortcut[] = [
  { id: 'voice', name: 'Voice note', short_name: 'Voice', description: 'Record a voice note' },
  { id: 'receipt', name: 'Scan receipt', short_name: 'Receipt', description: 'Photograph a receipt' },
  { id: 'note', name: 'New note', short_name: 'Note', description: 'Write a note' },
  { id: 'task', name: 'New task', short_name: 'Task', description: 'Add a task' },
  { id: 'money', name: 'Money', short_name: 'Money', description: 'Say an expense or an income' },
];

export const SHORTCUT_PARAM = 'shortcut';

export function shortcutUrl(id: ShortcutId): string {
  return `/?${SHORTCUT_PARAM}=${id}`;
}

export function shortcutIcon(id: ShortcutId): string {
  return `icons/shortcut-${id}.png`;
}

/** The shortcut asked for by a location search string (`?shortcut=voice`), or null. */
export function parseLaunch(search: string): ShortcutId | null {
  let value: string | null = null;
  try {
    value = new URLSearchParams(search).get(SHORTCUT_PARAM);
  } catch {
    return null;
  }
  return SHORTCUTS.some((s) => s.id === value) ? (value as ShortcutId) : null;
}

/** The same location without the shortcut parameter (what the address bar shows after the launch). */
export function stripLaunch(pathname: string, search: string, hash: string): string {
  const params = new URLSearchParams(search);
  params.delete(SHORTCUT_PARAM);
  const rest = params.toString();
  return `${pathname}${rest ? `?${rest}` : ''}${hash}`;
}
