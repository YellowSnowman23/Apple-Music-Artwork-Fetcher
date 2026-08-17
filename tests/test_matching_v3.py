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
    matching_basis,
    score_candidate,
)

RELEASE_ID = "12345678-1234-5678-9234-567812345678"
BARCODE = "012345678905"


def identifier_release(
    *,
    count: int = 6,
    barcode: str | None = None,
    release_id: str | None = None,
    complete_musicbrainz: bool = False,
) -> tuple[AlbumGroup, CatalogAlbum]:
    local_tracks = tuple(
        TrackMetadata(
            path=Path(f"{number:02}.flac"),
            title=f"Local Song {number}",
            artist="Trusted Artist",
            album="Trusted Album",
            album_artist="Trusted Artist",
            year=2024,
            track_number=number,
            track_total=count,
            disc_number=1,
            disc_total=1,
            duration_ms=180_000 + number * 1_000,
            barcode=barcode,
            musicbrainz_release_id=release_id,
            musicbrainz_recording_id=(
                f"00000000-0000-0000-0000-{number:012d}" if complete_musicbrainz else None
            ),
        )
        for number in range(1, count + 1)
    )
    group = AlbumGroup(
        album="Trusted Album",
        album_artist="Trusted Artist",
        year=2024,
        files=tuple(track.path for track in local_tracks),
        logical_tracks=local_tracks,
        barcode=barcode,
        musicbrainz_release_id=release_id,
        musicbrainz_provenance_complete=complete_musicbrainz,
    )
    candidate = CatalogAlbum(
        collection_id=3001,
        album="Trusted Album (Deluxe Edition)",
        artist="Trusted Artist",
        release_year=2023,
        artwork_url="https://is1-ssl.mzstatic.com/image/thumb/Music/example.jpg/100x100bb.jpg",
        track_count=count,
        tracks=tuple(
            CatalogTrack(
                title=f"Provider Name {number} (Bonus Explicit Version)",
                artist="Trusted Artist",
                duration_ms=180_000 + source_number * 1_000,
                disc_number=1,
                track_number=number,
            )
            for number, source_number in enumerate(range(count, 0, -1), 1)
        ),
        verified_musicbrainz_release_id=release_id,
        identifier_resolution="musicbrainz_search" if release_id else None,
        resolved_musicbrainz_title=("Trusted Album (Deluxe Edition)" if release_id else None),
        resolved_musicbrainz_artist=("Trusted Artist" if release_id else None),
        resolved_musicbrainz_track_count=(count if release_id else None),
        resolved_musicbrainz_release_year=(2023 if release_id else None),
        musicbrainz_search_track_count=(count if release_id else None),
        musicbrainz_search_track_count_source=("musicbrainz" if release_id else None),
    )
    return group, candidate


def test_upc_is_primary_and_ignores_names_qualifiers_and_track_order() -> None:
    group, candidate = identifier_release(barcode=BARCODE)
    candidate = replace(
        candidate,
        album="Completely Different Provider Presentation",
        artist="Provider Credit Variation",
        verified_barcode=BARCODE,
    )

    decision = choose_match(group, [candidate])

    assert decision.status == "matched"
    assert decision.match is not None
    assert decision.match.match_basis == "upc"
    assert decision.match.components["verified_upc"] == 1.0
    assert decision.match.components["duration_multiset"] == 1.0
    assert "track order difference ignored by identifier-first matching" in (
        decision.match.warnings
    )


def test_release_mbid_alone_enables_identifier_first_matching() -> None:
    group, candidate = identifier_release(release_id=RELEASE_ID)

    decision = choose_match(group, [candidate])

    assert decision.status == "matched"
    assert decision.match is not None
    assert decision.match.match_basis == "musicbrainz"
    assert decision.match.components["musicbrainz_release"] == 1.0
    assert decision.match.components["musicbrainz_complete"] == 0.0


def test_release_mbid_does_not_bless_an_arbitrary_unresolved_candidate() -> None:
    group, candidate = identifier_release(release_id=RELEASE_ID)
    candidate = replace(candidate, identifier_resolution=None)

    decision = choose_match(group, [candidate])

    assert decision.status == "no_match"
    assert "candidate lacks resolved identifier provenance" in decision.scores[0].reasons


