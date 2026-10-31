import { Link } from 'react-router-dom';

export function ErrorNotice({ message, retry }: { message: string; retry?: () => void }) {
  return <div className="error-notice" role="alert"><span>{message}</span>{retry && <button className="button secondary small" onClick={retry}>Try again</button>}</div>;
}

export function Loading({ label = 'Loading…' }: { label?: string }) {
  return <div className="loading" role="status"><span className="spinner" aria-hidden="true" />{label}</div>;
}

export function Timestamp({ value }: { value: string }) {
  const date = new Date(value);
  return <time dateTime={value} title={date.toLocaleString()}>{date.toLocaleString(undefined, { month: 'short', day: 'numeric', year: 'numeric', hour: '2-digit', minute: '2-digit' })}</time>;
}

export function Status({ value }: { value: string }) {
  const style = ['success', 'pending', 'failed'].includes(value) ? value : 'unknown';
  return <span className={`status ${style}`}><span aria-hidden="true" />{value.charAt(0).toUpperCase() + value.slice(1)}</span>;
}

export function Breadcrumbs({ requestId, file = false }: { requestId: string; file?: boolean }) {
  return <nav className="breadcrumbs" aria-label="Breadcrumb"><Link to="/requests">Requests</Link><span aria-hidden="true">/</span>{file ? <><Link to={`/requests/${requestId}/runs`}>Request runs</Link><span aria-hidden="true">/</span><span aria-current="page">File</span></> : <span aria-current="page">Request runs</span>}</nav>;
}
