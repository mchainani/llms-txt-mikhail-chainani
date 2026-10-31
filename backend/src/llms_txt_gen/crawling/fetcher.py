import asyncio
import json
import logging
import os
from datetime import timedelta
from time import perf_counter
from typing import cast
from uuid import uuid4

import anyio
import httpx
from crawlee import ConcurrencySettings, Request
from crawlee.configuration import Configuration
from crawlee.crawlers import BeautifulSoupCrawler
from crawlee.events import LocalEventManager
from crawlee.statistics import Statistics
from crawlee.storage_clients import MemoryStorageClient
from crawlee.storages import RequestQueue

from llms_txt_gen.crawling.http_client import (
    MAX_HTML_BYTES,
    FetchError,
    HtmlResponse,
    PublicHttpClient,
    create_http_client,
)
from llms_txt_gen.crawling.models import CrawlResponse, PageResult
from llms_txt_gen.crawling.parser import extract_page
from llms_txt_gen.urls import normalize_url

FETCH_TIMEOUT_SECONDS = float(os.environ.get("FETCH_TIMEOUT_SECONDS", "600"))
CRAWL_CONCURRENCY = int(os.environ.get("CRAWL_CONCURRENCY", "25"))
DEFAULT_MAX_PAGES = 50

logger = logging.getLogger(__name__)


async def fetch_page(url: str, configs: str | None = None) -> CrawlResponse:
    """Own each crawl's HTTP resources on the caller's event loop."""
    started = perf_counter()
    try:
        async with create_http_client() as client:
            return await _crawl_page(client, url, configs)
    finally:
        logger.info(
            "Fetch page operation for %s finished in %.3f seconds", url, perf_counter() - started
        )


