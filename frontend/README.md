# Frontend

React + TypeScript SPA built with Vite. Use Node.js 22.12+ and pnpm 11+.

```bash
cd frontend
pnpm install
pnpm dev
```

Open the local URL printed by Vite (normally http://localhost:5173). Start the backend API separately on port 8000 using the backend README. Generation also requires the database and background workers described there.

Vite proxies `/llms-txt` to `http://127.0.0.1:8000`. To use another backend, copy `.env.example` to `.env` and change `BACKEND_URL`, then restart Vite. The proxy also works with `pnpm preview`. No CORS configuration is needed for this same-origin setup.

## Pages

- `/requests`: submit an HTTP(S) URL with an optional cron expression, fetch timeout in seconds, maximum pages, and maximum depth, and browse existing requests. Blank schedules run once; cron schedules use UTC and are validated by the backend. Timeouts accept positive numbers, including fractions; leaving the field blank uses the server default (normally 600 seconds). Maximum pages accepts positive integers and defaults to 50 when blank. The crawl stops when that many unique links are collected and generates the file from those results. Maximum depth accepts positive numbers and defaults to 3 when blank: the root is level 0, and only levels below the limit are crawled. Pages are processed breadth-first, concurrently within each level. Creation opens the new request's runs.
- `/requests/:requestId/runs`: inspect runs, including their retry count, and open their generated files. Pending runs refresh every five seconds; a failed refresh offers a retry.
- `/requests/:requestId/runs/:runId/file`: view plain-text content and download `llms.txt`. Direct visits before generation completes refresh the run until output becomes available.

```bash
pnpm test       # component and API workflow tests (network fixtures)
pnpm build      # TypeScript checks and production bundle
pnpm preview   # local preview of dist/
```

For production, serve `dist/` with a history fallback to `index.html` for SPA routes, and reverse-proxy `/llms-txt` to the backend API. `BACKEND_URL` configures the Vite development/preview servers; configure the production reverse proxy separately.
