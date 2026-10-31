import asyncio
from collections import Counter

import httpx
import pytest
from test_fetcher import fetching, html_response, result_urls

from llms_txt_gen.crawling import fetcher
from llms_txt_gen.crawling.http_client import FetchError


@pytest.mark.parametrize("limit", [None, 3, 1])
def test_page_concurrency_is_bounded_and_shared_links_are_deduplicated(monkeypatch, limit):
    expected_peak = 25 if limit is None else limit
    if limit is not None:
        monkeypatch.setattr(fetcher, "CRAWL_CONCURRENCY", limit)
    active = 0
    peak = 0
    visits = Counter()

    async def exercise():
        saturated = asyncio.Event()

        async def upstream(request):
            nonlocal active, peak
            visits[request.url.path] += 1
            if request.url.path == "/":
                return html_response(
                    "".join(
                        f'<a href="/page-{i}">Page</a><a href="/page-{i}#duplicate">Again</a>'
                        for i in range(30)
                    )
                )
            active += 1
            peak = max(peak, active)
            if active == expected_peak:
                saturated.set()
            try:
                try:
                    await asyncio.wait_for(saturated.wait(), 0.2)
                except TimeoutError:
                    pass
                await asyncio.sleep(0.01)
                return html_response('<a href="/page-0">Shared</a><a href="/">Root</a>')
            finally:
                active -= 1

        with fetching(monkeypatch, upstream):
            return await fetcher.fetch_page("https://example.com/", '{"fetch_timeout_seconds": 3}')

    response = asyncio.run(exercise())
    assert peak == expected_peak
    assert active == 0
    assert len(response.results) == 31
    assert visits == Counter({"/": 1, **{f"/page-{i}": 1 for i in range(30)}})


@pytest.mark.parametrize("redirect", [False, True])
def test_starting_page_precedes_a_child_that_finishes_during_enqueue(monkeypatch, redirect):
    from crawlee import ConcurrencySettings
    from crawlee.storages import RequestQueue

    monkeypatch.setattr(
        fetcher,
        "ConcurrencySettings",
        lambda **kwargs: ConcurrencySettings(max_concurrency=2, desired_concurrency=2),
    )
    add_requests = RequestQueue.add_requests
    get_metadata = RequestQueue.get_metadata

    async def exercise():
        child_recorded = asyncio.Event()

        async def delayed_add(queue, requests, **kwargs):
            await add_requests(queue, requests, **kwargs)
            if any(
                getattr(request, "url", None) == "https://example.com/child" for request in requests
            ):
                await asyncio.wait_for(child_recorded.wait(), 2)

        async def metadata(queue):
            result = await get_metadata(queue)
            child_recorded.set()
            return result

        monkeypatch.setattr(RequestQueue, "add_requests", delayed_add)
        monkeypatch.setattr(RequestQueue, "get_metadata", metadata)

        def upstream(request):
            if redirect and request.url.path == "/":
                return httpx.Response(302, headers={"location": "/landing"})
            return html_response(
                '<title>Root</title><a href="/child">Child</a>'
                if request.url.path != "/child"
                else "<title>Child</title>"
            )

        with fetching(monkeypatch, upstream):
            return await fetcher.fetch_page("https://example.com/")

    response = asyncio.run(exercise())
    assert result_urls(response) == [
        "https://example.com/landing" if redirect else "https://example.com/",
        "https://example.com/child",
    ]
    from llms_txt_gen.generation.generator import build_document

    assert build_document(response).title == "Root"


