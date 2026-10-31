from collections import Counter

import httpx
from test_fetcher import fetching, html_response, result_urls


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
    assert result_urls(response) == [
        "https://example.com/",
        "https://example.com/a",
        "https://example.com/b?a=1&z=2",
        "https://example.com/leaf",
    ]
    assert Counter(visits) == {"/": 1, "/a": 1, "/b?a=1&z=2": 1, "/leaf": 1}


def test_failed_linked_pages_do_not_discard_successful_results(monkeypatch):

    def upstream(request):
        if request.url.path == "/":
            return html_response('<a href="/missing">Missing</a><a href="/ok">OK</a>')
        if request.url.path == "/missing":
            return httpx.Response(404)
        return html_response("<title>OK</title>")

    with fetching(monkeypatch, upstream) as fetch:
        response = fetch("https://example.com/")
    assert result_urls(response) == ["https://example.com/", "https://example.com/ok"]


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
