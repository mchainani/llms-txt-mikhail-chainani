from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import AnyHttpUrl


def normalize_url(url: str) -> str:
    """Canonicalize HTTP(S) URLs for fetching and deduplication.

    Query parameters use UTF-8 form encoding and sort by decoded name. The sort
    is stable: repeated names retain their value order and blank values remain.
    Path case, trailing slashes, and content-related parameters are preserved.
    """
    parts = urlsplit(str(AnyHttpUrl(url)))
    parameters = parse_qsl(parts.query, keep_blank_values=True, errors="strict")
    parameters.sort(key=lambda item: item[0])
    return urlunsplit(parts._replace(query=urlencode(parameters), fragment=""))
