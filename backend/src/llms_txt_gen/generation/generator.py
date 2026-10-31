"""Build llms.txt documents from crawl results and render them as Markdown."""

import re
from html import escape
from urllib.parse import quote, urlsplit

from llms_txt_gen.crawling.models import CrawlResponse
from llms_txt_gen.generation.models import LlmsTxtDocument, LlmsTxtEntry, LlmsTxtSection
from llms_txt_gen.urls import normalize_url

__all__ = ["build_document", "render_document"]


def _text(value: str | None) -> str:
    return " ".join((value or "").split())


def _markdown_text(value: str) -> str:
    # Metadata is plain text, including details. Prevent structural Markdown and HTML.
    text = re.sub(r"([\\`*{}\[\]()#+\-_|>~])", r"\\\1", escape(_text(value), quote=False))
    return re.sub(r"^(\d+)([.)])(?= )", r"\1\\\2", text)


def build_document(crawl: CrawlResponse, *, title: str | None = None) -> LlmsTxtDocument:
    """List retained pages once, sorted by normalized URL; first duplicate wins.

    The crawler returns the starting page first, including after a redirect.
    Pages missing a title or description go in Optional. URL paths supply
    labels when page titles are missing.
    """
    starting_page = crawl.results[0] if crawl.results else None
    hostname = urlsplit(crawl.requested_url).hostname or crawl.requested_url
    pages = {}
    for page in crawl.results:
        pages.setdefault(normalize_url(page.url), page)

    entries = []
    optional_entries = []
    for url, page in sorted(pages.items()):
        page_title = _text(page.title)
        description = _text(page.description)
        label = page_title or urlsplit(url).path or url
        entry = LlmsTxtEntry(title=label, url=url, description=description or None)
        if page_title and description:
            entries.append(entry)
        else:
            optional_entries.append(entry)

    sections = []
    if entries:
        sections.append(LlmsTxtSection(name="Pages", entries=entries))
    if optional_entries:
        sections.append(LlmsTxtSection(name="Optional", entries=optional_entries))

    site_title = (_text(starting_page.title) if starting_page else "") or hostname
    return LlmsTxtDocument(
        title=title if title is not None else site_title,
        summary=(_text(starting_page.description) or None) if starting_page else None,
        sections=sections,
    )


def render_document(document: LlmsTxtDocument) -> str:
    """Return Markdown with LF newlines, a trailing newline, and no BOM.

    Preserve supplied section/entry order, moving Optional sections to the end.
    Blank optional content and empty sections are omitted.
    """
    blocks = [f"# {_markdown_text(document.title)}"]
    if _text(document.summary):
        blocks.append(f"> {_markdown_text(document.summary)}")
    if _text(document.details):
        blocks.append(_markdown_text(document.details))
    sections = sorted(document.sections, key=lambda section: _text(section.name) == "Optional")
    for section in sections:
        if not section.entries:
            continue
        blocks.append(f"## {_markdown_text(section.name)}")
        lines = []
        for entry in section.entries:
            destination = quote(entry.url, safe="/:?#[]@!$&'*+,;=%~-").replace("&", "&amp;")
            label = _markdown_text(entry.title) or _markdown_text(entry.url)
            line = f"- [{label}]({destination})"
            if _text(entry.description):
                line += f": {_markdown_text(entry.description)}"
            lines.append(line)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"
