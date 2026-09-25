import asyncio
import json
import sys
from dataclasses import asdict

from .config import Config
from .errors import ApiError
from .options import ProcessOptions, ThumbnailOptions
from .transform import ImageResult
from .video import extract_frame


class ImageProcessor:
    """Run media tools outside the API process under one processing deadline."""

    def __init__(self, config: Config) -> None:
        self.config = config

    async def __call__(self, source: bytes, options: ProcessOptions) -> ImageResult:
        try:
            async with asyncio.timeout(self.config.process_timeout_seconds):
                if isinstance(options, ThumbnailOptions):
                    source = await extract_frame(source, options, self.config)
                    values = asdict(options)
                    values.pop("time")
                    values["format"] = options.format or "jpeg"
                    options = ProcessOptions(**values)
                return await self._transform(source, options)
        except TimeoutError:
            raise ApiError(504, "PROCESSING_TIMEOUT", "Media processing timed out") from None

    async def _transform(self, source: bytes, options: ProcessOptions) -> ImageResult:
        header = json.dumps({"options": asdict(options), "config": asdict(self.config)}).encode()
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "image_service.worker",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            output, _ = await process.communicate(header + b"\n" + source)
        except asyncio.CancelledError:
            if process.returncode is None:
                process.kill()
            await process.communicate()
            raise
        if process.returncode:
            raise ApiError(500, "INTERNAL_ERROR", "Image worker failed")
        metadata, data = output.split(b"\n", 1)
        result = json.loads(metadata)
        if "error" in result:
            raise ApiError(**result["error"])
        return ImageResult(data, **result)
