import { afterEach, describe, expect, it, vi } from 'vitest';
import { clientSnippet, mcpUrl, probeMcp, TOKEN_PLACEHOLDER } from '../mcp';

describe('mcp helpers', () => {
  it('builds the endpoint from the hub URL', () => {
    expect(mcpUrl('https://api.example.com/')).toBe('https://api.example.com/mcp');
    expect(mcpUrl('https://edge.example.com/api')).toBe('https://edge.example.com/api/mcp');
  });

  it('uses a placeholder unless a token is given', () => {
    const url = 'https://h/mcp';
    expect(clientSnippet('code', url)).toContain(`Bearer ${TOKEN_PLACEHOLDER}`);
    expect(clientSnippet('code', url, 'abc')).toBe(
      'claude mcp add --transport http --scope user omnifree https://h/mcp --header "Authorization: Bearer abc"',
    );
    const cursor = JSON.parse(clientSnippet('cursor', url, 'abc'));
    expect(cursor.mcpServers.omnifree).toEqual({ url, headers: { Authorization: 'Bearer abc' } });
    const desktop = JSON.parse(clientSnippet('desktop', url, 'abc'));
    expect(desktop.mcpServers.omnifree.args).toContain(url);
    expect(desktop.mcpServers.omnifree.env.OMNIFREE_AUTH).toBe('Bearer abc');
  });
});

describe('probeMcp', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('initializes then lists tools', async () => {
    const calls: string[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (_url: string, init: RequestInit) => {
        const req = JSON.parse(String(init.body));
        calls.push(req.method);
        const result = req.method === 'initialize' ? { serverInfo: { name: 'OmniFree', version: '0.3' } } : { tools: [{ name: 'llm_chat' }, { name: 'budget_status' }] };
        return new Response(JSON.stringify({ jsonrpc: '2.0', id: req.id, result }), { status: 200 });
      }),
    );
    const probe = await probeMcp('https://h/mcp', 'tok');
    expect(calls).toEqual(['initialize', 'tools/list']);
    expect(probe).toEqual({ server: 'OmniFree', version: '0.3', tools: ['llm_chat', 'budget_status'] });
  });

  it('explains a refused token', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('', { status: 401 })));
    await expect(probeMcp('https://h/mcp', 'bad')).rejects.toThrow(/401/);
  });
});
