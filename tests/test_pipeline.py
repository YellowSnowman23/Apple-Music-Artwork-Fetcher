import io
import json
import subprocess
from pathlib import Path

import pytest
from mutagen.flac import FLAC
from mutagen.id3 import PictureType
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from PIL import Image

import apple_artwork
import apple_music_artwork.cli as cli
import apple_music_artwork.pipeline as pipeline
from apple_artwork import (
    AlbumGroup,
    Artwork,
    CatalogAlbum,
    CatalogTrack,
    EmbedError,
    EmbedResult,
    TrackMetadata,
    decode_artwork,
    process_library,
)


def local_tracks(root: Path) -> tuple[TrackMetadata, ...]:
    return (
        TrackMetadata(
            root / "Odd" / "Depth" / "01.flac",
            "First Light",
            "Alpha",
            "Greatest Hits",
            "Alpha",
            2020,
            1,
            3,
            1,
            1,
            180_000,
        ),
        TrackMetadata(
            root / "Formats" / "FLAC" / "02.flac",
            "Home Again",
            "Alpha",
            "Greatest Hits",
            "Alpha",
            2020,
            2,
            3,
            1,
            1,
            200_000,
        ),
        TrackMetadata(
            root / "Formats" / "MP3" / "03.mp3",
            "Afterglow",
            "Alpha",
            "Greatest Hits",
            "Alpha",
            2020,
            3,
            3,
            1,
            1,
            225_000,
        ),
    )


def apple_album() -> CatalogAlbum:
    return CatalogAlbum(
        42,
        "Greatest Hits",
        "Alpha",
        2020,
        "https://is1-ssl.mzstatic.com/image/thumb/Music1/v4/a/b/c/source.jpg/100x100bb.jpg",
        3,
        (
            CatalogTrack("First Light", "Alpha", 180_100, 1, 1),
            CatalogTrack("Home Again", "Alpha", 199_900, 1, 2),
            CatalogTrack("Afterglow", "Alpha", 225_100, 1, 3),
        ),
    )


class FakeClient:
    def __init__(self, albums: list[CatalogAlbum]) -> None:
        self.albums = albums
        self.calls: list[AlbumGroup] = []

    def find_candidates(self, group: AlbumGroup) -> list[CatalogAlbum]:
        self.calls.append(group)
        return self.albums


class NeverDownload:
    def fetch(self, *_args: object, **_kwargs: object) -> Artwork:
        raise AssertionError("dry-run must not download artwork")


def test_process_library_dry_run_matches_but_never_downloads_or_embeds(
    tmp_path: Path, monkeypatch
) -> None:
    tracks = local_tracks(tmp_path)
    monkeypatch.setattr(pipeline, "discover_audio_files", lambda _root: [t.path for t in tracks])
    by_path = {track.path: track for track in tracks}
    monkeypatch.setattr(pipeline, "read_track_metadata", by_path.get)
    monkeypatch.setattr(
        pipeline,
        "embed_artwork",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("dry-run must not embed artwork")
        ),
    )
    preflighted: list[Path] = []

    def fake_preflight(path: Path, *_args: object, **_kwargs: object) -> EmbedResult:
        preflighted.append(path)
        return EmbedResult("ready", path.suffix, "safe")

    monkeypatch.setattr(pipeline, "preflight_artwork", fake_preflight)
    emitted: list[str] = []

    report = process_library(
        tmp_path,
        apply=False,
        client=FakeClient([apple_album()]),
        downloader=NeverDownload(),
        report_path=None,
        emit=emitted.append,
    )

    assert report["mode"] == "dry-run"
    assert report["summary"]["matched"] == 1
    assert report["summary"]["files_embedded"] == 0
    assert report["albums"][0]["status"] == "dry-run"
    assert report["albums"][0]["apple"]["collection_id"] == 42
    assert [result["status"] for result in report["albums"][0]["file_results"]] == [
        "ready",
        "ready",
        "ready",
    ]
    assert preflighted == sorted((track.path for track in tracks), key=str)
    assert any("DRY-RUN" in line for line in emitted)
    assert not any(line.startswith("VERBOSE ") for line in emitted)