def test_resolved_musicbrainz_link_is_direct_and_overrides_release_diagnostics() -> None:
    group, candidate = identifier_release(release_id=RELEASE_ID)
    candidate = replace(
        candidate,
        album="Different Provider Album Text",
        artist="Different Provider Artist Text",
        track_count=len(candidate.tracks) - 1,
        tracks=candidate.tracks[:-1],
        verified_musicbrainz_release_id=RELEASE_ID,
        identifier_resolution="musicbrainz_apple_relation",
    )

    decision = choose_match(group, [candidate])

    assert decision.status == "matched"
    assert decision.match is not None
    assert decision.match.components["verified_musicbrainz"] == 1.0
    assert any("track count difference" in warning for warning in decision.match.warnings)


def test_resolved_musicbrainz_link_rejects_a_conflicting_release_id() -> None:
    group, candidate = identifier_release(release_id=RELEASE_ID)
    candidate = replace(
        candidate,
        verified_musicbrainz_release_id="87654321-4321-8765-9321-876543218765",
    )

    decision = choose_match(group, [candidate])

    assert decision.status == "no_match"
    assert "MusicBrainz release ID mismatch" in decision.scores[0].reasons


def test_complete_musicbrainz_and_upc_report_both_identifier_sources() -> None:
    group, candidate = identifier_release(
        barcode=BARCODE,
        release_id=RELEASE_ID,
        complete_musicbrainz=True,
    )
    candidate = replace(candidate, verified_barcode=BARCODE)

    score = score_candidate(group, candidate)

    assert score.eligible is True
    assert score.match_basis == "musicbrainz+upc"
    assert score.components["musicbrainz_complete"] == 1.0
    assert matching_basis(group) == "musicbrainz+upc"


def test_verified_upc_outranks_an_unverified_candidate_with_cleaner_text() -> None:
    group, verified = identifier_release(barcode=BARCODE)
    verified = replace(verified, verified_barcode=BARCODE)
    unverified = replace(
        verified,
        collection_id=3002,
        verified_barcode=None,
        album=group.album,
        tracks=tuple(
            replace(track, title=local.title, duration_ms=local.duration_ms)
            for local, track in zip(group.logical_tracks, verified.tracks, strict=True)
        ),
    )

    decision = choose_match(group, [unverified, verified])

    assert decision.status == "matched"
    assert decision.match is not None
    assert decision.match.candidate.collection_id == verified.collection_id


def test_identifier_mode_uses_order_independent_durations_to_resolve_candidates() -> None:
    group, best = identifier_release(release_id=RELEASE_ID)
    worse = replace(
        best,
        collection_id=3002,
        tracks=tuple(
            replace(track, duration_ms=(track.duration_ms or 0) + 30_000) for track in best.tracks
        ),
    )

    decision = choose_match(group, [worse, best])

    assert decision.status == "matched"
    assert decision.match is not None
    assert decision.match.candidate.collection_id == best.collection_id


def test_identifier_mode_resolves_equivalent_apple_duplicates_deterministically() -> None:
    group, candidate = identifier_release(
        barcode=BARCODE,
        release_id=RELEASE_ID,
        complete_musicbrainz=True,
    )
    first = replace(candidate, collection_id=3001, verified_barcode=BARCODE)
    second = replace(candidate, collection_id=3002, verified_barcode=BARCODE)

    for candidates in ([second, first], [first, second]):
        decision = choose_match(group, candidates)
        assert decision.status == "matched"
        assert decision.match is not None
        assert decision.match.candidate.collection_id == 3001


def test_identifier_mode_leaves_non_equivalent_direct_collections_ambiguous() -> None:
    group, candidate = identifier_release(barcode=BARCODE)
    clean = replace(candidate, collection_id=3001, verified_barcode=BARCODE)
    explicit = replace(
        candidate,
        collection_id=3002,
        album="Trusted Album (Explicit)",
        verified_barcode=BARCODE,
    )

    for candidates in ([clean, explicit], [explicit, clean]):
        decision = choose_match(group, candidates)
        assert decision.status == "ambiguous"
        assert decision.match is None
        assert "multiple non-equivalent Apple collections" in decision.reason


