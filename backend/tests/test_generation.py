"""Background generation uses the real crawler and renderer."""

import anyio
import httpx
import pytest
from test_fetcher import fetching, html_response

from llms_txt_gen.crawling.fetcher import fetch_page
from llms_txt_gen.crawling.http_client import FetchError
from llms_txt_gen.generation.generator import build_document, render_document


def test_crawls_and_generates_llms_txt(monkeypatch):
    def upstream(request):
        if request.url.path == "/":
            return html_response(
                '<title>Café</title><meta name="description" content="Start here.">'
                '<a href="/guide">Read guide</a>'
            )
        assert request.url.path == "/guide"
        return html_response('<meta name="description" content="Learn more.">')

    with fetching(monkeypatch, upstream):
        crawl_results = anyio.run(fetch_page, "HTTPS://EXAMPLE.COM:443/#top")
        document = build_document(crawl_results)
        markdown = render_document(document)

    assert len(crawl_results.results) == 2
    assert markdown == (
        "# Café\n\n> Start here.\n\n## Pages\n\n"
        "- [Café](https://example.com/): Start here.\n"
        "\n## Optional\n\n- [/guide](https://example.com/guide): Learn more.\n"
    )


def test_generation_preserves_fetch_errors(monkeypatch):
    with fetching(monkeypatch, lambda request: httpx.Response(503)):
        with pytest.raises(FetchError) as error:
            anyio.run(fetch_page, "https://example.com/")
    assert error.value.status_code == 502


def test_client_error_links_appear_in_optional_section(monkeypatch):
    def upstream(request):
        if request.url.path == "/":
            return html_response(
                '<title>Docs</title><meta name="description" content="Start here.">'
                '<a href="/blocked">Blocked</a><a href="/guide">Guide</a>'
            )
        if request.url.path == "/blocked":
            return httpx.Response(403, text="<title>Challenge</title>")
        return html_response('<title>Guide</title><meta name="description" content="Read me.">')

    with fetching(monkeypatch, upstream):
        markdown = render_document(build_document(anyio.run(fetch_page, "https://example.com/")))

    assert "- [Guide](https://example.com/guide): Read me." in markdown
    assert markdown.endswith("## Optional\n\n- [/blocked](https://example.com/blocked)\n")
    assert "Challenge" not in markdown
