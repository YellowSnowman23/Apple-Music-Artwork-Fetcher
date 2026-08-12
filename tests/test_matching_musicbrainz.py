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

RELEASE_ID = "12345678-1234-5678-9234-567812345678"


def musicbrainz_case(
    *,
    local_album: str = "Signal",
    remote_album: str = "Signal",
    local_artist: str = "Exact Artist",
    remote_artist: str = "Exact Artist",
    count: int = 5,
) -> tuple[AlbumGroup, CatalogAlbum]:
    tracks = tuple(
        TrackMetadata(
            path=Path(f"{number:02}.flac"),
            title=f"Movement {number}",
            artist=local_artist,
            album=local_album,
            album_artist=local_artist,
            year=2024,
            track_number=number,
            track_total=count,
            disc_number=1,
            disc_total=1,
            duration_ms=180_000 + number * 10_000,
            musicbrainz_release_id=RELEASE_ID,
            musicbrainz_recording_id=f"00000000-0000-0000-0000-{number:012d}",
        )
        for number in range(1, count + 1)
    )
    group = AlbumGroup(
        album=local_album,
        album_artist=local_artist,
        year=2024,
        files=tuple(track.path for track in tracks),
        logical_tracks=tracks,
        musicbrainz_release_id=RELEASE_ID,
        musicbrainz_provenance_complete=True,
    )
    candidate = CatalogAlbum(
        collection_id=9001,
        album=remote_album,
        artist=remote_artist,
        release_year=2024,
        artwork_url="https://is1-ssl.mzstatic.com/image/thumb/Music/example.jpg/100x100bb.jpg",
        track_count=count,
        tracks=tuple(
            CatalogTrack(
                title=f"Movement {number}",
                artist=remote_artist,
                duration_ms=180_000 + number * 10_000,
                disc_number=1,
                track_number=number,
            )
            for number in range(1, count + 1)
        ),
        verified_musicbrainz_release_id=RELEASE_ID,
        identifier_resolution="musicbrainz_search",
        resolved_musicbrainz_title=remote_album,
        resolved_musicbrainz_artist=remote_artist,
        resolved_musicbrainz_track_count=count,
        resolved_musicbrainz_release_year=2024,
        musicbrainz_search_track_count=count,
        musicbrainz_search_track_count_source="musicbrainz",
    )
    return group, candidate


def test_complete_picard_ids_accept_provider_single_suffix_for_short_release() -> None:
    group, candidate = musicbrainz_case(
        local_album="Quiet Equation",
        remote_album="Quiet Equation - Single",
        count=1,
    )

    decision = choose_match(group, [candidate])

    assert decision.status == "matched"
    assert decision.match is not None
    assert decision.match.match_basis == "musicbrainz"
    assert decision.match.components["identifier"] == 1.0
    assert decision.match.components["musicbrainz_release"] == 1.0


def test_complete_picard_ids_accept_volume_abbreviation_and_ep_suffix() -> None:
    group, candidate = musicbrainz_case(
        local_album="The Sessions, Volume 1",
        remote_album="The Sessions, Vol. 1 - EP",
        count=4,
    )

    assert choose_match(group, [candidate]).status == "matched"


def test_complete_picard_ids_accept_feature_credit_in_single_album_identity() -> None:
    group, candidate = musicbrainz_case(
        local_album="Winter Lantern",
        remote_album="Winter Lantern (feat. Guest Artist) - Single",
        local_artist="Exact Artist feat. Guest Artist",
        remote_artist="Exact Artist",
        count=1,
    )
    candidate = replace(
        candidate,
        tracks=(replace(candidate.tracks[0], title="Movement 1 (feat. Guest Artist)"),),
    )

    assert choose_match(group, [candidate]).status == "matched"


def test_complete_picard_ids_accept_feature_credit_distribution() -> None:
    group, candidate = musicbrainz_case(count=5)
    local_tracks = list(group.logical_tracks)
    local_tracks[1] = replace(
        local_tracks[1],
        artist="Exact Artist feat. Guest One and Guest Two",
    )
    group = replace(group, logical_tracks=tuple(local_tracks))
    remote_tracks = list(candidate.tracks)
    remote_tracks[1] = replace(
        remote_tracks[1],
        artist="Exact Artist, Guest One & Guest Two",
    )
    candidate = replace(candidate, tracks=tuple(remote_tracks))

    score = score_candidate(group, candidate)

    assert score.eligible is True
    assert score.match_basis == "musicbrainz"
    assert score.components["identifier"] == 1.0
    assert score.components["order_agnostic"] == 1.0
    assert score.reasons == ()


def test_complete_picard_ids_accept_one_bounded_duration_drift() -> None:
    group, candidate = musicbrainz_case(count=10)
    remote_tracks = tuple(
        replace(track, duration_ms=(track.duration_ms or 0) + 5_000)
        if track.track_number == 10
        else track
        for track in candidate.tracks
    )

    decision = choose_match(group, [replace(candidate, tracks=remote_tracks)])

    assert decision.status == "matched"