def test_identifier_mode_leaves_different_direct_artwork_assets_ambiguous() -> None:
    group, candidate = identifier_release(barcode=BARCODE)
    first = replace(
        candidate,
        collection_id=3001,
        verified_barcode=BARCODE,
        artwork_url="https://example.invalid/cover-a.jpg",
    )
    second = replace(
        candidate,
        collection_id=3002,
        verified_barcode=BARCODE,
        artwork_url="https://example.invalid/cover-b.jpg",
    )

    for candidates in ([first, second], [second, first]):
        decision = choose_match(group, candidates)
        assert decision.status == "ambiguous"
        assert decision.match is None


def test_direct_identifier_uses_one_exact_track_count_to_break_an_artwork_tie() -> None:
    group, candidate = identifier_release(barcode=BARCODE)
    exact = replace(
        candidate,
        collection_id=3001,
        verified_barcode=BARCODE,
        artwork_url="https://example.invalid/exact.jpg",
    )
    expanded_tracks = (
        *candidate.tracks,
        CatalogTrack(
            title="Provider Bonus 7",
            artist="Trusted Artist",
            duration_ms=240_000,
            disc_number=1,
            track_number=7,
        ),
        CatalogTrack(
            title="Provider Bonus 8",
            artist="Trusted Artist",
            duration_ms=250_000,
            disc_number=1,
            track_number=8,
        ),
    )
    expanded = replace(
        candidate,
        collection_id=3002,
        track_count=8,
        tracks=expanded_tracks,
        verified_barcode=BARCODE,
        artwork_url="https://example.invalid/expanded.jpg",
    )

    for candidates in ([expanded, exact], [exact, expanded]):
        decision = choose_match(group, candidates)
        assert decision.status == "matched"
        assert decision.match is not None
        assert decision.match.candidate.collection_id == 3001
        assert "uniquely supported by exact local track count" in decision.reason


def test_direct_identifier_uses_one_strong_duration_fingerprint_to_break_an_artwork_tie() -> None:
    group, candidate = identifier_release(barcode=BARCODE)
    matching = replace(
        candidate,
        collection_id=3001,
        verified_barcode=BARCODE,
        artwork_url="https://example.invalid/matching.jpg",
    )
    different = replace(
        candidate,
        collection_id=3002,
        tracks=tuple(
            replace(track, duration_ms=(track.duration_ms or 0) + 30_000)
            for track in candidate.tracks
        ),
        verified_barcode=BARCODE,
        artwork_url="https://example.invalid/different.jpg",
    )

    for candidates in ([different, matching], [matching, different]):
        decision = choose_match(group, candidates)
        assert decision.status == "matched"
        assert decision.match is not None
        assert decision.match.candidate.collection_id == 3001
        assert "uniquely supported by duration fingerprint" in decision.reason


def test_direct_identifier_keeps_conflicting_count_and_duration_evidence_ambiguous() -> None:
    group, candidate = identifier_release(barcode=BARCODE)
    count_match = replace(
        candidate,
        collection_id=3001,
        tracks=tuple(
            replace(track, duration_ms=(track.duration_ms or 0) + 30_000)
            for track in candidate.tracks
        ),
        verified_barcode=BARCODE,
        artwork_url="https://example.invalid/count.jpg",
    )
    duration_match = replace(
        candidate,
        collection_id=3002,
        track_count=7,
        tracks=(
            *candidate.tracks,
            CatalogTrack(
                title="Provider Bonus 7",
                artist="Trusted Artist",
                duration_ms=250_000,
                disc_number=1,
                track_number=7,
            ),
        ),
        verified_barcode=BARCODE,
        artwork_url="https://example.invalid/duration.jpg",
    )

    for candidates in ([count_match, duration_match], [duration_match, count_match]):
        decision = choose_match(group, candidates)
        assert decision.status == "ambiguous"
        assert decision.match is None


