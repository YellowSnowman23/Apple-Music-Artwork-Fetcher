"""Plan and atomically write native-format album-folder artwork."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .constants import MAX_ARTWORK_BYTES
from .filesystem import _atomic_write_bytes, _open_secure_directory
from .matching import normalize_text, text_similarity
from .models import AlbumGroup, Artwork

_COVER_NAMES = frozenset({"cover.jpg", "cover.jpeg", "cover.png"})
_CONTAINER_DIRECTORY = re.compile(
    r"^(?:cd|disc|disk|part|volume|vol)[\s_.-]*\d+$|^(?:flac|mp3|m4a|aac|ogg|opus|wav|wave|aif|aiff|wv|lossless|hi[\s_.-]*res)$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class FolderArtworkPlan:
    """A non-mutating decision about one album-folder cover."""

    directory: Path
    target: Path | None
    existing: Path | None
    status: str
    message: str
    expected_existing_identity: tuple[int, int, int, int, int] | None = None
    expected_existing_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class FolderArtworkResult:
    path: Path
    status: str
    message: str


class FolderArtworkCommittedError(OSError):
    """A new native cover was written but an old extension could not be removed."""

    committed = True

    def __init__(self, path: Path, message: str) -> None:
        super().__init__(message)
        self.path = path


def album_artwork_directory(
    group: AlbumGroup,
    scan_root: Path,
    all_groups: Sequence[AlbumGroup],
    protected_paths: Sequence[Path] = (),
) -> Path:
    """Return a unique album root without ever selecting the scan root itself."""
    root = Path(os.path.abspath(os.fspath(scan_root)))
    parents = [Path(os.path.abspath(os.fspath(path.parent))) for path in group.files]
    if not parents:
        raise ValueError("album has no files from which to derive an artwork directory")
    album_identity = normalize_text(group.album)
    anchors = [_release_directory_anchor(parent, root, album_identity) for parent in parents]
    try:
        directory = Path(os.path.commonpath(anchors))
    except ValueError as exc:
        raise ValueError("album files do not share one filesystem root") from exc
    if directory == root or not directory.is_relative_to(root):
        raise ValueError("album artwork directory is not a unique child of the scan root")
    if len(set(anchors)) > 1 and normalize_text(directory.name) != album_identity:
        raise ValueError("album files span sibling directories without one identifiable album root")

    descriptor = _open_secure_directory(
        directory,
        create=False,
        private=False,
        require_owner=False,
    )
    os.close(descriptor)

    group_files = frozenset(group.files)
    for other in all_groups:
        if other is group:
            continue
        if any(
            path not in group_files
            and Path(os.path.abspath(os.fspath(path))).is_relative_to(directory)
            for path in other.files
        ):
            raise ValueError("album artwork directory also contains a different grouped release")
    if any(
        Path(os.path.abspath(os.fspath(path))).is_relative_to(directory) for path in protected_paths
    ):
        raise ValueError("album artwork directory contains an omitted protected audio path")
    return directory


def _release_directory_anchor(parent: Path, root: Path, album_identity: str) -> Path:
    """Prefer an album-named ancestor over a selected disc/format subdirectory."""
    for candidate in (parent, *parent.parents):
        if candidate == root.parent:
            break
        if candidate == root:
            break
        if album_identity and normalize_text(candidate.name) == album_identity:
            return candidate
    if _CONTAINER_DIRECTORY.fullmatch(parent.name):
        candidate = parent.parent
        while candidate != root and _CONTAINER_DIRECTORY.fullmatch(candidate.name):
            candidate = candidate.parent
        if candidate == root:
            raise ValueError(
                "conventional disc/format directory has no distinct album root below the scan root"
            )
        candidate_identity = normalize_text(candidate.name)
        if (
            candidate_identity == album_identity
            or text_similarity(candidate.name, album_identity) >= 0.55
        ):
            return candidate
        raise ValueError(
            "conventional disc/format directory parent does not identify the tagged album"
        )
    return parent


def plan_folder_artwork(
    group: AlbumGroup,
    scan_root: Path,
    all_groups: Sequence[AlbumGroup],
    artwork: Artwork | None,
    *,
    replace_existing: bool,
    protected_paths: Sequence[Path] = (),
) -> FolderArtworkPlan:
    """Inspect the destination and decide whether a folder cover needs writing."""
    directory = album_artwork_directory(
        group,
        scan_root,
        all_groups,
        protected_paths,
    )
    existing = _existing_cover(directory)
    if artwork is None:
        if existing is None:
            message = "native cover.jpg or cover.png will be selected after artwork download"
        elif replace_existing:
            message = f"existing {existing.name} will be compared and may be replaced"
        else:
            message = f"existing {existing.name} will be compared and preserved if different"
        return FolderArtworkPlan(
            directory,
            None,
            existing,
            "planned",
            message,
        )

    extension = ".jpg" if artwork.mime == "image/jpeg" else ".png"
    if artwork.mime not in {"image/jpeg", "image/png"}:
        raise ValueError(f"unsupported folder-artwork MIME type: {artwork.mime}")
    target = directory / f"cover{extension}"
    if existing is None:
        return FolderArtworkPlan(
            directory,
            target,
            None,
            "ready",
            f"write native {artwork.mime} source artwork",
        )

    identity, digest = _cover_identity_and_digest(existing)
    if digest == artwork.sha256 and existing.name == target.name:
        return FolderArtworkPlan(
            directory,
            target,
            existing,
            "unchanged",
            "native folder artwork already matches the selected source",
            identity,
            digest,
        )
    if not replace_existing:
        return FolderArtworkPlan(
            directory,
            target,
            existing,
            "skipped",
            f"existing {existing.name} differs; use --replace-existing to replace it",
            identity,
            digest,
        )
    return FolderArtworkPlan(
        directory,
        target,
        existing,
        "ready",
        f"replace {existing.name} with native {target.name}",
        identity,
        digest,
    )


def write_folder_artwork(plan: FolderArtworkPlan, artwork: Artwork) -> FolderArtworkResult:
    """Commit one planned cover while detecting ordinary intervening edits."""
    if plan.status != "ready" or plan.target is None:
        path = plan.target or plan.directory
        return FolderArtworkResult(path, plan.status, plan.message)

    existing = plan.existing
    if existing is not None:
        current_identity, current_digest = _cover_identity_and_digest(existing)
        if (
            current_identity != plan.expected_existing_identity
            or current_digest != plan.expected_existing_sha256
        ):
            raise OSError(f"folder artwork changed after preflight: {existing}")

    same_destination = existing is not None and existing == plan.target
    try:
        _atomic_write_bytes(
            plan.target,
            artwork.data,
            overwrite=same_destination,
            private_directory=False,
            file_mode=0o644,
            expected_identity=(plan.expected_existing_identity if same_destination else None),
        )
    except Exception as exc:
        if not isinstance(exc, FileExistsError) and _cover_matches_hash(
            plan.target,
            artwork.sha256,
        ):
            raise FolderArtworkCommittedError(
                plan.target,
                f"wrote {plan.target}, but durability verification failed: {exc}",
            ) from exc
        raise
    if existing is not None and existing != plan.target:
        try:
            current_identity, current_digest = _cover_identity_and_digest(existing)
            if (
                current_identity != plan.expected_existing_identity
                or current_digest != plan.expected_existing_sha256
            ):
                raise OSError(
                    f"old folder artwork changed after the new cover was written: {existing}"
                )
            existing.unlink()
            _fsync_open_directory(plan.directory)
        except Exception as exc:
            raise FolderArtworkCommittedError(
                plan.target,
                f"wrote {plan.target}, but could not safely remove old {existing.name}: {exc}",
            ) from exc
    return FolderArtworkResult(plan.target, "written", plan.message)


def _existing_cover(directory: Path) -> Path | None:
    matches: list[Path] = []
    for child in directory.iterdir():
        if child.name.casefold() not in _COVER_NAMES:
            continue
        info = child.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise OSError(f"folder artwork destination is not a regular file: {child}")
        matches.append(child)
    if len(matches) > 1:
        names = ", ".join(sorted(path.name for path in matches))
        raise OSError(f"multiple existing folder covers require manual cleanup: {names}")
    return matches[0] if matches else None


def _cover_identity_and_digest(path: Path) -> tuple[tuple[int, int, int, int, int], str]:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise OSError(f"folder artwork destination is not a regular file: {path}")
    if info.st_size < 0 or info.st_size > MAX_ARTWORK_BYTES:
        raise OSError(f"existing folder artwork exceeds the safety limit: {path}")
    data = path.read_bytes()
    after = path.lstat()
    identity = _stat_identity(info)
    if _stat_identity(after) != identity:
        raise OSError(f"folder artwork changed while it was being inspected: {path}")
    return identity, hashlib.sha256(data).hexdigest()


def _cover_matches_hash(path: Path, expected_sha256: str) -> bool:
    try:
        _identity, digest = _cover_identity_and_digest(path)
    except OSError:
        return False
    return digest == expected_sha256


def _stat_identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _fsync_open_directory(path: Path) -> None:
    descriptor = _open_secure_directory(
        path,
        create=False,
        private=False,
        require_owner=False,
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = (
    "FolderArtworkCommittedError",
    "FolderArtworkPlan",
    "FolderArtworkResult",
    "album_artwork_directory",
    "plan_folder_artwork",
    "write_folder_artwork",
)
