import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Literal, cast, get_args
from urllib.parse import urlencode, urljoin

from .config import Config
from .errors import ApiError

Format = Literal["jpeg", "png", "webp", "avif"]
Crop = Literal["fit", "fill", "pad"]
FORMATS = get_args(Format)
CROPS = get_args(Crop)
DEFAULT_QUALITY = 80
MAX_QUALITY = 100
MAX_URL_LENGTH = 4096
MAX_VIDEO_TIME = 86400


@dataclass(frozen=True)
class ProcessOptions:
    url: str
    width: int | None = None
    height: int | None = None
    format: Format | None = None
    quality: int | None = None
    crop: Crop = "fit"


@dataclass(frozen=True)
class ThumbnailOptions(ProcessOptions):
    format: Format | None = "jpeg"
    time: float = 0.0


def parse_thumbnail_options(query: Mapping[str, str], config: Config) -> ThumbnailOptions:
    value = query.get("time", "0")
    if not re.fullmatch(r"[0-9]{1,5}(?:\.[0-9]{1,3})?", value) or float(value) > MAX_VIDEO_TIME:
        raise ApiError(
            400,
            "INVALID_PARAMETER",
            f"time must be seconds between 0 and {MAX_VIDEO_TIME}, with up to 3 decimal places",
        )
    image_query = {key: value for key, value in query.items() if key != "time"}
    image_query.setdefault("format", "jpeg")
    options = parse_options(image_query, config)
    return ThumbnailOptions(**asdict(options), time=float(value))


def parse_options(query: Mapping[str, str], config: Config) -> ProcessOptions:
    for key, value in query.items():
        if key not in {"url", "width", "height", "format", "quality", "crop"}:
            raise ApiError(400, "INVALID_PARAMETER", f"Unknown parameter: {key}")
        if not value:
            raise ApiError(400, "INVALID_PARAMETER", f"{key} must be a non-empty value")

    url = query.get("url", "")
    if not url or len(url) > MAX_URL_LENGTH:
        raise ApiError(
            400,
            "INVALID_PARAMETER",
            f"url is required and must be at most {MAX_URL_LENGTH} characters",
        )
    width = _parse_integer(query, "width", config.max_dimension)
    height = _parse_integer(query, "height", config.max_dimension)
    if width and height and width * height > config.max_output_pixels:
        raise ApiError(
            400, "INVALID_PARAMETER", f"Output must not exceed {config.max_output_pixels} pixels"
        )
    image_format = query.get("format")
    if image_format is not None and image_format not in FORMATS:
        raise ApiError(400, "INVALID_PARAMETER", f"format must be one of: {', '.join(FORMATS)}")
    quality = _parse_integer(query, "quality", MAX_QUALITY)
    crop = query.get("crop", "fit")
    if crop not in CROPS:
        raise ApiError(400, "INVALID_PARAMETER", "crop must be fit, fill, or pad")
    if crop != "fit" and (not width or not height):
        raise ApiError(400, "INVALID_PARAMETER", "crop=fill and crop=pad require width and height")
    validate_quality(image_format, quality)
    return ProcessOptions(
        url=url,
        width=width,
        height=height,
        format=cast(Format | None, image_format),
        quality=quality,
        crop=cast(Crop, crop),
    )


def _parse_integer(query: Mapping[str, str], key: str, maximum: int) -> int | None:
    value = query.get(key)
    if value is None:
        return None
    if not re.fullmatch(r"[1-9][0-9]{0,9}", value) or int(value) > maximum:
        raise ApiError(
            400, "INVALID_PARAMETER", f"{key} must be an integer between 1 and {maximum}"
        )
    return int(value)


def validate_quality(image_format: str | None, quality: int | None) -> None:
    if image_format == "png" and quality is not None:
        raise ApiError(
            400, "INVALID_PARAMETER", "quality is only supported for jpeg, webp, and avif"
        )


def build_process_url(base_url: str, options: ProcessOptions) -> str:
    """Build a processing URL without losing the source URL's query parameters."""
    query = {key: value for key, value in asdict(options).items() if value is not None}
    return f"{urljoin(base_url, '/process')}?{urlencode(query)}"


def build_thumbnail_url(base_url: str, options: ThumbnailOptions) -> str:
    """Build a video thumbnail URL with typed options."""
    query = {key: value for key, value in asdict(options).items() if value is not None}
    return f"{urljoin(base_url, '/video/thumbnail')}?{urlencode(query)}"