def test_direct_identifier_accepts_collection_only_lookup_with_warning() -> None:
    group, candidate = identifier_release(barcode=BARCODE)
    candidate = replace(candidate, tracks=(), verified_barcode=BARCODE)

    decision = choose_match(group, [candidate])

    assert decision.status == "matched"
    assert decision.match is not None
    assert any(
        "incomplete or unavailable song tracklist" in warning for warning in decision.match.warnings
    )


def test_musicbrainz_search_still_rejects_collection_only_lookup() -> None:
    group, candidate = identifier_release(release_id=RELEASE_ID)
    candidate = replace(candidate, tracks=())

    decision = choose_match(group, [candidate])

    assert decision.status == "no_match"
    assert "Apple tracklist appears incomplete" in decision.scores[0].reasons


def test_identifier_mode_rejects_a_conflicting_verified_upc() -> None:
    group, candidate = identifier_release(barcode=BARCODE)
    candidate = replace(candidate, verified_barcode="4006381333931")

    decision = choose_match(group, [candidate])

    assert decision.status == "no_match"
    assert "barcode mismatch" in decision.scores[0].reasons


def test_equivalent_upca_and_ean13_widths_are_the_same_identifier() -> None:
    group, candidate = identifier_release(barcode=BARCODE)
    candidate = replace(candidate, verified_barcode=f"0{BARCODE}")

    decision = choose_match(group, [candidate])

    assert decision.status == "matched"
    assert decision.match is not None
    assert decision.match.components["verified_upc"] == 1.0


def test_verified_upc_treats_a_different_release_size_as_non_blocking() -> None:
    group, candidate = identifier_release(barcode=BARCODE)
    candidate = replace(
        candidate,
        track_count=len(candidate.tracks) - 1,
        tracks=candidate.tracks[:-1],
        verified_barcode=BARCODE,
    )

    decision = choose_match(group, [candidate])

    assert decision.status == "matched"
    assert decision.match is not None
    assert any("track count difference" in warning for warning in decision.match.warnings)


def test_resolved_mbid_search_accepts_local_album_and_artist_presentation_differences() -> None:
    group, candidate = identifier_release(release_id=RELEASE_ID)
    group = replace(
        group,
        album="Unrelated Local Presentation",
        album_artist="Different Local Credit",
    )

    decision = choose_match(group, [candidate])

    assert decision.status == "matched"
    assert decision.match is not None
    assert any("resolved MusicBrainz identity" in warning for warning in decision.match.warnings)


def test_resolved_mbid_search_accepts_feature_credit_relocation() -> None:
    group, candidate = identifier_release(release_id=RELEASE_ID)
    candidate = replace(
        candidate,
        album="Signal",
        artist="Primary feat. Alice",
        resolved_musicbrainz_title="Signal (feat. Alice)",
        resolved_musicbrainz_artist="Primary",
    )

    assert choose_match(group, [candidate]).status == "matched"


@pytest.mark.parametrize(
    ("resolved_title", "resolved_artist", "candidate_artist"),
    (
        ("Signal (feat. Alice)", "Primary", "Primary & Alice"),
        ("Signal (feat. Alice & Bob)", "Primary", "Primary, Alice & Bob"),
        ("Signal (feat. Alice)", "The Primary", "Primary & Alice"),
        ("Signal (feat. Alice)", "Primary", "The Primary & Alice"),
    ),
)
def test_resolved_mbid_search_accepts_known_features_in_unmarked_artist_suffix(
    resolved_title: str,
    resolved_artist: str,
    candidate_artist: str,
) -> None:
    group, candidate = identifier_release(release_id=RELEASE_ID)
    candidate = replace(
        candidate,
        album="Signal",
        artist=candidate_artist,
        resolved_musicbrainz_title=resolved_title,
        resolved_musicbrainz_artist=resolved_artist,
    )

    assert choose_match(group, [candidate]).status == "matched"


def test_resolved_mbid_search_accepts_redundant_explicit_and_joint_artist_credit() -> None:
    group, candidate = identifier_release(release_id=RELEASE_ID)
    candidate = replace(
        candidate,
        album="Signal (feat. Alice)",
        artist="Primary & Alice",
        resolved_musicbrainz_title="Signal (feat. Alice)",
        resolved_musicbrainz_artist="Primary",
    )

    assert choose_match(group, [candidate]).status == "matched"


