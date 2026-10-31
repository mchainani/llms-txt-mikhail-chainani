import { Link, useParams } from 'react-router-dom';
import { runPath } from '../api';
import type { FileInfo, RunInfo } from '../api';
import { Breadcrumbs, ErrorNotice, Loading, Status, Timestamp } from '../components';
import { useResource } from '../useResource';

const isPending = (run: RunInfo) => run.status === 'pending';

export function FilePage() {
  const { requestId = '', runId = '' } = useParams();
  const path = runPath(requestId, runId);
  const run = useResource<RunInfo>(path, isPending);
  const file = useResource<FileInfo>(run.data?.file_id ? `${path}/files` : null);
  return <>
    <Breadcrumbs requestId={requestId} file />
    <div className="page-heading"><div><p className="eyebrow">RUN OUTPUT</p><h1>Generated file</h1><p className="intro">Your generated content, ready to inspect and download.</p></div>{file.data && <a className="button primary" href={`data:text/plain;charset=utf-8,${encodeURIComponent(file.data.content)}`} download="llms.txt">↓ Download llms.txt</a>}</div>
    {run.error && <ErrorNotice message={run.error} retry={run.reload} />}
    {run.data && <div className="file-meta"><span>Run <code>{run.data.id}</code></span><Status value={run.data.status} /></div>}
    {(run.loading && !run.data || file.loading && !file.data) && <Loading label="Loading file…" />}
    {run.data && !run.data.file_id && <section className="panel empty-state"><h2>{run.data.status === 'failed' ? 'This run failed' : 'File not available yet'}</h2><p>{run.data.status === 'pending' ? 'The run is pending. This page refreshes every 5 seconds.' : 'This run has no generated file. Check its status on the runs page.'}</p></section>}
    {file.error && <ErrorNotice message={file.error} retry={file.reload} />}
    {file.data && <section className="panel file-panel" aria-label="Generated file content"><div className="list-heading"><div className="heading-with-count"><span className="file-icon" aria-hidden="true">≡</span><h2>llms.txt</h2><span className="count">{file.data.type}</span></div><span className="muted"><Timestamp value={file.data.created_at} /></span></div><div className="file-details"><span className="mono">{file.data.id}</span><span>{new TextEncoder().encode(file.data.content).length.toLocaleString()} bytes</span></div><pre tabIndex={0}><code>{file.data.content}</code></pre>{!file.data.content && <p className="empty-state">This file is empty.</p>}</section>}
    <Link className="back-link" to={`/requests/${requestId}/runs`}>← Back to runs</Link>
  </>;
}
