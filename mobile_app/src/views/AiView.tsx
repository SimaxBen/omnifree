import { Segmented } from '../components/ui';
import { useNav } from '../lib/nav';
import { writePref } from '../lib/settings';
import AgentPanel from './llm/AgentPanel';
import ChatPanel from './llm/ChatPanel';
import QuotaPanel from './llm/QuotaPanel';
import TranscribePanel from './llm/TranscribePanel';

type Panel = 'chat' | 'transcribe' | 'quotas' | 'agent';
const PANELS: Panel[] = ['chat', 'transcribe', 'quotas', 'agent'];

/** The LLM pool: chat (model picker inside), speech to text, per-provider quotas and the browser agent. */
export default function AiView() {
  const { route, setSub } = useNav();
  const panel: Panel = PANELS.includes(route.sub as Panel) ? (route.sub as Panel) : 'chat';
  return (
    <div className="space-y-4">
      <Segmented
        value={panel}
        onChange={(p) => setSub(p === 'chat' ? '' : p)}
        options={[
          { value: 'chat', label: 'Chat' },
          { value: 'transcribe', label: 'Voice' },
          { value: 'quotas', label: 'Quotas' },
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
        {panel === 'quotas' && <QuotaPanel />}
        {panel === 'agent' && <AgentPanel />}
      </div>
    </div>
  );
}
