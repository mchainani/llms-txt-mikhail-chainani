"""Structured llms.txt documents and their validation."""

from pydantic import AnyHttpUrl, BaseModel, Field, field_validator


class LlmsTxtEntry(BaseModel):
    title: str
    url: str
    description: str | None = None

    @field_validator("url")
    @classmethod
    def validate_url(cls, url: str) -> str:
        # Validate without changing query order, fragments, or other URL semantics.
        return str(AnyHttpUrl(url))


class LlmsTxtSection(BaseModel):
    name: str
    entries: list[LlmsTxtEntry] = Field(default_factory=list)


class LlmsTxtDocument(BaseModel):
    title: str
    summary: str | None = None
    details: str | None = None
    sections: list[LlmsTxtSection] = Field(default_factory=list)

    @field_validator("title")
    @classmethod
    def require_title(cls, title: str) -> str:
        if not title.strip():
            raise ValueError("An llms.txt document requires a nonblank title")
        return title
