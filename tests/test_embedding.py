import base64
import errno
import hashlib
import io
import json
import os
import subprocess
from pathlib import Path

import pytest
from mutagen.aiff import AIFF
from mutagen.apev2 import APEBinaryValue
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, TIT2, PictureType
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggopus import OggOpus
from mutagen.wave import WAVE
from mutagen.wavpack import WavPack
from PIL import Image

import apple_music_artwork.embedding as embedding
from apple_artwork import EmbedError, decode_artwork, embed_artwork


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


def transaction_names(target_name: str, marker: str) -> tuple[str, str, str, str]:
    transaction_token = hashlib.sha256(marker.encode("ascii")).hexdigest()[:32]
    staging_token = hashlib.sha256(f"staging:{marker}".encode("ascii")).hexdigest()[:32]
    suffix = Path(target_name).suffix
    return (
        f".{target_name}.artwork-{staging_token}{suffix}",
        f".{target_name}.artwork-backup-{transaction_token}{suffix}",
        f".{target_name}.artwork-original-{transaction_token}{suffix}",
        f".{target_name}.artwork-transaction-{transaction_token}.json",
    )


def image_bytes(color: tuple[int, int, int], image_format: str = "PNG") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (48, 48), color).save(buffer, format=image_format)
    return buffer.getvalue()


def flac_picture(data: bytes, picture_type: PictureType) -> Picture:
    picture = Picture()
    picture.type = picture_type
    picture.mime = "image/png"
    picture.width = 48
    picture.height = 48
    picture.depth = 24
    picture.data = data
    return picture


