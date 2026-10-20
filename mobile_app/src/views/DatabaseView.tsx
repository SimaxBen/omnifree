import { useEffect, useState, type KeyboardEvent } from 'react';
import { Badge, Button, Card, EmptyState, ErrorNote, IconButton, QuotaBar, SectionTitle, Spinner, Toggle, cx, inputClass } from '../components/ui';
import { cellText, formatBytes, formatMs, formatNumber } from '../lib/format';
import { errorText, useHub, useHubData } from '../lib/hub';
import { readPref, writePref } from '../lib/settings';
import type { DBProvider, QueryResult, TableInfo } from '../lib/types';

/** Starter statement for a table in the provider's native query language (see CONTRACT.md section 2). */
function templateFor(provider: DBProvider | undefined, table: TableInfo): string {
  const ident = table.schema_name ? `${table.schema_name}.${table.name}` : table.name;
  const sql = provider?.capabilities?.includes('sql');
  switch (table.kind) {
    case 'collection':
      if (provider?.name.includes('cloudant')) return JSON.stringify({ db: table.name, selector: {}, limit: 50 });
      return JSON.stringify({ find: table.name, filter: {}, limit: 50 });
    case 'keyspace':
      return `SCAN 0 MATCH ${table.name === '*' ? '*' : `${table.name}*`} COUNT 100`;
    case 'topic':
      return JSON.stringify({ consume: table.name, limit: 50 });
    default:
      return sql || provider?.name.includes('dynamo') ? `SELECT * FROM ${/[^A-Za-z0-9_.]/.test(ident) ? `"${ident}"` : ident}` : ident;
  }
}

