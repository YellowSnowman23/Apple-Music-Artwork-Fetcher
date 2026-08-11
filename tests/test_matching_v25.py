from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from apple_artwork import (
    AlbumGroup,
    CatalogAlbum,
    CatalogTrack,
    TrackMetadata,
    choose_match,
    score_candidate,
)


def aligned_release_case(
    *,
    local_album: str = "Signal",
    remote_album: str = "Signal (Expanded Edition)",
    local_suffix: str = "",
    remote_suffix: str = " (2024 Remaster)",
    count: int = 5,
) -> tuple[AlbumGroup, CatalogAlbum]:
    local_tracks = tuple(
        TrackMetadata(
            path=Path(f"{number:02}.flac"),
            title=f"Movement {number}{local_suffix}",
            artist="Exact Artist",
            album=local_album,
            album_artist="Exact Artist",
            year=2024,
            track_number=number,
            track_total=count,
            disc_number=1,
            disc_total=1,
            duration_ms=180_000 + number * 10_000,
        )
        for number in range(1, count + 1)
    )
    group = AlbumGroup(
        album=local_album,
        album_artist="Exact Artist",
        year=2024,
        files=tuple(track.path for track in local_tracks),
        logical_tracks=local_tracks,
    )
    candidate = CatalogAlbum(
        collection_id=2500,
        album=remote_album,
        artist="Exact Artist",
        release_year=1994,
        artwork_url="https://is1-ssl.mzstatic.com/image/thumb/Music/example.jpg/100x100bb.jpg",
        track_count=count,
        tracks=tuple(
            CatalogTrack(
                title=f"Movement {number}{remote_suffix}",
                artist="Exact Artist",
                duration_ms=180_000 + number * 10_000,
                disc_number=1,
                track_number=number,
            )
            for number in range(1, count + 1)
        ),
    )
    return group, candidate


def test_complete_aligned_release_accepts_provider_remaster_and_expanded_labels() -> None:
    group, candidate = aligned_release_case()

    score = score_candidate(group, candidate)
    decision = choose_match(group, [candidate])

    assert score.eligible is True
    assert score.components["track_coverage"] == 1.0
    assert score.components["track_title"] == 1.0
    assert decision.status == "matched"


def test_bracketed_album_name_matches_provider_remastered_album() -> None:
    group, candidate = aligned_release_case(
        local_album="[Signal]",
        remote_album="Signal (Remastered)",
        remote_suffix="",
        count=8,
    )

    decision = choose_match(group, [candidate])

    assert decision.status == "matched"
    assert decision.match is not None


def test_partial_track_remaster_labels_match_complete_expanded_release() -> None:
    group, candidate = aligned_release_case(count=10)
    mixed_tracks = tuple(
        replace(track, title=f"Movement {number} (2024 Remaster)")
        if number <= 4
        else replace(track, title=f"Movement {number}")
        for number, track in enumerate(candidate.tracks, start=1)
    )

    decision = choose_match(group, [replace(candidate, tracks=mixed_tracks)])

    assert decision.status == "matched"
    assert decision.match is not None


def test_album_version_matches_remaster_with_one_bounded_duration_drift() -> None:
    group, candidate = aligned_release_case(
        local_album="Signal (The Blue Album)",
        remote_album="Signal (2024 Remaster)",
        local_suffix=" (Album Version)",
        count=10,
    )
    remote_tracks = tuple(
        replace(track, duration_ms=(track.duration_ms or 0) + 4_857)
        if track.track_number == 10
        else track
        for track in candidate.tracks
    )

    score = score_candidate(group, replace(candidate, tracks=remote_tracks))
    decision = choose_match(group, [replace(candidate, tracks=remote_tracks)])

    assert score.eligible is True
    assert score.components["track_coverage"] == 1.0
    assert decision.status == "matched"


def test_equivalent_catalog_duplicates_use_better_duration_fingerprint() -> None:
    group, best = aligned_release_case(
        remote_album="Signal",
        remote_suffix="",
        count=12,
    )
    runner = replace(
        best,
        collection_id=2501,
        artwork_url="https://is1-ssl.mzstatic.com/image/thumb/Music/other.jpg/100x100bb.jpg",
        tracks=tuple(
            replace(track, duration_ms=(track.duration_ms or 0) + 1_000) for track in best.tracks
        ),
    )

    decision = choose_match(group, [runner, best])

    assert decision.status == "matched"
    assert decision.match is not None
    assert decision.match.candidate.collection_id == best.collection_id


def test_ordinary_release_rejects_cross_position_reordering() -> None:
    group, candidate = aligned_release_case(
        local_album="Signal",
        remote_album="Signal",
        local_suffix="",
        remote_suffix="",
    )
    source_tracks = tuple(reversed(candidate.tracks))
    reordered = tuple(
        replace(slot, title=source.title, duration_ms=source.duration_ms)
        for slot, source in zip(candidate.tracks, source_tracks, strict=True)
    )

    decision = choose_match(group, [replace(candidate, tracks=reordered)])

    assert decision.status == "no_match"
    assert "track order mismatch" in decision.scores[0].reasons


