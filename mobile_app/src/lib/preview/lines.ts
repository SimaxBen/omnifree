// Splits highlighter output into per-line HTML (for line numbers and wrapping): spans left open at
// a line end are closed there and reopened on the next line. Input is highlight.js output, i.e.
// escaped text plus <span class="..."> / </span> only.

const TAG = /<span class="[^"<>]*">|<\/span>/g;

export function splitHighlighted(html: string): string[] {
  const out: string[] = [];
  const open: string[] = [];
  for (const raw of html.split('\n')) {
    const prefix = open.join('');
    TAG.lastIndex = 0;
    let m: RegExpExecArray | null;
    while ((m = TAG.exec(raw))) {
      if (m[0] === '</span>') open.pop();
      else open.push(m[0]);
    }
    out.push(prefix + raw + '</span>'.repeat(open.length));
  }
  return out;
}

/** Plain text to HTML-safe text (for the unhighlighted path). */
export function escapeHtml(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
