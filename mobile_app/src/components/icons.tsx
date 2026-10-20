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
      strokeWidth={1.7}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...rest}
    >
      <path d={PATHS[name]} />
    </svg>
  );
}
