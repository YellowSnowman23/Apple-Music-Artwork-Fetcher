#!/usr/bin/env python3
"""Compatibility imports and direct-script launcher.

New code should import :mod:`apple_music_artwork`. This module preserves the
original public API and ``python apple_artwork.py`` invocation.
"""

import time as time

import mutagen as mutagen
from PIL import Image as Image

from apple_music_artwork import *  # noqa: F403
from apple_music_artwork import __all__ as __all__
from apple_music_artwork.adapters.id3 import _apev2_tags as _apev2_tags
from apple_music_artwork.cli import main as main
from apple_music_artwork.constants import (
    ITUNES_LOOKUP_URL as ITUNES_LOOKUP_URL,
)
from apple_music_artwork.constants import (
    ITUNES_SEARCH_URL as ITUNES_SEARCH_URL,
)
from apple_music_artwork.embedding import (
    _embed_artwork_in_place as _embed_artwork_in_place,
)
from apple_music_artwork.embedding import (
    _fsync_directory_descriptor as _fsync_directory_descriptor,
)
from apple_music_artwork.embedding import (
    _rename_exchange as _rename_exchange,
)
from apple_music_artwork.metadata import (
    _first_tag as _first_tag,
)
from apple_music_artwork.metadata import (
    _number_pair as _number_pair,
)
from apple_music_artwork.reports import (
    _path_matches as _path_matches,
)
from apple_music_artwork.reports import (
    _prepare_report_destination as _prepare_report_destination,
)
from apple_music_artwork.reports import (
    _write_json_report as _write_json_report,
)

if __name__ == "__main__":
    raise SystemExit(main())
