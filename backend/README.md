# llms-txt-gen

Recursively crawl public HTML pages by following `a[href]` links on the starting
URL's hostname. Built with Python, FastAPI, Crawlee, HTTPX, and BeautifulSoup.
Crawling and deterministic `llms.txt` generation run in Procrastinate background
jobs. FastAPI creates and lists persisted runs without loading Crawlee.

## Run locally

Requires Python 3.12 or newer.

```bash
cd ~/dev/llms-txt-gen/backend
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -c requirements.lock -e '.[dev]'
python -m uvicorn llms_txt_gen.main:app --reload --log-config logging.json
```

If the virtual environment is already installed, run the last command after activation.

Interactive API documentation: <http://127.0.0.1:8000/docs>.

## Run in Docker

Build one image for both roles:

```bash
docker build -t llms-txt-gen .
```

Pass `DATABASE_URL` at runtime, pointing to a Postgres host reachable from the
container. For the local database on Docker Desktop, use `host.docker.internal`
instead of `127.0.0.1`. Initialize the database schema and migrations as described
below before starting the app.

```bash
export DATABASE_URL='postgresql://app_dev:local_dev_password@host.docker.internal:5432/llms_txt_dev'

# FastAPI server (the default role), available at http://localhost:8000/docs
docker run --rm --name llms-txt-server -p 8000:8000 \
  -e DATABASE_URL llms-txt-gen server

# Procrastinate worker, in a separate terminal with DATABASE_URL exported
docker run --rm --name llms-txt-worker \
  -e DATABASE_URL llms-txt-gen worker
```

The `server` role runs `llms_txt_gen.main:app`; `worker` runs
`llms_txt_gen.jobs.app`. Extra arguments after the role are forwarded to Uvicorn
or the Procrastinate worker, for example `llms-txt-gen worker --concurrency 4`.
The worker also starts a minimal HTTP listener on `0.0.0.0`, using `PORT` (default
`10000`), so it can deploy as a Render Web Service. Keep the Render Docker Command
as `docker-entrypoint.sh worker`. The listener serves an `ok` page from an isolated
temporary directory; it does not check job processing or database health. To
access it locally, add `-p 10000:10000` to the worker's `docker run` command.
Render's free Web Services can spin down during inactivity, stopping job
processing until the service wakes up; use a Background Worker for continuous
processing.

Both roles run as a non-root user. Environment files are excluded from the image;
you can also supply configuration with `docker run --env-file .env ...`, ensuring
the database hostname is reachable from the container.

The worker logs at INFO level, suppressing DEBUG messages such as heartbeat updates.
Server, access, and worker logs include a timestamp with milliseconds, log level,
and logger name. Timestamps use the process's local timezone (UTC in the Docker
image by default). The server uses `logging.json`; override `UVICORN_LOG_CONFIG`
to select another logging configuration file. Override `LOG_FORMAT` to customize
the worker's log format.

## Postgres and background jobs

Postgres access uses psycopg 3; background jobs use Procrastinate. The queue
configuration is `llms_txt_gen.jobs.app`. It loads `DATABASE_URL` from the
project's `.env` file without overriding an existing environment variable.
Copy `.env.example` to `.env` when setting up a new checkout. The example
credentials are for local development only; `.env` is ignored by Git.

The local Docker container is `llms-txt-postgres`, listening on
`127.0.0.1:5432`, with database `llms_txt_dev`. Its data persists in the
`llms-txt-postgres-data` Docker volume. Start an existing container with:

```bash
docker start llms-txt-postgres
```

For a fresh database, initialize Procrastinate's tables once and verify the
connection using the installed virtual environment:

```bash
procrastinate --app=llms_txt_gen.jobs.app schema --apply
procrastinate --app=llms_txt_gen.jobs.app healthchecks
```

Apply Procrastinate's version-specific migrations when upgrading an existing
schema. Application SQL migrations are stored separately under `migrations/`.
Run the generation worker in a separate terminal with the virtual environment
activated:

```bash
procrastinate --log-level info \
  --log-format '%(asctime)s %(levelname)s [%(name)s] %(message)s' \
  --app=llms_txt_gen.jobs.app worker
```

