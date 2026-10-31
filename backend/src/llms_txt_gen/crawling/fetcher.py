import asyncio
import logging
import os
from datetime import timedelta
from typing import cast
from uuid import uuid4

import anyio
import httpx
from crawlee import ConcurrencySettings, Request
from crawlee.configuration import Configuration
from crawlee.crawlers import BeautifulSoupCrawler, BeautifulSoupCrawlingContext
from crawlee.events import LocalEventManager
from crawlee.statistics import Statistics
from crawlee.storage_clients import MemoryStorageClient
from crawlee.storages import RequestQueue

from llms_txt_gen.crawling.http_client import (
    FetchError,
    HtmlResponse,
    PublicHttpClient,
    create_http_client,
)
from llms_txt_gen.crawling.models import CrawlResponse, PageResult
from llms_txt_gen.crawling.parser import extract_page
from llms_txt_gen.urls import normalize_url

FETCH_TIMEOUT_SECONDS = float(os.environ.get("FETCH_TIMEOUT_SECONDS", "600"))

logger = logging.getLogger(__name__)


async def fetch_page(url: str) -> CrawlResponse:
    """Own each crawl's HTTP resources on the caller's event loop."""
    async with create_http_client() as client:
        return await _crawl_page(client, url)


async def _crawl_page(client: httpx.AsyncClient, url: str) -> CrawlResponse:
    """Recursively crawl normalized links on the original requested hostname."""
    requested_url = normalize_url(url)
    results: dict[str, PageResult] = {}
    failure = None
    storage_client = MemoryStorageClient()
    configuration = Configuration(purge_on_start=False)
    queue = await RequestQueue.open(
        name=f"page-{uuid4().hex}", storage_client=storage_client, configuration=configuration
    )
    crawler = BeautifulSoupCrawler(
        parser="html.parser",
        http_client=PublicHttpClient(client),
        storage_client=storage_client,
        request_manager=queue,
        event_manager=LocalEventManager(),
        configuration=configuration,
        statistics=Statistics.with_default_state(persistence_enabled=False),
        max_request_retries=2,
        use_session_pool=False,
        retry_on_blocked=False,
        configure_logging=False,
        concurrency_settings=ConcurrencySettings(max_concurrency=1, desired_concurrency=1),
        request_handler_timeout=timedelta(seconds=FETCH_TIMEOUT_SECONDS),
    )

    @crawler.router.default_handler
    async def handle_page(context: BeautifulSoupCrawlingContext) -> None:
        response = cast(HtmlResponse, context.http_response).response
        final_url = normalize_url(context.request.loaded_url or requested_url)
        page, links = await anyio.to_thread.run_sync(
            extract_page, response.text, final_url, context.soup, requested_url
        )
        await queue.add_requests(
            [Request.from_url(url, unique_key=url) for url in links],
            wait_time_between_batches=timedelta(0),
            wait_for_all_requests_to_be_added=True,
        )
        results.setdefault(final_url, page)
        metadata = await queue.get_metadata()
        # Crawlee marks this page handled after the handler returns. With one
        # concurrent request, exclude it to count only pages waiting to be fetched.
        queue_size = metadata.pending_request_count - 1
        logger.info(
            "Fetched page %s (status=%s, queue_size=%s)",
            final_url,
            response.status_code,
            queue_size,
        )

    @crawler.failed_request_handler
    async def handle_failure(context, error: Exception) -> None:
        nonlocal failure
        if context.request.unique_key == requested_url:
            failure = error

    deadline = asyncio.timeout(FETCH_TIMEOUT_SECONDS)
    try:
        async with deadline:
            await crawler.run([Request.from_url(requested_url, unique_key=requested_url)])
            if task := asyncio.current_task():
                if task.cancelling():
                    raise asyncio.CancelledError
    except TimeoutError as exc:
        raise FetchError(504, "The upstream page timed out.") from exc
    finally:
        await queue.drop()
    # Crawlee can consume cancellation while stopping a run; retain our deadline.
    if deadline.expired():
        raise FetchError(504, "The upstream page timed out.")
    if isinstance(failure, FetchError):
        raise failure
    if not results:
        raise FetchError(502, "Crawlee could not fetch the upstream page.") from failure
    # Different queued aliases may redirect to one final URL. Deduplicate output
    # only; Crawlee's unique keys handle scheduling and cycles without a visited set.
    return CrawlResponse(requested_url=requested_url, results=list(results.values()))
