from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from llms_txt_gen.crawling.models import PageResult
from llms_txt_gen.urls import normalize_url


def extract_page(
    html: str,
    url: str,
    soup: BeautifulSoup | None = None,
    requested_url: str | None = None,
) -> tuple[PageResult, list[str]]:
    """Return page metadata and internal crawl links on the requested hostname."""
    requested_url = requested_url or url
    if soup is None:
        soup = BeautifulSoup(html, "html.parser")
    base = soup.find("base", href=True)
    try:
        base_url = urljoin(url, str(base["href"])) if base else url
    except ValueError:
        base_url = url
    description = soup.find("meta", attrs={"name": "description"})
    if description is None:
        description = soup.find("meta", attrs={"property": "og:description"})

    links = []
    seen = set()
    requested_host = httpx.URL(requested_url).host
    for anchor in soup.select("a[href]"):
        href = str(anchor["href"])
        target = href.strip()
        if not target or target.startswith("#"):
            continue
        try:
            normalized = normalize_url(urljoin(base_url, target))
            parsed = httpx.URL(normalized)
            if (
                parsed.scheme not in {"http", "https"}
                or parsed.host != requested_host
                or parsed.userinfo
            ):
                continue
        except (ValueError, httpx.InvalidURL):
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        links.append(normalized)

    title = " ".join(soup.title.stripped_strings) if soup.title else None
    description_text = str(description.get("content", "")).strip() if description else None
    page = PageResult(
        url=normalize_url(url),
        title=title or None,
        description=description_text or None,
    )
    return page, links
