import { useState } from 'react';
import { Segmented } from '../../components/ui';
import { readPref, writePref } from '../../lib/settings';
import AgentPanel from './AgentPanel';
import ChatPanel from './ChatPanel';
import QuotaPanel from './QuotaPanel';

type Panel = 'chat' | 'quotas' | 'agent';

export default function LlmView() {
  const [panel, setPanel] = useState<Panel>(() => readPref<Panel>('llm.panel', 'chat'));
  const choose = (p: Panel) => {
    setPanel(p);
    writePref('llm.panel', p);
  };
  return (
    <div className="space-y-4">
      <Segmented
        value={panel}
        onChange={choose}
        options={[
          { value: 'chat', label: 'Chat' },
          { value: 'quotas', label: 'Quotas' },
          { value: 'agent', label: 'Agent' },
        ]}
      />
      {panel === 'chat' && <ChatPanel />}
      {panel === 'quotas' && <QuotaPanel />}
      {panel === 'agent' && <AgentPanel />}
    </div>
  );
}
