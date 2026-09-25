import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from image_service.cache import ByteCache
from image_service.errors import ApiError
from image_service.options import ProcessOptions
from image_service.service import ImageService
from image_service.source import Source
from image_service.transform import ImageResult

OPTIONS = ProcessOptions("https://example.com/a")
RESULT = ImageResult(b"image", "image/png", 1, 1)


def test_lru_size_and_count():
    cache = ByteCache(10, 2, clock=lambda: 0)
    cache.set("a", RESULT, 100)
    cache.set("b", RESULT, 100)
    cache.get("a")
    cache.set("c", RESULT, 100)
    assert cache.get("b") is None
    assert cache.get("a") == RESULT
    cache.set("a", replace(RESULT, data=b"1234567890"), 100)
    assert cache.get("c") is None


def test_expiry_and_disabled_cache():
    now = 0
    cache = ByteCache(5, 2, clock=lambda: now)
    cache.set("a", RESULT, 10)
    now = 10
    assert cache.get("a") is None
    cache.set("oversized", replace(RESULT, data=b"123456"), 100)
    cache.set("expired", RESULT, 9)
    assert cache.get("oversized") is None
    assert cache.get("expired") is None
    disabled = ByteCache(5, 0, clock=lambda: 0)
    disabled.set("a", RESULT, 100)
    assert disabled.get("a") is None


async def test_coalescing_and_overload(config):
    release = asyncio.Event()
    started = asyncio.Event()

    async def fetch(url):
        started.set()
        await release.wait()
        return Source(b"source", 60)

    service = ImageService(
        replace(config, max_concurrent_jobs=1), fetch, AsyncMock(return_value=RESULT)
    )
    first = asyncio.create_task(service.process(OPTIONS))
    await started.wait()
    second = asyncio.create_task(service.process(OPTIONS))
    with pytest.raises(ApiError) as error:
        await service.process(replace(OPTIONS, width=10))
    assert error.value.status == 503
    release.set()
    assert (await first)[1] == "MISS"
    assert (await second)[1] == "COALESCED"
    assert (await service.process(OPTIONS))[1] == "HIT"


async def test_failure_releases_capacity(config):
    fetch = AsyncMock(side_effect=[ValueError("failed"), Source(b"source", 60)])
    service = ImageService(config, fetch, AsyncMock(return_value=RESULT))
    with pytest.raises(ValueError):
        await service.process(OPTIONS)
    assert (await service.process(OPTIONS))[1] == "MISS"
    assert fetch.await_count == 2


async def test_cancelled_waiter_does_not_cancel_shared_work(config):
    release = asyncio.Event()
    started = asyncio.Event()

    async def fetch(url):
        started.set()
        await release.wait()
        return Source(b"source", 60)

    service = ImageService(config, fetch, AsyncMock(return_value=RESULT))
    first = asyncio.create_task(service.process(OPTIONS))
    await started.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert len(service.pending) == 1
    release.set()
    await service.close()
    assert not service.pending
    with pytest.raises(ApiError, match="shutting down"):
        await service.process(OPTIONS)


async def test_cache_expiry_and_option_keys(config, monkeypatch):
    now = 100
    monkeypatch.setattr("image_service.service.monotonic", lambda: now)
    fetch = AsyncMock(return_value=Source(b"source", 1))
    service = ImageService(config, fetch, AsyncMock(return_value=RESULT))
    service.cache.clock = lambda: now
    await service.process(OPTIONS)
    await service.process(replace(OPTIONS, format="webp"))
    now += 2
    assert (await service.process(OPTIONS))[1] == "MISS"
    assert fetch.await_count == 3
