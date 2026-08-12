import errno
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from mutagen.aiff import AIFF
from mutagen.easymp4 import EasyMP4
from mutagen.flac import FLAC
from mutagen.id3 import TALB, TDRC, TIT2, TPE1, TPE2, TPOS, TRCK, TXXX, UFID
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from mutagen.wave import WAVE

import apple_artwork
from apple_artwork import TrackMetadata, discover_audio_files, read_track_metadata


def make_flac(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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
            "0.25",
            "-c:a",
            "flac",
            str(path),
        ],
        check=True,
    )


def make_m4a(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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
            "0.25",
            "-c:a",
            "aac",
            str(path),
        ],
        check=True,
    )


def make_audio(path: Path, codec: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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
            "0.25",
            "-c:a",
            codec,
            str(path),
        ],
        check=True,
    )


def add_picard_id3_tags(audio: MP3 | WAVE | AIFF, *, identifier_name: str) -> None:
    if audio.tags is None:
        audio.add_tags()
    assert audio.tags is not None
    audio.tags.add(TIT2(encoding=3, text=["Tagged Song"]))
    audio.tags.add(TPE1(encoding=3, text=["Tagged Artist"]))
    audio.tags.add(TPE2(encoding=3, text=["Tagged Artist"]))
    audio.tags.add(TALB(encoding=3, text=["Tagged Album"]))
    audio.tags.add(TRCK(encoding=3, text=["1/1"]))
    audio.tags.add(TPOS(encoding=3, text=["1/1"]))
    audio.tags.add(TDRC(encoding=3, text=["2024"]))
    audio.tags.add(TXXX(encoding=3, desc=identifier_name, text=["012345678905"]))
    audio.tags.add(
        TXXX(
            encoding=3,
            desc="MusicBrainz Album Id",
            text=["12345678-1234-4abc-8def-123456789abc"],
        )
    )
    audio.tags.add(
        UFID(
            owner="http://musicbrainz.org",
            data=b"abcdefab-cdef-4abc-8def-abcdefabcdef",
        )
    )
    audio.save()


def test_read_track_metadata_uses_tags_and_only_reads_audio_headers(tmp_path: Path) -> None:
    path = tmp_path / "Unexpected" / "Depth" / "song.flac"
    make_flac(path)
    audio = FLAC(path)
    audio["title"] = "Paranoid Android"
    audio["artist"] = "Radiohead"
    audio["albumartist"] = "Radiohead"
    audio["album"] = "OK Computer"
    audio["date"] = "1997-05-21"
    audio["tracknumber"] = "2/12"
    audio["discnumber"] = "1/1"
    audio["barcode"] = "724385522925"
    audio["musicbrainz_albumid"] = "12345678-1234-1234-1234-123456789abc"
    audio["musicbrainz_trackid"] = "abcdefab-cdef-4abc-8def-abcdefabcdef"
    audio.save()

    track = read_track_metadata(path)

    assert track is not None
    assert track.path == path
    assert track.title == "Paranoid Android"
    assert track.album_artist == "Radiohead"
    assert track.year == 1997
    assert (track.track_number, track.track_total) == (2, 12)
    assert (track.disc_number, track.disc_total) == (1, 1)
    assert track.barcode == "724385522925"
    assert track.musicbrainz_release_id == "12345678-1234-1234-1234-123456789abc"
    assert track.musicbrainz_recording_id == "abcdefab-cdef-4abc-8def-abcdefabcdef"
    assert 200 <= (track.duration_ms or 0) <= 300


def test_group_tracks_requires_musicbrainz_ids_on_every_physical_file() -> None:
    release_id = "12345678-1234-1234-1234-123456789abc"
    recording_id = "abcdefab-cdef-4abc-8def-abcdefabcdef"
    base = TrackMetadata(
        path=Path("01.flac"),
        title="Song",
        artist="Artist",
        album="Album",
        album_artist="Artist",
        year=2024,
        track_number=1,
        track_total=1,
        disc_number=1,
        disc_total=1,
        duration_ms=100_000,
        musicbrainz_release_id=release_id,
        musicbrainz_recording_id=recording_id,
    )

    complete = apple_artwork.group_tracks([base])[0]
    incomplete = apple_artwork.group_tracks(
        [base, replace(base, path=Path("duplicate.mp3"), musicbrainz_recording_id=None)]
    )[0]

    assert complete.musicbrainz_provenance_complete is True
    assert incomplete.musicbrainz_provenance_complete is False


