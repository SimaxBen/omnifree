// DOM helpers that are safe to call from React effects.

/**
 * Scrolls `el` into view at the bottom edge. Always returns undefined: recent Chromium makes
 * Element.scrollIntoView() return a Promise, and a value leaking out of a concise-arrow
 * useEffect becomes the effect cleanup, which React then calls as a function.
 */
export function scrollToEnd(el: Pick<Element, 'scrollIntoView'> | null | undefined): void {
  if (!el) return;
  try {
    const result: unknown = el.scrollIntoView({ behavior: 'smooth', block: 'end' });
    // An interrupted smooth scroll may reject the returned Promise; that is not an error.
    if (result instanceof Promise) result.catch(() => undefined);
  } catch {
    // Scrolling is cosmetic.
  }
}

/**
 * Copies `text` to the clipboard: the async Clipboard API when available (secure contexts), else a
 * hidden textarea and document.execCommand('copy'). Resolves to whether the copy succeeded.
 */
export async function copyText(text: string): Promise<boolean> {
  try {
    if (typeof navigator !== 'undefined' && navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // Permission denied or insecure context: try the legacy path.
  }
  if (typeof document === 'undefined') return false;
  const area = document.createElement('textarea');
  area.value = text;
  area.setAttribute('readonly', '');
  area.style.position = 'fixed';
  area.style.opacity = '0';
  document.body.appendChild(area);
  area.select();
  try {
    return document.execCommand('copy');
  } catch {
    return false;
  } finally {
    area.remove();
  }
}
