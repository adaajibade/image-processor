# Image Processing Service

A Python API that resizes and converts images from public URLs and extracts video thumbnails. Built with FastAPI, aiohttp, Pillow, and FFmpeg. No database or queue is needed.

## Run

With Docker:

```sh
docker compose up --build -d
curl http://localhost:3000/health/ready
```

Or with Python 3.14, [uv](https://docs.astral.sh/uv/), and FFmpeg (`brew install ffmpeg` or `sudo apt-get install ffmpeg`):

```sh
uv sync --locked
uv run python -m image_service
```

Configuration comes from environment variables. Copy `.env.example` to `.env`; Compose reads it automatically, and locally you can run `uv run --env-file .env python -m image_service`. Without uv, install with `pip install --require-hashes -r requirements-dev.txt` in a Python 3.14 virtual environment.

## API

Interactive docs are at `/docs` and the OpenAPI spec at `/openapi.json`. `/health/live` and `/health/ready` are for load balancers.

### `GET /process`

| Parameter | Behavior |
| --- | --- |
| `url` | Required HTTP(S) URL. Encode the whole value. |
| `width`, `height` | Positive integers, up to 4096 by default. |
| `format` | `jpeg`, `png`, `webp`, or `avif`. Defaults to the source format. |
| `quality` | 1 to 100, default 80. Not allowed for PNG. |
| `crop` | `fit` (default), `fill`, or `pad`. |

`fit` keeps the aspect ratio inside the box, and a single dimension scales the other. `fill` crops from the center to the exact size. `pad` keeps the whole image and adds white borders. `fill` and `pad` need both dimensions.

Inputs must be static JPEG, PNG, WebP, or AVIF. EXIF rotation is applied, metadata is stripped, and transparency becomes white in JPEG output. Unknown, repeated, or malformed parameters return `400`.

```sh
SRC=https://raw.githubusercontent.com/github/explore/main/topics/python/python.png

# Resize
curl --fail-with-body -G localhost:3000/process --data-urlencode "url=$SRC" \
  -d width=500 -d height=300 -o resized.png

# Convert
curl --fail-with-body -G localhost:3000/process --data-urlencode "url=$SRC" \
  -d format=jpeg -d quality=80 -o converted.jpg

# Combine
curl --fail-with-body -G localhost:3000/process --data-urlencode "url=$SRC" \
  -d width=800 -d height=600 -d format=webp -d crop=fill -o thumbnail.webp
```

### `GET /video/thumbnail`

```sh
curl --fail-with-body -G localhost:3000/video/thumbnail \
  --data-urlencode 'url=https://interactive-examples.mdn.mozilla.net/media/cc0-videos/flower.mp4' \
  -d time=1.5 -d width=320 -o frame.jpg
```

Returns the first frame at or after `time` seconds (default `0`, up to three decimals). A time past the end returns `400 INVALID_TIME`. The other parameters work as on `/process`, but output defaults to JPEG.

Accepts MP4/MOV and WebM/Matroska with H.264, HEVC, MPEG-4, VP8, VP9, or AV1 video. Anything else returns `415 INVALID_VIDEO`. Videos share the 10 MiB download limit and 5-second processing deadline, so this route suits short clips.

### Python helper

Source URLs often carry their own query strings, which must be encoded to reach the API intact. `build_process_url` in `image_service/options.py` does this from typed options:

```python
from image_service.options import ProcessOptions, build_process_url

url = build_process_url(
    "http://localhost:3000",
    ProcessOptions(
        url="https://example.com/photo.png?version=2&size=large", width=800, format="webp"
    ),
)
```

`build_thumbnail_url` and `ThumbnailOptions` do the same for `/video/thumbnail`. The helper lives in the service package, not a separate client library, so it brings the server's dependencies with it.

### Responses and caching

Responses include `Content-Type`, `ETag`, `X-Image-Width`, `X-Image-Height`, `X-Cache` (`HIT`, `MISS`, or `COALESCED`), and `X-Request-Id`. Send `If-None-Match` to get `304` for unchanged output.

Results are cached in memory for up to 60 seconds, and identical in-flight requests share one job. The cache respects the source's `Cache-Control`, so `no-store` or `private` sources aren't kept. A changed source can stay stale until the TTL expires; use versioned URLs or set `CACHE_TTL_SECONDS=0`.

### Errors

```json
{
  "error": {
    "code": "INVALID_PARAMETER",
    "message": "width must be an integer between 1 and 4096",
    "requestId": "dfe358db-c02d-45c8-b35e-521f32330ec1"
  }
}
```

| Status | Codes |
| --- | --- |
| 400 | `INVALID_PARAMETER`, `INVALID_URL`, `INVALID_REQUEST`, `INVALID_TIME` |
| 403 | `SOURCE_NOT_ALLOWED`: private IP or host outside the allowlist |
| 404 | `NOT_FOUND`: unknown API route |
| 405 | `INVALID_REQUEST`: unsupported HTTP method |
| 413 | `SOURCE_TOO_LARGE`, `IMAGE_TOO_LARGE` |
| 415 | `INVALID_IMAGE`, `UNSUPPORTED_IMAGE`, `INVALID_VIDEO` |
| 500 | `INTERNAL_ERROR`, `VIDEO_UNAVAILABLE` (FFmpeg could not start) |
| 502 | `UPSTREAM_ERROR`: the source failed, including a missing image |
| 503 | `SERVICE_BUSY`: at capacity; sent with `Retry-After: 1` |
| 504 | `UPSTREAM_TIMEOUT`, `PROCESSING_TIMEOUT` |

`502`, `503`, and `504` are worth retrying with backoff. Other errors mean the request needs to change.

## Test

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

Tests generate their own images and videos, so they run offline, but video tests need FFmpeg. `pytest` requires 85% coverage. CI also audits dependencies, builds the container, and smoke-tests it.

With the service running, `uv run python scripts/smoke.py` checks a real public image end to end. Set `BASE_URL`, `SOURCE_URL`, or `VIDEO_SOURCE_URL` to change what it hits.

After changing dependencies, regenerate the pip files that Docker uses:

```sh
uv lock
uv export --locked --no-dev --no-emit-project -o requirements.txt
uv export --locked --no-emit-project -o requirements-dev.txt
```

## Configuration

| Variable | Default |
| --- | --- |
| `HOST`, `PORT` | `0.0.0.0`, `3000` |
| `LOG_LEVEL` | `info` |
| `ALLOWED_SOURCE_HOSTS` | Empty (any public host), or comma-separated hostnames |
| `MAX_SOURCE_BYTES`, `MAX_OUTPUT_BYTES` | 10 MiB each |
| `MAX_INPUT_PIXELS`, `MAX_OUTPUT_PIXELS` | `40000000`, `16000000` |
| `MAX_DIMENSION` | `4096` |
| `FETCH_TIMEOUT_MS` | `10000` |
| `PROCESS_TIMEOUT_SECONDS` | `5` |
| `MAX_CONCURRENT_JOBS` | `2` |
| `CACHE_MAX_BYTES`, `CACHE_MAX_ENTRIES` | 64 MiB, `256` |
| `CACHE_TTL_SECONDS` | `60` (`0` disables caching) |
