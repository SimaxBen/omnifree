// File type tiles (no network): an icon per kind, a mini grid for spreadsheets, and a "paper"
// tile with a document's title or first words when it has no embedded thumbnail.

import type { FileKind } from '../lib/files';
import { Icon, type IconName } from './icons';
import { cx } from './ui';

export const KIND_STYLE: Record<FileKind, { icon: IconName; tile: string }> = {
  image: { icon: 'eye', tile: 'bg-[#2B4A7A] text-[#A9CBFF]' },
  video: { icon: 'play', tile: 'bg-[#4A2B6E] text-[#D3B2FF]' },
  audio: { icon: 'wave', tile: 'bg-[#5A2340] text-[#FFAFCB]' },
  pdf: { icon: 'file', tile: 'bg-[#5E2A2A] text-[#FFB0A0]' },
  text: { icon: 'note', tile: 'bg-[#2E3A5E] text-[#C9D2F2]' },
  code: { icon: 'table', tile: 'bg-[#1F4A44] text-[#8FE6CF]' },
  sheet: { icon: 'table', tile: 'bg-[#244A2C] text-[#A6E8A0]' },
  doc: { icon: 'book', tile: 'bg-[#2A3D6B] text-[#AFC4FF]' },
  slides: { icon: 'chart', tile: 'bg-[#6B3A1F] text-[#FFC9A0]' },
  archive: { icon: 'folder', tile: 'bg-[#4F3B1E] text-[#FFD08A]' },
  other: { icon: 'file', tile: 'bg-ink-800 text-ink-300' },
};

function MiniGrid({ px }: { px: number }) {
  // A 4x3 sheet: header row and first column tinted.
  const cells = Array.from({ length: 12 }, (_, i) => i);
  return (
    <span className="grid grid-cols-4 gap-px overflow-hidden rounded-[3px] bg-[#A6E8A0]/40" style={{ width: px, height: px * 0.75 }}>
      {cells.map((i) => (
        <span key={i} className={i < 4 || i % 4 === 0 ? 'bg-[#A6E8A0]/70' : 'bg-[#244A2C]'} />
      ))}
    </span>
  );
}

export function FileTile({ kind, size = 44, className, fill }: { kind: FileKind; size?: number; className?: string; fill?: boolean }) {
  const s = KIND_STYLE[kind];
  const glyph = fill ? 32 : Math.round(size * 0.45);
  return (
    <span className={cx('grid shrink-0 place-items-center rounded-[12px]', fill && 'aspect-square w-full', s.tile, className)} style={fill ? undefined : { width: size, height: size }}>
      {kind === 'sheet' ? <MiniGrid px={fill ? 44 : Math.round(size * 0.55)} /> : <Icon name={s.icon} size={glyph} />}
    </span>
  );
}

/** A light "page" with the document's title or first words (documents without a picture thumbnail). */
export function PaperTile({ kind, text, size = 44, className, fill }: { kind: FileKind; text: string; size?: number; className?: string; fill?: boolean }) {
  const s = KIND_STYLE[kind];
  const small = !fill && size < 64;
  return (
    <span
      className={cx('relative block shrink-0 overflow-hidden rounded-[12px] bg-[#F3F1EA] text-left', fill && 'aspect-square w-full', className)}
      style={fill ? undefined : { width: size, height: size }}
      title={text}
    >
      <span className={cx('absolute inset-x-0 top-0 h-1', s.tile)} />
      <span
        className={cx('block overflow-hidden break-words font-sans font-semibold text-[#2a2a36]', small ? 'p-1 pt-1.5 text-[6.5px] leading-[1.25]' : 'p-2 pt-2.5 text-[11px] leading-snug')}
        style={{ display: '-webkit-box', WebkitBoxOrient: 'vertical', WebkitLineClamp: small ? 5 : 6 }}
      >
        {text}
      </span>
      <span className={cx('absolute bottom-1 right-1 grid place-items-center rounded-[6px]', s.tile, small ? 'h-3.5 w-3.5' : 'h-6 w-6')}>
        <Icon name={s.icon} size={small ? 9 : 14} />
      </span>
    </span>
  );
}
