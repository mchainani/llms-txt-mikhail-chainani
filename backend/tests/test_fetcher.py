"""Crawler behavior tested directly on the background worker's fetcher."""

import asyncio
import gzip
import socket
import subprocess
import sys
from contextlib import contextmanager
from html import escape

import anyio
import httpx
import pytest

from llms_txt_gen.crawling.fetcher import fetch_page
from llms_txt_gen.crawling.http_client import FetchError


@pytest.mark.parametrize("configured, expected", [(None, 600), ("120", 120), ("0.5", 0.5)])
def test_fetch_timeout_configuration(monkeypatch, configured, expected):
    if configured is None:
        monkeypatch.delenv("FETCH_TIMEOUT_SECONDS", raising=False)
    else:
        monkeypatch.setenv("FETCH_TIMEOUT_SECONDS", configured)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from llms_txt_gen.crawling.fetcher import FETCH_TIMEOUT_SECONDS; "
            "print(FETCH_TIMEOUT_SECONDS)",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert float(result.stdout) == expected


@contextmanager
def fetching(monkeypatch, handler):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", 443))
        ],
    )
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **kwargs: httpx.MockTransport(handler))

    def fetch(url, configs=None):
        return anyio.run(fetch_page, url, configs)

    yield fetch


def html_response(body, **kwargs):
    return httpx.Response(200, text=body, headers={"content-type": "text/html"}, **kwargs)


def result_urls(crawl):
    return [page.url for page in crawl.results]


def test_returns_requested_url_and_complete_page_results(monkeypatch):
    root_html = (
        '<title>Docs &amp; guides</title><meta name="description" content="Learn the API.">'
        '<a href="/start#install">Get <b>started</b></a>'
    )
    child_html = '<title>Start</title><meta property="og:description" content="Install it.">'
    with fetching(
        monkeypatch,
        lambda request: html_response(root_html if request.url.path == "/" else child_html),
    ) as fetch:
        response = fetch("HTTPS://EXAMPLE.COM:443/#top")

    assert response.model_dump() == {
        "requested_url": "https://example.com/",
        "results": [
            {
                "url": "https://example.com/",
                "title": "Docs & guides",
                "description": "Learn the API.",
            },
            {
                "url": "https://example.com/start",
                "title": "Start",
                "description": "Install it.",
            },
        ],
    }


def test_recursive_response_contains_only_page_metadata(monkeypatch):
    html = '<a href="/child">Child</a>'
    with fetching(monkeypatch, lambda request: html_response(html)) as fetch:
        response = fetch("https://example.com/")

    assert response.model_dump()["requested_url"] == "https://example.com/"
    assert result_urls(response) == ["https://example.com/", "https://example.com/child"]
    for page in response.model_dump()["results"]:
        assert page == {"url": page["url"], "title": None, "description": None}


def test_crawl_returns_successful_urls_only(monkeypatch):
    html = """
    <html><head><title> Docs &amp; guides </title>
    <meta name="description" content="Learn the API."></head><body>
    <a href="../start#install">Get <b>started</b></a>
    <a href="https://docs.example.com/start">Duplicate</a>
    <a href="?version=2">Version 2</a>
    <a href="//other.example.com/help">Elsewhere</a>
    <a href="mailto:team@example.com">Email</a>
    <a href="javascript:void(0)">Action</a>
    <a href="data:text/plain,hello">Data</a>
    <a href="#section">On this page</a><a href="">Empty</a><a>No href</a>
    </body></html>
    """
    with fetching(monkeypatch, lambda request: html_response(html)) as fetch:
        response = fetch("https://docs.example.com/guide/page")

    assert response.results[0].url == "https://docs.example.com/guide/page"
    assert sorted(result_urls(response)) == sorted(
        [
            "https://docs.example.com/guide/page",
            "https://docs.example.com/start",
            "https://docs.example.com/guide/page?version=2",
            "https://docs.example.com/start?version=2",
        ]
    )


def test_document_base_resolves_links_without_changing_domain_scope(monkeypatch):
    with fetching(
        monkeypatch,
        lambda request: html_response(
            '<base href="https://cdn.example.org/docs/"><a href="start">Start</a>'
            '<a href="https://example.com/keep">Keep</a>'
        ),
    ) as fetch:
        response = fetch("https://example.com/")

    assert result_urls(response) == ["https://example.com/", "https://example.com/keep"]


