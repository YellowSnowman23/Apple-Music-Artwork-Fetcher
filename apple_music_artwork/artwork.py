"""Artwork URL generation, downloading, decoding, validation, and caching."""

from __future__ import annotations

import hashlib
import json
import re
import struct
import time
import warnings
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit

import requests
from PIL import Image, UnidentifiedImageError

from .constants import (
    MAX_ARTWORK_BYTES,
    MAX_IMAGE_PIXELS,
    MIN_ARTWORK_DIMENSION,
)
from .filesystem import _atomic_write_bytes, _read_secure_file
from .models import Artwork, ArtworkError
from .network import (
    _close_response,
    _read_bounded_body,
    _request_with_validated_redirects,
    _retry_delay,
    _validate_remote_url,
)


def build_artwork_urls(artwork_url: str, *, max_dimension: int | None = None) -> list[str]:
    """Build direct-CDN candidates, preferring Apple's untouched uploaded master."""
    parsed = urlsplit(artwork_url)
    hostname = (parsed.hostname or "").casefold()
    if parsed.scheme != "https" or not hostname.endswith(".mzstatic.com"):
        return []
    marker = "/image/thumb/"
    if marker not in parsed.path or "/" not in parsed.path.removeprefix(marker):
        return [artwork_url]

    prefix = artwork_url.rsplit("/", 1)[0]
    transform = artwork_url.rsplit("/", 1)[-1]
    asset_path = parsed.path.split(marker, 1)[1].rsplit("/", 1)[0]
    asset_name = asset_path.rsplit("/", 1)[-1]
    source_extension = asset_name.rsplit(".", 1)[-1].casefold() if "." in asset_name else "jpg"
    if source_extension == "jpeg":
        source_extension = "jpg"
    if source_extension not in {"jpg", "png"}:
        source_extension = "jpg"

    candidates: list[str] = []
    plausible_source = bool(re.search(r"\.(?:jpe?g|png)$", asset_name, re.IGNORECASE))
    if max_dimension is None and plausible_source:
        candidates.append(f"https://a5.mzstatic.com/us/r1000/0/{asset_path}")
        if source_extension == "png":
            candidates.extend(
                [
                    f"{prefix}/1x1ss.png",
                    f"{prefix}/10000x10000-999.png",
                ]
            )
        else:
            candidates.extend(
                [
                    f"{prefix}/10000x10000-999.jpg",
                    f"{prefix}/1x1ss-100.jpg",
                ]
            )
    else:
        dimension = 10_000 if max_dimension is None else max(100, min(max_dimension, 10_000))
        candidates.append(f"{prefix}/{dimension}x{dimension}-999.{source_extension}")

    dimension = 10_000 if max_dimension is None else max(100, min(max_dimension, 10_000))
    preserved_suffix = re.sub(r"^\d+x\d+", f"{dimension}x{dimension}", transform)
    if preserved_suffix != transform:
        candidates.append(f"{prefix}/{preserved_suffix}")
    candidates.append(artwork_url)
    return list(dict.fromkeys(candidates))


def decode_artwork(data: bytes, source_url: str) -> Artwork:
    """Fully decode bounded JPEG/PNG bytes before any tag is touched."""
    if not data or len(data) > MAX_ARTWORK_BYTES:
        raise ArtworkError("artwork is not a valid JPEG or PNG")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        expected_format = "PNG"
    elif data.startswith(b"\xff\xd8\xff"):
        expected_format = "JPEG"
    else:
        raise ArtworkError("artwork is not a valid JPEG or PNG (allowlisted formats only)")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                image_format = (image.format or "").upper()
                if image_format != expected_format:
                    raise ArtworkError("artwork format does not match its JPEG/PNG signature")
                image.verify()
            with Image.open(BytesIO(data)) as image:
                if (image.format or "").upper() != expected_format:
                    raise ArtworkError("artwork format changed between validation passes")
                width, height = image.size
                if width * height > MAX_IMAGE_PIXELS:
                    raise ArtworkError(
                        f"artwork pixel count exceeds safety limit: {width}x{height}"
                    )
                image.load()
    except ArtworkError:
        raise
    except (
        Image.DecompressionBombError,
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        ValueError,
    ) as exc:
        raise ArtworkError("artwork is not a valid JPEG or PNG") from exc

    if image_format not in {"JPEG", "PNG"} or width < 1 or height < 1:
        raise ArtworkError("artwork is not a valid JPEG or PNG")
    if width < MIN_ARTWORK_DIMENSION or height < MIN_ARTWORK_DIMENSION:
        raise ArtworkError(f"artwork is too small to be a plausible cover: {width}x{height}")
    if width > 20_000 or height > 20_000:
        raise ArtworkError(f"artwork dimensions are unexpectedly large: {width}x{height}")
    return Artwork(
        data=data,
        mime="image/jpeg" if image_format == "JPEG" else "image/png",
        width=width,
        height=height,
        depth=_source_image_depth(data, image_format),
        source_url=source_url,
        sha256=hashlib.sha256(data).hexdigest(),
    )


