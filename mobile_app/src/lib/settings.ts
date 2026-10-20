// Hub connection settings, persisted in localStorage. Every storage access is guarded:
// private windows and blocked site data make localStorage throw or come back empty.

export interface HubSettings {
  hubUrl: string;
  token: string;
}

const KEY = 'omnifree.settings.v1';

export function defaultHubUrl(): string {
  const fromEnv = import.meta.env.VITE_HUB_URL;
  if (fromEnv) return fromEnv.replace(/\/+$/, '');
  if (typeof window !== 'undefined' && window.location?.origin?.startsWith('http')) {
    return window.location.origin;
  }
  return 'http://localhost:8080';
}

export function normalizeHubUrl(url: string): string {
  const trimmed = url.trim().replace(/\/+$/, '');
  if (!trimmed) return defaultHubUrl();
  if (!/^https?:\/\//i.test(trimmed)) return `https://${trimmed}`;
  return trimmed;
}

export function loadSettings(): HubSettings {
  const fallback: HubSettings = { hubUrl: defaultHubUrl(), token: '' };
  try {
    const raw = window.localStorage.getItem(KEY);
    if (!raw) return fallback;
    const parsed = JSON.parse(raw) as Partial<HubSettings>;
    return {
      hubUrl: typeof parsed.hubUrl === 'string' && parsed.hubUrl ? normalizeHubUrl(parsed.hubUrl) : fallback.hubUrl,
      token: typeof parsed.token === 'string' ? parsed.token : '',
    };
  } catch {
    return fallback;
  }
}

/** Returns false when the browser refused to persist (the settings still apply for this session). */
export function saveSettings(settings: HubSettings): boolean {
  try {
    window.localStorage.setItem(KEY, JSON.stringify({ ...settings, hubUrl: normalizeHubUrl(settings.hubUrl) }));
    return true;
  } catch {
    return false;
  }
}

export function readPref<T>(key: string, fallback: T): T {
  try {
    const raw = window.localStorage.getItem(`omnifree.pref.${key}`);
    return raw === null ? fallback : (JSON.parse(raw) as T);
  } catch {
    return fallback;
  }
}

export function writePref<T>(key: string, value: T): void {
  try {
    window.localStorage.setItem(`omnifree.pref.${key}`, JSON.stringify(value));
  } catch {
    // Preference persistence is a convenience; a storage failure only loses the remembered value.
  }
}
