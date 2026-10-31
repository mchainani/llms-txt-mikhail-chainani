import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { createRequest, requestPath, requestsPath } from '../api';
import type { RequestInfo } from '../api';
import { ErrorNotice, Loading, Timestamp } from '../components';
import { useResource } from '../useResource';
import { DeleteButton } from '../DeleteButton';
import { LatestRunStatus } from '../LatestRunStatus';

export function RequestsPage() {
  const requests = useResource<RequestInfo[]>(requestsPath);
  const [statusVersion, setStatusVersion] = useState(0);
  const navigate = useNavigate();
  const [expanded, setExpanded] = useState(false);
  const [url, setUrl] = useState('');
  const [cron, setCron] = useState('');
  const [timeout, setTimeout] = useState('');
  const [maxPages, setMaxPages] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  function refreshRequests() {
    requests.reload();
    setStatusVersion((value) => value + 1);
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting) { return; }
    setError(null);
    setSubmitting(true);
    try {
      const created = await createRequest(url, cron, timeout, maxPages);
      if (mounted.current) { navigate(`/requests/${created.id}/runs`); }
    } catch (error) {
      if (mounted.current) { setError(error instanceof Error ? error.message : 'Unable to create the request.'); }
    } finally { if (mounted.current) { setSubmitting(false); } }
  }

  return <>
    <div className="page-heading"><div><p className="eyebrow">YOUR GENERATION WORKSPACE</p><h1>Requests</h1><p className="intro">Turn a website into llms.txt. Run it once, or keep it on a schedule.</p></div><button className="button primary small" type="button" aria-expanded={expanded} aria-controls="new-request-form" disabled={submitting} onClick={() => setExpanded((value) => !value)}><span aria-hidden="true">{expanded ? '−' : '+'}</span>New request</button></div>
    {expanded && <section id="new-request-form" className="panel create-panel" aria-labelledby="create-heading">
      <div className="panel-heading"><span className="section-icon" aria-hidden="true">＋</span><div><h2 id="create-heading">New request</h2><p>Start with a URL. We’ll take it from there.</p></div></div>
      <form onSubmit={submit}>
        <div className="form-fields">
          <div className="field url-field"><label htmlFor="website-url">Website URL</label><input id="website-url" type="url" pattern="https?://.+" title="Enter an HTTP or HTTPS URL" placeholder="https://example.com" required autoFocus value={url} onChange={(event) => setUrl(event.target.value)} disabled={submitting} /><p className="field-hint">The page you want to generate an llms.txt file from.</p></div>
          <div className="field"><label htmlFor="cron">Cron expression <span className="optional">optional</span></label><input id="cron" className="mono" placeholder="0 9 * * 1" aria-describedby="cron-hint" value={cron} onChange={(event) => setCron(event.target.value)} disabled={submitting} /><p id="cron-hint" className="field-hint">Leave blank to run once. Schedules use UTC.</p></div>
          <div className="field"><label htmlFor="fetch-timeout">Fetch timeout (seconds) <span className="optional">optional</span></label><input id="fetch-timeout" type="number" min="0" step="any" placeholder="Server default" aria-describedby="fetch-timeout-hint" value={timeout} onChange={(event) => { setTimeout(event.target.value); event.target.setCustomValidity(event.target.value !== '' && Number(event.target.value) <= 0 ? 'Enter a timeout greater than zero.' : ''); }} disabled={submitting} /><p id="fetch-timeout-hint" className="field-hint">Maximum time for the entire crawl. Leave blank to use the server default.</p></div>
          <div className="field"><label htmlFor="max-pages">Maximum pages <span className="optional">optional</span></label><input id="max-pages" type="number" min="1" step="1" placeholder="50" aria-describedby="max-pages-hint" value={maxPages} onChange={(event) => setMaxPages(event.target.value)} disabled={submitting} /><p id="max-pages-hint" className="field-hint">Stop after collecting this many unique page links, then generate the file. Leave blank to use 50.</p></div>
        </div>
        {error && <ErrorNotice message={error} />}
        <div className="form-footer"><span><span aria-hidden="true">↳</span> Scheduled requests start at the next cron time; others start immediately.</span><button className="button primary" type="submit" disabled={submitting}>{submitting ? 'Creating…' : 'Create request'}<span aria-hidden="true">→</span></button></div>
      </form>
    </section>}
    <section className="panel" aria-labelledby="all-requests">
      <div className="list-heading"><div className="heading-with-count"><h2 id="all-requests">All requests</h2>{requests.data && <span className="count">{requests.data.length}</span>}</div><button className="button secondary small" onClick={refreshRequests} disabled={requests.loading}>↻ Refresh</button></div>
      {requests.error && <ErrorNotice message={requests.error} retry={refreshRequests} />}
      {requests.loading && !requests.data && <Loading label="Loading requests…" />}
      {requests.data?.length === 0 && <div className="empty-state"><span className="empty-icon" aria-hidden="true">↗</span><h3>No requests yet</h3><p>Select New request to generate your first file.</p></div>}
      {!!requests.data?.length && <div className="table-scroll"><table><thead><tr><th>Website / request</th><th>Schedule</th><th>Latest run</th><th>Job ID</th><th>Created</th><th><span className="sr-only">Actions</span></th></tr></thead><tbody>{[...requests.data].sort((a, b) => b.created_at.localeCompare(a.created_at)).map((request) => <tr key={request.id}><td><span className="website">{request.url}</span><span className="identifier mono" title={request.id}>{request.id}</span></td><td>{request.cron_expression ? <><code className="schedule">{request.cron_expression}</code><span className="cell-caption">Recurring · UTC</span></> : <span className="once">One-time</span>}</td><td><LatestRunStatus key={`${request.id}-${statusVersion}`} requestId={request.id} /></td><td><span className="mono">{request.job_id ?? '—'}</span></td><td className="date-cell"><Timestamp value={request.created_at} /></td><td className="action-cell"><div className="row-actions"><Link className="text-link" to={`/requests/${request.id}/runs`} aria-label={`View runs for ${request.url}`}>View runs <span aria-hidden="true">→</span></Link><DeleteButton path={requestPath(request.id)} label={`Delete request ${request.id}`} onDeleted={refreshRequests} /></div></td></tr>)}</tbody></table></div>}
    </section>
    <p className="page-note">A clear path from request to run to generated file.</p>
  </>;
}
