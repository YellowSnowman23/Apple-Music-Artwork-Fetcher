"""Format-agnostic preflight, preservation, and transactional replacement."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import secrets
import stat
from collections.abc import Callable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any

import mutagen
from mutagen.aiff import AIFF
from mutagen.flac import FLAC
from mutagen.id3 import APIC, PictureType
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis
from mutagen.wave import WAVE
from mutagen.wavpack import WavPack

from .adapters import adapter_for
from .adapters.id3 import _apev2_tags, _read_id3v1
from .adapters.mp4 import _mp4_children
from .adapters.xiph import _parse_xiph_pictures
from .filesystem import (
    _binary_source,
    _open_regular_source,
    _source_stat,
    _stat_identity,
)
from .models import (
    AlbumGroup,
    Artwork,
    EmbedCommittedError,
    EmbedCommittedInterrupt,
    EmbedError,
    EmbedResult,
)
from .mutagen_io import _load_mutagen

_RENAME_EXCHANGE = 2
_RENAME_NOREPLACE = 1
_TRANSACTION_JOURNAL_SCHEMA = 1
_MAX_TRANSACTION_JOURNAL_BYTES = 16 * 1024


def _preflight_opened(
    source: Path | int,
    display_path: Path,
    artwork: Artwork | None,
    *,
    replace_existing: bool,
) -> EmbedResult:
    try:
        audio = _load_mutagen(source, filename=display_path)
    except (OSError, mutagen.MutagenError) as exc:
        raise EmbedError(f"cannot open {display_path}: {exc}") from exc
    if audio is None:
        raise EmbedError(f"unsupported or unreadable audio file: {display_path}")
    adapter = adapter_for(audio)
    front_pictures = adapter.front_pictures(audio, source, display_path)
    format_name = adapter.result_format(audio)
    if artwork is not None and len(front_pictures) == 1 and front_pictures[0] == artwork.data:
        return EmbedResult("unchanged", format_name, "identical front cover already embedded")
    if front_pictures and not replace_existing:
        return EmbedResult("skipped", format_name, "front cover already exists")
    return EmbedResult("ready", format_name, "container and metadata stores passed preflight")


def preflight_artwork(
    path: Path,
    artwork: Artwork | None = None,
    *,
    replace_existing: bool = False,
    expected_identity: tuple[int, int, int, int, int] | None = None,
) -> EmbedResult:
    """Inspect one target through a bound descriptor without writing it."""
    path = Path(path)
    parent_descriptor = -1
    source_descriptor = -1
    try:
        parent_descriptor, source_descriptor, _info = _open_regular_source(
            path,
            expected_identity,
        )
        return _preflight_opened(
            source_descriptor,
            path,
            artwork,
            replace_existing=replace_existing,
        )
    except EmbedError:
        raise
    except OSError as exc:
        raise EmbedError(f"cannot inspect {path}: {exc}") from exc
    finally:
        if source_descriptor >= 0:
            os.close(source_descriptor)
        if parent_descriptor >= 0:
            os.close(parent_descriptor)


def _stable_value(value: object) -> object:
    if isinstance(value, bytes):
        return ("bytes", len(value), hashlib.sha256(value).hexdigest())
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    if isinstance(value, Mapping):
        return tuple(sorted((str(key), _stable_value(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_stable_value(item) for item in value)
    try:
        payload = bytes(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        pprint = getattr(value, "pprint", None)
        return (type(value).__name__, pprint() if callable(pprint) else str(value))
    return (type(value).__name__, len(payload), hashlib.sha256(payload).hexdigest())


def _hash_file_region(path: Path | int, start: int, end: int) -> tuple[int, str]:
    if start < 0 or end < start:
        raise EmbedError(f"invalid encoded-audio region in {path}")
    digest = hashlib.sha256()
    remaining = end - start
    try:
        with _binary_source(path) as handle:
            handle.seek(start)
            while remaining:
                chunk = handle.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise EmbedError(f"encoded-audio payload is truncated in {path}")
                digest.update(chunk)
                remaining -= len(chunk)
    except OSError as exc:
        raise EmbedError(f"failed to hash encoded-audio payload in {path}: {exc}") from exc
    return end - start, digest.hexdigest()


def _mp3_payload_digest(path: Path | int) -> tuple[int, str]:
    try:
        size = _source_stat(path).st_size
        with _binary_source(path) as handle:
            header = handle.read(10)
            start = 0
            if len(header) == 10 and header[:3] == b"ID3":
                if any(byte & 0x80 for byte in header[6:10]):
                    raise EmbedError(f"invalid ID3 size while hashing MP3 payload: {path}")
                tag_size = (header[6] << 21) | (header[7] << 14) | (header[8] << 7) | header[9]
                start = 10 + tag_size + (10 if header[5] & 0x10 else 0)
            end = size
            if end >= 128:
                handle.seek(end - 128)
                if handle.read(3) == b"TAG":
                    end -= 128
            if end >= 32:
                handle.seek(end - 32)
                footer = handle.read(32)
                if footer[:8] == b"APETAGEX":
                    ape_size = int.from_bytes(footer[12:16], "little")
                    if ape_size < 32 or ape_size > end - start:
                        raise EmbedError(f"invalid APEv2 size while hashing MP3 payload: {path}")
                    end -= ape_size
                    if end >= 32:
                        handle.seek(end - 32)
                        if handle.read(8) == b"APETAGEX":
                            end -= 32
    except OSError as exc:
        raise EmbedError(f"failed to locate encoded MP3 payload in {path}: {exc}") from exc
    return _hash_file_region(path, start, end)


def _flac_payload_digest(path: Path | int) -> tuple[int, str]:
    try:
        size = _source_stat(path).st_size
        with _binary_source(path) as handle:
            if handle.read(4) != b"fLaC":
                raise EmbedError(f"invalid FLAC marker while hashing payload: {path}")
            offset = 4
            while True:
                header = handle.read(4)
                if len(header) != 4:
                    raise EmbedError(f"truncated FLAC metadata while hashing payload: {path}")
                is_last = bool(header[0] & 0x80)
                block_size = int.from_bytes(header[1:4], "big")
                offset += 4 + block_size
                if offset > size:
                    raise EmbedError(f"invalid FLAC metadata size while hashing payload: {path}")
                handle.seek(block_size, os.SEEK_CUR)
                if is_last:
                    break
    except OSError as exc:
        raise EmbedError(f"failed to locate FLAC payload in {path}: {exc}") from exc
    return _hash_file_region(path, offset, size)


def _hash_regions(path: Path | int, regions: list[tuple[int, int]]) -> tuple[int, str]:
    if not regions:
        raise EmbedError(f"encoded-audio container has no payload regions: {path}")
    digest = hashlib.sha256()
    total = 0
    try:
        with _binary_source(path) as handle:
            for start, end in regions:
                if start < 0 or end < start:
                    raise EmbedError(f"invalid encoded-audio region in {path}")
                length = end - start
                digest.update(length.to_bytes(8, "big"))
                handle.seek(start)
                remaining = length
                while remaining:
                    chunk = handle.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise EmbedError(f"encoded-audio payload is truncated in {path}")
                    digest.update(chunk)
                    total += len(chunk)
                    remaining -= len(chunk)
    except OSError as exc:
        raise EmbedError(f"failed to hash encoded-audio payload in {path}: {exc}") from exc
    return total, digest.hexdigest()


def _mp4_payload_digest(path: Path | int) -> tuple[int, str]:
    try:
        size = _source_stat(path).st_size
        with _binary_source(path) as handle:
            regions = [
                (start, end)
                for box_type, start, end in _mp4_children(handle, 0, size)
                if box_type == b"mdat"
            ]
    except OSError as exc:
        raise EmbedError(f"failed to locate MP4 media payload in {path}: {exc}") from exc
    return _hash_regions(path, regions)


def _riff_payload_digest(path: Path | int) -> tuple[int, str]:
    regions: list[tuple[int, int]] = []
    try:
        size = _source_stat(path).st_size
        with _binary_source(path) as handle:
            header = handle.read(12)
            if len(header) != 12 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
                raise EmbedError(f"unsupported RIFF/WAVE structure while hashing payload: {path}")
            offset = 12
            while offset + 8 <= size:
                handle.seek(offset)
                chunk_header = handle.read(8)
                chunk_size = int.from_bytes(chunk_header[4:8], "little")
                payload_start = offset + 8
                payload_end = payload_start + chunk_size
                if payload_end > size:
                    raise EmbedError(f"truncated RIFF/WAVE chunk while hashing payload: {path}")
                if chunk_header[:4] == b"data":
                    regions.append((payload_start, payload_end))
                offset = payload_end + (chunk_size & 1)
    except OSError as exc:
        raise EmbedError(f"failed to locate RIFF/WAVE payload in {path}: {exc}") from exc
    return _hash_regions(path, regions)


def _aiff_payload_digest(path: Path | int) -> tuple[int, str]:
    regions: list[tuple[int, int]] = []
    try:
        size = _source_stat(path).st_size
        with _binary_source(path) as handle:
            header = handle.read(12)
            if len(header) != 12 or header[:4] != b"FORM" or header[8:12] not in {b"AIFF", b"AIFC"}:
                raise EmbedError(f"unsupported AIFF structure while hashing payload: {path}")
            offset = 12
            while offset + 8 <= size:
                handle.seek(offset)
                chunk_header = handle.read(8)
                chunk_size = int.from_bytes(chunk_header[4:8], "big")
                payload_start = offset + 8
                payload_end = payload_start + chunk_size
                if payload_end > size:
                    raise EmbedError(f"truncated AIFF chunk while hashing payload: {path}")
                if chunk_header[:4] == b"SSND":
                    regions.append((payload_start, payload_end))
                offset = payload_end + (chunk_size & 1)
    except OSError as exc:
        raise EmbedError(f"failed to locate AIFF payload in {path}: {exc}") from exc
    return _hash_regions(path, regions)


def _wavpack_payload_digest(path: Path | int) -> tuple[int, str]:
    try:
        end = _source_stat(path).st_size
        with _binary_source(path) as handle:
            if handle.read(4) != b"wvpk":
                raise EmbedError(f"invalid WavPack marker while hashing payload: {path}")
            if end >= 32:
                handle.seek(end - 32)
                footer = handle.read(32)
                if footer[:8] == b"APETAGEX":
                    ape_size = int.from_bytes(footer[12:16], "little")
                    if ape_size < 32 or ape_size > end:
                        raise EmbedError(f"invalid WavPack APEv2 size: {path}")
                    end -= ape_size
                    if end >= 32:
                        handle.seek(end - 32)
                        if handle.read(8) == b"APETAGEX":
                            end -= 32
    except OSError as exc:
        raise EmbedError(f"failed to locate WavPack payload in {path}: {exc}") from exc
    return _hash_file_region(path, 0, end)


def _ogg_payload_digest(path: Path | int) -> tuple[int, str]:
    states: dict[int, dict[str, Any]] = {}
    try:
        size = _source_stat(path).st_size
        offset = 0
        with _binary_source(path) as handle:
            while offset < size:
                handle.seek(offset)
                header = handle.read(27)
                if len(header) != 27 or header[:4] != b"OggS" or header[4] != 0:
                    raise EmbedError(f"invalid Ogg page while hashing payload: {path}")
                segment_count = header[26]
                lacing = handle.read(segment_count)
                if len(lacing) != segment_count:
                    raise EmbedError(f"truncated Ogg lacing table: {path}")
                body_size = sum(lacing)
                body = handle.read(body_size)
                if len(body) != body_size:
                    raise EmbedError(f"truncated Ogg page body: {path}")
                serial = int.from_bytes(header[14:18], "little")
                state = states.setdefault(
                    serial,
                    {
                        "buffer": bytearray(),
                        "digest": hashlib.sha256(),
                        "index": 0,
                        "total": 0,
                        "first": b"",
                    },
                )
                buffer = state["buffer"]
                assert isinstance(buffer, bytearray)
                position = 0
                for length in lacing:
                    buffer.extend(body[position : position + length])
                    position += length
                    if len(buffer) > 16 * 1024 * 1024:
                        raise EmbedError(f"unreasonably large Ogg packet: {path}")
                    if length < 255:
                        index = int(state["index"])
                        packet = bytes(buffer)
                        if index == 0:
                            state["first"] = packet[:16]
                        if index != 1:
                            digest = state["digest"]
                            digest.update(len(packet).to_bytes(8, "big"))
                            digest.update(packet)
                            state["total"] = int(state["total"]) + len(packet)
                        state["index"] = index + 1
                        buffer.clear()
                offset += 27 + segment_count + body_size
    except OSError as exc:
        raise EmbedError(f"failed to locate Ogg payload in {path}: {exc}") from exc
    if offset != size or len(states) != 1:
        raise EmbedError(f"Ogg file must contain exactly one complete logical stream: {path}")
    state = next(iter(states.values()))
    buffer = state["buffer"]
    first = bytes(state["first"])
    if (
        not isinstance(buffer, bytearray)
        or buffer
        or int(state["index"]) < 3
        or not (first.startswith(b"OpusHead") or first.startswith(b"\x01vorbis"))
    ):
        raise EmbedError(f"unsupported or incomplete Ogg audio stream: {path}")
    digest = state["digest"]
    return int(state["total"]), digest.hexdigest()


def _encoded_audio_payload_digest(path: Path | int, audio: object) -> tuple[int, str]:
    if isinstance(audio, MP3):
        return _mp3_payload_digest(path)
    if isinstance(audio, FLAC):
        return _flac_payload_digest(path)
    if isinstance(audio, MP4):
        return _mp4_payload_digest(path)
    if isinstance(audio, WAVE):
        return _riff_payload_digest(path)
    if isinstance(audio, AIFF):
        return _aiff_payload_digest(path)
    if isinstance(audio, WavPack):
        return _wavpack_payload_digest(path)
    if isinstance(audio, (OggOpus, OggVorbis)):
        return _ogg_payload_digest(path)
    raise EmbedError(f"cannot hash payload for unsupported container {type(audio).__name__}")


def _semantic_snapshot(path: Path | int) -> tuple[object, ...]:
    try:
        with _binary_source(path) as handle:
            audio = mutagen.File(handle)
    except (OSError, mutagen.MutagenError) as exc:
        raise EmbedError(f"cannot verify preserved metadata in {path}: {exc}") from exc
    if audio is None:
        raise EmbedError(f"cannot verify preserved metadata in {path}")
    info = getattr(audio, "info", None)
    stream = tuple(
        (name, _stable_value(getattr(info, name, None)))
        for name in ("sample_rate", "channels", "bits_per_sample", "bitrate", "length")
    )
    nonfront: list[object] = []
    tags: list[object] = []
    if isinstance(audio, (MP3, WAVE, AIFF)):
        for frame in audio.tags.values() if audio.tags is not None else []:
            if isinstance(frame, APIC):
                if frame.type != PictureType.COVER_FRONT:
                    nonfront.append(
                        (
                            frame.HashKey,
                            int(frame.encoding),
                            frame.mime,
                            int(frame.type),
                            frame.desc,
                            _stable_value(frame.data),
                        )
                    )
            else:
                tags.append((frame.HashKey, _stable_value(frame)))
        tag_version = audio.tags.version[1] if audio.tags is not None else None
    elif isinstance(audio, FLAC):
        tags = [
            (str(key).casefold(), _stable_value(value)) for key, value in (audio.tags or {}).items()
        ]
        nonfront = [
            (
                int(picture.type),
                picture.mime,
                picture.desc,
                int(picture.width),
                int(picture.height),
                int(picture.depth),
                int(picture.colors),
                _stable_value(picture.data),
            )
            for picture in audio.pictures
            if picture.type != PictureType.COVER_FRONT
        ]
        tag_version = None
    elif isinstance(audio, (OggOpus, OggVorbis)):
        for key, value in (audio.tags or {}).items():
            if str(key).casefold() != "metadata_block_picture":
                tags.append((str(key).casefold(), _stable_value(value)))
        for _encoded, picture in _parse_xiph_pictures(
            (audio.tags or {}).get("metadata_block_picture", [])
        ):
            if picture is not None and picture.type != PictureType.COVER_FRONT:
                nonfront.append(
                    (
                        int(picture.type),
                        picture.mime,
                        picture.desc,
                        int(picture.width),
                        int(picture.height),
                        int(picture.depth),
                        int(picture.colors),
                        _stable_value(picture.data),
                    )
                )
        tag_version = None
    elif isinstance(audio, MP4):
        tags = [
            (str(key), _stable_value(value))
            for key, value in (audio.tags or {}).items()
            if key != "covr"
        ]
        tag_version = None
    elif isinstance(audio, WavPack):
        tags = [
            (str(key).casefold(), _stable_value(value))
            for key, value in (audio.tags or {}).items()
            if str(key).casefold() != "cover art (front)".casefold()
        ]
        tag_version = None
    else:
        raise EmbedError(f"cannot snapshot unsupported metadata container {type(audio).__name__}")
    ape_snapshot: tuple[object, ...] | None = None
    payload_digest = _encoded_audio_payload_digest(path, audio)
    if isinstance(audio, MP3):
        ape_tags = _apev2_tags(path)
        ape_snapshot = (
            tuple(
                sorted(
                    (str(key).casefold(), _stable_value(value)) for key, value in ape_tags.items()
                )
            )
            if ape_tags is not None
            else ()
        )
    return (
        type(audio).__name__,
        stream,
        tag_version,
        tuple(sorted(tags, key=str)),
        tuple(sorted(nonfront, key=str)),
        _read_id3v1(path) if isinstance(audio, MP3) else None,
        ape_snapshot,
        payload_digest,
    )


def _semantic_snapshot_preserved(
    before: tuple[object, ...],
    after: tuple[object, ...],
) -> bool:
    if after == before:
        return True
    return (
        before[0] in {"MP3", "WAVE", "AIFF"}
        and before[0] == after[0]
        and before[2] is None
        and after[2] == 4
        and before[:2] == after[:2]
        and before[3:] == after[3:]
    )


def _embed_artwork_in_place(
    path: Path | int,
    artwork: Artwork,
    *,
    replace_existing: bool = False,
    display_path: Path | None = None,
) -> EmbedResult:
    """Embed a verified front cover through one already-bound file object."""
    shown_path = display_path or (path if isinstance(path, Path) else Path(f"fd:{path}"))
    plan = _preflight_opened(
        path,
        shown_path,
        artwork,
        replace_existing=replace_existing,
    )
    if plan.status != "ready":
        return plan
    try:
        audio = _load_mutagen(path)
    except (OSError, mutagen.MutagenError) as exc:
        raise EmbedError(f"cannot open {shown_path}: {exc}") from exc
    if audio is None:
        raise EmbedError(f"unsupported or unreadable audio file: {shown_path}")
    return adapter_for(audio).embed(
        audio,
        path,
        artwork,
        replace_existing=replace_existing,
    )


def _require_source_identity(
    path: Path,
    expected: tuple[int, int, int, int, int] | None,
) -> os.stat_result:
    parent_descriptor = -1
    source_descriptor = -1
    try:
        parent_descriptor, source_descriptor, info = _open_regular_source(path, expected)
        return info
    except (OSError, EmbedError) as exc:
        if isinstance(exc, EmbedError):
            raise
        raise EmbedError(f"audio source disappeared or cannot be inspected: {path}: {exc}") from exc
    finally:
        if source_descriptor >= 0:
            os.close(source_descriptor)
        if parent_descriptor >= 0:
            os.close(parent_descriptor)


def _verify_group_sources(group: AlbumGroup) -> None:
    for path, expected in group.source_identities:
        _require_source_identity(path, expected)


def _xattr_snapshot(path: Path | int) -> tuple[tuple[str, bytes], ...]:
    if not hasattr(os, "listxattr"):
        return ()
    try:
        if isinstance(path, int):
            names = os.listxattr(path)
            return tuple(sorted((name, os.getxattr(path, name)) for name in names))
        names = os.listxattr(path, follow_symlinks=False)
        return tuple(
            sorted((name, os.getxattr(path, name, follow_symlinks=False)) for name in names)
        )
    except OSError as exc:
        unsupported = {errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)}
        if exc.errno in unsupported:
            return ()
        raise EmbedError(f"failed to inspect extended attributes on {path}: {exc}") from exc


def _fsync_directory_descriptor(descriptor: int) -> None:
    os.fsync(descriptor)


def _rename_exchange(
    first_directory: int,
    first_name: str,
    second_directory: int,
    second_name: str,
) -> None:
    """Atomically exchange two existing directory entries (Linux renameat2)."""
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise EmbedError("secure compare-and-swap requires Linux renameat2(RENAME_EXCHANGE)")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    if (
        renameat2(
            first_directory,
            os.fsencode(first_name),
            second_directory,
            os.fsencode(second_name),
            _RENAME_EXCHANGE,
        )
        != 0
    ):
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


def _rename_noreplace(
    source_directory: int,
    source_name: str,
    destination_directory: int,
    destination_name: str,
) -> None:
    """Rename only when the destination does not exist (Linux renameat2)."""
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise EmbedError("safe fallback commit requires Linux renameat2(RENAME_NOREPLACE)")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    if (
        renameat2(
            source_directory,
            os.fsencode(source_name),
            destination_directory,
            os.fsencode(destination_name),
            _RENAME_NOREPLACE,
        )
        != 0
    ):
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


def _openat_regular(directory_descriptor: int, name: str) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    no_atime = getattr(os, "O_NOATIME", 0)
    try:
        descriptor = os.open(name, flags | no_atime, dir_fd=directory_descriptor)
    except OSError as exc:
        if not no_atime or exc.errno not in {errno.EPERM, errno.EACCES}:
            raise
        descriptor = os.open(name, flags, dir_fd=directory_descriptor)
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(descriptor)
        raise EmbedError(f"unsafe non-regular or linked transaction entry: {name}")
    return descriptor, info


def _create_staging_file(directory_descriptor: int, path: Path) -> tuple[int, str]:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | nofollow
    unsupported_flag_errors = {errno.EINVAL, errno.EOPNOTSUPP}
    for _attempt in range(64):
        name = f".{path.name}.artwork-{secrets.token_hex(16)}{path.suffix}"
        try:
            return os.open(name, flags, 0o600, dir_fd=directory_descriptor), name
        except FileExistsError:
            continue
        except OSError as exc:
            if not nofollow or exc.errno not in unsupported_flag_errors:
                raise
            # O_CREAT | O_EXCL already atomically refuses an existing symlink.
            # Some NAS-backed filesystems reject the redundant O_NOFOLLOW flag.
            try:
                descriptor = os.open(
                    name,
                    flags & ~nofollow,
                    0o600,
                    dir_fd=directory_descriptor,
                )
            except FileExistsError:
                continue
            return descriptor, name
    raise EmbedError(f"cannot allocate a private staging entry beside {path}")


def _create_transaction_journal(
    directory_descriptor: int,
    journal_name: str,
    *,
    target_name: str,
    backup_name: str,
    original_name: str,
    staging_name: str,
    source_inode: tuple[int, int],
    staging_inode: tuple[int, int],
    source_hash: str,
    staging_hash: str,
) -> tuple[int, int]:
    payload = (
        json.dumps(
            {
                "schema": _TRANSACTION_JOURNAL_SCHEMA,
                "target": target_name,
                "backup": backup_name,
                "original": original_name,
                "staging": staging_name,
                "source_inode": list(source_inode),
                "staging_inode": list(staging_inode),
                "source_sha256": source_hash,
                "staging_sha256": staging_hash,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("ascii")
    if len(payload) > _MAX_TRANSACTION_JOURNAL_BYTES:
        raise EmbedError("transaction journal exceeds its size limit")

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | nofollow
    descriptor = -1
    journal_inode: tuple[int, int] | None = None
    try:
        try:
            descriptor = os.open(journal_name, flags, 0o600, dir_fd=directory_descriptor)
        except OSError as exc:
            if not nofollow or exc.errno not in {errno.EINVAL, errno.EOPNOTSUPP}:
                raise
            descriptor = os.open(
                journal_name,
                flags & ~nofollow,
                0o600,
                dir_fd=directory_descriptor,
            )
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise EmbedError("transaction journal is not a private regular file")
        journal_inode = (info.st_dev, info.st_ino)
        written = 0
        while written < len(payload):
            count = os.write(descriptor, payload[written:])
            if count <= 0:
                raise OSError(errno.EIO, "short transaction journal write")
            written += count
        os.fsync(descriptor)
        _fsync_directory_descriptor(directory_descriptor)
        return journal_inode
    except BaseException:
        if journal_inode is not None:
            with suppress(OSError):
                _unlink_owned_entry(directory_descriptor, journal_name, journal_inode)
            with suppress(OSError):
                _fsync_directory_descriptor(directory_descriptor)
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _create_original_recovery_link(
    directory_descriptor: int,
    target_name: str,
    original_name: str,
    source_inode: tuple[int, int],
) -> tuple[int, int]:
    try:
        os.link(
            target_name,
            original_name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        info = _entry_stat(directory_descriptor, original_name)
        if not _same_inode(info, source_inode) or info.st_nlink != 2:
            raise EmbedError("original recovery link does not identify the verified source")
        _fsync_directory_descriptor(directory_descriptor)
        return source_inode
    except BaseException as exc:
        if not isinstance(exc, FileExistsError):
            with suppress(OSError):
                created_info = _entry_stat(directory_descriptor, original_name)
                _unlink_owned_entry(
                    directory_descriptor,
                    original_name,
                    (created_info.st_dev, created_info.st_ino),
                )
        with suppress(OSError):
            _fsync_directory_descriptor(directory_descriptor)
        raise


def _descriptor_sha256(descriptor: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while True:
        chunk = os.pread(descriptor, 1024 * 1024, offset)
        if not chunk:
            break
        digest.update(chunk)
        offset += len(chunk)
    return digest.hexdigest()


def _entry_stat(directory_descriptor: int, name: str) -> os.stat_result:
    return os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)


def _optional_entry_stat(directory_descriptor: int, name: str) -> os.stat_result | None:
    try:
        return _entry_stat(directory_descriptor, name)
    except FileNotFoundError:
        return None


def _same_inode(info: os.stat_result, identity: tuple[int, int]) -> bool:
    return (info.st_dev, info.st_ino) == identity


def _unlink_owned_entry(
    directory_descriptor: int,
    name: str,
    identity: tuple[int, int],
) -> None:
    try:
        info = _entry_stat(directory_descriptor, name)
    except FileNotFoundError:
        return
    if _same_inode(info, identity):
        os.unlink(name, dir_fd=directory_descriptor)


def _source_state_matches(
    descriptor: int,
    expected: os.stat_result,
    expected_hash: str,
    expected_xattrs: tuple[tuple[str, bytes], ...],
    *,
    expected_nlink: int = 1,
) -> bool:
    actual = os.fstat(descriptor)
    return (
        _same_inode(actual, (expected.st_dev, expected.st_ino))
        and actual.st_nlink == expected_nlink
        and actual.st_size == expected.st_size
        and actual.st_mtime_ns == expected.st_mtime_ns
        and actual.st_uid == expected.st_uid
        and actual.st_gid == expected.st_gid
        and stat.S_IMODE(actual.st_mode) == stat.S_IMODE(expected.st_mode)
        and _xattr_snapshot(descriptor) == expected_xattrs
        and _descriptor_sha256(descriptor) == expected_hash
    )


def _read_transaction_journal(
    directory_descriptor: int, journal_name: str
) -> tuple[dict[str, Any], tuple[int, int]]:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor = os.open(journal_name, flags, dir_fd=directory_descriptor)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size > _MAX_TRANSACTION_JOURNAL_BYTES
        ):
            raise EmbedError(f"unsafe transaction journal: {journal_name}")
        chunks: list[bytes] = []
        remaining = _MAX_TRANSACTION_JOURNAL_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(4096, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > _MAX_TRANSACTION_JOURNAL_BYTES:
            raise EmbedError(f"transaction journal is too large: {journal_name}")
    finally:
        os.close(descriptor)

    try:

        def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            decoded: dict[str, Any] = {}
            for key, value in pairs:
                if key in decoded:
                    raise ValueError(f"duplicate JSON key: {key}")
                decoded[key] = value
            return decoded

        payload = json.loads(raw.decode("ascii"), object_pairs_hook=reject_duplicate_keys)
    except (UnicodeDecodeError, ValueError) as exc:
        raise EmbedError(f"invalid transaction journal {journal_name}: {exc}") from exc
    if not isinstance(payload, dict):
        raise EmbedError(f"invalid transaction journal object: {journal_name}")
    expected_keys = {
        "schema",
        "target",
        "backup",
        "original",
        "staging",
        "source_inode",
        "staging_inode",
        "source_sha256",
        "staging_sha256",
    }
    if set(payload) != expected_keys or payload.get("schema") != _TRANSACTION_JOURNAL_SCHEMA:
        raise EmbedError(f"unsupported transaction journal schema: {journal_name}")
    names = [payload.get(key) for key in ("target", "backup", "original", "staging")]
    if (
        not all(
            isinstance(name, str)
            and name not in {"", ".", ".."}
            and os.path.basename(name) == name
            and "\x00" not in name
            for name in names
        )
        or len(set(names)) != 4
    ):
        raise EmbedError(f"unsafe transaction entry name in {journal_name}")
    target_name = payload["target"]
    backup_name = payload["backup"]
    original_name = payload["original"]
    staging_name = payload["staging"]
    transaction_prefix = f".{target_name}.artwork-transaction-"
    if journal_name.endswith("-restored.json"):
        transaction_suffix = "-restored.json"
        recovery_phase = "restored"
    else:
        transaction_suffix = ".json"
        recovery_phase = "prepared"
    transaction_token = journal_name[len(transaction_prefix) : -len(transaction_suffix)]
    target_suffix = Path(target_name).suffix
    staging_prefix = f".{target_name}.artwork-"
    staging_token = staging_name[len(staging_prefix) : -len(target_suffix)] if target_suffix else ""

    def is_lower_hex_token(value: str) -> bool:
        return len(value) == 32 and all(character in "0123456789abcdef" for character in value)

    if not (
        journal_name == f"{transaction_prefix}{transaction_token}{transaction_suffix}"
        and is_lower_hex_token(transaction_token)
        and backup_name == f".{target_name}.artwork-backup-{transaction_token}{target_suffix}"
        and original_name == f".{target_name}.artwork-original-{transaction_token}{target_suffix}"
        and staging_name == f"{staging_prefix}{staging_token}{target_suffix}"
        and is_lower_hex_token(staging_token)
    ):
        raise EmbedError(f"transaction journal names do not match: {journal_name}")
    for key in ("source_inode", "staging_inode"):
        inode = payload.get(key)
        if not (
            isinstance(inode, list)
            and len(inode) == 2
            and all(type(value) is int and value >= 0 for value in inode)
        ):
            raise EmbedError(f"invalid inode hint in {journal_name}")
    for key in ("source_sha256", "staging_sha256"):
        digest = payload.get(key)
        if not (
            isinstance(digest, str)
            and len(digest) == 64
            and all(character in "0123456789abcdef" for character in digest)
        ):
            raise EmbedError(f"invalid content digest in {journal_name}")
    if payload["source_sha256"] == payload["staging_sha256"]:
        raise EmbedError(f"identical source and staging digests in {journal_name}")
    payload["_recovery_phase"] = recovery_phase
    return payload, (info.st_dev, info.st_ino)


def _recovery_entry_kind(
    directory_descriptor: int,
    name: str,
    source_hash: str,
    staging_hash: str,
) -> tuple[str, os.stat_result | None]:
    info = _optional_entry_stat(directory_descriptor, name)
    if info is None:
        return "absent", None
    if not stat.S_ISREG(info.st_mode):
        return "other", info
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    no_atime = getattr(os, "O_NOATIME", 0)
    try:
        descriptor = os.open(name, flags | no_atime, dir_fd=directory_descriptor)
    except OSError as exc:
        if not no_atime or exc.errno not in {errno.EPERM, errno.EACCES}:
            raise
        descriptor = os.open(name, flags, dir_fd=directory_descriptor)
    try:
        digest = _descriptor_sha256(descriptor)
    finally:
        os.close(descriptor)
    if digest == source_hash:
        return "source", info
    if digest == staging_hash:
        return "staging", info
    return "other", info


def _remove_classified_recovery_entry(
    directory_descriptor: int,
    name: str,
    expected_kind: str,
    source_hash: str,
    staging_hash: str,
) -> None:
    kind, info = _recovery_entry_kind(directory_descriptor, name, source_hash, staging_hash)
    if kind == "absent":
        return
    if kind != expected_kind or info is None:
        raise EmbedError(f"recovery entry changed unexpectedly: {name}")
    os.unlink(name, dir_fd=directory_descriptor)
    if _optional_entry_stat(directory_descriptor, name) is not None:
        raise EmbedError(f"recovery entry was not removed: {name}")


def _remove_validated_transaction_journal(
    directory_descriptor: int,
    journal_name: str,
) -> None:
    _read_transaction_journal(directory_descriptor, journal_name)
    os.unlink(journal_name, dir_fd=directory_descriptor)
    if _optional_entry_stat(directory_descriptor, journal_name) is not None:
        raise EmbedError(f"transaction journal was not removed: {journal_name}")


def _recover_transaction_journal(journal_path: Path) -> Path:
    directory = journal_path.parent
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory_descriptor = os.open(directory, flags | nofollow)
    try:
        payload, _journal_inode = _read_transaction_journal(directory_descriptor, journal_path.name)
        recovery_phase = payload.pop("_recovery_phase")
        assert recovery_phase in {"prepared", "restored"}
        journal_name = journal_path.name
        target_name = payload["target"]
        backup_name = payload["backup"]
        original_name = payload["original"]
        staging_name = payload["staging"]
        source_hash = payload["source_sha256"]
        staging_hash = payload["staging_sha256"]
        assert all(
            isinstance(value, str)
            for value in (
                target_name,
                backup_name,
                original_name,
                staging_name,
                source_hash,
                staging_hash,
            )
        )

        target_kind, _ = _recovery_entry_kind(
            directory_descriptor, target_name, source_hash, staging_hash
        )
        backup_kind, _ = _recovery_entry_kind(
            directory_descriptor, backup_name, source_hash, staging_hash
        )
        original_kind, _ = _recovery_entry_kind(
            directory_descriptor, original_name, source_hash, staging_hash
        )
        staging_kind, _ = _recovery_entry_kind(
            directory_descriptor, staging_name, source_hash, staging_hash
        )

        if target_kind == "source":
            if backup_kind not in {"absent", "source"}:
                raise EmbedError(
                    f"incomplete transaction has an unrelated backup entry: {journal_path}"
                )
        elif target_kind == "absent":
            if backup_kind == "source":
                restore_name = backup_name
            elif backup_kind == "other" and original_kind == "source":
                # Record durable intent before the rename destroys the evidence that
                # the unrelated backup is the displaced concurrent save.
                if recovery_phase == "prepared":
                    restored_journal_name = f"{journal_name.removesuffix('.json')}-restored.json"
                    _rename_noreplace(
                        directory_descriptor,
                        journal_name,
                        directory_descriptor,
                        restored_journal_name,
                    )
                    _fsync_directory_descriptor(directory_descriptor)
                    journal_name = restored_journal_name
                    journal_path = directory / journal_name
                    recovery_phase = "restored"
                restore_name = backup_name
            elif backup_kind == "absent" and original_kind == "source":
                restore_name = original_name
            else:
                raise EmbedError(
                    f"incomplete transaction has no safely restorable target: {journal_path}"
                )
            _rename_noreplace(
                directory_descriptor,
                restore_name,
                directory_descriptor,
                target_name,
            )
            _fsync_directory_descriptor(directory_descriptor)
            target_kind, _ = _recovery_entry_kind(
                directory_descriptor, target_name, source_hash, staging_hash
            )
        elif target_kind == "staging":
            if staging_kind != "absent":
                raise EmbedError(
                    f"incomplete transaction has duplicate staged entries: {journal_path}"
                )
            if backup_kind == "source":
                restore_name = backup_name
            elif original_kind == "source":
                restore_name = original_name
            elif backup_kind == original_kind == "absent":
                # Recovery material was already released: the commit is irreversible.
                _remove_validated_transaction_journal(directory_descriptor, journal_name)
                _fsync_directory_descriptor(directory_descriptor)
                return directory / target_name
            else:
                raise EmbedError(f"incomplete transaction has no verified original: {journal_path}")
            _rename_noreplace(
                directory_descriptor,
                target_name,
                directory_descriptor,
                staging_name,
            )
            moved_kind, _ = _recovery_entry_kind(
                directory_descriptor,
                staging_name,
                source_hash,
                staging_hash,
            )
            if moved_kind != "staging":
                _rename_noreplace(
                    directory_descriptor,
                    staging_name,
                    directory_descriptor,
                    target_name,
                )
                _fsync_directory_descriptor(directory_descriptor)
                raise EmbedError(
                    f"concurrent save replaced the transaction target during recovery; "
                    f"restored and retained: {journal_path}"
                )
            _rename_noreplace(
                directory_descriptor,
                restore_name,
                directory_descriptor,
                target_name,
            )
            _fsync_directory_descriptor(directory_descriptor)
            target_kind = "source"
        elif target_kind == "other":
            if not (
                recovery_phase == "restored"
                and backup_kind == "absent"
                and original_kind in {"source", "absent"}
            ):
                raise EmbedError(
                    "incomplete transaction target contains unrelated data; "
                    f"retained: {journal_path}"
                )
        else:
            raise EmbedError(
                f"incomplete transaction target contains unrelated data; retained: {journal_path}"
            )

        if target_kind not in {"source", "other"}:
            raise EmbedError(f"transaction recovery did not restore a visible file: {journal_path}")
        if target_kind == "other" and recovery_phase != "restored":
            raise EmbedError(
                f"unrelated target appeared during transaction recovery; retained: {journal_path}"
            )

        current_target_kind, _ = _recovery_entry_kind(
            directory_descriptor,
            target_name,
            source_hash,
            staging_hash,
        )
        if current_target_kind != target_kind:
            raise EmbedError(
                f"transaction target changed before recovery cleanup; retained: {journal_path}"
            )

        _remove_classified_recovery_entry(
            directory_descriptor,
            original_name,
            "source",
            source_hash,
            staging_hash,
        )
        _remove_classified_recovery_entry(
            directory_descriptor,
            backup_name,
            "source" if target_kind == "source" else "other",
            source_hash,
            staging_hash,
        )
        _remove_classified_recovery_entry(
            directory_descriptor,
            staging_name,
            "staging",
            source_hash,
            staging_hash,
        )
        _remove_validated_transaction_journal(directory_descriptor, journal_name)
        _fsync_directory_descriptor(directory_descriptor)
        return directory / target_name
    finally:
        os.close(directory_descriptor)


def find_incomplete_transactions(root: Path) -> tuple[Path, ...]:
    root = Path(root)
    journals: list[Path] = []
    for directory, child_directories, files in os.walk(root, followlinks=False):
        base = Path(directory)
        child_directories[:] = [
            name for name in child_directories if not (base / name).is_symlink()
        ]
        journals.extend(
            base / name
            for name in files
            if name.startswith(".") and ".artwork-transaction-" in name and name.endswith(".json")
        )
    return tuple(sorted(journals, key=str))


def recover_transaction_journals(journal_paths: tuple[Path, ...]) -> tuple[Path, ...]:
    recovered: list[Path] = []
    for journal_path in sorted(journal_paths, key=str):
        recovered.append(_recover_transaction_journal(journal_path))
    return tuple(recovered)


def recover_incomplete_transactions(root: Path) -> tuple[Path, ...]:
    return recover_transaction_journals(find_incomplete_transactions(root))


def embed_artwork(
    path: Path,
    artwork: Artwork,
    *,
    replace_existing: bool = False,
    expected_identity: tuple[int, int, int, int, int] | None = None,
    _on_committed: Callable[[EmbedResult], None] | None = None,
) -> EmbedResult:
    """Stage, verify, compare-and-swap, and durably commit one artwork update."""
    path = Path(path)
    parent_descriptor = -1
    source_descriptor = -1
    staging_descriptor = -1
    staging_name: str | None = None
    staging_inode: tuple[int, int] | None = None
    backup_name: str | None = None
    backup_inode: tuple[int, int] | None = None
    original_name: str | None = None
    original_inode: tuple[int, int] | None = None
    journal_name: str | None = None
    journal_inode: tuple[int, int] | None = None
    source_inode = (-1, -1)
    commit_method: str | None = None
    source_moved = False
    exchanged = False
    backup_released = False
    retain_transaction = False
    result: EmbedResult | None = None

    try:
        parent_descriptor, source_descriptor, source_info = _open_regular_source(
            path,
            expected_identity,
        )
        plan = _preflight_opened(
            source_descriptor,
            path,
            artwork,
            replace_existing=replace_existing,
        )
        if plan.status != "ready":
            return plan

        source_identity = _stat_identity(source_info)
        source_inode = (source_info.st_dev, source_info.st_ino)
        source_mode = stat.S_IMODE(source_info.st_mode)
        source_xattrs = _xattr_snapshot(source_descriptor)
        staging_descriptor, staging_name = _create_staging_file(parent_descriptor, path)

        digest = hashlib.sha256()
        offset = 0
        while True:
            chunk = os.pread(source_descriptor, 1024 * 1024, offset)
            if not chunk:
                break
            digest.update(chunk)
            written = 0
            while written < len(chunk):
                count = os.pwrite(staging_descriptor, chunk[written:], offset + written)
                if count <= 0:
                    raise OSError(errno.EIO, "short staging write")
                written += count
            offset += len(chunk)
        source_hash = digest.hexdigest()
        if _stat_identity(os.fstat(source_descriptor)) != source_identity:
            raise EmbedError(f"audio file changed while being staged: {path}")

        try:
            os.fchown(staging_descriptor, source_info.st_uid, source_info.st_gid)
            os.fchmod(staging_descriptor, source_mode)
            for name, value in source_xattrs:
                os.setxattr(staging_descriptor, name, value)
            os.utime(
                staging_descriptor,
                ns=(source_info.st_atime_ns, source_info.st_mtime_ns),
            )
            os.fsync(staging_descriptor)
        except OSError as exc:
            raise EmbedError(
                f"cannot preserve source filesystem metadata for {path}: {exc}"
            ) from exc

        staged_info = os.fstat(staging_descriptor)
        staging_inode = (staged_info.st_dev, staged_info.st_ino)
        if not _same_inode(_entry_stat(parent_descriptor, staging_name), staging_inode):
            raise EmbedError(f"staging entry changed unexpectedly for {path}")
        semantic_before = _semantic_snapshot(staging_descriptor)

        result = _embed_artwork_in_place(
            staging_descriptor,
            artwork,
            replace_existing=replace_existing,
            display_path=path,
        )
        if result.status != "embedded":
            return result
        semantic_after = _semantic_snapshot(staging_descriptor)
        if not _semantic_snapshot_preserved(semantic_before, semantic_after):
            raise EmbedError(f"unrelated metadata or encoded audio was not preserved for {path}")

        try:
            os.utime(
                staging_descriptor,
                ns=(source_info.st_atime_ns, source_info.st_mtime_ns),
            )
            os.fsync(staging_descriptor)
        except OSError as exc:
            raise EmbedError(f"cannot restore source timestamps for {path}: {exc}") from exc
        updated_info = os.fstat(staging_descriptor)
        if (
            updated_info.st_uid != source_info.st_uid
            or updated_info.st_gid != source_info.st_gid
            or stat.S_IMODE(updated_info.st_mode) != source_mode
            or updated_info.st_atime_ns != source_info.st_atime_ns
            or updated_info.st_mtime_ns != source_info.st_mtime_ns
            or _xattr_snapshot(staging_descriptor) != source_xattrs
        ):
            raise EmbedError(f"filesystem metadata changed during staged update for {path}")

        if _stat_identity(os.fstat(source_descriptor)) != source_identity:
            raise EmbedError(f"audio file changed concurrently before commit: {path}")
        current_source = _entry_stat(parent_descriptor, path.name)
        if _stat_identity(current_source) != source_identity:
            raise EmbedError(f"audio file changed concurrently before commit: {path}")
        current_staging = _entry_stat(parent_descriptor, staging_name)
        if not _same_inode(current_staging, staging_inode):
            raise EmbedError(f"staging entry changed before commit: {path}")

        try:
            _rename_exchange(parent_descriptor, staging_name, parent_descriptor, path.name)
            backup_name = staging_name
            backup_inode = source_inode
            commit_method = "exchange"
        except OSError as exchange_error:
            if exchange_error.errno not in {errno.EINVAL, errno.EOPNOTSUPP}:
                raise
            interrupted_target = _entry_stat(parent_descriptor, path.name)
            interrupted_staging = _entry_stat(parent_descriptor, staging_name)
            if _same_inode(interrupted_target, staging_inode):
                _rename_exchange(
                    parent_descriptor,
                    staging_name,
                    parent_descriptor,
                    path.name,
                )
                _fsync_directory_descriptor(parent_descriptor)
                raise EmbedError(
                    f"atomic exchange for {path} completed but reported an error; "
                    "the visible file was safely restored"
                ) from exchange_error
            if not (
                _same_inode(interrupted_target, source_inode)
                and _same_inode(interrupted_staging, staging_inode)
            ):
                raise EmbedError(f"audio file changed concurrently before commit: {path}") from None
            transaction_token = secrets.token_hex(16)
            backup_name = f".{path.name}.artwork-backup-{transaction_token}{path.suffix}"
            original_name = f".{path.name}.artwork-original-{transaction_token}{path.suffix}"
            journal_name = f".{path.name}.artwork-transaction-{transaction_token}.json"
            backup_inode = source_inode
            commit_method = "noreplace"
            staging_hash = _descriptor_sha256(staging_descriptor)
            os.utime(
                staging_descriptor,
                ns=(source_info.st_atime_ns, source_info.st_mtime_ns),
            )
            os.fsync(staging_descriptor)
            journal_inode = _create_transaction_journal(
                parent_descriptor,
                journal_name,
                target_name=path.name,
                backup_name=backup_name,
                original_name=original_name,
                staging_name=staging_name,
                source_inode=source_inode,
                staging_inode=staging_inode,
                source_hash=source_hash,
                staging_hash=staging_hash,
            )
            original_inode = _create_original_recovery_link(
                parent_descriptor,
                path.name,
                original_name,
                source_inode,
            )
            current_source = _entry_stat(parent_descriptor, path.name)
            current_original = _entry_stat(parent_descriptor, original_name)
            if not (
                _same_inode(current_source, source_inode)
                and _same_inode(current_original, source_inode)
                and _source_state_matches(
                    source_descriptor,
                    source_info,
                    source_hash,
                    source_xattrs,
                    expected_nlink=2,
                )
            ):
                raise EmbedError(f"audio file changed before fallback commit: {path}") from None

            retain_transaction = True
            try:
                _rename_noreplace(
                    parent_descriptor,
                    path.name,
                    parent_descriptor,
                    backup_name,
                )
            except BaseException:
                try:
                    interrupted_target = _optional_entry_stat(parent_descriptor, path.name)
                    interrupted_backup = _optional_entry_stat(parent_descriptor, backup_name)
                except OSError:
                    retain_transaction = True
                else:
                    if (
                        interrupted_target is None
                        and interrupted_backup is not None
                        and _same_inode(interrupted_backup, source_inode)
                    ):
                        source_moved = True
                        retain_transaction = False
                    elif (
                        interrupted_target is not None
                        and _same_inode(interrupted_target, source_inode)
                        and interrupted_backup is None
                    ):
                        source_moved = False
                        retain_transaction = False
                    else:
                        retain_transaction = True
                raise
            source_moved = True
            retain_transaction = False

            displaced_entry = _entry_stat(parent_descriptor, backup_name)
            if not _same_inode(displaced_entry, source_inode):
                try:
                    _rename_noreplace(
                        parent_descriptor,
                        backup_name,
                        parent_descriptor,
                        path.name,
                    )
                    _fsync_directory_descriptor(parent_descriptor)
                    source_moved = False
                except BaseException as restore_error:
                    retain_transaction = True
                    raise EmbedCommittedError(
                        f"concurrent save detected for {path}; all versions retained because "
                        f"the concurrent path could not be restored safely: {restore_error}",
                        result,
                    ) from restore_error
                if original_inode is not None:
                    _unlink_owned_entry(parent_descriptor, original_name, original_inode)
                    original_inode = None
                _unlink_owned_entry(parent_descriptor, staging_name, staging_inode)
                staging_inode = None
                if journal_inode is not None:
                    _unlink_owned_entry(parent_descriptor, journal_name, journal_inode)
                    journal_inode = None
                _fsync_directory_descriptor(parent_descriptor)
                raise EmbedError(
                    f"concurrent atomic save preserved for {path}; artwork update aborted"
                ) from None

            backup_descriptor = -1
            try:
                backup_descriptor = os.open(
                    backup_name,
                    os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=parent_descriptor,
                )
                if not _source_state_matches(
                    backup_descriptor,
                    source_info,
                    source_hash,
                    source_xattrs,
                    expected_nlink=2,
                ):
                    retain_transaction = True
                    raise EmbedCommittedError(
                        f"fallback backup verification failed for {path}; recovery journal "
                        f"retained as {journal_name}",
                        result,
                    )
            finally:
                if backup_descriptor >= 0:
                    os.close(backup_descriptor)

            retain_transaction = True
            try:
                _rename_noreplace(
                    parent_descriptor,
                    staging_name,
                    parent_descriptor,
                    path.name,
                )
            except BaseException:
                try:
                    interrupted_target = _optional_entry_stat(parent_descriptor, path.name)
                    interrupted_staging = _optional_entry_stat(parent_descriptor, staging_name)
                except OSError:
                    retain_transaction = True
                else:
                    if (
                        interrupted_target is not None
                        and _same_inode(interrupted_target, staging_inode)
                        and interrupted_staging is None
                    ):
                        exchanged = True
                        retain_transaction = False
                    elif interrupted_target is None and (
                        interrupted_staging is not None
                        and _same_inode(interrupted_staging, staging_inode)
                    ):
                        exchanged = False
                        retain_transaction = False
                    else:
                        retain_transaction = True
                raise
        except BaseException:
            try:
                interrupted_backup = _entry_stat(parent_descriptor, staging_name)
                interrupted_target = _entry_stat(parent_descriptor, path.name)
            except OSError:
                pass
            else:
                if _same_inode(interrupted_backup, source_inode) and _same_inode(
                    interrupted_target, staging_inode
                ):
                    backup_name = staging_name
                    backup_inode = source_inode
                    commit_method = "exchange"
                    exchanged = True
            raise
        exchanged = True
        retain_transaction = False

        assert backup_name is not None
        backup_descriptor = -1
        try:
            if commit_method == "noreplace":
                backup_descriptor = os.open(
                    backup_name,
                    os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=parent_descriptor,
                )
                backup_info = os.fstat(backup_descriptor)
                expected_backup_links = 2
            else:
                backup_descriptor, backup_info = _openat_regular(parent_descriptor, backup_name)
                backup_inode = (backup_info.st_dev, backup_info.st_ino)
                expected_backup_links = 1
            committed_entry = _entry_stat(parent_descriptor, path.name)
            cas_valid = (
                _same_inode(committed_entry, staging_inode)
                and _same_inode(backup_info, source_inode)
                and _source_state_matches(
                    backup_descriptor,
                    source_info,
                    source_hash,
                    source_xattrs,
                    expected_nlink=expected_backup_links,
                )
            )
            if commit_method == "noreplace":
                assert original_name is not None
                original_entry = _entry_stat(parent_descriptor, original_name)
                cas_valid = cas_valid and _same_inode(original_entry, source_inode)
        finally:
            if backup_descriptor >= 0:
                os.close(backup_descriptor)

        if not cas_valid:
            backup_entry = _entry_stat(parent_descriptor, backup_name)
            committed_entry = _entry_stat(parent_descriptor, path.name)
            expected_displaced_inode = backup_inode if commit_method == "exchange" else source_inode
            if not (
                expected_displaced_inode is not None
                and _same_inode(backup_entry, expected_displaced_inode)
                and _same_inode(committed_entry, staging_inode)
            ):
                backup_inode = None
                retain_transaction = commit_method == "noreplace"
                raise EmbedCommittedError(
                    f"compare-and-swap state became ambiguous for {path}; "
                    f"original retained as {backup_name}",
                    result,
                )
            raise EmbedError(f"audio file changed during compare-and-swap: {path}")

        _fsync_directory_descriptor(parent_descriptor)
        committed_info = os.fstat(staging_descriptor)
        if (
            committed_info.st_uid != source_info.st_uid
            or committed_info.st_gid != source_info.st_gid
            or stat.S_IMODE(committed_info.st_mode) != source_mode
            or committed_info.st_atime_ns != source_info.st_atime_ns
            or committed_info.st_mtime_ns != source_info.st_mtime_ns
            or _xattr_snapshot(staging_descriptor) != source_xattrs
        ):
            raise EmbedError(f"committed file metadata verification failed for {path}")

        backup_entry = _entry_stat(parent_descriptor, backup_name)
        if not _same_inode(backup_entry, source_inode):
            backup_inode = None
            retain_transaction = commit_method == "noreplace"
            raise EmbedCommittedError(
                f"original backup entry changed before release for {path}: {backup_name}",
                result,
            )
        if commit_method == "noreplace":
            assert original_name is not None
            original_entry = _entry_stat(parent_descriptor, original_name)
            if not _same_inode(original_entry, source_inode):
                original_inode = None
                retain_transaction = True
                raise EmbedCommittedError(
                    f"original recovery entry changed before release for {path}: {original_name}",
                    result,
                )
            _unlink_owned_entry(parent_descriptor, original_name, source_inode)
            original_inode = None
        _unlink_owned_entry(parent_descriptor, backup_name, source_inode)
        backup_inode = None
        backup_released = True
        _fsync_directory_descriptor(parent_descriptor)
        if commit_method == "noreplace":
            assert journal_name is not None and journal_inode is not None
            _unlink_owned_entry(parent_descriptor, journal_name, journal_inode)
            journal_inode = None
            _fsync_directory_descriptor(parent_descriptor)
        if _on_committed is not None:
            _on_committed(result)
        return result
    except BaseException as exc:
        if backup_released and result is not None:
            if isinstance(exc, KeyboardInterrupt):
                raise EmbedCommittedInterrupt(
                    f"interrupted after artwork was committed to {path}",
                    result,
                    path,
                ) from exc
            if isinstance(exc, SystemExit):
                raise
            if isinstance(exc, EmbedCommittedError):
                raise
            raise EmbedCommittedError(
                f"artwork was committed but final durability reporting failed for {path}: {exc}",
                result,
            ) from exc
        if retain_transaction and result is not None:
            if isinstance(exc, KeyboardInterrupt):
                raise EmbedCommittedInterrupt(
                    f"interrupted during CIFS fallback for {path}; transaction journal and "
                    "all recovery entries were retained",
                    result,
                    path,
                ) from exc
            if isinstance(exc, SystemExit):
                raise
            if isinstance(exc, EmbedCommittedError):
                raise
            raise EmbedCommittedError(
                f"CIFS fallback state is uncertain for {path}; transaction journal and all "
                f"recovery entries were retained: {exc}",
                result,
            ) from exc
        if (
            (exchanged or (commit_method == "noreplace" and source_moved))
            and not backup_released
            and result is not None
            and staging_name is not None
            and backup_name is not None
        ):
            try:
                backup_entry = _entry_stat(parent_descriptor, backup_name)
                expected_rollback_inode = (
                    backup_inode if commit_method == "exchange" else source_inode
                )
                if expected_rollback_inode is None or not _same_inode(
                    backup_entry, expected_rollback_inode
                ):
                    raise EmbedError("transaction backup no longer permits safe rollback")
                if commit_method == "exchange":
                    committed_entry = _entry_stat(parent_descriptor, path.name)
                    if staging_inode is None or not _same_inode(committed_entry, staging_inode):
                        raise EmbedError("transaction target no longer permits safe rollback")
                    _rename_exchange(parent_descriptor, backup_name, parent_descriptor, path.name)
                    exchanged = False
                    backup_inode = None
                elif commit_method == "noreplace":
                    if staging_inode is None:
                        raise EmbedError("staging identity is unavailable during rollback")
                    if exchanged:
                        committed_entry = _entry_stat(parent_descriptor, path.name)
                        if not _same_inode(committed_entry, staging_inode):
                            raise EmbedError("transaction target no longer permits safe rollback")
                        _rename_noreplace(
                            parent_descriptor,
                            path.name,
                            parent_descriptor,
                            staging_name,
                        )
                        exchanged = False
                    else:
                        if _optional_entry_stat(parent_descriptor, path.name) is not None:
                            raise EmbedError(
                                "another file appeared at the transaction target during rollback"
                            )
                        staged_entry = _entry_stat(parent_descriptor, staging_name)
                        if not _same_inode(staged_entry, staging_inode):
                            raise EmbedError("staging entry no longer permits safe rollback")
                    _rename_noreplace(
                        parent_descriptor,
                        backup_name,
                        parent_descriptor,
                        path.name,
                    )
                    source_moved = False
                    backup_inode = None
                else:
                    raise EmbedError("transaction commit method is unknown")
            except BaseException as rollback_error:
                retain_transaction = commit_method == "noreplace"
                if isinstance(exc, SystemExit):
                    raise exc from rollback_error
                if isinstance(rollback_error, KeyboardInterrupt) or isinstance(
                    exc, KeyboardInterrupt
                ):
                    raise EmbedCommittedInterrupt(
                        f"interrupted after artwork commit for {path}; "
                        f"rollback could not be verified: {rollback_error}",
                        result,
                        path,
                    ) from rollback_error
                if isinstance(rollback_error, SystemExit):
                    raise
                raise EmbedCommittedError(
                    f"artwork commit for {path} could not be rolled back safely: {rollback_error}",
                    result,
                ) from exc
            try:
                _fsync_directory_descriptor(parent_descriptor)
            except BaseException as rollback_durability_error:
                retain_transaction = commit_method == "noreplace"
                if commit_method == "exchange":
                    retained_name = staging_name
                    staging_inode = None
                    backup_inode = None
                else:
                    retained_name = journal_name or staging_name
                if isinstance(exc, SystemExit):
                    raise exc from rollback_durability_error
                if isinstance(rollback_durability_error, KeyboardInterrupt) or isinstance(
                    exc, KeyboardInterrupt
                ):
                    raise EmbedCommittedInterrupt(
                        f"interrupted after rollback restored {path}, but rollback durability "
                        f"could not be verified; recovery entry retained as {retained_name}",
                        result,
                        path,
                    ) from rollback_durability_error
                if isinstance(rollback_durability_error, SystemExit):
                    raise
                raise EmbedCommittedError(
                    f"rollback restored {path}, but rollback durability could not be verified; "
                    f"recovery entry retained as {retained_name}: {rollback_durability_error}",
                    result,
                ) from rollback_durability_error
            if commit_method == "noreplace":
                try:
                    if original_name is not None and original_inode is not None:
                        _unlink_owned_entry(parent_descriptor, original_name, original_inode)
                        original_inode = None
                    if staging_inode is not None:
                        _unlink_owned_entry(parent_descriptor, staging_name, staging_inode)
                        staging_inode = None
                    if journal_name is not None and journal_inode is not None:
                        _unlink_owned_entry(parent_descriptor, journal_name, journal_inode)
                        journal_inode = None
                    _fsync_directory_descriptor(parent_descriptor)
                except BaseException as cleanup_durability_error:
                    retain_transaction = True
                    if isinstance(exc, SystemExit):
                        raise exc from cleanup_durability_error
                    if isinstance(cleanup_durability_error, KeyboardInterrupt) or isinstance(
                        exc, KeyboardInterrupt
                    ):
                        raise EmbedCommittedInterrupt(
                            f"interrupted after rollback durably restored {path}, but recovery "
                            "cleanup durability could not be verified",
                            result,
                            path,
                        ) from cleanup_durability_error
                    if isinstance(cleanup_durability_error, SystemExit):
                        raise
                    raise EmbedCommittedError(
                        f"rollback durably restored {path}, but recovery cleanup durability "
                        f"could not be verified: {cleanup_durability_error}",
                        result,
                    ) from cleanup_durability_error
        if isinstance(exc, EmbedError):
            raise
        if isinstance(exc, OSError):
            raise EmbedError(f"failed to stage artwork update for {path}: {exc}") from exc
        raise
    finally:
        cleanup_changed = False
        if not retain_transaction and parent_descriptor >= 0:
            if backup_name is not None and backup_inode is not None:
                with suppress(OSError):
                    _unlink_owned_entry(parent_descriptor, backup_name, backup_inode)
                    cleanup_changed = True
            if original_name is not None:
                owned_original = original_inode
                if owned_original is None and commit_method == "noreplace":
                    owned_original = source_inode
                if owned_original is not None:
                    with suppress(OSError):
                        _unlink_owned_entry(parent_descriptor, original_name, owned_original)
                        cleanup_changed = True
            if staging_name is not None and staging_inode is not None:
                with suppress(OSError):
                    _unlink_owned_entry(parent_descriptor, staging_name, staging_inode)
                    cleanup_changed = True
            if journal_name is not None and journal_inode is not None:
                with suppress(OSError):
                    _unlink_owned_entry(parent_descriptor, journal_name, journal_inode)
                    cleanup_changed = True
            if cleanup_changed:
                with suppress(OSError):
                    _fsync_directory_descriptor(parent_descriptor)
        if staging_descriptor >= 0:
            os.close(staging_descriptor)
        if source_descriptor >= 0:
            os.close(source_descriptor)
        if parent_descriptor >= 0:
            os.close(parent_descriptor)


__all__ = (
    "embed_artwork",
    "find_incomplete_transactions",
    "preflight_artwork",
    "recover_incomplete_transactions",
    "recover_transaction_journals",
)