def test_identifier_verified_candidate_outranks_duration_tiebreak() -> None:
    group, unverified = aligned_release_case(
        local_album="Signal",
        remote_album="Signal",
        local_suffix="",
        remote_suffix="",
    )
    barcode = "012345678905"
    group = replace(group, barcode=barcode)
    unverified = replace(unverified, collection_id=7101, verified_barcode=None)
    verified = replace(
        unverified,
        collection_id=7102,
        verified_barcode=barcode,
        tracks=tuple(
            replace(track, duration_ms=(track.duration_ms or 0) + 1_000)
            for track in unverified.tracks
        ),
    )

    decision = choose_match(group, [unverified, verified])

    assert decision.status == "matched"
    assert decision.match is not None
    assert decision.match.candidate.collection_id == verified.collection_id


def test_release_label_rule_rejects_arbitrary_album_name_prefix() -> None:
    group, candidate = aligned_release_case(
        local_album="Signal Original Album",
        remote_album="Signal (Expanded Edition)",
    )

    decision = choose_match(group, [candidate])

    assert decision.status == "no_match"


def test_album_field_album_version_is_harmless() -> None:
    group, candidate = aligned_release_case(
        count=8,
        local_album="Signal [Album Version]",
        remote_album="Signal",
        local_suffix="",
        remote_suffix="",
    )
    candidate = replace(candidate, release_year=group.year)

    decision = choose_match(group, [candidate])

    assert decision.status == "matched"


@pytest.mark.parametrize(
    ("local_album", "remote_album"),
    (
        ("The Dark Side of the Moon", "The Dark Side of the Moon Sessions"),
        ("The Dark Side of the Moon Sessions", "The Dark Side of the Moon"),
    ),
)
def test_ordinary_release_rejects_arbitrary_album_title_continuation(
    local_album: str, remote_album: str
) -> None:
    group, candidate = aligned_release_case(
        count=10,
        local_album=local_album,
        remote_album=remote_album,
        local_suffix="",
        remote_suffix="",
    )
    candidate = replace(candidate, release_year=group.year)

    decision = choose_match(group, [candidate])

    assert decision.status == "no_match"
    assert "album title mismatch" in decision.scores[0].reasons


@pytest.mark.parametrize("conflict_side", ("local", "remote"))
def test_ordinary_release_rejects_semantic_conflict_without_local_disc_tags(
    conflict_side: str,
) -> None:
    group, candidate = aligned_release_case(
        count=10,
        local_album="Signal",
        remote_album="Signal",
        local_suffix="",
        remote_suffix="",
    )
    candidate = replace(candidate, release_year=group.year)
    group = replace(
        group,
        logical_tracks=tuple(
            replace(
                track,
                disc_number=None,
                title=(
                    f"{track.title} (Live)"
                    if conflict_side == "local" and index == 0
                    else track.title
                ),
            )
            for index, track in enumerate(group.logical_tracks)
        ),
    )
    if conflict_side == "remote":
        candidate = replace(
            candidate,
            tracks=tuple(
                replace(track, title=f"{track.title} (Live)") if index == 0 else track
                for index, track in enumerate(candidate.tracks)
            ),
        )

    decision = choose_match(group, [candidate])

    assert decision.status == "no_match"
    assert "positioned tracklist mismatch" in decision.scores[0].reasons


def test_ordinary_release_rejects_near_identical_reordered_titles() -> None:
    group, candidate = aligned_release_case(
        count=10,
        local_album="Signal",
        remote_album="Signal",
        local_suffix="",
        remote_suffix="",
    )
    shared = "A Very Long Classical Movement With Many Shared Descriptive Words And Themes"
    local_titles = (f"{shared} Part One", f"{shared} Part Two")
    local_tracks = tuple(
        replace(track, title=local_titles[index], duration_ms=200_000) if index < 2 else track
        for index, track in enumerate(group.logical_tracks)
    )
    group = replace(
        group,
        files=tuple(track.path for track in local_tracks),
        logical_tracks=local_tracks,
    )
    remote_tracks = tuple(
        replace(track, title=local_titles[1 - index], duration_ms=200_000) if index < 2 else track
        for index, track in enumerate(candidate.tracks)
    )

    decision = choose_match(group, [replace(candidate, tracks=remote_tracks)])

    assert decision.status == "no_match"


@pytest.mark.parametrize("label_side", ["local", "provider"])
def test_ordinary_release_rejects_one_semantic_track_conflict(label_side: str) -> None:
    group, candidate = aligned_release_case(
        count=10,
        local_album="Signal",
        remote_album="Signal",
        local_suffix="",
        remote_suffix="",
    )
    if label_side == "local":
        local_tracks = (
            replace(group.logical_tracks[0], title="Movement 1 (Live)"),
            *group.logical_tracks[1:],
        )
        group = replace(group, logical_tracks=local_tracks)
    else:
        remote_tracks = (
            replace(candidate.tracks[0], title="Movement 1 (Live)"),
            *candidate.tracks[1:],
        )
        candidate = replace(candidate, tracks=remote_tracks)

    decision = choose_match(group, [candidate])

    assert decision.status == "no_match"


