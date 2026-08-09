import io
import json
import os
import shutil
import subprocess
from contextlib import suppress
from pathlib import Path

import pytest
from mutagen.apev2 import APEBinaryValue, APEv2
from mutagen.flac import FLAC
from mutagen.id3 import APIC, ID3, TIT2, PictureType
from mutagen.mp3 import MP3
from mutagen.wavpack import WavPack
from PIL import Image

import apple_artwork
from apple_artwork import (
    AlbumGroup,
    AppleCatalogClient,
    Artwork,
    ArtworkDownloader,
    ArtworkError,
    CatalogAlbum,
    CatalogTrack,
    EmbedError,
    TrackMetadata,
    choose_match,
    decode_artwork,
    embed_artwork,
    preflight_artwork,
    process_library,
    score_candidate,
)


def image_bytes(color: tuple[int, int, int] = (10, 20, 30), *, image_format: str = "PNG") -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (64, 64), color).save(output, format=image_format)
    return output.getvalue()


def artwork(color: tuple[int, int, int] = (10, 20, 30)) -> Artwork:
    return decode_artwork(image_bytes(color), "https://a5.mzstatic.com/cover.png")


def make_audio(
    path: Path,
    codec: str,
    *,
    duration: float = 0.5,
    title: str = "Old Song",
    album: str = "Old Album",
    artist: str = "Old Artist",
) -> None:
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
            str(duration),
            "-c:a",
            codec,
            "-metadata",
            f"title={title}",
            "-metadata",
            f"artist={artist}",
            "-metadata",
            f"album_artist={artist}",
            "-metadata",
            f"album={album}",
            "-metadata",
            "date=2020",
            "-metadata",
            "track=1/1",
            "-metadata",
            "disc=1/1",
            str(path),
        ],
        check=True,
    )


def candidate_for(track: TrackMetadata) -> CatalogAlbum:
    return CatalogAlbum(
        collection_id=101,
        album=track.album,
        artist=track.album_artist,
        release_year=track.year,
        artwork_url="https://a5.mzstatic.com/catalog.png",
        track_count=1,
        tracks=(
            CatalogTrack(
                track.title,
                track.artist,
                track.duration_ms,
                track.disc_number,
                track.track_number,
            ),
        ),
    )


class Response:
    def __init__(
        self,
        data: bytes,
        *,
        url: str,
        content_type: str,
    ) -> None:
        self.data = data
        self.url = url
        self.status_code = 200
        self.headers = {"Content-Type": content_type}

    def iter_content(self, chunk_size: int = 65_536):
        del chunk_size
        yield self.data

    def close(self) -> None:
        return None


class Session:
    def __init__(self, responses: list[Response]) -> None:
        self.responses = responses
        self.calls: list[str] = []
        self.headers: dict[str, str] = {}

    def get(self, url: str, **_kwargs: object) -> Response:
        self.calls.append(url)
        return self.responses.pop(0)


def test_report_overwrite_rejects_lexical_symlink_before_resolution(tmp_path: Path) -> None:
    root = tmp_path / "library"
    root.mkdir()
    victim = root / "victim.json"
    victim.write_text("do not replace", encoding="utf-8")
    report_link = root / "report.json"
    report_link.symlink_to(victim)

    with pytest.raises(ValueError, match=r"symlink|regular"):
        apple_artwork._prepare_report_destination(root, report_link, (), overwrite=True)

    assert report_link.is_symlink()
    assert victim.read_text(encoding="utf-8") == "do not replace"


def test_cache_rejects_a_symlinked_ancestor_without_writing_outside(tmp_path: Path) -> None:
    root = tmp_path / "library"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    cache_link = root / "cache"
    cache_link.symlink_to(outside, target_is_directory=True)
    downloader = ArtworkDownloader(cache_dir=cache_link, cdn_interval=0)
    url = "https://a5.mzstatic.com/cover.png"
    image_path, metadata_path = downloader._cache_paths(1, url, None)

    with pytest.raises((OSError, ValueError, ArtworkError), match=r"symlink|directory|cache"):
        downloader._save_cache(
            image_path,
            metadata_path,
            artwork(),
            collection_id=1,
            artwork_url=url,
            max_dimension=None,
        )

    assert list(outside.rglob("*")) == []


