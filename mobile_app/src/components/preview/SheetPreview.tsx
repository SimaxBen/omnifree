// Spreadsheets (xlsx/xls/ods via SheetJS CE) and CSV/TSV (own parser, delimiter detection) in one
// grid: sheet tabs, the first 1000 rows x 50 columns, rows virtualised, sticky header row and first
// column, numbers right-aligned with the file's own number/date formats, tap a cell for its value.

import { useEffect, useMemo, useRef, useState, type UIEvent } from 'react';
import { read, utils, type CellObject, type WorkBook } from 'xlsx';
import { copyText } from '../../lib/dom';
import { extensionOf } from '../../lib/files';
import { useHub } from '../../lib/hub';
import { detectDelimiter, looksNumeric, parseDelimited } from '../../lib/preview/delimited';
import type { PreviewSource } from '../../lib/preview/source';
import { Icon } from '../icons';
import { IconButton, cx } from '../ui';
import { Loading, PreviewError, decodeText, useSourceBytes } from './common';

export const SHEET_MAX_BYTES = 30 * 1024 * 1024;
const MAX_ROWS = 1000;
const MAX_COLS = 50;
const ROW_H = 34;
const OVERSCAN = 12;

interface GridCell {
  /** Displayed (formatted) text. */
  text: string;
  numeric: boolean;
  raw?: string;
  formula?: string;
  type?: string;
}

interface Grid {
  name: string;
  hidden?: boolean;
  rows: GridCell[][];
  columns: number;
  totalRows: number;
  totalCols: number;
}

const EMPTY: GridCell = { text: '', numeric: false };

function sheetGrid(wb: WorkBook, name: string, hidden: boolean): Grid {
  const ws = wb.Sheets[name];
  const ref = ws?.['!ref'];
  if (!ws || !ref) return { name, hidden, rows: [], columns: 0, totalRows: 0, totalCols: 0 };
  const range = utils.decode_range(ref);
  const full = ws['!fullref'] ? utils.decode_range(ws['!fullref'] as string) : range;
  const dense = (ws as unknown as { '!data'?: (CellObject | undefined)[][] })['!data'];
  const lastRow = Math.min(range.e.r, range.s.r + MAX_ROWS - 1);
  const lastCol = Math.min(range.e.c, range.s.c + MAX_COLS - 1);
  const rows: GridCell[][] = [];
  for (let r = range.s.r; r <= lastRow; r++) {
    const row: GridCell[] = [];
    for (let c = range.s.c; c <= lastCol; c++) {
      const cell = dense ? dense[r]?.[c] : (ws[utils.encode_cell({ r, c })] as CellObject | undefined);
      if (!cell || (cell.v === undefined && !cell.f)) {
        row.push(EMPTY);
        continue;
      }
      const text = cell.w ?? (cell.v instanceof Date ? cell.v.toLocaleString() : String(cell.v ?? ''));
      row.push({
        text,
        numeric: cell.t === 'n' || cell.t === 'd',
        raw: cell.v === undefined ? undefined : cell.v instanceof Date ? cell.v.toISOString() : String(cell.v),
        formula: cell.f,
        type: cell.t,
      });
    }
    rows.push(row);
  }
  return {
    name,
    hidden,
    rows,
    columns: lastCol - range.s.c + 1,
    totalRows: full.e.r - full.s.r + 1,
    totalCols: full.e.c - full.s.c + 1,
  };
}

function csvGrid(text: string, name: string, ext: string): Grid & { delimiter: string } {
  const delimiter = detectDelimiter(text, ext);
  const parsed = parseDelimited(text, delimiter, MAX_ROWS);
  const columns = Math.min(parsed.columns, MAX_COLS);
  const rows = parsed.rows.map((r) =>
    Array.from({ length: columns }, (_, c) => {
      const v = r[c] ?? '';
      return v ? { text: v, numeric: looksNumeric(v) } : EMPTY;
    }),
  );
  return { name, rows, columns, totalRows: parsed.truncated ? rows.length + 1 : rows.length, totalCols: parsed.columns, delimiter };
}

function colName(c: number): string {
  let s = '';
  for (let n = c + 1; n > 0; n = Math.floor((n - 1) / 26)) s = String.fromCharCode(65 + ((n - 1) % 26)) + s;
  return s;
}

