import asyncio
import subprocess
import sys
from dataclasses import replace
from io import BytesIO
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from image_service.app import create_app
from image_service.errors import ApiError
from image_service.options import (
    ThumbnailOptions,
    build_thumbnail_url,
    parse_thumbnail_options,
)
from image_service.processor import ImageProcessor
from image_service.source import Source
from image_service.video import _run

from .factories import make_image

OPTIONS = ThumbnailOptions("https://example.com/video.mp4")


@pytest.mark.parametrize("value", ["-1", "NaN", "inf", "1e2", "", "1.2345", "86401", "1;ls"])
def test_invalid_time(value, config):
    with pytest.raises(ApiError, match="time must"):
        parse_thumbnail_options({"url": OPTIONS.url, "time": value}, config)


def test_thumbnail_options_and_url_builder(config):
    assert parse_thumbnail_options({"url": OPTIONS.url}, config) == OPTIONS
    options = parse_thumbnail_options({"url": OPTIONS.url, "time": "1.250"}, config)
    assert options.time == 1.25
    url = urlsplit(build_thumbnail_url("http://localhost:3000", options))
    assert url.path == "/video/thumbnail"
    assert parse_qs(url.query)["time"] == ["1.25"]


@pytest.mark.parametrize(("time", "channel"), [(0, 0), (0.5, 0), (1.25, 2)])
async def test_frame_selection(video_source, config, time, channel):
    result = await ImageProcessor(config)(video_source, replace(OPTIONS, time=time))
    assert result.content_type == "image/jpeg"
    with Image.open(BytesIO(result.data)) as frame:
        assert frame.size == (96, 64)
        pixel = frame.getpixel((48, 32))
        assert pixel[channel] > 220
        assert pixel[2 if channel == 0 else 0] < 30


async def test_webm(webm_source, config):
    result = await ImageProcessor(config)(webm_source, replace(OPTIONS, time=1.25, format="webp"))
    with Image.open(BytesIO(result.data)) as frame:
        assert frame.format == "WEBP"
        assert frame.getpixel((48, 32))[2] > 220


@pytest.mark.parametrize("image_format", ["jpeg", "png", "webp", "avif"])
async def test_thumbnail_formats(video_source, config, image_format):
    result = await ImageProcessor(config)(video_source, replace(OPTIONS, format=image_format))
    with Image.open(BytesIO(result.data)) as frame:
        assert frame.format.lower() == image_format


@pytest.mark.parametrize(
    ("crop", "size"), [("fit", (30, 20)), ("fill", (30, 30)), ("pad", (30, 30))]
)
async def test_thumbnail_resize(video_source, config, crop, size):
    result = await ImageProcessor(config)(
        video_source, replace(OPTIONS, width=30, height=30, crop=crop, format="png")
    )
    with Image.open(BytesIO(result.data)) as frame:
        assert frame.size == size
        if crop == "pad":
            assert frame.getpixel((0, 0))[:3] == (255, 255, 255)


@pytest.mark.parametrize("time", [2, 999])
async def test_time_beyond_last_frame(video_source, config, time):
    with pytest.raises(ApiError) as error:
        await ImageProcessor(config)(video_source, replace(OPTIONS, time=time))
    assert error.value.status == 400
    assert error.value.code == "INVALID_TIME"


@pytest.mark.parametrize("body", [b"bad video", b"#EXTM3U\nhttp://127.0.0.1/secret.ts\n"])
async def test_invalid_video_and_playlists(body, config):
    with pytest.raises(ApiError) as error:
        await ImageProcessor(config)(body, OPTIONS)
    assert error.value.code == "INVALID_VIDEO"


@pytest.mark.parametrize("image_format", ["PNG", "AVIF"])
async def test_still_image_is_not_a_video(image_format, config):
    with pytest.raises(ApiError) as error:
        await ImageProcessor(config)(make_image(image_format=image_format), OPTIONS)
    assert error.value.status == 415