def test_aligned_release_rejects_conflicting_explicit_remaster_years() -> None:
    group, candidate = aligned_release_case(
        count=10,
        local_album="Signal (2009 Remaster)",
        remote_album="Signal (2010 Remaster)",
        local_suffix=" (2009 Remaster)",
        remote_suffix=" (2010 Remaster)",
    )

    decision = choose_match(group, [candidate])

    assert decision.status == "no_match"


def test_conflicting_verified_barcode_is_ineligible() -> None:
    group, candidate = aligned_release_case(
        local_album="Signal",
        remote_album="Signal",
        local_suffix="",
        remote_suffix="",
    )
    group = replace(group, barcode="012345678905")
    candidate = replace(candidate, verified_barcode="4006381333931")

    decision = choose_match(group, [candidate])

    assert decision.status == "no_match"
    assert "barcode mismatch" in decision.scores[0].reasons


def test_aligned_release_rule_rejects_incomplete_local_album() -> None:
    group, candidate = aligned_release_case(count=10)
    incomplete_tracks = group.logical_tracks[:-1]
    incomplete = replace(
        group,
        files=tuple(track.path for track in incomplete_tracks),
        logical_tracks=incomplete_tracks,
    )

    decision = choose_match(incomplete, [candidate])

    assert decision.status == "no_match"
    assert "local tracklist appears incomplete" in decision.scores[0].reasons


def test_aligned_release_rule_rejects_reordered_tracks() -> None:
    group, candidate = aligned_release_case(count=10)
    first, second, *remaining = candidate.tracks
    reordered = (
        replace(first, title=second.title, duration_ms=second.duration_ms),
        replace(second, title=first.title, duration_ms=first.duration_ms),
        *remaining,
    )

    decision = choose_match(group, [replace(candidate, tracks=reordered)])

    assert decision.status == "no_match"


def test_aligned_release_rule_rejects_near_identical_reordered_labeled_tracks() -> None:
    group, candidate = aligned_release_case(
        count=10,
        local_suffix=" (2024 Remaster)",
    )
    first_title = (
        "A Very Long Classical Movement With Many Shared Descriptive Words "
        "And Themes Part One (2024 Remaster)"
    )
    second_title = (
        "A Very Long Classical Movement With Many Shared Descriptive Words "
        "And Themes Part Two (2024 Remaster)"
    )
    local_tracks = list(group.logical_tracks)
    local_tracks[0] = replace(local_tracks[0], title=first_title, duration_ms=200_000)
    local_tracks[1] = replace(local_tracks[1], title=second_title, duration_ms=200_000)
    reordered_group = replace(group, logical_tracks=tuple(local_tracks))
    remote_tracks = list(candidate.tracks)
    remote_tracks[0] = replace(remote_tracks[0], title=second_title, duration_ms=200_000)
    remote_tracks[1] = replace(remote_tracks[1], title=first_title, duration_ms=200_000)

    decision = choose_match(
        reordered_group,
        [replace(candidate, tracks=tuple(remote_tracks))],
    )

    assert decision.status == "no_match"


@pytest.mark.parametrize(
    "semantic_label",
    ["Live", "Remix", "Acoustic", "Radio Edit", "Instrumental", "Mono", "Stereo", "Demo"],
)
def test_aligned_release_rule_preserves_semantic_version_conflicts(
    semantic_label: str,
) -> None:
    group, candidate = aligned_release_case(count=10)
    conflicting_tracks = tuple(
        replace(track, title=f"Movement 1 ({semantic_label}) (2024 Remaster)")
        if track.track_number == 1
        else track
        for track in candidate.tracks
    )

    decision = choose_match(group, [replace(candidate, tracks=conflicting_tracks)])

    assert decision.status == "no_match"


def test_aligned_release_rule_rejects_too_many_duration_outliers() -> None:
    group, candidate = aligned_release_case(count=10)
    drifted = tuple(
        replace(track, duration_ms=(track.duration_ms or 0) + 11_000)
        if track.track_number in {9, 10}
        else track
        for track in candidate.tracks
    )

    decision = choose_match(group, [replace(candidate, tracks=drifted)])

    assert decision.status == "no_match"


def test_aligned_release_rule_requires_known_durations() -> None:
    group, candidate = aligned_release_case(count=10)
    unknown = tuple(
        replace(track, duration_ms=None) if track.track_number == 1 else track
        for track in candidate.tracks
    )

    decision = choose_match(group, [replace(candidate, tracks=unknown)])

    assert decision.status == "no_match"


def test_aligned_release_rule_rejects_unrelated_album_name() -> None:
    group, candidate = aligned_release_case(count=10)

    decision = choose_match(
        group,
        [replace(candidate, album="Completely Different (Expanded Edition)")],
    )

    assert decision.status == "no_match"


def test_equal_duration_fingerprint_remains_ambiguous() -> None:
    group, candidate = aligned_release_case(
        remote_album="Signal",
        remote_suffix="",
        count=12,
    )
    duplicate = replace(candidate, collection_id=2502)

    decision = choose_match(group, [candidate, duplicate])

    assert decision.status == "ambiguous"
    assert decision.match is None