def test_process_library_verbose_emits_progress_and_candidate_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracks = local_tracks(tmp_path)
    monkeypatch.setattr(
        pipeline, "discover_audio_files", lambda _root: [track.path for track in tracks]
    )
    by_path = {track.path: track for track in tracks}
    monkeypatch.setattr(pipeline, "read_track_metadata", by_path.get)
    monkeypatch.setattr(
        pipeline,
        "preflight_artwork",
        lambda path, *_args, **_kwargs: EmbedResult("ready", path.suffix, "safe"),
    )
    emitted: list[str] = []

    process_library(
        tmp_path,
        verbose=True,
        client=FakeClient([apple_album()]),
        downloader=NeverDownload(),
        report_path=None,
        emit=emitted.append,
    )

    assert any(line.startswith("VERBOSE SCAN ") for line in emitted)
    assert any(
        line == "VERBOSE DISCOVERY discovered=3 selected=3 dcc_omitted=0" for line in emitted
    )
    assert any(
        line == "VERBOSE ALBUM Alpha — Greatest Hits logical_tracks=3 files=3" for line in emitted
    )
    assert any(
        line == "VERBOSE CANDIDATE Alpha — Greatest Hits collection_id=42 "
        "eligible=true score=0.998 reasons=none"
        for line in emitted
    )
    assert sum(line.startswith("VERBOSE PREFLIGHT ") for line in emitted) == 3


@pytest.mark.parametrize(
    ("apply_dcc", "expected_relative"),
    [
        (False, ["Artist/Album/00 Intro.flac"]),
        (
            True,
            [
                "00 AF-AFZ/01.flac",
                "Artist/00 DCC-GZS/02.flac",
                "Artist/Album/00 Intro.flac",
            ],
        ),
    ],
)
def test_process_library_omits_00_folders_unless_apply_dcc_is_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    apply_dcc: bool,
    expected_relative: list[str],
) -> None:
    root = tmp_path / "00 Root"
    paths = [
        root / "00 AF-AFZ" / "01.flac",
        root / "Artist" / "00 DCC-GZS" / "02.flac",
        root / "Artist" / "Album" / "00 Intro.flac",
    ]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"disposable test audio")

    monkeypatch.setattr(pipeline, "discover_audio_files", lambda _root: paths)
    inspected: list[Path] = []

    def record_metadata(path: Path, **_kwargs: object) -> None:
        inspected.append(path)
        return None

    monkeypatch.setattr(pipeline, "read_track_metadata", record_metadata)

    report = process_library(
        root,
        apply_dcc=apply_dcc,
        client=object(),
        downloader=object(),
        report_path=None,
        emit=lambda _message: None,
    )

    assert report["summary"]["discovered_files"] == 3
    assert report["summary"]["selected_files"] == len(expected_relative)
    assert report["summary"]["dcc_omitted_files"] == (0 if apply_dcc else 2)
    assert [path.relative_to(root).as_posix() for path in inspected] == expected_relative


def test_apply_dcc_cli_flag_does_not_enable_mutation(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_process(root: Path, **kwargs: object) -> dict[str, object]:
        captured["root"] = root
        captured.update(kwargs)
        return {"summary": {"failed": 0}}

    monkeypatch.setattr(cli, "process_library", fake_process)

    exit_code = apple_artwork.main([str(tmp_path), "--apply-dcc", "--no-report"])

    assert exit_code == 0
    assert captured["apply"] is False
    assert captured["apply_dcc"] is True


@pytest.mark.parametrize("flag", ["-v", "--verbose"])
def test_verbose_cli_aliases_do_not_enable_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str
) -> None:
    captured: dict[str, object] = {}

    def fake_process(root: Path, **kwargs: object) -> dict[str, object]:
        captured["root"] = root
        captured.update(kwargs)
        return {"summary": {"failed": 0}}

    monkeypatch.setattr(cli, "process_library", fake_process)

    exit_code = apple_artwork.main([str(tmp_path), flag, "--no-report"])

    assert exit_code == 0
    assert captured["apply"] is False
    assert captured["verbose"] is True


