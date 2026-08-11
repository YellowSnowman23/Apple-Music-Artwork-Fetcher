"""Application constants shared by package modules."""

ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
ITUNES_LOOKUP_URL = "https://itunes.apple.com/lookup"
VERSION = "2.1.4"

MAX_ARTWORK_BYTES = 128 * 1024 * 1024
MAX_API_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 64_000_000
MIN_ARTWORK_DIMENSION = 32
MAX_REDIRECTS = 5
MAX_RETRY_DELAY = 30.0
MAX_TAG_TEXT = 4096

AUDIO_EXTENSIONS = frozenset(
    {
        ".aif",
        ".aiff",
        ".flac",
        ".m4a",
        ".mp3",
        ".mp4",
        ".oga",
        ".ogg",
        ".opus",
        ".wav",
        ".wave",
        ".wv",
    }
)
