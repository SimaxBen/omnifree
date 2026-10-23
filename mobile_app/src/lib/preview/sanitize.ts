// HTML from documents (mammoth's .docx output, the .odt converter) is untrusted: DOMPurify with a
// strict profile. No script, no event handlers, no frames/forms/embeds/SVG/MathML, no inline styles,
// pictures only as data: images (never fetched from the network), links open outside the app.

import createDOMPurify, { type Config, type DOMPurify } from 'dompurify';

export const DOC_PURIFY_CONFIG: Config = {
  USE_PROFILES: { html: true },
  FORBID_TAGS: [
    'script', 'style', 'iframe', 'frame', 'frameset', 'object', 'embed', 'applet', 'form', 'input', 'button', 'textarea',
    'select', 'option', 'link', 'meta', 'base', 'svg', 'math', 'video', 'audio', 'source', 'track', 'template', 'noscript',
  ],
  FORBID_ATTR: ['style', 'srcset', 'action', 'formaction', 'background', 'ping', 'poster'],
  ALLOW_DATA_ATTR: false,
  ALLOWED_URI_REGEXP: /^(?:https?:|mailto:|tel:|#|data:image\/(?:png|jpe?g|gif|webp|bmp);base64,)/i,
};

let instance: DOMPurify | null = null;

function purifier(): DOMPurify {
  if (instance) return instance;
  const p = createDOMPurify(window);
  p.addHook('afterSanitizeAttributes', (node) => {
    const el = node as Element;
    if (el.tagName === 'IMG') {
      const src = el.getAttribute('src') ?? '';
      // External or relative pictures would be fetched (tracking pixels): only embedded data: images stay.
      if (!/^data:image\/(?:png|jpe?g|gif|webp|bmp);base64,/i.test(src)) el.remove();
      else el.setAttribute('loading', 'lazy');
    }
    if (el.tagName === 'A') {
      const href = el.getAttribute('href') ?? '';
      if (href.startsWith('#')) return;
      if (/^data:/i.test(href)) {
        el.removeAttribute('href');
        return;
      }
      el.setAttribute('target', '_blank');
      el.setAttribute('rel', 'noopener noreferrer nofollow');
    }
  });
  instance = p;
  return p;
}

export function sanitizeDocHtml(html: string): string {
  return purifier().sanitize(html, { ...DOC_PURIFY_CONFIG, RETURN_TRUSTED_TYPE: false }) as string;
}
