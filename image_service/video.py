import asyncio
import json
from tempfile import NamedTemporaryFile

from .config import Config
from .errors import ApiError
from .options import ThumbnailOptions

PROBE_OUTPUT_BYTES = 64 * 1024
VIDEO_CODECS = {"h264", "hevc", "mpeg4", "vp8", "vp9", "av1"}
IMAGE_BRANDS = {"avif", "avis", "heic", "heix", "hevc", "hevx", "mif1", "msf1"}
INPUT_OPTIONS = (
    "-protocol_whitelist",
    "file,pipe",
    "-format_whitelist",
    "mov,matroska,webm",
    "-max_streams",
    "16",
    "-threads",
    "1",
)


async def extract_frame(source: bytes, options: ThumbnailOptions, config: Config) -> bytes:
    """Decode only downloaded bytes; FFmpeg never receives the source URL."""
    input_options: tuple[str, ...] = INPUT_OPTIONS
    if not source.startswith(b"\x1aE\xdf\xa3"):
        input_options += ("-enable_drefs", "0", "-use_absolute_path", "0")
    with NamedTemporaryFile(prefix="thumbnail-", suffix=".media") as video:
        video.write(source)
        video.flush()
        await _validate_video(video.name, config.max_input_pixels, input_options)
        frame = await _run(
            "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-xerror",
            *input_options,
            "-max_pixels",
            str(config.max_input_pixels),
            "-ss",
            str(options.time),
            "-i",
            video.name,
            "-map",
            "0:V:0",
            "-frames:v",
            "1",
            "-an",
            "-sn",
            "-dn",
            "-filter_threads",
            "1",
            "-threads",
            "1",
            "-f",
            "image2pipe",
            "-c:v",
            "png",
            "pipe:1",
            output_limit=config.max_input_pixels * 4 + PROBE_OUTPUT_BYTES,
        )
    if not frame:
        raise ApiError(400, "INVALID_TIME", "No video frame exists at or after the requested time")
    return frame


async def _validate_video(path: str, max_pixels: int, input_options: tuple[str, ...]) -> None:
    output = await _run(
        "ffprobe",
        "-v",
        "error",
        *input_options,
        "-select_streams",
        "V:0",
        "-show_entries",
        "stream=codec_name,width,height:format_tags=major_brand",
        "-of",
        "json",
        path,
        output_limit=PROBE_OUTPUT_BYTES,
    )
    try:
        metadata = json.loads(output)
        if metadata.get("format", {}).get("tags", {}).get("major_brand") in IMAGE_BRANDS:
            raise ValueError
        stream = metadata["streams"][0]
        width, height = int(stream["width"]), int(stream["height"])
        if stream["codec_name"] not in VIDEO_CODECS or width <= 0 or height <= 0:
            raise ValueError
    except ValueError, KeyError, IndexError, TypeError:
        raise ApiError(415, "INVALID_VIDEO", "Source has no supported video track") from None
    if width * height > max_pixels:
        raise ApiError(413, "IMAGE_TOO_LARGE", "Video frame exceeds the input pixel limit")


async def _run(*command: str, output_limit: int) -> bytes:
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        raise ApiError(500, "VIDEO_UNAVAILABLE", "Video tools could not be started") from None
    try:
        if process.stdout is None:
            raise ApiError(500, "VIDEO_UNAVAILABLE", "Video tools could not be started")
        output = bytearray()
        while chunk := await process.stdout.read(64 * 1024):
            if len(output) + len(chunk) > output_limit:
                raise ApiError(413, "IMAGE_TOO_LARGE", "Video tool output exceeds the size limit")
            output.extend(chunk)
        if await process.wait():
            raise ApiError(415, "INVALID_VIDEO", "Source is not a valid supported video")
        return bytes(output)
    finally:
        if process.returncode is None:
            process.kill()
        await process.communicate()
