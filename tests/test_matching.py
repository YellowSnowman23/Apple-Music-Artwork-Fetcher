from pathlib import Path

from apple_artwork import TrackMetadata, group_tracks, normalize_text


def test_normalize_text_handles_unicode_ampersands_and_punctuation() -> None:
    assert normalize_text("  Beyoncé & JAY‑Z!  ") == "beyonce and jay z"  # noqa: RUF001


def test_group_tracks_combines_duplicate_formats_without_using_folder_depth() -> None:
    tracks = [
        TrackMetadata(
            path=Path("Artist/Album/FLAC/01 Song.flac"),
            title="Song",
            artist="Artist",
            album="Album",
            album_artist="Artist",
            year=2020,
            track_number=1,
            track_total=1,
            disc_number=1,
            disc_total=1,
            duration_ms=180_000,
        ),
        TrackMetadata(
            path=Path("Artist/Album/MultiFormat2/MP3/01 Song.mp3"),
            title="Song",
            artist="Artist",
            album="Album",
            album_artist="Artist",
            year=2020,
            track_number=1,
            track_total=1,
            disc_number=1,
            disc_total=1,
            duration_ms=180_020,
        ),
    ]

    groups = group_tracks(tracks)

    assert len(groups) == 1
    assert len(groups[0].files) == 2
    assert len(groups[0].logical_tracks) == 1


def test_group_tracks_never_merges_same_album_title_from_different_artists() -> None:
    common = {
        "title": "Opening Track",
        "album": "Greatest Hits",
        "year": 2020,
        "track_number": 1,
        "duration_ms": 180_000,
    }
    tracks = [
        TrackMetadata(
            path=Path("Alpha/Greatest Hits/01.flac"),
            artist="Alpha",
            album_artist="Alpha",
            **common,
        ),
        TrackMetadata(
            path=Path("Beta/Greatest Hits/01.flac"),
            artist="Beta",
            album_artist="Beta",
            **common,
        ),
    ]

    groups = group_tracks(tracks)

    assert [group.album_artist for group in groups] == ["Alpha", "Beta"]
