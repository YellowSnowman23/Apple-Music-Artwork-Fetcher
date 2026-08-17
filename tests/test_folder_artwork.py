import hashlib
from pathlib import Path

import pytest

import apple_music_artwork.folder_artwork as folder_module
from apple_music_artwork.folder_artwork import (
    album_artwork_directory,
    plan_folder_artwork,
    write_folder_artwork,
)
from apple_music_artwork.models import AlbumGroup, Artwork, TrackMetadata


def _group(path: Path, *, album: str = "Example Album") -> AlbumGroup:
    track = TrackMetadata(
        path,
        "Example Track",
        "Example Artist",
        album,
        "Example Artist",
        track_number=1,
        track_total=1,
    )
    return AlbumGroup(album, "Example Artist", 2024, (path,), (track,))


def _artwork(data: bytes, mime: str = "image/png") -> Artwork:
    return Artwork(
        data,
        mime,
        3000,
        3000,
        24,
        "https://a5.mzstatic.com/example",
        hashlib.sha256(data).hexdigest(),
    )


def test_writes_missing_native_folder_artwork(tmp_path: Path) -> None:
    audio = tmp_path / "Artist" / "Album" / "01.flac"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"audio")
    group = _group(audio)
    artwork = _artwork(b"\x89PNG\r\n\x1a\nsource bytes")

    plan = plan_folder_artwork(group, tmp_path, (group,), artwork, replace_existing=False)
    result = write_folder_artwork(plan, artwork)

    assert plan.status == "ready"
    assert result.status == "written"
    assert result.path == audio.parent / "cover.png"
    assert result.path.read_bytes() == artwork.data


def test_preserves_different_existing_cover_without_replace(tmp_path: Path) -> None:
    audio = tmp_path / "Artist" / "Album" / "01.flac"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"audio")
    existing = audio.parent / "cover.jpg"
    existing.write_bytes(b"existing")
    group = _group(audio)
    artwork = _artwork(b"\x89PNG\r\n\x1a\nnew")

    plan = plan_folder_artwork(group, tmp_path, (group,), artwork, replace_existing=False)
    result = write_folder_artwork(plan, artwork)

    assert result.status == "skipped"
    assert existing.read_bytes() == b"existing"
    assert not (audio.parent / "cover.png").exists()


def test_replace_can_switch_native_extension_after_new_cover_is_durable(tmp_path: Path) -> None:
    audio = tmp_path / "Artist" / "Album" / "01.flac"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"audio")
    old_cover = audio.parent / "cover.jpg"
    old_cover.write_bytes(b"existing")
    group = _group(audio)
    artwork = _artwork(b"\x89PNG\r\n\x1a\nnew")

    plan = plan_folder_artwork(group, tmp_path, (group,), artwork, replace_existing=True)
    result = write_folder_artwork(plan, artwork)

    assert result.status == "written"
    assert result.path == audio.parent / "cover.png"
    assert result.path.read_bytes() == artwork.data
    assert not old_cover.exists()


def test_never_selects_scan_root_or_a_directory_containing_another_group(
    tmp_path: Path,
) -> None:
    first_path = tmp_path / "Disc 1" / "01.flac"
    second_path = tmp_path / "Other Album" / "01.flac"
    first_path.parent.mkdir(parents=True)
    second_path.parent.mkdir(parents=True)
    first_path.write_bytes(b"first")
    second_path.write_bytes(b"second")
    root_spanning = AlbumGroup(
        "Combined",
        "Example Artist",
        2024,
        (first_path, second_path),
        (
            _group(first_path).logical_tracks[0],
            _group(second_path).logical_tracks[0],
        ),
    )
    with pytest.raises(ValueError, match=r"album root|not a unique child"):
        album_artwork_directory(root_spanning, tmp_path, (root_spanning,))

    nested = tmp_path / "Artist" / "Album" / "Disc 1" / "01.flac"
    nested_two = tmp_path / "Artist" / "Album" / "Disc 2" / "02.flac"
    other = tmp_path / "Artist" / "Album" / "Bonus" / "01.flac"
    nested.parent.mkdir(parents=True)
    nested_two.parent.mkdir(parents=True)
    other.parent.mkdir(parents=True)
    nested.write_bytes(b"nested")
    nested_two.write_bytes(b"nested two")
    other.write_bytes(b"other")
    first_track = _group(nested).logical_tracks[0]
    second_track = _group(nested_two).logical_tracks[0]
    first_group = AlbumGroup(
        "Album",
        "Example Artist",
        2024,
        (nested, nested_two),
        (first_track, second_track),
    )
    second_group = _group(other, album="Other")
    with pytest.raises(ValueError, match="different grouped release"):
        album_artwork_directory(first_group, tmp_path, (first_group, second_group))


