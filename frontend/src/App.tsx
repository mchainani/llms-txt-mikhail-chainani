import { useEffect, useRef } from 'react';
import { Link, Navigate, Route, Routes, useLocation } from 'react-router-dom';
import { RequestsPage } from './pages/RequestsPage';
import { RunsPage } from './pages/RunsPage';
import { FilePage } from './pages/FilePage';

export default function App() {
  const { pathname } = useLocation();
  const main = useRef<HTMLElement>(null);
  useEffect(() => {
    main.current?.focus();
    document.title = `${pathname.endsWith('/file') ? 'Generated file' : pathname.endsWith('/runs') ? 'Request runs' : 'Requests'} · llms.txt`;
  }, [pathname]);

  return (
    <>
      <a className="skip-link" href="#main">Skip to content</a>
      <header className="app-header">
        <div className="header-inner">
          <Link to="/requests" className="brand"><span className="brand-icon" aria-hidden="true">≋</span>llms<span className="brand-suffix">.txt</span></Link>
          <span className="workspace-label">GENERATION WORKSPACE</span>
        </div>
      </header>
      <main id="main" ref={main} tabIndex={-1}>
        <Routes>
          <Route path="/" element={<Navigate to="/requests" replace />} />
          <Route path="/requests" element={<RequestsPage />} />
          <Route path="/requests/:requestId/runs" element={<RunsPage />} />
          <Route path="/requests/:requestId/runs/:runId/file" element={<FilePage />} />
          <Route path="*" element={<section className="empty-state"><h1>Page not found</h1><Link to="/requests">Back to requests →</Link></section>} />
        </Routes>
      </main>
      <footer className="app-footer"><span>llms.txt generator</span><span>From your website to your next llms.txt.</span></footer>
    </>
  );
}
