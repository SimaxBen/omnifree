import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { SHORTCUTS, parseLaunch, shortcutIcon, shortcutUrl, stripLaunch } from '../launch';

const ROOT = join(__dirname, '..', '..', '..');

describe('app icon shortcuts', () => {
  it('lists the five captures, each with a generated icon', () => {
    expect(SHORTCUTS.map((s) => s.id)).toEqual(['voice', 'receipt', 'note', 'task', 'money']);
    for (const s of SHORTCUTS) {
      expect(existsSync(join(ROOT, 'public', shortcutIcon(s.id)))).toBe(true);
      expect(parseLaunch(shortcutUrl(s.id).slice(1))).toBe(s.id);
    }
  });

  it('reads and strips the launch parameter', () => {
    expect(parseLaunch('?shortcut=voice&x=1')).toBe('voice');
    expect(parseLaunch('?shortcut=nope')).toBeNull();
    expect(parseLaunch('')).toBeNull();
    expect(stripLaunch('/', '?shortcut=task', '#/home')).toBe('/#/home');
    expect(stripLaunch('/', '?a=1&shortcut=task', '')).toBe('/?a=1');
  });

  it('is wired into the web manifest next to the share target', () => {
    const config = readFileSync(join(ROOT, 'vite.config.ts'), 'utf8');
    expect(config).toContain('shortcuts: SHORTCUTS.map');
    expect(config).toContain("action: '/share-target'");
    expect(config).toContain("enctype: 'multipart/form-data'");
    expect(config).toContain("importScripts: ['push-sw.js', 'share-sw.js']");
  });
});
