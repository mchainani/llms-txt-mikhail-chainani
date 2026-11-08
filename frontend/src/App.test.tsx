import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import App from './App';

const request = {
  id: 'request-1', url: 'https://example.com/', cron_expression: null, configs: null,
  created_at: '2026-10-30T12:00:00Z', timestamp: '2026-10-30T12:00:00Z', job_id: null,
};
const run = {
  id: 'run-1', request_id: 'request-1', status: 'success', file_id: 'file-1', num_links: 1, retry_count: 3,
  created_at: '2026-10-30T12:00:00Z', timestamp: '2026-10-30T12:01:00Z', job_id: null,
};
const file = {
  id: 'file-1', type: 'llms.txt', content: '# Example\n<script>alert("test")</script>',
  created_at: '2026-10-30T12:01:00Z', timestamp: '2026-10-30T12:01:00Z',
};
const base = '/llms-txt/requests';

function serve(overrides: Record<string, unknown> = {}) {
  const data: Record<string, unknown> = {
    [base]: [request], [`${base}/request-1`]: request,
    [`${base}/request-1/runs`]: [run],
    [`${base}/request-1/runs/run-1`]: run,
    [`${base}/request-1/runs/latest`]: run,
    [`${base}/request-1/runs/run-1/files`]: file,
    ...overrides,
  };
  const fetcher = vi.fn(async (input: string, options?: RequestInit) => {
    if (options?.method === 'DELETE') {
      if (input === `${base}/request-1`) { data[base] = []; }
      if (input === `${base}/request-1/runs/run-1`) { data[`${base}/request-1/runs`] = []; }
      return new Response(null, { status: 204 });
    }
    if (options?.method === 'POST') {
      if (input === `${base}/request-1/runs`) {
        data[input] = [...data[input] as typeof run[], { ...run, id: 'run-2', status: 'pending', file_id: null }];
        return Response.json({ id: 'run-2' }, { status: 201 });
      }
      return Response.json(request, { status: 201 });
    }
    if (!(input in data)) {
      return Response.json({ detail: 'Not found.' }, { status: 404 });
    }
    const value = data[input];
    return value instanceof Response ? value.clone() : Response.json(value);
  });
  vi.stubGlobal('fetch', fetcher);
  return fetcher;
}

function open(path = '/requests') {
  return render(<MemoryRouter initialEntries={[path]}><App /></MemoryRouter>);
}

