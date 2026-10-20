import { useState } from 'react';
import { Badge, Button, Card, SectionTitle, Segmented, StatusDot, Toggle, riseStyle } from '../components/ui';
import { copyText } from '../lib/dom';
import { errorText, useHub } from '../lib/hub';
import { clientSnippet, mcpUrl, probeMcp, type McpClientId, type McpProbe } from '../lib/mcp';

const CLIENTS: { value: McpClientId; label: string }[] = [
  { value: 'code', label: 'Claude Code' },
  { value: 'desktop', label: 'Desktop' },
  { value: 'cursor', label: 'Cursor' },
];

const WHERE: Record<McpClientId, string[]> = {
  code: [
    'Open a terminal (PowerShell, Git Bash, or a macOS/Linux shell).',
    'Paste the command and press Enter. --scope user makes OmniFree available in every project.',
    'Start claude and type /mcp. "omnifree" should show as connected.',
  ],
  desktop: [
    'Claude Desktop > Settings > Developer > Edit config. On Windows the file is %APPDATA%\\Claude\\claude_desktop_config.json.',
    'Merge this block into the file and keep your other servers. npx needs Node.js 18 or later.',
    'Quit Claude Desktop completely and reopen it. The tools appear under the tools icon in the chat box.',
  ],
  cursor: [
    'Open ~/.cursor/mcp.json (all projects) or .cursor/mcp.json (one project).',
    'Merge this block into the file.',
    'In Cursor Settings > MCP, "omnifree" should turn green.',
  ],
};

const TOOL_GROUPS: { title: string; tools: string; ask: string }[] = [
  { title: 'AI pool', tools: 'llm_chat, llm_models', ask: 'Ask the free model pool to translate this paragraph into Darija.' },
  { title: 'Drive', tools: 'drive_list, drive_search, drive_read_text, drive_upload_text, drive_share_link', ask: 'Find my CV in the Drive and give me a share link that lasts one day.' },
  { title: 'Ask my Drive', tools: 'ask_drive, rag_index', ask: 'What does my lease say about the deposit? Cite the page.' },
  { title: 'Notes & tasks', tools: 'notes_search, notes_get, notes_create, tasks_list, tasks_add, tasks_complete', ask: 'Turn this meeting summary into tasks due this week.' },
  { title: 'Money', tools: 'expenses_month, expense_add', ask: 'How much did I spend on groceries this month?' },
  { title: 'Links & schedules', tools: 'links_create, drops_create, schedules_list, schedule_create', ask: 'Make a short link for this URL that expires in 7 days.' },
  { title: 'Budget', tools: 'budget_status', ask: 'Which free tiers are close to their limit?' },
];

type TestState = { state: 'idle' } | { state: 'testing' } | { state: 'ok'; probe: McpProbe } | { state: 'fail'; detail: string };

