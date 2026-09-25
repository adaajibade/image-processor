import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, replace
from time import monotonic
from typing import Literal

from .cache import ByteCache
from .config import Config
from .errors import ApiError
from .options import ProcessOptions
from .source import Source, validate_source_url
from .transform import ImageResult

FetchSource = Callable[[str], Awaitable[Source]]
Transform = Callable[[bytes, ProcessOptions], Awaitable[ImageResult]]
CacheStatus = Literal["HIT", "MISS", "COALESCED"]


@dataclass(frozen=True)
class ProcessedImage(ImageResult):
    etag: str
    expires_at: float


class ImageService:
    def __init__(self, config: Config, fetch_source: FetchSource, transform: Transform) -> None:
        self.config = config
        self.fetch_source = fetch_source
        self.transform = transform
        self.cache: ByteCache[ProcessedImage] = ByteCache(
            config.cache_max_bytes, config.cache_max_entries
        )
        self.pending: dict[str, asyncio.Task[ProcessedImage]] = {}
        self.draining = False

    async def process(self, options: ProcessOptions) -> tuple[ProcessedImage, CacheStatus]:
        """Reuse completed or in-flight work without exceeding the per-process job limit."""
        if self.draining:
            raise ApiError(503, "SERVICE_BUSY", "Service is shutting down")
        options = replace(
            options, url=str(validate_source_url(options.url, self.config.allowed_source_hosts))
        )
        key = hashlib.sha256(json.dumps(asdict(options), sort_keys=True).encode()).hexdigest()
        cached = self.cache.get(key)
        if cached:
            return cached, "HIT"
        if key in self.pending:
            return await asyncio.shield(self.pending[key]), "COALESCED"
        if len(self.pending) >= self.config.max_concurrent_jobs:
            raise ApiError(503, "SERVICE_BUSY", "Media processing is at capacity; retry shortly")
        task = asyncio.create_task(self._run(key, options))
        self.pending[key] = task
        task.add_done_callback(self._observe_completion)
        return await asyncio.shield(task), "MISS"

    @staticmethod
    def _observe_completion(task: asyncio.Task[ProcessedImage]) -> None:
        # A disconnected caller may no longer be waiting to retrieve the task's error.
        if not task.cancelled():
            task.exception()

    async def _run(self, key: str, options: ProcessOptions) -> ProcessedImage:
        try:
            source = await self.fetch_source(options.url)
            expires_at = monotonic() + source.ttl_seconds
            result = await self.transform(source.data, options)
            image = ProcessedImage(
                data=result.data,
                content_type=result.content_type,
                width=result.width,
                height=result.height,
                etag=f'"{hashlib.sha256(result.data).hexdigest()}"',
                expires_at=expires_at,
            )
            self.cache.set(key, image, expires_at)
            return image
        finally:
            self.pending.pop(key, None)

    async def close(self) -> None:
        self.draining = True
        await asyncio.gather(*self.pending.values(), return_exceptions=True)
