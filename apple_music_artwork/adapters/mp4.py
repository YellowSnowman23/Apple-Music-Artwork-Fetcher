"""MP4 covr atom adapter with audio-only container validation."""

from __future__ import annotations

import hashlib
import struct
from pathlib import Path
from typing import cast

import mutagen
from mutagen.mp4 import MP4, MP4Cover

from ..filesystem import _binary_source, _source_stat
from ..models import Artwork, EmbedError, EmbedResult
from ..mutagen_io import _load_mutagen_class, _save_mutagen
from .base import FormatAdapter


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


def _validate_m4a_container(source: Path | int, *, suffix: str = "") -> None:
    if suffix.casefold() not in {".m4a", ".mp4"}:
        raise EmbedError(f"only validated audio-only .m4a/.mp4 containers are accepted: {source}")
    try:
        file_size = _source_stat(source).st_size
        with _binary_source(source) as handle:
            top_level = _mp4_children(handle, 0, file_size)
            if any(box_type == b"moof" for box_type, _, _ in top_level):
                raise EmbedError(f"fragmented MP4/M4A is unsupported: {source}")
            moov_boxes = [(start, end) for box_type, start, end in top_level if box_type == b"moov"]
            if len(moov_boxes) != 1:
                raise EmbedError(f"MP4/M4A must contain exactly one moov atom: {source}")
            moov_start, moov_end = moov_boxes[0]
            if moov_end - moov_start > 64 * 1024 * 1024:
                raise EmbedError(f"MP4/M4A metadata atom is unexpectedly large: {source}")
            handle.seek(moov_start)
            moov_payload = handle.read(moov_end - moov_start)
            if any(
                marker in moov_payload for marker in (b"drms", b"encv", b"enca", b"sinf", b"mvex")
            ):
                raise EmbedError(f"encrypted or fragmented MP4/M4A is unsupported: {source}")
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
                    raise EmbedError(f"MP4/M4A track has no unique media atom: {source}")
                mdia_start, mdia_end = mdia_boxes[0]
                hdlr_boxes = [
                    (start, end)
                    for child_type, start, end in _mp4_children(handle, mdia_start, mdia_end)
                    if child_type == b"hdlr"
                ]
                if len(hdlr_boxes) != 1 or hdlr_boxes[0][0] + 12 > hdlr_boxes[0][1]:
                    raise EmbedError(f"MP4/M4A track has no valid handler: {source}")
                handle.seek(hdlr_boxes[0][0] + 8)
                handlers.append(handle.read(4))
    except EmbedError:
        raise
    except OSError as exc:
        raise EmbedError(f"failed to inspect MP4/M4A container {source}: {exc}") from exc
    if handlers != [b"soun"]:
        raise EmbedError(
            f"MP4/M4A must contain exactly one audio track and no video tracks: {source}"
        )


class MP4Adapter(FormatAdapter):
    format_name = "MP4"
    audio_types = (MP4,)

    def front_pictures(
        self,
        audio: object,
        source: Path | int,
        display_path: Path,
    ) -> list[bytes]:
        _validate_m4a_container(source, suffix=display_path.suffix)
        selected = cast(MP4, audio)
        return [bytes(cover) for cover in (selected.tags or {}).get("covr", [])]

    def embed(
        self,
        audio: object,
        source: Path | int,
        artwork: Artwork,
        *,
        replace_existing: bool,
    ) -> EmbedResult:
        selected = cast(MP4, audio)
        if selected.tags is None:
            selected.add_tags()
        assert selected.tags is not None
        covers = list(selected.tags.get("covr", []))
        if len(covers) == 1 and bytes(covers[0]) == artwork.data:
            return EmbedResult("unchanged", "MP4", "identical front cover already embedded")
        if covers and not replace_existing:
            return EmbedResult("skipped", "MP4", "front cover already exists")
        image_format = MP4Cover.FORMAT_PNG if artwork.mime == "image/png" else MP4Cover.FORMAT_JPEG
        try:
            selected.tags["covr"] = [MP4Cover(artwork.data, imageformat=image_format)]
            _save_mutagen(selected, source)
            verified = _load_mutagen_class(source, MP4)
        except (OSError, mutagen.MutagenError) as exc:
            raise EmbedError(f"failed to write MP4 artwork to {source}: {exc}") from exc
        assert verified.tags is not None
        written = list(verified.tags.get("covr", []))
        if len(written) != 1 or hashlib.sha256(bytes(written[0])).hexdigest() != artwork.sha256:
            raise EmbedError(f"MP4 artwork verification failed for {source}")
        return EmbedResult("embedded", "MP4", "front cover embedded and verified")
