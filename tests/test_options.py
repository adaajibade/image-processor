from urllib.parse import parse_qs, urlsplit

import pytest

from image_service.config import Config
from image_service.errors import ApiError
from image_service.options import ProcessOptions, build_process_url, parse_options


def test_defaults_and_dimensions(config):
    assert parse_options({"url": "https://example.com/a", "width": "30"}, config) == ProcessOptions(
        url="https://example.com/a",
        width=30,
    )


@pytest.mark.parametrize(
    "query",
    [
        {"url": ""},
        {"url": "a" * 4097},
        {"width": "0"},
        {"width": "-1"},
        {"width": "1.5"},
        {"width": "1e2"},
        {"width": "5000"},
        {"height": ""},
        {"quality": "101"},
        {"quality": "0"},
        {"format": "gif"},
        {"crop": "stretch"},
        {"crop": "fill", "width": "10"},
        {"crop": "pad"},
        {"format": "png", "quality": "80"},
        {"extra": "value"},
        {"width": "4096", "height": "4096"},
        {"width": "9" * 5000},
    ],
)
def test_invalid_options(query, config):
    with pytest.raises(ApiError) as error:
        parse_options({"url": "https://example.com/a", **query}, config)
    assert error.value.status == 400


def test_missing_url(config):
    with pytest.raises(ApiError):
        parse_options({}, config)


def test_url_builder():
    source = "https://example.com/a?x=1&token=a+b#section"
    url = urlsplit(build_process_url("http://localhost:3000", ProcessOptions(source, width=50)))
    assert url.path == "/process"
    assert parse_qs(url.query)["url"] == [source]
    assert parse_qs(url.query)["width"] == ["50"]


def test_configuration():
    config = Config.from_env(
        {
            "PORT": "4000",
            "CACHE_MAX_BYTES": "0",
            "ALLOWED_SOURCE_HOSTS": " CDN.example.com,images.example.com ",
        }
    )
    assert config.port == 4000
    assert config.cache_max_bytes == 0
    assert config.allowed_source_hosts == ("cdn.example.com", "images.example.com")


@pytest.mark.parametrize(
    "env",
    [
        {"PORT": "0"},
        {"MAX_CONCURRENT_JOBS": "1.5"},
        {"LOG_LEVEL": "invalid"},
        {"ALLOWED_SOURCE_HOSTS": "*.example.com"},
        {"PORT": "65536"},
        {"FETCH_TIMEOUT_MS": ""},
    ],
)
def test_invalid_configuration(env):
    with pytest.raises(ValueError):
        Config.from_env(env)
