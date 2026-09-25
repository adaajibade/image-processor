from time import monotonic
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse, Response

from .config import Config
from .errors import ApiError
from .options import (
    CROPS,
    DEFAULT_QUALITY,
    FORMATS,
    MAX_QUALITY,
    MAX_URL_LENGTH,
    MAX_VIDEO_TIME,
    parse_options,
    parse_thumbnail_options,
)
from .service import ImageService, ProcessedImage


class ErrorDetail(BaseModel):
    code: str
    message: str
    request_id: str = Field(alias="requestId")


class ErrorResponse(BaseModel):
    error: ErrorDetail


PROCESS_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "content": {
            f"image/{image_format}": {"schema": {"type": "string", "format": "binary"}}
            for image_format in FORMATS
        }
    },
    304: {"description": "Image unchanged"},
    **{status: {"model": ErrorResponse} for status in (400, 403, 413, 415, 500, 502, 503, 504)},
}


def _query_parameters(config: Config, *, video: bool = False) -> list[dict[str, Any]]:
    schemas = {
        "url": {
            "type": "string",
            "maxLength": MAX_URL_LENGTH,
            "description": "Public HTTP(S) URL; encode the full value.",
        },
        "width": {"type": "integer", "minimum": 1, "maximum": config.max_dimension},
        "height": {"type": "integer", "minimum": 1, "maximum": config.max_dimension},
        "format": {
            "type": "string",
            "enum": list(FORMATS),
            "description": "Defaults to JPEG." if video else "Defaults to the source format.",
        },
        "quality": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_QUALITY,
            "default": DEFAULT_QUALITY,
            "description": "Not supported for PNG.",
        },
        "crop": {
            "type": "string",
            "enum": list(CROPS),
            "default": "fit",
            "description": "fill and pad require both dimensions.",
        },
    }
    if video:
        schemas["time"] = {
            "type": "number",
            "minimum": 0,
            "maximum": MAX_VIDEO_TIME,
            "multipleOf": 0.001,
            "default": 0,
            "description": "Seconds from the start, with up to 3 decimal places.",
        }
    return [
        {"name": name, "in": "query", "required": name == "url", "schema": schema}
        for name, schema in schemas.items()
    ]


def create_router(config: Config) -> APIRouter:
    router = APIRouter()

    @router.get("/health/live", include_in_schema=False)
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/health/ready", include_in_schema=False)
    async def ready(request: Request) -> JSONResponse:
        service: ImageService = request.app.state.service
        return JSONResponse(
            {"status": "draining" if service.draining else "ready"},
            status_code=503 if service.draining else 200,
        )

    @router.get(
        "/process",
        response_class=Response,
        summary="Resize or convert an image",
        description=(
            "Static JPEG, PNG, WebP and AVIF. fit preserves aspect ratio; "
            "fill crops centrally; pad adds white padding. Metadata is stripped."
        ),
        responses=PROCESS_RESPONSES,
        openapi_extra={"parameters": _query_parameters(config)},
    )
    async def process(request: Request) -> Response:
        return await handle_image_request(request)

    @router.get(
        "/video/thumbnail",
        response_class=Response,
        summary="Extract a video frame",
        description=(
            "MP4/MOV and WebM/Matroska with H.264, HEVC, MPEG-4, VP8, VP9 or AV1. "
            "Returns the first frame at or after time (seconds), "
            "as JPEG by default. Supports the same image transformations as /process. "
            "A time beyond the last frame returns 400 INVALID_TIME. "
            f"Sources are limited to {config.max_source_bytes} bytes; "
            f"processing has a {config.process_timeout_seconds}-second deadline."
        ),
        responses=PROCESS_RESPONSES,
        openapi_extra={"parameters": _query_parameters(config, video=True)},
    )
    async def thumbnail(request: Request) -> Response:
        return await handle_image_request(request, video=True)

    async def handle_image_request(request: Request, *, video: bool = False) -> Response:
        if len(request.query_params.multi_items()) != len(request.query_params):
            raise ApiError(400, "INVALID_PARAMETER", "Each query parameter must appear once")
        parser = parse_thumbnail_options if video else parse_options
        options = parser(request.query_params, config)
        service: ImageService = request.app.state.service
        image, cache_status = await service.process(options)
        return _image_response(request, image, cache_status)

    return router


def _image_response(request: Request, image: ProcessedImage, cache_status: str) -> Response:
    max_age = max(0, int(image.expires_at - monotonic()))
    headers = {
        "Content-Type": image.content_type,
        "Cache-Control": f"private, max-age={max_age}" if max_age else "no-store",
        "ETag": image.etag,
        "X-Cache": cache_status,
        "X-Image-Width": str(image.width),
        "X-Image-Height": str(image.height),
    }
    tags = request.headers.get("If-None-Match", "").split(",")
    if any(tag.strip() == "*" or tag.strip().removeprefix("W/") == image.etag for tag in tags):
        return Response(status_code=304, headers=headers)
    return Response(image.data, headers=headers)