def test_resolved_mbid_search_accepts_repeated_equivalent_feature_markers() -> None:
    group, candidate = identifier_release(release_id=RELEASE_ID)
    candidate = replace(
        candidate,
        album="Signal (feat. Alice feat. Bob)",
        artist="Primary",
        resolved_musicbrainz_title="Signal (feat. Alice & Bob)",
        resolved_musicbrainz_artist="Primary",
    )

    assert choose_match(group, [candidate]).status == "matched"


@pytest.mark.parametrize(
    ("resolved_artist", "candidate_artist"),
    (
        ("Florence + The Machine", "Florence + The Machine + Alice"),
        ("Earth, Wind & Fire", "Earth, Wind & Fire & Alice"),
        ("Simon & Garfunkel", "Simon & Garfunkel & Alice"),
    ),
)
def test_resolved_mbid_search_preserves_delimiters_inside_primary_artist_name(
    resolved_artist: str,
    candidate_artist: str,
) -> None:
    group, candidate = identifier_release(release_id=RELEASE_ID)
    candidate = replace(
        candidate,
        album="Signal",
        artist=candidate_artist,
        resolved_musicbrainz_title="Signal (feat. Alice)",
        resolved_musicbrainz_artist=resolved_artist,
    )

    assert choose_match(group, [candidate]).status == "matched"


@pytest.mark.parametrize(
    ("resolved_artist", "candidate_artist"),
    (
        ("Florence + The Machine", "Florence feat. The Machine"),
        ("Earth, Wind & Fire", "Earth feat. Wind & Fire"),
        ("Simon & Garfunkel", "Simon feat. Garfunkel"),
        ("Sleeping With Sirens", "Sleeping feat. Sirens"),
    ),
)
def test_resolved_mbid_search_does_not_invent_features_inside_group_names(
    resolved_artist: str,
    candidate_artist: str,
) -> None:
    group, candidate = identifier_release(release_id=RELEASE_ID)
    candidate = replace(
        candidate,
        album="Signal",
        artist=candidate_artist,
        resolved_musicbrainz_title="Signal",
        resolved_musicbrainz_artist=resolved_artist,
    )

    assert choose_match(group, [candidate]).status == "no_match"


@pytest.mark.parametrize(
    ("resolved_title", "resolved_artist", "candidate_artist"),
    (
        ("Signal (feat. Supply)", "Air", "Air Supply"),
        ("Signal (feat. Smith)", "John", "John Smith"),
    ),
)
def test_resolved_mbid_search_rejects_unmarked_artist_without_a_delimiter(
    resolved_title: str,
    resolved_artist: str,
    candidate_artist: str,
) -> None:
    group, candidate = identifier_release(release_id=RELEASE_ID)
    candidate = replace(
        candidate,
        album="Signal",
        artist=candidate_artist,
        resolved_musicbrainz_title=resolved_title,
        resolved_musicbrainz_artist=resolved_artist,
    )

    assert choose_match(group, [candidate]).status == "no_match"


def test_resolved_mbid_search_does_not_treat_lexical_with_as_an_unmarked_delimiter() -> None:
    group, candidate = identifier_release(release_id=RELEASE_ID)
    candidate = replace(
        candidate,
        album="Signal",
        artist="Sleeping With Sirens",
        resolved_musicbrainz_title="Signal (feat. Sirens)",
        resolved_musicbrainz_artist="Sleeping",
    )

    assert choose_match(group, [candidate]).status == "no_match"


def test_resolved_mbid_search_does_not_split_lexical_with_as_a_feature() -> None:
    group, candidate = identifier_release(release_id=RELEASE_ID)
    candidate = replace(
        candidate,
        album="Sequence",
        artist="Artist feat. Tokens",
        resolved_musicbrainz_title="Sequence with Tokens",
        resolved_musicbrainz_artist="Artist",
    )

    assert choose_match(group, [candidate]).status == "no_match"


