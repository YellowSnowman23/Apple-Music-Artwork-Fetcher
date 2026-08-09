#!/usr/bin/env python3
"""Accuracy-first Apple Music artwork matching and embedding."""

from __future__ import annotations

import argparse
import base64
import ctypes
import errno
import fnmatch
import hashlib
import json
import math
import os
import random
import re
import secrets
import stat
import struct
import sys
import time
import unicodedata
import uuid
import warnings
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from io import BytesIO
from pathlib import Path
from typing import Any, BinaryIO, cast
from urllib.parse import urljoin, urlsplit

import mutagen
import requests
from mutagen._util import FileThing
from mutagen.aiff import AIFF
from mutagen.apev2 import APEBinaryValue, APENoHeaderError, APEv2
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, PictureType
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis
from mutagen.wave import WAVE
from mutagen.wavpack import WavPack
from PIL import Image, UnidentifiedImageError

ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
ITUNES_LOOKUP_URL = "https://itunes.apple.com/lookup"
VERSION = "2.0.3"

MAX_ARTWORK_BYTES = 128 * 1024 * 1024
MAX_API_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 64_000_000
MIN_ARTWORK_DIMENSION = 32
MAX_REDIRECTS = 5
MAX_RETRY_DELAY = 30.0
MAX_TAG_TEXT = 4096


AUDIO_EXTENSIONS = frozenset(
    {
        ".aif",
        ".aiff",
        ".flac",
        ".m4a",
        ".mp3",
        ".mp4",
        ".oga",
        ".ogg",
        ".opus",
        ".wav",
        ".wave",
        ".wv",
    }
)


@dataclass(frozen=True, slots=True)
class TrackMetadata:
    """Catalog-matching fields plus the filesystem identity observed during the scan."""

    path: Path
    title: str
    artist: str
    album: str
    album_artist: str
    year: int | None = None
    track_number: int | None = None
    track_total: int | None = None
    disc_number: int | None = None
    disc_total: int | None = None
    duration_ms: int | None = None
    barcode: str | None = None
    musicbrainz_release_id: str | None = None
    source_identity: tuple[int, int, int, int, int] | None = None


@dataclass(frozen=True, slots=True)
class AlbumGroup:
    album: str
    album_artist: str
    year: int | None
    files: tuple[Path, ...]
    logical_tracks: tuple[TrackMetadata, ...]
    barcode: str | None = None
    musicbrainz_release_id: str | None = None
    source_identities: tuple[tuple[Path, tuple[int, int, int, int, int]], ...] = ()


@dataclass(frozen=True, slots=True)
class CatalogTrack:
    title: str
    artist: str
    duration_ms: int | None
    disc_number: int | None
    track_number: int | None


@dataclass(frozen=True, slots=True)
class CatalogAlbum:
    collection_id: int
    album: str
    artist: str
    release_year: int | None
    artwork_url: str
    track_count: int | None
    tracks: tuple[CatalogTrack, ...]
    verified_barcode: str | None = None


@dataclass(frozen=True, slots=True)
class CandidateScore:
    candidate: CatalogAlbum
    total: float
    eligible: bool
    reasons: tuple[str, ...]
    components: Mapping[str, float]


@dataclass(frozen=True, slots=True)
class MatchDecision:
    status: str
    match: CandidateScore | None
    scores: tuple[CandidateScore, ...]
    reason: str


@dataclass(frozen=True, slots=True)
class Artwork:
    data: bytes
    mime: str
    width: int
    height: int
    depth: int
    source_url: str
    sha256: str


class ArtworkError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class EmbedResult:
    status: str
    format: str
    message: str


class EmbedError(RuntimeError):
    pass


class EmbedCommittedInterrupt(KeyboardInterrupt):
    """An interrupt arrived after the replacement became irreversible."""

    committed = True

    def __init__(self, message: str, result: EmbedResult, path: Path) -> None:
        super().__init__(message)
        self.result = result
        self.path = path


class EmbedCommittedError(EmbedError):
    """The replacement occurred, but post-commit durability/verification was uncertain."""

    committed = True

    def __init__(self, message: str, result: EmbedResult) -> None:
        super().__init__(message)
        self.result = result


def _is_allowed_https_url(url: str, *, api: bool) -> bool:
    """Allow only HTTPS Apple API or mzstatic CDN destinations, including redirects."""
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    if parsed.scheme.casefold() != "https" or parsed.username or parsed.password:
        return False
    try:
        if parsed.port not in {None, 443}:
            return False
    except ValueError:
        return False
    if api:
        return hostname == "itunes.apple.com"
    return hostname == "mzstatic.com" or hostname.endswith(".mzstatic.com")


def _validate_remote_url(url: str, *, api: bool) -> None:
    if not _is_allowed_https_url(url, api=api):
        destination = "Apple API" if api else "Apple mzstatic CDN"
        raise ValueError(f"redirect or URL is not an allowlisted HTTPS {destination} destination")


def _close_response(response: object) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        close()


def _request_with_validated_redirects(
    session: object,
    url: str,
    *,
    timeout: float,
    api: bool,
    params: Mapping[str, object] | None = None,
) -> tuple[object, str]:
    """Follow a small redirect chain while validating every requested and reported URL."""
    current = url
    current_params = dict(params) if params is not None else None
    for redirect_count in range(MAX_REDIRECTS + 1):
        _validate_remote_url(current, api=api)
        kwargs: dict[str, object] = {
            "timeout": timeout,
            "allow_redirects": False,
            "stream": True,
        }
        if current_params is not None:
            kwargs["params"] = current_params
        response = session.get(current, **kwargs)  # type: ignore[attr-defined]
        reported_url = str(getattr(response, "url", current) or current)
        try:
            _validate_remote_url(reported_url, api=api)
        except ValueError:
            _close_response(response)
            raise
        status = int(getattr(response, "status_code", 0))
        if status not in {301, 302, 303, 307, 308}:
            return response, reported_url
        location = str(getattr(response, "headers", {}).get("Location") or "")
        _close_response(response)
        if not location:
            raise ValueError("Apple redirect response omitted Location")
        if redirect_count >= MAX_REDIRECTS:
            raise ValueError("Apple redirect limit exceeded")
        current = urljoin(reported_url, location)
        _validate_remote_url(current, api=api)
        current_params = None
    raise ValueError("Apple redirect limit exceeded")


