import { useState } from 'react';
import { api } from './api';

export function DeleteButton({ path, label, onDeleted }: {
  path: string;
  label: string;
  onDeleted: () => void;
}) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function remove() {
    if (pending) { return; }
    setPending(true);
    setError(null);
    try {
      await api<void>(path, { method: 'DELETE' });
      onDeleted();
    } catch (error) {
      setError(error instanceof Error ? error.message : 'Unable to delete. Please try again.');
    } finally { setPending(false); }
  }

  return <span className="delete-control">
    <button type="button" className="delete-icon-button" aria-label={pending ? `Deleting: ${label}` : label} title={pending ? 'Deleting…' : label} disabled={pending} onClick={remove}>
      {pending ? <span className="spinner" aria-hidden="true" /> : <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M3 6h18M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2M5 6l1 14a1 1 0 0 0 1 1h10a1 1 0 0 0 1-1l1-14M10 10v7M14 10v7" /></svg>}
    </button>
    {error && <span className="delete-error" role="alert">{error}</span>}
  </span>;
}
