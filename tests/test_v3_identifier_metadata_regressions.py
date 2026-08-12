from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from mutagen.flac import FLAC

import apple_artwork
from apple_artwork import TrackMetadata, read_track_metadata
from apple_music_artwork.constants import MAX_TAG_TEXT

RELEASE_ID = "12345678-1234-4abc-8def-123456789abc"
RECORDING_ID = "abcdefab-cdef-4abc-8def-abcdefabcdef"
OTHER_RECORDING_ID = "87654321-4321-4abc-8def-987654321abc"
BARCODE = "012345678905"


def _track(path: str, *, number: int = 1, title: str = "Song") -> TrackMetadata:
    return TrackMetadata(
        path=Path(path),
        title=title,
        artist="Tagged Artist",
        album="Tagged Album",
        album_artist="Tagged Artist",
        year=2024,
        track_number=number,
        track_total=2,
        disc_number=1,
        disc_total=1,
        duration_ms=100_000 + number,
    )


def _make_flac(path: Path) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=44100:cl=stereo",
            "-t",
            "0.1",
            "-c:a",
            "flac",
            str(path),
        ],
        check=True,
    )


def test_partially_identifier_tagged_album_remains_one_group() -> None:
    tagged = replace(
        _track("01.flac"),
        barcode=BARCODE,
        musicbrainz_release_id=RELEASE_ID,
        musicbrainz_recording_id=RECORDING_ID,
    )
    untagged = _track("02.flac", number=2, title="Second Song")

    groups = apple_artwork.group_tracks([untagged, tagged])

    assert len(groups) == 1
    assert groups[0].files == (Path("01.flac"), Path("02.flac"))
    assert len(groups[0].logical_tracks) == 2
    assert groups[0].barcode == BARCODE
    assert groups[0].musicbrainz_release_id == RELEASE_ID
    assert groups[0].musicbrainz_provenance_complete is False
    assert groups[0].identifier_conflicts == ()


def test_recording_mbid_collapses_duplicate_encodings_despite_title_presentation() -> None:
    flac = replace(
        _track("song.flac"),
        title="Song (Album Version)",
        musicbrainz_release_id=RELEASE_ID,
        musicbrainz_recording_id=RECORDING_ID,
    )
    mp3 = replace(flac, path=Path("song.mp3"), title="Song")

    group = apple_artwork.group_tracks([mp3, flac])[0]

    assert group.files == (Path("song.flac"), Path("song.mp3"))
    assert len(group.logical_tracks) == 1
    assert group.logical_tracks[0].musicbrainz_recording_id == RECORDING_ID
    assert group.identifier_conflicts == ()


def test_recording_mbid_enriches_an_otherwise_identical_untagged_duplicate() -> None:
    tagged = replace(
        _track("song.flac"),
        musicbrainz_release_id=RELEASE_ID,
        musicbrainz_recording_id=RECORDING_ID,
    )
    untagged_recording = replace(tagged, path=Path("song.mp3"), musicbrainz_recording_id=None)

    group = apple_artwork.group_tracks([untagged_recording, tagged])[0]

    assert len(group.logical_tracks) == 1
    assert group.logical_tracks[0].musicbrainz_recording_id == RECORDING_ID
    assert group.musicbrainz_provenance_complete is False


@pytest.mark.parametrize(
    "tagged_title",
    (
        "Song (Album Version)",
        "Song (feat. Alice)",
        "Song [Explicit]",
        "Song (Bonus Track)",
    ),
)
def test_recording_mbid_enriches_duplicate_with_provider_presentation(
    tagged_title: str,
) -> None:
    tagged = replace(
        _track("song.flac"),
        title=tagged_title,
        musicbrainz_release_id=RELEASE_ID,
        musicbrainz_recording_id=RECORDING_ID,
    )
    untagged_recording = replace(
        tagged,
        path=Path("song.mp3"),
        title="Song",
        musicbrainz_recording_id=None,
    )

    group = apple_artwork.group_tracks([untagged_recording, tagged])[0]

    assert len(group.logical_tracks) == 1
    assert group.logical_tracks[0].musicbrainz_recording_id == RECORDING_ID
    assert group.musicbrainz_provenance_complete is False


