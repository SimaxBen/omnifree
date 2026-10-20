import { useMemo } from 'react';
import { encodeQr, qrPath } from '../lib/qr';

/** QR code as inline SVG (dark modules on a light tile, so phone cameras read it easily). */
export function QrCode({ value, size = 220, label }: { value: string; size?: number; label?: string }) {
  const qr = useMemo(() => {
    try {
      return encodeQr(value, 'M');
    } catch {
      return null;
    }
  }, [value]);
  if (!qr) return <p className="text-[14px] text-danger-400">This link is too long for a QR code.</p>;
  const dim = qr.size + 8;
  return (
    <svg
      role="img"
      aria-label={label ?? `QR code for ${value}`}
      width={size}
      height={size}
      viewBox={`0 0 ${dim} ${dim}`}
      shapeRendering="crispEdges"
      className="pop rounded-[18px]"
    >
      <rect width={dim} height={dim} fill="#F2EEE4" />
      <path d={qrPath(qr, 4)} fill="#0B0D1E" />
    </svg>
  );
}