@pytest.mark.parametrize("container_name", ["Disc 1", "CD01", "FLAC", "Hi-Res"])
def test_single_selected_disc_or_format_folder_still_uses_named_album_root(
    tmp_path: Path,
    container_name: str,
) -> None:
    audio = tmp_path / "Artist" / "Example Album" / container_name / "01.flac"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"audio")
    group = _group(audio)

    assert album_artwork_directory(group, tmp_path, (group,)) == (
        tmp_path / "Artist" / "Example Album"
    )


def test_identifier_bridged_sibling_folders_do_not_select_artist_directory(
    tmp_path: Path,
) -> None:
    first = tmp_path / "Artist" / "Physical Edition A" / "01.flac"
    second = tmp_path / "Artist" / "Physical Edition B" / "02.flac"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    first_track = _group(first).logical_tracks[0]
    second_track = _group(second).logical_tracks[0]
    group = AlbumGroup(
        "Canonical Album",
        "Example Artist",
        2024,
        (first, second),
        (first_track, second_track),
    )

    with pytest.raises(ValueError, match="span sibling directories"):
        album_artwork_directory(group, tmp_path, (group,))


def test_disc_folder_peels_to_related_album_folder_despite_provider_qualifier(
    tmp_path: Path,
) -> None:
    audio = tmp_path / "Artist" / "Example Album (Deluxe)" / "Disc 1" / "01.flac"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"audio")
    group = _group(audio, album="Example Album (Deluxe - Explicit)")

    assert album_artwork_directory(group, tmp_path, (group,)) == audio.parent.parent


def test_disc_folder_without_an_album_level_parent_fails_closed(tmp_path: Path) -> None:
    audio = tmp_path / "Artist" / "Disc 1" / "01.flac"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"audio")
    group = _group(audio, album="Unrelated Album")

    with pytest.raises(ValueError, match="does not identify"):
        album_artwork_directory(group, tmp_path, (group,))


def test_album_root_with_an_omitted_protected_path_fails_closed(tmp_path: Path) -> None:
    audio = tmp_path / "Artist" / "Example Album" / "01.flac"
    protected = tmp_path / "Artist" / "Example Album" / "DCC Gold" / "02.flac"
    audio.parent.mkdir(parents=True)
    protected.parent.mkdir(parents=True)
    audio.write_bytes(b"audio")
    protected.write_bytes(b"protected")
    group = _group(audio)

    with pytest.raises(ValueError, match="omitted protected"):
        album_artwork_directory(group, tmp_path, (group,), (protected,))


def test_same_extension_replace_refuses_an_ordinary_intervening_edit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audio = tmp_path / "Artist" / "Example Album" / "01.flac"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"audio")
    cover = audio.parent / "cover.png"
    cover.write_bytes(b"old cover")
    group = _group(audio)
    artwork = _artwork(b"\x89PNG\r\n\x1a\nnew cover")
    plan = plan_folder_artwork(group, tmp_path, (group,), artwork, replace_existing=True)
    atomic_write = folder_module._atomic_write_bytes

    def interleaved_write(path: Path, data: bytes, **kwargs: object) -> None:
        cover.write_bytes(b"ordinary concurrent edit")
        atomic_write(path, data, **kwargs)

    monkeypatch.setattr(folder_module, "_atomic_write_bytes", interleaved_write)

    with pytest.raises(OSError, match="changed after preflight"):
        write_folder_artwork(plan, artwork)
    assert cover.read_bytes() == b"ordinary concurrent edit"


def test_reports_when_folder_cover_was_written_before_a_durability_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audio = tmp_path / "Artist" / "Example Album" / "01.flac"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"audio")
    group = _group(audio)
    artwork = _artwork(b"\x89PNG\r\n\x1a\nnew cover")
    plan = plan_folder_artwork(group, tmp_path, (group,), artwork, replace_existing=False)

    def committed_then_failed(path: Path, data: bytes, **_kwargs: object) -> None:
        path.write_bytes(data)
        raise OSError("directory fsync failed")

    monkeypatch.setattr(folder_module, "_atomic_write_bytes", committed_then_failed)

    with pytest.raises(folder_module.FolderArtworkCommittedError, match="durability"):
        write_folder_artwork(plan, artwork)
    assert (audio.parent / "cover.png").read_bytes() == artwork.data
