"""Registered metadata-format artwork adapters."""

from __future__ import annotations

from .base import FormatAdapter
from .flac import FLACAdapter
from .id3 import ID3Adapter
from .mp4 import MP4Adapter
from .wavpack import WavPackAdapter
from .xiph import XiphAdapter

ADAPTERS = (XiphAdapter(), MP4Adapter(), ID3Adapter(), WavPackAdapter(), FLACAdapter())


def adapter_for(audio: object) -> FormatAdapter:
    for adapter in ADAPTERS:
        if adapter.supports(audio):
            return adapter
    from ..models import EmbedError

    raise EmbedError(f"artwork embedding is not implemented for {type(audio).__name__}")


__all__ = (
    "ADAPTERS",
    "FLACAdapter",
    "FormatAdapter",
    "ID3Adapter",
    "MP4Adapter",
    "WavPackAdapter",
    "XiphAdapter",
    "adapter_for",
)
