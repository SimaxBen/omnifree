import { useEffect, useMemo, useState } from 'react';
import { Sheet } from '../components/Sheet';
import { IconButton, Segmented } from '../components/ui';
import { useNav } from '../lib/nav';
import { useScreenAction } from '../lib/screenAction';
import { writePref } from '../lib/settings';
import AgentPanel from './llm/AgentPanel';
import ChatPanel from './llm/ChatPanel';
import QuotaPanel from './llm/QuotaPanel';
import TranscribePanel from './llm/TranscribePanel';

type Panel = 'chat' | 'transcribe' | 'agent';
const PANELS: Panel[] = ['chat', 'transcribe', 'agent'];

/** The LLM pool: chat (model picker and history inside), speech to text and the browser agent; quotas open from the title line. */
export default function AiView() {
  const { route, setSub } = useNav();
  const panel: Panel = PANELS.includes(route.sub as Panel) ? (route.sub as Panel) : 'chat';
  // Old links to #/ai/quotas open the quota sheet over the chat.
  const [quotasOpen, setQuotasOpen] = useState(route.sub === 'quotas');

  useEffect(() => {
    if (route.sub === 'quotas') {
      setQuotasOpen(true);
      setSub('');
    }
  }, [route.sub, setSub]);

  const action = useMemo(
    () => (
      <IconButton
        icon="gauge"
        label="Provider quotas"
        aria-haspopup="dialog"
        aria-expanded={quotasOpen}
        onClick={() => setQuotasOpen(true)}
        className={quotasOpen ? 'text-signal' : ''}
      />
    ),
    [quotasOpen],
  );
  useScreenAction(action);

  return (
    <div className="space-y-4">
      <Segmented
        value={panel}
        onChange={(p) => setSub(p === 'chat' ? '' : p)}
        options={[
          { value: 'chat', label: 'Chat' },
          { value: 'transcribe', label: 'Voice' },
          { value: 'agent', label: 'Agent' },
        ]}
      />
      <div key={panel} className="screen-in-up">
        {panel === 'chat' && <ChatPanel />}
        {panel === 'transcribe' && (
          <TranscribePanel
            onSend={(text) => {
              writePref('chat.draft', text);
              setSub('');
            }}
          />
        )}
        {panel === 'agent' && <AgentPanel />}
      </div>
      <Sheet open={quotasOpen} onClose={() => setQuotasOpen(false)} title="Quotas" full>
        {/* The sheet renders its children only while open (or closing), so quotas poll only then. */}
        <QuotaPanel />
      </Sheet>
    </div>
  );
}
