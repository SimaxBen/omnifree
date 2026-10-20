import { useState } from 'react';
import { Segmented } from '../components/ui';
import { readPref, writePref } from '../lib/settings';
import SchedulesView from './SchedulesView';
import SwitchboardView from './SwitchboardView';

type Section = 'switchboard' | 'schedules';

/** The Plugins tab: the provider switchboard, and the cron jobs those providers run (Schedules). */
export default function PluginsView() {
  const [section, setSection] = useState<Section>(() => {
    const saved = readPref<string>('plugins.section', 'switchboard');
    return saved === 'schedules' ? 'schedules' : 'switchboard';
  });
  const choose = (s: Section) => {
    setSection(s);
    writePref('plugins.section', s);
  };
  return (
    <div className="space-y-4">
      <Segmented
        value={section}
        onChange={choose}
        options={[
          { value: 'switchboard', label: 'Switchboard' },
          { value: 'schedules', label: 'Schedules' },
        ]}
      />
      {section === 'switchboard' ? <SwitchboardView /> : <SchedulesView />}
    </div>
  );
}
