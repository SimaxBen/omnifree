import { Segmented } from '../../components/ui';
import { useNav } from '../../lib/nav';
import AskDriveView from './AskDriveView';
import DropsView from './DropsView';
import FilesView from './FilesView';

type Section = 'files' | 'ask' | 'drops';

export default function DriveScreen() {
  const { route, setSub } = useNav();
  const section: Section = route.sub === 'ask' || route.sub === 'drops' ? route.sub : 'files';
  return (
    <div className="space-y-4">
      <Segmented
        value={section}
        onChange={(v) => setSub(v === 'files' ? '' : v)}
        options={[
          { value: 'files', label: 'Files' },
          { value: 'ask', label: 'Ask' },
          { value: 'drops', label: 'Drops' },
        ]}
      />
      <div key={section} className="screen-in-up">
        {section === 'files' && <FilesView />}
        {section === 'ask' && <AskDriveView />}
        {section === 'drops' && <DropsView />}
      </div>
    </div>
  );
}
