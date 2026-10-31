"""Page metadata and crawl results."""

from pydantic import BaseModel


class PageResult(BaseModel):
    url: str
    title: str | None
    description: str | None


class CrawlResponse(BaseModel):
    requested_url: str
    results: list[PageResult]