def test_follows_relative_redirect_and_resolves_against_final_url(monkeypatch):
    def upstream(request):
        if request.url.path == "/old":
            return httpx.Response(302, headers={"location": "/docs/"})
        return html_response('<a href="start">Start</a>')

    with fetching(monkeypatch, upstream) as fetch:
        response = fetch("https://example.com/old")

    assert result_urls(response) == ["https://example.com/docs/", "https://example.com/docs/start"]


def test_filters_links_by_requested_hostname_after_cross_host_redirect(monkeypatch):
    def upstream(request):
        if request.headers["host"] == "example.com":
            return httpx.Response(302, headers={"location": "https://docs.example.com/guide/"})
        return html_response(
            '<a href="child">Redirect host</a>'
            '<a href="https://example.com/return">Requested host</a>'
        )

    with fetching(monkeypatch, upstream) as fetch:
        response = fetch("https://example.com/start")

    assert result_urls(response) == ["https://docs.example.com/guide/"]


def test_exact_hostname_filter_preserves_different_schemes_and_ports(monkeypatch):
    html = """
        <a href="/docs">Docs</a>
        <a href="http://EXAMPLE.COM:8080/help">Help</a>
        <a href="https://www.example.com/">WWW</a>
        <a href="https://sub.example.com/">Subdomain</a>
        <a href="https://example.com.evil.test/">Lookalike</a>
        <a href="//elsewhere.test/docs">Other host</a>
    """
    with fetching(
        monkeypatch,
        lambda request: html_response(html if request.url.path == "/" else ""),
    ) as fetch:
        response = fetch("https://example.com/")

    assert response.results[0].url == "https://example.com/"
    assert sorted(result_urls(response)) == sorted(
        [
            "https://example.com/",
            "https://example.com/docs",
            "http://example.com:8080/help",
        ]
    )


@pytest.mark.parametrize(
    ("submitted", "expected", "expected_query"),
    [
        (
            "https://example.com/docs?z=2&a=1#install",
            "https://example.com/docs?a=1&z=2",
            "a=1&z=2",
        ),
        (
            "https://example.com/?q=hello%20world&name=%7e&plus=%2b",
            "https://example.com/?name=~&plus=%2B&q=hello+world",
            "name=~&plus=%2B&q=hello+world",
        ),
        (
            "https://example.com/?tag=b&z=1&tag=a&empty=&flag",
            "https://example.com/?empty=&flag=&tag=b&tag=a&z=1",
            "empty=&flag=&tag=b&tag=a&z=1",
        ),
        ("HTTPS://EXAMPLE.COM:443#top", "https://example.com/", ""),
        ("https://example.com/docs?#top", "https://example.com/docs", ""),
        (
            "https://example.com/?%61=one%26two%3Dthree&b=%E2%9C%93",
            "https://example.com/?a=one%26two%3Dthree&b=%E2%9C%93",
            "a=one%26two%3Dthree&b=%E2%9C%93",
        ),
        (
            "https://example.com/Case/Path/?Version=2&version=1",
            "https://example.com/Case/Path/?Version=2&version=1",
            "Version=2&version=1",
        ),
    ],
)
def test_normalizes_requested_url_before_fetching(monkeypatch, submitted, expected, expected_query):
    queries = []

    def upstream(request):
        queries.append(request.url.query.decode("ascii"))
        return html_response("")

    with fetching(monkeypatch, upstream) as fetch:
        response = fetch(submitted)

    assert result_urls(response) == [expected]
    assert response.model_dump()["requested_url"] == expected
    assert queries == [expected_query]


def test_deduplicates_links_with_equivalent_query_strings(monkeypatch):
    html = """
        <a href="/search?b=2&amp;q=hello%20world&amp;a=1#first">First</a>
        <a href="/search?a=1&amp;b=2&amp;q=hello+world#second">Equivalent</a>
        <a href="/search?tag=a&amp;tag=b">Tags AB</a>
        <a href="/search?tag=b&amp;tag=a">Tags BA</a>
    """
    with fetching(monkeypatch, lambda request: html_response(html)) as fetch:
        response = fetch("https://example.com/")

    assert response.results[0].url == "https://example.com/"
    assert sorted(result_urls(response)) == sorted(
        [
            "https://example.com/",
            "https://example.com/search?a=1&b=2&q=hello+world",
            "https://example.com/search?tag=a&tag=b",
            "https://example.com/search?tag=b&tag=a",
        ]
    )


