import { inputClass } from '../../components/ui';
import type { ModelInfo } from '../../lib/types';

/** Value format: "" = auto routing, otherwise "<provider>|<model>" or "*|<model>" (any provider hosting it). */
export function parseModelChoice(value: string): { provider: string | null; model: string | null } {
  if (!value) return { provider: null, model: null };
  const [provider, ...rest] = value.split('|');
  const model = rest.join('|') || null;
  return { provider: provider === '*' ? null : provider, model };
}

export default function ModelPicker({
  models,
  value,
  onChange,
  disabled,
}: {
  models: Record<string, ModelInfo[]> | undefined;
  value: string;
  onChange: (v: string) => void;
  disabled?: boolean;
}) {
  const providers = Object.keys(models ?? {}).sort();
  return (
    <select aria-label="Model" className={inputClass} value={value} onChange={(e) => onChange(e.target.value)} disabled={disabled}>
      <option value="">auto (fallback chain)</option>
      {providers.map((p) => (
        <optgroup key={p} label={p}>
          <option value={`${p}|`}>{p}: default model</option>
          {(models?.[p] ?? []).map((m) => (
            <option key={`${p}|${m.id}`} value={`${p}|${m.id}`}>
              {m.id}
            </option>
          ))}
        </optgroup>
      ))}
    </select>
  );
}