def test_staging_falls_back_when_nas_rejects_redundant_nofollow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        pytest.skip("platform does not expose O_NOFOLLOW")

    path = tmp_path / "song.flac"
    make_audio(path, "flac")
    replacement = decode_artwork(image_bytes((20, 40, 60), "JPEG"), source_url="test")
    real_open = os.open
    rejected_attempts = 0

    def nas_open(
        name: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal rejected_attempts
        if flags & os.O_CREAT and flags & os.O_EXCL and flags & nofollow:
            rejected_attempts += 1
            raise OSError(errno.EINVAL, "Invalid argument")
        return real_open(name, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(embedding.os, "open", nas_open)
    result = embed_artwork(path, replacement, replace_existing=True)

    assert result.status == "embedded"
    assert rejected_attempts == 1
    assert not list(tmp_path.glob(".*.artwork-*.flac"))
    embedded = FLAC(path).pictures
    assert len(embedded) == 1
    assert embedded[0].data == replacement.data


def test_cifs_without_rename_exchange_uses_journaled_noreplace_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "song.flac"
    make_audio(path, "flac")
    replacement = decode_artwork(image_bytes((80, 60, 40), "JPEG"), source_url="test")

    def unsupported_exchange(*_args: object) -> None:
        raise OSError(errno.EINVAL, "Invalid argument")

    monkeypatch.setattr(embedding, "_rename_exchange", unsupported_exchange)
    result = embed_artwork(path, replacement, replace_existing=True)

    assert result.status == "embedded"
    assert not list(tmp_path.glob(".*.artwork-*.flac"))
    embedded = FLAC(path).pictures
    assert len(embedded) == 1
    assert embedded[0].data == replacement.data


def test_cifs_fallback_restores_original_after_postcommit_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "song.flac"
    make_audio(path, "flac")
    original = path.read_bytes()
    replacement = decode_artwork(image_bytes((90, 70, 50), "JPEG"), source_url="test")
    real_fsync = embedding._fsync_directory_descriptor
    fsync_calls = 0

    def unsupported_exchange(*_args: object) -> None:
        raise OSError(errno.EINVAL, "Invalid argument")

    def fail_commit_directory_fsync(descriptor: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 3:
            raise OSError(errno.EIO, "simulated directory fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(embedding, "_rename_exchange", unsupported_exchange)
    monkeypatch.setattr(
        embedding,
        "_fsync_directory_descriptor",
        fail_commit_directory_fsync,
    )

    with pytest.raises(EmbedError, match=r"fsync|stage|I/O error"):
        embed_artwork(path, replacement, replace_existing=True)

    assert path.read_bytes() == original
    assert not list(tmp_path.glob(".*.artwork-*.flac"))


def test_cifs_fallback_retains_original_recovery_when_rollback_fsync_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "song.flac"
    make_audio(path, "flac")
    original = path.read_bytes()
    replacement = decode_artwork(image_bytes((100, 80, 60), "JPEG"), source_url="test")

    def unsupported_exchange(*_args: object) -> None:
        raise OSError(errno.EINVAL, "Invalid argument")

    real_fsync = embedding._fsync_directory_descriptor
    fsync_calls = 0

    def fail_commit_and_rollback_fsync(descriptor: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls in {3, 4}:
            raise OSError(errno.EIO, "fsync failed")
        real_fsync(descriptor)

    monkeypatch.setattr(embedding, "_rename_exchange", unsupported_exchange)
    monkeypatch.setattr(
        embedding,
        "_fsync_directory_descriptor",
        fail_commit_and_rollback_fsync,
    )

    with pytest.raises(embedding.EmbedCommittedError, match=r"rollback|durab|recovery"):
        embed_artwork(path, replacement, replace_existing=True)

    assert path.read_bytes() == original
    recovery_entries = list(tmp_path.glob(".*.artwork-*.flac"))
    assert len(recovery_entries) == 2
    original_entries = list(tmp_path.glob(".*.artwork-original-*.flac"))
    assert len(original_entries) == 1
    assert original_entries[0].read_bytes() == original
    assert len(list(tmp_path.glob(".*.artwork-transaction-*.json"))) == 1


def test_cifs_interrupt_after_atomic_replace_restores_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "song.flac"
    make_audio(path, "flac")
    original = path.read_bytes()
    replacement = decode_artwork(image_bytes((110, 90, 70), "JPEG"), source_url="test")
    real_noreplace = embedding._rename_noreplace
    noreplace_calls = 0

    def unsupported_exchange(*_args: object) -> None:
        raise OSError(errno.EINVAL, "Invalid argument")

    def rename_then_interrupt(*args: object) -> None:
        nonlocal noreplace_calls
        noreplace_calls += 1
        real_noreplace(*args)  # type: ignore[arg-type]
        if noreplace_calls == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(embedding, "_rename_exchange", unsupported_exchange)
    monkeypatch.setattr(embedding, "_rename_noreplace", rename_then_interrupt)

    with pytest.raises(KeyboardInterrupt):
        embed_artwork(path, replacement, replace_existing=True)

    assert path.read_bytes() == original
    assert not list(tmp_path.glob(".*.artwork-*.flac"))


def test_interrupt_after_rename_exchange_restores_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "song.flac"
    make_audio(path, "flac")
    original = path.read_bytes()
    replacement = decode_artwork(image_bytes((120, 100, 80), "JPEG"), source_url="test")
    real_exchange = embedding._rename_exchange
    exchange_calls = 0

    def exchange_then_interrupt(*args: object) -> None:
        nonlocal exchange_calls
        exchange_calls += 1
        real_exchange(*args)  # type: ignore[arg-type]
        if exchange_calls == 1:
            raise KeyboardInterrupt

    monkeypatch.setattr(embedding, "_rename_exchange", exchange_then_interrupt)

    with pytest.raises(KeyboardInterrupt):
        embed_artwork(path, replacement, replace_existing=True)

    assert path.read_bytes() == original
    assert not list(tmp_path.glob(".*.artwork-*.flac"))


def test_cifs_fallback_does_not_overwrite_concurrent_atomic_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "song.flac"
    concurrent = tmp_path / "concurrent.flac"
    make_audio(path, "flac")
    make_audio(concurrent, "flac")
    edited = FLAC(concurrent)
    edited["comment"] = "concurrent editor save"
    edited.save()
    concurrent_bytes = concurrent.read_bytes()
    replacement = decode_artwork(image_bytes((130, 110, 90), "JPEG"), source_url="test")
    real_descriptor_sha256 = embedding._descriptor_sha256
    real_replace = os.replace
    installed_concurrent_save = False

    def unsupported_exchange(*_args: object) -> None:
        raise OSError(errno.EINVAL, "Invalid argument")

    def hash_then_install_concurrent(descriptor: int) -> str:
        nonlocal installed_concurrent_save
        digest = real_descriptor_sha256(descriptor)
        if not installed_concurrent_save and os.fstat(descriptor).st_nlink == 2:
            installed_concurrent_save = True
            real_replace(concurrent, path)
        return digest

    monkeypatch.setattr(embedding, "_rename_exchange", unsupported_exchange)
    monkeypatch.setattr(embedding, "_descriptor_sha256", hash_then_install_concurrent)

    with pytest.raises(EmbedError, match=r"concurrent|transaction|commit") as caught:
        embed_artwork(path, replacement, replace_existing=True)

    assert not isinstance(caught.value, embedding.EmbedCommittedError)
    assert installed_concurrent_save
    assert path.read_bytes() == concurrent_bytes
    assert not list(tmp_path.glob(".*.artwork-*"))


def test_cifs_ambiguous_replace_retains_original_when_state_probe_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "song.flac"
    make_audio(path, "flac")
    original = path.read_bytes()
    replacement = decode_artwork(image_bytes((140, 120, 100), "JPEG"), source_url="test")
    real_noreplace = embedding._rename_noreplace
    real_optional_entry_stat = embedding._optional_entry_stat
    noreplace_calls = 0
    replacement_completed = False
    probe_failed = False

    def unsupported_exchange(*_args: object) -> None:
        raise OSError(errno.EINVAL, "Invalid argument")

    def rename_then_interrupt(*args: object) -> None:
        nonlocal noreplace_calls, replacement_completed
        noreplace_calls += 1
        real_noreplace(*args)  # type: ignore[arg-type]
        if noreplace_calls == 2:
            replacement_completed = True
            raise KeyboardInterrupt

    def transient_probe_failure(directory_descriptor: int, name: str) -> os.stat_result | None:
        nonlocal probe_failed
        if replacement_completed and not probe_failed:
            probe_failed = True
            raise OSError(errno.EIO, "transient CIFS stat failure")
        return real_optional_entry_stat(directory_descriptor, name)

    monkeypatch.setattr(embedding, "_rename_exchange", unsupported_exchange)
    monkeypatch.setattr(embedding, "_rename_noreplace", rename_then_interrupt)
    monkeypatch.setattr(embedding, "_optional_entry_stat", transient_probe_failure)

    try:
        embed_artwork(path, replacement, replace_existing=True)
    except embedding.EmbedCommittedInterrupt:
        pass
    except KeyboardInterrupt:
        pytest.fail("raw KeyboardInterrupt escaped without committed-state classification")
    else:
        pytest.fail("ambiguous completed replacement did not interrupt")

    assert probe_failed
    recovery_entries = list(tmp_path.glob(".*.artwork-backup-*.flac"))
    assert len(recovery_entries) == 1
    assert recovery_entries[0].read_bytes() == original
    assert len(list(tmp_path.glob(".*.artwork-original-*.flac"))) == 1
    assert len(list(tmp_path.glob(".*.artwork-transaction-*.json"))) == 1


def test_cifs_first_move_probe_interrupt_retains_recoverable_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "song.flac"
    make_audio(path, "flac")
    original = path.read_bytes()
    replacement = decode_artwork(image_bytes((142, 122, 102), "JPEG"), source_url="test")
    real_noreplace = embedding._rename_noreplace
    real_optional_entry_stat = embedding._optional_entry_stat
    first_move_completed = False
    probe_interrupted = False

    def unsupported_exchange(*_args: object) -> None:
        raise OSError(errno.EINVAL, "exchange unsupported")

    def first_move_then_io_error(*args: object) -> None:
        nonlocal first_move_completed
        real_noreplace(*args)  # type: ignore[arg-type]
        if not first_move_completed:
            first_move_completed = True
            raise OSError(errno.EIO, "ambiguous first move")

    def interrupt_first_probe(directory_descriptor: int, name: str) -> os.stat_result | None:
        nonlocal probe_interrupted
        if first_move_completed and not probe_interrupted:
            probe_interrupted = True
            raise KeyboardInterrupt
        return real_optional_entry_stat(directory_descriptor, name)

    monkeypatch.setattr(embedding, "_rename_exchange", unsupported_exchange)
    monkeypatch.setattr(embedding, "_rename_noreplace", first_move_then_io_error)
    monkeypatch.setattr(embedding, "_optional_entry_stat", interrupt_first_probe)

    with pytest.raises(embedding.EmbedCommittedInterrupt):
        embed_artwork(path, replacement, replace_existing=True)

    assert probe_interrupted
    assert list(tmp_path.glob(".*.artwork-transaction-*.json"))
    possible_originals = [path, *tmp_path.glob(".*.artwork-*.flac")]
    assert any(
        candidate.is_file() and candidate.read_bytes() == original
        for candidate in possible_originals
    )


def test_cifs_interrupt_during_rollback_remains_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "song.flac"
    make_audio(path, "flac")
    replacement = decode_artwork(image_bytes((150, 130, 110), "JPEG"), source_url="test")
    real_fsync = embedding._fsync_directory_descriptor
    real_noreplace = embedding._rename_noreplace
    fsync_calls = 0
    noreplace_calls = 0

    def unsupported_exchange(*_args: object) -> None:
        raise OSError(errno.EINVAL, "Invalid argument")

    def fail_commit_fsync(descriptor: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 3:
            raise OSError(errno.EIO, "commit fsync failed")
        real_fsync(descriptor)

    def interrupt_rollback_rename(*args: object) -> None:
        nonlocal noreplace_calls
        noreplace_calls += 1
        if noreplace_calls == 3:
            raise KeyboardInterrupt
        real_noreplace(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(embedding, "_rename_exchange", unsupported_exchange)
    monkeypatch.setattr(embedding, "_fsync_directory_descriptor", fail_commit_fsync)
    monkeypatch.setattr(embedding, "_rename_noreplace", interrupt_rollback_rename)

    try:
        embed_artwork(path, replacement, replace_existing=True)
    except embedding.EmbedCommittedInterrupt:
        pass
    except embedding.EmbedCommittedError:
        pytest.fail("rollback KeyboardInterrupt was reclassified as EmbedCommittedError")
    else:
        pytest.fail("rollback KeyboardInterrupt did not propagate")


def test_cifs_system_exit_survives_rollback_durability_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "song.flac"
    make_audio(path, "flac")
    original = path.read_bytes()
    replacement = decode_artwork(image_bytes((145, 125, 105), "JPEG"), source_url="test")
    real_noreplace = embedding._rename_noreplace
    real_fsync = embedding._fsync_directory_descriptor
    noreplace_calls = 0
    fsync_calls = 0

    def unsupported_exchange(*_args: object) -> None:
        raise OSError(errno.EINVAL, "exchange unsupported")

    def install_then_exit(*args: object) -> None:
        nonlocal noreplace_calls
        noreplace_calls += 1
        real_noreplace(*args)  # type: ignore[arg-type]
        if noreplace_calls == 2:
            raise SystemExit(77)

    def fail_rollback_fsync(descriptor: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 3:
            raise OSError(errno.EIO, "rollback directory fsync failed")
        real_fsync(descriptor)

    monkeypatch.setattr(embedding, "_rename_exchange", unsupported_exchange)
    monkeypatch.setattr(embedding, "_rename_noreplace", install_then_exit)
    monkeypatch.setattr(embedding, "_fsync_directory_descriptor", fail_rollback_fsync)

    with pytest.raises(SystemExit) as caught:
        embed_artwork(path, replacement, replace_existing=True)

    assert caught.value.code == 77
    assert path.read_bytes() == original
    assert list(tmp_path.glob(".*.artwork-transaction-*.json"))


@pytest.mark.parametrize(
    "secondary_failure",
    ["rollback-rename", "rollback-fsync", "cleanup-fsync"],
)
def test_cifs_primary_system_exit_outranks_secondary_keyboard_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    secondary_failure: str,
) -> None:
    path = tmp_path / "song.flac"
    make_audio(path, "flac")
    original = path.read_bytes()
    replacement = decode_artwork(image_bytes((142, 122, 102), "JPEG"), source_url="test")
    real_noreplace = embedding._rename_noreplace
    real_fsync = embedding._fsync_directory_descriptor
    noreplace_calls = 0
    fsync_calls = 0

    def unsupported_exchange(*_args: object) -> None:
        raise OSError(errno.EINVAL, "exchange unsupported")

    def install_then_terminate(*args: object) -> None:
        nonlocal noreplace_calls
        noreplace_calls += 1
        real_noreplace(*args)  # type: ignore[arg-type]
        if noreplace_calls == 2:
            raise SystemExit(77)
        if secondary_failure == "rollback-rename" and noreplace_calls == 3:
            raise KeyboardInterrupt

    def interrupt_secondary_fsync(descriptor: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        interrupt_call = 3 if secondary_failure == "rollback-fsync" else 4
        if secondary_failure != "rollback-rename" and fsync_calls == interrupt_call:
            raise KeyboardInterrupt
        real_fsync(descriptor)

    monkeypatch.setattr(embedding, "_rename_exchange", unsupported_exchange)
    monkeypatch.setattr(embedding, "_rename_noreplace", install_then_terminate)
    monkeypatch.setattr(embedding, "_fsync_directory_descriptor", interrupt_secondary_fsync)

    with pytest.raises(SystemExit) as caught:
        embed_artwork(path, replacement, replace_existing=True)

    assert caught.value.code == 77
    possible_originals = [path, *tmp_path.glob(".*.artwork-*.flac")]
    assert any(
        candidate.is_file() and candidate.read_bytes() == original
        for candidate in possible_originals
    )


def test_cifs_interrupt_after_backup_creation_does_not_leak_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "song.flac"
    make_audio(path, "flac")
    original = path.read_bytes()
    replacement = decode_artwork(image_bytes((160, 140, 120), "JPEG"), source_url="test")
    real_link = os.link
    link_completed = False

    def unsupported_exchange(*_args: object) -> None:
        raise OSError(errno.EINVAL, "Invalid argument")

    def link_then_interrupt(*args: object, **kwargs: object) -> None:
        nonlocal link_completed
        real_link(*args, **kwargs)  # type: ignore[arg-type]
        link_completed = True
        raise KeyboardInterrupt

    monkeypatch.setattr(embedding, "_rename_exchange", unsupported_exchange)
    monkeypatch.setattr(embedding.os, "link", link_then_interrupt)

    with pytest.raises(KeyboardInterrupt):
        embed_artwork(path, replacement, replace_existing=True)

    assert link_completed
    assert path.read_bytes() == original
    assert path.stat().st_nlink == 1
    assert not list(tmp_path.glob(".*.artwork-backup-*.flac"))


def test_exchange_fast_path_is_journal_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "song.flac"
    make_audio(path, "flac")
    replacement = decode_artwork(image_bytes((170, 150, 130), "JPEG"), source_url="test")

    def unexpected_fallback(*_args: object, **_kwargs: object) -> None:
        pytest.fail("local RENAME_EXCHANGE fast path entered the journaled fallback")

    monkeypatch.setattr(embedding, "_create_transaction_journal", unexpected_fallback)
    monkeypatch.setattr(embedding, "_rename_noreplace", unexpected_fallback)

    result = embed_artwork(path, replacement, replace_existing=True)

    assert result.status == "embedded"
    assert not list(tmp_path.glob(".*.artwork-*"))


def test_exchange_fast_path_restores_concurrent_atomic_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "song.flac"
    concurrent = tmp_path / "concurrent.flac"
    make_audio(path, "flac")
    make_audio(concurrent, "flac")
    edited = FLAC(concurrent)
    edited["comment"] = "concurrent editor save before exchange"
    edited.save()
    concurrent_bytes = concurrent.read_bytes()
    replacement = decode_artwork(image_bytes((175, 155, 135), "JPEG"), source_url="test")
    real_exchange = embedding._rename_exchange
    real_replace = os.replace
    injected = False

    def concurrent_then_exchange(*args: object) -> None:
        nonlocal injected
        if not injected:
            injected = True
            real_replace(concurrent, path)
        real_exchange(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(embedding, "_rename_exchange", concurrent_then_exchange)

    with pytest.raises(EmbedError, match=r"concurrent|changed|commit") as caught:
        embed_artwork(path, replacement, replace_existing=True)

    assert not isinstance(caught.value, embedding.EmbedCommittedError)
    assert injected
    assert path.read_bytes() == concurrent_bytes
    assert path.stat().st_nlink == 1
    assert not list(tmp_path.glob(".*.artwork-*"))


def test_cifs_concurrent_save_before_recovery_link_leaves_no_hidden_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "song.flac"
    concurrent = tmp_path / "concurrent.flac"
    make_audio(path, "flac")
    make_audio(concurrent, "flac")
    edited = FLAC(concurrent)
    edited["comment"] = "concurrent editor save before recovery link"
    edited.save()
    concurrent_bytes = concurrent.read_bytes()
    replacement = decode_artwork(image_bytes((165, 145, 125), "JPEG"), source_url="test")
    real_replace = os.replace
    injected = False

    def concurrent_then_unsupported(*_args: object) -> None:
        nonlocal injected
        if not injected:
            injected = True
            real_replace(concurrent, path)
        raise OSError(errno.EINVAL, "exchange unsupported")

    monkeypatch.setattr(embedding, "_rename_exchange", concurrent_then_unsupported)

    with pytest.raises(EmbedError, match=r"recovery link|source|changed"):
        embed_artwork(path, replacement, replace_existing=True)

    assert injected
    assert path.read_bytes() == concurrent_bytes
    assert path.stat().st_nlink == 1
    assert not list(tmp_path.glob(".*.artwork-*"))


def test_exchange_success_followed_by_unsupported_error_is_recognized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "song.flac"
    make_audio(path, "flac")
    original = path.read_bytes()
    replacement = decode_artwork(image_bytes((155, 135, 115), "JPEG"), source_url="test")
    real_exchange = embedding._rename_exchange
    called = False

    def exchange_then_unsupported(*args: object) -> None:
        nonlocal called
        real_exchange(*args)  # type: ignore[arg-type]
        if not called:
            called = True
            raise OSError(errno.EINVAL, "ambiguous post-success EINVAL")

    monkeypatch.setattr(embedding, "_rename_exchange", exchange_then_unsupported)

    with pytest.raises(EmbedError, match=r"completed.*error|safely restored"):
        embed_artwork(path, replacement, replace_existing=True)

    assert called
    assert path.read_bytes() == original
    assert path.stat().st_nlink == 1
    assert not list(tmp_path.glob(".*.artwork-*"))


def test_cifs_without_noreplace_support_fails_before_namespace_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "song.flac"
    make_audio(path, "flac")
    original = path.read_bytes()
    replacement = decode_artwork(image_bytes((160, 140, 120), "JPEG"), source_url="test")

    def unsupported_exchange(*_args: object) -> None:
        raise OSError(errno.EINVAL, "exchange unsupported")

    def unsupported_noreplace(*_args: object) -> None:
        raise OSError(errno.EOPNOTSUPP, "noreplace unsupported")

    monkeypatch.setattr(embedding, "_rename_exchange", unsupported_exchange)
    monkeypatch.setattr(embedding, "_rename_noreplace", unsupported_noreplace)

    with pytest.raises(EmbedError, match="noreplace unsupported"):
        embed_artwork(path, replacement, replace_existing=True)

    assert path.read_bytes() == original
    assert path.stat().st_nlink == 1
    assert not list(tmp_path.glob(".*.artwork-*"))


def test_cifs_journal_fsync_failure_precedes_namespace_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "song.flac"
    make_audio(path, "flac")
    original = path.read_bytes()
    replacement = decode_artwork(image_bytes((150, 130, 110), "JPEG"), source_url="test")

    def unsupported_exchange(*_args: object) -> None:
        raise OSError(errno.EINVAL, "exchange unsupported")

    def fail_directory_fsync(_descriptor: int) -> None:
        raise OSError(errno.EIO, "journal directory fsync failed")

    monkeypatch.setattr(embedding, "_rename_exchange", unsupported_exchange)
    monkeypatch.setattr(embedding, "_fsync_directory_descriptor", fail_directory_fsync)

    with pytest.raises(EmbedError, match="journal directory fsync failed"):
        embed_artwork(path, replacement, replace_existing=True)

    assert path.read_bytes() == original
    assert path.stat().st_nlink == 1
    assert not list(tmp_path.glob(".*.artwork-*"))


def test_recovery_restores_source_after_crash_between_noreplace_moves(
    tmp_path: Path,
) -> None:
    path = tmp_path / "song.flac"
    staging_name, backup_name, original_name, journal_name = transaction_names(
        path.name, "crash-between-moves"
    )
    staging = tmp_path / staging_name
    make_audio(path, "flac")
    make_audio(staging, "flac")
    staged_audio = FLAC(staging)
    staged_audio["comment"] = "staged update"
    staged_audio.save()
    original = path.read_bytes()
    directory_descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        source_info = path.stat()
        staging_info = staging.stat()
        source_inode = (source_info.st_dev, source_info.st_ino)
        staging_inode = (staging_info.st_dev, staging_info.st_ino)
        embedding._create_transaction_journal(
            directory_descriptor,
            journal_name,
            target_name=path.name,
            backup_name=backup_name,
            original_name=original_name,
            staging_name=staging_name,
            source_inode=source_inode,
            staging_inode=staging_inode,
            source_hash=hashlib.sha256(original).hexdigest(),
            staging_hash=hashlib.sha256(staging.read_bytes()).hexdigest(),
        )
        os.link(path, tmp_path / original_name)
        embedding._rename_noreplace(
            directory_descriptor,
            path.name,
            directory_descriptor,
            backup_name,
        )
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)

    recovered = embedding.recover_incomplete_transactions(tmp_path)

    assert recovered == (path,)
    assert path.read_bytes() == original
    assert path.stat().st_nlink == 1
    assert not list(tmp_path.glob(".*.artwork-*"))
    assert embedding.recover_incomplete_transactions(tmp_path) == ()


def test_transaction_journal_rejects_inconsistent_generated_name_tokens(
    tmp_path: Path,
) -> None:
    target_name = "song.flac"
    journal_name = f".{target_name}.artwork-transaction-{'a' * 32}.json"
    payload = {
        "schema": 1,
        "target": target_name,
        "backup": f".{target_name}.artwork-backup-{'b' * 32}.flac",
        "original": f".{target_name}.artwork-original-{'c' * 32}.flac",
        "staging": f".{target_name}.artwork-{'d' * 32}.flac",
        "source_inode": [1, 2],
        "staging_inode": [1, 3],
        "source_sha256": "e" * 64,
        "staging_sha256": "f" * 64,
    }
    (tmp_path / journal_name).write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
        encoding="ascii",
    )
    directory_descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(EmbedError, match=r"names do not match|unsafe.*name"):
            embedding._read_transaction_journal(directory_descriptor, journal_name)
    finally:
        os.close(directory_descriptor)


def test_recovery_rolls_back_unreported_installed_stage(tmp_path: Path) -> None:
    path = tmp_path / "song.flac"
    staging_name, backup_name, original_name, journal_name = transaction_names(
        path.name, "installed-stage"
    )
    staging = tmp_path / staging_name
    make_audio(path, "flac")
    make_audio(staging, "flac")
    staged_audio = FLAC(staging)
    staged_audio["comment"] = "installed but unreported"
    staged_audio.save()
    original = path.read_bytes()
    directory_descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        source_info = path.stat()
        staging_info = staging.stat()
        source_inode = (source_info.st_dev, source_info.st_ino)
        staging_inode = (staging_info.st_dev, staging_info.st_ino)
        embedding._create_transaction_journal(
            directory_descriptor,
            journal_name,
            target_name=path.name,
            backup_name=backup_name,
            original_name=original_name,
            staging_name=staging_name,
            source_inode=source_inode,
            staging_inode=staging_inode,
            source_hash=hashlib.sha256(original).hexdigest(),
            staging_hash=hashlib.sha256(staging.read_bytes()).hexdigest(),
        )
        os.link(path, tmp_path / original_name)
        embedding._rename_noreplace(
            directory_descriptor,
            path.name,
            directory_descriptor,
            backup_name,
        )
        embedding._rename_noreplace(
            directory_descriptor,
            staging_name,
            directory_descriptor,
            path.name,
        )
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)

    recovered = embedding.recover_incomplete_transactions(tmp_path)

    assert recovered == (path,)
    assert path.read_bytes() == original
    assert path.stat().st_nlink == 1
    assert not list(tmp_path.glob(".*.artwork-*"))


def test_recovery_restores_concurrent_save_that_replaces_classified_staged_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "song.flac"
    concurrent = tmp_path / "concurrent.flac"
    staging_name, backup_name, original_name, journal_name = transaction_names(
        path.name, "staged-target-race"
    )
    staging = tmp_path / staging_name
    make_audio(path, "flac")
    make_audio(staging, "flac")
    make_audio(concurrent, "flac")
    staged_audio = FLAC(staging)
    staged_audio["comment"] = "installed stage"
    staged_audio.save()
    concurrent_audio = FLAC(concurrent)
    concurrent_audio["comment"] = "concurrent recovery save"
    concurrent_audio.save()
    original = path.read_bytes()
    concurrent_bytes = concurrent.read_bytes()
    directory_descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        source_info = path.stat()
        staging_info = staging.stat()
        source_inode = (source_info.st_dev, source_info.st_ino)
        staging_inode = (staging_info.st_dev, staging_info.st_ino)
        embedding._create_transaction_journal(
            directory_descriptor,
            journal_name,
            target_name=path.name,
            backup_name=backup_name,
            original_name=original_name,
            staging_name=staging_name,
            source_inode=source_inode,
            staging_inode=staging_inode,
            source_hash=hashlib.sha256(original).hexdigest(),
            staging_hash=hashlib.sha256(staging.read_bytes()).hexdigest(),
        )
        os.link(path, tmp_path / original_name)
        embedding._rename_noreplace(
            directory_descriptor,
            path.name,
            directory_descriptor,
            backup_name,
        )
        embedding._rename_noreplace(
            directory_descriptor,
            staging_name,
            directory_descriptor,
            path.name,
        )
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)

    real_noreplace = embedding._rename_noreplace
    installed_concurrent = False

    def install_concurrent_before_recovery_move(*args: object) -> None:
        nonlocal installed_concurrent
        if not installed_concurrent:
            installed_concurrent = True
            os.replace(concurrent, path)
        real_noreplace(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(embedding, "_rename_noreplace", install_concurrent_before_recovery_move)

    with pytest.raises(EmbedError, match=r"concurrent|changed|retained"):
        embedding.recover_incomplete_transactions(tmp_path)

    assert installed_concurrent
    assert path.read_bytes() == concurrent_bytes
    assert (tmp_path / journal_name).is_file()
    assert any(
        candidate.read_bytes() == original
        for candidate in (tmp_path / backup_name, tmp_path / original_name)
        if candidate.is_file()
    )


def test_recovery_revalidates_visible_target_before_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "song.flac"
    concurrent = tmp_path / "concurrent.flac"
    staging_name, backup_name, original_name, journal_name = transaction_names(
        path.name, "cleanup-race"
    )
    staging = tmp_path / staging_name
    make_audio(path, "flac")
    make_audio(staging, "flac")
    make_audio(concurrent, "flac")
    staged_audio = FLAC(staging)
    staged_audio["comment"] = "staged cleanup race"
    staged_audio.save()
    concurrent_audio = FLAC(concurrent)
    concurrent_audio["comment"] = "save before recovery cleanup"
    concurrent_audio.save()
    original = path.read_bytes()
    concurrent_bytes = concurrent.read_bytes()
    directory_descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        source_info = path.stat()
        staging_info = staging.stat()
        source_inode = (source_info.st_dev, source_info.st_ino)
        staging_inode = (staging_info.st_dev, staging_info.st_ino)
        embedding._create_transaction_journal(
            directory_descriptor,
            journal_name,
            target_name=path.name,
            backup_name=backup_name,
            original_name=original_name,
            staging_name=staging_name,
            source_inode=source_inode,
            staging_inode=staging_inode,
            source_hash=hashlib.sha256(original).hexdigest(),
            staging_hash=hashlib.sha256(staging.read_bytes()).hexdigest(),
        )
        os.link(path, tmp_path / original_name)
        os.link(path, tmp_path / backup_name)
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)

    real_entry_kind = embedding._recovery_entry_kind
    target_classified = False
    installed_concurrent = False

    def install_after_target_classification(
        directory_descriptor: int,
        name: str,
        source_hash: str,
        staging_hash: str,
    ) -> tuple[str, os.stat_result | None]:
        nonlocal target_classified, installed_concurrent
        result = real_entry_kind(directory_descriptor, name, source_hash, staging_hash)
        if name == path.name:
            target_classified = True
        elif target_classified and not installed_concurrent:
            installed_concurrent = True
            os.replace(concurrent, path)
        return result

    monkeypatch.setattr(embedding, "_recovery_entry_kind", install_after_target_classification)

    with pytest.raises(EmbedError, match=r"unrelated|changed|retained"):
        embedding.recover_incomplete_transactions(tmp_path)

    assert installed_concurrent
    assert path.read_bytes() == concurrent_bytes
    assert (tmp_path / journal_name).is_file()
    assert any(
        candidate.is_file() and candidate.read_bytes() == original
        for candidate in (tmp_path / backup_name, tmp_path / original_name)
    )


def test_recovery_retains_journal_when_classified_entry_is_not_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "song.flac"
    staging_name, backup_name, original_name, journal_name = transaction_names(
        path.name, "inode-shift"
    )
    staging = tmp_path / staging_name
    make_audio(path, "flac")
    make_audio(staging, "flac")
    staged_audio = FLAC(staging)
    staged_audio["comment"] = "inode instability stage"
    staged_audio.save()
    original = path.read_bytes()
    directory_descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        source_info = path.stat()
        staging_info = staging.stat()
        embedding._create_transaction_journal(
            directory_descriptor,
            journal_name,
            target_name=path.name,
            backup_name=backup_name,
            original_name=original_name,
            staging_name=staging_name,
            source_inode=(source_info.st_dev, source_info.st_ino),
            staging_inode=(staging_info.st_dev, staging_info.st_ino),
            source_hash=hashlib.sha256(original).hexdigest(),
            staging_hash=hashlib.sha256(staging.read_bytes()).hexdigest(),
        )
        os.link(path, tmp_path / original_name)
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)

    def simulate_inode_mismatch(
        _directory_descriptor: int,
        _name: str,
        _identity: tuple[int, int],
    ) -> None:
        return

    monkeypatch.setattr(embedding, "_unlink_owned_entry", simulate_inode_mismatch)

    recovered = embedding.recover_incomplete_transactions(tmp_path)

    assert recovered == (path,)
    assert not (tmp_path / original_name).exists()
    assert not (tmp_path / journal_name).exists()
    assert path.read_bytes() == original
    assert path.stat().st_nlink == 1


def test_recovery_restores_displaced_concurrent_atomic_save(tmp_path: Path) -> None:
    path = tmp_path / "song.flac"
    concurrent = tmp_path / "concurrent.flac"
    staging_name, backup_name, original_name, journal_name = transaction_names(
        path.name, "displaced-concurrent"
    )
    staging = tmp_path / staging_name
    make_audio(path, "flac")
    make_audio(staging, "flac")
    make_audio(concurrent, "flac")
    staged_audio = FLAC(staging)
    staged_audio["comment"] = "our staged update"
    staged_audio.save()
    concurrent_audio = FLAC(concurrent)
    concurrent_audio["comment"] = "external editor update"
    concurrent_audio.save()
    original = path.read_bytes()
    concurrent_bytes = concurrent.read_bytes()
    directory_descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        source_info = path.stat()
        staging_info = staging.stat()
        source_inode = (source_info.st_dev, source_info.st_ino)
        staging_inode = (staging_info.st_dev, staging_info.st_ino)
        embedding._create_transaction_journal(
            directory_descriptor,
            journal_name,
            target_name=path.name,
            backup_name=backup_name,
            original_name=original_name,
            staging_name=staging_name,
            source_inode=source_inode,
            staging_inode=staging_inode,
            source_hash=hashlib.sha256(original).hexdigest(),
            staging_hash=hashlib.sha256(staging.read_bytes()).hexdigest(),
        )
        os.link(path, tmp_path / original_name)
        os.replace(concurrent, path)
        embedding._rename_noreplace(
            directory_descriptor,
            path.name,
            directory_descriptor,
            backup_name,
        )
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)

    recovered = embedding.recover_incomplete_transactions(tmp_path)

    assert recovered == (path,)
    assert path.read_bytes() == concurrent_bytes
    assert not list(tmp_path.glob(".*.artwork-*"))


@pytest.mark.parametrize("crash_call", [1, 2])
def test_recovery_of_displaced_concurrent_save_is_restart_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crash_call: int
) -> None:
    path = tmp_path / "song.flac"
    concurrent = tmp_path / "concurrent.flac"
    staging_name, backup_name, original_name, journal_name = transaction_names(
        path.name, "displaced-concurrent-restart"
    )
    staging = tmp_path / staging_name
    make_audio(path, "flac")
    make_audio(staging, "flac")
    make_audio(concurrent, "flac")
    staged_audio = FLAC(staging)
    staged_audio["comment"] = "restart staged update"
    staged_audio.save()
    concurrent_audio = FLAC(concurrent)
    concurrent_audio["comment"] = "restart external save"
    concurrent_audio.save()
    original = path.read_bytes()
    concurrent_bytes = concurrent.read_bytes()
    directory_descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        source_info = path.stat()
        staging_info = staging.stat()
        embedding._create_transaction_journal(
            directory_descriptor,
            journal_name,
            target_name=path.name,
            backup_name=backup_name,
            original_name=original_name,
            staging_name=staging_name,
            source_inode=(source_info.st_dev, source_info.st_ino),
            staging_inode=(staging_info.st_dev, staging_info.st_ino),
            source_hash=hashlib.sha256(original).hexdigest(),
            staging_hash=hashlib.sha256(staging.read_bytes()).hexdigest(),
        )
        os.link(path, tmp_path / original_name)
        os.replace(concurrent, path)
        embedding._rename_noreplace(
            directory_descriptor,
            path.name,
            directory_descriptor,
            backup_name,
        )
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)

    real_noreplace = embedding._rename_noreplace
    rename_calls = 0

    def crash_after_concurrent_restore(*args: object) -> None:
        nonlocal rename_calls
        rename_calls += 1
        real_noreplace(*args)  # type: ignore[arg-type]
        if rename_calls == crash_call:
            raise KeyboardInterrupt

    monkeypatch.setattr(embedding, "_rename_noreplace", crash_after_concurrent_restore)

    with pytest.raises(KeyboardInterrupt):
        embedding.recover_incomplete_transactions(tmp_path)

    assert list(tmp_path.glob(".*.artwork-transaction-*-restored.json"))
    monkeypatch.setattr(embedding, "_rename_noreplace", real_noreplace)

    recovered = embedding.recover_incomplete_transactions(tmp_path)

    assert recovered == (path,)
    assert path.read_bytes() == concurrent_bytes
    assert not list(tmp_path.glob(".*.artwork-*"))


def test_recovery_restored_phase_converges_after_original_cleanup(
    tmp_path: Path,
) -> None:
    path = tmp_path / "song.flac"
    concurrent = tmp_path / "concurrent.flac"
    staging_name, backup_name, original_name, journal_name = transaction_names(
        path.name, "restored-after-original-cleanup"
    )
    restored_journal_name = f"{journal_name.removesuffix('.json')}-restored.json"
    staging = tmp_path / staging_name
    make_audio(path, "flac")
    make_audio(staging, "flac")
    make_audio(concurrent, "flac")
    staged_audio = FLAC(staging)
    staged_audio["comment"] = "cleanup crash stage"
    staged_audio.save()
    concurrent_audio = FLAC(concurrent)
    concurrent_audio["comment"] = "cleanup crash concurrent save"
    concurrent_audio.save()
    source = path.read_bytes()
    concurrent_bytes = concurrent.read_bytes()
    directory_descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        source_info = path.stat()
        staging_info = staging.stat()
        embedding._create_transaction_journal(
            directory_descriptor,
            journal_name,
            target_name=path.name,
            backup_name=backup_name,
            original_name=original_name,
            staging_name=staging_name,
            source_inode=(source_info.st_dev, source_info.st_ino),
            staging_inode=(staging_info.st_dev, staging_info.st_ino),
            source_hash=hashlib.sha256(source).hexdigest(),
            staging_hash=hashlib.sha256(staging.read_bytes()).hexdigest(),
        )
        os.link(path, tmp_path / original_name)
        os.replace(concurrent, path)
        os.rename(
            journal_name,
            restored_journal_name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
        )
        os.unlink(original_name, dir_fd=directory_descriptor)
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)

    recovered = embedding.recover_incomplete_transactions(tmp_path)

    assert recovered == (path,)
    assert path.read_bytes() == concurrent_bytes
    assert not list(tmp_path.glob(".*.artwork-*"))