def test_read_track_metadata_reads_picard_freeform_barcode_from_m4a(tmp_path: Path) -> None:
    path = tmp_path / "song.m4a"
    make_m4a(path)
    easy = EasyMP4(path)
    easy["title"] = ["Tagged Song"]
    easy["artist"] = ["Tagged Artist"]
    easy["albumartist"] = ["Tagged Artist"]
    easy["album"] = ["Tagged Album"]
    easy["tracknumber"] = ["1/1"]
    easy["musicbrainz_albumid"] = ["12345678-1234-4abc-8def-123456789abc"]
    easy["musicbrainz_trackid"] = ["abcdefab-cdef-4abc-8def-abcdefabcdef"]
    easy.save()
    raw = MP4(path)
    assert raw.tags is not None
    raw.tags["----:com.apple.iTunes:BARCODE"] = [b"012345678905"]
    raw.save()

    track = read_track_metadata(path)

    assert track is not None
    assert track.barcode == "012345678905"
    assert track.musicbrainz_release_id == "12345678-1234-4abc-8def-123456789abc"
    assert track.musicbrainz_recording_id == "abcdefab-cdef-4abc-8def-abcdefabcdef"


def test_read_track_metadata_reads_picard_upc_from_mp3(tmp_path: Path) -> None:
    path = tmp_path / "song.mp3"
    make_audio(path, "libmp3lame")
    add_picard_id3_tags(MP3(path), identifier_name="UPC")

    track = read_track_metadata(path)

    assert track is not None
    assert track.barcode == "012345678905"
    assert track.musicbrainz_release_id == "12345678-1234-4abc-8def-123456789abc"
    assert track.musicbrainz_recording_id == "abcdefab-cdef-4abc-8def-abcdefabcdef"


@pytest.mark.parametrize(
    ("suffix", "codec", "loader"),
    ((".wav", "pcm_s16le", WAVE), (".aiff", "pcm_s16be", AIFF)),
)
def test_read_track_metadata_converts_wave_and_aiff_id3_to_easy_tags(
    tmp_path: Path,
    suffix: str,
    codec: str,
    loader: type[WAVE] | type[AIFF],
) -> None:
    path = tmp_path / f"song{suffix}"
    make_audio(path, codec)
    add_picard_id3_tags(loader(path), identifier_name="BARCODE")

    track = read_track_metadata(path)

    assert track is not None
    assert track.title == "Tagged Song"
    assert track.album_artist == "Tagged Artist"
    assert track.album == "Tagged Album"
    assert (track.track_number, track.track_total) == (1, 1)
    assert track.year == 2024
    assert track.barcode == "012345678905"
    assert track.musicbrainz_release_id == "12345678-1234-4abc-8def-123456789abc"
    assert track.musicbrainz_recording_id == "abcdefab-cdef-4abc-8def-abcdefabcdef"


def test_group_tracks_connects_release_mbid_and_upc_transitively() -> None:
    release_id = "12345678-1234-4abc-8def-123456789abc"
    barcode = "012345678905"
    first = TrackMetadata(
        Path("01.flac"),
        "First",
        "Artist",
        "Album",
        "Artist",
        2024,
        1,
        2,
        1,
        1,
        100_000,
        barcode,
        release_id,
    )
    second = replace(
        first,
        path=Path("02.flac"),
        title="Second",
        track_number=2,
        musicbrainz_release_id=None,
    )

    groups = apple_artwork.group_tracks([first, second])

    assert len(groups) == 1
    assert groups[0].musicbrainz_release_id == release_id
    assert groups[0].barcode == barcode
    assert groups[0].identifier_conflicts == ()


def test_group_tracks_surfaces_transitively_connected_identifier_conflicts() -> None:
    first = TrackMetadata(
        Path("01.flac"),
        "First",
        "Artist",
        "Album",
        "Artist",
        2024,
        1,
        2,
        1,
        1,
        100_000,
        "012345678905",
        "12345678-1234-4abc-8def-123456789abc",
    )
    second = replace(
        first,
        path=Path("02.flac"),
        title="Second",
        track_number=2,
        musicbrainz_release_id="abcdefab-cdef-4abc-8def-abcdefabcdef",
    )

    group = apple_artwork.group_tracks([first, second])[0]

    assert group.musicbrainz_release_id is None
    assert group.barcode == "012345678905"
    assert group.identifier_conflicts == (
        "conflicting MusicBrainz release MBIDs within the album group",
    )


def test_group_tracks_does_not_silently_drop_conflicting_barcodes() -> None:
    release_id = "12345678-1234-4abc-8def-123456789abc"
    first = TrackMetadata(
        Path("01.flac"),
        "First",
        "Artist",
        "Album",
        "Artist",
        2024,
        1,
        2,
        1,
        1,
        100_000,
        "012345678905",
        release_id,
    )
    second = replace(
        first,
        path=Path("02.flac"),
        title="Second",
        track_number=2,
        barcode="4006381333931",
    )

    group = apple_artwork.group_tracks([first, second])[0]

    assert group.musicbrainz_release_id == release_id
    assert group.barcode is None
    assert group.identifier_conflicts == ("conflicting UPC/barcode tags within the album group",)


