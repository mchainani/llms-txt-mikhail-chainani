import { useId } from 'react';

const options = [
  { key: 'fetch_timeout_seconds', label: 'Fetch timeout', unit: ' s' },
  { key: 'max_pages', label: 'Maximum pages', unit: '' },
  { key: 'max_depth', label: 'Maximum depth', unit: '' },
];

export function RequestOptions({ configs }: { configs: string | null }) {
  const id = useId();
  let values: Record<string, unknown> = {};
  if (configs !== null) {
    try {
      const parsed: unknown = JSON.parse(configs);
      if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
        throw new Error('Invalid request options');
      }
      values = parsed as Record<string, unknown>;
    } catch {
      return <p className="muted">Request options unavailable</p>;
    }
  }

  return <dl className="request-options">{options.map(({ key, label, unit }) => {
    const value = values[key];
    const display = value == null ? 'Server default'
      : typeof value === 'number' && Number.isFinite(value) && value > 0 ? `${value}${unit}` : 'Unavailable';
    return <div key={key}><dt id={`${id}-${key}`}>{label}</dt><dd aria-labelledby={`${id}-${key}`}>{display}</dd></div>;
  })}</dl>;
}
