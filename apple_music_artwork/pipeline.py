"""Library scan, catalog match, preflight/apply orchestration, and reporting."""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Callable, Iterable
from pathlib import Path

from .artwork import ArtworkDownloader
from .catalog import AppleCatalogClient
from .embedding import (
    _verify_group_sources,
    embed_artwork,
    preflight_artwork,
    recover_transaction_journals,
)
from .filesystem import _open_secure_directory
from .matching import choose_match
from .metadata import (
    _terminal_safe,
    discover_audio_files,
    group_tracks,
    read_track_metadata,
)
from .models import (
    EmbedCommittedError,
    EmbedCommittedInterrupt,
    EmbedResult,
    TrackMetadata,
)
from .reports import (
    _candidate_score_report,
    _path_matches,
    _prepare_report_destination,
    _write_json_report,
)


class _ReportCommittedError(RuntimeError):
    """A report checkpoint failed after at least one file commit."""

    committed = True


def process_library(
    root: Path,
    *,
    apply: bool = False,
    replace_existing: bool = False,
    country: str = "US",
    cache_dir: Path | None = None,
    report_path: Path | None = None,
    overwrite_report: bool = False,
    include: Iterable[str] = (),
    exclude: Iterable[str] = (),
    apply_dcc: bool = False,
    allow_short_releases: bool = False,
    max_dimension: int | None = None,
    refresh_artwork: bool = False,
    verbose: bool = False,
    client: object | None = None,
    downloader: object | None = None,
    emit: Callable[[str], None] = print,
) -> dict[str, object]:
    """Scan, match, report, and optionally atomically embed a library root."""

    def say(message: object) -> None:
        emit(_terminal_safe(message))

    def detail(message: object) -> None:
        if verbose:
            say(f"VERBOSE {message}")

    supplied_root = root.expanduser()
    try:
        supplied_info = supplied_root.lstat()
    except OSError as exc:
        raise ValueError(f"library root cannot be inspected: {supplied_root}: {exc}") from exc
    if supplied_root.is_symlink():
        raise ValueError(f"library root must not be a symlink: {supplied_root}")
    if not stat.S_ISDIR(supplied_info.st_mode):
        raise ValueError(f"library root is not a directory: {supplied_root}")
    try:
        root_descriptor = _open_secure_directory(
            supplied_root,
            create=False,
            private=False,
            require_owner=False,
        )
    except OSError as exc:
        raise ValueError(f"library root contains a symlink or unsafe component: {exc}") from exc
    try:
        opened_root = os.fstat(root_descriptor)
        if (opened_root.st_dev, opened_root.st_ino) != (
            supplied_info.st_dev,
            supplied_info.st_ino,
        ):
            raise ValueError("library root changed while it was being opened")
    finally:
        os.close(root_descriptor)
    root = supplied_root.resolve()
    if root == Path(root.anchor):
        raise ValueError(f"refusing to scan a filesystem root: {root}")
    country = country.upper()
    if not re.fullmatch(r"[A-Z]{2}", country):
        raise ValueError("country must be a two-letter storefront code")
    cache_dir = (cache_dir or root / ".apple-artwork-cache").expanduser()
    include_patterns = tuple(include)
    exclude_patterns = tuple(exclude)
    detail(
        f"SCAN root={root} mode={'apply' if apply else 'dry-run'} country={country} "
        f"apply_dcc={str(apply_dcc).lower()}"
    )

    discovered = discover_audio_files(root)
    transaction_journals = tuple(getattr(discovered, "transaction_journals", ()))
    if transaction_journals:
        if not apply:
            raise ValueError(
                f"found {len(transaction_journals)} incomplete artwork transaction(s); "
                "rerun with --apply to recover them before a dry run"
            )
        recovered_paths = recover_transaction_journals(transaction_journals)
        for recovered_path in recovered_paths:
            say(f"RECOVER {recovered_path}")
        discovered = discover_audio_files(root)
        remaining_journals = tuple(getattr(discovered, "transaction_journals", ()))
        if remaining_journals:
            raise ValueError(
                "transaction recovery did not clear every journal; refusing to continue"
            )
    selected: list[Path] = []
    dcc_omitted_files = 0
    for path in discovered:
        relative_path = path.relative_to(root)
        protected_prefix = next(
            (
                prefix.upper()
                for part in relative_path.parts[:-1]
                for prefix in ("00", "dcc", "gzs")
                if part.casefold().startswith(prefix)
            ),
            None,
        )
        if not apply_dcc and protected_prefix is not None:
            dcc_omitted_files += 1
            detail(f"OMIT-{protected_prefix} {relative_path.as_posix()}")
            continue
        relative = relative_path.as_posix()
        if include_patterns and not any(
            _path_matches(relative, pattern) for pattern in include_patterns
        ):
            detail(f"OMIT-INCLUDE {relative}")
            continue
        if any(_path_matches(relative, pattern) for pattern in exclude_patterns):
            detail(f"OMIT-EXCLUDE {relative}")
            continue
        selected.append(path)
    selected.sort(key=str)
    detail(
        f"DISCOVERY discovered={len(discovered)} selected={len(selected)} "
        f"dcc_omitted={dcc_omitted_files}"
    )

    report_destination = (
        _prepare_report_destination(
            root,
            report_path,
            selected,
            overwrite=overwrite_report,
        )
        if report_path is not None
        else None
    )
    if report_destination is not None:
        _write_json_report(
            report_destination,
            {
                "schema_version": 2,
                "status": "in_progress",
                "mode": "apply" if apply else "dry-run",
                "root": str(root),
                "country": country,
                "summary": {},
                "albums": [],
                "errors": [],
            },
            overwrite=overwrite_report,
        )

    errors: list[dict[str, str]] = []
    adapter_errors: dict[Path, str] = {}
    adapter_plans: dict[Path, EmbedResult] = {}
    tracks: list[TrackMetadata] = []
    for path in selected:
        try:
            track = read_track_metadata(path)
        except Exception as exc:
            track = None
            error_text = f"metadata parser failed: {exc}"
        else:
            error_text = "unreadable audio file or unsupported metadata container"
        if track is None:
            errors.append(
                {
                    "stage": "metadata",
                    "path": str(path),
                    "error": error_text,
                }
            )
            say(f"ERROR   {path}: {error_text}")
            continue
        if not track.title or not track.album or not (track.album_artist or track.artist):
            error_text = "missing required title, album, or artist tags"
            errors.append(
                {
                    "stage": "metadata",
                    "path": str(path),
                    "error": error_text,
                }
            )
            say(f"ERROR   {path}: {error_text}")
            continue
        try:
            adapter_plans[path] = preflight_artwork(
                path,
                replace_existing=False,
                expected_identity=track.source_identity,
            )
        except Exception as exc:
            adapter_errors[path] = str(exc)
            errors.append(
                {
                    "stage": "adapter_preflight",
                    "path": str(path),
                    "error": str(exc),
                }
            )
            say(f"ERROR   {path}: adapter preflight failed: {exc}")
        else:
            plan = adapter_plans[path]
            detail(
                f"LOCAL-PREFLIGHT {path.relative_to(root).as_posix()} "
                f"status={plan.status} format={plan.format}"
            )
        tracks.append(track)

    groups = group_tracks(tracks)
    detail(f"GROUPS albums={len(groups)} metadata_tracks={len(tracks)}")
    catalog = client or AppleCatalogClient(country=country, cache_dir=cache_dir)
    artwork_client = downloader or ArtworkDownloader(cache_dir=cache_dir)
    summary: dict[str, int] = {
        "discovered_files": len(discovered),
        "selected_files": len(selected),
        "dcc_omitted_files": dcc_omitted_files,
        "metadata_tracks": len(tracks),
        "albums": len(groups),
        "matched": 0,
        "ambiguous": 0,
        "low_confidence": 0,
        "no_match": 0,
        "metadata_failures": sum(error["stage"] == "metadata" for error in errors),
        "adapter_preflight_failures": len(adapter_errors),
        "failed": len(errors),
        "files_embedded": 0,
        "files_skipped": 0,
        "files_unchanged": 0,
        "file_failures": len(adapter_errors),
    }
    album_reports: list[dict[str, object]] = []

    def checkpoint_report(
        active_album: dict[str, object] | None = None,
        *,
        committed_path: Path | None = None,
        summary_override: dict[str, int] | None = None,
    ) -> None:
        if report_destination is None:
            return
        checkpoint_albums = list(album_reports)
        if active_album is not None:
            checkpoint_albums.append(active_album)
        checkpoint_summary = summary if summary_override is None else summary_override
        try:
            _write_json_report(
                report_destination,
                {
                    "schema_version": 2,
                    "status": "in_progress",
                    "mode": "apply" if apply else "dry-run",
                    "root": str(root),
                    "country": country,
                    "summary": checkpoint_summary,
                    "albums": checkpoint_albums,
                    "errors": errors,
                },
                overwrite=True,
            )
        except Exception as exc:
            if committed_path is not None or checkpoint_summary["files_embedded"]:
                committed_context = (
                    f"artwork was committed to {committed_path}"
                    if committed_path is not None
                    else f"{checkpoint_summary['files_embedded']} file(s) were already committed"
                )
                raise _ReportCommittedError(
                    f"{committed_context}, but the report checkpoint failed: {exc}"
                ) from exc
            raise

    def committed_checkpoint_callback(
        committed_path: Path,
        current_file_results: list[dict[str, str]],
        current_album: dict[str, object],
    ) -> Callable[[EmbedResult], None]:
        def persist_committed_result(committed_result: EmbedResult) -> None:
            committed_file_results = [
                *current_file_results,
                {
                    "path": str(committed_path),
                    "status": committed_result.status,
                    "format": committed_result.format,
                    "message": committed_result.message,
                },
            ]
            committed_album = {
                **current_album,
                "status": "in_progress",
                "file_results": committed_file_results,
            }
            committed_summary = {
                **summary,
                "files_embedded": summary["files_embedded"] + 1,
            }
            checkpoint_report(
                committed_album,
                committed_path=committed_path,
                summary_override=committed_summary,
            )

        return persist_committed_result

    for group in groups:
        label = f"{group.album_artist} — {group.album}"
        detail(f"ALBUM {label} logical_tracks={len(group.logical_tracks)} files={len(group.files)}")
        base_report: dict[str, object] = {
            "artist": group.album_artist,
            "album": group.album,
            "year": group.year,
            "files": [str(path) for path in group.files],
            "logical_track_count": len(group.logical_tracks),
            "barcode": group.barcode,
            "musicbrainz_release_id": group.musicbrainz_release_id,
        }
        blocked_files = [path for path in group.files if path in adapter_errors]
        if blocked_files:
            base_report.update(
                {
                    "status": "preflight_failed",
                    "reason": (
                        f"{len(blocked_files)} file(s) failed local adapter preflight; "
                        "no catalog request was sent"
                    ),
                    "file_results": [
                        {
                            "path": str(path),
                            "status": "failed",
                            "error": adapter_errors[path],
                        }
                        for path in blocked_files
                    ],
                }
            )
            album_reports.append(base_report)
            checkpoint_report()
            say(
                f"ERROR   {label}: {len(blocked_files)} file(s) failed local adapter "
                "preflight; Apple was not contacted"
            )
            continue
        expected_by_path = dict(group.source_identities)
        try:
            _verify_group_sources(group)
            candidates = catalog.find_candidates(group)  # type: ignore[attr-defined]
            _verify_group_sources(group)
            decision = choose_match(
                group,
                candidates,
                allow_short_releases=allow_short_releases,
            )
        except Exception as exc:
            summary["failed"] += 1
            base_report.update(
                {
                    "status": "failed",
                    "reason": f"Apple catalog lookup failed: {exc}",
                }
            )
            album_reports.append(base_report)
            checkpoint_report()
            say(f"ERROR   {label}: Apple lookup failed: {exc}")
            continue

        for score in decision.scores:
            reasons = "; ".join(score.reasons) or "none"
            detail(
                f"CANDIDATE {label} collection_id={score.candidate.collection_id} "
                f"eligible={str(score.eligible).lower()} score={score.total:.3f} "
                f"reasons={reasons}"
            )

        base_report["reason"] = decision.reason
        base_report["candidates"] = [_candidate_score_report(score) for score in decision.scores]
        if decision.status != "matched" or decision.match is None:
            summary[decision.status] = summary.get(decision.status, 0) + 1
            base_report["status"] = decision.status
            album_reports.append(base_report)
            checkpoint_report()
            say(f"{decision.status.upper():8} {label}: {decision.reason}")
            continue

        summary["matched"] += 1
        matched = decision.match
        candidate = matched.candidate
        base_report["apple"] = {
            "collection_id": candidate.collection_id,
            "artist": candidate.artist,
            "album": candidate.album,
            "release_year": candidate.release_year,
            "track_count": candidate.track_count,
            "artwork_url": candidate.artwork_url,
            "score": round(matched.total, 6),
        }
        if not apply:
            file_results: list[dict[str, str]] = []
            preflight_failures = 0
            for path in group.files:
                try:
                    plan = adapter_plans[path]
                    file_results.append(
                        {
                            "path": str(path),
                            "status": plan.status,
                            "format": plan.format,
                            "message": plan.message,
                        }
                    )
                    detail(
                        f"PREFLIGHT {path.relative_to(root).as_posix()} "
                        f"status={plan.status} format={plan.format}"
                    )
                except Exception as exc:
                    preflight_failures += 1
                    summary["file_failures"] += 1
                    file_results.append({"path": str(path), "status": "failed", "error": str(exc)})
            base_report["file_results"] = file_results
            if preflight_failures:
                summary["failed"] += 1
                base_report["status"] = "preflight_failed"
                base_report["reason"] = (
                    f"{preflight_failures} file(s) failed non-mutating adapter preflight"
                )
                say(f"ERROR   {label}: {preflight_failures} file(s) failed dry-run preflight")
            else:
                base_report["status"] = "dry-run"
                say(
                    f"DRY-RUN {label} -> Apple {candidate.collection_id} "
                    f"({matched.total:.3f}); {len(file_results)} file(s) preflighted"
                )
            album_reports.append(base_report)
            checkpoint_report()
            continue

        try:
            _verify_group_sources(group)
            artwork = artwork_client.fetch(  # type: ignore[attr-defined]
                candidate.collection_id,
                candidate.artwork_url,
                max_dimension=max_dimension,
                refresh=refresh_artwork,
            )
            _verify_group_sources(group)
        except Exception as exc:
            summary["failed"] += 1
            base_report.update({"status": "failed", "reason": f"artwork download failed: {exc}"})
            album_reports.append(base_report)
            checkpoint_report()
            say(f"ERROR   {label}: artwork download failed: {exc}")
            continue

        base_report["artwork"] = {
            "source_url": artwork.source_url,
            "mime": artwork.mime,
            "width": artwork.width,
            "height": artwork.height,
            "depth": artwork.depth,
            "sha256": artwork.sha256,
        }
        detail(
            f"ARTWORK collection_id={candidate.collection_id} mime={artwork.mime} "
            f"dimensions={artwork.width}x{artwork.height} sha256={artwork.sha256}"
        )
        planned: list[tuple[Path, EmbedResult]] = []
        file_results: list[dict[str, str]] = []
        preflight_failures = 0
        for path in group.files:
            try:
                plan = preflight_artwork(
                    path,
                    artwork,
                    replace_existing=replace_existing,
                    expected_identity=expected_by_path.get(path),
                )
                planned.append((path, plan))
                file_results.append(
                    {
                        "path": str(path),
                        "status": plan.status,
                        "format": plan.format,
                        "message": plan.message,
                    }
                )
                detail(
                    f"PREFLIGHT {path.relative_to(root).as_posix()} "
                    f"status={plan.status} format={plan.format}"
                )
            except Exception as exc:
                preflight_failures += 1
                summary["file_failures"] += 1
                file_results.append({"path": str(path), "status": "failed", "error": str(exc)})
        if preflight_failures:
            summary["failed"] += 1
            base_report["status"] = "preflight_failed"
            base_report["reason"] = (
                f"{preflight_failures} file(s) failed; album-wide preflight prevented all writes"
            )
            base_report["file_results"] = file_results
            album_reports.append(base_report)
            checkpoint_report()
            say(
                f"ERROR   {label}: {preflight_failures} file(s) failed preflight; "
                "no album files were changed"
            )
            continue

        file_results = []
        album_failures = 0
        album_embedded = 0
        for path, plan in planned:
            if plan.status != "ready":
                file_results.append(
                    {
                        "path": str(path),
                        "status": plan.status,
                        "format": plan.format,
                        "message": plan.message,
                    }
                )
                detail(
                    f"RESULT {path.relative_to(root).as_posix()} "
                    f"status={plan.status} format={plan.format}"
                )
                if plan.status == "unchanged":
                    summary["files_unchanged"] += 1
                else:
                    summary["files_skipped"] += 1
                base_report["status"] = "in_progress"
                base_report["file_results"] = file_results
                checkpoint_report(base_report)
                continue
            try:
                persist_committed_result = committed_checkpoint_callback(
                    path,
                    file_results,
                    base_report,
                )
                result = embed_artwork(
                    path,
                    artwork,
                    replace_existing=replace_existing,
                    expected_identity=expected_by_path.get(path),
                    _on_committed=persist_committed_result,
                )
                file_results.append(
                    {
                        "path": str(path),
                        "status": result.status,
                        "format": result.format,
                        "message": result.message,
                    }
                )
                detail(
                    f"RESULT {path.relative_to(root).as_posix()} "
                    f"status={result.status} format={result.format}"
                )
                if result.status == "embedded":
                    album_embedded += 1
                    summary["files_embedded"] += 1
                elif result.status == "unchanged":
                    summary["files_unchanged"] += 1
                else:
                    summary["files_skipped"] += 1
            except _ReportCommittedError:
                raise
            except EmbedCommittedInterrupt as exc:
                album_failures += 1
                album_embedded += 1
                summary["file_failures"] += 1
                summary["files_embedded"] += 1
                file_results.append(
                    {
                        "path": str(path),
                        "status": "committed_interrupted",
                        "format": exc.result.format,
                        "error": str(exc),
                    }
                )
                base_report["file_results"] = file_results
                base_report["status"] = "interrupted_committed"
                base_report["reason"] = str(exc)
                summary["failed"] += 1
                album_reports.append(base_report)
                interrupted_report: dict[str, object] = {
                    "schema_version": 2,
                    "status": "interrupted_committed",
                    "mode": "apply",
                    "root": str(root),
                    "country": country,
                    "summary": summary,
                    "albums": album_reports,
                    "errors": errors,
                }
                if report_destination is not None:
                    try:
                        _write_json_report(report_destination, interrupted_report, overwrite=True)
                    except (Exception, KeyboardInterrupt) as report_error:
                        say(
                            "ERROR   committed-interrupt report write failed: "
                            f"{_terminal_safe(report_error)}"
                        )
                    else:
                        exc.report_persisted = True
                say(f"INTERRUPTED-COMMITTED {path}: {exc}")
                raise
            except EmbedCommittedError as exc:
                album_failures += 1
                album_embedded += 1
                summary["file_failures"] += 1
                summary["files_embedded"] += 1
                file_results.append(
                    {
                        "path": str(path),
                        "status": "committed_unverified",
                        "error": str(exc),
                    }
                )
            except Exception as exc:
                album_failures += 1
                summary["file_failures"] += 1
                file_results.append({"path": str(path), "status": "failed", "error": str(exc)})
            base_report["status"] = "in_progress"
            base_report["file_results"] = file_results
            checkpoint_report(
                base_report,
                committed_path=(
                    path
                    if file_results[-1]["status"] in {"embedded", "committed_unverified"}
                    else None
                ),
            )
        base_report["file_results"] = file_results
        if album_failures:
            summary["failed"] += 1
            base_report["status"] = "partial_failure" if album_embedded else "failed"
            say(
                f"ERROR   {label}: embedded {album_embedded}/{len(group.files)} files; "
                f"{album_failures} failed after preflight"
            )
        elif album_embedded:
            base_report["status"] = "applied"
            say(f"APPLIED {label}: embedded {album_embedded} file(s)")
        else:
            base_report["status"] = "unchanged"
            say(f"SKIPPED {label}: no file required a change")
        album_reports.append(base_report)
        checkpoint_report()

    report: dict[str, object] = {
        "schema_version": 2,
        "status": "complete",
        "mode": "apply" if apply else "dry-run",
        "root": str(root),
        "country": country,
        "summary": summary,
        "albums": album_reports,
        "errors": errors,
    }
    if report_destination is not None:
        try:
            _write_json_report(report_destination, report, overwrite=True)
        except Exception as exc:
            if summary["files_embedded"]:
                raise _ReportCommittedError(
                    f"{summary['files_embedded']} file(s) were committed, but final report "
                    f"finalization failed: {exc}"
                ) from exc
            raise
        say(f"REPORT  {report_destination}")
    return report


__all__ = ("process_library",)