@pytest.mark.parametrize("collaborator", ("Live", "Mono", "Stereo", "Demo", "Remix"))
def test_resolved_mbid_search_ignores_edition_words_inside_feature_credit(
    collaborator: str,
) -> None:
    group, candidate = identifier_release(release_id=RELEASE_ID)
    candidate = replace(
        candidate,
        album="Signal",
        artist=f"Primary feat. {collaborator}",
        resolved_musicbrainz_title=f"Signal (feat. {collaborator})",
        resolved_musicbrainz_artist="Primary",
    )

    assert choose_match(group, [candidate]).status == "matched"


@pytest.mark.parametrize("marker", ("with", "w/"))
def test_resolved_mbid_search_accepts_bracketed_feature_marker_variants(
    marker: str,
) -> None:
    group, candidate = identifier_release(release_id=RELEASE_ID)
    candidate = replace(
        candidate,
        album="Signal",
        artist="Primary feat. Alice",
        resolved_musicbrainz_title=f"Signal ({marker} Alice)",
        resolved_musicbrainz_artist="Primary",
    )

    assert choose_match(group, [candidate]).status == "matched"


def test_resolved_mbid_search_accepts_unbracketed_w_slash_feature_marker() -> None:
    group, candidate = identifier_release(release_id=RELEASE_ID)
    candidate = replace(
        candidate,
        album="Signal",
        artist="Primary feat. Alice",
        resolved_musicbrainz_title="Signal w/ Alice",
        resolved_musicbrainz_artist="Primary",
    )

    assert choose_match(group, [candidate]).status == "matched"


@pytest.mark.parametrize("separator", (" - ", "-", " — ", "—"))
def test_resolved_mbid_search_preserves_dash_delimited_semantic_qualifier(
    separator: str,
) -> None:
    group, candidate = identifier_release(release_id=RELEASE_ID)
    candidate = replace(
        candidate,
        album="Signal (Live)",
        artist="Primary feat. Alice",
        resolved_musicbrainz_title=f"Signal feat. Alice{separator}Live",
        resolved_musicbrainz_artist="Primary",
    )

    assert choose_match(group, [candidate]).status == "matched"


@pytest.mark.parametrize(
    "collaborator",
    ("JAY\u2011Z", "T\u2010Pain", "A\u2013Trak", "Jean\u2013Luc"),
)
def test_resolved_mbid_search_preserves_dashes_inside_featured_artist(
    collaborator: str,
) -> None:
    group, candidate = identifier_release(release_id=RELEASE_ID)
    candidate = replace(
        candidate,
        album="Signal",
        artist=f"Primary feat. {collaborator}",
        resolved_musicbrainz_title=f"Signal feat. {collaborator}",
        resolved_musicbrainz_artist="Primary",
    )

    assert choose_match(group, [candidate]).status == "matched"


def test_resolved_mbid_search_keeps_feature_identity_with_trailing_qualifiers() -> None:
    group, candidate = identifier_release(release_id=RELEASE_ID)
    candidate = replace(
        candidate,
        album="Signal (Kygo Remix) [feat. Alice]",
        artist="Primary",
        resolved_musicbrainz_title="Signal (feat. Alice) [Kygo Remix]",
        resolved_musicbrainz_artist="Primary",
    )

    assert choose_match(group, [candidate]).status == "matched"


@pytest.mark.parametrize(
    ("resolved_title", "candidate_title"),
    (
        ("Signal feat. Alice - 2011 Remaster", "Signal (feat. Alice) [2011 Remaster]"),
        ("Signal feat. Alice - Album Version", "Signal (feat. Alice) [Album Version]"),
        ("Signal (feat. Alice - Live)", "Signal (feat. Alice) [Live]"),
        ("Signal feat. Alice - Kygo Remix", "Signal (feat. Alice) [Kygo Remix]"),
        ("Signal feat. Alice - Bonus Track", "Signal (feat. Alice) [Bonus Track]"),
        ("Signal feat. Alice - Explicit", "Signal (feat. Alice) [Explicit]"),
    ),
)
def test_resolved_mbid_search_keeps_feature_identity_around_trailing_qualifiers(
    resolved_title: str,
    candidate_title: str,
) -> None:
    group, candidate = identifier_release(release_id=RELEASE_ID)
    candidate = replace(
        candidate,
        album=candidate_title,
        artist="Primary",
        resolved_musicbrainz_title=resolved_title,
        resolved_musicbrainz_artist="Primary",
    )

    assert choose_match(group, [candidate]).status == "matched"


