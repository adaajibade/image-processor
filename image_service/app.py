import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from time import monotonic
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse, Response

from .api import create_router
from .config import Config
from .errors import ApiError
from .processor import ImageProcessor
from .service import FetchSource, ImageService, Transform
from .source import SourceFetcher, create_session

logger = logging.getLogger("image_service")
MAX_QUERY_BYTES = 8192


def _error_response(request: Request, error: ApiError) -> JSONResponse:
    return JSONResponse(
        {
            "error": {
                "code": error.code,
                "message": error.message,
                "requestId": request.state.request_id,
            }
        },
        status_code=error.status,
        headers={
            "Cache-Control": "no-store",
            **({"Retry-After": "1"} if error.status == 503 else {}),
        },
    )


def create_app(
    config: Config | None = None,
    fetch_source: FetchSource | None = None,
    transform: Transform | None = None,
) -> FastAPI:
    config = config or Config.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with create_session(config) as session:
            app.state.service = ImageService(
                config,
                fetch_source or SourceFetcher(config, session),
                transform or ImageProcessor(config),
            )
            try:
                yield
            finally:
                await app.state.service.close()

    app = FastAPI(title="Image Processing Service", version="1.0.0", lifespan=lifespan)

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request.state.request_id = str(uuid4())
        started = monotonic()
        try:
            if len(request.scope.get("query_string", b"")) > MAX_QUERY_BYTES:
                raise ApiError(400, "INVALID_PARAMETER", "Query string is too long")
            response = await call_next(request)
        except ApiError as error:
            response = _error_response(request, error)
        except Exception as error:
            # Error messages can contain signed source URLs. Log only safe diagnostic fields.
            logger.error(
                json.dumps(
                    {
                        "requestId": request.state.request_id,
                        "errorType": type(error).__name__,
                        "message": "unexpected request failure",
                    }
                )
            )
            response = _error_response(
                request, ApiError(500, "INTERNAL_ERROR", "An unexpected error occurred")
            )
        response.headers["X-Request-Id"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers.setdefault("Cache-Control", "no-store")
        if config.log_level != "silent":
            logger.info(
                json.dumps(
                    {
                        "requestId": request.state.request_id,
                        "route": getattr(request.scope.get("route"), "path", "unmatched"),
                        "statusCode": response.status_code,
                        "elapsedMs": round((monotonic() - started) * 1000, 2),
                    }
                )
            )
        return response

    @app.exception_handler(ApiError)
    async def api_error(request: Request, error: ApiError) -> JSONResponse:
        return _error_response(request, error)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
        return _error_response(
            request, ApiError(400, "INVALID_PARAMETER", "Check the query parameters; see /docs")
        )

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException) -> JSONResponse:
        code = "NOT_FOUND" if error.status_code == 404 else "INVALID_REQUEST"
        return _error_response(request, ApiError(error.status_code, code, str(error.detail)))

    app.include_router(create_router(config))
    return app