def _source_image_depth(data: bytes, image_format: str) -> int:
    if image_format == "PNG":
        if len(data) < 26 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
            raise ArtworkError("artwork is not a valid JPEG or PNG")
        bit_depth = data[24]
        channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(data[25])
        if channels is None or bit_depth not in {1, 2, 4, 8, 16}:
            raise ArtworkError("artwork has an invalid PNG color depth")
        return bit_depth * channels

    if len(data) < 4 or data[:2] != b"\xff\xd8":
        raise ArtworkError("artwork is not a valid JPEG or PNG")
    position = 2
    sof_markers = {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    }
    while position + 4 <= len(data):
        if data[position] != 0xFF:
            position += 1
            continue
        while position < len(data) and data[position] == 0xFF:
            position += 1
        if position >= len(data):
            break
        marker = data[position]
        position += 1
        if marker in {0x01, 0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        if position + 2 > len(data):
            break
        length = struct.unpack(">H", data[position : position + 2])[0]
        if length < 2 or position + length > len(data):
            break
        payload = data[position + 2 : position + length]
        if marker in sof_markers and len(payload) >= 6:
            precision = payload[0]
            components = payload[5]
            if precision > 0 and components > 0:
                return precision * components
            break
        position += length
    raise ArtworkError("artwork has no valid JPEG frame header")


class ArtworkDownloader:
    """Fetch and validate one highest-quality image, then reuse it from disk."""

    def __init__(
        self,
        *,
        cache_dir: Path = Path(".apple-artwork-cache"),
        session: object | None = None,
        timeout: float = 30.0,
        cdn_interval: float = 0.75,
        max_retries: int = 4,
        max_response_bytes: int = MAX_ARTWORK_BYTES,
    ) -> None:
        self.cache_dir = cache_dir / "artwork"
        self.session = session or requests.Session()
        headers = getattr(self.session, "headers", None)
        if headers is not None:
            headers.update(
                {
                    "Accept": "image/png,image/jpeg",
                    "User-Agent": "AppleMusicArtworkEmbedder/2.0 (+local library tool)",
                }
            )
        self.timeout = max(1.0, float(timeout))
        self.cdn_interval = max(0.0, cdn_interval)
        self.max_retries = max(1, max_retries)
        self.max_response_bytes = max(1, min(int(max_response_bytes), MAX_ARTWORK_BYTES))
        self._last_request = 0.0

    @staticmethod
    def _cache_key(collection_id: int, artwork_url: str, max_dimension: int | None) -> str:
        variant = json.dumps(
            [collection_id, artwork_url, max_dimension],
            ensure_ascii=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(variant.encode("ascii")).hexdigest()

    def _cache_paths(
        self, collection_id: int, artwork_url: str, max_dimension: int | None
    ) -> tuple[Path, Path]:
        key = self._cache_key(collection_id, artwork_url, max_dimension)[:20]
        base = self.cache_dir / f"{collection_id}-{key}"
        return base.with_suffix(".img"), base.with_suffix(".json")

    def _load_cache(
        self,
        image_path: Path,
        metadata_path: Path,
        *,
        collection_id: int,
        artwork_url: str,
        max_dimension: int | None,
    ) -> Artwork | None:
        try:
            metadata_bytes = _read_secure_file(metadata_path, 64 * 1024)
            metadata = json.loads(metadata_bytes.decode("utf-8"))
            if not isinstance(metadata, dict):
                return None
            expected_key = self._cache_key(collection_id, artwork_url, max_dimension)
            if (
                metadata.get("schema_version") != 2
                or metadata.get("cache_key") != expected_key
                or metadata.get("collection_id") != collection_id
                or metadata.get("artwork_url") != artwork_url
                or metadata.get("max_dimension") != max_dimension
            ):
                return None
            data = _read_secure_file(image_path, self.max_response_bytes)
            expected_hash = metadata.get("sha256")
            if not isinstance(expected_hash, str) or not re.fullmatch(
                r"[0-9a-f]{64}", expected_hash
            ):
                return None
            if hashlib.sha256(data).hexdigest() != expected_hash:
                return None
            source_url = str(metadata["source_url"])
            _validate_remote_url(source_url, api=False)
            artwork = decode_artwork(data, source_url)
            expected = {
                "sha256": artwork.sha256,
                "width": artwork.width,
                "height": artwork.height,
                "depth": artwork.depth,
                "mime": artwork.mime,
            }
            if any(metadata.get(name) != value for name, value in expected.items()):
                return None
            return artwork
        except (OSError, KeyError, TypeError, ValueError, UnicodeError, ArtworkError):
            return None

    def _save_cache(
        self,
        image_path: Path,
        metadata_path: Path,
        artwork: Artwork,
        *,
        collection_id: int,
        artwork_url: str,
        max_dimension: int | None,
    ) -> None:
        _atomic_write_bytes(image_path, artwork.data)
        metadata = json.dumps(
            {
                "schema_version": 2,
                "cache_key": self._cache_key(collection_id, artwork_url, max_dimension),
                "collection_id": collection_id,
                "artwork_url": artwork_url,
                "max_dimension": max_dimension,
                "source_url": artwork.source_url,
                "sha256": artwork.sha256,
                "width": artwork.width,
                "height": artwork.height,
                "depth": artwork.depth,
                "mime": artwork.mime,
            },
            ensure_ascii=True,
            sort_keys=True,
        ).encode("ascii")
        _atomic_write_bytes(metadata_path, metadata)

    def _pace(self) -> None:
        remaining = self.cdn_interval - (time.monotonic() - self._last_request)
        if remaining > 0:
            time.sleep(remaining)

    def fetch(
        self,
        collection_id: int,
        artwork_url: str,
        *,
        max_dimension: int | None = None,
        refresh: bool = False,
    ) -> Artwork:
        image_path, metadata_path = self._cache_paths(collection_id, artwork_url, max_dimension)
        if not refresh:
            cached = self._load_cache(
                image_path,
                metadata_path,
                collection_id=collection_id,
                artwork_url=artwork_url,
                max_dimension=max_dimension,
            )
            if cached is not None:
                return cached

        candidate_urls = build_artwork_urls(artwork_url, max_dimension=max_dimension)
        if not candidate_urls:
            raise ArtworkError("Apple returned an invalid artwork URL")
        failures: list[str] = []
        for candidate_url in candidate_urls:
            for attempt in range(self.max_retries):
                self._pace()
                response: object | None = None
                try:
                    response, final_url = _request_with_validated_redirects(
                        self.session,
                        candidate_url,
                        timeout=self.timeout,
                        api=False,
                    )
                    self._last_request = time.monotonic()
                    status = int(getattr(response, "status_code", 0))
                    if status == 200:
                        content_type = (
                            str(getattr(response, "headers", {}).get("Content-Type") or "")
                            .split(";", 1)[0]
                            .strip()
                            .casefold()
                        )
                        if content_type not in {"image/jpeg", "image/png"}:
                            raise ArtworkError(
                                f"Apple CDN returned unsupported Content-Type {content_type!r}"
                            )
                        data = _read_bounded_body(
                            response,
                            maximum=self.max_response_bytes,
                            timeout=self.timeout,
                        )
                        try:
                            artwork = decode_artwork(data, final_url)
                        except ArtworkError as exc:
                            failures.append(f"{candidate_url}: {exc}")
                            break
                        self._save_cache(
                            image_path,
                            metadata_path,
                            artwork,
                            collection_id=collection_id,
                            artwork_url=artwork_url,
                            max_dimension=max_dimension,
                        )
                        return artwork
                    if status in {400, 404, 410}:
                        failures.append(f"{candidate_url}: HTTP {status}")
                        break
                    if status not in {403, 429} and status < 500:
                        response.raise_for_status()  # type: ignore[attr-defined]
                    failures.append(f"{candidate_url}: HTTP {status}")
                    if attempt + 1 < self.max_retries:
                        retry_after = getattr(response, "headers", {}).get("Retry-After")
                        time.sleep(_retry_delay(retry_after, attempt))
                except (ValueError, ArtworkError) as exc:
                    failures.append(f"{candidate_url}: {exc}")
                    break
                except (requests.RequestException, OSError, TimeoutError) as exc:
                    failures.append(f"{candidate_url}: {exc}")
                    if attempt + 1 < self.max_retries:
                        time.sleep(_retry_delay(None, attempt))
                finally:
                    if response is not None:
                        _close_response(response)
        detail = failures[-1] if failures else "no CDN candidate succeeded"
        raise ArtworkError(f"unable to download valid Apple artwork: {detail}")


__all__ = (
    "ArtworkDownloader",
    "build_artwork_urls",
    "decode_artwork",
)
