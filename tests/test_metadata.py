import subprocess
from pathlib import Path

from mutagen.flac import FLAC

from apple_artwork import discover_audio_files, read_track_metadata


def make_flac(path: Path) -> None:
    path.parent.mkdir(parents=True)
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
    assert 200 <= (track.duration_ms or 0) <= 300


def test_discover_audio_files_is_recursive_and_structure_agnostic(tmp_path: Path) -> None:
    expected = [
        tmp_path / "Artist" / "Album" / "track.FLAC",
        tmp_path / "Odd" / "Format" / "Depth" / "track.m4a",
    ]
    for path in expected:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    (tmp_path / "Artist" / "Album" / "cover.jpg").touch()
    hidden = tmp_path / ".apple-artwork-cache" / "cached.flac"
    hidden.parent.mkdir()
    hidden.touch()

    assert discover_audio_files(tmp_path) == expected