def test_normalizes_redirect_url_before_fetching(monkeypatch):
    def upstream(request):
        if request.url.path == "/old":
            return httpx.Response(302, headers={"location": "/docs?z=2&a=1#section"})
        return html_response(f"<title>{escape(request.url.query.decode('ascii'))}</title>")

    with fetching(monkeypatch, upstream) as fetch:
        response = fetch("https://example.com/old#top")

    assert result_urls(response) == ["https://example.com/docs?a=1&z=2"]


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://169.254.169.254/",
        "https://user:pass@example.com",
    ],
)
def test_rejects_private_addresses_and_credentials(monkeypatch, url):
    with fetching(monkeypatch, lambda request: html_response("")) as fetch:
        with pytest.raises(FetchError) as error:
            fetch(url)
    assert error.value.status_code == 400


def test_rejects_private_redirect_destination(monkeypatch):
    with fetching(
        monkeypatch,
        lambda request: httpx.Response(302, headers={"location": "http://127.0.0.1/secret"}),
    ) as fetch:
        with pytest.raises(FetchError) as error:
            fetch("https://example.com/")
    assert error.value.status_code == 400


@pytest.mark.parametrize(
    ("upstream_response", "expected"),
    [
        (httpx.Response(503), 502),
        (httpx.Response(302, headers={"location": "/loop"}), 502),
        (httpx.Response(302), 502),
    ],
)
def test_returns_useful_fetch_errors(monkeypatch, upstream_response, expected):
    with fetching(monkeypatch, lambda request: upstream_response) as fetch:
        with pytest.raises(FetchError) as error:
            fetch("https://example.com/")
    assert error.value.status_code == expected
    assert error.value.detail


@pytest.mark.parametrize(
    ("error_type", "expected"), [(httpx.ReadTimeout, 504), (httpx.ConnectError, 502)]
)
def test_handles_network_failures(monkeypatch, error_type, expected):
    def upstream(request):
        raise error_type("upstream failed", request=request)

    with fetching(monkeypatch, upstream) as fetch:
        with pytest.raises(FetchError) as error:
            fetch("https://example.com/")
    assert error.value.status_code == expected