Both request and run jobs are registered with the shared app. The
`process_llms_txt_request` job accepts `request_id`, verifies that request exists,
and creates a pending run and its generation job in one transaction, using the
same operation as `POST /llms-txt/requests/{request_id}/runs`.
For scheduled requests, that transaction also enqueues the next cron occurrence
and updates the request's `job_id` to that next job's ID. Cron expressions use UTC.
Request creation enqueues the first execution at the next matching cron time when
a cron expression is set, or immediately otherwise. For recurring requests, each
execution schedules the next matching time after now, skipping missed intervals.
Schedules survive server and worker restarts because the next job is in Postgres.
For finite schedules with a final year, attempting to schedule beyond the final
occurrence raises `CroniterBadDateError`. The transaction rolls back, preserving
the request's current `job_id`, and the worker retries under its existing policy.

To queue an unscheduled request explicitly:

```bash
procrastinate --app=llms_txt_gen.jobs.app defer process_llms_txt_request \
  '{"request_id":"REQUEST_UUID"}'
```

If run creation, generation enqueueing, scheduling the next occurrence, or saving
the next job ID fails, all writes roll back. Request jobs retry after 30 seconds
without a retry limit. Scheduled executions lock their request row and compare
their job ID with the request's current `job_id`; replaying a completed occurrence
does not create another run or fork its schedule. A run's generation failure has
its own retry policy and does not stop the request's schedule.

The run worker processes `process_llms_txt_run` jobs. Each job loads its run and processes
it only while its status is `pending`. It loads the URL from `llms_txt_requests`, crawls and renders it in the
background worker, and stores the generated content in
`llms_txt_files.content` with `type='text/markdown'`,
sets the run status to `success`, updates `timestamp`, and logs the resulting
`llms.txt`. After generation, one transaction locks the run with
`SELECT ... FOR UPDATE`, rechecks that it is pending, inserts a file with a new
UUID, and updates the run's nullable `file_id` and success status before
committing. If the locked run is no longer pending, it returns successfully
without inserting a file. Already completed or otherwise non-pending
runs are skipped. The task uses a Procrastinate retry strategy with a fixed
30-second delay. Before generation, it atomically increments the pending run's
persisted `retry_count`, committing the increment before crawling so failures
retain the count. Counts 1 through 5 allow generation. When the incremented count
exceeds 5, the same update marks the run `failed` and refreshes its timestamp;
the job returns successfully without another crawl. Completed runs and their
counts are preserved. Jobs for deleted runs finish without generation.
Each invocation logs the run ID, job ID, and persisted
retry count. Procrastinate's attempt counter does not control this limit.
Generation errors leave the run pending until it succeeds or reaches the cutoff.
Recovery of jobs abandoned by a hard-killed worker is not configured. Jobs already marked
failed must be explicitly retried; changing the task policy does not automatically
requeue them.

## Requests, runs, and files

The API follows the hierarchy `/llms-txt/requests/{request_id}/runs/{run_id}/files`:

| Method | Path | Behavior |
| --- | --- | --- |
| POST | `/llms-txt/requests` | Create a request and enqueue processing at the next cron time, or immediately without `cron_expression` |
| GET | `/llms-txt/requests` | List all requests, newest first |
| GET | `/llms-txt/requests/{request_id}` | Get one request |
| DELETE | `/llms-txt/requests/{request_id}` | Stop its job and delete the request, its runs, and their files |
| POST | `/llms-txt/requests/{request_id}/runs` | Create and enqueue a run for the request |
| GET | `/llms-txt/requests/{request_id}/runs` | List that request's runs, newest first |
| GET | `/llms-txt/requests/{request_id}/runs/{run_id}` | Get one run; `run_id` can be `latest` |
| DELETE | `/llms-txt/requests/{request_id}/runs/{run_id}` | Stop its job and delete the run and its file; requires a UUID |
| GET | `/llms-txt/requests/{request_id}/runs/{run_id}/files` | Get the single file referenced by the run's `file_id` |

Delete endpoints return HTTP 204 with no body, or HTTP 404 when the request or
run does not exist or the run belongs to another request. Deletion cancels queued
jobs and requests abortion of running jobs; completed job records remain unchanged.
Job cancellation and removal of requests, runs, and files share one transaction,
so a cleanup failure rolls back all changes. Run creation locks its parent request
to prevent orphaned runs during concurrent request deletion.