@pytest.mark.parametrize(
    "distinct_title",
    ("Song (Live)", "Song (Radio Edit)", "Song (Remix)"),
)
def test_semantic_track_versions_are_not_collapsed_as_duplicate_encodes(
    distinct_title: str,
) -> None:
    tagged = replace(
        _track("song.flac"),
        title=distinct_title,
        musicbrainz_release_id=RELEASE_ID,
        musicbrainz_recording_id=RECORDING_ID,
    )
    other = replace(
        tagged,
        path=Path("song.mp3"),
        title="Song",
        musicbrainz_recording_id=None,
    )

    group = apple_artwork.group_tracks([other, tagged])[0]

    assert len(group.logical_tracks) == 2


def test_different_feature_credits_at_one_position_are_not_collapsed() -> None:
    alice = _track("alice.flac", title="Song (feat. Alice)")
    bob = replace(alice, path=Path("bob.flac"), title="Song (feat. Bob)")

    for tracks in ([alice, bob], [bob, alice]):
        group = apple_artwork.group_tracks(tracks)[0]
        assert len(group.logical_tracks) == 2


def test_different_artist_feature_credits_at_one_position_are_not_collapsed() -> None:
    alice = replace(_track("alice.flac"), artist="Primary feat. Alice")
    bob = replace(alice, path=Path("bob.flac"), artist="Primary feat. Bob")

    for tracks in ([alice, bob], [bob, alice]):
        group = apple_artwork.group_tracks(tracks)[0]
        assert len(group.logical_tracks) == 2


def test_same_recording_mbid_at_two_album_positions_remains_two_tracks() -> None:
    first = replace(
        _track("first.flac"),
        track_number=1,
        musicbrainz_release_id=RELEASE_ID,
        musicbrainz_recording_id=RECORDING_ID,
    )
    reprise = replace(
        first,
        path=Path("reprise.flac"),
        track_number=2,
    )

    group = apple_artwork.group_tracks([first, reprise])[0]

    assert len(group.logical_tracks) == 2
    assert {track.track_number for track in group.logical_tracks} == {1, 2}
    assert group.identifier_conflicts == ()


def test_conflicting_recording_mbids_at_one_position_are_not_deduplicated() -> None:
    first = replace(
        _track("first.flac"),
        musicbrainz_release_id=RELEASE_ID,
        musicbrainz_recording_id=RECORDING_ID,
    )
    second = replace(
        first,
        path=Path("second.flac"),
        musicbrainz_recording_id=OTHER_RECORDING_ID,
    )

    group = apple_artwork.group_tracks([first, second])[0]

    assert len(group.logical_tracks) == 2
    assert group.identifier_conflicts == (
        "conflicting MusicBrainz recording MBIDs at one album track position",
    )


def test_identifier_reader_retains_valid_values_and_warns_for_mixed_bad_values(
    tmp_path: Path,
) -> None:
    path = tmp_path / "mixed-identifiers.flac"
    _make_flac(path)
    audio = FLAC(path)
    audio["title"] = "Song"
    audio["artist"] = "Tagged Artist"
    audio["album"] = "Tagged Album"
    audio["barcode"] = [BARCODE, "x" * (MAX_TAG_TEXT + 1), "\x01\x02"]
    audio["musicbrainz_albumid"] = [RELEASE_ID, "not-an-mbid"]
    audio["musicbrainz_trackid"] = ["\x01\x02"]
    audio.save()

    track = read_track_metadata(path)

    assert track is not None
    assert track.barcode == BARCODE
    assert track.musicbrainz_release_id == RELEASE_ID
    assert track.musicbrainz_recording_id is None
    assert track.identifier_warnings == (
        "malformed UPC/barcode tag ignored",
        "malformed MusicBrainz release MBID tag ignored",
        "malformed MusicBrainz recording MBID tag ignored",
    )
