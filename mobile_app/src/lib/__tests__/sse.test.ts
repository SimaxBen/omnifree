import { describe, expect, it } from 'vitest';
import { SSEParser, readSSE } from '../sse';

describe('SSEParser', () => {
  it('parses messages split across arbitrary chunks', () => {
    const p = new SSEParser();
    const out = [...p.feed('data: {"delta":"he'), ...p.feed('llo"}\n'), ...p.feed('\ndata: [DONE]\n\n')];
    expect(out.map((m) => m.data)).toEqual(['{"delta":"hello"}', '[DONE]']);
    expect(out[0].event).toBe('message');
  });

  it('handles CRLF, comments, named events and multi-line data', () => {
    const p = new SSEParser();
    const out = p.feed(': keepalive\r\nevent: error\r\ndata: line1\r\ndata: line2\r\nid: 7\r\n\r\n');
    expect(out).toEqual([{ event: 'error', data: 'line1\nline2', id: '7' }]);
  });

  it('waits when a chunk ends on a lone CR', () => {
    const p = new SSEParser();
    expect(p.feed('data: a\r')).toEqual([]);
    expect(p.feed('\n\r\n').map((m) => m.data)).toEqual(['a']);
  });

  it('flushes an unterminated trailing message on end()', () => {
    const p = new SSEParser();
    expect(p.feed('data: tail')).toEqual([]);
    expect(p.end().map((m) => m.data)).toEqual(['tail']);
  });
});

describe('readSSE', () => {
  it('reads a ReadableStream of bytes', async () => {
    const enc = new TextEncoder();
    const body = new ReadableStream<Uint8Array>({
      start(c) {
        c.enqueue(enc.encode('data: {"delta":"a"}\n\nda'));
        c.enqueue(enc.encode('ta: {"delta":"b"}\n\n'));
        c.close();
      },
    });
    const got: string[] = [];
    for await (const m of readSSE(body)) got.push(m.data);
    expect(got).toEqual(['{"delta":"a"}', '{"delta":"b"}']);
  });
});
