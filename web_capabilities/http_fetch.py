"""Experimental public HTTPS broker primitive, not a browser security boundary.

Requires Python 3.11+ and aiohttp 3.14.3. Share one FetchBudget across page fanout.
Budgets bound retained entity bytes, not TLS/framing bytes or transport buffers.
No upstream headers are returned; a broker must synthesize fulfillment headers,
intercept every browser request, and separately bound browser/DOM resources.
OS getaddrinfo workers may outlive cancellation; no connection uses late answers.
ASCII URLs only; IDNs must use A-labels. Special/transition IP ranges fail closed.
Injected resolvers and transports are trusted test seams, not caller options.
"""

import asyncio
import ipaddress
import math
import re
import socket
import ssl
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

import aiohttp
from yarl import URL


ALLOWED_CONTENT_TYPES = frozenset({
    "text/html", "text/javascript", "application/javascript", "text/css",
    "text/plain", "application/json",
})
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_SPECIAL_NETWORKS = tuple(ipaddress.ip_network(network) for network in (
    "192.0.0.0/24", "192.88.99.0/24", "2001::/23", "2002::/16", "3fff::/20",
))
_IPV6_UNICAST = ipaddress.ip_network("2000::/3")


class FetchError(RuntimeError):
    """Opaque failure without upstream content or credentials."""


class FetchPolicyError(FetchError):
    """URL, DNS answer, redirect, or response violates policy."""


class FetchLimitError(FetchError):
    """A response, page budget, or deadline was exhausted."""


def _positive_integer(value):
    return type(value) is int and value > 0


def _positive_seconds(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


class FetchBudget:
    """Atomic, nonrefundable page accounting; the deadline starts at construction.

    Failed attempts consume requests and accepted chunks consume bytes, even if
    their response later fails. Concurrent reads may buffer data, but cannot
    accumulate bytes past this budget. Exhaustion denies subsequent operations.
    """

    def __init__(self, *, max_requests=32, max_bytes=8 * 1024 * 1024, total_timeout=30):
        if not (_positive_integer(max_requests) and _positive_integer(max_bytes)
                and _positive_seconds(total_timeout)):
            raise ValueError("Invalid page budget")
        self._max_requests = max_requests
        self._max_bytes = max_bytes
        self._deadline = time.monotonic() + total_timeout
        self._requests_used = 0
        self._bytes_used = 0
        self._bytes_exhausted = False
        self._lock = threading.Lock()

    @property
    def requests_used(self):
        with self._lock:
            return self._requests_used

    @property
    def bytes_used(self):
        with self._lock:
            return self._bytes_used

    @property
    def request_count(self):
        """Reserved attempts, including failed DNS checks and followed hops."""
        return self.requests_used

    @property
    def total_bytes(self):
        """Charged entity bytes, including chunks from subsequently failed fetches."""
        return self.bytes_used

    @property
    def bytes_remaining(self):
        with self._lock:
            return 0 if self._bytes_exhausted else self._max_bytes - self._bytes_used

    def remaining_seconds(self):
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise FetchLimitError("Page deadline exhausted")
        return remaining

    def reserve_request(self):
        with self._lock:
            self.remaining_seconds()
            if self._requests_used >= self._max_requests:
                raise FetchLimitError("Page request budget exhausted")
            if self._bytes_exhausted:
                raise FetchLimitError("Page byte budget exhausted")
            self._requests_used += 1

    def consume_bytes(self, size):
        if type(size) is not int or size < 0:
            raise ValueError("Invalid byte count")
        with self._lock:
            self.remaining_seconds()
            if self._bytes_exhausted or size > self._max_bytes - self._bytes_used:
                self._bytes_exhausted = True
                raise FetchLimitError("Page byte budget exhausted")
            self._bytes_used += size


@dataclass(frozen=True)
class PinnedTarget:
    url: str
    host: str
    addresses: tuple[str, ...]


@dataclass(frozen=True)
class RedirectHop:
    source_url: str
    target_url: str
    status: int


@dataclass(frozen=True)
class FetchResult:
    body: bytes
    content_type: str | None
    final_url: str
    status: int
    redirect_chain: tuple[RedirectHop, ...]
    location: str | None = None


def _host(host):
    if not isinstance(host, str) or not host.isascii():
        raise FetchPolicyError("Invalid exact host")
    host = host.lower()
    labels = host.split(".")
    if (len(host) > 253 or len(labels) < 2
            or not re.fullmatch(r"[a-z][a-z0-9-]*", labels[-1])
            or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                   for label in labels)):
        raise FetchPolicyError("Invalid exact host")
    try:
        socket.inet_aton(host)
    except OSError:
        return host
    raise FetchPolicyError("IP literals are forbidden")