def test_main_defaults_to_current_directory_and_dry_run(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    captured: dict[str, object] = {}

    def fake_process(root: Path, **kwargs: object) -> dict[str, object]:
        captured["root"] = root
        captured.update(kwargs)
        return {
            "summary": {
                "albums": 0,
                "matched": 0,
                "ambiguous": 0,
                "no_match": 0,
                "failed": 0,
                "files_embedded": 0,
            }
        }

    monkeypatch.setattr(cli, "process_library", fake_process)

    exit_code = apple_artwork.main([])

    assert exit_code == 0
    assert Path(captured["root"]).resolve() == tmp_path
    assert captured["apply"] is False
    assert captured["replace_existing"] is False
    assert captured["apply_dcc"] is False
    assert captured["verbose"] is False
    assert captured["report_path"] == Path("apple-artwork-report.json")


def make_tagged_audio(path: Path, codec: str, *, title: str, track_number: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=44100:cl=stereo",
            "-t",
            "0.5",
            "-c:a",
            codec,
            "-metadata",
            f"title={title}",
            "-metadata",
            "artist=Alpha",
            "-metadata",
            "album_artist=Alpha",
            "-metadata",
            "album=Pipeline Album",
            "-metadata",
            "date=2020",
            "-metadata",
            f"track={track_number}/3",
            "-metadata",
            "disc=1/1",
            str(path),
        ],
        check=True,
    )


def test_apply_pipeline_scans_matches_downloads_embeds_and_writes_report(
    tmp_path: Path,
) -> None:
    paths = (
        tmp_path / "Alpha" / "Pipeline Album" / "FLAC" / "01.flac",
        tmp_path / "Alpha" / "Pipeline Album" / "MP3" / "02.mp3",
        tmp_path / "Alpha" / "Pipeline Album" / "MultiFormat1" / "03.m4a",
    )
    for path, codec, title, number in zip(
        paths,
        ("flac", "libmp3lame", "aac"),
        ("First Light", "Home Again", "Afterglow"),
        (1, 2, 3),
        strict=True,
    ):
        make_tagged_audio(path, codec, title=title, track_number=number)

    local = tuple(apple_artwork.read_track_metadata(path) for path in paths)
    assert all(track is not None for track in local)
    typed_local = tuple(track for track in local if track is not None)
    candidate = CatalogAlbum(
        4242,
        "Pipeline Album",
        "Alpha",
        2020,
        "https://is1-ssl.mzstatic.com/image/thumb/Music1/v4/a/b/c/source.png/100x100bb.jpg",
        3,
        tuple(
            CatalogTrack(
                track.title,
                track.artist,
                track.duration_ms,
                track.disc_number,
                track.track_number,
            )
            for track in typed_local
        ),
    )
    image = io.BytesIO()
    Image.new("RGB", (64, 64), (12, 34, 56)).save(image, format="PNG")
    artwork = decode_artwork(image.getvalue(), "https://a5.mzstatic.com/master.png")

    class FixedDownloader:
        def fetch(self, *_args: object, **_kwargs: object) -> Artwork:
            return artwork

    report_path = tmp_path / "result.json"
    emitted: list[str] = []
    report = process_library(
        tmp_path,
        apply=True,
        verbose=True,
        client=FakeClient([candidate]),
        downloader=FixedDownloader(),
        report_path=report_path,
        emit=emitted.append,
    )

    assert report["summary"]["albums"] == 1
    assert report["summary"]["files_embedded"] == 3
    assert report["albums"][0]["status"] == "applied"
    assert any(
        line.startswith("VERBOSE ARTWORK collection_id=4242 mime=image/png dimensions=64x64")
        for line in emitted
    )
    assert sum(line.startswith("VERBOSE RESULT ") for line in emitted) == 3
    persisted = json.loads(report_path.read_text(encoding="utf-8"))
    assert persisted["mode"] == "apply"

    flac = FLAC(paths[0])
    assert [
        picture.data for picture in flac.pictures if picture.type == PictureType.COVER_FRONT
    ] == [artwork.data]
    mp3 = MP3(paths[1])
    assert mp3.tags is not None
    assert [
        picture.data
        for picture in mp3.tags.getall("APIC")
        if picture.type == PictureType.COVER_FRONT
    ] == [artwork.data]
    mp4 = MP4(paths[2])
    assert mp4.tags is not None
    assert [bytes(cover) for cover in mp4.tags["covr"]] == [artwork.data]