def test_recovery_does_not_overwrite_concurrent_file_installed_during_gap(
    tmp_path: Path,
) -> None:
    path = tmp_path / "song.flac"
    concurrent = tmp_path / "concurrent.flac"
    staging_name, backup_name, original_name, journal_name = transaction_names(
        path.name, "concurrent-gap"
    )
    staging = tmp_path / staging_name
    make_audio(path, "flac")
    make_audio(staging, "flac")
    make_audio(concurrent, "flac")
    staged_audio = FLAC(staging)
    staged_audio["comment"] = "our staged update"
    staged_audio.save()
    concurrent_audio = FLAC(concurrent)
    concurrent_audio["comment"] = "save during gap"
    concurrent_audio.save()
    original = path.read_bytes()
    concurrent_bytes = concurrent.read_bytes()
    directory_descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        source_info = path.stat()
        staging_info = staging.stat()
        source_inode = (source_info.st_dev, source_info.st_ino)
        staging_inode = (staging_info.st_dev, staging_info.st_ino)
        embedding._create_transaction_journal(
            directory_descriptor,
            journal_name,
            target_name=path.name,
            backup_name=backup_name,
            original_name=original_name,
            staging_name=staging_name,
            source_inode=source_inode,
            staging_inode=staging_inode,
            source_hash=hashlib.sha256(original).hexdigest(),
            staging_hash=hashlib.sha256(staging.read_bytes()).hexdigest(),
        )
        os.link(path, tmp_path / original_name)
        embedding._rename_noreplace(
            directory_descriptor,
            path.name,
            directory_descriptor,
            backup_name,
        )
        os.replace(concurrent, path)
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)

    with pytest.raises(EmbedError, match=r"unrelated data|retained"):
        embedding.recover_incomplete_transactions(tmp_path)

    assert path.read_bytes() == concurrent_bytes
    assert (tmp_path / backup_name).read_bytes() == original
    assert (tmp_path / original_name).read_bytes() == original
    assert (tmp_path / journal_name).is_file()