Create a request with:

```bash
curl -X POST http://127.0.0.1:8000/llms-txt/requests \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com/","fetch_timeout_seconds":120}'
```

The response is HTTP 201 with `id`, normalized `url`, `created_at`, `timestamp`,
nullable `cron_expression`, nullable `configs`, and nullable `job_id`. Creating a request enqueues a
`process_llms_txt_request` job and saves its ID in the same transaction. The job
is scheduled at the next cron time when a cron expression is set, or immediately
otherwise.
The endpoint does not create a run; the worker creates it when processing the job.
Without a cron expression (omitted or `null`), processing runs once.

The optional `fetch_timeout_seconds` field sets the total crawl deadline and page
handler timeout for every run of the request. It must be a positive, finite JSON
number within Crawlee's duration range (at most 43,199,999,999,999 seconds); fractional
seconds are supported. Omit it or pass `null` to use `FETCH_TIMEOUT_SECONDS`
(600 seconds by default). The request's nullable `configs` TEXT column stores
`{"fetch_timeout_seconds":120.0}` as JSON text; responses expose that JSON text
in `configs`. Existing requests retain `configs=NULL` and use the default.

To schedule recurring generation, include `cron_expression`:

```bash
curl -X POST http://127.0.0.1:8000/llms-txt/requests \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com/","cron_expression":"0 9 * * 1"}'
```

This schedules the first execution for the next Monday at 09:00 UTC, then repeats
on Mondays at 09:00 UTC. The request and initial job are created in one transaction;
the worker creates a run when the job executes. The request's `job_id` is the Procrastinate job ID as text,
and changes when each occurrence schedules its successor. Any enqueue or job ID
update failure rolls back the entire request creation.

Cron expressions use croniter syntax (including aliases such as `@daily`).
Invalid expressions, including expressions without a reachable future occurrence,
return HTTP 422 before persistence. The URL must use HTTP or HTTPS; missing,
invalid, or extra payload fields also return HTTP 422.

Create a pending run for an existing request with no request body:

```bash
curl -X POST http://127.0.0.1:8000/llms-txt/requests/REQUEST_UUID/runs
```

The response is HTTP 201 with `{"id":"<run_uuid>"}`. The run references the existing
request and starts with `status='pending'`, `file_id=null`, `num_links=null`, and
`retry_count=0`. The run and its Procrastinate generation job are inserted, and the job's integer ID is saved as
text in the run's `job_id`, in one Postgres transaction. If either insert or the
job ID update fails, all three operations roll back and the existing request
remains. The worker loads the URL from that request after commit; jobs for the
same run share a Procrastinate execution lock. Multiple runs can belong to the
same request.

```bash
curl http://127.0.0.1:8000/llms-txt/requests
curl http://127.0.0.1:8000/llms-txt/requests/REQUEST_UUID
curl http://127.0.0.1:8000/llms-txt/requests/REQUEST_UUID/runs
curl http://127.0.0.1:8000/llms-txt/requests/REQUEST_UUID/runs/RUN_UUID
curl http://127.0.0.1:8000/llms-txt/requests/REQUEST_UUID/runs/latest
curl http://127.0.0.1:8000/llms-txt/requests/REQUEST_UUID/runs/RUN_UUID/files
```

Run responses include `id`, `request_id`, `status`, `file_id`, `created_at`,
`timestamp`, nullable `job_id`, nullable integer `num_links`, and integer
`retry_count`. Successful runs save `num_links` as the number of retained links in
the crawl results, including the starting page and links retained without metadata
after 4xx fetch failures, in the same transaction as the generated file and success status.
`latest` returns the run with the newest
`created_at` for that request, regardless of status; a request with no runs
returns HTTP 404 for `latest`.
The file endpoint requires a concrete run UUID, reads `llms_txt_runs.file_id`,
and returns one JSON object with `id`, `type`, `content`, `created_at`, and `timestamp`.
It returns HTTP 404 until the run has a generated file. There is no file ID path
parameter or `latest` selector on the file endpoint.

Nested routes verify that the request exists and that the run belongs to it.
Unknown requests, unknown runs, and runs under the wrong request return HTTP 404.
Malformed UUIDs return HTTP 422. Lists return `[]` when the existing request has no
runs or there are no requests. The former top-level `/llms-txt/runs` and
`/llms-txt/files/{file_id}` routes are removed.