@pytest.mark.parametrize("status", [429, 503])
def test_retries_a_temporary_upstream_failure(monkeypatch, status):
    attempts = 0

    def upstream(request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(status)
        return html_response("<title>Recovered docs</title>")

    with fetching(monkeypatch, upstream) as fetch:
        response = fetch("https://example.com/")

    assert result_urls(response) == ["https://example.com/"]
    assert attempts == 2


def test_repeated_calls_to_the_same_url_are_independent(monkeypatch):
    calls = 0

    def upstream(request):
        nonlocal calls
        calls += 1
        return html_response(f"<title>Version {calls}</title>")

    with fetching(monkeypatch, upstream) as fetch:
        first = fetch("https://example.com/")
        second = fetch("https://example.com/")

    assert result_urls(first) == result_urls(second) == ["https://example.com/"]
    assert first.model_dump()["results"][0]["title"] == "Version 1"
    assert second.model_dump()["results"][0]["title"] == "Version 2"
    assert calls == 2


def test_accepts_html_at_two_mib_limit(monkeypatch):
    html = "<title>Large page</title>".ljust(2 * 1024 * 1024)
    with fetching(monkeypatch, lambda request: html_response(html)) as fetch:
        response = fetch("https://example.com/")
    assert response.results[0].title == "Large page"


@pytest.mark.parametrize(
    "upstream_response",
    [
        httpx.Response(403, text='<title>Blocked</title><a href="/challenge">Challenge</a>'),
        httpx.Response(404),
        httpx.Response(429),
    ],
)
def test_retains_unreadable_starting_page_with_null_metadata(monkeypatch, upstream_response):
    calls = []

    def upstream(request):
        calls.append(request.url.path)
        return upstream_response

    with fetching(monkeypatch, upstream) as fetch:
        response = fetch("https://example.com/")
    assert response.results[0].model_dump() == {
        "url": "https://example.com/", "title": None, "description": None
    }
    assert calls == ["/"] * (3 if upstream_response.status_code == 429 else 1)


def test_retains_final_redirect_url_for_unreadable_page(monkeypatch):
    def upstream(request):
        if request.url.path == "/":
            return httpx.Response(302, headers={"location": "/blocked"})
        return httpx.Response(403)

    with fetching(monkeypatch, upstream) as fetch:
        response = fetch("https://example.com/")
    assert response.results[0].model_dump() == {
        "url": "https://example.com/blocked", "title": None, "description": None
    }


@pytest.mark.parametrize("media_type", [None, "application/pdf", "text/plain"])
@pytest.mark.parametrize("redirect", [False, True])
def test_non_html_starting_page_is_skipped_without_errors(
    monkeypatch, caplog, media_type, redirect
):
    def upstream(request):
        if redirect and request.url.path == "/":
            return httpx.Response(302, headers={"location": "/download"})
        return httpx.Response(
            200,
            content=b'<title>Not HTML</title><a href="/ignored">Ignore</a>',
            headers={"content-type": media_type} if media_type else {},
        )

    with fetching(monkeypatch, upstream) as fetch:
        response = fetch("https://example.com/")

    assert response.model_dump() == {"requested_url": "https://example.com/", "results": []}
    assert not any(record.levelname == "ERROR" for record in caplog.records)


@pytest.mark.parametrize("redirect", [False, True])
@pytest.mark.parametrize("compressed", [False, True])
def test_oversized_starting_page_is_skipped_without_errors(
    monkeypatch, caplog, redirect, compressed
):
    html = '<title>Too large</title><a href="/ignored">Ignore</a>'.ljust(2 * 1024 * 1024 + 1)

    def upstream(request):
        if redirect and request.url.path == "/":
            return httpx.Response(302, headers={"location": "/large"})
        if compressed:
            return httpx.Response(
                200, content=gzip.compress(html.encode()),
                headers={"content-type": "text/html", "content-encoding": "gzip"},
            )
        return html_response(html)

    with fetching(monkeypatch, upstream) as fetch:
        response = fetch("https://example.com/")

    assert response.model_dump() == {"requested_url": "https://example.com/", "results": []}
    assert not any(record.levelname == "ERROR" for record in caplog.records)


def test_preserves_declared_html_encoding(monkeypatch):
    with fetching(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            content=b'<a href="/caf\xe9">Caf\xe9</a>',
            headers={"content-type": "text/html; charset=iso-8859-1"},
        ),
    ) as fetch:
        response = fetch("https://example.com/")

    assert result_urls(response) == ["https://example.com/", "https://example.com/caf%C3%A9"]


def test_rejects_mixed_public_private_dns_answers(monkeypatch):
    with fetching(monkeypatch, lambda request: html_response("")) as fetch:
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda *args, **kwargs: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", 443)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443)),
            ],
        )
        with pytest.raises(FetchError) as error:
            fetch("https://example.com/")
    assert error.value.status_code == 400


def test_connects_to_validated_ip_preserving_virtual_host_and_tls_sni(monkeypatch):
    def upstream(request):
        if (
            request.url.host != "93.184.215.14"
            or request.headers["host"] != "example.com:8443"
            or request.extensions.get("sni_hostname") != "example.com"
        ):
            return httpx.Response(421)
        return html_response("<title>Correct virtual host</title>")

    with fetching(monkeypatch, upstream) as fetch:
        response = fetch("https://example.com:8443/")

    assert result_urls(response) == ["https://example.com:8443/"]


def test_handles_gzip_html_without_decoding_twice(monkeypatch):
    with fetching(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            content=gzip.compress(b'<a href="/child">Child</a>'),
            headers={"content-type": "text/html", "content-encoding": "gzip"},
        ),
    ) as fetch:
        response = fetch("https://example.com/")

    assert result_urls(response) == ["https://example.com/", "https://example.com/child"]


def test_recovers_from_malformed_base_url(monkeypatch):
    with fetching(
        monkeypatch,
        lambda request: html_response('<base href="https://[invalid"><a href="start">Start</a>'),
    ) as fetch:
        response = fetch("https://example.com/docs/")

    assert result_urls(response) == ["https://example.com/docs/", "https://example.com/docs/start"]