def test_group_tracks_detects_conflicting_bare_identifiers_from_same_album() -> None:
    first = TrackMetadata(
        Path("01.flac"),
        "First",
        "Artist",
        "Album",
        "Artist",
        2024,
        1,
        2,
        1,
        1,
        100_000,
        "012345678905",
    )
    second = replace(
        first,
        path=Path("02.flac"),
        title="Second",
        track_number=2,
        barcode="4006381333931",
    )

    groups = apple_artwork.group_tracks([first, second])

    assert len(groups) == 1
    assert groups[0].barcode is None
    assert groups[0].identifier_conflicts == (
        "conflicting UPC/barcode tags within the album group",
    )


def test_group_tracks_treats_equivalent_upca_and_ean13_widths_as_one_barcode() -> None:
    first = TrackMetadata(
        Path("01.flac"),
        "First",
        "Artist",
        "Album",
        "Artist",
        2024,
        1,
        2,
        1,
        1,
        100_000,
        "012345678905",
    )
    second = replace(
        first,
        path=Path("02.flac"),
        title="Second",
        track_number=2,
        barcode="0012345678905",
    )

    group = apple_artwork.group_tracks([first, second])[0]

    assert group.barcode in {"012345678905", "0012345678905"}
    assert group.identifier_conflicts == ()


def test_read_track_metadata_rejects_conflicting_identifier_values_within_one_file(
    tmp_path: Path,
) -> None:
    path = tmp_path / "conflict.flac"
    make_flac(path)
    audio = FLAC(path)
    audio["title"] = "Tagged Song"
    audio["artist"] = "Tagged Artist"
    audio["album"] = "Tagged Album"
    audio["barcode"] = ["012345678905", "4006381333931"]
    audio["musicbrainz_albumid"] = [
        "12345678-1234-4abc-8def-123456789abc",
        "abcdefab-cdef-4abc-8def-abcdefabcdef",
    ]
    audio.save()

    track = read_track_metadata(path)

    assert track is not None
    assert track.barcode is None
    assert track.musicbrainz_release_id is None
    assert track.identifier_warnings == (
        "conflicting UPC/barcode values within one file",
        "conflicting MusicBrainz release MBIDs within one file",
    )
    group = apple_artwork.group_tracks([track])[0]
    assert group.identifier_warnings == ()
    assert group.identifier_conflicts == track.identifier_warnings


def test_read_track_metadata_reports_malformed_identifier_tags(tmp_path: Path) -> None:
    path = tmp_path / "bad-tags.flac"
    make_flac(path)
    audio = FLAC(path)
    audio["title"] = "Tagged Song"
    audio["artist"] = "Tagged Artist"
    audio["album"] = "Tagged Album"
    audio["barcode"] = "not-a-upc"
    audio["musicbrainz_albumid"] = "not-an-mbid"
    audio["musicbrainz_trackid"] = "also-not-an-mbid"
    audio.save()

    track = read_track_metadata(path)

    assert track is not None
    assert track.barcode is None
    assert track.musicbrainz_release_id is None
    assert track.identifier_warnings == (
        "malformed UPC/barcode tag ignored",
        "malformed MusicBrainz release MBID tag ignored",
        "malformed MusicBrainz recording MBID tag ignored",
    )


def test_discover_audio_files_is_recursive_and_structure_agnostic(tmp_path: Path) -> None:
    expected = [
        tmp_path / "Artist" / "Album" / "track.FLAC",
        tmp_path / "Odd" / "Format" / "Depth" / "track.m4a",
    ]
    for path in expected:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    (tmp_path / "Artist" / "Album" / "cover.jpg").touch()
    (tmp_path / "Artist" / "Album" / "native.dsf").touch()
    (tmp_path / "Artist" / "Album" / "native.dff").touch()
    hidden = tmp_path / ".apple-artwork-cache" / "cached.flac"
    hidden.parent.mkdir()
    hidden.touch()

    assert discover_audio_files(tmp_path) == expected


def test_discovery_surfaces_journal_candidate_when_lstat_is_transiently_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audio = tmp_path / "song.flac"
    audio.touch()
    journal = tmp_path / ".song.flac.artwork-transaction-0123456789abcdef.json"
    journal.write_text("{}", encoding="ascii")
    real_lstat = Path.lstat

    def fail_only_for_journal(path: Path) -> object:
        if path == journal:
            raise OSError(errno.EIO, "transient SMB metadata failure")
        return real_lstat(path)

    monkeypatch.setattr(Path, "lstat", fail_only_for_journal)

    discovered = discover_audio_files(tmp_path)

    assert discovered == [audio]
    assert journal in discovered.transaction_journals