These endpoints require `DATABASE_URL` and the migrations under `migrations/`,
applied once in filename order. All routes are documented at `/docs`.

Database access lives in `RequestRepository`, `RunRepository`, and `FileRepository`
under `llms_txt_gen.repositories`. Each accepts a psycopg async connection and
exposes `create`, `read`, `modify`, `delete`, and `list`, returning immutable
`Request`, `Run`, and `File` dataclasses from `llms_txt_gen.repositories.entities`.
Requests and runs list newest first with ID as a tie breaker; files list by
descending ID. Empty lists return `[]`. Requests accept an optional
`job_id: str | None` and nullable `cron_expression` on creation. With no supplied
job ID, creation atomically enqueues a request job at the next cron time, or
immediately when no cron expression is set. Updating a request's `job_id` to `None`
schedules its next cron job in the same transaction when a cron expression is
present. Runs enqueue their processing job and save its ID atomically on creation.

`RunRepository.list_by_request_id(request_id, limit=None)` returns only that request's runs
in descending `created_at` order, matching the
`ak_llms_txt_runs_request_id (request_id, created_at DESC)` index. Runs with
identical creation times have no defined relative order in these two methods.
Pass a nonnegative `limit` to bound the list; `limit=0` returns an empty list.
`get_latest_by_request_id(request_id)` returns the first run in that order,
or raises `NoDataFound` if there are no runs for the request.

`modify(id, callback)` selects the current row with `FOR UPDATE`, invokes a sync
or async callback, and writes the returned replacement in the same transaction.
Use `dataclasses.replace` to change fields; returning the unchanged entity skips
the write. Requests allow changes only to `job_id`; runs allow changes only to
`status`, `file_id`, `num_links`, and `retry_count`. IDs and database-generated
timestamps cannot be replaced.
Request and run updates refresh `timestamp` automatically and preserve `created_at`.
Files reject every `modify` call with `ValueError` without invoking the callback.

```python
from dataclasses import replace

from llms_txt_gen.database import database_connection
from llms_txt_gen.repositories import RequestRepository, RunRepository

async with database_connection() as connection:
    runs = RunRepository(connection)
    async with connection.transaction():
        request = await RequestRepository(connection).create(url="https://example.com/")
        run = await runs.create(request_id=request.id)
    run = await runs.modify(run.id, lambda current: replace(current, status="failed"))
```

Application connections use `autocommit=False`. SQL outside an explicit transaction
block is committed when the connection context exits successfully and rolled back
if an exception escapes. Each repository operation manages its own explicit
transaction or savepoint. Wrap multiple operations
in `connection.transaction()` to commit them together; use that same connection
for writes within a modify callback. Missing IDs on read or delete, and on request
or run modify, raise `NoDataFound`. Errors in `repositories.errors` distinguish unique, foreign-key,
not-null, and check constraint violations, serialization failures, deadlocks, and
connection failures. Driver exceptions remain available as `__cause__`;
constraint names are available as `constraint_name`. Failed operations roll back
before raising, including when nested within a caller's transaction.

The base application schema is defined in `migrations/001_create_schema.sql`.
Apply all migrations once in filename order to a fresh database after installing
Procrastinate's schema:

```bash
docker exec -i llms-txt-postgres \
  psql -U app_dev -d llms_txt_dev -v ON_ERROR_STOP=1 --single-transaction \
  < migrations/001_create_schema.sql
docker exec -i llms-txt-postgres \
  psql -U app_dev -d llms_txt_dev -v ON_ERROR_STOP=1 --single-transaction \
  < migrations/002_add_job_ids.sql
docker exec -i llms-txt-postgres \
  psql -U app_dev -d llms_txt_dev -v ON_ERROR_STOP=1 --single-transaction \
  < migrations/003_add_request_schedule.sql
docker exec -i llms-txt-postgres \
  psql -U app_dev -d llms_txt_dev -v ON_ERROR_STOP=1 --single-transaction \
  < migrations/004_drop_request_status.sql
docker exec -i llms-txt-postgres \
  psql -U app_dev -d llms_txt_dev -v ON_ERROR_STOP=1 --single-transaction \
  < migrations/005_add_run_num_links.sql
docker exec -i llms-txt-postgres \
  psql -U app_dev -d llms_txt_dev -v ON_ERROR_STOP=1 --single-transaction \
  < migrations/006_add_request_configs.sql
docker exec -i llms-txt-postgres \
  psql -U app_dev -d llms_txt_dev -v ON_ERROR_STOP=1 --single-transaction \
  < migrations/007_add_run_retry_count.sql
```

