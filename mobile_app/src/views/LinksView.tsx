import { useRef, useState, type FormEvent } from 'react';
import { Icon } from '../components/icons';
import { QrCode } from '../components/QrCode';
import { Sheet } from '../components/Sheet';
import { SwipeRow } from '../components/SwipeRow';
import { Badge, Button, Chip, EmptyState, ErrorNote, Segmented, SkeletonList, cx, haptic, inputClass, riseStyle } from '../components/ui';
import { HubError } from '../lib/api';
import { announce, useTopic } from '../lib/bus';
import { copyText } from '../lib/dom';
import { relativeTime } from '../lib/files';
import { formatDateTime } from '../lib/format';
import { errorText, useHub, useHubData } from '../lib/hub';
import { usePullRefresh } from '../lib/nav';
import type { ShortLink } from '../lib/types';
import DropsView from './drive/DropsView';

const EXPIRY = [
  { d: 0, label: 'Never' },
  { d: 1, label: '1 day' },
  { d: 7, label: '1 week' },
  { d: 30, label: '30 days' },
];

function LinkSheet({ open, link, onClose }: { open: boolean; link: ShortLink; onClose: () => void }) {
  const { notify } = useHub();
  const copy = async () => {
    const ok = await copyText(link.short_url);
    haptic(8);
    notify(ok ? 'Short link copied' : 'Copy failed', ok ? 'ok' : 'error');
  };
  return (
    <Sheet open={open} onClose={onClose} title={link.title || link.code}>
      <div className="flex flex-col items-center gap-4 pb-2 text-center">
        <QrCode value={link.short_url} size={220} />
        <p className="font-display text-[20px] font-semibold text-ink-100">{link.short_url.replace(/^https?:\/\//, '')}</p>
        <p className="max-w-full break-all text-[13px] text-ink-400">{link.url}</p>
        <div className="flex flex-wrap justify-center gap-1.5">
          <Badge tone="signal">
            {link.clicks} click{link.clicks === 1 ? '' : 's'}
          </Badge>
          <Badge>{link.expires_at ? `expires ${formatDateTime(link.expires_at)}` : 'never expires'}</Badge>
        </div>
        <div className="flex w-full gap-2">
          <Button variant="primary" icon="copy" className="flex-1" onClick={() => void copy()}>
            Copy
          </Button>
          <a href={link.url} target="_blank" rel="noopener noreferrer" className="press inline-flex min-h-[44px] flex-1 items-center justify-center gap-2 rounded-full bg-ink-800 px-4 text-[15px] text-ink-100">
            <Icon name="external" size={18} />
            Open
          </a>
        </div>
      </div>
    </Sheet>
  );
}

function ShortLinks() {
  const { client, notify } = useHub();
  const links = useHubData((c, s) => c.links(s), []);
  const [url, setUrl] = useState('');
  const [code, setCode] = useState('');
  const [title, setTitle] = useState('');
  const [days, setDays] = useState(0);
  const [more, setMore] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fresh, setFresh] = useState<string | null>(null);
  const [showing, setShowing] = useState<{ id: number; open: boolean; link: ShortLink } | null>(null);
  const ids = useRef(1);
  useTopic(['links'], links.reload);
  usePullRefresh(links.reload);

  const create = async (e: FormEvent) => {
    e.preventDefault();
    let target = url.trim();
    if (!target) return;
    if (!/^https?:\/\//i.test(target)) target = `https://${target}`;
    setBusy(true);
    setError(null);
    try {
      const link = await client.createLink({
        url: target,
        ...(code.trim() ? { code: code.trim() } : {}),
        ...(title.trim() ? { title: title.trim() } : {}),
        ...(days ? { expires_in_days: days } : {}),
      });
      haptic([10, 40, 10]);
      links.setData((ls) => [link, ...(ls ?? [])]);
      setFresh(link.code);
      setUrl('');
      setCode('');
      setTitle('');
      announce('links');
      const ok = await copyText(link.short_url);
      notify(ok ? `Created and copied ${link.short_url.replace(/^https?:\/\//, '')}` : 'Short link created', 'ok');
      setShowing({ id: ids.current++, open: true, link });
    } catch (err) {
      setError(err instanceof HubError && err.status === 409 ? `The code “${code.trim()}” is taken. Pick another, or leave it empty for a random one.` : errorText(err));
    } finally {
      setBusy(false);
    }
  };

  const remove = async (l: ShortLink) => {
    try {
      await client.deleteLink(l.code);
      links.setData((ls) => ls?.filter((x) => x.code !== l.code));
      announce('links');
      return true;
    } catch (err) {
      notify(errorText(err), 'error');
      return false;
    }
  };

  const list = links.data ?? [];
  return (
    <div className="space-y-5">
      <form onSubmit={(e) => void create(e)} className="space-y-3 rounded-card bg-ink-900 p-3 ring-1 ring-inset ring-ink-800">
        <div className="flex gap-2">
          <input
            type="url"
            inputMode="url"
            aria-label="Long URL"
            autoCapitalize="off"
            autoCorrect="off"
            spellCheck={false}
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="Paste a long link"
            className={cx(inputClass, 'min-w-0 flex-1')}
          />
          <Button variant="primary" type="submit" disabled={!url.trim() || busy}>
            {busy ? '…' : 'Shorten'}
          </Button>
        </div>
        <button type="button" onClick={() => setMore((v) => !v)} className="press flex min-h-[36px] items-center gap-1 px-1 text-[13px] text-ink-400">
          <Icon name={more ? 'up' : 'down'} size={14} />
          Custom code, title and expiry
        </button>
        {more && (
          <div className="screen-in-up space-y-3">
            <div className="flex gap-2">
              <input aria-label="Custom code" value={code} onChange={(e) => setCode(e.target.value.replace(/[^A-Za-z0-9_-]/g, ''))} placeholder="code (optional)" className={cx(inputClass, 'w-36')} />
              <input aria-label="Title" value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Title (optional)" className={cx(inputClass, 'min-w-0 flex-1')} />
            </div>
            <div className="flex flex-wrap gap-2">
              {EXPIRY.map((x) => (
                <Chip key={x.d} active={days === x.d} onClick={() => setDays(x.d)}>
                  {x.label}
                </Chip>
              ))}
            </div>
          </div>
        )}
        {error && <ErrorNote message={error} />}
      </form>

      {links.error && <ErrorNote message={links.error} onRetry={links.reload} />}
      {links.loading && !links.data && <SkeletonList rows={3} />}
      {links.data && !list.length && <EmptyState icon="link" title="No short links yet" hint="Shorten a link to get a QR code and a click count." />}
      <div className="space-y-2">
        {list.map((l, i) => (
          <div key={l.code} className={fresh === l.code ? 'pop' : 'rise'} style={riseStyle(i)}>
            <SwipeRow end={{ icon: 'trash', label: 'Delete', tone: 'danger', run: () => remove(l) }}>
              <button type="button" onClick={() => setShowing({ id: ids.current++, open: true, link: l })} className="flex min-h-[64px] w-full items-center gap-3 px-3 py-2 text-left">
                <span className="grid h-10 w-10 shrink-0 place-items-center rounded-full bg-signal/15 text-signal">
                  <Icon name="qr" size={18} />
                </span>
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-[15px] text-ink-100">{l.title || `/${l.code}`}</span>
                  <span className="block truncate text-[12px] text-ink-500">{l.url.replace(/^https?:\/\//, '')}</span>
                </span>
                <span className="shrink-0 text-right">
                  <span className="block font-display text-[17px] font-semibold text-ink-100">{l.clicks}</span>
                  <span className="block text-[11px] text-ink-500">{relativeTime(l.created_at)}</span>
                </span>
              </button>
            </SwipeRow>
          </div>
        ))}
      </div>
      {showing && <LinkSheet key={showing.id} open={showing.open} link={showing.link} onClose={() => setShowing((s) => (s ? { ...s, open: false } : s))} />}
    </div>
  );
}

export default function LinksView() {
  const [section, setSection] = useState<'links' | 'drops'>('links');
  return (
    <div className="space-y-4">
      <Segmented
        value={section}
        onChange={setSection}
        options={[
          { value: 'links', label: 'Short links' },
          { value: 'drops', label: 'File drops' },
        ]}
      />
      <div key={section} className="screen-in-up">
        {section === 'links' ? <ShortLinks /> : <DropsView />}
      </div>
    </div>
  );
}