def _url_text(value):
    if (not isinstance(value, str) or not value or len(value) > 8192
            or any(ord(character) <= 32 or ord(character) >= 127 for character in value)
            or "\\" in value
            or re.search(r"%(?:0[0-9a-f]|1[0-9a-f]|7f|5c)", value, re.IGNORECASE)
            or re.search(r"%(?![0-9a-f]{2})", value, re.IGNORECASE)):
        raise FetchPolicyError("Invalid URL syntax")


def _canonical_url(value, allowed_hosts):
    _url_text(value)
    try:
        parsed = urlsplit(value)
        host = _host(parsed.hostname)
        if (parsed.scheme.lower() != "https" or host not in allowed_hosts
                or parsed.netloc.lower() not in {host, host + ":443"}):
            raise ValueError
        return urlunsplit(("https", host, parsed.path or "/", parsed.query, ""))
    except (ValueError, TypeError):
        raise FetchPolicyError("URL denied by exact HTTPS host policy") from None


def _public_address(address):
    if not isinstance(address, str) or "%" in address:
        raise FetchPolicyError("Invalid DNS answer")
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        raise FetchPolicyError("Invalid DNS answer") from None
    if (not parsed.is_global or parsed.is_multicast or parsed.is_reserved
            or parsed.is_loopback or parsed.is_link_local or parsed.is_unspecified
            or any(parsed in network for network in _SPECIAL_NETWORKS)
            or (parsed.version == 6 and parsed not in _IPV6_UNICAST)):
        raise FetchPolicyError("DNS answer is not public unicast")
    return str(parsed)


async def resolve_public_host(host):
    records = await asyncio.get_running_loop().getaddrinfo(
        host, 443, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM,
        proto=socket.IPPROTO_TCP,
    )
    return [record[4][0] for record in records]


class _PinnedResolver(aiohttp.abc.AbstractResolver):
    def __init__(self, target):
        self.target = target

    async def resolve(self, host, port=443, family=socket.AF_UNSPEC):
        if host != self.target.host or port != 443:
            raise FetchPolicyError("Transport attempted an unpinned destination")
        return [dict(hostname=host, host=address, port=443,
                     family=socket.AF_INET6 if ":" in address else socket.AF_INET,
                     proto=socket.IPPROTO_TCP, flags=socket.AI_NUMERICHOST)
                for address in self.target.addresses
                if family in (socket.AF_UNSPEC,
                              socket.AF_INET6 if ":" in address else socket.AF_INET)]

    async def close(self):
        return None


class AiohttpPinnedTransport:
    """Fresh direct TLS connection per hop, with no ambient credentials or DNS.

    The pinned aiohttp version's private retry switch prevents hidden GET/HEAD
    retries from bypassing request accounting; regression tests cover this seam.
    """

    @asynccontextmanager
    async def open(self, target, method, timeout):
        connector = aiohttp.TCPConnector(
            resolver=_PinnedResolver(target), use_dns_cache=False, force_close=True,
            limit=1, ssl=ssl.create_default_context(),
        )
        async with aiohttp.ClientSession(
            connector=connector, trust_env=False, cookie_jar=aiohttp.DummyCookieJar(),
            auto_decompress=False, timeout=aiohttp.ClientTimeout(total=timeout),
            headers={"Accept-Encoding": "identity", "Accept": ", ".join(sorted(ALLOWED_CONTENT_TYPES))},
            skip_auto_headers={"User-Agent", "Content-Type"}, read_bufsize=16384,
            max_line_size=8192, max_field_size=8192, max_headers=64,
        ) as session:
            session._retry_connection = False
            async with session.request(
                method, URL(target.url, encoded=True), allow_redirects=False,
                proxy=None, auth=None, server_hostname=target.host,
            ) as response:
                try:
                    yield response
                finally:
                    response.close()


def _header(response, name):
    values = response.headers.getall(name, [])
    if len(values) > 1:
        raise FetchPolicyError("Ambiguous response headers")
    if not values:
        return None
    value = values[0]
    if len(value) > 8192 or any(ord(character) < 32 or ord(character) >= 127 for character in value):
        raise FetchPolicyError("Invalid response header")
    return value.strip()


