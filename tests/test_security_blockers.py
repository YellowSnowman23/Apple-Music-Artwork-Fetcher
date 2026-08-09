import io
import json
from pathlib import Path

import pytest
from PIL import Image

import apple_artwork
from apple_artwork import (
    AppleCatalogClient,
    ArtworkDownloader,
    ArtworkError,
    decode_artwork,
    discover_audio_files,
    process_library,
)


def png_bytes(size: tuple[int, int] = (48, 48), color: tuple[int, int, int] = (1, 2, 3)) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, format="PNG")
    return output.getvalue()


def jpeg_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (64, 64), (10, 20, 30)).save(output, format="JPEG", quality=90)
    return output.getvalue()


class StreamingResponse:
    def __init__(
        self,
        chunks: list[bytes],
        *,
        status_code: int = 200,
        url: str = "https://a5.mzstatic.com/art.png",
        headers: dict[str, str] | None = None,
    ) -> None:
        self._chunks = chunks
        self.status_code = status_code
        self.url = url
        self.headers = headers or {"Content-Type": "image/png"}

    @property
    def content(self) -> bytes:
        raise AssertionError("response.content must not materialize an unbounded body")

    def json(self) -> object:
        raise AssertionError("response.json() must not materialize an unbounded body")

    def iter_content(self, chunk_size: int = 65_536):
        del chunk_size
        yield from self._chunks

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def close(self) -> None:
        return None