def test_discovery_rejects_hardlinks_before_metadata_can_leave_root(tmp_path: Path) -> None:
    root = tmp_path / "library"
    root.mkdir()
    external = tmp_path / "external.flac"
    external.write_bytes(b"private metadata")
    os.link(external, root / "linked.flac")

    assert apple_artwork.discover_audio_files(root) == []


def test_staging_copy_never_reopens_the_safe_tempfile_by_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "source.mp3"
    make_audio(path, "libmp3lame")
    victim = tmp_path / "victim.bin"
    original_victim = b"do not overwrite"
    victim.write_bytes(original_victim)
    real_copy2 = shutil.copy2

    def attacking_copy2(source: object, destination: object, *args: object, **kwargs: object):
        planted = Path(destination)  # type: ignore[arg-type]
        planted.unlink()
        planted.symlink_to(victim)
        return real_copy2(source, destination, *args, **kwargs)

    monkeypatch.setattr(shutil, "copy2", attacking_copy2)
    with suppress(EmbedError):
        embed_artwork(path, artwork(), replace_existing=True)

    assert victim.read_bytes() == original_victim


def test_scan_identity_change_aborts_before_download_or_commit(tmp_path: Path) -> None:
    path = tmp_path / "song.flac"
    make_audio(path, "flac")
    scanned = apple_artwork.read_track_metadata(path)
    assert scanned is not None
    candidate = candidate_for(scanned)

    class MutatingClient:
        def find_candidates(self, _group: AlbumGroup) -> list[CatalogAlbum]:
            changed = FLAC(path)
            changed["title"] = ["New Song"]
            changed["artist"] = ["New Artist"]
            changed["albumartist"] = ["New Artist"]
            changed["album"] = ["New Album"]
            changed.save()
            return [candidate]

    download_calls: list[object] = []

    class NeverDownload:
        def fetch(self, *_args: object, **_kwargs: object) -> Artwork:
            download_calls.append(object())
            raise AssertionError("changed source must be rejected before artwork download")

    report = process_library(
        tmp_path,
        apply=True,
        allow_short_releases=True,
        client=MutatingClient(),
        downloader=NeverDownload(),
        report_path=None,
        emit=lambda _line: None,
    )

    assert download_calls == []
    assert report["summary"]["files_embedded"] == 0
    assert report["summary"]["failed"] >= 1
    assert FLAC(path).pictures == []


def test_report_write_is_reserved_before_any_audio_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "song.flac"
    make_audio(path, "flac")
    track = apple_artwork.read_track_metadata(path)
    assert track is not None

    class FixedClient:
        def find_candidates(self, _group: AlbumGroup) -> list[CatalogAlbum]:
            return [candidate_for(track)]

    class FixedDownloader:
        def fetch(self, *_args: object, **_kwargs: object) -> Artwork:
            return artwork()

    monkeypatch.setattr(
        apple_artwork,
        "_write_json_report",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("report denied")),
    )

    with pytest.raises(PermissionError, match="report denied"):
        process_library(
            tmp_path,
            apply=True,
            allow_short_releases=True,
            client=FixedClient(),
            downloader=FixedDownloader(),
            report_path=tmp_path / "report.json",
            emit=lambda _line: None,
        )

    assert FLAC(path).pictures == []