class PublicHTTPSFetcher:
    """GET/HEAD only. Resolver returns all IP strings; transport.open yields an
    aiohttp-shaped response with status, multidict headers, and content.read(n).
    With follow_redirects=False, location is validated and DNS-checked but not
    fetched. Its address snapshot is NOT reusable authority: re-pin on each call.
    Set max_redirects=0 to reject all redirects, including in non-following mode.
    Otherwise the broker must track redirect depth across separate fetch calls.
    """

    def __init__(self, *, allowed_hosts, max_response_bytes=2 * 1024 * 1024,
                 total_timeout=15, max_redirects=5, resolver=None, transport=None):
        if (isinstance(allowed_hosts, (str, bytes))
                or not _positive_integer(max_response_bytes)
                or not _positive_seconds(total_timeout)
                or type(max_redirects) is not int or not 0 <= max_redirects <= 5):
            raise ValueError("Invalid fetcher configuration")
        self.allowed_hosts = frozenset(_host(host) for host in allowed_hosts)
        if not self.allowed_hosts:
            raise ValueError("Exact allowed hosts are required")
        self.max_response_bytes = max_response_bytes
        self.total_timeout = total_timeout
        self.max_redirects = max_redirects
        self.resolver = resolver if resolver is not None else resolve_public_host
        self.transport = transport if transport is not None else AiohttpPinnedTransport()

    async def _pin(self, url):
        canonical = _canonical_url(url, self.allowed_hosts)
        host = urlsplit(canonical).hostname
        addresses = await self.resolver(host)
        if not isinstance(addresses, (list, tuple)) or not 1 <= len(addresses) <= 64:
            raise FetchPolicyError("Missing or excessive DNS answers")
        validated = tuple(dict.fromkeys(_public_address(address) for address in addresses))
        return PinnedTarget(canonical, host, validated)

    def _response_headers(self, response):
        disposition = _header(response, "Content-Disposition")
        if disposition is not None and (
                disposition.split(";", 1)[0].strip().lower() != "inline" or "," in disposition):
            raise FetchPolicyError("Download responses are forbidden")
        encoding = _header(response, "Content-Encoding")
        if encoding is not None and encoding.lower() != "identity":
            raise FetchPolicyError("Encoded responses are forbidden")
        length = _header(response, "Content-Length")
        transfer = _header(response, "Transfer-Encoding")
        if transfer is not None and (transfer.lower() != "chunked" or length is not None):
            raise FetchPolicyError("Ambiguous response framing")
        if length is not None:
            if not re.fullmatch(r"[0-9]{1,20}", length):
                raise FetchPolicyError("Invalid content length")
            if int(length) > self.max_response_bytes:
                raise FetchLimitError("Response byte limit exceeded")

    async def fetch(self, url, *, budget, method="GET", follow_redirects=True):
        if method not in {"GET", "HEAD"} or type(follow_redirects) is not bool:
            raise FetchPolicyError("Only explicit GET/HEAD requests are supported")
        if not isinstance(budget, FetchBudget):
            raise TypeError("A shared FetchBudget is required")
        canonical = _canonical_url(url, self.allowed_hosts)
        try:
            async with asyncio.timeout(min(self.total_timeout, budget.remaining_seconds())):
                return await self._fetch(canonical, budget, method, follow_redirects)
        except TimeoutError:
            raise FetchLimitError("Fetch deadline exhausted") from None
        except (aiohttp.ClientError, OSError):
            raise FetchError("HTTPS fetch failed") from None

    async def _fetch(self, url, budget, method, follow_redirects):
        budget.reserve_request()
        target = await self._pin(url)
        chain = []
        while True:
            async with self.transport.open(target, method, budget.remaining_seconds()) as response:
                self._response_headers(response)
                status = response.status
                if status in _REDIRECT_STATUSES:
                    if len(chain) >= self.max_redirects:
                        raise FetchLimitError("Redirect limit exceeded")
                    location = _header(response, "Location")
                    _url_text(location)
                    if location.startswith("///"):
                        raise FetchPolicyError("Ambiguous redirect URL")
                    try:
                        next_url = (location if urlsplit(location).scheme
                                    else urljoin(target.url, location))
                    except ValueError:
                        raise FetchPolicyError("Invalid redirect URL") from None
                    next_url = _canonical_url(next_url, self.allowed_hosts)
                else:
                    if not (200 <= status < 300 or 400 <= status < 600) or status == 206:
                        raise FetchPolicyError("Unsupported response status")
                    content_type = _header(response, "Content-Type")
                    if (content_type is None
                            or content_type.split(";", 1)[0].strip().lower() not in ALLOWED_CONTENT_TYPES):
                        raise FetchPolicyError("Response MIME type is forbidden")
                    body = bytearray()
                    if method != "HEAD":
                        while True:
                            read_size = min(65536, self.max_response_bytes - len(body) + 1,
                                            budget.bytes_remaining + 1)
                            chunk = await response.content.read(read_size)
                            if not chunk:
                                break
                            budget.consume_bytes(len(chunk))
                            if len(chunk) > self.max_response_bytes - len(body):
                                raise FetchLimitError("Response byte limit exceeded")
                            body.extend(chunk)
                    budget.remaining_seconds()
                    return FetchResult(bytes(body), content_type, target.url, status, tuple(chain))
            if follow_redirects:
                budget.reserve_request()
            next_target = await self._pin(next_url)
            chain.append(RedirectHop(target.url, next_target.url, status))
            if not follow_redirects:
                return FetchResult(b"", None, target.url, status, tuple(chain), next_target.url)
            target = next_target
