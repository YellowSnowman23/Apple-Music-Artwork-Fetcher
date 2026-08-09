import io

import pytest
from PIL import Image

from apple_artwork import ArtworkDownloader, ArtworkError, build_artwork_urls, decode_artwork


def test_build_artwork_urls_prefers_the_unmodified_mzstatic_master() -> None:
    thumbnail = (
        "https://is1-ssl.mzstatic.com/image/thumb/Music126/v4/dd/50/c7/"
        "dd50c790-99ac-d3d0-5ab8-e3891fb8fd52/634904032463.png/100x100bb.jpg"
    )

    urls = build_artwork_urls(thumbnail)

    assert urls[0] == (
        "https://a5.mzstatic.com/us/r1000/0/Music126/v4/dd/50/c7/"
        "dd50c790-99ac-d3d0-5ab8-e3891fb8fd52/634904032463.png"
    )
    assert urls[1].endswith("/1x1ss.png")
    assert any(url.endswith("/10000x10000-999.png") for url in urls)
    assert urls[-1] == thumbnail


def test_decode_artwork_verifies_real_image_bytes_and_dimensions() -> None:
    buffer = io.BytesIO()
    Image.new("RGB", (32, 48), (10, 20, 30)).save(buffer, format="PNG")

    artwork = decode_artwork(buffer.getvalue(), "https://a5.mzstatic.com/master.png")

    assert artwork.mime == "image/png"
    assert (artwork.width, artwork.height, artwork.depth) == (32, 48, 24)
    assert len(artwork.sha256) == 64


def test_decode_artwork_rejects_an_html_error_body() -> None:
    with pytest.raises(ArtworkError, match="valid JPEG or PNG"):
        decode_artwork(b"<html>not artwork</html>", "https://a5.mzstatic.com/error")


class FakeImageResponse:
    def __init__(self, content: bytes, status_code: int = 200) -> None:
        self.content = content
        self.status_code = status_code
        self.headers: dict[str, str] = {"Content-Type": "image/png"}
        self.url = ""

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size: int) -> object:
        for offset in range(0, len(self.content), chunk_size):
            yield self.content[offset : offset + chunk_size]

    def close(self) -> None:
        return None


class FakeImageSession:
    def __init__(self, responses: list[FakeImageResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[str] = []
        self.headers: dict[str, str] = {}

    def get(
        self, url: str, *, timeout: float, allow_redirects: bool, stream: bool
    ) -> FakeImageResponse:
        self.calls.append(url)
        response = self.responses.pop(0)
        response.url = url
        return response


def test_artwork_downloader_falls_back_from_bad_master_and_caches_result(tmp_path) -> None:
    buffer = io.BytesIO()
    Image.new("RGB", (64, 64), (1, 2, 3)).save(buffer, format="PNG")
    session = FakeImageSession(
        [FakeImageResponse(b"bad master"), FakeImageResponse(buffer.getvalue())]
    )
    thumbnail = (
        "https://is1-ssl.mzstatic.com/image/thumb/Music126/v4/dd/50/c7/"
        "dd50c790-99ac-d3d0-5ab8-e3891fb8fd52/634904032463.png/100x100bb.jpg"
    )
    downloader = ArtworkDownloader(cache_dir=tmp_path / "cache", session=session, cdn_interval=0)

    first = downloader.fetch(42, thumbnail)
    second = downloader.fetch(42, thumbnail)

    assert first == second
    assert first.source_url.endswith("/1x1ss.png")
    assert len(session.calls) == 2
