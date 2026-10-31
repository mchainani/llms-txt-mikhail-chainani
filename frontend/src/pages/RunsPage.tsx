import { useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { api, requestPath, runPath, runsPath } from '../api';
import type { RequestInfo, RunInfo } from '../api';
import { Breadcrumbs, ErrorNotice, Loading, Status, Timestamp } from '../components';
import { useResource } from '../useResource';
import { DeleteButton } from '../DeleteButton';

const hasPendingRuns = (runs: RunInfo[]) => runs.some((run) => run.status === 'pending');

export function RunsPage() {
  const { requestId = '' } = useParams();
  const request = useResource<RequestInfo>(requestPath(requestId));
  const runs = useResource<RunInfo[]>(runsPath(requestId), hasPendingRuns);
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);

  async function startRun() {
    if (starting) { return; }
    setStarting(true);
    setStartError(null);
    try {
      await api<{ id: string }>(runsPath(requestId), { method: 'POST' });
      runs.reload();
    } catch (error) {
      setStartError(error instanceof Error ? error.message : 'Unable to start the run.');
    } finally { setStarting(false); }
  }

  return <>
    <Breadcrumbs requestId={requestId} />
    <div className="page-heading"><div><p className="eyebrow">GENERATION HISTORY</p><h1>Request runs</h1><p className="intro">Follow each run from the queue to its generated file.</p></div><button type="button" className="button primary small" onClick={startRun} disabled={starting || !request.data}><span aria-hidden="true">＋</span>{starting ? 'Starting…' : 'Start new run'}</button></div>
    {startError && <ErrorNotice message={startError} />}
    {request.error && <ErrorNotice message={request.error} retry={request.reload} />}
    {request.loading && !request.data && <Loading label="Loading request…" />}
    {request.data && <section className="panel request-summary"><div><span className="eyebrow">WEBSITE</span><p className="summary-url">{request.data.url}</p><span className="identifier mono">{request.data.id}</span></div><div><span className="eyebrow">SCHEDULE</span><p>{request.data.cron_expression ? <code>{request.data.cron_expression}</code> : 'One-time request'}</p>{request.data.cron_expression && <span className="muted">Timezone: UTC</span>}</div><div><span className="eyebrow">CREATED</span><p><Timestamp value={request.data.created_at} /></p></div></section>}
    <section className="panel" aria-labelledby="runs-heading"><div className="list-heading"><div className="heading-with-count"><h2 id="runs-heading">Runs</h2>{runs.data && <span className="count">{runs.data.length}</span>}</div><button className="button secondary small" onClick={runs.reload} disabled={runs.loading}>↻ Refresh</button></div>
      {runs.data && hasPendingRuns(runs.data) && <p className="polling-note"><span className="pulse-dot" aria-hidden="true" />Pending runs refresh every 5 seconds.</p>}
      {runs.error && <ErrorNotice message={runs.error} retry={runs.reload} />}
      {runs.loading && !runs.data && <Loading label="Loading runs…" />}
      {runs.data?.length === 0 && <div className="empty-state"><h3>No runs yet</h3><p>Runs will appear here as they are created.</p></div>}
      {!!runs.data?.length && <div className="table-scroll"><table><thead><tr><th>Run</th><th>Status</th><th>Job ID</th><th>Created</th><th>Last updated</th><th>Output</th><th><span className="sr-only">Actions</span></th></tr></thead><tbody>{[...runs.data].sort((a, b) => b.created_at.localeCompare(a.created_at)).map((run) => <tr key={run.id}><td><span className="mono run-id" title={run.id}>{run.id}</span></td><td><Status value={run.status} /></td><td><span className="mono">{run.job_id ?? '—'}</span></td><td className="date-cell"><Timestamp value={run.created_at} /></td><td className="date-cell"><Timestamp value={run.timestamp} /></td><td className="action-cell">{run.file_id ? <Link className="text-link" to={`/requests/${requestId}/runs/${run.id}/file`} aria-label={`View file for run ${run.id}`}>View file →</Link> : <span className="muted">{run.status === 'failed' ? 'No file generated' : run.status === 'pending' ? 'Awaiting output' : 'No file available'}</span>}</td><td className="action-cell"><DeleteButton path={runPath(requestId, run.id)} label={`Delete run ${run.id}`} onDeleted={runs.reload} /></td></tr>)}</tbody></table></div>}
    </section>
    <Link className="back-link" to="/requests">← Back to requests</Link>
  </>;
}