def test_embed_flac_replaces_only_front_cover_and_preserves_other_metadata(tmp_path: Path) -> None:
    path = tmp_path / "track.flac"
    make_audio(path, "flac")
    old_front = image_bytes((255, 0, 0))
    old_back = image_bytes((0, 0, 255))
    new_front = image_bytes((0, 255, 0))
    audio = FLAC(path)
    audio["title"] = "Do Not Lose Me"
    audio.add_picture(flac_picture(old_front, PictureType.COVER_FRONT))
    audio.add_picture(flac_picture(old_back, PictureType.COVER_BACK))
    audio.save()
    artwork = decode_artwork(new_front, "https://a5.mzstatic.com/master.png")

    result = embed_artwork(path, artwork, replace_existing=True)

    updated = FLAC(path)
    assert result.status == "embedded"
    assert updated["title"] == ["Do Not Lose Me"]
    assert [
        picture.data for picture in updated.pictures if picture.type == PictureType.COVER_FRONT
    ] == [new_front]
    assert [
        picture.data for picture in updated.pictures if picture.type == PictureType.COVER_BACK
    ] == [old_back]


@pytest.mark.parametrize(
    ("suffix", "codec", "loader"),
    (
        (".mp3", "libmp3lame", MP3),
        (".wav", "pcm_s16le", WAVE),
        (".aiff", "pcm_s16be", AIFF),
    ),
)
def test_embed_first_artwork_into_tagless_id3_container(
    tmp_path: Path,
    suffix: str,
    codec: str,
    loader: type[MP3] | type[WAVE] | type[AIFF],
) -> None:
    path = tmp_path / f"tagless{suffix}"
    make_audio(path, codec)
    initial = loader(path)
    initial.delete()
    assert loader(path).tags is None
    new_front = image_bytes((0, 255, 0))
    artwork = decode_artwork(new_front, "https://a5.mzstatic.com/master.png")

    result = embed_artwork(path, artwork)

    updated = loader(path)
    assert result.status == "embedded"
    assert updated.tags is not None
    assert [
        picture.data
        for picture in updated.tags.getall("APIC")
        if picture.type == PictureType.COVER_FRONT
    ] == [new_front]