export default function DatabaseView() {
  const { client, notify } = useHub();
  const providers = useHubData((c, s) => c.dbProviders(s));
  const [provider, setProvider] = useState<string>(() => readPref('db.provider', ''));
  const [statement, setStatement] = useState('');
  const [write, setWrite] = useState(false);
  const [limit, setLimit] = useState(200);
  const [result, setResult] = useState<QueryResult | null>(null);
  const [queryError, setQueryError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [selectedTable, setSelectedTable] = useState<TableInfo | null>(null);

  const enabled = (providers.data ?? []).filter((p) => p.enabled);
  const active = enabled.find((p) => p.name === provider);

  useEffect(() => {
    if (!active && enabled.length) setProvider(enabled[0].name);
  }, [active, enabled]);
  useEffect(() => {
    writePref('db.provider', provider);
    setResult(null);
    setQueryError(null);
    setSelectedTable(null);
  }, [provider]);

  const tables = useHubData((c, s) => (provider ? c.dbTables(provider, s) : Promise.resolve([] as TableInfo[])), [provider]);
  const usage = useHubData((c, s) => (provider ? c.dbUsage(provider, s) : Promise.resolve(undefined)), [provider]);

  const openTable = async (t: TableInfo) => {
    setStatement(templateFor(active, t));
    setWrite(false);
    setSelectedTable(t);
    if (t.columns.length === 0) {
      try {
        const described = await client.dbDescribe(provider, t.name);
        setSelectedTable(described);
      } catch (err) {
        notify(`Describe failed: ${errorText(err)}`, 'error');
      }
    }
  };

  const run = async () => {
    if (!statement.trim() || !provider || running) return;
    if (write && !window.confirm(`Run a write statement against ${provider}?`)) return;
    setRunning(true);
    setQueryError(null);
    try {
      setResult(await client.dbQuery(provider, statement.trim(), { write, limit }));
    } catch (err) {
      setResult(null);
      setQueryError(errorText(err));
    } finally {
      setRunning(false);
    }
  };

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      void run();
    }
  };

  return (
    <div className="space-y-5">
      {providers.error && <ErrorNote message={providers.error} onRetry={providers.reload} />}
      <Card className="space-y-3 p-3">
        <div className="flex gap-2">
          <select aria-label="Database provider" className={cx(inputClass, 'flex-1')} value={provider} onChange={(e) => setProvider(e.target.value)}>
            {enabled.length === 0 && <option value="">{providers.loading ? 'Loading...' : 'No enabled databases'}</option>}
            {enabled.map((p) => (
              <option key={p.name} value={p.name}>
                {p.display_name ? `${p.display_name} (${p.name})` : p.name}
              </option>
            ))}
          </select>
          <IconButton
            icon="refresh"
            label="Refresh tables"
            onClick={() => {
              tables.reload();
              usage.reload();
            }}
          />
        </div>
        {active?.missing_secrets && active.missing_secrets.length > 0 && (
          <p className="text-xs text-amber-400">Missing secrets: {active.missing_secrets.join(', ')}</p>
        )}
        {usage.data && <QuotaBar quota={usage.data} label="Storage used" />}
        {usage.error && <p className="text-xs text-ink-500">Usage unavailable: {usage.error}</p>}
      </Card>

      <div>
        <SectionTitle action={tables.loading && <Spinner />}>Tables {tables.data && <span className="text-ink-500">({tables.data.length})</span>}</SectionTitle>
        {tables.error && <ErrorNote message={tables.error} onRetry={tables.reload} />}
        {!tables.loading && !tables.error && provider && (tables.data ?? []).length === 0 && <EmptyState icon="table" title="No tables found" />}
        <div className="scroll-thin -mx-4 flex gap-2 overflow-x-auto px-4 pb-1">
          {(tables.data ?? []).map((t) => (
            <button
              key={`${t.schema_name ?? ''}.${t.name}`}
              type="button"
              onClick={() => void openTable(t)}
              className={cx(
                'shrink-0 rounded-lg border px-3 py-2 text-left transition-colors',
                selectedTable?.name === t.name ? 'border-signal/50 bg-ink-850' : 'border-ink-700 bg-ink-900 hover:border-ink-600',
              )}
            >
              <span className="block max-w-[11rem] truncate font-mono text-[12px] text-ink-100">{t.name}</span>
              <span className="text-[11px] text-ink-500">
                {t.kind}
                {t.row_count != null && ` · ${formatNumber(t.row_count)} rows`}
                {t.size_bytes != null && ` · ${formatBytes(t.size_bytes)}`}
              </span>
            </button>
          ))}
        </div>
        {selectedTable && selectedTable.columns.length > 0 && (
          <div className="mt-2 flex flex-wrap gap-1">
            {selectedTable.columns.map((c) => (
              <Badge key={c.name} tone={c.primary_key ? 'signal' : 'neutral'}>
                {c.name}
                <span className="text-ink-500">{c.type}</span>
              </Badge>
            ))}
          </div>
        )}
      </div>

      <Card className="space-y-2 p-3">
        <textarea
          aria-label="Query"
          className={cx(inputClass, 'min-h-[110px] py-2 font-mono text-[13px]')}
          placeholder={active?.capabilities?.includes('sql') ? 'SELECT ...' : 'Native statement (SQL, PartiQL, Redis command, Mongo/Mango JSON)'}
          spellCheck={false}
          autoCapitalize="off"
          autoCorrect="off"
          value={statement}
          onChange={(e) => setStatement(e.target.value)}
          onKeyDown={onKey}
        />
        <div className="flex flex-wrap items-center gap-3">
          <label className="flex items-center gap-2 text-xs text-ink-300">
            <Toggle checked={write} onChange={setWrite} label="Write mode" />
            <span className={write ? 'text-amber-400' : ''}>{write ? 'write (execute)' : 'read-only'}</span>
          </label>
          <label className="flex items-center gap-1.5 text-xs text-ink-400">
            limit
            <input
              type="number"
              min={1}
              max={5000}
              className={cx(inputClass, '!w-20 min-h-[32px] px-2 font-mono text-xs')}
              value={limit}
              onChange={(e) => setLimit(Math.max(1, Math.min(5000, Number(e.target.value) || 1)))}
            />
          </label>
          <Button variant={write ? 'danger' : 'primary'} icon="play" onClick={() => void run()} disabled={running || !statement.trim() || !provider} className="ml-auto">
            {running ? 'Running...' : 'Run'}
          </Button>
        </div>
      </Card>

      {queryError && <ErrorNote message={queryError} />}
      {result && <ResultGrid result={result} />}
    </div>
  );
}

function ResultGrid({ result }: { result: QueryResult }) {
  return (
    <div className="space-y-2">
      <p className="flex flex-wrap gap-2 px-1 font-mono text-[11px] text-ink-400">
        <span>{formatNumber(result.row_count)} rows</span>
        {result.affected_rows != null && <span>{formatNumber(result.affected_rows)} affected</span>}
        {result.elapsed_ms != null && <span>{formatMs(result.elapsed_ms)}</span>}
        {result.backend && <span>{result.backend}</span>}
        {result.truncated && <span className="text-amber-400">truncated</span>}
      </p>
      {result.columns.length > 0 && (
        <div className="scroll-thin max-h-[60vh] overflow-auto rounded-xl border border-ink-700/70 bg-ink-900/80">
          <table className="min-w-full border-collapse font-mono text-[12px]">
            <thead className="sticky top-0 bg-ink-850">
              <tr>
                {result.columns.map((c) => (
                  <th key={c} className="whitespace-nowrap border-b border-ink-700 px-3 py-2 text-left font-medium text-ink-300">
                    {c}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {result.rows.map((row, i) => (
                <tr key={i} className="odd:bg-ink-900 even:bg-ink-850/40">
                  {row.map((v, j) => (
                    <td key={j} className={cx('max-w-[18rem] truncate whitespace-nowrap border-b border-ink-800 px-3 py-1.5', v === null ? 'text-ink-500' : 'text-ink-100')} title={cellText(v)}>
                      {cellText(v)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
