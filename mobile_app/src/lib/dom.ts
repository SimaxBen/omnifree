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
