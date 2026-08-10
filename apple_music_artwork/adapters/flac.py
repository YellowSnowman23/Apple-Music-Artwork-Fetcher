"""Native FLAC picture-block adapter."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import cast

import mutagen
from mutagen.flac import FLAC, Picture
from mutagen.id3 import PictureType

from ..filesystem import _binary_source
from ..models import Artwork, EmbedError, EmbedResult
from ..mutagen_io import _load_mutagen_class, _save_mutagen
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


def _has_leading_id3(source: Path | int) -> bool:
    try:
        with _binary_source(source) as handle:
            return handle.read(3) == b"ID3"
    except OSError as exc:
        raise EmbedError(f"failed to inspect leading metadata in {source}: {exc}") from exc


class FLACAdapter(FormatAdapter):
    format_name = "FLAC"
    audio_types = (FLAC,)

    def front_pictures(
        self,
        audio: object,
        source: Path | int,
        display_path: Path,
    ) -> list[bytes]:
        del display_path
        selected = cast(FLAC, audio)
        if _has_leading_id3(source):
            raise EmbedError("mixed FLAC/leading-ID3 metadata is unsupported; refusing to modify")
        competing = {
            str(key).casefold()
            for key in (selected.tags.keys() if selected.tags is not None else [])
            if str(key).casefold() in {"metadata_block_picture", "coverart", "coverartmime"}
        }
        if competing:
            names = ", ".join(sorted(competing))
            raise EmbedError(
                f"competing FLAC picture metadata store(s) present ({names}); refusing to modify"
            )
        return [
            picture.data for picture in selected.pictures if picture.type == PictureType.COVER_FRONT
        ]

    def embed(
        self,
        audio: object,
        source: Path | int,
        artwork: Artwork,
        *,
        replace_existing: bool,
    ) -> EmbedResult:
        selected = cast(FLAC, audio)
        front_pictures = [
            picture for picture in selected.pictures if picture.type == PictureType.COVER_FRONT
        ]
        if len(front_pictures) == 1 and front_pictures[0].data == artwork.data:
            return EmbedResult("unchanged", "FLAC", "identical front cover already embedded")
        if front_pictures and not replace_existing:
            return EmbedResult("skipped", "FLAC", "front cover already exists")
        preserved = [
            picture for picture in selected.pictures if picture.type != PictureType.COVER_FRONT
        ]
        try:
            selected.clear_pictures()
            for picture in preserved:
                selected.add_picture(picture)
            selected.add_picture(_flac_picture_from_artwork(artwork))
            _save_mutagen(selected, source)
            verified = _load_mutagen_class(source, FLAC)
        except (OSError, mutagen.MutagenError) as exc:
            raise EmbedError(f"failed to write FLAC artwork to {source}: {exc}") from exc
        written = [
            picture for picture in verified.pictures if picture.type == PictureType.COVER_FRONT
        ]
        if len(written) != 1 or hashlib.sha256(written[0].data).hexdigest() != artwork.sha256:
            raise EmbedError(f"FLAC artwork verification failed for {source}")
        return EmbedResult("embedded", "FLAC", "front cover embedded and verified")
