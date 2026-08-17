"""Descriptor-safe filesystem primitives used across the application."""

from __future__ import annotations

import os
import stat
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import BinaryIO, cast

from .models import EmbedError


def _open_secure_directory(
    path: Path,
    *,
    create: bool,
    private: bool,
    require_owner: bool = True,
) -> int:
    """Open a directory component-by-component without following symlinks."""
    absolute = Path(os.path.abspath(os.fspath(path.expanduser())))
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    directory_flags |= getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(absolute.anchor, directory_flags)
    try:
        for part in absolute.parts[1:]:
            try:
                child = os.open(part, directory_flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                with suppress(FileExistsError):
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                child = os.open(part, directory_flags, dir_fd=descriptor)
            except OSError as exc:
                raise OSError(
                    exc.errno,
                    f"directory path contains a symlink or unsafe component: {absolute}",
                ) from exc
            os.close(descriptor)
            descriptor = child
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            raise OSError(f"path is not a real directory: {absolute}")
        if require_owner and hasattr(os, "geteuid") and info.st_uid != os.geteuid():
            raise OSError(f"directory is not owned by the current user: {absolute}")
        if private and info.st_mode & 0o077:
            os.fchmod(descriptor, stat.S_IMODE(info.st_mode) & ~0o077)
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _open_regular_source(
    path: Path,
    expected: tuple[int, int, int, int, int] | None = None,
) -> tuple[int, int, os.stat_result]:
    """Open a regular source through a no-follow parent walk and bind it to an inode."""
    absolute = Path(os.path.abspath(os.fspath(path.expanduser())))
    parent = _open_secure_directory(
        absolute.parent,
        create=False,
        private=False,
        require_owner=False,
    )
    descriptor = -1
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        noatime = getattr(os, "O_NOATIME", 0)
        try:
            descriptor = os.open(absolute.name, flags | noatime, dir_fd=parent)
        except PermissionError:
            descriptor = os.open(absolute.name, flags, dir_fd=parent)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise EmbedError(f"audio source is not a single-link regular file: {path}")
        if expected is not None and _stat_identity(info) != expected:
            raise EmbedError(f"audio source changed after metadata scanning: {path}")
        return parent, descriptor, info
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent)
        raise


@contextmanager
def _binary_source(source: Path | int, mode: str = "rb") -> Iterator[BinaryIO]:
    handle = os.fdopen(os.dup(source), mode) if isinstance(source, int) else source.open(mode)
    handle.seek(0)
    try:
        yield cast(BinaryIO, handle)
    finally:
        handle.close()


def _source_stat(source: Path | int) -> os.stat_result:
    return os.fstat(source) if isinstance(source, int) else source.stat()


def _secure_cache_directory(path: Path) -> None:
    descriptor = _open_secure_directory(path, create=True, private=True)
    os.close(descriptor)


def _read_secure_file(path: Path, maximum: int) -> bytes:
    directory = _open_secure_directory(path.parent, create=False, private=True)
    descriptor = -1
    try:
        try:
            info = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        except OSError as exc:
            raise OSError(f"cannot inspect secure cache entry {path}: {exc}") from exc
        if not stat.S_ISREG(info.st_mode):
            raise OSError(f"cache entry is not a regular file: {path}")
        if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
            raise OSError(f"cache entry is not owned by the current user: {path}")
        if info.st_mode & 0o022:
            raise OSError(f"cache entry is writable by another user: {path}")
        if info.st_size < 0 or info.st_size > maximum:
            raise OSError(f"cache entry exceeds the {maximum}-byte limit: {path}")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path.name, flags, dir_fd=directory)
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise OSError(f"cache entry changed while opening: {path}")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65_536, maximum + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > maximum:
                raise OSError(f"cache entry exceeds the {maximum}-byte limit: {path}")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(directory)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_bytes(
    path: Path,
    data: bytes,
    *,
    overwrite: bool = True,
    private_directory: bool = True,
    file_mode: int = 0o600,
    expected_identity: tuple[int, int, int, int, int] | None = None,
) -> None:
    directory = _open_secure_directory(
        path.parent,
        create=True,
        private=private_directory,
    )
    temporary_name = f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor = -1
    temporary_exists = False
    try:
        try:
            existing = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if not stat.S_ISREG(existing.st_mode):
                raise OSError(f"destination is not a regular file: {path}")
            if hasattr(os, "geteuid") and existing.st_uid != os.geteuid():
                raise OSError(f"destination is not owned by the current user: {path}")
            if not overwrite:
                raise FileExistsError(path)
        if expected_identity is not None:
            if not overwrite:
                raise ValueError("an expected destination identity requires overwrite mode")
            if existing is None or _stat_identity(existing) != expected_identity:
                raise OSError(f"destination changed after preflight: {path}")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary_name, flags, file_mode, dir_fd=directory)
        temporary_exists = True
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if overwrite:
            if expected_identity is not None:
                try:
                    current = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
                except OSError as exc:
                    raise OSError(f"destination changed after preflight: {path}") from exc
                if _stat_identity(current) != expected_identity:
                    raise OSError(f"destination changed after preflight: {path}")
            os.replace(
                temporary_name,
                path.name,
                src_dir_fd=directory,
                dst_dir_fd=directory,
            )
            temporary_exists = False
        else:
            try:
                os.link(
                    temporary_name,
                    path.name,
                    src_dir_fd=directory,
                    dst_dir_fd=directory,
                    follow_symlinks=False,
                )
            except FileExistsError as exc:
                raise FileExistsError(
                    f"destination appeared concurrently; refusing to overwrite: {path}"
                ) from exc
            os.unlink(temporary_name, dir_fd=directory)
            temporary_exists = False
        os.fsync(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_exists:
            with suppress(FileNotFoundError):
                os.unlink(temporary_name, dir_fd=directory)
        os.close(directory)


def _stat_identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
