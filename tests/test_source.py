import asyncio
import socket
from contextlib import asynccontextmanager
from dataclasses import replace
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiohttp import web
from multidict import CIMultiDict
from yarl import URL

from image_service.errors import ApiError
from image_service.source import (
    PublicResolver,
    SourceFetcher,
    create_session,
    is_public_address,
    source_ttl,
    validate_source_url,
)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.1.2.3",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "0.0.0.0",
        "100.64.0.1",
        "224.0.0.1",
        "192.0.2.1",
        "255.255.255.255",
        "::1",
        "::",
        "fc00::1",
        "fe80::1",
        "::ffff:127.0.0.1",
        "2001:db8::1",
        "2002:7f00:1::",
        "64:ff9b::7f00:1",
        "invalid",
    ],
)
def test_block_nonpublic_addresses(address):
    assert not is_public_address(address)


@pytest.mark.parametrize("address", ["8.8.8.8", "2606:4700:4700::1111"])
def test_public_addresses(address):
    assert is_public_address(address)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/a",
        "not a url",
        "https://user:pass@example.com/a",
        "http://example.com:8080/a",
        "http://[",
        "http://127.0.0.1",
        "http://[::1]",
        "http://[::ffff:127.0.0.1]",
        "http://2130706433/a",
        "http://0x7f000001/a",
        "http://127.1/a",
    ],
)
def test_unsafe_urls(url):
    with pytest.raises(ApiError):
        validate_source_url(url)


def test_url_normalization_and_allowlist():
    assert (
        str(validate_source_url("https://EXAMPLE.com:443/a#fragment", ("example.com",)))
        == "https://example.com/a"
    )
    with pytest.raises(ApiError, match="host is not allowed"):
        validate_source_url("https://sub.example.com/a", ("example.com",))


def test_noncanonical_public_ip():
    with pytest.raises(ApiError, match="canonical"):
        validate_source_url("http://0x08080808/a")


def address(host):
    return {
        "hostname": "example.com",
        "host": host,
        "port": 80,
        "family": socket.AF_INET,
        "proto": 0,
        "flags": socket.AI_NUMERICHOST,
    }


async def test_resolver_returns_only_checked_addresses():
    dns = AsyncMock()
    dns.resolve.return_value = [address("93.184.216.34")]
    resolver = PublicResolver(dns)
    assert await resolver.resolve("example.com", 80) == dns.resolve.return_value
    dns.resolve.assert_awaited_once()
    await resolver.close()
    dns.close.assert_awaited_once()


@pytest.mark.parametrize(
    "answers", [[], [address("127.0.0.1")], [address("8.8.8.8"), address("10.0.0.1")]]
)
async def test_unsafe_dns_answers(answers):
    dns = AsyncMock()
    dns.resolve.return_value = answers
    with pytest.raises(ApiError):
        await PublicResolver(dns).resolve("example.com")


@pytest.mark.parametrize("host", ["2130706433", "0x7f000001", "127.1"])
async def test_alternate_loopback_notation(host):
    resolver = PublicResolver()
    try:
        with pytest.raises(ApiError) as error:
            await resolver.resolve(host, 80)
        assert error.value.status == 403
    finally:
        await resolver.close()


class FakeResponse:
    def __init__(self, status=200, headers=None, chunks=None, failure=None):
        self.status = status
        self.headers = CIMultiDict(headers or {})
        self.chunks = [b"image"] if chunks is None else chunks
        self.failure = failure
        self.content = self

    async def iter_chunked(self, size):
        for chunk in self.chunks:
            yield chunk
        if self.failure:
            raise self.failure


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.urls = []

    @asynccontextmanager
    async def get(self, url, **kwargs):
        assert kwargs == {"allow_redirects": False}
        self.urls.append(str(url))
        yield self.responses.pop(0)


async def test_download_and_relative_redirect(config):
    session = FakeSession(FakeResponse(302, {"Location": "/b"}), FakeResponse())
    result = await SourceFetcher(config, session)("https://example.com/a")
    assert result.data == b"image"
    assert result.ttl_seconds == 60
    assert session.urls == ["https://example.com/a", "https://example.com/b"]


async def test_rebinding_between_redirects(config):
    dns = AsyncMock()
    dns.resolve.side_effect = [[address("93.184.216.34")], [address("127.0.0.1")]]
    resolver = PublicResolver(dns)

    class ResolvingSession(FakeSession):
        @asynccontextmanager
        async def get(self, url, **kwargs):
            await resolver.resolve(url.host, url.port)
            async with super().get(url, **kwargs) as response:
                yield response

    session = ResolvingSession(FakeResponse(302, {"Location": "/b"}), FakeResponse())
    with pytest.raises(ApiError) as error:
        await SourceFetcher(config, session)("https://example.com/a")
    assert error.value.status == 403
    assert dns.resolve.await_count == 2
    assert session.urls == ["https://example.com/a"]


