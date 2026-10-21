import type { SVGProps } from 'react';

// Stroke icons drawn on a 24px grid.
const PATHS = {
  chat: 'M4 5h16v11H8l-4 4V5z M8 9h8 M8 12h5',
  drive: 'M3 7l3-3h12l3 3v12H3V7z M3 7h18 M8 12h8',
  network: 'M12 3v6 M12 15v6 M3 12h6 M15 12h6 M12 9a3 3 0 110 6 3 3 0 010-6z',
  database: 'M4 6c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3z M4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6 M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3',
  switchboard: 'M4 6h10 M18 6h2 M4 12h4 M12 12h8 M4 18h12 M20 18h0 M16 4v4 M10 10v4 M18 16v4',
  settings: 'M12 9a3 3 0 110 6 3 3 0 010-6z M19 12l2-1-1-3-2 .2-1.3-1.3L17 5l-3-1-1 2h-2L10 4 7 5l.3 2-1.3 1.3L4 8 3 11l2 1v1l-2 1 1 3 2-.2 1.3 1.3L7 19l3 1 1-2h2l1 2 3-1-.3-2 1.3-1.3 2 .3 1-3-2-1v-1z',
  send: 'M4 12l16-8-6 16-2-6-8-2z',
  stop: 'M7 7h10v10H7z',
  upload: 'M12 16V4 M7 9l5-5 5 5 M4 16v4h16v-4',
  download: 'M12 4v12 M7 11l5 5 5-5 M4 20h16',
  trash: 'M5 7h14 M10 7V4h4v3 M7 7l1 13h8l1-13',
  refresh: 'M20 11a8 8 0 10-2 6 M20 5v6h-6',
  search: 'M11 4a7 7 0 110 14 7 7 0 010-14z M16 16l4 4',
  up: 'M12 19V5 M6 11l6-6 6 6',
  down: 'M12 5v14 M6 13l6 6 6-6',
  grip: 'M9 6h.01 M15 6h.01 M9 12h.01 M15 12h.01 M9 18h.01 M15 18h.01',
  play: 'M7 5l12 7-12 7V5z',
  bolt: 'M13 3L5 14h6l-1 7 8-11h-6l1-7z',
  shield: 'M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6l8-3z',
  table: 'M4 5h16v14H4z M4 10h16 M10 10v9',
  x: 'M6 6l12 12 M18 6L6 18',
  pulse: 'M3 12h4l3-7 4 14 3-7h4',
  agent: 'M12 3a4 4 0 110 8 4 4 0 010-8z M5 21c0-4 3-7 7-7s7 3 7 7 M9 7h.01 M15 7h.01',
  clock: 'M12 3a9 9 0 110 18 9 9 0 010-18z M12 7v5l3 2',
  plus: 'M12 5v14 M5 12h14',
  copy: 'M9 9h11v11H9z M5 15H4V4h11v1',
  mic: 'M12 3a3 3 0 013 3v5a3 3 0 01-6 0V6a3 3 0 013-3z M5 11a7 7 0 0014 0 M12 18v3',
  home: 'M4 11l8-7 8 7v9h-5v-6H9v6H4v-9z',
  note: 'M6 3h9l4 4v14H6V3z M14 3v5h5 M9 12h7 M9 16h5',
  check: 'M5 12.5l4.5 4.5L19 7.5',
  circle: 'M12 3a9 9 0 110 18 9 9 0 010-18z',
  camera: 'M4 8h3l2-3h6l2 3h3v11H4V8z M12 10a3.5 3.5 0 110 7 3.5 3.5 0 010-7z',
  receipt: 'M6 3h12v18l-3-2-3 2-3-2-3 2V3z M9 8h6 M9 12h6 M9 16h3',
  link: 'M10 14a4 4 0 005.7 0l3-3a4 4 0 00-5.7-5.7l-1 1 M14 10a4 4 0 00-5.7 0l-3 3a4 4 0 005.7 5.7l1-1',
  qr: 'M4 4h6v6H4z M14 4h6v6h-6z M4 14h6v6H4z M14 14h2v2h-2z M18 18h2v2h-2z M14 18h2 M18 14h2',
  left: 'M15 5l-7 7 7 7',
  right: 'M9 5l7 7-7 7',
  grid: 'M4 4h6v6H4z M14 4h6v6h-6z M4 14h6v6H4z M14 14h6v6h-6z',
  pin: 'M9 4h6l-1 6 3 3H7l3-3-1-6z M12 13v7',
  tag: 'M3 12V4h8l9 9-8 8-9-9z M7.5 7.5h.01',
  wallet: 'M4 7h15v12H4V7z M4 7l11-3v3 M15 13h4',
  spark: 'M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8L12 3z M19 16l.8 2.2L22 19l-2.2.8L19 22l-.8-2.2L16 19l2.2-.8L19 16z',
  folder: 'M3 6h6l2 2h10v11H3V6z',
  file: 'M6 3h9l4 4v14H6V3z M14 3v5h5',
  lock: 'M6 11h12v9H6v-9z M8 11V8a4 4 0 018 0v3',
  eye: 'M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z M12 9a3 3 0 110 6 3 3 0 010-6z',
  edit: 'M4 20h4L19 9l-4-4L4 16v4z M13 7l4 4',
  book: 'M4 5a2 2 0 012-2h13v16H6a2 2 0 00-2 2V5z M4 19a2 2 0 012-2h13',
  calendar: 'M4 6h16v14H4V6z M4 10h16 M8 3v5 M16 3v5',
  flag: 'M5 21V4 M5 4h12l-2 4 2 4H5',
  gauge: 'M4 16a8 8 0 1116 0 M12 16l4-5',
  external: 'M14 4h6v6 M20 4l-9 9 M18 14v6H4V6h6',
  share: 'M12 15V3 M7 8l5-5 5 5 M5 12v8h14v-8',
  more: 'M5 12h.01 M12 12h.01 M19 12h.01',
  chart: 'M4 20V10 M10 20V4 M16 20v-7 M3 20h18',
  wave: 'M3 12h2 M7 8v8 M11 5v14 M15 9v6 M19 11v2',
  undo: 'M9 14L4 9l5-5 M4 9h10a6 6 0 010 12h-3',
  mail: 'M3 6h18v12H3z M3 7l9 6 9-6',
  cash: 'M3 6h18v12H3z M12 9a3 3 0 110 6 3 3 0 010-6z M6.5 9.5v.01 M17.5 14.5v.01',
  repeat: 'M17 3l3 3-3 3 M4 11V9a3 3 0 013-3h13 M7 21l-3-3 3-3 M20 13v2a3 3 0 01-3 3H4',
  bell: 'M6 16V11a6 6 0 0112 0v5l2 2H4l2-2z M10 20a2 2 0 004 0',
} as const;

export type IconName = keyof typeof PATHS;

export function Icon({ name, size = 20, ...rest }: { name: IconName; size?: number } & SVGProps<SVGSVGElement>) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...rest}
    >
      <path d={PATHS[name]} />
    </svg>
  );
}