def test_resolved_mbid_search_accepts_dash_vs_bracket_remaster_presentation() -> None:
    group, candidate = identifier_release(release_id=RELEASE_ID)
    candidate = replace(
        candidate,
        album="Signal [2011 Remaster]",
        artist="Primary feat. Alice",
        resolved_musicbrainz_title="Signal [feat. Alice] — 2011 Remaster",
        resolved_musicbrainz_artist="Primary",
    )

    assert choose_match(group, [candidate]).status == "matched"


@pytest.mark.parametrize("collaborator", ("(G)I-DLE", "Alice (US)"))
def test_resolved_mbid_search_preserves_parentheses_inside_featured_artist(
    collaborator: str,
) -> None:
    group, candidate = identifier_release(release_id=RELEASE_ID)
    candidate = replace(
        candidate,
        album="Signal",
        artist=f"Primary feat. {collaborator}",
        resolved_musicbrainz_title=f"Signal (feat. {collaborator})",
        resolved_musicbrainz_artist="Primary",
    )

    assert choose_match(group, [candidate]).status == "matched"


def test_resolved_mbid_search_rejects_an_unbalanced_feature_credit() -> None:
    group, candidate = identifier_release(release_id=RELEASE_ID)
    candidate = replace(
        candidate,
        album="Signal",
        artist="Primary feat. Alice",
        resolved_musicbrainz_title="Signal (feat. Alice",
        resolved_musicbrainz_artist="Primary",
    )

    assert choose_match(group, [candidate]).status == "no_match"


def test_resolved_mbid_search_rejects_a_different_featured_artist() -> None:
    group, candidate = identifier_release(release_id=RELEASE_ID)
    candidate = replace(
        candidate,
        album="Signal (feat. Bob)",
        artist="Primary",
        resolved_musicbrainz_title="Signal (feat. Alice)",
        resolved_musicbrainz_artist="Primary feat. Alice",
    )

    decision = choose_match(group, [candidate])

    assert decision.status == "no_match"
    assert "candidate lacks resolved identifier provenance" in decision.scores[0].reasons


def test_resolved_mbid_search_accepts_unicode_dash_presentation_labels() -> None:
    group, candidate = identifier_release(release_id=RELEASE_ID)
    candidate = replace(
        candidate,
        album="Midnight Signals (Deluxe)",
        artist="Example Artist",
        resolved_musicbrainz_title="Midnight Signals (Deluxe \u2013 Explicit)",
        resolved_musicbrainz_artist="Example Artist",
    )

    assert choose_match(group, [candidate]).status == "matched"


def test_legacy_fallback_remains_strict_when_identifiers_are_absent() -> None:
    group, candidate = identifier_release()

    decision = choose_match(group, [candidate])

    assert matching_basis(group) == "legacy"
    assert decision.status == "no_match"
    assert decision.scores[0].match_basis == "legacy"


def test_invalid_identifier_placeholders_use_the_legacy_fallback() -> None:
    group, candidate = identifier_release()
    group = replace(
        group,
        barcode="not-a-upc",
        musicbrainz_release_id="not-an-mbid",
    )

    assert matching_basis(group) == "legacy"
    assert choose_match(group, [candidate]).status == "no_match"


def test_nil_mbid_and_all_zero_gtin_are_not_valid_identifiers() -> None:
    group, candidate = identifier_release()
    group = replace(
        group,
        barcode="00000000",
        musicbrainz_release_id="00000000-0000-0000-0000-000000000000",
    )

    assert matching_basis(group) == "legacy"
    assert choose_match(group, [candidate]).status == "no_match"


def test_conflicting_group_identifiers_are_a_hard_block_for_custom_candidates() -> None:
    group, candidate = identifier_release()
    conflict = "conflicting UPC/barcode tags within the album group"
    group = replace(group, identifier_conflicts=(conflict,))

    decision = choose_match(group, [candidate])

    assert decision.status == "no_match"
    assert conflict in decision.scores[0].reasons
