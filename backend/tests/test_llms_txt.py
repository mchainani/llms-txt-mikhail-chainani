import pytest

from llms_txt_gen.crawling.models import CrawlResponse, PageResult
from llms_txt_gen.generation.generator import build_document, render_document
from llms_txt_gen.generation.models import LlmsTxtDocument, LlmsTxtEntry, LlmsTxtSection


def test_renders_sections_in_spec_order_and_optional_last():
    document = LlmsTxtDocument(
        title="Acme",
        summary="Tools for developers.",
        details="Start with the guide.",
        sections=[
            LlmsTxtSection(
                name="Optional",
                entries=[LlmsTxtEntry(title="News", url="https://example.com/news")],
            ),
            LlmsTxtSection(name="Empty"),
            LlmsTxtSection(
                name="Documentation",
                entries=[
                    LlmsTxtEntry(
                        title="Guide", url="https://example.com/guide", description="Begin here"
                    )
                ],
            ),
        ],
    )
    assert render_document(document) == (
        "# Acme\n\n> Tools for developers.\n\nStart with the guide.\n\n"
        "## Documentation\n\n- [Guide](https://example.com/guide): Begin here\n\n"
        "## Optional\n\n- [News](https://example.com/news)\n"
    )


def test_minimal_document_and_blank_optional_fields():
    assert render_document(LlmsTxtDocument(title="Site", summary=" \n", details="")) == "# Site\n"


def test_ipv6_link_authority_keeps_required_brackets():
    document = LlmsTxtDocument(
        title="Site",
        sections=[
            LlmsTxtSection(
                name="Pages",
                entries=[LlmsTxtEntry(title="DNS", url="https://[2606:4700:4700::1111]/docs")],
            )
        ],
    )
    assert render_document(document) == (
        "# Site\n\n## Pages\n\n- [DNS](https://[2606:4700:4700::1111]/docs)\n"
    )


def test_metadata_cannot_inject_markdown_or_html_and_destinations_are_encoded():
    document = LlmsTxtDocument(
        title="Site\n## injected",
        summary="<b>Hi</b> &copy;",
        sections=[
            LlmsTxtSection(
                name="Pages",
                entries=[
                    LlmsTxtEntry(
                        title="[link] *bold* \\ text",
                        url="https://example.com/a(b)?x=1&copy=2",
                        description="first\n\n## injected",
                    )
                ],
            )
        ],
    )
    assert render_document(document) == (
        "# Site \\#\\# injected\n\n> &lt;b&gt;Hi&lt;/b&gt; &amp;copy;\n\n"
        "## Pages\n\n- [\\[link\\] \\*bold\\* \\\\ text]"
        "(https://example.com/a%28b%29?x=1&amp;copy=2): first \\#\\# injected\n"
    )


def test_crawl_mapping_deduplicates_sorts_and_uses_metadata_fallbacks():
    crawl = CrawlResponse(
        requested_url="https://example.com/",
        results=[
            PageResult(
                url="https://example.com/",
                title="Example",
                description="A site",
            ),
            PageResult(url="https://example.com/z", title=None, description=None),
            PageResult(url="https://example.com/guide#top", title=" ", description=None),
            PageResult(url="https://example.com/guide", title="Duplicate", description=None),
        ],
    )
    document = build_document(crawl)
    assert document.title == "Example"
    assert document.summary == "A site"
    assert [(entry.title, entry.url) for entry in document.sections[0].entries] == [
        ("Example", "https://example.com/"),
    ]
    assert document.sections[1].name == "Optional"
    assert [(entry.title, entry.url) for entry in document.sections[1].entries] == [
        ("/guide", "https://example.com/guide"),
        ("/z", "https://example.com/z"),
    ]
    assert crawl.results[2].url.endswith("#top")


@pytest.mark.parametrize(
    ("title", "description"),
    [
        (None, "Details"),
        ("Title", None),
        (None, None),
        ("", "Details"),
        ("Title", ""),
        (" \n", "Details"),
        ("Title", "\t "),
    ],
)
def test_missing_either_metadata_field_places_page_in_optional(title, description):
    crawl = CrawlResponse(
        requested_url="https://example.com/",
        results=[
            PageResult(url="https://example.com/", title="Site", description="Summary"),
            PageResult(url="https://example.com/child", title=title, description=description),
        ],
    )
    document = build_document(crawl)
    assert [section.name for section in document.sections] == ["Pages", "Optional"]
    assert [entry.url for entry in document.sections[0].entries] == ["https://example.com/"]
    assert [entry.url for entry in document.sections[1].entries] == ["https://example.com/child"]


def test_complete_metadata_does_not_add_an_empty_optional_section():
    crawl = CrawlResponse(
        requested_url="https://example.com/",
        results=[
            PageResult(url="https://example.com/", title="Site", description="Summary"),
        ],
    )
    assert [section.name for section in build_document(crawl).sections] == ["Pages"]


def test_starting_page_can_redirect_and_title_can_be_overridden():
    crawl = CrawlResponse(
        requested_url="https://example.com/start",
        results=[
            PageResult(url="https://example.com/docs", title="Docs", description="Learn"),
        ],
    )
    assert build_document(crawl).summary == "Learn"
    assert build_document(crawl, title="My project").title == "My project"


def test_empty_crawl_falls_back_to_hostname():
    crawl = CrawlResponse(requested_url="https://example.com/", results=[])
    assert render_document(build_document(crawl)) == "# example.com\n"


@pytest.mark.parametrize("title", ["", " \n\t"])
def test_rejects_blank_required_title(title):
    with pytest.raises(ValueError):
        LlmsTxtDocument(title=title)


@pytest.mark.parametrize("url", ["javascript:alert(1)", "/relative"])
def test_rejects_non_http_or_relative_entry_urls(url):
    with pytest.raises(ValueError):
        LlmsTxtEntry(title="Bad", url=url)
