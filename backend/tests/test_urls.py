import pytest

from llms_txt_gen.urls import normalize_url


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "HTTPS://Example.COM:443/a/../docs?b=2&a=hello%20world#top",
            "https://example.com/docs?a=hello+world&b=2",
        ),
        (
            "https://bücher.example/?q=caf%C3%A9&b=2",
            "https://xn--bcher-kva.example/?b=2&q=caf%C3%A9",
        ),
        (
            "https://example.com/a%2Fb/?tag=b&z=2&tag=a&empty=",
            "https://example.com/a%2Fb/?empty=&tag=b&tag=a&z=2",
        ),
    ],
)
def test_url_normalization_is_idempotent(url, expected):
    assert normalize_url(url) == expected
    assert normalize_url(expected) == expected


def test_rejects_invalid_utf8_query_instead_of_merging_distinct_invalid_bytes():
    with pytest.raises(ValueError):
        normalize_url("https://example.com/?q=%FF")


def test_normalization_preserves_long_links_and_redirect_destinations():
    value = "x" * 2100
    assert normalize_url(f"https://example.com/search?z=2&q={value}#section") == (
        f"https://example.com/search?q={value}&z=2"
    )
