"""Opt-in Playwright connection to a separately managed Chromium sidecar."""

import asyncio
import logging
import os
from datetime import timedelta

import anyio
import httpx
from crawlee import ConcurrencySettings
from crawlee.browsers import BrowserPool, PlaywrightBrowserController, PlaywrightBrowserPlugin
from crawlee.crawlers import PlaywrightCrawler
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from llms_txt_gen.crawling.http_client import FetchError, resolve_public_address

logger = logging.getLogger(__name__)


class SidecarController(PlaywrightBrowserController):
    """Close only this crawl's incognito contexts, then disconnect from CDP."""

    def __init__(self, connection, *, deadline_at: float, **kwargs):
        super().__init__(connection, **kwargs)
        self.connection = connection
        self.owned_contexts = set()
        self.deadline_at = deadline_at
        self.expiry_tasks = set()

    async def new_page(self, **kwargs):
        async with asyncio.timeout_at(self.deadline_at):
            page = await super().new_page(**kwargs)
        self.owned_contexts.add(page.context)

        async def expire_context():
            await asyncio.sleep(max(0, self.deadline_at - asyncio.get_running_loop().time()))
            await page.context.close()

        task = asyncio.create_task(expire_context())
        self.expiry_tasks.add(task)
        return page

    async def close(self, *, force=False):
        if self.pages_count and not force:
            raise ValueError("Cannot disconnect while pages are open.")
        for task in self.expiry_tasks:
            task.cancel()
        await asyncio.gather(*self.expiry_tasks, return_exceptions=True)
        self.expiry_tasks.clear()
        for context in self.owned_contexts:
            await context.close()
        self.owned_contexts.clear()
        # Browser.close on a CDP connection disconnects; the sidecar owns Chromium.
        await self.connection.close()


class SidecarPlugin(PlaywrightBrowserPlugin):
    """Use Crawlee's public browser plugin interface without launching a browser."""

    def __init__(self, endpoint: str, concurrency: int, deadline_at: float):
        super().__init__(
            use_incognito_pages=True,
            max_open_pages_per_browser=concurrency,
            browser_new_context_options={
                "service_workers": "block",
                "accept_downloads": False,
                "user_agent": "llms-txt-gen/0.1",
            },
        )
        self.endpoint = endpoint
        self.deadline_at = deadline_at
        self.remote_playwright = None

    @property
    def active(self):
        return self.remote_playwright is not None

    async def __aenter__(self):
        if self.active:
            raise RuntimeError("Sidecar plugin is already active.")
        self.remote_playwright = await async_playwright().start()
        return self

    async def __aexit__(self, *args):
        if self.remote_playwright:
            await self.remote_playwright.stop()
            self.remote_playwright = None

    async def new_browser(self):
        if self.remote_playwright is None:
            raise RuntimeError("Sidecar plugin is not active.")
        async with asyncio.timeout_at(self.deadline_at):
            connection = await self.remote_playwright.chromium.connect_over_cdp(
                self.endpoint, timeout=15_000
            )
        return SidecarController(
            connection,
            deadline_at=self.deadline_at,
            use_incognito_pages=True,
            max_open_pages_per_browser=self.max_open_pages_per_browser,
            header_generator=None,
        )


async def route_public_request(route):
    """Check navigations, redirects and subresources; not an IP-pinned transport."""
    request = route.request
    try:
        await anyio.to_thread.run_sync(
            resolve_public_address, httpx.URL(request.url), abandon_on_cancel=True
        )
    except (FetchError, ValueError, httpx.InvalidURL):
        logger.info("Blocked browser request to %s", request.url)
        await route.abort("blockedbyclient")
        return
    if request.resource_type in {"image", "media", "font"}:
        await route.abort()
        return
    # Playwright does not route subsequent HTTP redirect hops. Do not let the
    # browser follow them: fetch one response, then fulfill only non-redirects.
    response = await route.fetch(max_redirects=0, timeout=15_000)
    try:
        if 300 <= response.status < 400:
            logger.info("Blocked browser HTTP redirect from %s", request.url)
            await route.abort("blockedbyclient")
        else:
            await route.fulfill(response=response)
    finally:
        await response.dispose()


def create_browser_crawler(*, timeout_seconds: float, deadline_at: float, **kwargs):
    endpoint = os.environ.get("PLAYWRIGHT_CDP_URL")
    if not endpoint:
        raise FetchError(502, "Playwright mode requires PLAYWRIGHT_CDP_URL for a Chromium sidecar.")
    concurrency = int(os.environ.get("PLAYWRIGHT_CONCURRENCY", "3"))
    if concurrency <= 0:
        raise ValueError("PLAYWRIGHT_CONCURRENCY must be positive.")
    crawler = PlaywrightCrawler(
        browser_pool=BrowserPool(plugins=[SidecarPlugin(endpoint, concurrency, deadline_at)]),
        concurrency_settings=ConcurrencySettings(
            max_concurrency=concurrency, desired_concurrency=concurrency
        ),
        navigation_timeout=timedelta(seconds=min(30, timeout_seconds)),
        goto_options={"wait_until": "domcontentloaded"},
        ignore_http_error_status_codes=[code for code in range(400, 500) if code != 429],
        additional_http_error_status_codes=[429],
        **kwargs,
    )

    @crawler.pre_navigation_hook
    async def guard_navigation(context):
        if asyncio.get_running_loop().time() >= deadline_at:
            context.request.no_retry = True
            raise FetchError(504, "The upstream page timed out.")
        # Fail unsafe starting URLs directly, rather than retrying aborted navigation.
        try:
            await anyio.to_thread.run_sync(
                resolve_public_address, httpx.URL(context.request.url), abandon_on_cancel=True
            )
        except FetchError:
            context.request.no_retry = True
            raise

        async def bounded_route(route):
            try:
                async with asyncio.timeout_at(deadline_at):
                    await route_public_request(route)
            except (TimeoutError, PlaywrightError):
                # An exception in a route callback leaves Chromium's intercepted
                # request pending. Explicitly release it when the deadline expires.
                if asyncio.get_running_loop().time() >= deadline_at:
                    context.request.no_retry = True
                try:
                    await route.abort("failed")
                except PlaywrightError:
                    pass  # The context may already have closed at its deadline.

        await context.page.context.route("**/*", bounded_route)
        await context.page.context.route_web_socket("**/*", lambda socket: socket.close())

    return crawler