@pytest.mark.parametrize("variant", ["audio", "unsupported", "truncated"])
async def test_video_requires_a_supported_track(variant, tmp_path, video_source, config):
    source = tmp_path / "source.mp4"
    source.write_bytes(video_source)
    target = tmp_path / "variant.mov"
    if variant == "audio":
        inputs = ["-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono", "-t", "1", "-c:a", "aac"]
    else:
        inputs = ["-i", str(source), "-c:v", "png", "-threads", "1"]
    if variant == "truncated":
        data = video_source[: len(video_source) // 2]
    else:
        await asyncio.to_thread(
            subprocess.run,
            ["ffmpeg", "-v", "error", "-nostdin", *inputs, str(target)],
            check=True,
            capture_output=True,
            timeout=10,
        )
        data = target.read_bytes()
    with pytest.raises(ApiError) as error:
        await ImageProcessor(config)(data, OPTIONS)
    assert error.value.code == "INVALID_VIDEO"


@pytest.mark.parametrize(
    "limits", [{"max_input_pixels": 100}, {"max_output_pixels": 100}, {"max_output_bytes": 10}]
)
async def test_video_limits(video_source, config, limits):
    with pytest.raises(ApiError) as error:
        await ImageProcessor(replace(config, **limits))(video_source, OPTIONS)
    assert error.value.status == 413


async def test_missing_ffmpeg_is_reported(monkeypatch, video_source, config):
    monkeypatch.setenv("PATH", "")
    with pytest.raises(ApiError) as error:
        await ImageProcessor(config)(video_source, OPTIONS)
    assert error.value.code == "VIDEO_UNAVAILABLE"


async def test_video_output_is_bounded():
    with pytest.raises(ApiError) as error:
        await _run(sys.executable, "-c", "print('a' * 100000)", output_limit=10)
    assert error.value.status == 413


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("stage", [1, 2])
async def test_video_deadline_and_cancellation_reap_process(
    monkeypatch, tmp_path, video_source, config, cancel, stage
):
    original = asyncio.create_subprocess_exec
    started = asyncio.Event()
    processes = []
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))

    async def slow_tool(*args, **kwargs):
        slow = len(processes) + 1 == stage
        command = (sys.executable, "-c", "import time; time.sleep(60)") if slow else args
        process = await original(*command, **kwargs)
        processes.append(process)
        if slow:
            started.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", slow_tool)
    task = asyncio.create_task(
        ImageProcessor(replace(config, process_timeout_seconds=1))(video_source, OPTIONS)
    )
    await started.wait()
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(ApiError) as error:
            await task
        assert error.value.code == "PROCESSING_TIMEOUT"
    assert all(process.returncode is not None for process in processes)
    assert not list(tmp_path.iterdir())


def test_video_endpoint_cache_etag_and_time_keys(video_source, config):
    fetch = AsyncMock(return_value=Source(video_source, 60))
    with TestClient(create_app(config, fetch_source=fetch)) as client:
        params = {"url": OPTIONS.url, "time": "0", "width": "48"}
        first = client.get("/video/thumbnail", params=params)
        assert first.status_code == 200, first.text
        assert first.headers["content-type"] == "image/jpeg"
        assert first.headers["x-image-width"] == "48"
        cached = client.get("/video/thumbnail", params=params)
        assert cached.headers["x-cache"] == "HIT"
        conditional = client.get(
            "/video/thumbnail", params=params, headers={"If-None-Match": first.headers["etag"]}
        )
        assert conditional.status_code == 304
        later = client.get("/video/thumbnail", params={**params, "time": "1.25"})
        assert later.status_code == 200
        assert later.content != first.content
        assert fetch.await_count == 2
        assert client.get("/process", params={"url": OPTIONS.url}).status_code == 415


@pytest.mark.parametrize(
    "query",
    [
        "url=http://127.0.0.1/a",
        "url=file:///etc/passwd",
        "url=https://example.com/a&time=1&time=2",
        "url=https://example.com/a&time=-1",
        "url=https://example.com/a&format=png&quality=80",
        "url=https://example.com/a&extra=1",
    ],
)
def test_video_query_rejected_before_download(query, config):
    fetch = AsyncMock()
    with TestClient(create_app(config, fetch_source=fetch)) as client:
        response = client.get("/video/thumbnail?" + query)
        assert response.status_code in (400, 403)
        assert response.json()["error"]["requestId"] == response.headers["x-request-id"]
        fetch.assert_not_awaited()


def test_video_openapi(config):
    with TestClient(create_app(config)) as client:
        endpoint = client.get("/openapi.json").json()["paths"]["/video/thumbnail"]["get"]
        time = next(item for item in endpoint["parameters"] if item["name"] == "time")
        assert time["schema"]["minimum"] == 0
        assert time["schema"]["default"] == 0