async def _crawl_page(
    client: httpx.AsyncClient, url: str, configs: str | None = None
) -> CrawlResponse:
    """Recursively crawl normalized links on the original requested hostname."""
    requested_url = normalize_url(url)
    options = json.loads(configs) if configs is not None else {}
    max_pages = options.get("max_pages")
    if max_pages is None:
        max_pages = DEFAULT_MAX_PAGES
    timeout_seconds = options.get("fetch_timeout_seconds")
    if timeout_seconds is None:
        timeout_seconds = FETCH_TIMEOUT_SECONDS
    crawler_mode = options.get("crawler") or "beautifulsoup"
    if crawler_mode not in {"beautifulsoup", "playwright"}:
        raise ValueError(f"Unknown crawler: {crawler_mode}")
    render_wait = options.get("render_wait_seconds")
    if render_wait is None:
        render_wait = 1.0
    deadline_at = asyncio.get_running_loop().time() + timeout_seconds
    results: dict[str, PageResult] = {}
    failure = None
    starting_page_skipped = False
    storage_client = MemoryStorageClient()
    configuration = Configuration(purge_on_start=False)
    queue = await RequestQueue.open(
        name=f"page-{uuid4().hex}", storage_client=storage_client, configuration=configuration
    )
    crawler_options = dict(
        storage_client=storage_client,
        request_manager=queue,
        event_manager=LocalEventManager(),
        configuration=configuration,
        statistics=Statistics.with_default_state(persistence_enabled=False),
        max_request_retries=2,
        use_session_pool=False,
        retry_on_blocked=False,
        configure_logging=False,
        request_handler_timeout=timedelta(seconds=timeout_seconds),
    )
    if crawler_mode == "playwright":
        from llms_txt_gen.crawling.browser import create_browser_crawler

        crawler = create_browser_crawler(
            timeout_seconds=timeout_seconds, deadline_at=deadline_at, **crawler_options
        )
    else:
        crawler = BeautifulSoupCrawler(
            parser="html.parser",
            http_client=PublicHttpClient(client, deadline_at=deadline_at),
            concurrency_settings=ConcurrencySettings(
                max_concurrency=CRAWL_CONCURRENCY, desired_concurrency=CRAWL_CONCURRENCY
            ),
            **crawler_options,
        )

    def retain_page(page: PageResult) -> bool:
        # No await: the check and insertion run together on the crawl's event loop,
        # including when other handlers resume after extracting pages in threads.
        if len(results) >= max_pages:
            return False
        results.setdefault(page.url, page)
        if len(results) >= max_pages:
            logger.info("Reached result limit of %s pages for %s", max_pages, requested_url)
            crawler.stop(reason=f"Collected {max_pages} unique result links.")
            return False
        return True

    async def process_page(context) -> None:
        nonlocal starting_page_skipped
        if len(results) >= max_pages:
            return
        final_url = normalize_url(context.request.loaded_url or requested_url)
        if crawler_mode == "playwright":
            response = context.response
            status_code = response.status
            headers = await response.all_headers()
            skip_reason = None
            if 400 <= status_code < 500:
                retain_page(PageResult(url=final_url, title=None, description=None))
                return
            # DOMContentLoaded alone can precede React hydration or asynchronous rendering.
            await context.page.wait_for_timeout(render_wait * 1000)
            html = await context.page.content()
            if len(html.encode("utf-8")) > MAX_HTML_BYTES:
                skip_reason = "Rendered HTML exceeds the 2 MiB limit"
            soup = None
            final_url = normalize_url(context.page.url)
        else:
            response = cast(HtmlResponse, context.http_response).response
            status_code = response.status_code
            headers = response.headers
            skip_reason = response.extensions.get("llms_txt_gen_skip_reason")
            html = response.text
            soup = context.soup
        media_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if skip_reason is None and media_type not in {"text/html", "application/xhtml+xml"}:
            skip_reason = f"unsupported content type: {media_type or 'missing'}"
        if skip_reason is not None:
            if context.request.unique_key == requested_url:
                starting_page_skipped = True
            logger.info("Skipped page %s (reason=%s)", final_url, skip_reason)
            return
        page, links = await anyio.to_thread.run_sync(
            extract_page, html, final_url, soup, requested_url
        )
        # Record the starting page before its children can run during enqueue.
        if retain_page(page):
            await queue.add_requests(
                [Request.from_url(url, unique_key=url) for url in links],
                wait_time_between_batches=timedelta(0),
                wait_for_all_requests_to_be_added=True,
            )
        metadata = await queue.get_metadata()
        logger.info(
            "Fetched page %s (status=%s, current_page_count=%s, pending_request_count=%s)",
            final_url,
            status_code,
            len(results),
            metadata.pending_request_count,
        )

    @crawler.router.default_handler
    async def handle_page(context):
        async with asyncio.timeout_at(deadline_at):
            await process_page(context)

    @crawler.failed_request_handler
    async def handle_failure(context, error: Exception) -> None:
        nonlocal failure
        if len(results) >= max_pages:
            return
        if isinstance(error, FetchError) and 400 <= error.status_code < 500 and error.url:
            retain_page(PageResult(url=error.url, title=None, description=None))
            logger.info(
                "Retained page %s without metadata (status=%s)", error.url, error.status_code
            )
            return
        if context.request.unique_key == requested_url:
            failure = error

    deadline = asyncio.timeout_at(deadline_at)
    try:
        async with deadline:
            await crawler.run([Request.from_url(requested_url, unique_key=requested_url)])
            if task := asyncio.current_task():
                if task.cancelling():
                    raise asyncio.CancelledError
    except Exception as exc:
        # Cancellation during Crawlee startup can surface as a statistics error.
        if isinstance(exc, TimeoutError) or deadline.expired():
            # Once the result limit is met, a slow in-flight page must not turn
            # the completed collection into a failed generation run.
            if len(results) < max_pages:
                raise FetchError(504, "The upstream page timed out.") from exc
        else:
            raise
    finally:
        await queue.drop()
    # Crawlee can consume cancellation while stopping a run; retain our deadline.
    if deadline.expired() and len(results) < max_pages:
        raise FetchError(504, "The upstream page timed out.")
    if isinstance(failure, FetchError):
        raise failure
    if not results and not starting_page_skipped:
        raise FetchError(502, "Crawlee could not fetch the upstream page.") from failure
    # Different queued aliases may redirect to one final URL. Deduplicate output
    # only; Crawlee's unique keys handle scheduling and cycles without a visited set.
    return CrawlResponse(requested_url=requested_url, results=list(results.values()))