def test_json_report_serializes_surrogateescaped_paths_safely(tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"

    apple_artwork._write_json_report(
        report_path,
        {"path": "bad\udcff.flac"},
    )

    payload = report_path.read_bytes()
    assert payload.isascii()
    assert json.loads(payload)["path"] == "bad\udcff.flac"


def test_postcommit_durability_failure_rolls_back_before_backup_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "durability.mp3"
    make_audio(path, "libmp3lame")
    monkeypatch.setattr(
        apple_artwork,
        "_fsync_directory_descriptor",
        lambda _descriptor: (_ for _ in ()).throw(OSError("simulated directory fsync EIO")),
    )

    with pytest.raises(EmbedError, match=r"fsync|stage|EIO"):
        embed_artwork(path, artwork(), replace_existing=True)

    final = MP3(path)
    assert final.tags is not None
    assert final.tags.getall("APIC") == []


def test_artwork_cache_pair_is_bound_to_requested_key(tmp_path: Path) -> None:
    first_bytes = image_bytes((1, 2, 3))
    second_bytes = image_bytes((4, 5, 6))
    session = Session(
        [
            Response(first_bytes, url="https://a5.mzstatic.com/a.png", content_type="image/png"),
            Response(second_bytes, url="https://a5.mzstatic.com/b.png", content_type="image/png"),
        ]
    )
    downloader = ArtworkDownloader(cache_dir=tmp_path / "cache", session=session, cdn_interval=0)
    url_a = "https://a5.mzstatic.com/a.png"
    url_b = "https://a5.mzstatic.com/b.png"
    assert downloader.fetch(1, url_a).data == first_bytes
    source_paths = downloader._cache_paths(1, url_a, None)
    target_paths = downloader._cache_paths(2, url_b, None)
    target_paths[0].parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_paths[0], target_paths[0])
    shutil.copyfile(source_paths[1], target_paths[1])

    assert downloader.fetch(2, url_b).data == second_bytes
    assert len(session.calls) == 2


def test_catalog_cache_envelope_is_bound_to_request_key(tmp_path: Path) -> None:
    first = json.dumps({"results": [{"collectionId": 1}]}).encode()
    second = json.dumps({"results": [{"collectionId": 2}]}).encode()
    session = Session(
        [
            Response(first, url=apple_artwork.ITUNES_SEARCH_URL, content_type="application/json"),
            Response(second, url=apple_artwork.ITUNES_SEARCH_URL, content_type="application/json"),
        ]
    )
    client = AppleCatalogClient(cache_dir=tmp_path / "cache", session=session, api_interval=0)
    params_a = {"term": "Album One", "country": "US", "entity": "album"}
    params_b = {"term": "Album Two", "country": "US", "entity": "album"}
    assert (
        client._request_results(apple_artwork.ITUNES_SEARCH_URL, params_a)[0]["collectionId"] == 1
    )
    source = client._cache_path(apple_artwork.ITUNES_SEARCH_URL, params_a)
    target = client._cache_path(apple_artwork.ITUNES_SEARCH_URL, params_b)
    shutil.copyfile(source, target)

    assert (
        client._request_results(apple_artwork.ITUNES_SEARCH_URL, params_b)[0]["collectionId"] == 2
    )
    assert len(session.calls) == 2


def test_unsupported_image_magic_is_rejected_before_pillow_decoder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unsupported = io.BytesIO()
    Image.new("RGB", (64, 64), (1, 2, 3)).save(unsupported, format="WEBP")
    calls: list[object] = []
    real_open = apple_artwork.Image.open

    def tracked_open(*args: object, **kwargs: object):
        calls.append(args[0])
        return real_open(*args, **kwargs)

    monkeypatch.setattr(apple_artwork.Image, "open", tracked_open)
    with pytest.raises(ArtworkError, match=r"JPEG|PNG|format"):
        decode_artwork(unsupported.getvalue(), "https://a5.mzstatic.com/unsupported.webp")

    assert calls == []


