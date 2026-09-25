import asyncio
import sys
from dataclasses import replace
from io import BytesIO

import pytest
from PIL import Image

from image_service.errors import ApiError
from image_service.options import ProcessOptions
from image_service.processor import ImageProcessor
from image_service.transform import transform

from .factories import make_image

OPTIONS = ProcessOptions("https://example.com/a")


@pytest.mark.parametrize("image_format", ["jpeg", "png", "webp", "avif"])
def test_output_formats_and_preservation(image_format, source, config):
    result = transform(source, replace(OPTIONS, format=image_format), config)
    with Image.open(BytesIO(result.data)) as decoded:
        assert decoded.format.lower() == image_format
    assert result.content_type == f"image/{image_format}"
    assert transform(result.data, OPTIONS, config).content_type == result.content_type


@pytest.mark.parametrize(
    ("options", "dimensions"),
    [
        ({"width": 60}, (60, 40)),
        ({"height": 40}, (60, 40)),
        ({"width": 50, "height": 50}, (50, 33)),
        ({"width": 50, "height": 50, "crop": "fill"}, (50, 50)),
        ({"width": 50, "height": 50, "crop": "pad"}, (50, 50)),
        ({"width": 240}, (240, 160)),
    ],
)
def test_resize(options, dimensions, source, config):
    result = transform(source, replace(OPTIONS, **options), config)
    with Image.open(BytesIO(result.data)) as decoded:
        assert decoded.size == dimensions


def test_padding_and_jpeg_alpha(source, config):
    result = transform(source, replace(OPTIONS, width=50, height=50, crop="pad"), config)
    with Image.open(BytesIO(result.data)) as decoded:
        assert decoded.getpixel((0, 0)) == (255, 255, 255, 255)
    jpeg = transform(source, replace(OPTIONS, format="jpeg"), config)
    with Image.open(BytesIO(jpeg.data)) as decoded:
        assert decoded.mode == "RGB"
        assert decoded.getpixel((0, 0))[0] > 220
        assert decoded.getpixel((0, 0))[1] > 130


def test_center_crop(config):
    image = Image.new("RGB", (90, 30), "red")
    image.paste("blue", (30, 0, 60, 30))
    data = BytesIO()
    image.save(data, format="PNG")
    result = transform(data.getvalue(), replace(OPTIONS, width=30, height=30, crop="fill"), config)
    with Image.open(BytesIO(result.data)) as decoded:
        assert decoded.getextrema() == ((0, 0), (0, 0), (255, 255))


def test_orientation_and_metadata(config):
    exif = Image.Exif()
    exif[274] = 6
    exif[315] = "private author"
    source = make_image(mode="RGB", color="red", image_format="JPEG", exif=exif)
    result = transform(source, replace(OPTIONS, width=40), config)
    with Image.open(BytesIO(result.data)) as decoded:
        assert decoded.size == (40, 60)
        assert not decoded.getexif()


def test_quality(config):
    image = Image.frombytes("RGB", (100, 100), bytes((i * 47) % 251 for i in range(30000)))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    low = transform(buffer.getvalue(), replace(OPTIONS, format="jpeg", quality=10), config)
    high = transform(buffer.getvalue(), replace(OPTIONS, format="jpeg", quality=95), config)
    assert len(low.data) < len(high.data)


@pytest.mark.parametrize(
    "source",
    [
        b"not an image",
        b'<svg width="10" height="10"/>',
        make_image(image_format="TIFF"),
        make_image(image_format="GIF"),
    ],
)
def test_unsupported_input(source, config):
    with pytest.raises(ApiError) as error:
        transform(source, OPTIONS, config)
    assert error.value.status == 415


def test_animated_input(config):
    output = BytesIO()
    Image.new("RGB", (2, 2), "red").save(
        output,
        format="WEBP",
        save_all=True,
        append_images=[Image.new("RGB", (2, 2), "blue")],
        duration=100,
        loop=0,
    )
    with pytest.raises(ApiError, match="static"):
        transform(output.getvalue(), OPTIONS, config)


def test_quality_on_inferred_png(source, config):
    with pytest.raises(ApiError, match="quality"):
        transform(source, replace(OPTIONS, quality=80), config)


@pytest.mark.parametrize(
    "overrides", [{"max_input_pixels": 50}, {"max_output_pixels": 50}, {"max_output_bytes": 10}]
)
def test_image_limits(overrides, source, config):
    with pytest.raises(ApiError) as error:
        transform(source, OPTIONS, replace(config, **overrides))
    assert error.value.status == 413


async def test_worker_process(source, config):
    result = await ImageProcessor(config)(source, replace(OPTIONS, width=60, format="webp"))
    with Image.open(BytesIO(result.data)) as decoded:
        assert decoded.size == (60, 40)
    with pytest.raises(ApiError) as error:
        await ImageProcessor(config)(b"bad image", OPTIONS)
    assert error.value.status == 415


async def test_timed_out_worker_is_killed_and_reaped(monkeypatch, source, config):
    original = asyncio.create_subprocess_exec
    processes = []

    async def slow_worker(*args, **kwargs):
        process = await original(sys.executable, "-c", "import time; time.sleep(60)", **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", slow_worker)
    with pytest.raises(ApiError) as error:
        await ImageProcessor(replace(config, process_timeout_seconds=1))(source, OPTIONS)
    assert error.value.code == "PROCESSING_TIMEOUT"
    assert processes[0].returncode is not None


async def test_cancelled_worker_is_reaped(monkeypatch, source, config):
    original = asyncio.create_subprocess_exec
    started = asyncio.Event()
    processes = []

    async def slow_worker(*args, **kwargs):
        process = await original(sys.executable, "-c", "import time; time.sleep(60)", **kwargs)
        processes.append(process)
        started.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", slow_worker)
    task = asyncio.create_task(ImageProcessor(config)(source, OPTIONS))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert processes[0].returncode is not None


async def test_failed_worker(monkeypatch, source, config):
    original = asyncio.create_subprocess_exec

    async def failing_worker(*args, **kwargs):
        return await original(sys.executable, "-c", "raise SystemExit(1)", **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", failing_worker)
    with pytest.raises(ApiError, match="worker failed"):
        await ImageProcessor(config)(source, OPTIONS)
