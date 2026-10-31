import { runPath } from './api';
import type { RunInfo } from './api';
import { Status } from './components';
import { useResource } from './useResource';

const isPending = (run: RunInfo) => run.status === 'pending';

export function LatestRunStatus({ requestId }: { requestId: string }) {
  const latest = useResource<RunInfo>(runPath(requestId, 'latest'), isPending);

  if (latest.errorStatus === 404) { return <span className="muted">No runs</span>; }
  if (latest.error) {
    return <button type="button" className="button secondary small" title={latest.error} aria-label="Retry latest run status" onClick={latest.reload}>Unavailable · Retry</button>;
  }
  if (!latest.data) { return <span className="muted">Loading…</span>; }
  return <Status value={latest.data.status} />;
}
