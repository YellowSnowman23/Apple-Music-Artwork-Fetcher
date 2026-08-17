"""Command-line parser and entry point."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .constants import VERSION
from .metadata import _terminal_safe
from .models import EmbedCommittedInterrupt
from .pipeline import process_library


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="apple-artwork",
        description=(
            "Identifier-first Apple artwork matching. Dry-run is the default; "
            "audio files change only with --apply."
        ),
    )
    parser.add_argument(
        "root",
        nargs="?",
        type=Path,
        default=Path.cwd(),
        help="music-library root (default: current directory)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "atomically embed verified artwork and save native cover.jpg/cover.png "
            "(without this, only report matches)"
        ),
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="show discovery, candidate-score, and per-file progress details",
    )
    parser.add_argument(
        "--replace-existing",
        action="store_true",
        help=(
            "replace existing embedded front covers and differing album-folder covers; "
            "for supported M4A this replaces every covr item because M4A has no front/back role"
        ),
    )
    parser.add_argument(
        "--country",
        default="US",
        metavar="CC",
        help="two-letter Apple storefront code (default: US)",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        help="cache directory (default: ROOT/.apple-artwork-cache)",
    )
    parser.add_argument(
        "--report",
        dest="report_path",
        type=Path,
        default=Path("apple-artwork-report.json"),
        help="JSON report path relative to ROOT (default: apple-artwork-report.json)",
    )
    parser.add_argument(
        "--no-report",
        action="store_true",
        help="do not write a JSON report",
    )
    parser.add_argument(
        "--overwrite-report",
        action="store_true",
        help="explicitly replace an existing regular .json report inside ROOT",
    )
    parser.add_argument(
        "--include",
        action="append",
        default=[],
        metavar="GLOB",
        help="include relative paths matching GLOB (repeatable)",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help="exclude relative paths matching GLOB (repeatable)",
    )
    parser.add_argument(
        "--apply-dcc",
        action="store_true",
        help=(
            "include protected folders whose relative names start with '00', 'DCC', or 'GZS'; "
            "does not enable --apply"
        ),
    )
    parser.add_argument(
        "--max-dimension",
        type=int,
        metavar="PX",
        help="cap requested artwork dimensions at 100-10000 pixels",
    )
    parser.add_argument(
        "--allow-short-releases",
        action="store_true",
        help="allow one/two-track matches without UPC evidence (less conservative)",
    )
    parser.add_argument(
        "--refresh-artwork",
        action="store_true",
        help="ignore cached artwork bytes and revalidate Apple CDN candidates",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.replace_existing and not args.apply:
        parser.error("--replace-existing requires --apply")
    if args.max_dimension is not None and not 100 <= args.max_dimension <= 10_000:
        parser.error("--max-dimension must be between 100 and 10000")

    try:
        report = process_library(
            args.root,
            apply=args.apply,
            replace_existing=args.replace_existing,
            country=args.country,
            cache_dir=args.cache_dir,
            report_path=None if args.no_report else args.report_path,
            overwrite_report=args.overwrite_report,
            include=args.include,
            exclude=args.exclude,
            apply_dcc=args.apply_dcc,
            allow_short_releases=args.allow_short_releases,
            max_dimension=args.max_dimension,
            refresh_artwork=args.refresh_artwork,
            verbose=args.verbose,
        )
    except EmbedCommittedInterrupt as exc:
        report_message = (
            "the report records the committed state."
            if exc.report_persisted
            else "report persistence was not confirmed; inspect the affected file before resuming."
        )
        print(
            f"Interrupted after artwork was committed to {_terminal_safe(exc.path)}; "
            f"{report_message}",
            file=sys.stderr,
        )
        return 130
    except KeyboardInterrupt:
        recovery_guidance = (
            "Because reporting was disabled, inspect affected files before resuming."
            if args.no_report
            else "Check the in-progress report before resuming."
        )
        print(
            f"Interrupted; any previously completed files remain committed. {recovery_guidance}",
            file=sys.stderr,
        )
        return 130
    except Exception as exc:
        print(f"apple-artwork: {_terminal_safe(exc)}", file=sys.stderr)
        return 2

    summary = report["summary"]
    assert isinstance(summary, dict)
    print(
        "SUMMARY "
        f"albums={summary.get('albums', 0)} "
        f"matched={summary.get('matched', 0)} "
        f"ambiguous={summary.get('ambiguous', 0)} "
        f"low_confidence={summary.get('low_confidence', 0)} "
        f"no_match={summary.get('no_match', 0)} "
        f"dcc_omitted={summary.get('dcc_omitted_files', 0)} "
        f"metadata_failures={summary.get('metadata_failures', 0)} "
        f"failed={summary.get('failed', 0)} "
        f"embedded={summary.get('files_embedded', 0)} "
        f"folder_covers={summary.get('folder_covers_written', 0)}"
    )
    has_failures = any(
        int(summary.get(field, 0))
        for field in (
            "failed",
            "metadata_failures",
            "adapter_preflight_failures",
            "file_failures",
            "folder_cover_failures",
        )
    )
    return 1 if has_failures else 0


__all__ = ("main",)
