export type CrawlerType = 'beautifulsoup' | 'playwright';

export interface RequestInfo {
  id: string;
  url: string;
  cron_expression: string | null;
  configs: string | null;
  created_at: string;
  timestamp: string;
  job_id: string | null;
}

export interface RunInfo {
  id: string;
  request_id: string;
  status: string;
  file_id: string | null;
  num_links: number | null;
  retry_count: number;
  created_at: string;
  timestamp: string;
  job_id: string | null;
}

export interface FileInfo {
  id: string;
  type: string;
  content: string;
  created_at: string;
  timestamp: string;
}

export const requestsPath = '/llms-txt/requests';
export const requestPath = (id: string) => `${requestsPath}/${encodeURIComponent(id)}`;
export const runsPath = (id: string) => `${requestPath(id)}/runs`;
export const runPath = (requestId: string, runId: string) => `${runsPath(requestId)}/${encodeURIComponent(runId)}`;

export class ApiError extends Error {
  constructor(message: string, public readonly status: number) {
    super(message);
    this.name = 'ApiError';
  }
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      ...options,
      headers: { Accept: 'application/json', ...options.headers },
    });
  } catch (error) {
    if (error instanceof Error && error.name === 'AbortError') { throw error; }
    throw new Error('Unable to reach the backend. Check that the service is running, then try again.');
  }
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const detail: unknown = body?.detail;
    let message = `The backend returned an error (${response.status}). Please try again.`;
    if (typeof detail === 'string') { message = detail; }
    if (Array.isArray(detail)) {
      message = detail.map((issue: { loc?: string[]; msg?: string }) =>
        `${issue.loc?.slice(1).join('.') || 'Request'}: ${issue.msg || 'Invalid value.'}`,
      ).join(' ');
    }
    throw new ApiError(message, response.status);
  }
  if (response.status === 204) { return undefined as T; }
  return response.json() as Promise<T>;
}

export function createRequest(url: string, cron: string, timeout = '', maxPages = '', crawler: CrawlerType = 'beautifulsoup'): Promise<RequestInfo> {
  return api(requestsPath, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      url: url.trim(), cron_expression: cron.trim() || null, crawler,
      ...(timeout.trim() ? { fetch_timeout_seconds: Number(timeout) } : {}),
      ...(maxPages.trim() ? { max_pages: Number(maxPages) } : {}),
    }),
  });
}
