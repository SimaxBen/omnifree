// Minimal Server-Sent Events parser for fetch() streams (EventSource cannot POST or send headers).

export interface SSEMessage {
  event: string;
  data: string;
  id?: string;
}

/**
 * Incremental parser: feed it decoded text, it returns complete messages.
 * Follows the WHATWG field rules for `event`, `data`, `id` and comment lines.
 */
export class SSEParser {
  private buffer = '';
  private event = '';
  private data: string[] = [];
  private id: string | undefined;

  feed(text: string): SSEMessage[] {
    this.buffer += text;
    const out: SSEMessage[] = [];
    let idx: number;
    while ((idx = this.nextLineBreak()) >= 0) {
      const line = this.buffer.slice(0, idx);
      const skip = this.buffer[idx] === '\r' && this.buffer[idx + 1] === '\n' ? 2 : 1;
      this.buffer = this.buffer.slice(idx + skip);
      const msg = this.line(line);
      if (msg) out.push(msg);
    }
    return out;
  }

  /** Flush a trailing message that was not terminated by a blank line. */
  end(): SSEMessage[] {
    const out = this.buffer ? this.feed('\n') : [];
    const msg = this.line('');
    if (msg) out.push(msg);
    return out;
  }

  private nextLineBreak(): number {
    const n = this.buffer.indexOf('\n');
    const r = this.buffer.indexOf('\r');
    if (r >= 0 && r === this.buffer.length - 1) {
      // A lone trailing CR may be the first half of CRLF; wait for more input.
      return n >= 0 && n < r ? n : -1;
    }
    if (n < 0) return r;
    if (r < 0) return n;
    return Math.min(n, r);
  }

  private line(line: string): SSEMessage | null {
    if (line === '') {
      if (this.data.length === 0) {
        this.event = '';
        return null;
      }
      const msg: SSEMessage = { event: this.event || 'message', data: this.data.join('\n'), id: this.id };
      this.event = '';
      this.data = [];
      return msg;
    }
    if (line.startsWith(':')) return null;
    const colon = line.indexOf(':');
    const field = colon >= 0 ? line.slice(0, colon) : line;
    let value = colon >= 0 ? line.slice(colon + 1) : '';
    if (value.startsWith(' ')) value = value.slice(1);
    if (field === 'event') this.event = value;
    else if (field === 'data') this.data.push(value);
    else if (field === 'id') this.id = value;
    return null;
  }
}

export async function* readSSE(body: ReadableStream<Uint8Array>, signal?: AbortSignal): AsyncGenerator<SSEMessage> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  const parser = new SSEParser();
  try {
    while (true) {
      if (signal?.aborted) return;
      const { done, value } = await reader.read();
      if (done) break;
      for (const msg of parser.feed(decoder.decode(value, { stream: true }))) yield msg;
    }
    for (const msg of parser.feed(decoder.decode())) yield msg;
    for (const msg of parser.end()) yield msg;
  } finally {
    reader.releaseLock();
  }
}