class StreamingSession:
    def __init__(self, responses: list[StreamingResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.headers: dict[str, str] = {}

    def get(self, url: str, **kwargs: object) -> StreamingResponse:
        self.calls.append((url, dict(kwargs)))
        return self.responses.pop(0)


def test_decode_artwork_fully_decodes_and_rejects_truncated_jpeg() -> None:
    with pytest.raises(ArtworkError, match=r"valid JPEG or PNG"):
        decode_artwork(jpeg_bytes()[:-1], "https://a5.mzstatic.com/truncated.jpg")


def test_decode_artwork_rejects_placeholder_dimensions() -> None:
    with pytest.raises(ArtworkError, match=r"too small"):
        decode_artwork(png_bytes((1, 1)), "https://a5.mzstatic.com/placeholder.png")


def test_artwork_downloader_streams_a_bounded_body(tmp_path: Path) -> None:
    image = png_bytes()
    session = StreamingSession([StreamingResponse([image[:20], image[20:]])])
    downloader = ArtworkDownloader(
        cache_dir=tmp_path / "cache",
        session=session,
        cdn_interval=0,
    )

    artwork = downloader.fetch(1, "https://a5.mzstatic.com/art.png")

    assert artwork.data == image
    assert session.calls[0][1]["stream"] is True
    assert session.calls[0][1]["allow_redirects"] is False


def test_artwork_downloader_rejects_untrusted_final_redirect_url(tmp_path: Path) -> None:
    response = StreamingResponse(
        [png_bytes()],
        url="http://169.254.169.254/latest/meta-data",
    )
    downloader = ArtworkDownloader(
        cache_dir=tmp_path / "cache",
        session=StreamingSession([response]),
        cdn_interval=0,
    )

    with pytest.raises(ArtworkError, match=r"redirect|URL|HTTPS|host"):
        downloader.fetch(1, "https://a5.mzstatic.com/art.png")


def test_artwork_downloader_rejects_body_over_configured_limit(tmp_path: Path) -> None:
    response = StreamingResponse([b"12345678", b"abcdefgh"])
    downloader = ArtworkDownloader(
        cache_dir=tmp_path / "cache",
        session=StreamingSession([response]),
        cdn_interval=0,
        max_response_bytes=12,
    )

    with pytest.raises(ArtworkError, match=r"large|limit"):
        downloader.fetch(1, "https://a5.mzstatic.com/art.png")


def test_retry_after_is_not_honored_after_final_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = StreamingResponse(
        [],
        status_code=429,
        headers={"Retry-After": "999999999999"},
    )
    downloader = ArtworkDownloader(
        cache_dir=tmp_path / "cache",
        session=StreamingSession([response]),
        cdn_interval=0,
        max_retries=1,
    )
    sleeps: list[float] = []
    monkeypatch.setattr(apple_artwork.time, "sleep", sleeps.append)

    with pytest.raises(ArtworkError):
        downloader.fetch(1, "https://a5.mzstatic.com/art.png")

    assert sleeps == []


def test_artwork_cache_rejects_valid_image_substitution(tmp_path: Path) -> None:
    original = png_bytes(color=(1, 2, 3))
    replacement = png_bytes(color=(4, 5, 6))
    downloaded_again = png_bytes(color=(7, 8, 9))
    session = StreamingSession(
        [StreamingResponse([original]), StreamingResponse([downloaded_again])]
    )
    downloader = ArtworkDownloader(
        cache_dir=tmp_path / "cache",
        session=session,
        cdn_interval=0,
    )
    url = "https://a5.mzstatic.com/art.png"

    first = downloader.fetch(7, url)
    image_path, _ = downloader._cache_paths(7, url, None)
    image_path.write_bytes(replacement)
    second = downloader.fetch(7, url)

    assert first.data == original
    assert second.data == downloaded_again
    assert len(session.calls) == 2


def test_artwork_cache_atomic_write_does_not_follow_predictable_temp_symlink(
    tmp_path: Path,
) -> None:
    downloader = ArtworkDownloader(cache_dir=tmp_path / "cache", cdn_interval=0)
    image_path, metadata_path = downloader._cache_paths(8, "https://a5.mzstatic.com/art.png", None)
    image_path.parent.mkdir(parents=True)
    victim = tmp_path / "victim.txt"
    victim.write_text("do not overwrite", encoding="utf-8")
    planted = image_path.with_suffix(".img.tmp")
    planted.symlink_to(victim)
    artwork = decode_artwork(png_bytes(), "https://a5.mzstatic.com/art.png")

    downloader._save_cache(
        image_path,
        metadata_path,
        artwork,
        collection_id=8,
        artwork_url="https://a5.mzstatic.com/art.png",
        max_dimension=None,
    )

    assert victim.read_text(encoding="utf-8") == "do not overwrite"
    assert image_path.is_file() and not image_path.is_symlink()


@pytest.mark.parametrize("content_type", ["application/json", "text/javascript"])
def test_api_client_streams_bounded_json_and_disables_redirects(
    tmp_path: Path, content_type: str
) -> None:
    payload = json.dumps({"results": []}).encode()
    session = StreamingSession(
        [
            StreamingResponse(
                [payload],
                url="https://itunes.apple.com/search",
                headers={"Content-Type": content_type},
            )
        ]
    )
    client = AppleCatalogClient(cache_dir=tmp_path / "cache", session=session, api_interval=0)

    assert (
        client._request_results(
            apple_artwork.ITUNES_SEARCH_URL,
            {"term": "safe", "country": "US", "entity": "album"},
        )
        == []
    )
    assert session.calls[0][1]["stream"] is True
    assert session.calls[0][1]["allow_redirects"] is False


def test_api_client_rejects_untrusted_final_url(tmp_path: Path) -> None:
    payload = json.dumps({"results": []}).encode()
    session = StreamingSession(
        [
            StreamingResponse(
                [payload],
                url="http://127.0.0.1/private",
                headers={"Content-Type": "application/json"},
            )
        ]
    )
    client = AppleCatalogClient(
        cache_dir=tmp_path / "cache",
        session=session,
        api_interval=0,
        max_retries=1,
    )

    with pytest.raises((ValueError, RuntimeError), match=r"redirect|URL|HTTPS|host"):
        client._request_results(
            apple_artwork.ITUNES_SEARCH_URL,
            {"term": "safe", "country": "US", "entity": "album"},
        )


def test_discovery_rejects_audio_symlinks_outside_root(tmp_path: Path) -> None:
    root = tmp_path / "library"
    root.mkdir()
    external = tmp_path / "external.mp3"
    external.write_bytes(b"outside")
    (root / "linked.mp3").symlink_to(external)

    assert discover_audio_files(root) == []


def test_process_library_rejects_symlinked_and_filesystem_roots(tmp_path: Path) -> None:
    root = tmp_path / "library"
    root.mkdir()
    linked_root = tmp_path / "linked-library"
    linked_root.symlink_to(root, target_is_directory=True)

    with pytest.raises(ValueError, match=r"symlink"):
        process_library(linked_root, report_path=None)
    with pytest.raises(ValueError, match=r"filesystem root"):
        process_library(Path("/"), report_path=None)


def test_report_destination_cannot_clobber_selected_audio_in_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    victim = tmp_path / "song.mp3"
    original = b"precious audio bytes"
    victim.write_bytes(original)
    monkeypatch.setattr(apple_artwork, "discover_audio_files", lambda _root: [victim])

    with pytest.raises(ValueError, match=r"report.*audio|audio.*report"):
        process_library(tmp_path, report_path=victim, apply=False)

    assert victim.read_bytes() == original


def test_report_destination_must_stay_inside_root_and_not_overwrite_by_default(
    tmp_path: Path,
) -> None:
    root = tmp_path / "library"
    root.mkdir()
    outside = tmp_path / "outside.json"
    existing = root / "existing.json"
    existing.write_text("keep", encoding="utf-8")

    with pytest.raises(ValueError, match=r"inside"):
        process_library(root, report_path=Path("../outside.json"))
    with pytest.raises(FileExistsError, match=r"overwrite"):
        process_library(root, report_path=existing)

    assert not outside.exists()
    assert existing.read_text(encoding="utf-8") == "keep"