async def test_redirect_to_private_address(config):
    session = FakeSession(FakeResponse(302, {"Location": "http://169.254.169.254/metadata"}))
    with pytest.raises(ApiError) as error:
        await SourceFetcher(config, session)("https://example.com/a")
    assert error.value.status == 403
    assert len(session.urls) == 1


async def test_redirect_outside_allowlist(config):
    session = FakeSession(FakeResponse(302, {"Location": "https://other.com/a"}))
    with pytest.raises(ApiError) as error:
        await SourceFetcher(replace(config, allowed_source_hosts=("example.com",)), session)(
            "https://example.com/a"
        )
    assert error.value.status == 403


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse(404),
        FakeResponse(500),
        FakeResponse(204),
        FakeResponse(302),
        FakeResponse(302, {"Location": "http://["}),
        FakeResponse(headers={"Content-Encoding": "gzip"}),
        FakeResponse(failure=aiohttp.ClientPayloadError("secret")),
    ],
)
async def test_bad_upstream(response, config):
    with pytest.raises(ApiError) as error:
        await SourceFetcher(config, FakeSession(response))("https://example.com/a")
    assert error.value.status == 502
    assert "secret" not in error.value.message


async def test_redirect_loop(config):
    session = FakeSession(*(FakeResponse(302, {"Location": "/loop"}) for _ in range(4)))
    with pytest.raises(ApiError) as error:
        await SourceFetcher(config, session)("https://example.com/a")
    assert error.value.status == 502
    assert len(session.urls) == 4


@pytest.mark.parametrize(
    "response",
    [FakeResponse(headers={"Content-Length": "5"}), FakeResponse(chunks=[b"123", b"45"])],
)
async def test_source_byte_limit(response, config):
    with pytest.raises(ApiError) as error:
        await SourceFetcher(replace(config, max_source_bytes=4), FakeSession(response))(
            "https://example.com/a"
        )
    assert error.value.status == 413


async def test_empty_source(config):
    with pytest.raises(ApiError) as error:
        await SourceFetcher(config, FakeSession(FakeResponse(chunks=[])))("https://example.com/a")
    assert error.value.status == 415


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"Cache-Control": "public, max-age=30", "Age": "10"}, 20),
        ({"Cache-Control": 'max-age="100"'}, 60),
        ({"Cache-Control": "no-store"}, 0),
        ({"Cache-Control": "private"}, 0),
        ({"Cache-Control": "no-cache"}, 0),
        ({"Set-Cookie": "session=1"}, 0),
        ({"Vary": "*"}, 0),
        ({"Age": "-1"}, 0),
        ({"Age": "invalid"}, 0),
    ],
)
def test_source_cache_policy(headers, expected):
    assert source_ttl(CIMultiDict(headers), 60) == expected


@pytest.fixture
async def origin(unused_tcp_port):
    async def handler(request):
        if request.path == "/stall":
            response = web.StreamResponse()
            await response.prepare(request)
            await response.write(b"first bytes")
            await asyncio.sleep(0.2)
            return response
        return web.Response(body=b"actual image bytes", headers={"X-Seen-Host": request.host})

    app = web.Application()
    app.router.add_get("/{path:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", unused_tcp_port).start()
    try:
        yield unused_tcp_port
    finally:
        await runner.cleanup()


async def test_connector_uses_the_resolved_ip_and_original_host(origin):
    dns = AsyncMock()
    dns.resolve.return_value = [{**address("127.0.0.1"), "port": origin}]
    async with aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(resolver=dns, use_dns_cache=False)
    ) as session:
        async with session.get(f"http://does-not-resolve.invalid:{origin}/a") as response:
            assert await response.read() == b"actual image bytes"
            assert response.headers["X-Seen-Host"] == f"does-not-resolve.invalid:{origin}"
    dns.resolve.assert_awaited_once()


async def test_stalled_download_deadline(origin, monkeypatch, config):
    monkeypatch.setattr("image_service.source.validate_source_url", lambda value, hosts: URL(value))
    async with create_session(config) as session:
        with pytest.raises(ApiError) as error:
            await SourceFetcher(replace(config, fetch_timeout_ms=50), session)(
                f"http://127.0.0.1:{origin}/stall"
            )
    assert error.value.code == "UPSTREAM_TIMEOUT"


async def test_dns_timeout(monkeypatch, config):
    async def resolve(*args, **kwargs):
        await asyncio.sleep(0.2)
        return []

    monkeypatch.setattr(PublicResolver, "resolve", resolve)
    async with create_session(config) as session:
        with pytest.raises(ApiError) as error:
            await SourceFetcher(replace(config, fetch_timeout_ms=20), session)(
                "https://example.com/a"
            )
    assert error.value.status == 504
