"""Xiph picture adapter for Ogg Vorbis and Opus."""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Iterable
from pathlib import Path
from typing import cast

import mutagen
from mutagen.flac import Picture
from mutagen.id3 import PictureType
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

from ..models import Artwork, EmbedError, EmbedResult
from ..mutagen_io import _load_mutagen, _save_mutagen
from .base import FormatAdapter


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


class XiphAdapter(FormatAdapter):
    format_name = "Xiph"
    audio_types = (OggOpus, OggVorbis)

    def front_pictures(
        self,
        audio: object,
        source: Path | int,
        display_path: Path,
    ) -> list[bytes]:
        del source, display_path
        selected = cast(OggOpus | OggVorbis, audio)
        assert selected.tags is not None
        parsed = _parse_xiph_pictures(selected.tags.get("metadata_block_picture", []))
        if any(picture is None for _, picture in parsed):
            raise EmbedError("malformed METADATA_BLOCK_PICTURE; refusing to modify")
        if selected.tags.get("coverart") or selected.tags.get("coverartmime"):
            raise EmbedError("legacy COVERART fields have no picture role; refusing to modify")
        return [
            picture.data
            for _, picture in parsed
            if picture is not None and picture.type == PictureType.COVER_FRONT
        ]

    def embed(
        self,
        audio: object,
        source: Path | int,
        artwork: Artwork,
        *,
        replace_existing: bool,
    ) -> EmbedResult:
        selected = cast(OggOpus | OggVorbis, audio)
        if selected.tags is None:
            selected.add_tags()
        assert selected.tags is not None
        parsed = _parse_xiph_pictures(selected.tags.get("metadata_block_picture", []))
        if any(picture is None for _, picture in parsed):
            raise EmbedError(f"malformed METADATA_BLOCK_PICTURE in {source}; refusing to modify")
        front_pictures = [
            picture
            for _, picture in parsed
            if picture is not None and picture.type == PictureType.COVER_FRONT
        ]
        legacy_cover = list(selected.tags.get("coverart", []))
        if legacy_cover or selected.tags.get("coverartmime"):
            raise EmbedError(
                f"legacy COVERART fields in {source} have no picture role; refusing to modify"
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
            selected.tags["metadata_block_picture"] = [*preserved, encoded]
            _save_mutagen(selected, source)
            verified = _load_mutagen(source)
        except (OSError, mutagen.MutagenError) as exc:
            raise EmbedError(f"failed to write Xiph artwork to {source}: {exc}") from exc
        assert verified is not None and verified.tags is not None
        written = [
            picture
            for _, picture in _parse_xiph_pictures(verified.tags.get("metadata_block_picture", []))
            if picture is not None and picture.type == PictureType.COVER_FRONT
        ]
        if len(written) != 1 or hashlib.sha256(written[0].data).hexdigest() != artwork.sha256:
            raise EmbedError(f"Xiph artwork verification failed for {source}")
        return EmbedResult("embedded", "Xiph", "front cover embedded and verified")