describe('request workflow', () => {
  it.each(['pending', 'success', 'failed'])('shows the latest run status %s on the request row', async (status) => {
    serve({ [`${base}/request-1/runs/latest`]: { ...run, status } });
    open();
    expect(await screen.findByText(status.charAt(0).toUpperCase() + status.slice(1))).toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: 'Latest run' })).toBeInTheDocument();
  });

  it('shows No runs when the latest run does not exist', async () => {
    serve({ [`${base}/request-1/runs/latest`]: Response.json({ detail: 'Run not found.' }, { status: 404 }) });
    open();
    expect(await screen.findByText('No runs')).toBeInTheDocument();
  });

  it('offers a retry when the latest run cannot be loaded', async () => {
    const fetcher = serve();
    const original = fetcher.getMockImplementation()!;
    let failed = true;
    fetcher.mockImplementation((input, options) => input.endsWith('/runs/latest') && failed
      ? Promise.resolve(Response.json({ detail: 'Temporarily unavailable.' }, { status: 503 }))
      : original(input, options));
    const user = userEvent.setup();
    open();
    const retry = await screen.findByRole('button', { name: 'Retry latest run status' });
    expect(screen.queryByText('No runs')).not.toBeInTheDocument();
    failed = false;
    await user.click(retry);
    expect(await screen.findByText('Success')).toBeInTheDocument();
  });

  it('refreshes latest run status when the requests list is refreshed', async () => {
    const fetcher = serve();
    const original = fetcher.getMockImplementation()!;
    let status = 'success';
    fetcher.mockImplementation((input, options) => input.endsWith('/runs/latest')
      ? Promise.resolve(Response.json({ ...run, status }))
      : original(input, options));
    const user = userEvent.setup();
    open();
    expect(await screen.findByText('Success')).toBeInTheDocument();
    status = 'failed';
    await user.click(screen.getByRole('button', { name: /Refresh/ }));
    expect(await screen.findByText('Failed')).toBeInTheDocument();
  });

  it('starts an immediate run and refreshes the request runs list', async () => {
    const fetcher = serve();
    const user = userEvent.setup();
    open('/requests/request-1/runs');
    await user.click(await screen.findByRole('button', { name: 'Start new run' }));
    expect(await screen.findByText('run-2')).toBeInTheDocument();
    expect(screen.getByText('Pending')).toBeInTheDocument();
    const post = fetcher.mock.calls.find(([, options]) => options?.method === 'POST');
    expect(post?.[0]).toBe(`${base}/request-1/runs`);
    expect(screen.getByRole('heading', { name: 'Request runs' })).toBeInTheDocument();
  });

  it('shows an error and allows retrying when a new run cannot be started', async () => {
    const fetcher = serve();
    const original = fetcher.getMockImplementation()!;
    fetcher.mockImplementation((input, options) => options?.method === 'POST'
      ? Promise.resolve(Response.json({ detail: 'Unable to queue run.' }, { status: 503 }))
      : original(input, options));
    const user = userEvent.setup();
    open('/requests/request-1/runs');
    await user.click(await screen.findByRole('button', { name: 'Start new run' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Unable to queue run.');
    expect(screen.getByRole('button', { name: 'Start new run' })).toBeEnabled();
    expect(screen.getByText('run-1')).toBeInTheDocument();
  });

  it.each([
    ['/requests', 'Delete request request-1', `${base}/request-1`, 'No requests yet'],
    ['/requests/request-1/runs', 'Delete run run-1', `${base}/request-1/runs/run-1`, 'No runs yet'],
  ])('deletes a row from %s and refreshes the list after a 204 response', async (page, label, endpoint, emptyMessage) => {
    const fetcher = serve();
    const user = userEvent.setup();
    open(page);
    await user.click(await screen.findByRole('button', { name: label }));
    expect(await screen.findByText(emptyMessage)).toBeInTheDocument();
    const deletion = fetcher.mock.calls.find(([, options]) => options?.method === 'DELETE');
    expect(deletion?.[0]).toBe(endpoint);
  });

  it('keeps the row and displays the backend error when deletion fails', async () => {
    const fetcher = serve();
    const original = fetcher.getMockImplementation()!;
    fetcher.mockImplementation((input, options) => options?.method === 'DELETE'
      ? Promise.resolve(Response.json({ detail: 'Deletion failed.' }, { status: 409 }))
      : original(input, options));
    const user = userEvent.setup();
    open();
    await user.click(await screen.findByRole('button', { name: 'Delete request request-1' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Deletion failed.');
    expect(screen.getByRole('link', { name: /View runs/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Delete request request-1' })).toBeEnabled();
  });

  it.each([['', null], [' 0 9 * * 1 ', '0 9 * * 1']])(
    'submits a URL with cron %j and opens its runs', async (cron, expected) => {
      const fetcher = serve();
      const user = userEvent.setup();
      open();
      await user.click(screen.getByRole('button', { name: 'New request' }));
      await user.type(screen.getByLabelText('Website URL'), 'https://example.com/');
      if (cron) { await user.type(screen.getByLabelText(/Cron expression/), cron); }
      await user.click(screen.getByRole('button', { name: 'Create request' }));
      expect(await screen.findByRole('heading', { name: 'Request runs' })).toBeInTheDocument();
      const post = fetcher.mock.calls.find(([, options]) => options?.method === 'POST');
      expect(post?.[0]).toBe(base);
      expect(JSON.parse(post?.[1]?.body as string)).toEqual({
        url: 'https://example.com/', cron_expression: expected,
      });
    },
  );

  it.each(['120', '0.5'])('submits fetch timeout %s in seconds', async (timeout) => {
    const fetcher = serve();
    const user = userEvent.setup();
    open();
    await user.click(screen.getByRole('button', { name: 'New request' }));
    await user.type(screen.getByLabelText('Website URL'), 'https://example.com/');
    await user.type(screen.getByLabelText(/Fetch timeout/), timeout);
    await user.click(screen.getByRole('button', { name: 'Create request' }));
    expect(await screen.findByRole('heading', { name: 'Request runs' })).toBeInTheDocument();
    const post = fetcher.mock.calls.find(([, options]) => options?.method === 'POST');
    expect(JSON.parse(post?.[1]?.body as string)).toEqual({
      url: 'https://example.com/', cron_expression: null, fetch_timeout_seconds: Number(timeout),
    });
  });

  it.each(['', '1', '500'])('submits maximum pages %j with a default of 50', async (limit) => {
    const fetcher = serve();
    const user = userEvent.setup();
    open();
    await user.click(screen.getByRole('button', { name: 'New request' }));
    await user.type(screen.getByLabelText('Website URL'), 'https://example.com/');
    const input = screen.getByLabelText(/Maximum pages/);
    expect(input).toHaveAttribute('placeholder', '50');
    if (limit) { await user.type(input, limit); }
    await user.click(screen.getByRole('button', { name: 'Create request' }));
    expect(await screen.findByRole('heading', { name: 'Request runs' })).toBeInTheDocument();
    const post = fetcher.mock.calls.find(([, options]) => options?.method === 'POST');
    expect(JSON.parse(post?.[1]?.body as string)).toEqual({
      url: 'https://example.com/', cron_expression: null,
      ...(limit ? { max_pages: Number(limit) } : {}),
    });
  });

  it.each(['0', '-1', '1.5'])('rejects invalid maximum pages %s before submission', async (limit) => {
    const fetcher = serve();
    const user = userEvent.setup();
    open();
    await user.click(screen.getByRole('button', { name: 'New request' }));
    await user.type(screen.getByLabelText('Website URL'), 'https://example.com/');
    await user.type(screen.getByLabelText(/Maximum pages/), limit);
    await user.click(screen.getByRole('button', { name: 'Create request' }));
    expect(screen.getByLabelText(/Maximum pages/)).toBeInvalid();
    expect(fetcher.mock.calls.some(([, options]) => options?.method === 'POST')).toBe(false);
  });

  it.each(['', '1', '4', '1.5'])('submits maximum depth %j with a default of 3', async (depth) => {
    const fetcher = serve();
    const user = userEvent.setup();
    open();
    await user.click(screen.getByRole('button', { name: 'New request' }));
    await user.type(screen.getByLabelText('Website URL'), 'https://example.com/');
    const input = screen.getByLabelText(/Maximum depth/);
    expect(input).toHaveAttribute('placeholder', '3');
    if (depth) { await user.type(input, depth); }
    await user.click(screen.getByRole('button', { name: 'Create request' }));
    expect(await screen.findByRole('heading', { name: 'Request runs' })).toBeInTheDocument();
    const post = fetcher.mock.calls.find(([, options]) => options?.method === 'POST');
    expect(JSON.parse(post?.[1]?.body as string)).toEqual({
      url: 'https://example.com/', cron_expression: null,
      ...(depth ? { max_depth: Number(depth) } : {}),
    });
  });

  it.each(['0', '-1'])('rejects invalid maximum depth %s before submission', async (depth) => {
    const fetcher = serve();
    const user = userEvent.setup();
    open();
    await user.click(screen.getByRole('button', { name: 'New request' }));
    await user.type(screen.getByLabelText('Website URL'), 'https://example.com/');
    await user.type(screen.getByLabelText(/Maximum depth/), depth);
    await user.click(screen.getByRole('button', { name: 'Create request' }));
    expect(screen.getByLabelText(/Maximum depth/)).toBeInvalid();
    expect(fetcher.mock.calls.some(([, options]) => options?.method === 'POST')).toBe(false);
  });

  it('shows backend cron validation without losing the entered values', async () => {
    serve();
    vi.stubGlobal('fetch', vi.fn(async (_input: string, options?: RequestInit) =>
      options?.method === 'POST'
        ? Response.json({ detail: [{ loc: ['body', 'cron_expression'], msg: 'Invalid cron expression.' }] }, { status: 422 })
        : Response.json([]),
    ));
    const user = userEvent.setup();
    open();
    await user.click(screen.getByRole('button', { name: 'New request' }));
    await user.type(screen.getByLabelText('Website URL'), 'https://example.com/');
    await user.type(screen.getByLabelText(/Cron expression/), 'bad cron');
    await user.click(screen.getByRole('button', { name: 'Create request' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Invalid cron expression.');
    expect(screen.getByLabelText(/Cron expression/)).toHaveValue('bad cron');
  });

  it('navigates from a request through a run to safely rendered file content', async () => {
    serve();
    const user = userEvent.setup();
    const { container } = open();
    await user.click(await screen.findByRole('link', { name: /View runs/ }));
    await user.click(await screen.findByRole('link', { name: /View file/ }));
    expect(await screen.findByRole('heading', { name: 'Generated file' })).toBeInTheDocument();
    await waitFor(() => expect(container.querySelector('pre')).toHaveTextContent('<script>alert("test")</script>'));
    expect(container.querySelector('script')).toBeNull();
    expect(screen.getByRole('link', { name: /Download/ })).toHaveAttribute('download', 'llms.txt');
  });

  it('does not offer a file link for pending runs without files', async () => {
    serve({ [`${base}/request-1/runs`]: [{ ...run, status: 'pending', file_id: null }] });
    open('/requests/request-1/runs');
    expect(await screen.findByText('Pending')).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /View file/ })).not.toBeInTheDocument();
    expect(screen.getByText(/refresh.*5 seconds/i)).toBeInTheDocument();
  });

  it.each([0, 3, 6])('shows attempts %i in the runs table', async (attempts) => {
    serve({ [`${base}/request-1/runs`]: [{ ...run, retry_count: attempts }] });
    open('/requests/request-1/runs');
    expect(await screen.findByRole('columnheader', { name: 'Attempts' })).toBeInTheDocument();
    expect(screen.getByRole('cell', { name: String(attempts) })).toBeInTheDocument();
  });

  it('shows an empty request list', async () => {
    serve({ [base]: [] });
    open();
    expect(await screen.findByText('No requests yet')).toBeInTheDocument();
  });

  it('retries a failed list load', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockResolvedValue(Response.json([request])));
    const user = userEvent.setup();
    open();
    expect(await screen.findByRole('alert')).toHaveTextContent(/backend/i);
    await user.click(screen.getByRole('button', { name: 'Try again' }));
    expect(await screen.findByRole('link', { name: /View runs/ })).toBeInTheDocument();
  });

  it('explains unavailable files on direct navigation', async () => {
    serve({ [`${base}/request-1/runs/run-1`]: { ...run, file_id: null, status: 'pending' } });
    open('/requests/request-1/runs/run-1/file');
    expect(await screen.findByText('File not available yet')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /Back to runs/ })).toHaveAttribute('href', '/requests/request-1/runs');
  });

  it('does not redirect back when a creation finishes after leaving the form', async () => {
    const fetcher = serve();
    const original = fetcher.getMockImplementation()!;
    let finish!: (response: Response) => void;
    const pending = new Promise<Response>((resolve) => { finish = resolve; });
    fetcher.mockImplementation((input, options) => options?.method === 'POST' ? pending : original(input, options));
    const user = userEvent.setup();
    open();
    await user.click(screen.getByRole('button', { name: 'New request' }));
    await user.type(screen.getByLabelText('Website URL'), 'https://example.com/');
    await user.click(screen.getByRole('button', { name: 'Create request' }));
    await user.click(await screen.findByRole('link', { name: /View runs/ }));
    await user.click(await screen.findByRole('link', { name: /View file/ }));
    expect(await screen.findByRole('heading', { name: 'Generated file' })).toBeInTheDocument();
    await act(async () => { finish(Response.json(request, { status: 201 })); });
    expect(screen.getByRole('heading', { name: 'Generated file' })).toBeInTheDocument();
  });

  it('refreshes pending runs and reveals their completed files', async () => {
    vi.useFakeTimers();
    try {
      let calls = 0;
      vi.stubGlobal('fetch', vi.fn(async (input: string) => Response.json(
        input.endsWith('/runs')
          ? [calls++ === 0 ? { ...run, status: 'pending', file_id: null } : run]
          : request,
      )));
      await act(async () => { open('/requests/request-1/runs'); });
      expect(screen.getByText('Pending')).toBeInTheDocument();
      await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
      expect(screen.getByText('Success')).toBeInTheDocument();
      expect(screen.getByRole('link', { name: /View file/ })).toHaveAttribute('href', '/requests/request-1/runs/run-1/file');
      expect(screen.queryByText(/Pending runs refresh/)).not.toBeInTheDocument();
    } finally { vi.useRealTimers(); }
  });
});
