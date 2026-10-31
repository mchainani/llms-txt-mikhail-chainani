"""Browser integration tests run against a separate, disposable Chromium sidecar."""

import asyncio
import json
import os

import httpx
import pytest
from pydantic import ValidationError

from llms_txt_gen.crawling.fetcher import fetch_page
from llms_txt_gen.crawling.http_client import FetchError
from llms_txt_gen.routers.models import FetchPageRequest


def test_request_accepts_playwright_options():
    payload = FetchPageRequest(
        url="https://example.com/", crawler="playwright", render_wait_seconds=0.1
    )
    assert payload.model_dump(exclude_none=True)["crawler"] == "playwright"
    assert payload.render_wait_seconds == 0.1


@pytest.mark.parametrize("options", [{"crawler": "chrome"}, {"render_wait_seconds": -1}])
def test_request_rejects_invalid_browser_options(options):
    with pytest.raises(ValidationError):
        FetchPageRequest(url="https://example.com/", **options)


def test_playwright_requires_sidecar(monkeypatch):
    monkeypatch.delenv("PLAYWRIGHT_CDP_URL", raising=False)
    with pytest.raises(FetchError, match="PLAYWRIGHT_CDP_URL"):
        asyncio.run(fetch_page("https://example.com/", '{"crawler":"playwright"}'))


@pytest.fixture
def sidecar(monkeypatch):
    endpoint = os.environ.get("PLAYWRIGHT_TEST_CDP_URL")
    if not endpoint:
        pytest.skip("Set PLAYWRIGHT_TEST_CDP_URL to run browser integration tests")
    monkeypatch.setenv("PLAYWRIGHT_CDP_URL", endpoint)
    return endpoint


def test_browser_executes_js_and_follows_rendered_links(sidecar, monkeypatch):
    from llms_txt_gen.crawling import browser

    async def fixture_route(route):
        path = httpx.URL(route.request.url).path
        if path == "/":
            html = """<title>Shell</title><div id="root"></div><script>
                setTimeout(() => {
                    document.title = 'Rendered root';
                    document.querySelector('#root').innerHTML =
                        '<a href="/child#fragment">Child</a><a href="/child">Duplicate</a>' +
                        '<a href="https://other.example/">External</a>';
                }, 50);
            </script>"""
        else:
            html = '<title>Child</title><meta name="description" content="Rendered child">'
        await route.fulfill(status=200, content_type="text/html", body=html)

    monkeypatch.setattr(browser, "route_public_request", fixture_route)
    options = {"crawler": "playwright", "render_wait_seconds": 0.15, "max_pages": 5}
    result = asyncio.run(fetch_page("https://example.com/", json.dumps(options)))
    assert [(page.url, page.title) for page in result.results] == [
        ("https://example.com/", "Rendered root"),
        ("https://example.com/child", "Child"),
    ]
    assert result.results[1].description == "Rendered child"
    # A second crawl must get fresh storage and leave the sidecar running.
    options["max_pages"] = 1
    again = asyncio.run(fetch_page("https://example.com/", json.dumps(options)))
    assert len(again.results) == 1
    assert again.results[0].title == "Rendered root"


def test_browser_retains_missing_pages_without_metadata(sidecar, monkeypatch):
    from llms_txt_gen.crawling import browser

    async def missing(route):
        await route.fulfill(status=404, content_type="text/html", body="<title>Not found</title>")

    monkeypatch.setattr(browser, "route_public_request", missing)
    result = asyncio.run(fetch_page("https://example.com/", '{"crawler":"playwright"}'))
    assert [page.model_dump() for page in result.results] == [
        {"url": "https://example.com/", "title": None, "description": None}
    ]


def test_browser_rejects_private_starting_address(sidecar):
    with pytest.raises(FetchError) as error:
        asyncio.run(fetch_page("http://127.0.0.1:12345/", '{"crawler":"playwright"}'))
    assert error.value.status_code == 400


def test_browser_deadline_during_render_wait(sidecar, monkeypatch):
    from llms_txt_gen.crawling import browser

    async def html_page(route):
        await route.fulfill(status=200, content_type="text/html", body="<title>Slow render</title>")

    monkeypatch.setattr(browser, "route_public_request", html_page)
    with pytest.raises(FetchError) as error:
        asyncio.run(
            fetch_page(
                "https://example.com/",
                '{"crawler":"playwright","fetch_timeout_seconds":1,"render_wait_seconds":5}',
            )
        )
    assert error.value.status_code == 504


def test_browser_guard_blocks_redirect_response():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from llms_txt_gen.crawling.browser import route_public_request

    response = SimpleNamespace(status=302, dispose=AsyncMock())
    route = SimpleNamespace(
        request=SimpleNamespace(
            url="https://93.184.216.34/", resource_type="document", redirected_from=None
        ),
        fetch=AsyncMock(return_value=response),
        abort=AsyncMock(),
        fulfill=AsyncMock(),
        continue_=AsyncMock(),
    )
    asyncio.run(route_public_request(route))
    route.abort.assert_awaited_once()
    route.continue_.assert_not_awaited()
    route.fulfill.assert_not_awaited()
    response.dispose.assert_awaited_once()


def test_browser_deadline_bounds_late_navigation(sidecar, monkeypatch):
    from time import perf_counter

    from llms_txt_gen.crawling import browser

    async def slow_child(route):
        if httpx.URL(route.request.url).path == "/child":
            await asyncio.sleep(10)
        else:
            await route.fulfill(
                status=200, content_type="text/html", body='<a href="/child">Child</a>'
            )

    monkeypatch.setattr(browser, "route_public_request", slow_child)
    started = perf_counter()
    with pytest.raises(FetchError) as error:
        asyncio.run(
            fetch_page(
                "https://example.com/",
                '{"crawler":"playwright","fetch_timeout_seconds":2,"render_wait_seconds":1.2}',
            )
        )
    assert error.value.status_code == 504
    assert perf_counter() - started < 2.8


def test_concurrent_crawls_close_only_their_own_contexts(sidecar, monkeypatch):
    from llms_txt_gen.crawling import browser

    async def html_page(route):
        name = httpx.URL(route.request.url).path.strip("/")
        await route.fulfill(status=200, content_type="text/html", body=f"<title>{name}</title>")

    monkeypatch.setattr(browser, "route_public_request", html_page)

    async def exercise():
        return await asyncio.gather(
            fetch_page(
                "https://example.com/fast",
                '{"crawler":"playwright","max_pages":1,"render_wait_seconds":0.05}',
            ),
            fetch_page(
                "https://example.com/slow",
                '{"crawler":"playwright","max_pages":1,"render_wait_seconds":1}',
            ),
        )

    fast, slow = asyncio.run(exercise())
    assert [page.title for page in fast.results] == ["fast"]
    assert [page.title for page in slow.results] == ["slow"]
