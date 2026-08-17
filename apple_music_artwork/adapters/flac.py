"""Native FLAC picture-block adapter."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import cast

import mutagen
from mutagen.flac import FLAC, Picture
from mutagen.id3 import PictureType

from ..artwork import ArtworkDerivation, derive_embeddable_artwork
from ..filesystem import _binary_source
from ..models import Artwork, ArtworkError, EmbedError, EmbedResult
from ..mutagen_io import _load_mutagen_class, _save_mutagen
from .base import FormatAdapter

FLAC_PICTURE_BLOCK_MAX_BYTES = 0xFFFFFF


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


def _flac_picture_payload_size(artwork: Artwork) -> int:
    """Return the serialized PICTURE payload size, excluding its four-byte block header."""
    return len(_flac_picture_from_artwork(artwork).write())


def _flac_jpeg_data_limit(maximum_picture_bytes: int) -> int:
    empty_jpeg = Artwork(
        data=b"",
        mime="image/jpeg",
        width=1,
        height=1,
        depth=24,
        source_url="",
        sha256="",
    )
    return maximum_picture_bytes - _flac_picture_payload_size(empty_jpeg)


def derive_flac_artwork(
    artwork: Artwork,
    *,
    maximum_picture_bytes: int = FLAC_PICTURE_BLOCK_MAX_BYTES,
) -> ArtworkDerivation:
    """Derive the highest-quality variant that fits one legal FLAC PICTURE block."""
    if not 1 <= maximum_picture_bytes <= FLAC_PICTURE_BLOCK_MAX_BYTES:
        raise ArtworkError(
            f"FLAC PICTURE limit must be from 1 through {FLAC_PICTURE_BLOCK_MAX_BYTES}"
        )

    def fits(candidate: Artwork) -> bool:
        return _flac_picture_payload_size(candidate) <= maximum_picture_bytes

    derivation = derive_embeddable_artwork(
        artwork,
        fits=fits,
        target_data_bytes=_flac_jpeg_data_limit(maximum_picture_bytes),
    )
    payload_size = _flac_picture_payload_size(derivation.artwork)
    if payload_size > maximum_picture_bytes:
        raise ArtworkError(
            f"derived FLAC PICTURE payload is {payload_size} bytes; "
            f"maximum is {maximum_picture_bytes}"
        )
    return derivation


def _has_leading_id3(source: Path | int, display_path: Path) -> bool:
    try:
        with _binary_source(source) as handle:
            return handle.read(3) == b"ID3"
    except OSError as exc:
        raise EmbedError(f"failed to inspect leading metadata in {display_path}: {exc}") from exc


class FLACAdapter(FormatAdapter):
    format_name = "FLAC"
    audio_types = (FLAC,)

    def front_pictures(
        self,
        audio: object,
        source: Path | int,
        display_path: Path,
    ) -> list[bytes]:
        selected = cast(FLAC, audio)
        if _has_leading_id3(source, display_path):
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
        picture_payload_size = _flac_picture_payload_size(artwork)
        if picture_payload_size > FLAC_PICTURE_BLOCK_MAX_BYTES:
            raise EmbedError(
                f"FLAC front-cover PICTURE payload is {picture_payload_size} bytes; "
                f"maximum is {FLAC_PICTURE_BLOCK_MAX_BYTES}"
            )
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
            destination = source if isinstance(source, Path) else "the staged FLAC file"
            raise EmbedError(f"failed to write FLAC artwork to {destination}: {exc}") from exc
        written = [
            picture for picture in verified.pictures if picture.type == PictureType.COVER_FRONT
        ]
        if len(written) != 1 or hashlib.sha256(written[0].data).hexdigest() != artwork.sha256:
            destination = source if isinstance(source, Path) else "the staged FLAC file"
            raise EmbedError(f"FLAC artwork verification failed for {destination}")
        return EmbedResult("embedded", "FLAC", "front cover embedded and verified")
