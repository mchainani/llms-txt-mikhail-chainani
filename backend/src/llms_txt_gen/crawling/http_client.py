"""Crawlee HTTP client with bounded, public-only network access."""

import asyncio
import ipaddress
import socket
from contextlib import asynccontextmanager
from http.cookiejar import CookieJar, DefaultCookiePolicy

import anyio
import httpx
from crawlee import HttpHeaders, Request
from crawlee.http_clients import HttpClient, HttpCrawlingResult

from llms_txt_gen.urls import normalize_url

MAX_HTML_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 5
REDIRECT_STATUSES = {301, 302, 303, 307, 308}


def create_http_client() -> httpx.AsyncClient:
    """Create an HTTP client owned by one crawl and its worker event loop."""
    return httpx.AsyncClient(
        transport=PublicHtmlTransport(),
        cookies=CookieJar(policy=DefaultCookiePolicy(allowed_domains=[])),
        timeout=httpx.Timeout(15, connect=5),
        follow_redirects=False,
        max_redirects=MAX_REDIRECTS,
        trust_env=False,
        headers={
            "User-Agent": "llms-txt-gen/0.1",
            "Accept": "text/html, application/xhtml+xml",
            "Accept-Encoding": "identity",
        },
    )


class FetchError(Exception):
    def __init__(self, status_code: int, detail: str, *, retryable: bool = False) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.retryable = retryable


def resolve_public_address(url: httpx.URL) -> str:
    """Resolve once and reject any non-public result before selecting an IP."""
    if url.scheme not in {"http", "https"} or not url.host or url.userinfo:
        raise FetchError(400, "Use an HTTP(S) URL without embedded credentials.")
    try:
        addresses = [ipaddress.ip_address(url.host)]
    except ValueError:
        try:
            records = socket.getaddrinfo(url.host, url.port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise FetchError(
                502, "Could not resolve the upstream hostname.", retryable=True
            ) from exc
        addresses = [ipaddress.ip_address(record[4][0]) for record in records]
    if not addresses or any(not ip.is_global or ip.is_multicast for ip in addresses):
        raise FetchError(400, "Only public internet addresses are allowed.")
    return str(addresses[0])


class PublicHtmlTransport(httpx.AsyncBaseTransport):
    """Validate every hop while HTTPX handles redirect mechanics for Crawlee."""

    def __init__(self) -> None:
        self._transport = httpx.AsyncHTTPTransport(
            retries=0, limits=httpx.Limits(max_keepalive_connections=0)
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        request.url = httpx.URL(normalize_url(str(request.url)))
        address = await anyio.to_thread.run_sync(
            resolve_public_address, request.url, abandon_on_cancel=True
        )
        headers = request.headers.copy()
        headers["Host"] = request.url.netloc.decode("ascii")
        pinned = httpx.Request(
            request.method,
            request.url.copy_with(host=address),
            headers=headers,
            stream=request.stream,
            extensions={**request.extensions, "sni_hostname": request.url.host},
        )
        response = await self._transport.handle_async_request(pinned)
        try:
            if response.status_code in REDIRECT_STATUSES:
                if not response.headers.get("location"):
                    raise FetchError(502, "The upstream redirect has no destination.")
                # Discard redirect bodies without buffering them.
                return httpx.Response(response.status_code, headers=response.headers)
            if not response.is_success:
                raise FetchError(
                    502,
                    f"The upstream server returned HTTP {response.status_code}.",
                    retryable=response.status_code == 429 or response.status_code >= 500,
                )
            media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if media_type not in {"text/html", "application/xhtml+xml"}:
                raise FetchError(415, "The upstream response must be HTML or XHTML.")
            body = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=16 * 1024):
                if len(body) + len(chunk) > MAX_HTML_BYTES:
                    raise FetchError(413, "The upstream HTML exceeds the 2 MiB limit.")
                body.extend(chunk)
            headers = response.headers.copy()
            # The stream is already decompressed. Preserve charset and other headers.
            headers.pop("content-encoding", None)
            headers.pop("content-length", None)
            return httpx.Response(response.status_code, headers=headers, content=bytes(body))
        finally:
            await response.aclose()

    async def aclose(self) -> None:
        await self._transport.aclose()


class HtmlResponse:
    """Adapt a bounded HTTPX response to Crawlee's public HttpResponse protocol."""

    def __init__(self, response: httpx.Response) -> None:
        self.response = response

    @property
    def http_version(self) -> str:
        return self.response.http_version

    @property
    def status_code(self) -> int:
        return self.response.status_code

    @property
    def headers(self) -> HttpHeaders:
        return HttpHeaders(dict(self.response.headers))

    async def read(self) -> bytes:
        return self.response.content

    def read_stream(self):
        return self.response.aiter_bytes()


class PublicHttpClient(HttpClient):
    """Crawlee adapter; the surrounding crawl context owns the HTTPX client."""

    def __init__(self, client: httpx.AsyncClient, *, deadline_at: float | None = None) -> None:
        super().__init__(persist_cookies_per_session=False)
        self._client = client
        self._deadline_at = deadline_at

    async def crawl(self, request: Request, *, statistics=None, **kwargs) -> HttpCrawlingResult:
        try:
            # Crawlee may wait for active navigation during cancellation. Give
            # every HTTP request the crawl's deadline so that cleanup cannot
            # wait for another network timeout or start a fresh retry.
            async with asyncio.timeout_at(self._deadline_at):
                if (
                    self._deadline_at is not None
                    and asyncio.get_running_loop().time() >= self._deadline_at
                ):
                    raise TimeoutError
                response = await self.send_request(request.url)
        except TimeoutError as exc:
            request.no_retry = True
            raise FetchError(504, "The upstream page timed out.") from exc
        except FetchError as exc:
            request.no_retry = not exc.retryable
            raise
        request.loaded_url = str(response.response.url)
        if statistics is not None:
            statistics.register_status_code(response.status_code)
        return HttpCrawlingResult(http_response=response)

    async def send_request(
        self, url: str, *, method="GET", headers=None, payload=None, **kwargs
    ) -> HtmlResponse:
        try:
            response = await self._client.request(
                method, url, headers=dict(headers or {}), content=payload, follow_redirects=True
            )
        except httpx.TimeoutException as exc:
            raise FetchError(504, "The upstream page timed out.", retryable=True) from exc
        except httpx.TooManyRedirects as exc:
            raise FetchError(502, "Too many upstream redirects.") from exc
        except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
            raise FetchError(502, "Could not fetch the upstream page.", retryable=True) from exc
        return HtmlResponse(response)

    @asynccontextmanager
    async def stream(self, url: str, **kwargs):
        yield await self.send_request(url, **kwargs)

    async def cleanup(self) -> None:
        # The surrounding async context manager closes the HTTPX client.
        pass