def test_embed_mp3_replaces_front_apic_and_preserves_other_id3_frames(tmp_path: Path) -> None:
    path = tmp_path / "track.mp3"
    make_audio(path, "libmp3lame")
    old_front = image_bytes((255, 0, 0), "JPEG")
    old_back = image_bytes((0, 0, 255), "JPEG")
    new_front = image_bytes((0, 255, 0), "JPEG")
    audio = MP3(path)
    if audio.tags is None:
        audio.add_tags()
    assert audio.tags is not None
    audio.tags.add(TIT2(encoding=3, text=["Keep This Title"]))
    audio.tags.add(
        APIC(
            encoding=3,
            mime="image/jpeg",
            type=PictureType.COVER_FRONT,
            desc="front",
            data=old_front,
        )
    )
    audio.tags.add(
        APIC(
            encoding=3,
            mime="image/jpeg",
            type=PictureType.COVER_BACK,
            desc="back",
            data=old_back,
        )
    )
    audio.save(v2_version=3)
    artwork = decode_artwork(new_front, "https://a5.mzstatic.com/master.jpg")

    result = embed_artwork(path, artwork, replace_existing=True)

    updated = MP3(path)
    assert updated.tags is not None
    pictures = updated.tags.getall("APIC")
    assert result.status == "embedded"
    assert str(updated.tags["TIT2"]) == "Keep This Title"
    assert [picture.data for picture in pictures if picture.type == PictureType.COVER_FRONT] == [
        new_front
    ]
    assert [picture.data for picture in pictures if picture.type == PictureType.COVER_BACK] == [
        old_back
    ]