function GridView({ grid }: { grid: Grid }) {
  const { notify } = useHub();
  const scroller = useRef<HTMLDivElement>(null);
  const [view, setView] = useState({ top: 0, height: 600 });
  const [picked, setPicked] = useState<{ r: number; c: number } | null>(null);

  const widths = useMemo(() => {
    const w: number[] = [];
    for (let c = 0; c < grid.columns; c++) {
      let max = 3;
      for (let r = 0; r < Math.min(grid.rows.length, 200); r++) max = Math.max(max, grid.rows[r][c]?.text.length ?? 0);
      w.push(Math.round(Math.min(220, Math.max(56, max * 8.2 + 22))));
    }
    return w;
  }, [grid]);
  const lefts = useMemo(() => widths.reduce<number[]>((acc, _w, i) => (acc.push(i ? acc[i - 1] + widths[i - 1] : 0), acc), []), [widths]);
  const totalW = widths.reduce((s, w) => s + w, 0);
  const header = grid.rows[0] ?? [];
  const body = grid.rows.length - 1;

  useEffect(() => {
    const el = scroller.current;
    if (!el) return undefined;
    el.scrollTo(0, 0);
    setView({ top: 0, height: el.clientHeight });
    const ro = new ResizeObserver(() => setView((v) => ({ ...v, height: el.clientHeight })));
    ro.observe(el);
    return () => ro.disconnect();
  }, [grid]);

  const onScroll = (e: UIEvent<HTMLDivElement>) => {
    const el = e.currentTarget;
    setView({ top: el.scrollTop, height: el.clientHeight });
  };

  if (!grid.rows.length) return <p className="p-8 text-center text-[14px] text-ink-400">This sheet is empty.</p>;

  const first = Math.max(1, Math.floor(view.top / ROW_H) + 1 - OVERSCAN);
  const last = Math.min(grid.rows.length - 1, Math.ceil((view.top + view.height) / ROW_H) + OVERSCAN);
  const cellCls = (c: number, numeric: boolean) =>
    cx('absolute top-0 truncate border-b border-r border-[#2c3159] px-2 leading-[33px]', numeric && 'text-right tabular-nums', c === 0 && 'sticky left-0 z-10');
  const rows: number[] = [];
  for (let r = first; r <= last; r++) rows.push(r);
  const pickedCell = picked ? grid.rows[picked.r]?.[picked.c] : null;

  return (
    <div className="relative h-full">
      <div ref={scroller} onScroll={onScroll} className="h-full overflow-auto overscroll-contain text-[13px] text-ink-200">
        <div className="relative" style={{ width: totalW, height: ROW_H * (body + 1) }}>
          <div className="sticky top-0 z-20 bg-ink-850 font-semibold text-ink-100" style={{ width: totalW, height: ROW_H }}>
            {header.slice(0, grid.columns).map((cell, c) => (
              <button
                key={c}
                type="button"
                onClick={() => setPicked({ r: 0, c })}
                className={cx(cellCls(c, false), 'text-left', c === 0 ? 'z-30 bg-ink-800' : 'bg-ink-850')}
                style={{ left: c === 0 ? 0 : lefts[c], width: widths[c], position: c === 0 ? 'sticky' : 'absolute' }}
              >
                {cell.text}
              </button>
            ))}
          </div>
          {rows.map((r) => (
            <div key={r} className={cx('absolute left-0', r % 2 ? 'bg-ink-950' : 'bg-[#0e1126]')} style={{ top: r * ROW_H, height: ROW_H, width: totalW }}>
              {grid.rows[r].slice(0, grid.columns).map((cell, c) => (
                <button
                  key={c}
                  type="button"
                  onClick={() => setPicked({ r, c })}
                  className={cx(cellCls(c, cell.numeric), c === 0 && (r % 2 ? 'bg-ink-900' : 'bg-[#121531]'), picked?.r === r && picked.c === c && 'outline outline-2 -outline-offset-2 outline-signal')}
                  style={{ left: c === 0 ? 0 : lefts[c], width: widths[c], position: c === 0 ? 'sticky' : 'absolute' }}
                >
                  {cell.text}
                </button>
              ))}
            </div>
          ))}
        </div>
      </div>
      {picked && pickedCell && (
        <div className="screen-in-up absolute inset-x-2 bottom-2 z-40 max-h-[45%] overflow-y-auto rounded-card bg-ink-900 p-4 shadow-2xl ring-1 ring-inset ring-ink-700">
          <div className="mb-2 flex items-center gap-2">
            <span className="rounded-md bg-ink-800 px-2 py-0.5 font-mono text-[12px] text-signal">
              {colName(picked.c)}
              {picked.r + 1}
            </span>
            <span className="min-w-0 flex-1 truncate text-[12px] text-ink-500">{header[picked.c]?.text && picked.r > 0 ? header[picked.c].text : ''}</span>
            <IconButton
              icon="copy"
              label="Copy value"
              onClick={async () => {
                const ok = await copyText(pickedCell.text);
                notify(ok ? 'Copied' : 'Copy failed', ok ? 'ok' : 'error');
              }}
              className="h-9 w-9"
            />
            <IconButton icon="x" label="Close" onClick={() => setPicked(null)} className="h-9 w-9" />
          </div>
          <p className="whitespace-pre-wrap break-words text-[15px] leading-relaxed text-ink-100">{pickedCell.text || <span className="text-ink-500">(empty)</span>}</p>
          {pickedCell.raw !== undefined && pickedCell.raw !== pickedCell.text && <p className="mt-2 break-all font-mono text-[12px] text-ink-400">Value: {pickedCell.raw}</p>}
          {pickedCell.formula && <p className="mt-1 break-all font-mono text-[12px] text-ink-400">={pickedCell.formula}</p>}
        </div>
      )}
    </div>
  );
}

