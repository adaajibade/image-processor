from dataclasses import replace
from io import BytesIO
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from image_service.app import create_app
from image_service.errors import ApiError
from image_service.source import Source


@pytest.fixture
def fetch_source(source):
    return AsyncMock(return_value=Source(source, 60))


@pytest.fixture
def client(config, fetch_source):
    with TestClient(create_app(config, fetch_source=fetch_source)) as client:
        yield client


def test_image_bytes_and_headers(client):
    response = client.get(
        "/process",
        params={
            "url": "https://example.com/a",
            "width": 60,
            "height": 40,
            "crop": "fill",
            "format": "webp",
        },
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/webp"
    assert response.headers["x-cache"] == "MISS"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-image-width"] == "60"
    with Image.open(BytesIO(response.content)) as image:
        assert image.size == (60, 40)
        assert image.format == "WEBP"


def test_cache_and_conditional_requests(client, fetch_source):
    url = "/process?url=https://example.com/a"
    first = client.get(url)
    second = client.get(url)
    assert second.headers["x-cache"] == "HIT"
    assert first.content == second.content
    assert fetch_source.await_count == 1
    etag = first.headers["etag"]
    for tag in [f"W/{etag}", f'"other", {etag}', "*"]:
        response = client.get(url, headers={"If-None-Match": tag})
        assert response.status_code == 304
        assert not response.content
        assert response.headers["etag"] == etag
    assert client.get(url, headers={"If-None-Match": '"different"'}).status_code == 200


def test_no_store_sources(client, fetch_source, source):
    fetch_source.return_value = Source(source, 0)
    for _ in range(2):
        response = client.get("/process?url=https://example.com/a")
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-cache"] == "MISS"
    assert fetch_source.await_count == 2


@pytest.mark.parametrize(
    "url",
    [
        "/process",
        "/process?url=https://example.com/a&width=0",
        "/process?url=https://example.com/a&width=20&width=30",
        "/process?url=https://example.com/a&quality=90&format=png",
        "/process?url=https://example.com/a&unexpected=1",
        "/process?url=https://example.com/a&crop=fill",
        "/process?url=file:///etc/passwd",
        "/process?url=" + "x" * 8200,
    ],
)
def test_invalid_query(client, fetch_source, url):
    response = client.get(url)
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"]
    assert error["message"]
    assert error["requestId"] == response.headers["x-request-id"]
    assert response.headers["cache-control"] == "no-store"
    fetch_source.assert_not_awaited()


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (403, "SOURCE_NOT_ALLOWED"),
        (413, "SOURCE_TOO_LARGE"),
        (415, "INVALID_IMAGE"),
        (502, "UPSTREAM_ERROR"),
        (503, "SERVICE_BUSY"),
        (504, "UPSTREAM_TIMEOUT"),
    ],
)
def test_error_contract(client, fetch_source, status, code):
    fetch_source.side_effect = ApiError(status, code, "Safe message")
    response = client.get("/process?url=https://example.com/a")
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    if status == 503:
        assert response.headers["retry-after"] == "1"


def test_internal_errors_are_sanitized(client, fetch_source, caplog):
    fetch_source.side_effect = RuntimeError("private token=secret")
    response = client.get("/process?url=https://example.com/a")
    assert response.status_code == 500
    assert "secret" not in response.text
    assert "secret" not in caplog.text
    assert response.headers["x-request-id"] in caplog.text
    assert "RuntimeError" in caplog.text


def test_health_docs_and_routing(client):
    assert client.get("/health/live").json() == {"status": "ok"}
    assert client.get("/health/ready").json() == {"status": "ready"}
    assert client.get("/docs").status_code == 200
    spec = client.get("/openapi.json").json()
    endpoint = spec["paths"]["/process"]["get"]
    assert "url" in [param["name"] for param in endpoint["parameters"]]
    assert endpoint["responses"]["200"]["content"]["image/webp"]["schema"]["format"] == "binary"
    assert "requestId" in spec["components"]["schemas"]["ErrorDetail"]["properties"]
    assert client.get("/missing").json()["error"]["code"] == "NOT_FOUND"
    assert client.post("/process").status_code == 405
    client.app.state.service.draining = True
    assert client.get("/health/ready").status_code == 503


@pytest.mark.parametrize("max_dimension", [50, 100])
def test_documented_limits_match_query_validation(config, fetch_source, max_dimension):
    with TestClient(
        create_app(replace(config, max_dimension=max_dimension), fetch_source)
    ) as client:
        parameters = client.get("/openapi.json").json()["paths"]["/process"]["get"]["parameters"]
        width_schema = next(
            parameter["schema"] for parameter in parameters if parameter["name"] == "width"
        )
        assert width_schema["maximum"] == max_dimension
        response = client.get(
            "/process", params={"url": "https://example.com/a", "width": max_dimension + 1}
        )
        assert response.status_code == 400
        fetch_source.assert_not_awaited()


def test_request_logs_omit_source_urls(config, fetch_source, caplog):
    with (
        caplog.at_level("INFO", logger="image_service"),
        TestClient(create_app(replace(config, log_level="info"), fetch_source)) as client,
    ):
        client.get("/process?url=https://example.com/a?token=secret")
    assert '"route": "/process"' in caplog.text
    assert "token" not in caplog.text
