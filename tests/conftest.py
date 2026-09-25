import pytest

from image_service.config import Config

from .factories import make_image, make_video


@pytest.fixture
def config():
    return Config(log_level="silent")


@pytest.fixture
def source():
    return make_image()


@pytest.fixture(scope="session")
def video_source(tmp_path_factory):
    return make_video(tmp_path_factory.mktemp("videos") / "sample.mp4")


@pytest.fixture(scope="session")
def webm_source(tmp_path_factory):
    return make_video(tmp_path_factory.mktemp("webm") / "sample.webm", webm=True)