export default function ConnectClaudeView() {
  const { settings, notify } = useHub();
  const [client, setClient] = useState<McpClientId>('code');
  const [withToken, setWithToken] = useState(false);
  const [test, setTest] = useState<TestState>({ state: 'idle' });
  const url = mcpUrl(settings.hubUrl);
  const snippet = clientSnippet(client, url, withToken ? settings.token : undefined);

  const copy = async (text: string, what: string) => {
    const ok = await copyText(text);
    notify(ok ? `${what} copied` : 'Copy failed: select the text and copy it manually', ok ? 'ok' : 'error');
  };

  const runTest = async () => {
    setTest({ state: 'testing' });
    try {
      setTest({ state: 'ok', probe: await probeMcp(url, settings.token) });
    } catch (err) {
      setTest({ state: 'fail', detail: errorText(err) });
    }
  };

  let n = 0;
  return (
    <div className="space-y-5">
      <div className="rise" style={riseStyle(n++)}>
        <Card className="space-y-2 p-4">
          <p className="font-display text-lg font-semibold text-ink-100">Use OmniFree from Claude</p>
          <p className="text-sm text-ink-300">
            OmniFree runs an MCP server. Once it is connected, Claude can read your Drive, add notes and tasks, log
            expenses, make short links and chat through your free model pool. None of its tools delete data.
          </p>
          <div className="flex items-center gap-2 pt-1">
            <code className="min-w-0 flex-1 truncate rounded-lg bg-ink-950 px-2 py-1.5 font-mono text-[13px] text-signal">{url}</code>
            <Button variant="ghost" icon="copy" aria-label="Copy MCP address" onClick={() => copy(url, 'Address')} />
          </div>
        </Card>
      </div>

      <section className="rise space-y-2" style={riseStyle(n++)}>
        <SectionTitle>1 · Pick your app</SectionTitle>
        <Segmented value={client} options={CLIENTS} onChange={setClient} />
      </section>

      <section className="rise space-y-2" style={riseStyle(n++)}>
        <SectionTitle>2 · Copy this</SectionTitle>
        <Card className="space-y-3 p-3">
          <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-ink-950 p-3 font-mono text-[12.5px] leading-relaxed text-ink-100">
            {snippet}
          </pre>
          <div className="flex items-center justify-between gap-3 text-sm text-ink-200">
            <span>Insert my hub token</span>
            <Toggle checked={withToken} onChange={setWithToken} label="Insert my hub token" disabled={!settings.token} />
          </div>
          <p className="text-xs text-ink-400">
            {withToken
              ? 'The hub token opens the whole hub. It is fine on your own computer, but never paste it into a shared file or a repo.'
              : 'Replace <token> with your agent token, which only opens /mcp and can be revoked on its own. To get it, run python -m core.vault get MCP_API_TOKEN in the omnifree folder on your PC. Or turn on the switch above to use your hub token.'}
          </p>
          <Button variant="primary" icon="copy" className="w-full" onClick={() => copy(snippet, client === 'code' ? 'Command' : 'Config')}>
            Copy
          </Button>
        </Card>
      </section>

      <section className="rise space-y-2" style={riseStyle(n++)}>
        <SectionTitle>3 · Paste it</SectionTitle>
        <Card className="p-3">
          <ol className="space-y-2.5">
            {WHERE[client].map((step, i) => (
              <li key={step} className="flex gap-3 text-sm text-ink-200">
                <span className="grid h-6 w-6 shrink-0 place-items-center rounded-full bg-ink-800 font-display text-xs text-signal">{i + 1}</span>
                <span className="min-w-0 break-words">{step}</span>
              </li>
            ))}
          </ol>
        </Card>
      </section>

      <section className="rise space-y-2" style={riseStyle(n++)}>
        <SectionTitle>4 · Check the server</SectionTitle>
        <Card className="space-y-3 p-3">
          <Button icon="pulse" className="w-full" onClick={runTest} disabled={test.state === 'testing' || !settings.token}>
            {test.state === 'testing' ? 'Talking to /mcp…' : 'Test the MCP server'}
          </Button>
          {test.state === 'ok' && (
            <div className="space-y-2 text-sm">
              <p className="flex items-center gap-2 text-ink-100">
                <StatusDot tone="ok" /> {test.probe.server} {test.probe.version} answers with {test.probe.tools.length} tools
              </p>
              <div className="flex flex-wrap gap-1.5">
                {test.probe.tools.map((t) => (
                  <Badge key={t}>{t}</Badge>
                ))}
              </div>
            </div>
          )}
          {test.state === 'fail' && (
            <p className="flex items-center gap-2 text-sm text-danger-400">
              <StatusDot tone="bad" /> {test.detail}
            </p>
          )}
          {!settings.token && <p className="text-xs text-ink-400">Add your hub token in Settings first.</p>}
        </Card>
      </section>

      <section className="rise space-y-2" style={riseStyle(n++)}>
        <SectionTitle>Things to ask Claude</SectionTitle>
        <div className="divide-y divide-ink-800 overflow-hidden rounded-card bg-ink-900 ring-1 ring-inset ring-ink-800">
          {TOOL_GROUPS.map((g) => (
            <button key={g.title} type="button" className="press block w-full p-3 text-left" onClick={() => copy(g.ask, 'Prompt')}>
              <p className="flex items-center justify-between font-display text-[15px] font-semibold text-ink-100">
                {g.title} <span className="font-sans text-xs font-normal text-ink-400">tap to copy</span>
              </p>
              <p className="mt-0.5 text-sm text-ink-200">“{g.ask}”</p>
              <p className="mt-1 break-words font-mono text-[11px] text-ink-400">{g.tools}</p>
            </button>
          ))}
        </div>
      </section>
    </div>
  );
}