def test_embed_m4a_replaces_covr_and_preserves_mp4_metadata(tmp_path: Path) -> None:
    path = tmp_path / "track.m4a"
    make_audio(path, "alac")
    old_front = image_bytes((255, 0, 0), "PNG")
    new_front = image_bytes((0, 255, 0), "PNG")
    audio = MP4(path)
    if audio.tags is None:
        audio.add_tags()
    assert audio.tags is not None
    audio.tags["\xa9nam"] = ["Keep This Title"]
    audio.tags["covr"] = [MP4Cover(old_front, imageformat=MP4Cover.FORMAT_PNG)]
    audio.save()
    artwork = decode_artwork(new_front, "https://a5.mzstatic.com/master.png")

    result = embed_artwork(path, artwork, replace_existing=True)

    updated = MP4(path)
    assert updated.tags is not None
    assert result.status == "embedded"
    assert updated.tags["\xa9nam"] == ["Keep This Title"]
    assert [bytes(cover) for cover in updated.tags["covr"]] == [new_front]


def test_embed_opus_replaces_front_picture_and_preserves_vorbis_comments(tmp_path: Path) -> None:
    path = tmp_path / "track.opus"
    make_audio(path, "libopus")
    old_front = image_bytes((255, 0, 0), "PNG")
    old_back = image_bytes((0, 0, 255), "PNG")
    new_front = image_bytes((0, 255, 0), "PNG")
    front_picture = flac_picture(old_front, PictureType.COVER_FRONT)
    back_picture = flac_picture(old_back, PictureType.COVER_BACK)
    audio = OggOpus(path)
    if audio.tags is None:
        audio.add_tags()
    assert audio.tags is not None
    audio.tags["title"] = ["Keep This Title"]
    audio.tags["metadata_block_picture"] = [
        base64.b64encode(front_picture.write()).decode("ascii"),
        base64.b64encode(back_picture.write()).decode("ascii"),
    ]
    audio.save()
    artwork = decode_artwork(new_front, "https://a5.mzstatic.com/master.png")

    result = embed_artwork(path, artwork, replace_existing=True)

    updated = OggOpus(path)
    assert updated.tags is not None
    pictures = [
        Picture(base64.b64decode(value)) for value in updated.tags["metadata_block_picture"]
    ]
    assert result.status == "embedded"
    assert updated.tags["title"] == ["Keep This Title"]
    assert [picture.data for picture in pictures if picture.type == PictureType.COVER_FRONT] == [
        new_front
    ]
    assert [picture.data for picture in pictures if picture.type == PictureType.COVER_BACK] == [
        old_back
    ]


