// Helpers for the "Connect Claude" page: the MCP endpoint of the hub and ready-to-paste client configs.

export type McpClientId = 'code' | 'desktop' | 'cursor';

export const TOKEN_PLACEHOLDER = '<token>';

/** The hub serves MCP at <hubUrl>/mcp (works through the edge /api prefix as well). */
export function mcpUrl(hubUrl: string): string {
  return `${hubUrl.trim().replace(/\/+$/, '')}/mcp`;
}

/** Text to paste for one client. `token` is inserted only when given, otherwise a placeholder. */
export function clientSnippet(client: McpClientId, url: string, token?: string): string {
  const t = token?.trim() || TOKEN_PLACEHOLDER;
  switch (client) {
    case 'code':
      return `claude mcp add --transport http --scope user omnifree ${url} --header "Authorization: Bearer ${t}"`;
    case 'cursor':
      return JSON.stringify({ mcpServers: { omnifree: { url, headers: { Authorization: `Bearer ${t}` } } } }, null, 2);
    case 'desktop':
      return JSON.stringify(
        {
          mcpServers: {
            omnifree: {
              command: 'npx',
              args: ['-y', 'mcp-remote', url, '--transport', 'http-only', '--header', 'Authorization:${OMNIFREE_AUTH}'],
              env: { OMNIFREE_AUTH: `Bearer ${t}` },
            },
          },
        },
        null,
        2,
      );
  }
}

export interface McpProbe {
  server: string;
  version: string;
  tools: string[];
}

async function rpc(url: string, token: string, id: number, method: string, params: object = {}): Promise<Record<string, unknown>> {
  const res = await fetch(url, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
      Accept: 'application/json, text/event-stream',
      'Mcp-Protocol-Version': '2025-11-25',
    },
    body: JSON.stringify({ jsonrpc: '2.0', id, method, params }),
  });
  if (res.status === 401) throw new Error('Token refused (401): check the token in Settings');
  if (!res.ok) throw new Error(`MCP endpoint answered HTTP ${res.status}`);
  const body = (await res.json()) as { result?: Record<string, unknown>; error?: { message?: string } };
  if (body.error) throw new Error(body.error.message || 'MCP error');
  return body.result ?? {};
}

/** Talks to the MCP endpoint like a client would: initialize, then tools/list. */
export async function probeMcp(url: string, token: string): Promise<McpProbe> {
  const init = await rpc(url, token, 1, 'initialize', {
    protocolVersion: '2025-11-25',
    capabilities: {},
    clientInfo: { name: 'omnifree-pwa', version: '1' },
  });
  const info = (init.serverInfo ?? {}) as { name?: string; version?: string };
  const list = await rpc(url, token, 2, 'tools/list');
  const tools = ((list.tools ?? []) as { name: string }[]).map((t) => t.name);
  return { server: info.name ?? 'omnifree', version: info.version ?? '', tools };
}
