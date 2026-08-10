"""Descriptor-backed Mutagen loading and saving helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any, BinaryIO

import mutagen
from mutagen._util import FileThing

from .filesystem import _binary_source


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