@pytest.mark.parametrize(
    ("local_title", "remote_title"),
    (
        ("Walkin\u2019 Through Rain", "Walking Through Rain"),
        ("Walking Through Rain", "Walkin' Through Rain"),
    ),
)
def test_colloquial_ing_spelling_matches_in_both_directions(
    local_title: str,
    remote_title: str,
) -> None:
    group, candidate = musicbrainz_case(count=5)
    local_tracks = (replace(group.logical_tracks[0], title=local_title), *group.logical_tracks[1:])
    remote_tracks = (replace(candidate.tracks[0], title=remote_title), *candidate.tracks[1:])

    decision = choose_match(
        replace(group, logical_tracks=local_tracks),
        [replace(candidate, tracks=remote_tracks)],
    )

    assert decision.status == "matched"


@pytest.mark.parametrize(
    ("local_title", "remote_title"),
    (
        ("Signal (Kygo Remix)", "Signal (feat. Guest Artist) [Kygo Remix]"),
        ("Signal (feat. Guest Artist) [Kygo Remix]", "Signal (Kygo Remix)"),
    ),
)
def test_feature_credit_normalization_preserves_trailing_remix_in_both_directions(
    local_title: str,
    remote_title: str,
) -> None:
    group, candidate = musicbrainz_case(count=5)
    local_tracks = (replace(group.logical_tracks[0], title=local_title), *group.logical_tracks[1:])
    remote_tracks = (replace(candidate.tracks[0], title=remote_title), *candidate.tracks[1:])

    decision = choose_match(
        replace(group, logical_tracks=local_tracks),
        [replace(candidate, tracks=remote_tracks)],
    )

    assert decision.status == "matched"


def test_release_mbid_selects_identifier_mode_without_complete_recording_ids() -> None:
    group, candidate = musicbrainz_case(
        local_album="Quiet Equation",
        remote_album="Quiet Equation - Single",
        count=3,
    )
    incomplete_tracks = (
        replace(group.logical_tracks[0], musicbrainz_recording_id=None),
        *group.logical_tracks[1:],
    )

    score = score_candidate(replace(group, logical_tracks=incomplete_tracks), candidate)

    assert score.eligible is True
    assert score.match_basis == "musicbrainz"
    assert score.components["musicbrainz_release"] == 1.0
    assert score.components["musicbrainz_complete"] == 0.0


def test_release_mbid_is_authoritative_even_when_a_recording_id_is_invalid() -> None:
    group, candidate = musicbrainz_case(
        local_album="Quiet Equation",
        remote_album="Quiet Equation - Single",
        count=3,
    )
    invalid_tracks = (
        replace(group.logical_tracks[0], musicbrainz_recording_id="not-a-uuid"),
        *group.logical_tracks[1:],
    )

    decision = choose_match(replace(group, logical_tracks=invalid_tracks), [candidate])

    assert decision.status == "matched"
    assert decision.match is not None
    assert decision.match.match_basis == "musicbrainz"
    assert decision.match.components["musicbrainz_complete"] == 0.0


def test_invalid_release_mbid_uses_the_legacy_matcher() -> None:
    group, candidate = musicbrainz_case(
        local_album="Quiet Equation",
        remote_album="Quiet Equation - Single",
        count=1,
    )
    invalid_tracks = tuple(
        replace(track, musicbrainz_release_id="not-a-uuid") for track in group.logical_tracks
    )

    decision = choose_match(
        replace(
            group,
            logical_tracks=invalid_tracks,
            musicbrainz_release_id="not-a-uuid",
        ),
        [candidate],
    )

    assert decision.status == "no_match"
    assert decision.scores[0].match_basis == "legacy"
    assert "album title mismatch" in decision.scores[0].reasons


def test_resolved_mbid_search_uses_canonical_identity_over_local_album_wording() -> None:
    group, candidate = musicbrainz_case(remote_album="Completely Different")

    decision = choose_match(group, [candidate])

    assert decision.status == "matched"
    assert decision.match is not None
    assert decision.match.match_basis == "musicbrainz"
    assert any("resolved MusicBrainz identity" in warning for warning in decision.match.warnings)


@pytest.mark.parametrize("semantic_label", ["Live", "Remix", "Acoustic", "Instrumental"])
def test_release_mbid_makes_semantic_track_labels_non_blocking(semantic_label: str) -> None:
    group, candidate = musicbrainz_case()
    remote_tracks = (
        replace(candidate.tracks[0], title=f"Movement 1 ({semantic_label})"),
        *candidate.tracks[1:],
    )

    decision = choose_match(group, [replace(candidate, tracks=remote_tracks)])

    assert decision.status == "matched"
    assert decision.match is not None
    assert decision.match.match_basis == "musicbrainz"


def test_release_mbid_makes_arbitrary_track_title_presentation_non_blocking() -> None:
    group, candidate = musicbrainz_case()
    remote_tracks = (
        replace(candidate.tracks[0], title="Completely Different Provider Presentation"),
        *candidate.tracks[1:],
    )

    decision = choose_match(group, [replace(candidate, tracks=remote_tracks)])

    assert decision.status == "matched"
    assert decision.match is not None
    assert decision.match.components["order_agnostic"] == 1.0


