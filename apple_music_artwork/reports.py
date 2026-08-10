"""Secure JSON reports and lexical path-selection helpers."""

from __future__ import annotations

import fnmatch
import json
import os
import stat
from collections.abc import Iterable, Mapping
from pathlib import Path

from .filesystem import _atomic_write_bytes, _open_secure_directory
from .models import CandidateScore


def _candidate_score_report(score: CandidateScore) -> dict[str, object]:
    return {
        "collection_id": score.candidate.collection_id,
        "artist": score.candidate.artist,
        "album": score.candidate.album,
        "score": round(score.total, 6),
        "eligible": score.eligible,
        "reasons": list(score.reasons),
        "components": {name: round(value, 6) for name, value in score.components.items()},
    }


def _prepare_report_destination(
    root: Path,
    report_path: Path,
    audio_paths: Iterable[Path],
    *,
    overwrite: bool,
) -> Path:
    root = Path(os.path.abspath(os.fspath(root)))
    destination = report_path.expanduser()
    if not destination.is_absolute():
        destination = root / destination
    destination = Path(os.path.abspath(os.fspath(destination)))
    if not destination.is_relative_to(root):
        raise ValueError("report destination must stay inside the selected library root")
    for audio_path in audio_paths:
        audio_lexical = Path(os.path.abspath(os.fspath(audio_path)))
        if destination == audio_lexical:
            raise ValueError("report destination collides with a selected audio file")
    if destination.suffix.casefold() != ".json":
        raise ValueError("report destination must use a .json extension")
    try:
        destination_info = destination.lstat()
    except FileNotFoundError:
        destination_info = None
    except OSError as exc:
        raise ValueError(f"report destination cannot be inspected: {destination}: {exc}") from exc
    if destination_info is not None:
        if stat.S_ISLNK(destination_info.st_mode) or not stat.S_ISREG(destination_info.st_mode):
            raise ValueError("report destination is a symlink or is not a regular file")
        if not overwrite:
            raise FileExistsError(
                f"report already exists; pass --overwrite-report to overwrite it: {destination}"
            )
    try:
        parent_descriptor = _open_secure_directory(
            destination.parent,
            create=True,
            private=False,
        )
    except OSError as exc:
        raise ValueError(f"report parent contains a symlink or unsafe directory: {exc}") from exc
    os.close(parent_descriptor)
    return destination


def _write_json_report(
    path: Path,
    report: Mapping[str, object],
    *,
    overwrite: bool = False,
) -> None:
    payload = (json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode(
        "ascii"
    )
    _atomic_write_bytes(
        path,
        payload,
        overwrite=overwrite,
        private_directory=False,
    )


def _path_matches(relative_path: str, pattern: str) -> bool:
    """Match POSIX-relative paths with separator-aware `*` and recursive `**`."""
    path_parts = tuple(part for part in relative_path.replace("\\", "/").split("/") if part)
    pattern_parts = tuple(part for part in pattern.replace("\\", "/").split("/") if part)
    if not path_parts or not pattern_parts:
        return False
    memo: dict[tuple[int, int], bool] = {}

    def match(pattern_index: int, path_index: int) -> bool:
        key = (pattern_index, path_index)
        if key in memo:
            return memo[key]
        if pattern_index == len(pattern_parts):
            result = path_index == len(path_parts)
        elif pattern_parts[pattern_index] == "**":
            result = match(pattern_index + 1, path_index) or (
                path_index < len(path_parts) and match(pattern_index, path_index + 1)
            )
        else:
            result = (
                path_index < len(path_parts)
                and fnmatch.fnmatchcase(path_parts[path_index], pattern_parts[pattern_index])
                and match(pattern_index + 1, path_index + 1)
            )
        memo[key] = result
        return result

    return match(0, 0)