def test_embed_wave_writes_and_verifies_id3_front_cover(tmp_path: Path) -> None:
    path = tmp_path / "track.wav"
    make_audio(path, "pcm_s16le")
    new_front = image_bytes((0, 255, 0), "PNG")
    audio = WAVE(path)
    if audio.tags is None:
        audio.add_tags()
    assert audio.tags is not None
    audio.tags.add(TIT2(encoding=3, text=["Keep This Title"]))
    audio.save()
    artwork = decode_artwork(new_front, "https://a5.mzstatic.com/master.png")

    result = embed_artwork(path, artwork)

    updated = WAVE(path)
    assert updated.tags is not None
    pictures = updated.tags.getall("APIC")
    assert result.status == "embedded"
    assert str(updated.tags["TIT2"]) == "Keep This Title"
    assert [picture.data for picture in pictures if picture.type == PictureType.COVER_FRONT] == [
        new_front
    ]


def test_embed_opus_refuses_malformed_picture_metadata_instead_of_deleting_it(
    tmp_path: Path,
) -> None:
    path = tmp_path / "malformed.opus"
    make_audio(path, "libopus")
    audio = OggOpus(path)
    if audio.tags is None:
        audio.add_tags()
    assert audio.tags is not None
    audio.tags["metadata_block_picture"] = ["not-valid-base64!!!"]
    audio.save()
    artwork = decode_artwork(image_bytes((0, 255, 0), "PNG"), "https://a5.mzstatic.com/master.png")

    with pytest.raises(EmbedError, match="malformed METADATA_BLOCK_PICTURE"):
        embed_artwork(path, artwork, replace_existing=True)

    unchanged = OggOpus(path)
    assert unchanged.tags is not None
    assert unchanged.tags["metadata_block_picture"] == ["not-valid-base64!!!"]