This creates `public.llms_txt_requests`, `public.llms_txt_runs`, and
`public.llms_txt_files`, plus `ak_llms_txt_runs_request_id` on
`llms_txt_runs (request_id, created_at DESC)`. The base migration replaces the
historical migration chain and is intended for a fresh database.

For databases that already have the base schema, apply only the remaining
migrations in order. `002_add_job_ids.sql` adds nullable text `job_id` columns to
requests and runs; existing records keep `job_id=NULL`.
`003_add_request_schedule.sql` adds nullable text `cron_expression` and non-null
text `status` to requests; existing requests receive `cron_expression=NULL` and
`status='created'`. Existing unscheduled requests do not automatically create runs.
`004_drop_request_status.sql` removes the request `status` column. Requests have
no status field; run status continues to track generation progress.
`005_add_run_num_links.sql` adds nullable integer `num_links` to runs; existing
runs retain `num_links=NULL` until explicitly updated.
`006_add_request_configs.sql` adds nullable TEXT `configs` to requests for JSON
configuration, including per-request `fetch_timeout_seconds`.

`007_add_run_retry_count.sql` adds `retry_count INT NOT NULL DEFAULT 0` to runs;
existing rows are initialized to zero.

## Background crawling

The Procrastinate worker crawls sites for queued generation runs. Each page result
contains only its normalized `url`, `title`, and `description`. Links are extracted
internally for recursive crawling. FastAPI handles run persistence and job
submission; it has no crawler initialization, crawl thread, or synchronous
generation endpoints.

### Crawl behavior

- Fetch the starting page, then recursively enqueue HTTP(S) links with the exact
  hostname of the original normalized starting URL. Subdomains are distinct;
  schemes and ports do not affect hostname matching. This scope remains fixed
  even when a page redirects to a different hostname.
- Resolve relative URLs against the final page URL, honoring the first HTML
  `<base href>` element. Ignore malformed base URLs.
- Use each normalized URL as the Crawlee request's explicit `unique_key`.
  Crawlee's request queue deduplicates requests across pages and terminates
  cycles without a separate application-level visited set. Retries can still
  make additional network requests for a queued URL.
- Preserve query parameter values. Skip empty hrefs, fragment-only links, embedded
  credentials, malformed URLs, and schemes such as `mailto:`, `tel:`, and `javascript:`.
- Return the normalized starting `requested_url` and a `results` array of
  crawled pages and links retained after 4xx fetch failures. Unless skipped for a non-HTML
  media type, the starting page is always first, using its
  final URL after redirects; remaining pages follow completion order. Each result contains
  only its normalized final `url`, `title`, and `description`. Missing titles
  and descriptions are `null`. Extracted links are used internally for crawling.
- Different aliases can redirect to the same final URL. They are distinct queue
  keys and may cause repeated fetches of that destination, but it appears only
  once in the response. Redirect destinations still undergo public-address checks.
- Retain upstream HTTP 4xx responses and local 413 failures as links with
  `title=null` and `description=null`, including the starting page. Do not extract
  metadata or links from error responses. Continue crawling other queued pages.
  HTTP 429 is retried before retaining the link. Unsafe URLs are still rejected.
- Skip successful responses whose media type is neither HTML nor XHTML, including
  missing content types. Do not read their bodies or include them in the results.
  A non-HTML starting page returns an empty results array without failing the crawl.
- Skip linked pages with other fetch failures after their retries, retaining other results.
  Other failures on the starting page raise `FetchError`. If the overall crawl deadline
  expires, it raises a timeout error and the background job follows its retry policy.

There is no page-count or crawl-depth limit. The overall crawl deadline
and per-page network limits below still apply. The crawler does not execute
JavaScript or summarize content. The worker uses the Python functions below to
generate `llms.txt`.

### URL normalization

