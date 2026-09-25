import asyncio
import ipaddress
import re
import socket
from collections.abc import Mapping
from dataclasses import dataclass

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult
from yarl import URL

from .config import Config
from .errors import ApiError

MAX_REDIRECTS = 3
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
DOWNLOAD_CHUNK_BYTES = 64 * 1024


@dataclass(frozen=True)
class Source:
    data: bytes
    ttl_seconds: int


def is_public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped or address.sixtofour or address.teredo or address.scope_id:
            return False
        if address in ipaddress.ip_network("64:ff9b::/96"):
            return False
    return address.is_global and not address.is_multicast


def validate_source_url(value: str, allowed_hosts: tuple[str, ...] = ()) -> URL:
    try:
        url = URL(value)
        if (
            url.scheme not in {"http", "https"}
            or not url.host
            or url.user is not None
            or url.password is not None
            or url.port != (443 if url.scheme == "https" else 80)
        ):
            raise ValueError
        host = url.host
    except ValueError, UnicodeError:
        raise ApiError(
            400, "INVALID_URL", "Use HTTP(S) on its default port without credentials"
        ) from None
    if allowed_hosts and host.lower() not in allowed_hosts:
        raise ApiError(403, "SOURCE_NOT_ALLOWED", "Source host is not allowed")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        try:
            address = str(ipaddress.IPv4Address(socket.inet_aton(host)))
        except OSError:
            pass
        else:
            if not is_public_address(address):
                raise ApiError(403, "SOURCE_NOT_ALLOWED", "Source must use a public IP address")
            raise ApiError(400, "INVALID_URL", "Use a canonical dotted-decimal IP address")
    else:
        if not is_public_address(host):
            raise ApiError(403, "SOURCE_NOT_ALLOWED", "Source must use a public IP address")
    return url.with_fragment(None)


class PublicResolver(AbstractResolver):
    def __init__(self, resolver: AbstractResolver | None = None) -> None:
        self.resolver = resolver or aiohttp.ThreadedResolver()

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> list[ResolveResult]:
        addresses = await self.resolver.resolve(host, port, family)
        if not addresses:
            raise ApiError(502, "UPSTREAM_ERROR", "Source host could not be resolved")
        if any(not is_public_address(address["host"]) for address in addresses):
            raise ApiError(
                403, "SOURCE_NOT_ALLOWED", "Source must resolve only to public IP addresses"
            )
        # The connector uses these addresses directly and retains the hostname for TLS.
        return addresses

    async def close(self) -> None:
        await self.resolver.close()


def source_ttl(headers: Mapping[str, str], limit: int) -> int:
    control = headers.get("Cache-Control", "")
    if (
        re.search(r"\b(no-store|private|no-cache)\b", control, re.I)
        or "Set-Cookie" in headers
        or headers.get("Vary", "").strip() == "*"
    ):
        return 0
    max_age = re.search(r'(?:^|,)\s*max-age\s*=\s*"?([0-9]+)', control, re.I)
    age = headers.get("Age", "0")
    if not re.fullmatch(r"[0-9]{1,10}", age):
        return 0
    return max(0, min(limit, int(max_age[1]) if max_age else limit) - int(age))


class SourceFetcher:
    def __init__(self, config: Config, session: aiohttp.ClientSession) -> None:
        self.config = config
        self.session = session

    async def __call__(self, value: str) -> Source:
        try:
            async with asyncio.timeout(self.config.fetch_timeout_ms / 1000):
                return await self._download(value)
        except TimeoutError:
            raise ApiError(504, "UPSTREAM_TIMEOUT", "Source download timed out") from None
        except aiohttp.ClientError, OSError, ValueError:
            raise ApiError(502, "UPSTREAM_ERROR", "Source could not be downloaded") from None

    async def _download(self, value: str) -> Source:
        url = validate_source_url(value, self.config.allowed_source_hosts)
        for redirects in range(MAX_REDIRECTS + 1):
            async with self.session.get(url, allow_redirects=False) as response:
                if response.status in REDIRECT_STATUSES:
                    location = response.headers.get("Location")
                    if redirects == MAX_REDIRECTS or not location:
                        raise ApiError(
                            502, "UPSTREAM_ERROR", "Source returned too many or invalid redirects"
                        )
                    url = validate_source_url(
                        str(url.join(URL(location))), self.config.allowed_source_hosts
                    )
                    continue
                return await self._read_response(response)
        raise AssertionError("Redirect limit was not enforced")

    async def _read_response(self, response: aiohttp.ClientResponse) -> Source:
        if response.status != 200:
            raise ApiError(
                502, "UPSTREAM_ERROR", "Source did not return a successful media response"
            )
        if response.headers.get("Content-Encoding", "identity").lower() != "identity":
            raise ApiError(502, "UPSTREAM_ERROR", "Source returned an unsupported content encoding")
        if int(response.headers.get("Content-Length", 0)) > self.config.max_source_bytes:
            raise ApiError(413, "SOURCE_TOO_LARGE", "Source exceeds the download size limit")
        data = bytearray()
        async for chunk in response.content.iter_chunked(DOWNLOAD_CHUNK_BYTES):
            if len(data) + len(chunk) > self.config.max_source_bytes:
                raise ApiError(413, "SOURCE_TOO_LARGE", "Source exceeds the download size limit")
            data.extend(chunk)
        if not data:
            raise ApiError(415, "INVALID_IMAGE", "Source is empty")
        return Source(bytes(data), source_ttl(response.headers, self.config.cache_ttl_seconds))


def create_session(config: Config) -> aiohttp.ClientSession:
    connector = aiohttp.TCPConnector(
        resolver=PublicResolver(),
        use_dns_cache=False,
        force_close=True,
        limit=config.max_concurrent_jobs,
    )
    return aiohttp.ClientSession(
        connector=connector,
        auto_decompress=False,
        trust_env=False,
        cookie_jar=aiohttp.DummyCookieJar(),
        timeout=aiohttp.ClientTimeout(total=None),
        headers={
            "Accept": "image/*, video/*",
            "Accept-Encoding": "identity",
            "User-Agent": "ImageProcessingService/1.0",
        },
    )