def test_embed_mp3_preserves_existing_id3v1_bytes_exactly(tmp_path: Path) -> None:
    path = tmp_path / "id3v1.mp3"
    make_audio(path, "libmp3lame")
    audio = MP3(path)
    if audio.tags is None:
        audio.add_tags()
    assert audio.tags is not None
    audio.tags.add(TIT2(encoding=1, text=["Modern V2 Title"]))
    audio.save(v2_version=3)
    id3v1 = b"".join(
        [
            b"TAG",
            b"Original V1 Title".ljust(30, b"\0"),
            b"Original Artist".ljust(30, b"\0"),
            b"Original Album".ljust(30, b"\0"),
            b"1999",
            b"Original Comment".ljust(30, b"\0"),
            bytes([13]),
        ]
    )
    assert len(id3v1) == 128
    with path.open("ab") as handle:
        handle.write(id3v1)
    artwork = decode_artwork(image_bytes((0, 255, 0), "JPEG"), "https://a5.mzstatic.com/master.jpg")

    embed_artwork(path, artwork)

    with path.open("rb") as handle:
        handle.seek(-128, 2)
        assert handle.read(128) == id3v1


def test_embed_aiff_writes_and_verifies_id3_front_cover(tmp_path: Path) -> None:
    path = tmp_path / "track.aiff"
    make_audio(path, "pcm_s16be")
    new_front = image_bytes((0, 255, 0), "PNG")
    audio = AIFF(path)
    if audio.tags is None:
        audio.add_tags()
    assert audio.tags is not None
    audio.tags.add(TIT2(encoding=3, text=["Keep This Title"]))
    audio.save()
    artwork = decode_artwork(new_front, "https://a5.mzstatic.com/master.png")

    result = embed_artwork(path, artwork)

    updated = AIFF(path)
    assert updated.tags is not None
    pictures = updated.tags.getall("APIC")
    assert result.status == "embedded"
    assert result.format == "AIFF"
    assert str(updated.tags["TIT2"]) == "Keep This Title"
    assert [picture.data for picture in pictures if picture.type == PictureType.COVER_FRONT] == [
        new_front
    ]


def test_embed_wavpack_replaces_front_and_preserves_back_artwork(tmp_path: Path) -> None:
    path = tmp_path / "track.wv"
    make_audio(path, "wavpack")
    old_front = image_bytes((255, 0, 0), "JPEG")
    old_back = image_bytes((0, 0, 255), "PNG")
    new_front = image_bytes((0, 255, 0), "PNG")
    audio = WavPack(path)
    if audio.tags is None:
        audio.add_tags()
    assert audio.tags is not None
    audio.tags["Title"] = "Keep This Title"
    audio.tags["Cover Art (Front)"] = APEBinaryValue(b"front.jpg\0" + old_front)
    audio.tags["Cover Art (Back)"] = APEBinaryValue(b"back.png\0" + old_back)
    audio.save()
    artwork = decode_artwork(new_front, "https://a5.mzstatic.com/master.png")

    result = embed_artwork(path, artwork, replace_existing=True)

    updated = WavPack(path)
    assert updated.tags is not None
    assert result.status == "embedded"
    assert result.format == "WavPack"
    assert str(updated.tags["Title"]) == "Keep This Title"
    assert bytes(updated.tags["Cover Art (Back)"]) == b"back.png\0" + old_back
    assert bytes(updated.tags["Cover Art (Front)"]) == b"cover.png\0" + new_front


def test_embedding_failure_never_corrupts_the_original_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "rollback.mp3"
    make_audio(path, "libmp3lame")
    original = path.read_bytes()
    artwork = decode_artwork(image_bytes((0, 255, 0), "JPEG"), "https://a5.mzstatic.com/master.jpg")

    def corrupt_then_fail(audio: MP3, *args: object, **kwargs: object) -> None:
        del audio, kwargs
        staged = args[0]
        assert hasattr(staged, "seek") and hasattr(staged, "truncate")
        staged.seek(0)  # type: ignore[union-attr]
        staged.truncate(0)  # type: ignore[union-attr]
        staged.write(b"corrupted staged file")  # type: ignore[union-attr]
        staged.flush()  # type: ignore[union-attr]
        raise OSError("simulated disk failure")

    monkeypatch.setattr(MP3, "save", corrupt_then_fail)

    with pytest.raises(EmbedError, match="simulated disk failure"):
        embed_artwork(path, artwork)

    assert path.read_bytes() == original