@pytest.mark.parametrize(
    "configs, default",
    [
        (None, 0.01),
        ("{}", 0.01),
        ('{"fetch_timeout_seconds": null}', 0.01),
        ('{"fetch_timeout_seconds": 0.01}', 60),
        ('{"fetch_timeout_seconds": 1e-10}', 60),
    ],
)
def test_total_deadline_covers_a_slow_response(monkeypatch, configs, default):
    from llms_txt_gen.crawling import fetcher

    async def upstream(request):
        await anyio.sleep(1)
        return html_response("")

    monkeypatch.setattr(fetcher, "FETCH_TIMEOUT_SECONDS", default)
    with fetching(monkeypatch, upstream) as fetch:
        with pytest.raises(FetchError) as error:
            fetch("https://example.com/", configs)
    assert error.value.status_code == 504


@pytest.mark.parametrize(
    "configs", ['{"fetch_timeout_seconds": 2}', '{"fetch_timeout_seconds": 43199999999999}']
)
def test_request_timeout_can_extend_default_deadline(monkeypatch, configs):
    from llms_txt_gen.crawling import fetcher

    async def upstream(request):
        await anyio.sleep(0.05)
        return html_response("<title>Slow page</title>")

    monkeypatch.setattr(fetcher, "FETCH_TIMEOUT_SECONDS", 0.01)
    with fetching(monkeypatch, upstream) as fetch:
        response = fetch("https://example.com/", configs)
    assert response.results[0].title == "Slow page"


def test_handles_dns_failure(monkeypatch):
    def fail_dns(*args, **kwargs):
        raise socket.gaierror("No such hostname")

    with fetching(monkeypatch, lambda request: html_response("")) as fetch:
        monkeypatch.setattr(socket, "getaddrinfo", fail_dns)
        with pytest.raises(FetchError) as error:
            fetch("https://missing.example/")
    assert error.value.status_code == 502


def test_does_not_forward_upstream_cookies_to_another_host_on_the_same_ip(monkeypatch):
    def upstream(request):
        if request.headers["host"] == "first.example.com":
            return httpx.Response(
                302,
                headers={
                    "location": "https://second.example.com/",
                    "set-cookie": "session=private; Path=/; Secure; HttpOnly",
                },
            )
        if request.headers.get("cookie"):
            return httpx.Response(421)
        return html_response("")

    with fetching(monkeypatch, upstream) as fetch:
        response = fetch("https://first.example.com/")

    assert result_urls(response) == ["https://second.example.com/"]


def test_concurrent_crawls_do_not_share_results(monkeypatch):
    async def upstream(request):
        return html_response(f'<title>{request.url.path}</title><a href="child">Child</a>')

    async def exercise():
        responses = await asyncio.gather(
            fetch_page("https://example.com/alpha"),
            fetch_page("https://example.com/beta"),
        )
        assert [result_urls(response) for response in responses] == [
            ["https://example.com/alpha", "https://example.com/child"],
            ["https://example.com/beta", "https://example.com/child"],
        ]

    with fetching(monkeypatch, upstream):
        anyio.run(exercise)


def test_shared_ip_hosts_each_establish_their_own_tls_hostname(monkeypatch):
    # Keep HTTPX/httpcore pooling real; replace only the network socket boundary.
    from httpcore import AsyncNetworkStream
    from httpcore._backends.auto import AutoBackend

    tls_hostnames = []
    body = b"<title>Docs</title>"
    wire_response = (
        b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: "
        + str(len(body)).encode()
        + b"\r\n\r\n"
        + body
    )

    class Stream(AsyncNetworkStream):
        async def read(self, max_bytes, timeout=None):  # noqa: ASYNC109 -- httpcore interface
            return wire_response

        async def write(self, buffer, timeout=None):  # noqa: ASYNC109 -- httpcore interface
            pass

        async def aclose(self):
            pass

        async def start_tls(
            self,
            ssl_context,
            server_hostname=None,
            timeout=None,  # noqa: ASYNC109
        ):
            tls_hostnames.append(server_hostname)
            return self

        def get_extra_info(self, info):
            return None

    async def connect_tcp(self, host, port, **kwargs):
        return Stream()

    monkeypatch.setattr(AutoBackend, "connect_tcp", connect_tcp)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", 443))
        ],
    )

    async def exercise():
        for url in ["https://first.example.com/", "https://second.example.com/"]:
            crawl = await fetch_page(url)
            assert crawl.requested_url == url

    anyio.run(exercise)
    assert tls_hostnames == ["first.example.com", "second.example.com"]