def test_complete_disc_topology_is_a_hard_match_gate() -> None:
    local = tuple(
        TrackMetadata(
            Path(f"{number}.flac"),
            f"Song {number}",
            "Artist",
            "Album",
            "Artist",
            2020,
            number,
            4,
            1,
            1,
            180_000 + number * 1_000,
        )
        for number in range(1, 5)
    )
    group = AlbumGroup("Album", "Artist", 2020, tuple(t.path for t in local), local)
    remote = CatalogAlbum(
        1,
        "Album",
        "Artist",
        2020,
        "https://a5.mzstatic.com/a.png",
        4,
        tuple(
            CatalogTrack(f"Song {number}", "Artist", 180_000 + number * 1_000, disc, track)
            for number, (disc, track) in enumerate(((1, 1), (1, 2), (2, 1), (2, 2)), 1)
        ),
    )

    score = score_candidate(group, remote)
    assert score.eligible is False
    assert "disc/track topology mismatch" in score.reasons
    assert choose_match(group, [remote]).status == "no_match"


def test_mixed_flac_leading_id3_front_art_is_refused_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "mixed.flac"
    make_audio(path, "flac")
    prefix = tmp_path / "front.id3"
    tags = ID3()
    tags.add(
        APIC(
            encoding=3,
            mime="image/png",
            type=PictureType.COVER_FRONT,
            desc="stale front",
            data=image_bytes((255, 0, 0)),
        )
    )
    tags.save(prefix, v2_version=4)
    path.write_bytes(prefix.read_bytes() + path.read_bytes())
    before = path.read_bytes()

    with pytest.raises(EmbedError, match=r"mixed|ID3|metadata"):
        embed_artwork(path, artwork(), replace_existing=True)

    assert path.read_bytes() == before


def test_mp3_with_apev2_front_art_is_refused_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "mixed.mp3"
    make_audio(path, "libmp3lame")
    ape = APEv2()
    ape["Cover Art (Front)"] = APEBinaryValue(b"old.png\0" + image_bytes((255, 0, 0)))
    ape["Keep"] = "unrelated"
    ape.save(path)
    before = path.read_bytes()

    with pytest.raises(EmbedError, match=r"APE|mixed|front"):
        embed_artwork(path, artwork(), replace_existing=True)

    assert path.read_bytes() == before


def test_staged_mp3_audio_payload_truncation_is_rejected_before_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "payload.mp3"
    make_audio(path, "libmp3lame", duration=5.0)
    before = path.read_bytes()
    real_embed = apple_artwork._embed_artwork_in_place

    def truncate_after_embedding(
        temporary: int,
        selected: Artwork,
        *,
        replace_existing: bool = False,
        display_path: Path | None = None,
    ):
        result = real_embed(
            temporary,
            selected,
            replace_existing=replace_existing,
            display_path=display_path,
        )
        with os.fdopen(os.dup(temporary), "rb") as handle:
            handle.seek(0)
            id3_size = ID3(handle).size
        os.ftruncate(temporary, id3_size + 2500)
        return result

    monkeypatch.setattr(apple_artwork, "_embed_artwork_in_place", truncate_after_embedding)

    with pytest.raises(EmbedError, match=r"audio|payload|metadata|preserv"):
        embed_artwork(path, artwork(), replace_existing=True)

    assert path.read_bytes() == before


@pytest.mark.parametrize(
    ("local_artist", "remote_artist"),
    [("The The", "The"), ("The", "The The")],
)
def test_artist_article_normalization_does_not_collapse_real_names(
    local_artist: str, remote_artist: str
) -> None:
    track = TrackMetadata(
        Path("one.flac"), "Song", local_artist, "Album", local_artist, 2020, 1, 1, 1, 1, 180_000
    )
    group = AlbumGroup("Album", local_artist, 2020, (track.path,), (track,))
    remote = CatalogAlbum(
        1,
        "Album",
        remote_artist,
        2020,
        "https://a5.mzstatic.com/a.png",
        1,
        (CatalogTrack("Song", remote_artist, 180_000, 1, 1),),
    )

    assert "artist mismatch" in score_candidate(group, remote, allow_short_releases=True).reasons