def test_apply_preflights_entire_album_before_any_embed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracks = local_tracks(tmp_path)
    monkeypatch.setattr(
        pipeline, "discover_audio_files", lambda _root: [track.path for track in tracks]
    )
    by_path = {track.path: track for track in tracks}
    monkeypatch.setattr(pipeline, "read_track_metadata", by_path.get)
    image = io.BytesIO()
    Image.new("RGB", (64, 64), (12, 34, 56)).save(image, format="PNG")
    selected_artwork = decode_artwork(image.getvalue(), "https://a5.mzstatic.com/master.png")

    class FixedDownloader:
        def fetch(self, *_args: object, **_kwargs: object) -> Artwork:
            return selected_artwork

    def fake_preflight(path: Path, *_args: object, **_kwargs: object) -> EmbedResult:
        if path.name == "03.mp3":
            raise EmbedError("unsupported tail")
        return EmbedResult("ready", path.suffix, "safe")

    embedded: list[Path] = []
    monkeypatch.setattr(pipeline, "preflight_artwork", fake_preflight)
    monkeypatch.setattr(
        pipeline,
        "embed_artwork",
        lambda path, *_args, **_kwargs: embedded.append(path),
    )

    report = process_library(
        tmp_path,
        apply=True,
        client=FakeClient([apple_album()]),
        downloader=FixedDownloader(),
        report_path=None,
        emit=lambda _line: None,
    )

    assert embedded == []
    assert report["albums"][0]["status"] == "preflight_failed"
    assert report["summary"]["file_failures"] == 1
    assert report["summary"]["failed"] == 1


def test_metadata_failures_are_visible_nonzero_and_terminal_safe(tmp_path: Path) -> None:
    bad = tmp_path / "bad\x1b]52;c;payload.mp3"
    bad.write_bytes(b"not audio")
    emitted: list[str] = []

    report = process_library(tmp_path, report_path=None, emit=emitted.append)

    assert report["summary"]["metadata_failures"] == 1
    assert report["summary"]["failed"] == 1
    assert len(report["errors"]) == 1
    assert any("ERROR" in line for line in emitted)
    assert all("\x1b" not in line for line in emitted)


def test_tag_text_is_bounded_and_control_characters_are_sanitized() -> None:
    assert apple_artwork._first_tag({"artist": ["Alpha\x1b]52;c;payload"]}, "artist") == (
        "Alpha ]52;c;payload"
    )
    assert apple_artwork._first_tag({"artist": ["x" * 5000]}, "artist") == ""


def test_main_prints_stable_low_confidence_and_metadata_failure_fields(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli,
        "process_library",
        lambda *_args, **_kwargs: {
            "summary": {
                "albums": 1,
                "matched": 0,
                "ambiguous": 0,
                "low_confidence": 1,
                "no_match": 0,
                "metadata_failures": 0,
                "failed": 0,
                "files_embedded": 0,
            }
        },
    )

    assert apple_artwork.main(["--no-report"]) == 0
    output = capsys.readouterr().out
    assert "low_confidence=1" in output
    assert "metadata_failures=0" in output
