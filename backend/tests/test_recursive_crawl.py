import asyncio
import json
from collections import Counter

import httpx
import pytest
from test_fetcher import fetching, html_response, result_urls

from llms_txt_gen.crawling import fetcher
from llms_txt_gen.crawling.http_client import FetchError


@pytest.mark.parametrize(
    "depth, expected_paths",
    [
        (None, ["/", "/1", "/2"]),
        (0.5, ["/"]),
        (1, ["/"]),
        (1.5, ["/", "/1"]),
        (2, ["/", "/1"]),
        (3, ["/", "/1", "/2"]),
        (4, ["/", "/1", "/2", "/3"]),
    ],
)
def test_depth_excludes_pages_at_or_beyond_limit(monkeypatch, depth, expected_paths):
    visits = []

    def upstream(request):
        path = request.url.path
        visits.append(path)
        level = 0 if path == "/" else int(path[1:])
        return html_response(f'<a href="/{level + 1}">Next</a><a href="/">Root</a>')

    configs = None if depth is None else json.dumps({"max_depth": depth})
    with fetching(monkeypatch, upstream) as fetch:
        response = fetch("https://example.com/", configs)

    assert visits == expected_paths
    assert result_urls(response) == [f"https://example.com{path}" for path in expected_paths]


def test_bfs_waits_for_slow_parent_and_uses_shortest_path(monkeypatch):
    monkeypatch.setattr(fetcher, "CRAWL_CONCURRENCY", 2)
    visits = Counter()
    started_before_parent_finished = []

    async def exercise():
        deeper_started = asyncio.Event()
        slow_parent_finished = False

        async def upstream(request):
            nonlocal slow_parent_finished
            path = request.url.path
            visits[path] += 1
            if path == "/b":
                try:
                    await asyncio.wait_for(deeper_started.wait(), 0.1)
                except TimeoutError:
                    pass
                slow_parent_finished = True
            if path in {"/c", "/x", "/y"}:
                deeper_started.set()
                if not slow_parent_finished:
                    started_before_parent_finished.append(path)
            pages = {
                "/": '<a href="/a">A</a><a href="/b">B</a>',
                "/a": '<a href="/c">C</a>',
                "/b": '<a href="/x">X</a>',
                "/c": '<a href="/x">X</a>',
                "/x": '<a href="/y">Y</a>',
                "/y": '<a href="/">Root</a>',
            }
            return html_response(pages[path])

        with fetching(monkeypatch, upstream):
            return await fetcher.fetch_page("https://example.com/", '{"max_depth": 4}')

    response = asyncio.run(exercise())
    assert started_before_parent_finished == []
    assert visits == Counter({"/": 1, "/a": 1, "/b": 1, "/c": 1, "/x": 1, "/y": 1})
    assert set(result_urls(response)) == {
        "https://example.com/",
        "https://example.com/a",
        "https://example.com/b",
        "https://example.com/c",
        "https://example.com/x",
        "https://example.com/y",
    }


def test_page_limit_is_shared_between_levels(monkeypatch):
    visits = []

    def upstream(request):
        path = request.url.path
        visits.append(path)
        level = 0 if path == "/" else int(path[1:])
        return html_response(f'<a href="/{level + 1}">Next</a>')

    with fetching(monkeypatch, upstream) as fetch:
        response = fetch("https://example.com/", '{"max_depth": 5, "max_pages": 3}')

    assert visits == ["/", "/1", "/2"]
    assert len(response.results) == 3