export default function SheetPreview({ src }: { src: PreviewSource }) {
  const { data, error: loadError } = useSourceBytes(src, SHEET_MAX_BYTES);
  const ext = extensionOf(src.path);
  const isCsv = ext === 'csv' || ext === 'tsv';
  const [book, setBook] = useState<{ names: string[]; hidden: boolean[]; wb: WorkBook | null; csv: (Grid & { delimiter: string }) | null } | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [tab, setTab] = useState(0);

  useEffect(() => {
    if (!data) return undefined;
    setBook(null);
    setError(null);
    setTab(0);
    // Parsing is synchronous: yield a frame so the spinner shows first.
    const t = window.setTimeout(() => {
      try {
        if (isCsv) {
          const csv = csvGrid(decodeText(data), src.name, ext);
          setBook({ names: [src.name], hidden: [false], wb: null, csv });
          return;
        }
        const wb = read(data, { type: 'array', dense: true, sheetRows: MAX_ROWS, cellFormula: true, cellStyles: false, WTF: false });
        const hidden = wb.SheetNames.map((_, i) => !!wb.Workbook?.Sheets?.[i]?.Hidden);
        setBook({ names: wb.SheetNames, hidden, wb, csv: null });
      } catch (err) {
        setError(/password|encrypt/i.test(String(err)) ? 'This spreadsheet is password-protected.' : err);
      }
    }, 30);
    return () => window.clearTimeout(t);
  }, [data, isCsv, ext, src.name]);

  const grid = useMemo<Grid | null>(() => {
    if (!book) return null;
    if (book.csv) return book.csv;
    const name = book.names[tab];
    return name && book.wb ? sheetGrid(book.wb, name, book.hidden[tab]) : null;
  }, [book, tab]);

  if (loadError || error) return <PreviewError src={src} error={loadError || error} />;
  if (!book || !grid) return <Loading label={data ? 'Reading the spreadsheet' : 'Downloading the spreadsheet'} />;
  const cut = grid.totalRows > grid.rows.length || grid.totalCols > grid.columns;
  const delim = book.csv ? { ',': 'comma', ';': 'semicolon', '\t': 'tab', '|': 'pipe' }[book.csv.delimiter] : null;
  return (
    <div className="flex h-full flex-col bg-ink-950">
      {(cut || delim) && (
        <p className="shrink-0 border-b border-ink-800 bg-ink-900 px-3 py-1.5 text-[12px] text-ink-400">
          {cut && (
            <>
              <Icon name="eye" size={12} className="mr-1 inline" />
              Showing the first {Math.min(grid.rows.length, MAX_ROWS).toLocaleString()} rows × {grid.columns} columns
              {grid.totalRows > grid.rows.length ? ` of ${book.csv ? 'more' : grid.totalRows.toLocaleString()} rows` : ''}
              {grid.totalCols > grid.columns ? ` and ${grid.totalCols} columns` : ''}. Download it for the rest.
            </>
          )}
          {!cut && delim && `${grid.rows.length.toLocaleString()} rows, ${delim}-separated`}
        </p>
      )}
      <div className="min-h-0 flex-1">
        <GridView grid={grid} />
      </div>
      {book.names.length > 1 && (
        <nav aria-label="Sheets" className="no-scrollbar flex shrink-0 gap-1 overflow-x-auto border-t border-ink-800 bg-ink-900 px-2 py-1.5">
          {book.names.map((n, i) => (
            <button
              key={n}
              type="button"
              onClick={() => setTab(i)}
              aria-current={i === tab ? 'page' : undefined}
              className={cx('press min-h-[40px] shrink-0 rounded-full px-4 text-[13px]', i === tab ? 'bg-signal font-semibold text-ink-950' : 'bg-ink-850 text-ink-300', book.hidden[i] && 'italic opacity-70')}
            >
              {n}
            </button>
          ))}
        </nav>
      )}
    </div>
  );
}