@pytest.mark.parametrize(
    ("local_album", "remote_album"),
    [
        ("Shared Album (Remastered 2009)", "Shared Album (Remastered 2010)"),
        ("Shared Album (2009 Digital Remaster)", "Shared Album (2010 Digital Remaster)"),
        ("Shared Album (20th Anniversary Edition)", "Shared Album (25th Anniversary Edition)"),
    ],
)
def test_asymmetric_edition_years_are_hard_conflicts(local_album: str, remote_album: str) -> None:
    local = tuple(
        TrackMetadata(
            Path(f"{n}.flac"),
            f"Song {n}",
            "Artist",
            local_album,
            "Artist",
            2020,
            n,
            3,
            1,
            1,
            180_000 + n,
        )
        for n in range(1, 4)
    )
    group = AlbumGroup(local_album, "Artist", 2020, tuple(t.path for t in local), local)
    remote = CatalogAlbum(
        1,
        remote_album,
        "Artist",
        2020,
        "https://a5.mzstatic.com/a.png",
        3,
        tuple(CatalogTrack(f"Song {n}", "Artist", 180_000 + n, 1, n) for n in range(1, 4)),
    )

    assert "edition/version conflict" in score_candidate(group, remote).reasons


def test_audio_only_mp4_is_supported_after_container_validation(tmp_path: Path) -> None:
    path = tmp_path / "audio-only.mp4"
    make_audio(path, "aac")

    result = embed_artwork(path, artwork(), replace_existing=True)

    assert result.status == "embedded"
    assert result.format == "MP4"


def test_mixed_flac_is_blocked_before_any_catalog_request(tmp_path: Path) -> None:
    path = tmp_path / "mixed.flac"
    make_audio(path, "flac")
    prefix_file = tmp_path / "prefix.id3"
    tags = ID3()
    tags.add(APIC(mime="image/png", type=3, desc="front", data=image_bytes((1, 2, 3))))
    tags.save(prefix_file)
    path.write_bytes(prefix_file.read_bytes() + path.read_bytes())

    calls: list[AlbumGroup] = []

    class Client:
        def find_candidates(self, group: AlbumGroup) -> list[CatalogAlbum]:
            calls.append(group)
            return []

    report = process_library(
        tmp_path,
        client=Client(),
        downloader=object(),
        report_path=None,
        allow_short_releases=True,
    )

    assert calls == []
    assert report["albums"][0]["status"] == "preflight_failed"
    assert report["summary"]["adapter_preflight_failures"] == 1


def test_library_root_with_symlinked_ancestor_is_rejected(tmp_path: Path) -> None:
    actual_root = tmp_path / "actual" / "music"
    actual_root.mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path / "actual", target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        process_library(
            alias / "music",
            client=object(),
            downloader=object(),
            report_path=None,
        )


def test_audio_directory_symlink_swap_is_rejected_before_metadata_disclosure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "library"
    album_dir = root / "Album"
    source = album_dir / "song.mp3"
    make_audio(source, "libmp3lame", title="Inside", album="Inside Album")
    outside_dir = tmp_path / "outside"
    outside = outside_dir / "song.mp3"
    make_audio(outside, "libmp3lame", title="SECRET-OUTSIDE", album="Secret Album")
    outside_before = outside.read_bytes()
    hidden = root / "hidden-original"
    real_discover = apple_artwork.discover_audio_files

    def swapping_discover(scan_root: Path) -> list[Path]:
        paths = real_discover(scan_root)
        album_dir.rename(hidden)
        album_dir.symlink_to(outside_dir, target_is_directory=True)
        return paths

    catalog_calls: list[str] = []

    class Client:
        def find_candidates(self, group: AlbumGroup) -> list[CatalogAlbum]:
            catalog_calls.append(group.album)
            return [candidate_for(group.logical_tracks[0])]

    class Downloader:
        def fetch(self, *_args: object, **_kwargs: object) -> Artwork:
            return artwork()

    monkeypatch.setattr(apple_artwork, "discover_audio_files", swapping_discover)

    report = process_library(
        root,
        apply=True,
        client=Client(),
        downloader=Downloader(),
        report_path=None,
        allow_short_releases=True,
    )

    assert catalog_calls == []
    assert report["summary"]["files_embedded"] == 0
    assert outside.read_bytes() == outside_before