def test_deadline_cancels_all_active_pages(monkeypatch):
    monkeypatch.setattr(fetcher, "CRAWL_CONCURRENCY", 3)
    active = 0
    started = []
    cancelled = []

    async def upstream(request):
        nonlocal active
        if request.url.path == "/":
            return html_response("".join(f'<a href="/{i}">Page</a>' for i in range(10)))
        active += 1
        started.append(request.url.path)
        try:
            await asyncio.sleep(1.5)
        except asyncio.CancelledError:
            cancelled.append(request.url.path)
            raise
        finally:
            active -= 1
        return html_response("")

    async def exercise():
        with fetching(monkeypatch, upstream):
            with pytest.raises(FetchError) as error:
                await fetcher.fetch_page("https://example.com/", '{"fetch_timeout_seconds": 1}')
            assert error.value.status_code == 504
            assert active == 0
            assert len(started) == 3
            assert sorted(cancelled) == sorted(started)
            await asyncio.sleep(0.05)
            assert len(started) == 3

    asyncio.run(exercise())


def test_recurses_through_cycles_and_deduplicates_normalized_urls(monkeypatch):
    visits = []

    def upstream(request):
        path = request.url.raw_path.decode()
        visits.append(path)
        pages = {
            "/": (
                '<a href="/a#one">A</a><a href="/a#two">A again</a>'
                '<a href="/b?z=2&amp;a=1">B</a><a href="/b?a=1&amp;z=2">B again</a>'
                '<a href="https://other.example.com/">External</a>'
            ),
            "/a": '<a href="/">Root</a><a href="/leaf">Leaf</a>',
            "/b?a=1&z=2": '<a href="/a">A</a><a href="/leaf">Leaf</a>',
            "/leaf": '<a href="/a">Cycle</a>',
        }
        if path not in pages:
            return httpx.Response(404)
        return html_response(pages[path])

    with fetching(monkeypatch, upstream) as fetch:
        response = fetch("https://example.com/#top")
    assert response.results[0].url == "https://example.com/"
    assert sorted(result_urls(response)) == sorted(
        [
            "https://example.com/",
            "https://example.com/a",
            "https://example.com/b?a=1&z=2",
            "https://example.com/leaf",
        ]
    )
    assert Counter(visits) == {"/": 1, "/a": 1, "/b?a=1&z=2": 1, "/leaf": 1}


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 415, 429, 499])
def test_client_errors_are_retained_while_other_pages_are_crawled(monkeypatch, status):

    def upstream(request):
        if request.url.path == "/":
            return html_response('<a href="/missing">Missing</a><a href="/ok">OK</a>')
        if request.url.path == "/missing":
            return httpx.Response(status)
        return html_response("<title>OK</title>")

    with fetching(monkeypatch, upstream) as fetch:
        response = fetch("https://example.com/")
    pages = {page.url: page for page in response.results}
    assert set(pages) == {
        "https://example.com/", "https://example.com/missing", "https://example.com/ok"
    }
    assert pages["https://example.com/missing"].title is None
    assert pages["https://example.com/missing"].description is None
    assert pages["https://example.com/ok"].title == "OK"


def test_redirect_aliases_produce_unique_final_urls(monkeypatch):

    def upstream(request):
        if request.url.path == "/":
            return html_response('<a href="/old">Old</a><a href="/new">New</a>')
        if request.url.path == "/old":
            return httpx.Response(301, headers={"location": "/new"})
        return html_response('<a href="/">Root</a>')

    with fetching(monkeypatch, upstream) as fetch:
        response = fetch("https://example.com/")
    assert result_urls(response) == ["https://example.com/", "https://example.com/new"]


def test_non_html_linked_page_is_skipped_while_crawling_continues(monkeypatch, caplog):
    def upstream(request):
        if request.url.path == "/":
            return html_response('<a href="/download">Download</a><a href="/ok">OK</a>')
        if request.url.path == "/download":
            return httpx.Response(200, headers={"content-type": "application/pdf"})
        return html_response("<title>OK</title>")

    with fetching(monkeypatch, upstream) as fetch:
        response = fetch("https://example.com/")

    assert result_urls(response) == ["https://example.com/", "https://example.com/ok"]
    assert response.results[1].title == "OK"
    assert not any(record.levelname == "ERROR" for record in caplog.records)