def test_release_mbid_makes_disc_track_topology_non_blocking() -> None:
    group, candidate = musicbrainz_case()
    remote_tracks = (
        replace(candidate.tracks[0], disc_number=2),
        *candidate.tracks[1:],
    )

    decision = choose_match(group, [replace(candidate, tracks=remote_tracks)])

    assert decision.status == "matched"
    assert decision.match is not None
    assert decision.match.components["order_agnostic"] == 1.0


def _split_local_release_across_two_discs(
    group: AlbumGroup,
) -> AlbumGroup:
    midpoint = len(group.logical_tracks) // 2
    tracks = tuple(
        replace(
            track,
            disc_number=1 if index <= midpoint else 2,
            disc_total=2,
            track_number=index if index <= midpoint else index - midpoint,
            track_total=midpoint,
        )
        for index, track in enumerate(group.logical_tracks, 1)
    )
    return replace(group, logical_tracks=tracks)


@pytest.mark.parametrize("reverse", [False, True])
def test_complete_picard_ids_and_exact_upc_accept_sequential_disc_flattening(
    reverse: bool,
) -> None:
    group, candidate = musicbrainz_case(count=16)
    group = replace(
        _split_local_release_across_two_discs(group),
        barcode="029876543213",
    )
    local_tracks = list(group.logical_tracks)
    local_tracks[-1] = replace(
        local_tracks[-1],
        title="Movement 16 (acoustic)",
    )
    group = replace(group, logical_tracks=tuple(local_tracks))
    remote_tracks = list(candidate.tracks)
    remote_tracks[-1] = replace(
        remote_tracks[-1],
        title="Movement 16 (Acoustic Version)",
    )
    candidate = replace(
        candidate,
        tracks=tuple(remote_tracks),
        verified_barcode="029876543213",
    )
    unverified = replace(candidate, collection_id=9002, verified_barcode=None)
    candidates = [unverified, candidate] if reverse else [candidate, unverified]

    decision = choose_match(group, candidates)

    assert decision.status == "matched"
    assert decision.match is not None
    assert decision.match.candidate.collection_id == candidate.collection_id
    assert decision.match.match_basis == "musicbrainz+upc"
    assert decision.match.components["verified_upc"] == 1.0


def test_release_mbid_accepts_disc_flattening_without_direct_upc_verification() -> None:
    group, candidate = musicbrainz_case(count=6)
    group = replace(
        _split_local_release_across_two_discs(group),
        barcode="029876543213",
    )

    decision = choose_match(group, [candidate])

    assert decision.status == "matched"
    assert decision.match is not None
    assert decision.match.match_basis == "musicbrainz+upc"
    assert decision.match.components["verified_upc"] == 0.0


def test_exact_upc_and_release_mbid_accept_reordered_flattened_tracks() -> None:
    group, candidate = musicbrainz_case(count=6)
    group = replace(
        _split_local_release_across_two_discs(group),
        barcode="029876543213",
    )
    remote_tracks = list(candidate.tracks)
    remote_tracks[3] = replace(remote_tracks[3], track_number=5)
    remote_tracks[4] = replace(remote_tracks[4], track_number=4)
    candidate = replace(
        candidate,
        tracks=tuple(remote_tracks),
        verified_barcode="029876543213",
    )

    decision = choose_match(group, [candidate])

    assert decision.status == "matched"
    assert decision.match is not None
    assert decision.match.components["verified_upc"] == 1.0


def test_release_mbid_tolerates_an_isolated_duration_outlier() -> None:
    group, candidate = musicbrainz_case(count=10)
    remote_tracks = tuple(
        replace(track, duration_ms=(track.duration_ms or 0) + 11_000)
        if track.track_number == 10
        else track
        for track in candidate.tracks
    )

    decision = choose_match(group, [replace(candidate, tracks=remote_tracks)])

    assert decision.status == "matched"
    assert decision.match is not None
    assert decision.match.match_basis == "musicbrainz"


def test_release_mbid_makes_per_track_artist_differences_non_blocking() -> None:
    group, candidate = musicbrainz_case()
    remote_tracks = (
        replace(candidate.tracks[0], artist="Completely Different Artist"),
        *candidate.tracks[1:],
    )

    decision = choose_match(group, [replace(candidate, tracks=remote_tracks)])

    assert decision.status == "matched"
    assert decision.match is not None
    assert decision.match.match_basis == "musicbrainz"


@pytest.mark.parametrize("reverse", [False, True])
def test_release_mbid_resolves_duplicate_candidates_deterministically(reverse: bool) -> None:
    group, candidate = musicbrainz_case()
    duplicate = replace(candidate, collection_id=9002)
    candidates = [duplicate, candidate] if reverse else [candidate, duplicate]

    decision = choose_match(group, candidates)

    assert decision.status == "matched"
    assert decision.match is not None
    assert decision.match.candidate.collection_id == 9001
