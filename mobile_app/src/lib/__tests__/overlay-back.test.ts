// @vitest-environment jsdom
import { describe, expect, it } from 'vitest';
import { backStackDepth, backTo, isLive, pushBackEntry, releaseBackEntry } from '../overlay';

const settle = () => new Promise((r) => setTimeout(r, 40));

function backGesture(): Promise<unknown> {
  window.history.back();
  return settle();
}

describe('stacked overlays and the back gesture', () => {
  it('closes only the top overlay per back gesture (zip inside zip inside the viewer)', async () => {
    const closed: string[] = [];
    const startLen = window.history.length;
    const viewer = pushBackEntry(() => closed.push('viewer'));
    const zip1 = pushBackEntry(() => closed.push('zip1'));
    pushBackEntry(() => closed.push('zip2'));
    expect(window.history.length).toBe(startLen + 3);

    await backGesture();
    expect(closed).toEqual(['zip2']);
    await backGesture();
    expect(closed).toEqual(['zip2', 'zip1']);
    expect(isLive(viewer)).toBe(true);
    expect(isLive(zip1)).toBe(false);
    await backGesture();
    expect(closed).toEqual(['zip2', 'zip1', 'viewer']);
    expect(backStackDepth()).toBe(0);
  });

  it('a UI close pops its entry (and those above) without closing what is below', async () => {
    const closed: string[] = [];
    const viewer = pushBackEntry(() => closed.push('viewer'));
    const nested = pushBackEntry(() => closed.push('nested'));
    let after = false;
    backTo(nested, () => {
      after = true;
    });
    await settle();
    expect(closed).toEqual(['nested']);
    expect(after).toBe(true);
    expect(isLive(viewer)).toBe(true);

    // Closing the viewer while a nested overlay is still open (Escape key) pops both entries.
    pushBackEntry(() => closed.push('nested2'));
    backTo(viewer);
    await settle();
    expect(closed).toEqual(['nested', 'nested2', 'viewer']);
    expect(backStackDepth()).toBe(0);
  });

  it('an overlay unmounted under another is skipped by the next back gesture', async () => {
    const closed: string[] = [];
    const lower = pushBackEntry(() => closed.push('lower'));
    const middle = pushBackEntry(() => closed.push('middle'));
    pushBackEntry(() => closed.push('top'));
    releaseBackEntry(middle); // e.g. its parent re-rendered it away
    expect(isLive(middle)).toBe(false);
    await backGesture(); // closes top, then drops the dead middle entry by itself
    expect(closed).toEqual(['top']);
    expect(isLive(lower)).toBe(true);
    await backGesture();
    expect(closed).toEqual(['top', 'lower']);
    expect(backStackDepth()).toBe(0);
  });

  it('releasing the top overlay pops its history entry without closing anything else', async () => {
    const closed: string[] = [];
    const lower = pushBackEntry(() => closed.push('lower'));
    const top = pushBackEntry(() => closed.push('top'));
    releaseBackEntry(top);
    await settle();
    expect(closed).toEqual([]);
    expect(isLive(lower)).toBe(true);
    await backGesture();
    expect(closed).toEqual(['lower']);
  });
});