The starting URL is normalized before fetching. Redirect destinations and
discovered links use the same normalization rules:

- Remove fragments and empty query delimiters.
- Sort query parameters by decoded name, preserving the original value order for
  repeated names. Preserve duplicate entries and blank values; `flag` becomes `flag=`.
- Use UTF-8 form encoding consistently: spaces become `+`, literal plus signs
  become `%2B`, and escaped query separators remain escaped. Invalid UTF-8 query
  values are rejected rather than collapsed into replacement characters.
- Normalize scheme and hostname case, internationalized hostnames to IDNA, default
  ports, empty root paths to `/`, and literal dot segments via Pydantic's URL parser.
- Preserve path case, encoded path separators, non-root trailing slashes, query
  name/value case, meaningful parameters, and HTTP versus HTTPS.

For example:

```text
HTTPS://EXAMPLE.COM:443/docs?z=2&q=hello%20world&a=1#install
https://example.com/docs?a=1&q=hello+world&z=2
```

This policy targets documentation/content sites. Query ordering and encoding can
be significant for signed URLs and some servers; supporting those requires a
site-specific policy or a separate deduplication key that leaves the fetched URL
unchanged.

Possible future deduplication rules should be enabled per site: remove known
tracking parameters (such as `utm_*`, `gclid`, and `fbclid`), restrict parameters to
an allowlist, learn aliases from redirects and verified canonical links, and hash
cleaned content to find duplicates served at different URLs. Avoid globally
collapsing `www`, subdomains, trailing slashes, or documentation version/language
parameters; they can identify different pages.

### Fetch behavior

- Each generation job runs a recursive Crawlee `BeautifulSoupCrawler` with a uniquely
  named in-memory request queue that is dropped when the crawl finishes. Repeated
  and concurrent jobs do not reuse completed requests or page results.
- Crawlee runs on the Procrastinate worker's event loop. Each crawl owns its HTTP
  client, which closes when the crawl finishes. The server loads only the task
  definition; the worker imports the crawler when it executes a generation job.
- Crawlee owns the request lifecycle and HTML parsing. A custom client using
  Crawlee's public `HttpClient` interface preserves bounded public-only HTTP access.
- Each crawl starts with a desired concurrency of 25 pages and a maximum of 25.
  Crawlee can reduce concurrency under resource pressure. Set `CRAWL_CONCURRENCY`
  to a positive integer in the worker's environment or `.env` file to change both
  values, then restart the worker. This is separate from worker concurrency:
  five simultaneous generation jobs can fetch up to 125 pages at once.
- Page fetch logs include `pending_request_count`, covering queued and in-progress
  requests. A final timing log covers the complete `fetch_page` operation, including
  HTTP client setup and cleanup, on success, failure, or cancellation.
- Retry temporary network failures, timeouts, HTTP 429, and HTTP 5xx responses up
  to two times within the same overall crawl deadline. Do not retry unsafe URLs,
  non-HTML or oversized responses, or permanent upstream errors.
- Public HTTP(S) URLs only; embedded credentials and non-public IP destinations
  are rejected, including after redirects and when DNS returns mixed destinations.
- Pin connections to a validated IP while preserving the original Host header,
  TLS SNI, and certificate verification. Environment proxies are disabled.
- Disable upstream cookies and connection reuse to isolate hosts sharing an IP.
- Maximum five redirects per page, configurable overall crawl deadline, five-second connect
  timeout, and 15-second per-operation network timeout.
- Active HTTP requests share the overall crawl deadline and stop without retries
  when it expires.
- Maximum 4 MiB of decoded HTML. Accept `text/html` and `application/xhtml+xml`.
- User agent: `llms-txt-gen/0.1`.

Set `FETCH_TIMEOUT_SECONDS` in the worker's environment or `.env` file to configure
the overall crawl deadline in seconds (for example, `FETCH_TIMEOUT_SECONDS=120`).
When unset, it defaults to 600 seconds (10 minutes). The value is read when the
crawler module loads; restart the worker after changing it. Per-operation network
timeouts still apply.

| FetchError code | Meaning |
| --- | --- |
| 400 | Unsafe destination or embedded credentials |
| 4xx | Upstream client error; retain the link with null metadata |
| 413 | HTML exceeds 4 MiB; retain the link with null metadata |
| 502 | DNS/network failure, upstream non-4xx error status, or invalid/excessive redirects |
| 504 | Fetch deadline or network timeout |