def test_staged_audio_is_never_reopened_by_mutable_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.mp3"
    victim = tmp_path / "victim.mp3"
    make_audio(source, "libmp3lame", title="Source")
    make_audio(victim, "libmp3lame", title="Victim")
    victim_before = victim.read_bytes()
    real_mutagen_file = apple_artwork.mutagen.File
    staged_path_opens = 0

    def swapping_file(filething: object, *args: object, **kwargs: object) -> object:
        nonlocal staged_path_opens
        if isinstance(filething, (str, os.PathLike)):
            candidate = Path(filething)
            if ".artwork-" in candidate.name:
                staged_path_opens += 1
                if staged_path_opens == 2:
                    candidate.unlink()
                    candidate.symlink_to(victim)
        return real_mutagen_file(filething, *args, **kwargs)

    monkeypatch.setattr(apple_artwork.mutagen, "File", swapping_file)

    result = embed_artwork(source, artwork(), replace_existing=True)

    assert result.status == "embedded"
    assert staged_path_opens == 0
    assert victim.read_bytes() == victim_before


def test_commit_is_compare_and_swap_and_preserves_concurrent_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "cas.mp3"
    make_audio(source, "libmp3lame", title="Original")
    real_exchange = apple_artwork._rename_exchange
    raced = False

    def racing_exchange(
        first_directory: int,
        first_name: str,
        second_directory: int,
        second_name: str,
    ) -> None:
        nonlocal raced
        if not raced and second_name == source.name:
            raced = True
            concurrent = MP3(source)
            assert concurrent.tags is not None
            concurrent.tags.setall("TIT2", [TIT2(encoding=3, text=["Concurrent edit"])])
            concurrent.save()
        real_exchange(first_directory, first_name, second_directory, second_name)

    monkeypatch.setattr(apple_artwork, "_rename_exchange", racing_exchange)

    with pytest.raises(EmbedError, match=r"changed|concurrent|compare"):
        embed_artwork(source, artwork(), replace_existing=True)

    final = MP3(source)
    assert final.tags is not None
    assert final.tags.getall("TIT2")[0].text == ["Concurrent edit"]
    assert final.tags.getall("APIC") == []


@pytest.mark.parametrize("apply", [False, True])
def test_dry_run_and_apply_preserve_source_atime_and_mtime(tmp_path: Path, apply: bool) -> None:
    root = tmp_path / "library"
    path = root / "Album" / "track.mp3"
    make_audio(path, "libmp3lame", title="Song", album="Album", artist="Artist")
    atime_ns = 1_600_000_000_123_456_789
    mtime_ns = 1_600_000_100_987_654_321
    os.utime(path, ns=(atime_ns, mtime_ns))

    class Client:
        def find_candidates(self, group: AlbumGroup) -> list[CatalogAlbum]:
            return [candidate_for(group.logical_tracks[0])]

    class Downloader:
        def fetch(
            self,
            _collection_id: int,
            _artwork_url: str,
            *,
            max_dimension: int | None = None,
            refresh: bool = False,
        ) -> Artwork:
            del max_dimension, refresh
            return artwork()

    report = process_library(
        root,
        client=Client(),
        downloader=Downloader(),
        report_path=None,
        allow_short_releases=True,
        apply=apply,
        replace_existing=True,
    )

    final = path.stat()
    assert final.st_atime_ns == atime_ns
    assert final.st_mtime_ns == mtime_ns
    assert report["summary"]["files_embedded"] == (1 if apply else 0)