def _read_bounded_body(response: object, *, maximum: int, timeout: float) -> bytes:
    headers = getattr(response, "headers", {})
    content_length = headers.get("Content-Length") if isinstance(headers, Mapping) else None
    if content_length is not None:
        try:
            declared = int(content_length)
        except (TypeError, ValueError) as exc:
            raise ValueError("response has an invalid Content-Length") from exc
        if declared < 0 or declared > maximum:
            raise ValueError(f"response body exceeds the {maximum}-byte limit")
    iterator = getattr(response, "iter_content", None)
    if not callable(iterator):
        raise ValueError("HTTP client did not provide a streaming response")
    deadline = time.monotonic() + max(1.0, float(timeout))
    chunks: list[bytes] = []
    total = 0
    for chunk in iterator(chunk_size=65_536):
        if time.monotonic() > deadline:
            raise TimeoutError("response transfer exceeded its total deadline")
        if not chunk:
            continue
        if not isinstance(chunk, bytes):
            chunk = bytes(chunk)
        total += len(chunk)
        if total > maximum:
            raise ValueError(f"response body exceeds the {maximum}-byte limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _retry_delay(value: object, attempt: int) -> float:
    fallback = min(MAX_RETRY_DELAY, 1.8**attempt)
    if value is None:
        base = fallback
    else:
        text = str(value).strip()
        try:
            base = float(text)
            if not math.isfinite(base):
                raise ValueError
        except ValueError:
            try:
                parsed = parsedate_to_datetime(text)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                base = (parsed - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                base = fallback
    base = min(MAX_RETRY_DELAY, max(0.0, base))
    return min(MAX_RETRY_DELAY, base + random.uniform(0.0, 0.25))


def _open_secure_directory(
    path: Path,
    *,
    create: bool,
    private: bool,
    require_owner: bool = True,
) -> int:
    """Open a directory component-by-component without following symlinks."""
    absolute = Path(os.path.abspath(os.fspath(path.expanduser())))
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    directory_flags |= getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(absolute.anchor, directory_flags)
    try:
        for part in absolute.parts[1:]:
            try:
                child = os.open(part, directory_flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                with suppress(FileExistsError):
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                child = os.open(part, directory_flags, dir_fd=descriptor)
            except OSError as exc:
                raise OSError(
                    exc.errno,
                    f"directory path contains a symlink or unsafe component: {absolute}",
                ) from exc
            os.close(descriptor)
            descriptor = child
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            raise OSError(f"path is not a real directory: {absolute}")
        if require_owner and hasattr(os, "geteuid") and info.st_uid != os.geteuid():
            raise OSError(f"directory is not owned by the current user: {absolute}")
        if private and info.st_mode & 0o077:
            os.fchmod(descriptor, stat.S_IMODE(info.st_mode) & ~0o077)
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _open_regular_source(
    path: Path,
    expected: tuple[int, int, int, int, int] | None = None,
) -> tuple[int, int, os.stat_result]:
    """Open a regular source through a no-follow parent walk and bind it to an inode."""
    absolute = Path(os.path.abspath(os.fspath(path.expanduser())))
    parent = _open_secure_directory(
        absolute.parent,
        create=False,
        private=False,
        require_owner=False,
    )
    descriptor = -1
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        noatime = getattr(os, "O_NOATIME", 0)
        try:
            descriptor = os.open(absolute.name, flags | noatime, dir_fd=parent)
        except PermissionError:
            descriptor = os.open(absolute.name, flags, dir_fd=parent)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise EmbedError(f"audio source is not a single-link regular file: {path}")
        if expected is not None and _stat_identity(info) != expected:
            raise EmbedError(f"audio source changed after metadata scanning: {path}")
        return parent, descriptor, info
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent)
        raise


@contextmanager
def _binary_source(source: Path | int, mode: str = "rb") -> Iterator[BinaryIO]:
    handle = os.fdopen(os.dup(source), mode) if isinstance(source, int) else source.open(mode)
    handle.seek(0)
    try:
        yield cast(BinaryIO, handle)
    finally:
        handle.close()


def _source_stat(source: Path | int) -> os.stat_result:
    return os.fstat(source) if isinstance(source, int) else source.stat()


def _secure_cache_directory(path: Path) -> None:
    descriptor = _open_secure_directory(path, create=True, private=True)
    os.close(descriptor)


def _read_secure_file(path: Path, maximum: int) -> bytes:
    directory = _open_secure_directory(path.parent, create=False, private=True)
    descriptor = -1
    try:
        try:
            info = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        except OSError as exc:
            raise OSError(f"cannot inspect secure cache entry {path}: {exc}") from exc
        if not stat.S_ISREG(info.st_mode):
            raise OSError(f"cache entry is not a regular file: {path}")
        if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
            raise OSError(f"cache entry is not owned by the current user: {path}")
        if info.st_mode & 0o022:
            raise OSError(f"cache entry is writable by another user: {path}")
        if info.st_size < 0 or info.st_size > maximum:
            raise OSError(f"cache entry exceeds the {maximum}-byte limit: {path}")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path.name, flags, dir_fd=directory)
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise OSError(f"cache entry changed while opening: {path}")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65_536, maximum + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > maximum:
                raise OSError(f"cache entry exceeds the {maximum}-byte limit: {path}")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(directory)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_bytes(
    path: Path,
    data: bytes,
    *,
    overwrite: bool = True,
    private_directory: bool = True,
) -> None:
    directory = _open_secure_directory(
        path.parent,
        create=True,
        private=private_directory,
    )
    temporary_name = f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor = -1
    temporary_exists = False
    try:
        try:
            existing = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if not stat.S_ISREG(existing.st_mode):
                raise OSError(f"destination is not a regular file: {path}")
            if hasattr(os, "geteuid") and existing.st_uid != os.geteuid():
                raise OSError(f"destination is not owned by the current user: {path}")
            if not overwrite:
                raise FileExistsError(path)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary_name, flags, 0o600, dir_fd=directory)
        temporary_exists = True
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if overwrite:
            os.replace(
                temporary_name,
                path.name,
                src_dir_fd=directory,
                dst_dir_fd=directory,
            )
            temporary_exists = False
        else:
            try:
                os.link(
                    temporary_name,
                    path.name,
                    src_dir_fd=directory,
                    dst_dir_fd=directory,
                    follow_symlinks=False,
                )
            except FileExistsError as exc:
                raise FileExistsError(
                    f"destination appeared concurrently; refusing to overwrite: {path}"
                ) from exc
            os.unlink(temporary_name, dir_fd=directory)
            temporary_exists = False
        os.fsync(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_exists:
            with suppress(FileNotFoundError):
                os.unlink(temporary_name, dir_fd=directory)
        os.close(directory)


def normalize_text(value: str) -> str:
    """Normalize catalog text without throwing away meaningful words."""
    value = unicodedata.normalize("NFKD", value).casefold()
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = value.replace("&", " and ")
    value = re.sub(r"[^\w]+", " ", value, flags=re.UNICODE)
    return " ".join(value.split())


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


def _flac_picture_from_artwork(artwork: Artwork) -> Picture:
    picture = Picture()
    picture.type = PictureType.COVER_FRONT
    picture.mime = artwork.mime
    picture.desc = "Front cover"
    picture.width = artwork.width
    picture.height = artwork.height
    picture.depth = artwork.depth
    picture.data = artwork.data
    return picture


def _parse_xiph_pictures(values: Iterable[str]) -> list[tuple[str, Picture | None]]:
    parsed: list[tuple[str, Picture | None]] = []
    for value in values:
        try:
            parsed.append((value, Picture(base64.b64decode(value, validate=True))))
        except (ValueError, TypeError, mutagen.MutagenError):
            parsed.append((value, None))
    return parsed


def _read_id3v1(path: Path | int) -> bytes | None:
    try:
        if _source_stat(path).st_size < 128:
            return None
        with _binary_source(path) as handle:
            handle.seek(-128, 2)
            value = handle.read(128)
    except OSError as exc:
        raise EmbedError(f"failed to inspect ID3v1 data in {path}: {exc}") from exc
    return value if value.startswith(b"TAG") else None


def _id3v2_major(path: Path | int) -> int | None:
    try:
        with _binary_source(path) as handle:
            header = handle.read(4)
    except OSError as exc:
        raise EmbedError(f"failed to inspect ID3v2 data in {path}: {exc}") from exc
    return header[3] if len(header) == 4 and header[:3] == b"ID3" else None


def _restore_id3v1(path: Path | int, original: bytes | None) -> None:
    if original is None:
        return
    try:
        with _binary_source(path, "r+b") as handle:
            handle.seek(0, os.SEEK_END)
            handle.write(original)
            handle.flush()
    except OSError as exc:
        raise EmbedError(f"failed to restore ID3v1 data in {path}: {exc}") from exc


def _wavpack_tail_kind(path: Path | int) -> str | None:
    if _read_id3v1(path) is not None:
        return "ID3v1"
    marker = b"APETAGEX"
    marker_offsets: list[int] = []
    try:
        size = _source_stat(path).st_size
        with _binary_source(path) as handle:
            overlap = b""
            offset = 0
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                searchable = overlap + chunk
                base = offset - len(overlap)
                cursor = 0
                while True:
                    found = searchable.find(marker, cursor)
                    if found < 0:
                        break
                    marker_offsets.append(base + found)
                    cursor = found + 1
                overlap = searchable[-(len(marker) - 1) :]
                offset += len(chunk)

            allowed_ape_offsets: set[int] = set()
            if size >= 32:
                handle.seek(size - 32)
                footer = handle.read(32)
                if footer.startswith(marker):
                    tag_size = int.from_bytes(footer[12:16], "little")
                    if tag_size < 32 or tag_size > size:
                        return "malformed terminal APEv2"
                    footer_offset = size - 32
                    tag_start = size - tag_size
                    allowed_ape_offsets.add(footer_offset)
                    for possible_header in (tag_start, tag_start - 32):
                        if possible_header < 0:
                            continue
                        handle.seek(possible_header)
                        if handle.read(len(marker)) == marker:
                            allowed_ape_offsets.add(possible_header)

            if marker_offsets and (
                not allowed_ape_offsets
                or any(offset not in allowed_ape_offsets for offset in marker_offsets)
            ):
                return "non-terminal or duplicate APEv2"

            handle.seek(max(0, size - 65_536))
            tail = handle.read(65_536)
    except OSError as exc:
        raise EmbedError(f"failed to inspect WavPack tail data in {path}: {exc}") from exc
    if any(marker in tail for marker in (b"LYRICSBEGIN", b"LYRICSEND", b"LYRICS200")):
        return "Lyrics3"
    return None


def _mp4_box_header(handle: object, offset: int, boundary: int) -> tuple[bytes, int, int] | None:
    if offset < 0 or offset + 8 > boundary:
        return None
    handle.seek(offset)  # type: ignore[attr-defined]
    header = handle.read(8)  # type: ignore[attr-defined]
    if len(header) != 8:
        return None
    size, box_type = struct.unpack(">I4s", header)
    header_size = 8
    if size == 1:
        extended = handle.read(8)  # type: ignore[attr-defined]
        if len(extended) != 8:
            return None
        size = struct.unpack(">Q", extended)[0]
        header_size = 16
    elif size == 0:
        size = boundary - offset
    if size < header_size or offset + size > boundary:
        return None
    return box_type, offset + header_size, offset + size


def _mp4_children(handle: object, start: int, end: int) -> list[tuple[bytes, int, int]]:
    children: list[tuple[bytes, int, int]] = []
    offset = start
    while offset < end:
        box = _mp4_box_header(handle, offset, end)
        if box is None:
            raise EmbedError("malformed MP4 atom layout")
        children.append(box)
        _box_type, _payload_start, box_end = box
        if box_end <= offset:
            raise EmbedError("malformed MP4 atom size")
        offset = box_end
    return children


def _validate_m4a_container(path: Path | int, *, suffix: str | None = None) -> None:
    actual_suffix = suffix if suffix is not None else path.suffix if isinstance(path, Path) else ""
    if actual_suffix.casefold() not in {".m4a", ".mp4"}:
        raise EmbedError(f"only validated audio-only .m4a/.mp4 containers are accepted: {path}")
    try:
        file_size = _source_stat(path).st_size
        with _binary_source(path) as handle:
            top_level = _mp4_children(handle, 0, file_size)
            if any(box_type == b"moof" for box_type, _, _ in top_level):
                raise EmbedError(f"fragmented MP4/M4A is unsupported: {path}")
            moov_boxes = [(start, end) for box_type, start, end in top_level if box_type == b"moov"]
            if len(moov_boxes) != 1:
                raise EmbedError(f"MP4/M4A must contain exactly one moov atom: {path}")
            moov_start, moov_end = moov_boxes[0]
            if moov_end - moov_start > 64 * 1024 * 1024:
                raise EmbedError(f"MP4/M4A metadata atom is unexpectedly large: {path}")
            handle.seek(moov_start)
            moov_payload = handle.read(moov_end - moov_start)
            if any(
                marker in moov_payload for marker in (b"drms", b"encv", b"enca", b"sinf", b"mvex")
            ):
                raise EmbedError(f"encrypted or fragmented MP4/M4A is unsupported: {path}")
            handlers: list[bytes] = []
            for box_type, trak_start, trak_end in _mp4_children(handle, moov_start, moov_end):
                if box_type != b"trak":
                    continue
                mdia_boxes = [
                    (start, end)
                    for child_type, start, end in _mp4_children(handle, trak_start, trak_end)
                    if child_type == b"mdia"
                ]
                if len(mdia_boxes) != 1:
                    raise EmbedError(f"MP4/M4A track has no unique media atom: {path}")
                mdia_start, mdia_end = mdia_boxes[0]
                hdlr_boxes = [
                    (start, end)
                    for child_type, start, end in _mp4_children(handle, mdia_start, mdia_end)
                    if child_type == b"hdlr"
                ]
                if len(hdlr_boxes) != 1 or hdlr_boxes[0][0] + 12 > hdlr_boxes[0][1]:
                    raise EmbedError(f"MP4/M4A track has no valid handler: {path}")
                handle.seek(hdlr_boxes[0][0] + 8)
                handlers.append(handle.read(4))
    except EmbedError:
        raise
    except OSError as exc:
        raise EmbedError(f"failed to inspect MP4/M4A container {path}: {exc}") from exc
    if handlers != [b"soun"]:
        raise EmbedError(
            f"MP4/M4A must contain exactly one audio track and no video tracks: {path}"
        )


def _has_leading_id3(path: Path | int) -> bool:
    try:
        with _binary_source(path) as handle:
            return handle.read(3) == b"ID3"
    except OSError as exc:
        raise EmbedError(f"failed to inspect leading metadata in {path}: {exc}") from exc


def _apev2_tags(path: Path | int) -> APEv2 | None:
    try:
        if isinstance(path, int):
            with _binary_source(path) as handle:
                return APEv2(fileobj=handle)
        return APEv2(path)
    except APENoHeaderError:
        return None
    except (OSError, mutagen.MutagenError) as exc:
        raise EmbedError(f"failed to inspect APEv2 metadata in {path}: {exc}") from exc


def _front_pictures_for_audio(audio: object, path: Path | int) -> tuple[str, list[bytes]]:
    if isinstance(audio, (OggOpus, OggVorbis)):
        assert audio.tags is not None
        parsed = _parse_xiph_pictures(audio.tags.get("metadata_block_picture", []))
        if any(picture is None for _, picture in parsed):
            raise EmbedError("malformed METADATA_BLOCK_PICTURE; refusing to modify")
        if audio.tags.get("coverart") or audio.tags.get("coverartmime"):
            raise EmbedError("legacy COVERART fields have no picture role; refusing to modify")
        return "Xiph", [
            picture.data
            for _, picture in parsed
            if picture is not None and picture.type == PictureType.COVER_FRONT
        ]
    if isinstance(audio, MP4):
        return "MP4", [bytes(cover) for cover in (audio.tags or {}).get("covr", [])]
    if isinstance(audio, (MP3, WAVE, AIFF)):
        format_name = (
            "MP3" if isinstance(audio, MP3) else "AIFF" if isinstance(audio, AIFF) else "WAVE"
        )
        if isinstance(audio, MP3):
            ape_tags = _apev2_tags(path)
            if ape_tags is not None and any(
                str(key).casefold() == "cover art (front)" for key in ape_tags
            ):
                raise EmbedError(
                    "mixed MP3 ID3/APEv2 front artwork is unsupported; refusing to modify"
                )
        if audio.tags is not None and audio.tags.version[1] == 2:
            raise EmbedError("ID3v2.2 artwork updates are unsupported")
        return format_name, [
            picture.data
            for picture in (audio.tags.getall("APIC") if audio.tags is not None else [])
            if picture.type == PictureType.COVER_FRONT
        ]
    if isinstance(audio, WavPack):
        existing = audio.tags.get("Cover Art (Front)") if audio.tags is not None else None
        payload = bytes(existing) if existing is not None else None
        if payload is not None and b"\0" not in payload:
            raise EmbedError("malformed WavPack front-cover field; refusing to modify")
        return "WavPack", [payload.split(b"\0", 1)[1]] if payload is not None else []
    if isinstance(audio, FLAC):
        if _has_leading_id3(path):
            raise EmbedError("mixed FLAC/leading-ID3 metadata is unsupported; refusing to modify")
        competing = {
            str(key).casefold()
            for key in (audio.tags.keys() if audio.tags is not None else [])
            if str(key).casefold() in {"metadata_block_picture", "coverart", "coverartmime"}
        }
        if competing:
            names = ", ".join(sorted(competing))
            raise EmbedError(
                f"competing FLAC picture metadata store(s) present ({names}); refusing to modify"
            )
        return "FLAC", [
            picture.data for picture in audio.pictures if picture.type == PictureType.COVER_FRONT
        ]
    raise EmbedError(f"artwork embedding is not implemented for {type(audio).__name__}")


def _load_mutagen(
    source: Path | int,
    *,
    easy: bool = False,
    filename: Path | None = None,
) -> Any:
    with _binary_source(source) as handle:
        filething: BinaryIO | FileThing = handle
        if filename is not None:
            filething = FileThing(handle, str(filename), str(filename))
        return mutagen.File(filething, easy=easy)


def _load_mutagen_class(source: Path | int, audio_type: Any) -> Any:
    with _binary_source(source) as handle:
        return audio_type(handle)


def _save_mutagen(audio: Any, source: Path | int, **kwargs: object) -> None:
    save = audio.save
    with _binary_source(source, "r+b") as handle:
        save(handle, **kwargs)
        handle.flush()


def _preflight_opened(
    source: Path | int,
    display_path: Path,
    artwork: Artwork | None,
    *,
    replace_existing: bool,
) -> EmbedResult:
    try:
        audio = _load_mutagen(source, filename=display_path)
    except (OSError, mutagen.MutagenError) as exc:
        raise EmbedError(f"cannot open {display_path}: {exc}") from exc
    if audio is None:
        raise EmbedError(f"unsupported or unreadable audio file: {display_path}")
    if isinstance(audio, MP4):
        _validate_m4a_container(source, suffix=display_path.suffix)
    if isinstance(audio, WavPack):
        tail_kind = _wavpack_tail_kind(source)
        if tail_kind:
            raise EmbedError(
                f"unsupported {tail_kind} WavPack tail; refusing to modify: {display_path}"
            )
    format_name, front_pictures = _front_pictures_for_audio(audio, source)
    if artwork is not None and len(front_pictures) == 1 and front_pictures[0] == artwork.data:
        return EmbedResult("unchanged", format_name, "identical front cover already embedded")
    if front_pictures and not replace_existing:
        return EmbedResult("skipped", format_name, "front cover already exists")
    return EmbedResult("ready", format_name, "container and metadata stores passed preflight")


def preflight_artwork(
    path: Path,
    artwork: Artwork | None = None,
    *,
    replace_existing: bool = False,
    expected_identity: tuple[int, int, int, int, int] | None = None,
) -> EmbedResult:
    """Inspect one target through a bound descriptor without writing it."""
    path = Path(path)
    parent_descriptor = -1
    source_descriptor = -1
    try:
        parent_descriptor, source_descriptor, _info = _open_regular_source(
            path,
            expected_identity,
        )
        return _preflight_opened(
            source_descriptor,
            path,
            artwork,
            replace_existing=replace_existing,
        )
    except EmbedError:
        raise
    except OSError as exc:
        raise EmbedError(f"cannot inspect {path}: {exc}") from exc
    finally:
        if source_descriptor >= 0:
            os.close(source_descriptor)
        if parent_descriptor >= 0:
            os.close(parent_descriptor)


def _stable_value(value: object) -> object:
    if isinstance(value, bytes):
        return ("bytes", len(value), hashlib.sha256(value).hexdigest())
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    if isinstance(value, Mapping):
        return tuple(sorted((str(key), _stable_value(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_stable_value(item) for item in value)
    try:
        payload = bytes(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        pprint = getattr(value, "pprint", None)
        return (type(value).__name__, pprint() if callable(pprint) else str(value))
    return (type(value).__name__, len(payload), hashlib.sha256(payload).hexdigest())


def _hash_file_region(path: Path | int, start: int, end: int) -> tuple[int, str]:
    if start < 0 or end < start:
        raise EmbedError(f"invalid encoded-audio region in {path}")
    digest = hashlib.sha256()
    remaining = end - start
    try:
        with _binary_source(path) as handle:
            handle.seek(start)
            while remaining:
                chunk = handle.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise EmbedError(f"encoded-audio payload is truncated in {path}")
                digest.update(chunk)
                remaining -= len(chunk)
    except OSError as exc:
        raise EmbedError(f"failed to hash encoded-audio payload in {path}: {exc}") from exc
    return end - start, digest.hexdigest()


def _mp3_payload_digest(path: Path | int) -> tuple[int, str]:
    try:
        size = _source_stat(path).st_size
        with _binary_source(path) as handle:
            header = handle.read(10)
            start = 0
            if len(header) == 10 and header[:3] == b"ID3":
                if any(byte & 0x80 for byte in header[6:10]):
                    raise EmbedError(f"invalid ID3 size while hashing MP3 payload: {path}")
                tag_size = (header[6] << 21) | (header[7] << 14) | (header[8] << 7) | header[9]
                start = 10 + tag_size + (10 if header[5] & 0x10 else 0)
            end = size
            if end >= 128:
                handle.seek(end - 128)
                if handle.read(3) == b"TAG":
                    end -= 128
            if end >= 32:
                handle.seek(end - 32)
                footer = handle.read(32)
                if footer[:8] == b"APETAGEX":
                    ape_size = int.from_bytes(footer[12:16], "little")
                    if ape_size < 32 or ape_size > end - start:
                        raise EmbedError(f"invalid APEv2 size while hashing MP3 payload: {path}")
                    end -= ape_size
                    if end >= 32:
                        handle.seek(end - 32)
                        if handle.read(8) == b"APETAGEX":
                            end -= 32
    except OSError as exc:
        raise EmbedError(f"failed to locate encoded MP3 payload in {path}: {exc}") from exc
    return _hash_file_region(path, start, end)


def _flac_payload_digest(path: Path | int) -> tuple[int, str]:
    try:
        size = _source_stat(path).st_size
        with _binary_source(path) as handle:
            if handle.read(4) != b"fLaC":
                raise EmbedError(f"invalid FLAC marker while hashing payload: {path}")
            offset = 4
            while True:
                header = handle.read(4)
                if len(header) != 4:
                    raise EmbedError(f"truncated FLAC metadata while hashing payload: {path}")
                is_last = bool(header[0] & 0x80)
                block_size = int.from_bytes(header[1:4], "big")
                offset += 4 + block_size
                if offset > size:
                    raise EmbedError(f"invalid FLAC metadata size while hashing payload: {path}")
                handle.seek(block_size, os.SEEK_CUR)
                if is_last:
                    break
    except OSError as exc:
        raise EmbedError(f"failed to locate FLAC payload in {path}: {exc}") from exc
    return _hash_file_region(path, offset, size)


def _hash_regions(path: Path | int, regions: list[tuple[int, int]]) -> tuple[int, str]:
    if not regions:
        raise EmbedError(f"encoded-audio container has no payload regions: {path}")
    digest = hashlib.sha256()
    total = 0
    try:
        with _binary_source(path) as handle:
            for start, end in regions:
                if start < 0 or end < start:
                    raise EmbedError(f"invalid encoded-audio region in {path}")
                length = end - start
                digest.update(length.to_bytes(8, "big"))
                handle.seek(start)
                remaining = length
                while remaining:
                    chunk = handle.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise EmbedError(f"encoded-audio payload is truncated in {path}")
                    digest.update(chunk)
                    total += len(chunk)
                    remaining -= len(chunk)
    except OSError as exc:
        raise EmbedError(f"failed to hash encoded-audio payload in {path}: {exc}") from exc
    return total, digest.hexdigest()


def _mp4_payload_digest(path: Path | int) -> tuple[int, str]:
    try:
        size = _source_stat(path).st_size
        with _binary_source(path) as handle:
            regions = [
                (start, end)
                for box_type, start, end in _mp4_children(handle, 0, size)
                if box_type == b"mdat"
            ]
    except OSError as exc:
        raise EmbedError(f"failed to locate MP4 media payload in {path}: {exc}") from exc
    return _hash_regions(path, regions)


def _riff_payload_digest(path: Path | int) -> tuple[int, str]:
    regions: list[tuple[int, int]] = []
    try:
        size = _source_stat(path).st_size
        with _binary_source(path) as handle:
            header = handle.read(12)
            if len(header) != 12 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
                raise EmbedError(f"unsupported RIFF/WAVE structure while hashing payload: {path}")
            offset = 12
            while offset + 8 <= size:
                handle.seek(offset)
                chunk_header = handle.read(8)
                chunk_size = int.from_bytes(chunk_header[4:8], "little")
                payload_start = offset + 8
                payload_end = payload_start + chunk_size
                if payload_end > size:
                    raise EmbedError(f"truncated RIFF/WAVE chunk while hashing payload: {path}")
                if chunk_header[:4] == b"data":
                    regions.append((payload_start, payload_end))
                offset = payload_end + (chunk_size & 1)
    except OSError as exc:
        raise EmbedError(f"failed to locate RIFF/WAVE payload in {path}: {exc}") from exc
    return _hash_regions(path, regions)


def _aiff_payload_digest(path: Path | int) -> tuple[int, str]:
    regions: list[tuple[int, int]] = []
    try:
        size = _source_stat(path).st_size
        with _binary_source(path) as handle:
            header = handle.read(12)
            if len(header) != 12 or header[:4] != b"FORM" or header[8:12] not in {b"AIFF", b"AIFC"}:
                raise EmbedError(f"unsupported AIFF structure while hashing payload: {path}")
            offset = 12
            while offset + 8 <= size:
                handle.seek(offset)
                chunk_header = handle.read(8)
                chunk_size = int.from_bytes(chunk_header[4:8], "big")
                payload_start = offset + 8
                payload_end = payload_start + chunk_size
                if payload_end > size:
                    raise EmbedError(f"truncated AIFF chunk while hashing payload: {path}")
                if chunk_header[:4] == b"SSND":
                    regions.append((payload_start, payload_end))
                offset = payload_end + (chunk_size & 1)
    except OSError as exc:
        raise EmbedError(f"failed to locate AIFF payload in {path}: {exc}") from exc
    return _hash_regions(path, regions)


def _wavpack_payload_digest(path: Path | int) -> tuple[int, str]:
    try:
        end = _source_stat(path).st_size
        with _binary_source(path) as handle:
            if handle.read(4) != b"wvpk":
                raise EmbedError(f"invalid WavPack marker while hashing payload: {path}")
            if end >= 32:
                handle.seek(end - 32)
                footer = handle.read(32)
                if footer[:8] == b"APETAGEX":
                    ape_size = int.from_bytes(footer[12:16], "little")
                    if ape_size < 32 or ape_size > end:
                        raise EmbedError(f"invalid WavPack APEv2 size: {path}")
                    end -= ape_size
                    if end >= 32:
                        handle.seek(end - 32)
                        if handle.read(8) == b"APETAGEX":
                            end -= 32
    except OSError as exc:
        raise EmbedError(f"failed to locate WavPack payload in {path}: {exc}") from exc
    return _hash_file_region(path, 0, end)


def _ogg_payload_digest(path: Path | int) -> tuple[int, str]:
    states: dict[int, dict[str, Any]] = {}
    try:
        size = _source_stat(path).st_size
        offset = 0
        with _binary_source(path) as handle:
            while offset < size:
                handle.seek(offset)
                header = handle.read(27)
                if len(header) != 27 or header[:4] != b"OggS" or header[4] != 0:
                    raise EmbedError(f"invalid Ogg page while hashing payload: {path}")
                segment_count = header[26]
                lacing = handle.read(segment_count)
                if len(lacing) != segment_count:
                    raise EmbedError(f"truncated Ogg lacing table: {path}")
                body_size = sum(lacing)
                body = handle.read(body_size)
                if len(body) != body_size:
                    raise EmbedError(f"truncated Ogg page body: {path}")
                serial = int.from_bytes(header[14:18], "little")
                state = states.setdefault(
                    serial,
                    {
                        "buffer": bytearray(),
                        "digest": hashlib.sha256(),
                        "index": 0,
                        "total": 0,
                        "first": b"",
                    },
                )
                buffer = state["buffer"]
                assert isinstance(buffer, bytearray)
                position = 0
                for length in lacing:
                    buffer.extend(body[position : position + length])
                    position += length
                    if len(buffer) > 16 * 1024 * 1024:
                        raise EmbedError(f"unreasonably large Ogg packet: {path}")
                    if length < 255:
                        index = int(state["index"])
                        packet = bytes(buffer)
                        if index == 0:
                            state["first"] = packet[:16]
                        if index != 1:
                            digest = state["digest"]
                            digest.update(len(packet).to_bytes(8, "big"))
                            digest.update(packet)
                            state["total"] = int(state["total"]) + len(packet)
                        state["index"] = index + 1
                        buffer.clear()
                offset += 27 + segment_count + body_size
    except OSError as exc:
        raise EmbedError(f"failed to locate Ogg payload in {path}: {exc}") from exc
    if offset != size or len(states) != 1:
        raise EmbedError(f"Ogg file must contain exactly one complete logical stream: {path}")
    state = next(iter(states.values()))
    buffer = state["buffer"]
    first = bytes(state["first"])
    if (
        not isinstance(buffer, bytearray)
        or buffer
        or int(state["index"]) < 3
        or not (first.startswith(b"OpusHead") or first.startswith(b"\x01vorbis"))
    ):
        raise EmbedError(f"unsupported or incomplete Ogg audio stream: {path}")
    digest = state["digest"]
    return int(state["total"]), digest.hexdigest()


def _encoded_audio_payload_digest(path: Path | int, audio: object) -> tuple[int, str]:
    if isinstance(audio, MP3):
        return _mp3_payload_digest(path)
    if isinstance(audio, FLAC):
        return _flac_payload_digest(path)
    if isinstance(audio, MP4):
        return _mp4_payload_digest(path)
    if isinstance(audio, WAVE):
        return _riff_payload_digest(path)
    if isinstance(audio, AIFF):
        return _aiff_payload_digest(path)
    if isinstance(audio, WavPack):
        return _wavpack_payload_digest(path)
    if isinstance(audio, (OggOpus, OggVorbis)):
        return _ogg_payload_digest(path)
    raise EmbedError(f"cannot hash payload for unsupported container {type(audio).__name__}")


def _semantic_snapshot(path: Path | int) -> tuple[object, ...]:
    try:
        with _binary_source(path) as handle:
            audio = mutagen.File(handle)
    except (OSError, mutagen.MutagenError) as exc:
        raise EmbedError(f"cannot verify preserved metadata in {path}: {exc}") from exc
    if audio is None:
        raise EmbedError(f"cannot verify preserved metadata in {path}")
    info = getattr(audio, "info", None)
    stream = tuple(
        (name, _stable_value(getattr(info, name, None)))
        for name in ("sample_rate", "channels", "bits_per_sample", "bitrate", "length")
    )
    nonfront: list[object] = []
    tags: list[object] = []
    if isinstance(audio, (MP3, WAVE, AIFF)):
        for frame in audio.tags.values() if audio.tags is not None else []:
            if isinstance(frame, APIC):
                if frame.type != PictureType.COVER_FRONT:
                    nonfront.append(
                        (
                            frame.HashKey,
                            int(frame.encoding),
                            frame.mime,
                            int(frame.type),
                            frame.desc,
                            _stable_value(frame.data),
                        )
                    )
            else:
                tags.append((frame.HashKey, _stable_value(frame)))
        tag_version = audio.tags.version[1] if audio.tags is not None else None
    elif isinstance(audio, FLAC):
        tags = [
            (str(key).casefold(), _stable_value(value)) for key, value in (audio.tags or {}).items()
        ]
        nonfront = [
            (int(picture.type), picture.mime, picture.desc, _stable_value(picture.data))
            for picture in audio.pictures
            if picture.type != PictureType.COVER_FRONT
        ]
        tag_version = None
    elif isinstance(audio, (OggOpus, OggVorbis)):
        for key, value in (audio.tags or {}).items():
            if str(key).casefold() != "metadata_block_picture":
                tags.append((str(key).casefold(), _stable_value(value)))
        for _encoded, picture in _parse_xiph_pictures(
            (audio.tags or {}).get("metadata_block_picture", [])
        ):
            if picture is not None and picture.type != PictureType.COVER_FRONT:
                nonfront.append(
                    (int(picture.type), picture.mime, picture.desc, _stable_value(picture.data))
                )
        tag_version = None
    elif isinstance(audio, MP4):
        tags = [
            (str(key), _stable_value(value))
            for key, value in (audio.tags or {}).items()
            if key != "covr"
        ]
        tag_version = None
    elif isinstance(audio, WavPack):
        tags = [
            (str(key).casefold(), _stable_value(value))
            for key, value in (audio.tags or {}).items()
            if str(key).casefold() != "cover art (front)".casefold()
        ]
        tag_version = None
    else:
        raise EmbedError(f"cannot snapshot unsupported metadata container {type(audio).__name__}")
    ape_snapshot: tuple[object, ...] | None = None
    payload_digest = _encoded_audio_payload_digest(path, audio)
    if isinstance(audio, MP3):
        ape_tags = _apev2_tags(path)
        ape_snapshot = (
            tuple(
                sorted(
                    (str(key).casefold(), _stable_value(value)) for key, value in ape_tags.items()
                )
            )
            if ape_tags is not None
            else ()
        )
    return (
        type(audio).__name__,
        stream,
        tag_version,
        tuple(sorted(tags, key=str)),
        tuple(sorted(nonfront, key=str)),
        _read_id3v1(path) if isinstance(audio, MP3) else None,
        ape_snapshot,
        payload_digest,
    )


def _embed_artwork_in_place(
    path: Path | int,
    artwork: Artwork,
    *,
    replace_existing: bool = False,
    display_path: Path | None = None,
) -> EmbedResult:
    """Embed a verified front cover through one already-bound file object."""
    shown_path = display_path or (path if isinstance(path, Path) else Path(f"fd:{path}"))
    plan = _preflight_opened(
        path,
        shown_path,
        artwork,
        replace_existing=replace_existing,
    )
    if plan.status != "ready":
        return plan
    try:
        audio = _load_mutagen(path)
    except (OSError, mutagen.MutagenError) as exc:
        raise EmbedError(f"cannot open {shown_path}: {exc}") from exc
    if audio is None:
        raise EmbedError(f"unsupported or unreadable audio file: {shown_path}")

    if isinstance(audio, (OggOpus, OggVorbis)):
        if audio.tags is None:
            audio.add_tags()
        assert audio.tags is not None
        parsed = _parse_xiph_pictures(audio.tags.get("metadata_block_picture", []))
        if any(picture is None for _, picture in parsed):
            raise EmbedError(f"malformed METADATA_BLOCK_PICTURE in {path}; refusing to modify")
        front_pictures = [
            picture
            for _, picture in parsed
            if picture is not None and picture.type == PictureType.COVER_FRONT
        ]
        legacy_cover = list(audio.tags.get("coverart", []))
        if legacy_cover or audio.tags.get("coverartmime"):
            raise EmbedError(
                f"legacy COVERART fields in {path} have no picture role; refusing to modify"
            )
        if len(front_pictures) == 1 and not legacy_cover and front_pictures[0].data == artwork.data:
            return EmbedResult("unchanged", "Xiph", "identical front cover already embedded")
        if (front_pictures or legacy_cover) and not replace_existing:
            return EmbedResult("skipped", "Xiph", "front cover already exists")
        preserved = [
            value
            for value, picture in parsed
            if picture is None or picture.type != PictureType.COVER_FRONT
        ]
        encoded = base64.b64encode(_flac_picture_from_artwork(artwork).write()).decode("ascii")
        try:
            audio.tags["metadata_block_picture"] = [*preserved, encoded]
            _save_mutagen(audio, path)
            verified = _load_mutagen(path)
        except (OSError, mutagen.MutagenError) as exc:
            raise EmbedError(f"failed to write Xiph artwork to {path}: {exc}") from exc
        assert verified is not None and verified.tags is not None
        written = [
            picture
            for _, picture in _parse_xiph_pictures(verified.tags.get("metadata_block_picture", []))
            if picture is not None and picture.type == PictureType.COVER_FRONT
        ]
        if len(written) != 1 or hashlib.sha256(written[0].data).hexdigest() != artwork.sha256:
            raise EmbedError(f"Xiph artwork verification failed for {path}")
        return EmbedResult("embedded", "Xiph", "front cover embedded and verified")

    if isinstance(audio, MP4):
        if audio.tags is None:
            audio.add_tags()
        assert audio.tags is not None
        covers = list(audio.tags.get("covr", []))
        if len(covers) == 1 and bytes(covers[0]) == artwork.data:
            return EmbedResult("unchanged", "MP4", "identical front cover already embedded")
        if covers and not replace_existing:
            return EmbedResult("skipped", "MP4", "front cover already exists")
        image_format = MP4Cover.FORMAT_PNG if artwork.mime == "image/png" else MP4Cover.FORMAT_JPEG
        try:
            audio.tags["covr"] = [MP4Cover(artwork.data, imageformat=image_format)]
            _save_mutagen(audio, path)
            verified = _load_mutagen_class(path, MP4)
        except (OSError, mutagen.MutagenError) as exc:
            raise EmbedError(f"failed to write MP4 artwork to {path}: {exc}") from exc
        assert verified.tags is not None
        written = list(verified.tags.get("covr", []))
        if len(written) != 1 or hashlib.sha256(bytes(written[0])).hexdigest() != artwork.sha256:
            raise EmbedError(f"MP4 artwork verification failed for {path}")
        return EmbedResult("embedded", "MP4", "front cover embedded and verified")

    if isinstance(audio, (MP3, WAVE, AIFF)):
        is_mp3 = isinstance(audio, MP3)
        format_name = "MP3" if is_mp3 else "AIFF" if isinstance(audio, AIFF) else "WAVE"
        original_id3v1 = _read_id3v1(path) if is_mp3 else None
        if is_mp3 and _id3v2_major(path) == 2:
            raise EmbedError(f"ID3v2.2 artwork updates are unsupported for {path}")
        if audio.tags is None:
            audio.add_tags()
        assert audio.tags is not None
        pictures = audio.tags.getall("APIC")
        front_pictures = [
            picture for picture in pictures if picture.type == PictureType.COVER_FRONT
        ]
        if len(front_pictures) == 1 and front_pictures[0].data == artwork.data:
            return EmbedResult("unchanged", format_name, "identical front cover already embedded")
        if front_pictures and not replace_existing:
            return EmbedResult("skipped", format_name, "front cover already exists")
        version = 3 if audio.tags.version[1] == 3 else 4
        used_descriptions = {
            picture.desc for picture in pictures if picture.type != PictureType.COVER_FRONT
        }
        description = "Front cover"
        suffix = 2
        while description in used_descriptions:
            description = f"Front cover ({suffix})"
            suffix += 1
        try:
            for picture in front_pictures:
                del audio.tags[picture.HashKey]
            audio.tags.add(
                APIC(
                    encoding=0 if version == 3 else 3,
                    mime=artwork.mime,
                    type=PictureType.COVER_FRONT,
                    desc=description,
                    data=artwork.data,
                )
            )
            if is_mp3:
                _save_mutagen(audio, path, v2_version=version, v1=0)
                _restore_id3v1(path, original_id3v1)
            else:
                _save_mutagen(audio, path, v2_version=version)
            verified = _load_mutagen(path)
        except (OSError, mutagen.MutagenError) as exc:
            raise EmbedError(f"failed to write {format_name} artwork to {path}: {exc}") from exc
        assert verified is not None and verified.tags is not None
        written = [
            picture
            for picture in verified.tags.getall("APIC")
            if picture.type == PictureType.COVER_FRONT
        ]
        if len(written) != 1 or hashlib.sha256(written[0].data).hexdigest() != artwork.sha256:
            raise EmbedError(f"{format_name} artwork verification failed for {path}")
        return EmbedResult("embedded", format_name, "front cover embedded and verified")

    if isinstance(audio, WavPack):
        if audio.tags is None:
            audio.add_tags()
        assert audio.tags is not None
        existing = audio.tags.get("Cover Art (Front)")
        existing_payload = bytes(existing) if existing is not None else None
        if existing_payload is not None and b"\0" not in existing_payload:
            raise EmbedError(f"malformed WavPack front-cover field in {path}; refusing to modify")
        existing_data = (
            existing_payload.split(b"\0", 1)[1] if existing_payload is not None else None
        )
        if existing_data == artwork.data:
            return EmbedResult("unchanged", "WavPack", "identical front cover already embedded")
        if existing_payload is not None and not replace_existing:
            return EmbedResult("skipped", "WavPack", "front cover already exists")
        filename = b"cover.png" if artwork.mime == "image/png" else b"cover.jpg"
        try:
            audio.tags["Cover Art (Front)"] = APEBinaryValue(filename + b"\0" + artwork.data)
            _save_mutagen(audio, path)
            verified = _load_mutagen_class(path, WavPack)
        except (OSError, mutagen.MutagenError) as exc:
            raise EmbedError(f"failed to write WavPack artwork to {path}: {exc}") from exc
        assert verified.tags is not None
        written = bytes(verified.tags["Cover Art (Front)"])
        if (
            b"\0" not in written
            or hashlib.sha256(written.split(b"\0", 1)[1]).hexdigest() != artwork.sha256
        ):
            raise EmbedError(f"WavPack artwork verification failed for {path}")
        return EmbedResult("embedded", "WavPack", "front cover embedded and verified")

    if isinstance(audio, FLAC):
        front_pictures = [
            picture for picture in audio.pictures if picture.type == PictureType.COVER_FRONT
        ]
        if len(front_pictures) == 1 and front_pictures[0].data == artwork.data:
            return EmbedResult("unchanged", "FLAC", "identical front cover already embedded")
        if front_pictures and not replace_existing:
            return EmbedResult("skipped", "FLAC", "front cover already exists")
        preserved = [
            picture for picture in audio.pictures if picture.type != PictureType.COVER_FRONT
        ]
        try:
            audio.clear_pictures()
            for picture in preserved:
                audio.add_picture(picture)
            audio.add_picture(_flac_picture_from_artwork(artwork))
            _save_mutagen(audio, path)
            verified = _load_mutagen_class(path, FLAC)
        except (OSError, mutagen.MutagenError) as exc:
            raise EmbedError(f"failed to write FLAC artwork to {path}: {exc}") from exc
        written = [
            picture for picture in verified.pictures if picture.type == PictureType.COVER_FRONT
        ]
        if len(written) != 1 or hashlib.sha256(written[0].data).hexdigest() != artwork.sha256:
            raise EmbedError(f"FLAC artwork verification failed for {path}")
        return EmbedResult("embedded", "FLAC", "front cover embedded and verified")

    raise EmbedError(f"artwork embedding is not implemented for {type(audio).__name__}: {path}")


def _stat_identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _require_source_identity(
    path: Path,
    expected: tuple[int, int, int, int, int] | None,
) -> os.stat_result:
    parent_descriptor = -1
    source_descriptor = -1
    try:
        parent_descriptor, source_descriptor, info = _open_regular_source(path, expected)
        return info
    except (OSError, EmbedError) as exc:
        if isinstance(exc, EmbedError):
            raise
        raise EmbedError(f"audio source disappeared or cannot be inspected: {path}: {exc}") from exc
    finally:
        if source_descriptor >= 0:
            os.close(source_descriptor)
        if parent_descriptor >= 0:
            os.close(parent_descriptor)


def _verify_group_sources(group: AlbumGroup) -> None:
    for path, expected in group.source_identities:
        _require_source_identity(path, expected)


def _xattr_snapshot(path: Path | int) -> tuple[tuple[str, bytes], ...]:
    if not hasattr(os, "listxattr"):
        return ()
    try:
        if isinstance(path, int):
            names = os.listxattr(path)
            return tuple(sorted((name, os.getxattr(path, name)) for name in names))
        names = os.listxattr(path, follow_symlinks=False)
        return tuple(
            sorted((name, os.getxattr(path, name, follow_symlinks=False)) for name in names)
        )
    except OSError as exc:
        unsupported = {errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)}
        if exc.errno in unsupported:
            return ()
        raise EmbedError(f"failed to inspect extended attributes on {path}: {exc}") from exc


_RENAME_EXCHANGE = 2


def _fsync_directory_descriptor(descriptor: int) -> None:
    os.fsync(descriptor)


def _rename_exchange(
    first_directory: int,
    first_name: str,
    second_directory: int,
    second_name: str,
) -> None:
    """Atomically exchange two existing directory entries (Linux renameat2)."""
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise EmbedError("secure compare-and-swap requires Linux renameat2(RENAME_EXCHANGE)")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    if (
        renameat2(
            first_directory,
            os.fsencode(first_name),
            second_directory,
            os.fsencode(second_name),
            _RENAME_EXCHANGE,
        )
        != 0
    ):
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


def _openat_regular(directory_descriptor: int, name: str) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    no_atime = getattr(os, "O_NOATIME", 0)
    try:
        descriptor = os.open(name, flags | no_atime, dir_fd=directory_descriptor)
    except OSError as exc:
        if not no_atime or exc.errno not in {errno.EPERM, errno.EACCES}:
            raise
        descriptor = os.open(name, flags, dir_fd=directory_descriptor)
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(descriptor)
        raise EmbedError(f"unsafe non-regular or linked transaction entry: {name}")
    return descriptor, info


def _create_staging_file(directory_descriptor: int, path: Path) -> tuple[int, str]:
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    for _attempt in range(64):
        name = f".{path.name}.artwork-{secrets.token_hex(16)}{path.suffix}"
        try:
            return os.open(name, flags, 0o600, dir_fd=directory_descriptor), name
        except FileExistsError:
            continue
    raise EmbedError(f"cannot allocate a private staging entry beside {path}")


def _descriptor_sha256(descriptor: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while True:
        chunk = os.pread(descriptor, 1024 * 1024, offset)
        if not chunk:
            break
        digest.update(chunk)
        offset += len(chunk)
    return digest.hexdigest()


def _entry_stat(directory_descriptor: int, name: str) -> os.stat_result:
    return os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)


def _same_inode(info: os.stat_result, identity: tuple[int, int]) -> bool:
    return (info.st_dev, info.st_ino) == identity


def _unlink_owned_entry(
    directory_descriptor: int,
    name: str,
    identity: tuple[int, int],
) -> None:
    try:
        info = _entry_stat(directory_descriptor, name)
    except FileNotFoundError:
        return
    if _same_inode(info, identity):
        os.unlink(name, dir_fd=directory_descriptor)


def _source_state_matches(
    descriptor: int,
    expected: os.stat_result,
    expected_hash: str,
    expected_xattrs: tuple[tuple[str, bytes], ...],
) -> bool:
    actual = os.fstat(descriptor)
    return (
        _same_inode(actual, (expected.st_dev, expected.st_ino))
        and actual.st_nlink == 1
        and actual.st_size == expected.st_size
        and actual.st_mtime_ns == expected.st_mtime_ns
        and actual.st_uid == expected.st_uid
        and actual.st_gid == expected.st_gid
        and stat.S_IMODE(actual.st_mode) == stat.S_IMODE(expected.st_mode)
        and _xattr_snapshot(descriptor) == expected_xattrs
        and _descriptor_sha256(descriptor) == expected_hash
    )


def embed_artwork(
    path: Path,
    artwork: Artwork,
    *,
    replace_existing: bool = False,
    expected_identity: tuple[int, int, int, int, int] | None = None,
) -> EmbedResult:
    """Stage, verify, compare-and-swap, and durably commit one artwork update."""
    path = Path(path)
    parent_descriptor = -1
    source_descriptor = -1
    staging_descriptor = -1
    staging_name: str | None = None
    staging_inode: tuple[int, int] | None = None
    exchanged = False
    backup_released = False
    result: EmbedResult | None = None

    try:
        parent_descriptor, source_descriptor, source_info = _open_regular_source(
            path,
            expected_identity,
        )
        plan = _preflight_opened(
            source_descriptor,
            path,
            artwork,
            replace_existing=replace_existing,
        )
        if plan.status != "ready":
            return plan

        source_identity = _stat_identity(source_info)
        source_inode = (source_info.st_dev, source_info.st_ino)
        source_mode = stat.S_IMODE(source_info.st_mode)
        source_xattrs = _xattr_snapshot(source_descriptor)
        staging_descriptor, staging_name = _create_staging_file(parent_descriptor, path)

        digest = hashlib.sha256()
        offset = 0
        while True:
            chunk = os.pread(source_descriptor, 1024 * 1024, offset)
            if not chunk:
                break
            digest.update(chunk)
            written = 0
            while written < len(chunk):
                count = os.pwrite(staging_descriptor, chunk[written:], offset + written)
                if count <= 0:
                    raise OSError(errno.EIO, "short staging write")
                written += count
            offset += len(chunk)
        source_hash = digest.hexdigest()
        if _stat_identity(os.fstat(source_descriptor)) != source_identity:
            raise EmbedError(f"audio file changed while being staged: {path}")

        try:
            os.fchown(staging_descriptor, source_info.st_uid, source_info.st_gid)
            os.fchmod(staging_descriptor, source_mode)
            for name, value in source_xattrs:
                os.setxattr(staging_descriptor, name, value)
            os.utime(
                staging_descriptor,
                ns=(source_info.st_atime_ns, source_info.st_mtime_ns),
            )
            os.fsync(staging_descriptor)
        except OSError as exc:
            raise EmbedError(
                f"cannot preserve source filesystem metadata for {path}: {exc}"
            ) from exc

        staged_info = os.fstat(staging_descriptor)
        staging_inode = (staged_info.st_dev, staged_info.st_ino)
        if not _same_inode(_entry_stat(parent_descriptor, staging_name), staging_inode):
            raise EmbedError(f"staging entry changed unexpectedly for {path}")
        semantic_before = _semantic_snapshot(staging_descriptor)

        result = _embed_artwork_in_place(
            staging_descriptor,
            artwork,
            replace_existing=replace_existing,
            display_path=path,
        )
        if result.status != "embedded":
            return result
        if _semantic_snapshot(staging_descriptor) != semantic_before:
            raise EmbedError(f"unrelated metadata or encoded audio was not preserved for {path}")

        try:
            os.utime(
                staging_descriptor,
                ns=(source_info.st_atime_ns, source_info.st_mtime_ns),
            )
            os.fsync(staging_descriptor)
        except OSError as exc:
            raise EmbedError(f"cannot restore source timestamps for {path}: {exc}") from exc
        updated_info = os.fstat(staging_descriptor)
        if (
            updated_info.st_uid != source_info.st_uid
            or updated_info.st_gid != source_info.st_gid
            or stat.S_IMODE(updated_info.st_mode) != source_mode
            or updated_info.st_atime_ns != source_info.st_atime_ns
            or updated_info.st_mtime_ns != source_info.st_mtime_ns
            or _xattr_snapshot(staging_descriptor) != source_xattrs
        ):
            raise EmbedError(f"filesystem metadata changed during staged update for {path}")

        if _stat_identity(os.fstat(source_descriptor)) != source_identity:
            raise EmbedError(f"audio file changed concurrently before commit: {path}")
        current_source = _entry_stat(parent_descriptor, path.name)
        if _stat_identity(current_source) != source_identity:
            raise EmbedError(f"audio file changed concurrently before commit: {path}")
        current_staging = _entry_stat(parent_descriptor, staging_name)
        if not _same_inode(current_staging, staging_inode):
            raise EmbedError(f"staging entry changed before commit: {path}")

        _rename_exchange(parent_descriptor, staging_name, parent_descriptor, path.name)
        exchanged = True

        backup_descriptor = -1
        try:
            backup_descriptor, backup_info = _openat_regular(parent_descriptor, staging_name)
            committed_entry = _entry_stat(parent_descriptor, path.name)
            cas_valid = (
                _same_inode(committed_entry, staging_inode)
                and _same_inode(backup_info, source_inode)
                and _source_state_matches(
                    backup_descriptor,
                    source_info,
                    source_hash,
                    source_xattrs,
                )
            )
        finally:
            if backup_descriptor >= 0:
                os.close(backup_descriptor)

        if not cas_valid:
            backup_entry = _entry_stat(parent_descriptor, staging_name)
            committed_entry = _entry_stat(parent_descriptor, path.name)
            if not (
                _same_inode(backup_entry, source_inode)
                and _same_inode(committed_entry, staging_inode)
            ):
                raise EmbedCommittedError(
                    f"compare-and-swap state became ambiguous for {path}; "
                    f"original retained as {staging_name}",
                    result,
                )
            _rename_exchange(parent_descriptor, staging_name, parent_descriptor, path.name)
            exchanged = False
            _fsync_directory_descriptor(parent_descriptor)
            raise EmbedError(
                f"audio file changed during compare-and-swap; concurrent version restored: {path}"
            )

        _fsync_directory_descriptor(parent_descriptor)
        committed_info = os.fstat(staging_descriptor)
        if (
            committed_info.st_uid != source_info.st_uid
            or committed_info.st_gid != source_info.st_gid
            or stat.S_IMODE(committed_info.st_mode) != source_mode
            or committed_info.st_atime_ns != source_info.st_atime_ns
            or committed_info.st_mtime_ns != source_info.st_mtime_ns
            or _xattr_snapshot(staging_descriptor) != source_xattrs
        ):
            raise EmbedError(f"committed file metadata verification failed for {path}")

        backup_entry = _entry_stat(parent_descriptor, staging_name)
        if not _same_inode(backup_entry, source_inode):
            raise EmbedCommittedError(
                f"original backup entry changed before release for {path}: {staging_name}",
                result,
            )
        os.unlink(staging_name, dir_fd=parent_descriptor)
        backup_released = True
        _fsync_directory_descriptor(parent_descriptor)
        return result
    except BaseException as exc:
        if backup_released and result is not None:
            if isinstance(exc, KeyboardInterrupt):
                raise EmbedCommittedInterrupt(
                    f"interrupted after artwork was committed to {path}",
                    result,
                    path,
                ) from exc
            if isinstance(exc, SystemExit):
                raise
            if isinstance(exc, EmbedCommittedError):
                raise
            raise EmbedCommittedError(
                f"artwork was committed but final durability reporting failed for {path}: {exc}",
                result,
            ) from exc
        if exchanged and not backup_released and result is not None and staging_name is not None:
            try:
                backup_entry = _entry_stat(parent_descriptor, staging_name)
                committed_entry = _entry_stat(parent_descriptor, path.name)
                if staging_inode is None or not (
                    _same_inode(backup_entry, (source_info.st_dev, source_info.st_ino))
                    and _same_inode(committed_entry, staging_inode)
                ):
                    raise EmbedError("transaction entries no longer permit safe rollback")
                _rename_exchange(parent_descriptor, staging_name, parent_descriptor, path.name)
                exchanged = False
            except BaseException as rollback_error:
                if isinstance(exc, KeyboardInterrupt):
                    raise EmbedCommittedInterrupt(
                        f"interrupted after artwork commit for {path}; "
                        f"rollback could not be verified: {rollback_error}",
                        result,
                        path,
                    ) from exc
                if isinstance(exc, SystemExit):
                    raise
                raise EmbedCommittedError(
                    f"artwork commit for {path} could not be rolled back safely: {rollback_error}",
                    result,
                ) from exc
            with suppress(OSError):
                _fsync_directory_descriptor(parent_descriptor)
        if isinstance(exc, EmbedError):
            raise
        if isinstance(exc, OSError):
            raise EmbedError(f"failed to stage artwork update for {path}: {exc}") from exc
        raise
    finally:
        if staging_name is not None and staging_inode is not None and parent_descriptor >= 0:
            with suppress(OSError):
                _unlink_owned_entry(parent_descriptor, staging_name, staging_inode)
        if staging_descriptor >= 0:
            os.close(staging_descriptor)
        if source_descriptor >= 0:
            os.close(source_descriptor)
        if parent_descriptor >= 0:
            os.close(parent_descriptor)


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


def text_similarity(left: str, right: str) -> float:
    left_n = normalize_text(left)
    right_n = normalize_text(right)
    if not left_n or not right_n:
        return 0.0
    if left_n == right_n:
        return 1.0
    char_score = SequenceMatcher(None, left_n, right_n).ratio()
    left_tokens = set(left_n.split())
    right_tokens = set(right_n.split())
    token_score = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
    return 0.65 * char_score + 0.35 * token_score


def _artist_identity(value: str) -> str:
    """Normalize punctuation/case while preserving meaningful leading articles."""
    return normalize_text(value)


def _artists_equivalent(left: str, right: str) -> bool:
    left_identity = _artist_identity(left)
    right_identity = _artist_identity(right)
    if not left_identity or not right_identity:
        return False
    if left_identity == right_identity:
        return True
    if left_identity.startswith("the "):
        base = left_identity[4:]
        if base != "the" and base == right_identity:
            return True
    if right_identity.startswith("the "):
        base = right_identity[4:]
        if base != "the" and base == left_identity:
            return True
    return False


def _version_qualifiers(value: str) -> frozenset[str]:
    normalized = normalize_text(value)
    qualifiers: set[str] = set()
    patterns = {
        "deluxe": r"\bdeluxe(?: edition)?\b",
        "expanded": r"\bexpanded(?: edition)?\b",
        "anniversary": r"\banniversary(?: edition)?\b",
        "special": r"\bspecial edition\b",
        "collector": r"\bcollector(?: s)? edition\b",
        "extended": r"\bextended(?: edition| version)?\b",
        "soundtrack": r"\bsoundtrack\b",
        "live": r"\blive\b",
        "mono": r"\bmono\b",
        "stereo": r"\bstereo\b",
        "acoustic": r"\bacoustic\b",
        "instrumental": r"\binstrumental\b",
        "radio edit": r"\bradio edit\b",
        "drumless": r"\bdrumless\b",
        "demo": r"\bdemo\b",
        "bonus": r"\bbonus\b",
    }
    for name, pattern in patterns.items():
        if re.search(pattern, normalized):
            qualifiers.add(name)

    anniversary = re.search(r"\b(\d{1,3})(?:st|nd|rd|th)\s+anniversary\b", normalized)
    if anniversary:
        qualifiers.add(f"anniversary:{anniversary.group(1)}")

    remaster = re.search(
        r"\b(?:(18\d{2}|19\d{2}|20\d{2}|21\d{2})\s+)?(?:digital\s+)?"
        r"remaster(?:ed)?(?:\s+(18\d{2}|19\d{2}|20\d{2}|21\d{2}))?\b",
        normalized,
    )
    if remaster:
        qualifiers.add("remaster")
        remaster_year = remaster.group(1) or remaster.group(2)
        if remaster_year:
            qualifiers.add(f"remaster:{remaster_year}")
    remix = re.search(
        r"\b(?:(18\d{2}|19\d{2}|20\d{2}|21\d{2})\s+)?remix(?:ed)?"
        r"(?:\s+(18\d{2}|19\d{2}|20\d{2}|21\d{2}))?\b",
        normalized,
    )
    if remix:
        qualifiers.add("remix")
        remix_year = remix.group(1) or remix.group(2)
        if remix_year:
            qualifiers.add(f"remix:{remix_year}")
    return frozenset(qualifiers)


def _has_version_conflict(left: str, right: str) -> bool:
    return _version_qualifiers(left) != _version_qualifiers(right)


def _title_similarity(left: str, right: str) -> float:
    def without_feature(value: str) -> str:
        normalized = normalize_text(value)
        return re.split(r"\b(?:feat|featuring|ft)\b", normalized, maxsplit=1)[0].strip()

    def without_album_version(value: str) -> str:
        return re.sub(
            r"\s*[\[(]\s*album\s+version\s*[\])]\s*$",
            "",
            value,
            flags=re.IGNORECASE,
        )

    left_without_version = without_album_version(left)
    right_without_version = without_album_version(right)
    return max(
        text_similarity(left, right),
        text_similarity(without_feature(left), without_feature(right)),
        text_similarity(left_without_version, right_without_version),
        text_similarity(
            without_feature(left_without_version),
            without_feature(right_without_version),
        ),
    )


def _duration_similarity(left_ms: int | None, right_ms: int | None) -> float:
    if left_ms is None or right_ms is None:
        return 0.5
    difference = abs(left_ms - right_ms)
    tolerance = min(4_000.0, max(2_000.0, max(left_ms, right_ms) * 0.005))
    if difference > tolerance:
        return 0.0
    return 1.0 - 0.2 * (difference / tolerance)


def _position_similarity(local: TrackMetadata, remote: CatalogTrack) -> float:
    known = 0
    matched = 0
    for left, right in (
        (local.disc_number, remote.disc_number),
        (local.track_number, remote.track_number),
    ):
        if left is not None and right is not None:
            known += 1
            matched += int(left == right)
    return matched / known if known else 0.5


def _match_tracks(
    local_tracks: tuple[TrackMetadata, ...], remote_tracks: tuple[CatalogTrack, ...]
) -> list[tuple[TrackMetadata, CatalogTrack, float, float, float]]:
    possible: list[tuple[float, TrackMetadata, CatalogTrack, float, float, float]] = []
    for local in local_tracks:
        for remote in remote_tracks:
            title_score = _title_similarity(local.title, remote.title)
            duration_score = _duration_similarity(local.duration_ms, remote.duration_ms)
            if title_score < 0.93 or _has_version_conflict(local.title, remote.title):
                continue
            if (
                local.duration_ms is not None
                and remote.duration_ms is not None
                and duration_score == 0
            ):
                continue
            position_score = _position_similarity(local, remote)
            pair_score = 0.74 * title_score + 0.16 * duration_score + 0.10 * position_score
            possible.append(
                (
                    pair_score,
                    local,
                    remote,
                    title_score,
                    duration_score,
                    position_score,
                )
            )

    matches: list[tuple[TrackMetadata, CatalogTrack, float, float, float]] = []
    used_local: set[Path] = set()
    used_remote: set[tuple[int | None, int | None, str]] = set()
    for _pair_score, local, remote, title_score, duration_score, position_score in sorted(
        possible, key=lambda item: item[0], reverse=True
    ):
        remote_key = (remote.disc_number, remote.track_number, normalize_text(remote.title))
        if local.path in used_local or remote_key in used_remote:
            continue
        used_local.add(local.path)
        used_remote.add(remote_key)
        matches.append((local, remote, title_score, duration_score, position_score))
    return matches


def _local_tracklist_incomplete(group: AlbumGroup) -> bool:
    tracks = group.logical_tracks
    if not tracks:
        return True

    positions: dict[int, list[int]] = defaultdict(list)
    track_totals: dict[int, set[int]] = defaultdict(set)
    declared_disc_totals: set[int] = set()
    for track in tracks:
        disc = track.disc_number or 1
        number = track.track_number
        if disc < 1 or number is None or number < 1:
            return True
        positions[disc].append(number)
        if track.track_total is not None:
            if track.track_total < 1:
                return True
            track_totals[disc].add(track.track_total)
        if track.disc_total is not None:
            if track.disc_total < 1:
                return True
            declared_disc_totals.add(track.disc_total)

    observed_discs = set(positions)
    if observed_discs != set(range(1, max(observed_discs) + 1)):
        return True
    if len(declared_disc_totals) > 1:
        return True
    if declared_disc_totals and observed_discs != set(
        range(1, next(iter(declared_disc_totals)) + 1)
    ):
        return True

    all_declared_totals = {value for values in track_totals.values() for value in values}
    global_total_convention = len(all_declared_totals) == 1 and next(
        iter(all_declared_totals), 0
    ) == len(tracks)
    for disc, numbers in positions.items():
        if len(numbers) != len(set(numbers)):
            return True
        if set(numbers) != set(range(1, max(numbers) + 1)):
            return True
        declared = track_totals.get(disc, set())
        if len(declared) > 1:
            return True
        if declared and not global_total_convention and next(iter(declared)) != len(numbers):
            return True
    return False


def _normalize_barcode(value: str | None) -> str | None:
    if not value:
        return None
    compact = re.sub(r"[\s-]+", "", value)
    if not compact.isascii() or not compact.isdigit() or len(compact) not in {8, 12, 13, 14}:
        return None
    checksum = sum(
        int(digit) * (3 if (len(compact) - index) % 2 == 0 else 1)
        for index, digit in enumerate(compact)
    )
    return compact if checksum % 10 == 0 else None


def _normalize_release_id(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return str(uuid.UUID(value.strip()))
    except (ValueError, AttributeError):
        return None


def score_candidate(
    group: AlbumGroup,
    candidate: CatalogAlbum,
    *,
    allow_short_releases: bool = False,
) -> CandidateScore:
    """Score a candidate with hard identity and tracklist gates before fuzzy ranking."""
    album_score = text_similarity(group.album, candidate.album)
    artist_score = text_similarity(group.album_artist, candidate.artist)
    matches = _match_tracks(group.logical_tracks, candidate.tracks)
    local_count = len(group.logical_tracks)
    remote_song_count = len(candidate.tracks)
    remote_count = candidate.track_count or remote_song_count
    coverage = len(matches) / max(local_count, remote_song_count, remote_count, 1)
    track_title_score = sum(match[2] for match in matches) / len(matches) if matches else 0.0
    duration_score = sum(match[3] for match in matches) / len(matches) if matches else 0.0
    position_score = sum(match[4] for match in matches) / len(matches) if matches else 0.0
    track_artist_score = (
        sum(
            float(_artists_equivalent(local.artist, remote.artist)) for local, remote, *_ in matches
        )
        / len(matches)
        if matches
        else 0.0
    )
    count_score = (
        max(0.0, 1.0 - abs(local_count - remote_count) / max(local_count, remote_count, 1))
        if remote_count
        else 0.5
    )
    if group.year is None or candidate.release_year is None:
        year_score = 0.5
    else:
        year_score = max(0.0, 1.0 - abs(group.year - candidate.release_year) / 5)

    reasons: list[str] = []
    local_incomplete = _local_tracklist_incomplete(group)
    if local_incomplete:
        reasons.append("local tracklist appears incomplete")
    if remote_count != remote_song_count:
        reasons.append("Apple tracklist appears incomplete")
    local_positions = tuple(
        sorted(
            (track.disc_number, track.track_number)
            for track in group.logical_tracks
            if track.disc_number is not None and track.track_number is not None
        )
    )
    remote_positions = tuple(
        sorted(
            (track.disc_number, track.track_number)
            for track in candidate.tracks
            if track.disc_number is not None and track.track_number is not None
        )
    )
    if (
        not local_incomplete
        and len(local_positions) == local_count
        and len(remote_positions) == remote_song_count
        and local_positions != remote_positions
    ):
        reasons.append("disc/track topology mismatch")
    if _has_version_conflict(group.album, candidate.album):
        reasons.append("edition/version conflict")
    if album_score < 0.72:
        reasons.append("album mismatch")
    if not _artists_equivalent(group.album_artist, candidate.artist):
        reasons.append("artist mismatch")
    if matches and track_artist_score < 1.0:
        reasons.append("track artist mismatch")
    required_matches = (
        1 if local_count == 1 else 2 if local_count == 2 else max(3, math.ceil(0.70 * local_count))
    )
    if len(matches) < required_matches:
        reasons.append("tracklist mismatch")
    local_barcode = _normalize_barcode(group.barcode)
    verified_barcode = _normalize_barcode(candidate.verified_barcode)
    identifier_verified = bool(local_barcode and local_barcode == verified_barcode)
    if len(matches) < 3 and not identifier_verified and not allow_short_releases:
        reasons.append("fewer than three strong tracks")
    if coverage < 0.85:
        reasons.append("tracklist coverage below 85%")

    topology_score = 0.5 * count_score + 0.5 * position_score
    components = {
        "album": album_score,
        "artist": artist_score,
        "track_artist": track_artist_score,
        "track_coverage": coverage,
        "track_title": track_title_score,
        "duration": duration_score,
        "topology": topology_score,
        "track_count": count_score,
        "year": year_score,
        "position": position_score,
    }
    weights = {
        "album": 0.20,
        "artist": 0.15,
        "track_artist": 0.05,
        "track_title": 0.25,
        "duration": 0.20,
        "topology": 0.10,
        "year": 0.05,
    }
    total = sum(components[name] * weight for name, weight in weights.items())
    return CandidateScore(
        candidate=candidate,
        total=total,
        eligible=not reasons,
        reasons=tuple(reasons),
        components=components,
    )


def choose_match(
    group: AlbumGroup,
    candidates: Iterable[CatalogAlbum],
    *,
    min_score: float = 0.92,
    min_margin: float = 0.10,
    allow_short_releases: bool = False,
) -> MatchDecision:
    scores = tuple(
        sorted(
            (
                score_candidate(
                    group,
                    candidate,
                    allow_short_releases=allow_short_releases,
                )
                for candidate in candidates
            ),
            key=lambda score: score.total,
            reverse=True,
        )
    )
    eligible = [score for score in scores if score.eligible]
    if not eligible:
        return MatchDecision(
            "no_match", None, scores, "no candidate passed identity and tracklist gates"
        )
    best = eligible[0]
    if best.total < min_score:
        return MatchDecision(
            "low_confidence", None, scores, f"best score {best.total:.3f} is below {min_score:.3f}"
        )
    if len(eligible) > 1:
        margin = best.total - eligible[1].total
        if margin < min_margin:
            return MatchDecision(
                "ambiguous",
                None,
                scores,
                f"top-candidate margin {margin:.3f} is below {min_margin:.3f}",
            )
    return MatchDecision("matched", best, scores, "verified metadata and tracklist match")


def _as_int(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def candidate_ids_from_album_search(
    rows: Iterable[Mapping[str, object]],
    artist: str,
    album: str,
    *,
    limit: int = 12,
) -> list[int]:
    ranked: list[tuple[float, int]] = []
    for row in rows:
        collection_id = _as_int(row.get("collectionId"))
        if collection_id is None or not row.get("artworkUrl100"):
            continue
        row_album = str(row.get("collectionName") or "")
        row_artist = str(row.get("collectionArtistName") or row.get("artistName") or "")
        album_score = text_similarity(album, row_album)
        artist_score = text_similarity(artist, row_artist)
        if album_score < 0.62 or not _artists_equivalent(artist, row_artist):
            continue
        ranked.append((0.62 * album_score + 0.38 * artist_score, collection_id))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return list(dict.fromkeys(collection_id for _, collection_id in ranked[:limit]))


def candidate_ids_from_song_search(
    rows: Iterable[Mapping[str, object]],
    *,
    artist: str,
    album: str,
    title: str,
) -> list[int]:
    ranked: list[tuple[float, int]] = []
    for row in rows:
        collection_id = _as_int(row.get("collectionId"))
        if collection_id is None or row.get("kind") != "song":
            continue
        title_score = text_similarity(title, str(row.get("trackName") or ""))
        row_artist = str(row.get("artistName") or "")
        artist_score = text_similarity(artist, row_artist)
        album_score = text_similarity(album, str(row.get("collectionName") or ""))
        if title_score < 0.78 or not _artists_equivalent(artist, row_artist) or album_score < 0.50:
            continue
        ranked.append(
            (0.46 * title_score + 0.34 * artist_score + 0.20 * album_score, collection_id)
        )
    ranked.sort(key=lambda item: item[0], reverse=True)
    return list(dict.fromkeys(collection_id for _, collection_id in ranked[:8]))


def catalog_albums_from_lookup(rows: Iterable[Mapping[str, object]]) -> list[CatalogAlbum]:
    """Convert complete iTunes collection+song rows into verified album tracklists."""
    collection_rows: dict[int, Mapping[str, object]] = {}
    track_rows: dict[int, list[Mapping[str, object]]] = defaultdict(list)
    for row in rows:
        collection_id = _as_int(row.get("collectionId"))
        if collection_id is None or collection_id < 1:
            continue
        if row.get("wrapperType") == "collection":
            collection_rows[collection_id] = row
        elif row.get("wrapperType") == "track" and row.get("kind") == "song":
            track_rows[collection_id].append(row)

    albums: list[CatalogAlbum] = []
    for collection_id, collection in collection_rows.items():
        tracks_data = track_rows.get(collection_id, [])
        declared_count = _as_int(collection.get("trackCount"))
        if declared_count is None or declared_count < 1 or len(tracks_data) != declared_count:
            continue
        seen_positions: set[tuple[int, int]] = set()
        positions_by_disc: dict[int, set[int]] = defaultdict(set)
        tracks: list[CatalogTrack] = []
        valid = True
        for row in tracks_data:
            title = str(row.get("trackName") or "").strip()
            artist = str(row.get("artistName") or "").strip()
            disc = _as_int(row.get("discNumber"))
            number = _as_int(row.get("trackNumber"))
            if not title or not artist or disc is None or disc < 1 or number is None or number < 1:
                valid = False
                break
            position = (disc, number)
            if position in seen_positions:
                valid = False
                break
            seen_positions.add(position)
            positions_by_disc[disc].add(number)
            tracks.append(
                CatalogTrack(
                    title=title,
                    artist=artist,
                    duration_ms=_as_int(row.get("trackTimeMillis")),
                    disc_number=disc,
                    track_number=number,
                )
            )
        if not valid or set(positions_by_disc) != set(
            range(1, max(positions_by_disc, default=0) + 1)
        ):
            continue
        if any(
            numbers != set(range(1, max(numbers) + 1)) for numbers in positions_by_disc.values()
        ):
            continue
        album_name = str(collection.get("collectionName") or "").strip()
        album_artist = str(
            collection.get("collectionArtistName") or collection.get("artistName") or ""
        ).strip()
        artwork_url = str(collection.get("artworkUrl100") or "").strip()
        if not album_name or not album_artist or not artwork_url:
            continue
        tracks.sort(
            key=lambda track: (track.disc_number or 0, track.track_number or 0, track.title)
        )
        albums.append(
            CatalogAlbum(
                collection_id=collection_id,
                album=album_name,
                artist=album_artist,
                release_year=_year(str(collection.get("releaseDate") or "")),
                artwork_url=artwork_url,
                track_count=declared_count,
                tracks=tuple(tracks),
            )
        )
    return sorted(albums, key=lambda album: album.collection_id)


class AppleCatalogClient:
    """Polite, cached client for Apple's public iTunes Search/Lookup API."""

    def __init__(
        self,
        *,
        country: str = "US",
        cache_dir: Path = Path(".apple-artwork-cache"),
        session: object | None = None,
        timeout: float = 20.0,
        api_interval: float = 3.1,
        max_retries: int = 4,
        cache_ttl_days: int = 30,
        max_response_bytes: int = MAX_API_BYTES,
    ) -> None:
        country = country.upper()
        if not re.fullmatch(r"[A-Z]{2}", country):
            raise ValueError("country must be a two-letter storefront code")
        self.country = country
        self.cache_dir = cache_dir / "api"
        self.session = session or requests.Session()
        headers = getattr(self.session, "headers", None)
        if headers is not None:
            headers.update(
                {
                    "Accept": "application/json",
                    "User-Agent": "AppleMusicArtworkEmbedder/2.0 (+local library tool)",
                }
            )
        self.timeout = max(1.0, float(timeout))
        self.api_interval = max(0.0, api_interval)
        self.max_retries = max(1, max_retries)
        self.cache_ttl_seconds = max(0, cache_ttl_days) * 86_400
        self.max_response_bytes = max(1, min(int(max_response_bytes), MAX_API_BYTES))
        self._last_request = 0.0

    @staticmethod
    def _cache_key(url: str, params: Mapping[str, object]) -> str:
        canonical = json.dumps(
            [url, sorted(params.items())],
            ensure_ascii=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("ascii")).hexdigest()

    def _cache_path(self, url: str, params: Mapping[str, object]) -> Path:
        return self.cache_dir / f"{self._cache_key(url, params)}.json"

    def _read_cache(self, path: Path, *, expected_key: str) -> list[Mapping[str, object]] | None:
        try:
            info = path.lstat()
            if time.time() - info.st_mtime > self.cache_ttl_seconds:
                return None
            envelope = json.loads(_read_secure_file(path, self.max_response_bytes).decode("utf-8"))
            if (
                not isinstance(envelope, dict)
                or envelope.get("schema_version") != 1
                or envelope.get("cache_key") != expected_key
            ):
                return None
            payload = envelope.get("response")
            results = payload.get("results") if isinstance(payload, dict) else None
            if not isinstance(results, list) or not all(isinstance(row, dict) for row in results):
                return None
            return results
        except (OSError, ValueError, UnicodeError, TypeError):
            return None

    def _write_cache(
        self,
        path: Path,
        payload: Mapping[str, object],
        *,
        cache_key: str,
    ) -> None:
        envelope = {
            "schema_version": 1,
            "cache_key": cache_key,
            "response": payload,
        }
        encoded = json.dumps(envelope, ensure_ascii=True, sort_keys=True).encode("ascii")
        if len(encoded) > self.max_response_bytes:
            raise ValueError("Apple JSON cache payload exceeds the configured limit")
        _atomic_write_bytes(path, encoded)

    def _pace(self) -> None:
        remaining = self.api_interval - (time.monotonic() - self._last_request)
        if remaining > 0:
            time.sleep(remaining)

    def _request_results(self, url: str, params: dict[str, object]) -> list[Mapping[str, object]]:
        _validate_remote_url(url, api=True)
        cache_key = self._cache_key(url, params)
        cache_path = self._cache_path(url, params)
        cached = self._read_cache(cache_path, expected_key=cache_key)
        if cached is not None:
            return cached

        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            self._pace()
            response: object | None = None
            try:
                response, _final_url = _request_with_validated_redirects(
                    self.session,
                    url,
                    params=params,
                    timeout=self.timeout,
                    api=True,
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
                    if content_type not in {
                        "application/json",
                        "text/json",
                        "application/javascript",
                        "text/javascript",
                    }:
                        raise ValueError(
                            f"Apple API returned unsupported Content-Type {content_type!r}"
                        )
                    body = _read_bounded_body(
                        response,
                        maximum=self.max_response_bytes,
                        timeout=self.timeout,
                    )
                    payload = json.loads(body.decode("utf-8"))
                    if not isinstance(payload, dict) or not isinstance(
                        payload.get("results"), list
                    ):
                        raise ValueError("Apple returned malformed JSON")
                    if not all(isinstance(row, dict) for row in payload["results"]):
                        raise ValueError("Apple returned malformed JSON rows")
                    self._write_cache(cache_path, payload, cache_key=cache_key)
                    return payload["results"]
                if status not in {403, 429} and status < 500:
                    response.raise_for_status()  # type: ignore[attr-defined]
                last_error = requests.RequestException(f"Apple API returned HTTP {status}")
                if attempt + 1 < self.max_retries:
                    retry_after = getattr(response, "headers", {}).get("Retry-After")
                    time.sleep(_retry_delay(retry_after, attempt))
            except (
                requests.RequestException,
                OSError,
                TimeoutError,
                UnicodeError,
                ValueError,
            ) as exc:
                last_error = exc
                if attempt + 1 < self.max_retries:
                    time.sleep(_retry_delay(None, attempt))
            finally:
                if response is not None:
                    _close_response(response)
        if last_error:
            raise last_error
        raise requests.RequestException("Apple API request failed after retries")

    def _lookup_collection_ids(self, collection_ids: Iterable[int]) -> list[CatalogAlbum]:
        ordered_ids = list(dict.fromkeys(collection_ids))[:24]
        albums: list[CatalogAlbum] = []
        for start in range(0, len(ordered_ids), 8):
            chunk = ordered_ids[start : start + 8]
            lookup_rows = self._request_results(
                ITUNES_LOOKUP_URL,
                {
                    "id": ",".join(str(collection_id) for collection_id in chunk),
                    "country": self.country,
                    "entity": "song",
                    "limit": 200,
                },
            )
            parsed = catalog_albums_from_lookup(lookup_rows)
            albums.extend(parsed)
            returned = {album.collection_id for album in parsed}
            for missing_id in (
                collection_id for collection_id in chunk if collection_id not in returned
            ):
                individual_rows = self._request_results(
                    ITUNES_LOOKUP_URL,
                    {
                        "id": str(missing_id),
                        "country": self.country,
                        "entity": "song",
                        "limit": 200,
                    },
                )
                albums.extend(catalog_albums_from_lookup(individual_rows))
        return list({album.collection_id: album for album in albums}.values())

    def _song_fallback_ids(self, group: AlbumGroup) -> list[int]:
        ranked_tracks = sorted(
            group.logical_tracks,
            key=lambda track: (len(normalize_text(track.title)), track.duration_ms or 0),
            reverse=True,
        )
        anchors = ranked_tracks[:1]
        if group.logical_tracks and group.logical_tracks[0] not in anchors:
            anchors.append(group.logical_tracks[0])
        fallback_ids: list[int] = []
        for anchor in anchors:
            song_rows = self._request_results(
                ITUNES_SEARCH_URL,
                {
                    "term": f"{group.album_artist} {anchor.title}",
                    "country": self.country,
                    "media": "music",
                    "entity": "song",
                    "limit": 25,
                },
            )
            fallback_ids.extend(
                candidate_ids_from_song_search(
                    song_rows,
                    artist=group.album_artist,
                    album=group.album,
                    title=anchor.title,
                )
            )
        return list(dict.fromkeys(fallback_ids))[:8]

    def find_candidates(self, group: AlbumGroup) -> list[CatalogAlbum]:
        barcode = _normalize_barcode(group.barcode)
        if barcode:
            upc_rows = self._request_results(
                ITUNES_LOOKUP_URL,
                {
                    "upc": barcode,
                    "country": self.country,
                    "entity": "song",
                    "limit": 200,
                },
            )
            upc_albums = [
                replace(album, verified_barcode=barcode)
                for album in catalog_albums_from_lookup(upc_rows)
            ]
            if upc_albums:
                return upc_albums

        search_rows = self._request_results(
            ITUNES_SEARCH_URL,
            {
                "term": f"{group.album_artist} {group.album}",
                "country": self.country,
                "media": "music",
                "entity": "album",
                "limit": 50,
            },
        )
        collection_ids = candidate_ids_from_album_search(
            search_rows, group.album_artist, group.album
        )
        albums = self._lookup_collection_ids(collection_ids) if collection_ids else []
        has_verified_tracklist = any(
            score_candidate(group, album, allow_short_releases=True).eligible for album in albums
        )
        if not has_verified_tracklist:
            fallback_ids = [
                collection_id
                for collection_id in self._song_fallback_ids(group)
                if collection_id not in collection_ids
            ]
            albums.extend(self._lookup_collection_ids(fallback_ids))
        return list({album.collection_id: album for album in albums}.values())


def _clean_untrusted_text(value: object, *, maximum: int = MAX_TAG_TEXT) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    text = str(value)
    if len(text) > maximum:
        return ""
    cleaned = "".join(
        " " if unicodedata.category(character).startswith("C") else character for character in text
    )
    return " ".join(cleaned.split())


def _terminal_safe(value: object) -> str:
    text = str(value)
    return "".join(
        " " if unicodedata.category(character).startswith("C") else character for character in text
    )


def _first_tag(tags: Mapping[str, object] | None, *names: str) -> str:
    if not tags:
        return ""
    lowered = {str(key).casefold(): value for key, value in tags.items()}
    for name in names:
        value = lowered.get(name.casefold())
        if isinstance(value, (list, tuple)):
            value = value[0] if value else ""
        if value is not None:
            cleaned = _clean_untrusted_text(value)
            if cleaned:
                return cleaned
    return ""


def _number_pair(value: str) -> tuple[int | None, int | None]:
    if not value or len(value) > 32:
        return None, None
    match = re.fullmatch(r"\s*(\d{1,4})(?:\s*/\s*(\d{1,4}))?\s*", value)
    if not match:
        return None, None
    number = int(match.group(1))
    total = int(match.group(2)) if match.group(2) else None
    if number < 1 or (total is not None and (total < number or total > 9999)):
        return None, None
    return number, total


def _year(value: str) -> int | None:
    match = re.search(r"(?<!\d)(?:18|19|20|21)\d{2}(?!\d)", value)
    return int(match.group()) if match else None


def discover_audio_files(root: Path) -> list[Path]:
    """Recursively discover only regular, non-symlinked files contained by root."""
    root = root.expanduser()
    try:
        root_info = root.lstat()
    except OSError:
        return []
    if root.is_symlink() or not stat.S_ISDIR(root_info.st_mode):
        return []
    root_resolved = root.resolve()
    discovered: list[Path] = []
    for path in root.rglob("*"):
        try:
            info = path.lstat()
            relative = path.relative_to(root)
            resolved = path.resolve(strict=True)
        except (OSError, ValueError):
            continue
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_nlink != 1:
            continue
        if not resolved.is_relative_to(root_resolved):
            continue
        if path.suffix.casefold() not in AUDIO_EXTENSIONS:
            continue
        if any(part.startswith(".") for part in relative.parts):
            continue
        discovered.append(path)
    return sorted(discovered, key=str)


def read_track_metadata(
    path: Path,
    *,
    expected_identity: tuple[int, int, int, int, int] | None = None,
) -> TrackMetadata | None:
    """Read tags and headers while binding the result to one safely opened object."""
    parent_descriptor = -1
    source_descriptor = -1
    try:
        parent_descriptor, source_descriptor, before = _open_regular_source(
            path,
            expected_identity,
        )
        source_identity = _stat_identity(before)
        audio = _load_mutagen(source_descriptor, easy=True, filename=path)
        after = os.fstat(source_descriptor)
    except (EmbedError, mutagen.MutagenError, OSError):
        return None
    finally:
        if source_descriptor >= 0:
            os.close(source_descriptor)
        if parent_descriptor >= 0:
            os.close(parent_descriptor)
    if audio is None or _stat_identity(after) != source_identity:
        return None

    tags = audio.tags
    title = _first_tag(tags, "title")
    album = _first_tag(tags, "album")
    artist = _first_tag(tags, "artist")
    album_artist = _first_tag(tags, "albumartist", "album artist") or artist
    track_number, track_total = _number_pair(_first_tag(tags, "tracknumber"))
    disc_number, disc_total = _number_pair(_first_tag(tags, "discnumber"))
    date = _first_tag(tags, "date", "year", "originaldate")
    barcode = _first_tag(tags, "barcode", "upc") or None
    release_id = _first_tag(tags, "musicbrainz_albumid", "musicbrainz release id") or None
    duration = getattr(getattr(audio, "info", None), "length", None)
    duration_ms: int | None = None
    try:
        numeric_duration = float(duration) if duration is not None else None
        if (
            numeric_duration is not None
            and math.isfinite(numeric_duration)
            and 0 < numeric_duration <= 86_400
        ):
            duration_ms = round(numeric_duration * 1_000)
    except (TypeError, ValueError, OverflowError):
        duration_ms = None

    return TrackMetadata(
        path=path,
        title=title,
        artist=artist,
        album=album,
        album_artist=album_artist,
        year=_year(date),
        track_number=track_number,
        track_total=track_total,
        disc_number=disc_number,
        disc_total=disc_total,
        duration_ms=duration_ms,
        barcode=_normalize_barcode(barcode),
        musicbrainz_release_id=_normalize_release_id(release_id),
        source_identity=source_identity,
    )


def _release_key(track: TrackMetadata) -> tuple[object, ...]:
    identity = (
        normalize_text(track.album_artist or track.artist),
        normalize_text(track.album),
        track.year,
    )
    release_id = _normalize_release_id(track.musicbrainz_release_id)
    barcode = _normalize_barcode(track.barcode)
    if release_id:
        return ("musicbrainz", release_id, *identity)
    if barcode:
        return ("barcode", barcode, *identity)
    return ("tags", *identity)


def _logical_track_key(track: TrackMetadata) -> tuple[object, ...]:
    disc = track.disc_number or 1
    if track.track_number:
        return (disc, track.track_number, normalize_text(track.title))
    duration_bucket = round((track.duration_ms or 0) / 2_000)
    return (disc, normalize_text(track.title), duration_bucket)


def group_tracks(tracks: Iterable[TrackMetadata]) -> list[AlbumGroup]:
    """Group by release tags and collapse duplicate encodings of each logical track."""
    buckets: dict[tuple[object, ...], list[TrackMetadata]] = defaultdict(list)
    for track in tracks:
        if track.album and track.title and (track.album_artist or track.artist):
            buckets[_release_key(track)].append(track)

    groups: list[AlbumGroup] = []
    for members in buckets.values():
        logical: dict[tuple[object, ...], TrackMetadata] = {}
        for track in sorted(members, key=lambda item: str(item.path)):
            logical.setdefault(_logical_track_key(track), track)
        first = members[0]
        groups.append(
            AlbumGroup(
                album=first.album,
                album_artist=first.album_artist or first.artist,
                year=first.year,
                files=tuple(sorted((track.path for track in members), key=str)),
                logical_tracks=tuple(logical.values()),
                barcode=_normalize_barcode(first.barcode),
                musicbrainz_release_id=_normalize_release_id(first.musicbrainz_release_id),
                source_identities=tuple(
                    (track.path, track.source_identity)
                    for track in sorted(members, key=lambda item: str(item.path))
                    if track.source_identity is not None
                ),
            )
        )
    return sorted(
        groups, key=lambda group: (normalize_text(group.album_artist), normalize_text(group.album))
    )


def _candidate_score_report(score: CandidateScore) -> dict[str, object]:
    return {
        "collection_id": score.candidate.collection_id,
        "artist": score.candidate.artist,
        "album": score.candidate.album,
        "score": round(score.total, 6),
        "eligible": score.eligible,
        "reasons": list(score.reasons),
        "components": {name: round(value, 6) for name, value in score.components.items()},
    }


def _prepare_report_destination(
    root: Path,
    report_path: Path,
    audio_paths: Iterable[Path],
    *,
    overwrite: bool,
) -> Path:
    root = Path(os.path.abspath(os.fspath(root)))
    destination = report_path.expanduser()
    if not destination.is_absolute():
        destination = root / destination
    destination = Path(os.path.abspath(os.fspath(destination)))
    if not destination.is_relative_to(root):
        raise ValueError("report destination must stay inside the selected library root")
    for audio_path in audio_paths:
        audio_lexical = Path(os.path.abspath(os.fspath(audio_path)))
        if destination == audio_lexical:
            raise ValueError("report destination collides with a selected audio file")
    if destination.suffix.casefold() != ".json":
        raise ValueError("report destination must use a .json extension")
    try:
        destination_info = destination.lstat()
    except FileNotFoundError:
        destination_info = None
    except OSError as exc:
        raise ValueError(f"report destination cannot be inspected: {destination}: {exc}") from exc
    if destination_info is not None:
        if stat.S_ISLNK(destination_info.st_mode) or not stat.S_ISREG(destination_info.st_mode):
            raise ValueError("report destination is a symlink or is not a regular file")
        if not overwrite:
            raise FileExistsError(
                f"report already exists; pass --overwrite-report to overwrite it: {destination}"
            )
    try:
        parent_descriptor = _open_secure_directory(
            destination.parent,
            create=True,
            private=False,
        )
    except OSError as exc:
        raise ValueError(f"report parent contains a symlink or unsafe directory: {exc}") from exc
    os.close(parent_descriptor)
    return destination


def _write_json_report(
    path: Path,
    report: Mapping[str, object],
    *,
    overwrite: bool = False,
) -> None:
    payload = (json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode(
        "ascii"
    )
    _atomic_write_bytes(
        path,
        payload,
        overwrite=overwrite,
        private_directory=False,
    )


def _path_matches(relative_path: str, pattern: str) -> bool:
    """Match POSIX-relative paths with separator-aware `*` and recursive `**`."""
    path_parts = tuple(part for part in relative_path.replace("\\", "/").split("/") if part)
    pattern_parts = tuple(part for part in pattern.replace("\\", "/").split("/") if part)
    if not path_parts or not pattern_parts:
        return False
    memo: dict[tuple[int, int], bool] = {}

    def match(pattern_index: int, path_index: int) -> bool:
        key = (pattern_index, path_index)
        if key in memo:
            return memo[key]
        if pattern_index == len(pattern_parts):
            result = path_index == len(path_parts)
        elif pattern_parts[pattern_index] == "**":
            result = match(pattern_index + 1, path_index) or (
                path_index < len(path_parts) and match(pattern_index, path_index + 1)
            )
        else:
            result = (
                path_index < len(path_parts)
                and fnmatch.fnmatchcase(path_parts[path_index], pattern_parts[pattern_index])
                and match(pattern_index + 1, path_index + 1)
            )
        memo[key] = result
        return result

    return match(0, 0)


def process_library(
    root: Path,
    *,
    apply: bool = False,
    replace_existing: bool = False,
    country: str = "US",
    cache_dir: Path | None = None,
    report_path: Path | None = None,
    overwrite_report: bool = False,
    include: Iterable[str] = (),
    exclude: Iterable[str] = (),
    apply_dcc: bool = False,
    allow_short_releases: bool = False,
    max_dimension: int | None = None,
    refresh_artwork: bool = False,
    verbose: bool = False,
    client: object | None = None,
    downloader: object | None = None,
    emit: Callable[[str], None] = print,
) -> dict[str, object]:
    """Scan, match, report, and optionally atomically embed a library root."""

    def say(message: object) -> None:
        emit(_terminal_safe(message))

    def detail(message: object) -> None:
        if verbose:
            say(f"VERBOSE {message}")

    supplied_root = root.expanduser()
    try:
        supplied_info = supplied_root.lstat()
    except OSError as exc:
        raise ValueError(f"library root cannot be inspected: {supplied_root}: {exc}") from exc
    if supplied_root.is_symlink():
        raise ValueError(f"library root must not be a symlink: {supplied_root}")
    if not stat.S_ISDIR(supplied_info.st_mode):
        raise ValueError(f"library root is not a directory: {supplied_root}")
    try:
        root_descriptor = _open_secure_directory(
            supplied_root,
            create=False,
            private=False,
            require_owner=False,
        )
    except OSError as exc:
        raise ValueError(f"library root contains a symlink or unsafe component: {exc}") from exc
    try:
        opened_root = os.fstat(root_descriptor)
        if (opened_root.st_dev, opened_root.st_ino) != (
            supplied_info.st_dev,
            supplied_info.st_ino,
        ):
            raise ValueError("library root changed while it was being opened")
    finally:
        os.close(root_descriptor)
    root = supplied_root.resolve()
    if root == Path(root.anchor):
        raise ValueError(f"refusing to scan a filesystem root: {root}")
    country = country.upper()
    if not re.fullmatch(r"[A-Z]{2}", country):
        raise ValueError("country must be a two-letter storefront code")
    cache_dir = (cache_dir or root / ".apple-artwork-cache").expanduser()
    include_patterns = tuple(include)
    exclude_patterns = tuple(exclude)
    detail(
        f"SCAN root={root} mode={'apply' if apply else 'dry-run'} country={country} "
        f"apply_dcc={str(apply_dcc).lower()}"
    )

    discovered = discover_audio_files(root)
    selected: list[Path] = []
    dcc_omitted_files = 0
    for path in discovered:
        relative_path = path.relative_to(root)
        if not apply_dcc and any(part.startswith("00") for part in relative_path.parts[:-1]):
            dcc_omitted_files += 1
            detail(f"OMIT-00 {relative_path.as_posix()}")
            continue
        relative = relative_path.as_posix()
        if include_patterns and not any(
            _path_matches(relative, pattern) for pattern in include_patterns
        ):
            detail(f"OMIT-INCLUDE {relative}")
            continue
        if any(_path_matches(relative, pattern) for pattern in exclude_patterns):
            detail(f"OMIT-EXCLUDE {relative}")
            continue
        selected.append(path)
    selected.sort(key=str)
    detail(
        f"DISCOVERY discovered={len(discovered)} selected={len(selected)} "
        f"dcc_omitted={dcc_omitted_files}"
    )

    report_destination = (
        _prepare_report_destination(
            root,
            report_path,
            selected,
            overwrite=overwrite_report,
        )
        if report_path is not None
        else None
    )
    if report_destination is not None:
        _write_json_report(
            report_destination,
            {
                "schema_version": 2,
                "status": "in_progress",
                "mode": "apply" if apply else "dry-run",
                "root": str(root),
                "country": country,
                "summary": {},
                "albums": [],
                "errors": [],
            },
            overwrite=overwrite_report,
        )

    errors: list[dict[str, str]] = []
    adapter_errors: dict[Path, str] = {}
    adapter_plans: dict[Path, EmbedResult] = {}
    tracks: list[TrackMetadata] = []
    for path in selected:
        try:
            track = read_track_metadata(path)
        except Exception as exc:
            track = None
            error_text = f"metadata parser failed: {exc}"
        else:
            error_text = "unreadable audio file or unsupported metadata container"
        if track is None:
            errors.append(
                {
                    "stage": "metadata",
                    "path": str(path),
                    "error": error_text,
                }
            )
            say(f"ERROR   {path}: {error_text}")
            continue
        if not track.title or not track.album or not (track.album_artist or track.artist):
            error_text = "missing required title, album, or artist tags"
            errors.append(
                {
                    "stage": "metadata",
                    "path": str(path),
                    "error": error_text,
                }
            )
            say(f"ERROR   {path}: {error_text}")
            continue
        try:
            adapter_plans[path] = preflight_artwork(
                path,
                replace_existing=False,
                expected_identity=track.source_identity,
            )
        except Exception as exc:
            adapter_errors[path] = str(exc)
            errors.append(
                {
                    "stage": "adapter_preflight",
                    "path": str(path),
                    "error": str(exc),
                }
            )
            say(f"ERROR   {path}: adapter preflight failed: {exc}")
        else:
            plan = adapter_plans[path]
            detail(
                f"LOCAL-PREFLIGHT {path.relative_to(root).as_posix()} "
                f"status={plan.status} format={plan.format}"
            )
        tracks.append(track)

    groups = group_tracks(tracks)
    detail(f"GROUPS albums={len(groups)} metadata_tracks={len(tracks)}")
    catalog = client or AppleCatalogClient(country=country, cache_dir=cache_dir)
    artwork_client = downloader or ArtworkDownloader(cache_dir=cache_dir)
    summary: dict[str, int] = {
        "discovered_files": len(discovered),
        "selected_files": len(selected),
        "dcc_omitted_files": dcc_omitted_files,
        "metadata_tracks": len(tracks),
        "albums": len(groups),
        "matched": 0,
        "ambiguous": 0,
        "low_confidence": 0,
        "no_match": 0,
        "metadata_failures": sum(error["stage"] == "metadata" for error in errors),
        "adapter_preflight_failures": len(adapter_errors),
        "failed": len(errors),
        "files_embedded": 0,
        "files_skipped": 0,
        "files_unchanged": 0,
        "file_failures": len(adapter_errors),
    }
    album_reports: list[dict[str, object]] = []

    for group in groups:
        label = f"{group.album_artist} — {group.album}"
        detail(f"ALBUM {label} logical_tracks={len(group.logical_tracks)} files={len(group.files)}")
        base_report: dict[str, object] = {
            "artist": group.album_artist,
            "album": group.album,
            "year": group.year,
            "files": [str(path) for path in group.files],
            "logical_track_count": len(group.logical_tracks),
            "barcode": group.barcode,
            "musicbrainz_release_id": group.musicbrainz_release_id,
        }
        blocked_files = [path for path in group.files if path in adapter_errors]
        if blocked_files:
            base_report.update(
                {
                    "status": "preflight_failed",
                    "reason": (
                        f"{len(blocked_files)} file(s) failed local adapter preflight; "
                        "no catalog request was sent"
                    ),
                    "file_results": [
                        {
                            "path": str(path),
                            "status": "failed",
                            "error": adapter_errors[path],
                        }
                        for path in blocked_files
                    ],
                }
            )
            album_reports.append(base_report)
            say(
                f"ERROR   {label}: {len(blocked_files)} file(s) failed local adapter "
                "preflight; Apple was not contacted"
            )
            continue
        expected_by_path = dict(group.source_identities)
        try:
            _verify_group_sources(group)
            candidates = catalog.find_candidates(group)  # type: ignore[attr-defined]
            _verify_group_sources(group)
            decision = choose_match(
                group,
                candidates,
                allow_short_releases=allow_short_releases,
            )
        except Exception as exc:
            summary["failed"] += 1
            base_report.update(
                {
                    "status": "failed",
                    "reason": f"Apple catalog lookup failed: {exc}",
                }
            )
            album_reports.append(base_report)
            say(f"ERROR   {label}: Apple lookup failed: {exc}")
            continue

        for score in decision.scores:
            reasons = "; ".join(score.reasons) or "none"
            detail(
                f"CANDIDATE {label} collection_id={score.candidate.collection_id} "
                f"eligible={str(score.eligible).lower()} score={score.total:.3f} "
                f"reasons={reasons}"
            )

        base_report["reason"] = decision.reason
        base_report["candidates"] = [_candidate_score_report(score) for score in decision.scores]
        if decision.status != "matched" or decision.match is None:
            summary[decision.status] = summary.get(decision.status, 0) + 1
            base_report["status"] = decision.status
            album_reports.append(base_report)
            say(f"{decision.status.upper():8} {label}: {decision.reason}")
            continue

        summary["matched"] += 1
        matched = decision.match
        candidate = matched.candidate
        base_report["apple"] = {
            "collection_id": candidate.collection_id,
            "artist": candidate.artist,
            "album": candidate.album,
            "release_year": candidate.release_year,
            "track_count": candidate.track_count,
            "artwork_url": candidate.artwork_url,
            "score": round(matched.total, 6),
        }
        if not apply:
            file_results: list[dict[str, str]] = []
            preflight_failures = 0
            for path in group.files:
                try:
                    plan = adapter_plans[path]
                    file_results.append(
                        {
                            "path": str(path),
                            "status": plan.status,
                            "format": plan.format,
                            "message": plan.message,
                        }
                    )
                    detail(
                        f"PREFLIGHT {path.relative_to(root).as_posix()} "
                        f"status={plan.status} format={plan.format}"
                    )
                except Exception as exc:
                    preflight_failures += 1
                    summary["file_failures"] += 1
                    file_results.append({"path": str(path), "status": "failed", "error": str(exc)})
            base_report["file_results"] = file_results
            if preflight_failures:
                summary["failed"] += 1
                base_report["status"] = "preflight_failed"
                base_report["reason"] = (
                    f"{preflight_failures} file(s) failed non-mutating adapter preflight"
                )
                say(f"ERROR   {label}: {preflight_failures} file(s) failed dry-run preflight")
            else:
                base_report["status"] = "dry-run"
                say(
                    f"DRY-RUN {label} -> Apple {candidate.collection_id} "
                    f"({matched.total:.3f}); {len(file_results)} file(s) preflighted"
                )
            album_reports.append(base_report)
            continue

        try:
            _verify_group_sources(group)
            artwork = artwork_client.fetch(  # type: ignore[attr-defined]
                candidate.collection_id,
                candidate.artwork_url,
                max_dimension=max_dimension,
                refresh=refresh_artwork,
            )
            _verify_group_sources(group)
        except Exception as exc:
            summary["failed"] += 1
            base_report.update({"status": "failed", "reason": f"artwork download failed: {exc}"})
            album_reports.append(base_report)
            say(f"ERROR   {label}: artwork download failed: {exc}")
            continue

        base_report["artwork"] = {
            "source_url": artwork.source_url,
            "mime": artwork.mime,
            "width": artwork.width,
            "height": artwork.height,
            "depth": artwork.depth,
            "sha256": artwork.sha256,
        }
        detail(
            f"ARTWORK collection_id={candidate.collection_id} mime={artwork.mime} "
            f"dimensions={artwork.width}x{artwork.height} sha256={artwork.sha256}"
        )
        planned: list[tuple[Path, EmbedResult]] = []
        file_results: list[dict[str, str]] = []
        preflight_failures = 0
        for path in group.files:
            try:
                plan = preflight_artwork(
                    path,
                    artwork,
                    replace_existing=replace_existing,
                    expected_identity=expected_by_path.get(path),
                )
                planned.append((path, plan))
                file_results.append(
                    {
                        "path": str(path),
                        "status": plan.status,
                        "format": plan.format,
                        "message": plan.message,
                    }
                )
                detail(
                    f"PREFLIGHT {path.relative_to(root).as_posix()} "
                    f"status={plan.status} format={plan.format}"
                )
            except Exception as exc:
                preflight_failures += 1
                summary["file_failures"] += 1
                file_results.append({"path": str(path), "status": "failed", "error": str(exc)})
        if preflight_failures:
            summary["failed"] += 1
            base_report["status"] = "preflight_failed"
            base_report["reason"] = (
                f"{preflight_failures} file(s) failed; album-wide preflight prevented all writes"
            )
            base_report["file_results"] = file_results
            album_reports.append(base_report)
            say(
                f"ERROR   {label}: {preflight_failures} file(s) failed preflight; "
                "no album files were changed"
            )
            continue

        file_results = []
        album_failures = 0
        album_embedded = 0
        for path, plan in planned:
            if plan.status != "ready":
                file_results.append(
                    {
                        "path": str(path),
                        "status": plan.status,
                        "format": plan.format,
                        "message": plan.message,
                    }
                )
                detail(
                    f"RESULT {path.relative_to(root).as_posix()} "
                    f"status={plan.status} format={plan.format}"
                )
                if plan.status == "unchanged":
                    summary["files_unchanged"] += 1
                else:
                    summary["files_skipped"] += 1
                continue
            try:
                result = embed_artwork(
                    path,
                    artwork,
                    replace_existing=replace_existing,
                    expected_identity=expected_by_path.get(path),
                )
                file_results.append(
                    {
                        "path": str(path),
                        "status": result.status,
                        "format": result.format,
                        "message": result.message,
                    }
                )
                detail(
                    f"RESULT {path.relative_to(root).as_posix()} "
                    f"status={result.status} format={result.format}"
                )
                if result.status == "embedded":
                    album_embedded += 1
                    summary["files_embedded"] += 1
                elif result.status == "unchanged":
                    summary["files_unchanged"] += 1
                else:
                    summary["files_skipped"] += 1
            except EmbedCommittedInterrupt as exc:
                album_failures += 1
                album_embedded += 1
                summary["file_failures"] += 1
                summary["files_embedded"] += 1
                file_results.append(
                    {
                        "path": str(path),
                        "status": "committed_interrupted",
                        "format": exc.result.format,
                        "error": str(exc),
                    }
                )
                base_report["file_results"] = file_results
                base_report["status"] = "interrupted_committed"
                base_report["reason"] = str(exc)
                summary["failed"] += 1
                album_reports.append(base_report)
                interrupted_report: dict[str, object] = {
                    "schema_version": 2,
                    "status": "interrupted_committed",
                    "mode": "apply",
                    "root": str(root),
                    "country": country,
                    "summary": summary,
                    "albums": album_reports,
                    "errors": errors,
                }
                if report_destination is not None:
                    try:
                        _write_json_report(report_destination, interrupted_report, overwrite=True)
                    except Exception as report_error:
                        say(
                            "ERROR   committed-interrupt report write failed: "
                            f"{_terminal_safe(report_error)}"
                        )
                say(f"INTERRUPTED-COMMITTED {path}: {exc}")
                raise
            except EmbedCommittedError as exc:
                album_failures += 1
                album_embedded += 1
                summary["file_failures"] += 1
                summary["files_embedded"] += 1
                file_results.append(
                    {
                        "path": str(path),
                        "status": "committed_unverified",
                        "error": str(exc),
                    }
                )
            except Exception as exc:
                album_failures += 1
                summary["file_failures"] += 1
                file_results.append({"path": str(path), "status": "failed", "error": str(exc)})
        base_report["file_results"] = file_results
        if album_failures:
            summary["failed"] += 1
            base_report["status"] = "partial_failure" if album_embedded else "failed"
            say(
                f"ERROR   {label}: embedded {album_embedded}/{len(group.files)} files; "
                f"{album_failures} failed after preflight"
            )
        elif album_embedded:
            base_report["status"] = "applied"
            say(f"APPLIED {label}: embedded {album_embedded} file(s)")
        else:
            base_report["status"] = "unchanged"
            say(f"SKIPPED {label}: no file required a change")
        album_reports.append(base_report)

    report: dict[str, object] = {
        "schema_version": 2,
        "mode": "apply" if apply else "dry-run",
        "root": str(root),
        "country": country,
        "summary": summary,
        "albums": album_reports,
        "errors": errors,
    }
    if report_destination is not None:
        _write_json_report(report_destination, report, overwrite=True)
        say(f"REPORT  {report_destination}")
    return report


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="apple-artwork",
        description=(
            "Accuracy-first Apple artwork matching. Dry-run is the default; "
            "audio files change only with --apply."
        ),
    )
    parser.add_argument(
        "root",
        nargs="?",
        type=Path,
        default=Path.cwd(),
        help="music-library root (default: current directory)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="atomically embed verified artwork (without this, only report matches)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="show discovery, candidate-score, and per-file progress details",
    )
    parser.add_argument(
        "--replace-existing",
        action="store_true",
        help=(
            "replace existing front covers; for supported M4A this replaces every covr item "
            "because M4A has no front/back role"
        ),
    )
    parser.add_argument(
        "--country",
        default="US",
        metavar="CC",
        help="two-letter Apple storefront code (default: US)",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        help="cache directory (default: ROOT/.apple-artwork-cache)",
    )
    parser.add_argument(
        "--report",
        dest="report_path",
        type=Path,
        default=Path("apple-artwork-report.json"),
        help="JSON report path relative to ROOT (default: apple-artwork-report.json)",
    )
    parser.add_argument(
        "--no-report",
        action="store_true",
        help="do not write a JSON report",
    )
    parser.add_argument(
        "--overwrite-report",
        action="store_true",
        help="explicitly replace an existing regular .json report inside ROOT",
    )
    parser.add_argument(
        "--include",
        action="append",
        default=[],
        metavar="GLOB",
        help="include relative paths matching GLOB (repeatable)",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help="exclude relative paths matching GLOB (repeatable)",
    )
    parser.add_argument(
        "--apply-dcc",
        action="store_true",
        help=("include folders whose relative names start with '00'; does not enable --apply"),
    )
    parser.add_argument(
        "--max-dimension",
        type=int,
        metavar="PX",
        help="cap requested artwork dimensions at 100-10000 pixels",
    )
    parser.add_argument(
        "--allow-short-releases",
        action="store_true",
        help="allow one/two-track matches without UPC evidence (less conservative)",
    )
    parser.add_argument(
        "--refresh-artwork",
        action="store_true",
        help="ignore cached artwork bytes and revalidate Apple CDN candidates",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.replace_existing and not args.apply:
        parser.error("--replace-existing requires --apply")
    if args.max_dimension is not None and not 100 <= args.max_dimension <= 10_000:
        parser.error("--max-dimension must be between 100 and 10000")

    try:
        report = process_library(
            args.root,
            apply=args.apply,
            replace_existing=args.replace_existing,
            country=args.country,
            cache_dir=args.cache_dir,
            report_path=None if args.no_report else args.report_path,
            overwrite_report=args.overwrite_report,
            include=args.include,
            exclude=args.exclude,
            apply_dcc=args.apply_dcc,
            allow_short_releases=args.allow_short_releases,
            max_dimension=args.max_dimension,
            refresh_artwork=args.refresh_artwork,
            verbose=args.verbose,
        )
    except EmbedCommittedInterrupt as exc:
        print(
            f"Interrupted after artwork was committed to {_terminal_safe(exc.path)}; "
            "the report records the committed state.",
            file=sys.stderr,
        )
        return 130
    except KeyboardInterrupt:
        print("Interrupted; no in-progress staged file was committed.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"apple-artwork: {_terminal_safe(exc)}", file=sys.stderr)
        return 2

    summary = report["summary"]
    assert isinstance(summary, dict)
    print(
        "SUMMARY "
        f"albums={summary.get('albums', 0)} "
        f"matched={summary.get('matched', 0)} "
        f"ambiguous={summary.get('ambiguous', 0)} "
        f"low_confidence={summary.get('low_confidence', 0)} "
        f"no_match={summary.get('no_match', 0)} "
        f"dcc_omitted={summary.get('dcc_omitted_files', 0)} "
        f"metadata_failures={summary.get('metadata_failures', 0)} "
        f"failed={summary.get('failed', 0)} "
        f"embedded={summary.get('files_embedded', 0)}"
    )
    return 1 if int(summary.get("failed", 0)) else 0


if __name__ == "__main__":
    raise SystemExit(main())