Bind to localhost during development. A public deployment should add authentication,
request rate limits, and network egress restrictions.

## Generate llms.txt

Create a run through the persisted runs API above to generate and save a document
in the background. The worker calls `fetch_page(url)`, converts the crawl results
to an `LlmsTxtDocument` with `build_document`, and converts the document to Markdown
with `render_document`, without an LLM or a Markdown dependency.

The generator can also be used directly outside the server:

```python
import asyncio
from pathlib import Path

from llms_txt_gen.crawling.fetcher import fetch_page
from llms_txt_gen.generation.generator import build_document, render_document

crawl_results = asyncio.run(fetch_page("https://example.com/"))
document = build_document(crawl_results)
markdown = render_document(document)
Path("llms.txt").write_text(markdown, encoding="utf-8")
```

`build_document` and `render_document` are the generation module's public entry
points. Escaping helpers are private. The document models remain in `generation/models.py`.

The starting page's title supplies the H1, falling back to the requested hostname.
Its description supplies the optional blockquote. This also handles a starting
page that redirects, because the crawler returns that page first.

Successful pages with both a title and description appear under `Pages`.
Pages missing either field appear under `Optional` at the end; null, empty,
and whitespace-only values count as missing. Each section is sorted by
normalized URL with duplicates removed (first result wins). Empty sections are
omitted. Link labels use the page title, falling back to the
URL path. Descriptions become optional link notes. Discovered
links retained after 4xx fetch failures appear under `Optional` with URL path labels
and no description. Links absent from the crawl results are not included.

The renderer preserves supplied section and entry order, moves `Optional` to
the end, and omits empty sections and blank optional content. Metadata, including
the optional `details` paragraph, is treated as plain text: whitespace is
collapsed and Markdown/HTML characters are escaped. Link destinations must be
absolute HTTP(S) URLs and are encoded for Markdown. Output has LF newlines, a
trailing newline, and no BOM.

## Development

```bash
python -m pytest -q
ruff check .
ruff format --check .
```

Server tests verify the persisted routes and startup without crawler imports.
Crawler and generation tests call the worker functions directly; only outbound
DNS and HTTP are replaced with controlled fixtures. The suite does not need
internet access.

The runs API and repository integration tests use real Postgres and are skipped unless
`TEST_DATABASE_URL` is set. Each test creates and removes an isolated schema,
leaving application rows untouched. The database user needs permission to create
schemas. Run these tests against the local Docker database with:

```bash
TEST_DATABASE_URL='postgresql://app_dev:local_dev_password@127.0.0.1:5432/llms_txt_dev' \
  python -m pytest tests/test_runs.py tests/test_repositories.py -q
```

## Project structure

```text
src/llms_txt_gen/
  main.py       FastAPI application and persistence routers
  repositories/ Request/run/file CRUD, locked modifications, and persistence errors
    entities.py Immutable request, run, and file database entities
  routers/      Request, run, and file HTTP endpoints and shared dependencies
    models.py   HTTP request and response models
  jobs/
    __init__.py Shared Procrastinate app and task-module registration
    request_worker.py Request jobs that create and enqueue generation runs
    run_worker.py Run generation and atomic file/run completion
  crawling/
    fetcher.py  Recursive Crawlee lifecycle, link enqueueing, and retries
    http_client.py Crawlee adapter and bounded public-only network transport
    parser.py   HTML metadata and anchor extraction
    models.py   Page metadata and crawl result schemas
  generation/
    generator.py Crawl-result document builder and Markdown renderer
    models.py   Structured llms.txt document schemas
  urls.py       Shared URL normalization for requests, redirects, and links
tests/
  test_api.py   Server routes and startup without crawler imports
  test_requests.py Request persistence, validation, and creation without generation
  test_request_jobs.py Request jobs, atomic enqueueing, and background generation
  test_files.py File content, metadata, and lookup errors
  test_fetcher.py Crawl, extraction, and transport regression tests
  test_generation.py Background crawl-and-render generation tests
  test_recursive_crawl.py Cycles, normalized deduplication, failures, and redirect aliases
  test_urls.py  Canonical URL and idempotency tests
```