@pytest.mark.parametrize("terminal_failure", [False, True])
def test_next_level_waits_for_parent_retries(monkeypatch, terminal_failure):
    monkeypatch.setattr(fetcher, "CRAWL_CONCURRENCY", 2)
    attempts = 0
    attempts_when_child_started = []

    async def exercise():
        child_started = asyncio.Event()

        async def upstream(request):
            nonlocal attempts
            path = request.url.path
            if path == "/":
                return html_response('<a href="/a">A</a><a href="/retry">Retry</a>')
            if path == "/a":
                return html_response('<a href="/child">Child</a>')
            if path == "/retry":
                attempts += 1
                if attempts == 1:
                    try:
                        await asyncio.wait_for(child_started.wait(), 0.1)
                    except TimeoutError:
                        pass
                if terminal_failure or attempts == 1:
                    return httpx.Response(503)
                return html_response("<title>Recovered</title>")
            attempts_when_child_started.append(attempts)
            child_started.set()
            return html_response("<title>Child</title>")

        with fetching(monkeypatch, upstream):
            return await fetcher.fetch_page("https://example.com/")

    response = asyncio.run(exercise())
    assert attempts_when_child_started == [3 if terminal_failure else 2]
    assert "https://example.com/child" in result_urls(response)


def test_deadline_is_shared_between_levels(monkeypatch):
    visits = []

    async def upstream(request):
        path = request.url.path
        visits.append(path)
        await asyncio.sleep(0.2)
        level = 0 if path == "/" else int(path[1:])
        return html_response(f'<a href="/{level + 1}">Next</a>')

    with fetching(monkeypatch, upstream) as fetch:
        with pytest.raises(FetchError) as error:
            fetch("https://example.com/", '{"fetch_timeout_seconds": 0.5}')

    assert error.value.status_code == 504
    assert visits == ["/", "/1", "/2"]


@pytest.mark.parametrize(
    "configs, expected",
    [
        (None, 50),
        ('{"max_pages": null}', 50),
        ('{"max_pages": 1}', 1),
        ('{"max_pages": 3}', 3),
        ('{"max_pages": 300}', 271),
    ],
)
def test_result_limit_stops_crawling_and_preserves_collected_pages(monkeypatch, configs, expected):
    visits = []

    async def upstream(request):
        visits.append(request.url.path)
        if request.url.path == "/":
            return html_response(
                "<title>Root</title>" + "".join(f'<a href="/page-{i}">Page</a>' for i in range(270))
            )
        await asyncio.sleep(0.001)
        return html_response('<title>Child</title><a href="/">Root</a>')

    with fetching(monkeypatch, upstream) as fetch:
        response = fetch("https://example.com/", configs)

    assert len(response.results) == expected
    assert response.results[0].title == "Root"
    assert len(set(result_urls(response))) == expected
    if expected < 271:
        assert len(visits) < 271
    if expected == 1:
        assert visits == ["/"]
    from llms_txt_gen.generation.generator import build_document, render_document

    assert render_document(build_document(response)).startswith("# Root\n")


def test_concurrent_completions_cannot_exceed_result_limit(monkeypatch):
    monkeypatch.setattr(fetcher, "CRAWL_CONCURRENCY", 6)
    active = 0
    peak = 0
    visits = []

    async def exercise():
        saturated = asyncio.Event()

        async def upstream(request):
            nonlocal active, peak
            visits.append(request.url.path)
            if request.url.path == "/":
                return html_response("".join(f'<a href="/{i}">Page</a>' for i in range(40)))
            active += 1
            peak = max(peak, active)
            if active == 6:
                saturated.set()
            try:
                await asyncio.wait_for(saturated.wait(), 2)
                return html_response("<title>Child</title>")
            finally:
                active -= 1

        with fetching(monkeypatch, upstream):
            return await fetcher.fetch_page("https://example.com/", '{"max_pages": 3}')

    response = asyncio.run(exercise())
    assert len(response.results) == 3
    assert peak == 6
    assert active == 0
    assert len(visits) < 41


