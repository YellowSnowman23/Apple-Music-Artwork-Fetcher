"""Shared interface for metadata-format artwork adapters."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..models import Artwork, EmbedResult


class FormatAdapter:
    """Inspect and replace front artwork for one metadata format family."""

    format_name = ""
    audio_types: tuple[type[Any], ...] = ()

    def supports(self, audio: object) -> bool:
        return isinstance(audio, self.audio_types)

    def result_format(self, audio: object) -> str:
        return self.format_name

    def front_pictures(
        self,
        audio: object,
        source: Path | int,
        display_path: Path,
    ) -> list[bytes]:
        raise NotImplementedError

    def embed(
        self,
        audio: object,
        source: Path | int,
        artwork: Artwork,
        *,
        replace_existing: bool,
    ) -> EmbedResult:
        raise NotImplementedError
