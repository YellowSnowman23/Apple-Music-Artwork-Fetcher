"""ID3 APIC adapter for MP3, WAVE, and AIFF."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import cast

import mutagen
from mutagen.aiff import AIFF
from mutagen.apev2 import APENoHeaderError, APEv2
from mutagen.id3 import APIC, PictureType
from mutagen.mp3 import MP3
from mutagen.wave import WAVE

from ..filesystem import _binary_source, _source_stat
from ..models import Artwork, EmbedError, EmbedResult
from ..mutagen_io import _load_mutagen, _save_mutagen
from .base import FormatAdapter


def _read_id3v1(source: Path | int) -> bytes | None:
    try:
        if _source_stat(source).st_size < 128:
            return None
        with _binary_source(source) as handle:
            handle.seek(-128, 2)
            value = handle.read(128)
    except OSError as exc:
        raise EmbedError(f"failed to inspect ID3v1 data in {source}: {exc}") from exc
    return value if value.startswith(b"TAG") else None


def _id3v2_major(source: Path | int) -> int | None:
    try:
        with _binary_source(source) as handle:
            header = handle.read(4)
    except OSError as exc:
        raise EmbedError(f"failed to inspect ID3v2 data in {source}: {exc}") from exc
    return header[3] if len(header) == 4 and header[:3] == b"ID3" else None


def _restore_id3v1(source: Path | int, original: bytes | None) -> None:
    if original is None:
        return
    try:
        with _binary_source(source, "r+b") as handle:
            handle.seek(0, os.SEEK_END)
            handle.write(original)
            handle.flush()
    except OSError as exc:
        raise EmbedError(f"failed to restore ID3v1 data in {source}: {exc}") from exc


def _apev2_tags(source: Path | int) -> APEv2 | None:
    try:
        if isinstance(source, int):
            with _binary_source(source) as handle:
                return APEv2(fileobj=handle)
        return APEv2(source)
    except APENoHeaderError:
        return None
    except (OSError, mutagen.MutagenError) as exc:
        raise EmbedError(f"failed to inspect APEv2 metadata in {source}: {exc}") from exc


class ID3Adapter(FormatAdapter):
    format_name = "ID3"
    audio_types = (MP3, WAVE, AIFF)

    def result_format(self, audio: object) -> str:
        return "MP3" if isinstance(audio, MP3) else "AIFF" if isinstance(audio, AIFF) else "WAVE"

    def front_pictures(
        self,
        audio: object,
        source: Path | int,
        display_path: Path,
    ) -> list[bytes]:
        del display_path
        selected = cast(MP3 | WAVE | AIFF, audio)
        if isinstance(selected, MP3):
            ape_tags = _apev2_tags(source)
            if ape_tags is not None and any(
                str(key).casefold() == "cover art (front)" for key in ape_tags
            ):
                raise EmbedError(
                    "mixed MP3 ID3/APEv2 front artwork is unsupported; refusing to modify"
                )
        if selected.tags is not None and selected.tags.version[1] == 2:
            raise EmbedError("ID3v2.2 artwork updates are unsupported")
        return [
            picture.data
            for picture in (selected.tags.getall("APIC") if selected.tags is not None else [])
            if picture.type == PictureType.COVER_FRONT
        ]

    def embed(
        self,
        audio: object,
        source: Path | int,
        artwork: Artwork,
        *,
        replace_existing: bool,
    ) -> EmbedResult:
        selected = cast(MP3 | WAVE | AIFF, audio)
        is_mp3 = isinstance(selected, MP3)
        format_name = self.result_format(selected)
        original_id3v1 = _read_id3v1(source) if is_mp3 else None
        if is_mp3 and _id3v2_major(source) == 2:
            raise EmbedError(f"ID3v2.2 artwork updates are unsupported for {source}")
        if selected.tags is None:
            selected.add_tags()
        assert selected.tags is not None
        pictures = selected.tags.getall("APIC")
        front_pictures = [
            picture for picture in pictures if picture.type == PictureType.COVER_FRONT
        ]
        if len(front_pictures) == 1 and front_pictures[0].data == artwork.data:
            return EmbedResult("unchanged", format_name, "identical front cover already embedded")
        if front_pictures and not replace_existing:
            return EmbedResult("skipped", format_name, "front cover already exists")
        version = 3 if selected.tags.version[1] == 3 else 4
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
                del selected.tags[picture.HashKey]
            selected.tags.add(
                APIC(
                    encoding=0 if version == 3 else 3,
                    mime=artwork.mime,
                    type=PictureType.COVER_FRONT,
                    desc=description,
                    data=artwork.data,
                )
            )
            if is_mp3:
                _save_mutagen(selected, source, v2_version=version, v1=0)
                _restore_id3v1(source, original_id3v1)
            else:
                _save_mutagen(selected, source, v2_version=version)
            verified = _load_mutagen(source)
        except (OSError, mutagen.MutagenError) as exc:
            raise EmbedError(f"failed to write {format_name} artwork to {source}: {exc}") from exc
        assert verified is not None and verified.tags is not None
        written = [
            picture
            for picture in verified.tags.getall("APIC")
            if picture.type == PictureType.COVER_FRONT
        ]
        if len(written) != 1 or hashlib.sha256(written[0].data).hexdigest() != artwork.sha256:
            raise EmbedError(f"{format_name} artwork verification failed for {source}")
        return EmbedResult("embedded", format_name, "front cover embedded and verified")