def test_limit_counts_retained_errors_but_not_skips_or_redirect_duplicates(monkeypatch):
    monkeypatch.setattr(fetcher, "CRAWL_CONCURRENCY", 1)
    visits = []

    def upstream(request):
        visits.append(request.url.path)
        if request.url.path == "/":
            return html_response(
                '<a href="/skip">Skip</a><a href="/alias">Alias</a>'
                '<a href="/page">Page</a><a href="/missing">Missing</a>'
                '<a href="/later">Later</a>'
            )
        if request.url.path == "/skip":
            return httpx.Response(200, headers={"content-type": "application/pdf"})
        if request.url.path == "/alias":
            return httpx.Response(302, headers={"location": "/page"})
        if request.url.path == "/missing":
            return httpx.Response(404)
        return html_response("<title>Page</title>")

    with fetching(monkeypatch, upstream) as fetch:
        response = fetch("https://example.com/", json.dumps({"max_pages": 3}))

    assert result_urls(response) == [
        "https://example.com/",
        "https://example.com/page",
        "https://example.com/missing",
    ]
    assert response.results[-1].title is None
    assert "/later" not in visits


def test_reached_limit_returns_results_when_in_flight_page_hits_deadline(monkeypatch):
    monkeypatch.setattr(fetcher, "CRAWL_CONCURRENCY", 2)
    active = 0

    async def exercise():
        slow_started = asyncio.Event()

        async def upstream(request):
            nonlocal active
            if request.url.path == "/":
                return html_response('<a href="/fast">Fast</a><a href="/slow">Slow</a>')
            active += 1
            try:
                if request.url.path == "/slow":
                    slow_started.set()
                    await asyncio.sleep(2)
                else:
                    await asyncio.wait_for(slow_started.wait(), 1)
                return html_response("<title>Child</title>")
            finally:
                active -= 1

        with fetching(monkeypatch, upstream):
            return await fetcher.fetch_page(
                "https://example.com/", '{"max_pages": 2, "fetch_timeout_seconds": 0.5}'
            )

    response = asyncio.run(exercise())
    assert result_urls(response) == ["https://example.com/", "https://example.com/fast"]
    assert active == 0


def test_pending_request_log_includes_current_result_count_at_limit(monkeypatch, caplog):
    monkeypatch.setattr(fetcher, "CRAWL_CONCURRENCY", 1)
    caplog.set_level("INFO", logger=fetcher.__name__)

    def upstream(request):
        return html_response('<a href="/child">Child</a>')

    with fetching(monkeypatch, upstream) as fetch:
        response = fetch("https://example.com/", '{"max_pages": 2}')

    assert len(response.results) == 2
    assert (
        "Fetched page https://example.com/ "
        "(status=200, current_page_count=1, pending_request_count="
    ) in caplog.text
    assert (
        "Fetched page https://example.com/child "
        "(status=200, current_page_count=2, pending_request_count="
    ) in caplog.text


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
def test_starting_page_precedes_children_after_redirects(monkeypatch, redirect):
    def upstream(request):
        if redirect and request.url.path == "/":
            return httpx.Response(302, headers={"location": "/landing"})
        return html_response(
            '<title>Root</title><a href="/child">Child</a>'
            if request.url.path != "/child"
            else "<title>Child</title>"
        )

    with fetching(monkeypatch, upstream) as fetch:
        response = fetch("https://example.com/")
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
        "https://example.com/",
        "https://example.com/missing",
        "https://example.com/ok",
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


def test_oversized_linked_page_is_skipped_while_crawling_continues(monkeypatch, caplog):
    def upstream(request):
        if request.url.path == "/":
            return html_response('<a href="/large">Large</a><a href="/ok">OK</a>')
        if request.url.path == "/large":
            return html_response(
                '<title>Too large</title><a href="/ignored">Ignore</a>'.ljust(2 * 1024 * 1024 + 1)
            )
        return html_response("<title>OK</title>")

    with fetching(monkeypatch, upstream) as fetch:
        response = fetch("https://example.com/")

    assert result_urls(response) == ["https://example.com/", "https://example.com/ok"]
    assert response.results[1].title == "OK"
    assert not any(record.levelname == "ERROR" for record in caplog.records)