def test_keyboard_interrupt_after_backup_release_is_explicitly_committed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "committed-interrupt.mp3"
    make_audio(path, "libmp3lame")
    real_fsync = apple_artwork._fsync_directory_descriptor
    calls = 0

    def interrupt_second_fsync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt
        real_fsync(descriptor)

    monkeypatch.setattr(
        apple_artwork,
        "_fsync_directory_descriptor",
        interrupt_second_fsync,
    )

    with pytest.raises(KeyboardInterrupt) as raised:
        embed_artwork(path, artwork(), replace_existing=True)

    assert type(raised.value).__name__ == "EmbedCommittedInterrupt"
    final = MP3(path)
    assert final.tags is not None
    assert final.tags.getall("APIC")


def test_committed_interrupt_updates_report_before_propagating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "library"
    path = root / "Album" / "track.mp3"
    report_path = root / "interrupt-report.json"
    make_audio(path, "libmp3lame", title="Song", album="Album", artist="Artist")

    class Client:
        def find_candidates(self, group: AlbumGroup) -> list[CatalogAlbum]:
            return [candidate_for(group.logical_tracks[0])]

    class Downloader:
        def fetch(
            self,
            _collection_id: int,
            _artwork_url: str,
            *,
            max_dimension: int | None = None,
            refresh: bool = False,
        ) -> Artwork:
            del max_dimension, refresh
            return artwork()

    real_fsync = apple_artwork._fsync_directory_descriptor
    calls = 0

    def interrupt_second_fsync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt
        real_fsync(descriptor)

    monkeypatch.setattr(
        apple_artwork,
        "_fsync_directory_descriptor",
        interrupt_second_fsync,
    )

    with pytest.raises(KeyboardInterrupt):
        process_library(
            root,
            client=Client(),
            downloader=Downloader(),
            report_path=report_path,
            allow_short_releases=True,
            apply=True,
            replace_existing=True,
        )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "interrupted_committed"
    assert report["summary"]["files_embedded"] == 1
    assert report["albums"][0]["status"] == "interrupted_committed"
    assert report["albums"][0]["file_results"][0]["status"] == "committed_interrupted"


def test_main_reports_committed_interrupt_truthfully(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def interrupted_process(*_args: object, **_kwargs: object) -> object:
        raise apple_artwork.EmbedCommittedInterrupt(
            "committed",
            apple_artwork.EmbedResult("embedded", "MP3", "embedded front cover"),
            Path("album/track.mp3"),
        )

    monkeypatch.setattr(apple_artwork, "process_library", interrupted_process)
    assert apple_artwork.main([".", "--apply", "--no-report"]) == 130
    stderr = capsys.readouterr().err
    assert "after artwork was committed" in stderr
    assert "no in-progress staged file was committed" not in stderr


def test_wavpack_nonterminal_hidden_apev2_store_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "hidden-ape.wv"
    make_audio(path, "wavpack")
    audio = WavPack(path)
    audio["Title"] = "Visible before trailer"
    audio.save()
    with path.open("ab") as handle:
        handle.write(b"NONTERMINAL-TRAILER")
    before = path.read_bytes()
    assert b"APETAGEX" in before

    with pytest.raises(EmbedError, match=r"APE|non-terminal|ambiguous"):
        preflight_artwork(path, artwork(), replace_existing=True)
    with pytest.raises(EmbedError, match=r"APE|non-terminal|ambiguous"):
        embed_artwork(path, artwork(), replace_existing=True)

    assert path.read_bytes() == before


def test_dependency_floors_exclude_reviewed_vulnerable_versions() -> None:
    pyproject = (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    requirements = (Path(__file__).parents[1] / "requirements.txt").read_text(encoding="utf-8")
    requirements_dev = (Path(__file__).parents[1] / "requirements-dev.txt").read_text(
        encoding="utf-8"
    )

    assert "Pillow>=12.3.0,<13" in pyproject and "Pillow>=12.3.0,<13" in requirements
    assert "requests>=2.34.2,<3" in pyproject and "requests>=2.34.2,<3" in requirements
    assert "pytest>=9.0.3,<10" in pyproject and "pytest>=9.0.3,<10" in requirements_dev
    assert '"setuptools>=83"' in pyproject
