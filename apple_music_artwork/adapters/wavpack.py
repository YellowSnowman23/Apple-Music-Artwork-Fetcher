"""WavPack APEv2 artwork adapter."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import cast

import mutagen
from mutagen.apev2 import APEBinaryValue
from mutagen.wavpack import WavPack

from ..filesystem import _binary_source, _source_stat
from ..models import Artwork, EmbedError, EmbedResult
from ..mutagen_io import _load_mutagen_class, _save_mutagen
from .base import FormatAdapter
from .id3 import _read_id3v1


def _wavpack_tail_kind(source: Path | int) -> str | None:
    if _read_id3v1(source) is not None:
        return "ID3v1"
    marker = b"APETAGEX"
    marker_offsets: list[int] = []
    try:
        size = _source_stat(source).st_size
        with _binary_source(source) as handle:
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
        raise EmbedError(f"failed to inspect WavPack tail data in {source}: {exc}") from exc
    if any(marker in tail for marker in (b"LYRICSBEGIN", b"LYRICSEND", b"LYRICS200")):
        return "Lyrics3"
    return None


class WavPackAdapter(FormatAdapter):
    format_name = "WavPack"
    audio_types = (WavPack,)

    def front_pictures(
        self,
        audio: object,
        source: Path | int,
        display_path: Path,
    ) -> list[bytes]:
        tail_kind = _wavpack_tail_kind(source)
        if tail_kind:
            raise EmbedError(
                f"unsupported {tail_kind} WavPack tail; refusing to modify: {display_path}"
            )
        selected = cast(WavPack, audio)
        existing = selected.tags.get("Cover Art (Front)") if selected.tags is not None else None
        if existing is not None and not isinstance(existing, APEBinaryValue):
            raise EmbedError(
                "malformed WavPack front-cover field is not binary; refusing to modify"
            )
        payload = bytes(existing) if existing is not None else None
        if payload is not None and b"\0" not in payload:
            raise EmbedError("malformed WavPack front-cover field; refusing to modify")
        return [payload.split(b"\0", 1)[1]] if payload is not None else []

    def embed(
        self,
        audio: object,
        source: Path | int,
        artwork: Artwork,
        *,
        replace_existing: bool,
    ) -> EmbedResult:
        selected = cast(WavPack, audio)
        if selected.tags is None:
            selected.add_tags()
        assert selected.tags is not None
        existing = selected.tags.get("Cover Art (Front)")
        if existing is not None and not isinstance(existing, APEBinaryValue):
            raise EmbedError(
                f"malformed WavPack front-cover field in {source} is not binary; refusing to modify"
            )
        existing_payload = bytes(existing) if existing is not None else None
        if existing_payload is not None and b"\0" not in existing_payload:
            raise EmbedError(f"malformed WavPack front-cover field in {source}; refusing to modify")
        existing_data = (
            existing_payload.split(b"\0", 1)[1] if existing_payload is not None else None
        )
        if existing_data == artwork.data:
            return EmbedResult("unchanged", "WavPack", "identical front cover already embedded")
        if existing_payload is not None and not replace_existing:
            return EmbedResult("skipped", "WavPack", "front cover already exists")
        filename = b"cover.png" if artwork.mime == "image/png" else b"cover.jpg"
        try:
            selected.tags["Cover Art (Front)"] = APEBinaryValue(filename + b"\0" + artwork.data)
            _save_mutagen(selected, source)
            verified = _load_mutagen_class(source, WavPack)
        except (OSError, mutagen.MutagenError) as exc:
            raise EmbedError(f"failed to write WavPack artwork to {source}: {exc}") from exc
        assert verified.tags is not None
        written_value = verified.tags["Cover Art (Front)"]
        if not isinstance(written_value, APEBinaryValue):
            raise EmbedError(f"WavPack artwork verification found a non-binary field in {source}")
        written = bytes(written_value)
        if (
            b"\0" not in written
            or hashlib.sha256(written.split(b"\0", 1)[1]).hexdigest() != artwork.sha256
        ):
            raise EmbedError(f"WavPack artwork verification failed for {source}")
        return EmbedResult("embedded", "WavPack", "front cover embedded and verified")
