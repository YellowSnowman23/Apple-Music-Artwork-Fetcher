from dataclasses import replace
from pathlib import Path

from apple_artwork import (
    AlbumGroup,
    CatalogAlbum,
    CatalogTrack,
    TrackMetadata,
    choose_match,
    score_candidate,
)


def local_group(artist: str = "Alpha", album: str = "Greatest Hits") -> AlbumGroup:
    tracks = tuple(
        TrackMetadata(
            path=Path(f"{artist}/{album}/{number:02}.flac"),
            title=title,
            artist=artist,
            album=album,
            album_artist=artist,
            year=2020,
            track_number=number,
            track_total=3,
            disc_number=1,
            disc_total=1,
            duration_ms=duration,
        )
        for number, (title, duration) in enumerate(
            [("First Light", 180_000), ("Home Again", 201_000), ("Last Dance", 242_000)],
            start=1,
        )
    )
    return AlbumGroup(
        album=album,
        album_artist=artist,
        year=2020,
        files=tuple(track.path for track in tracks),
        logical_tracks=tracks,
    )


def catalog_album(
    artist: str, album: str = "Greatest Hits", collection_id: int = 1
) -> CatalogAlbum:
    return CatalogAlbum(
        collection_id=collection_id,
        album=album,
        artist=artist,
        release_year=2020,
        artwork_url="https://is1-ssl.mzstatic.com/image/thumb/Music/test.jpg/100x100bb.jpg",
        track_count=3,
        tracks=(
            CatalogTrack("First Light", artist, 180_100, 1, 1),
            CatalogTrack("Home Again", artist, 200_900, 1, 2),
            CatalogTrack("Last Dance", artist, 242_200, 1, 3),
        ),
    )


def test_exact_album_title_cannot_override_a_different_artist() -> None:
    score = score_candidate(local_group(), catalog_album("Completely Different Artist"))

    assert score.eligible is False
    assert "artist mismatch" in score.reasons


def test_trailing_album_version_annotation_does_not_block_tracklist_match() -> None:
    group = local_group()
    annotated_tracks = tuple(
        replace(track, title=f"{track.title} (Album Version)") for track in group.logical_tracks
    )
    annotated_group = replace(
        group,
        files=tuple(track.path for track in annotated_tracks),
        logical_tracks=annotated_tracks,
    )

    score = score_candidate(annotated_group, catalog_album("Alpha"))

    assert score.eligible is True
    assert score.components["track_coverage"] == 1.0


def _van_halen_iii_case() -> tuple[AlbumGroup, CatalogAlbum]:
    local_rows = (
        ("Neworld (Instrumental Album Version)", 105_560),
        ("Without You (Album Version)", 390_107),
        ("One I Want (Album Version)", 330_800),
        ("From Afar (Album Version)", 324_227),
        ("Dirty Water Dog (Album Version)", 327_307),
        ("Once (Album Version)", 462_733),
        ("Fire in the Hole", 331_627),
        ("Josephina (Album Version)", 342_400),
        ("Year to the Day (Album Version)", 514_533),
        ("Primary (Instrumental Album Version)", 87_000),
        ("Ballot or the Bullet (Album Version)", 342_107),
        ("How Many Say I (Album Version)", 364_027),
    )
    remote_rows = (
        ("Neworld", 105_560),
        ("Without You", 390_107),
        ("One I Want", 330_800),
        ("From Afar", 324_227),
        ("Dirty Water Dog", 327_307),
        ("Once", 462_733),
        ("Fire In the Hole", 331_627),
        ("Josephina", 342_400),
        ("Year to the Day", 514_533),
        ("Primary", 87_000),
        ("Ballot or the Bullet", 342_107),
        ("How Many Say I", 364_027),
    )
    tracks = tuple(
        TrackMetadata(
            path=Path(f"{number:02}.flac"),
            title=title,
            artist="Van Halen",
            album="Van Halen III",
            album_artist="Van Halen",
            year=1998,
            track_number=number,
            track_total=12,
            disc_number=1,
            disc_total=1,
            duration_ms=duration,
        )
        for number, (title, duration) in enumerate(local_rows, start=1)
    )
    group = AlbumGroup(
        album="Van Halen III",
        album_artist="Van Halen",
        year=1998,
        files=tuple(track.path for track in tracks),
        logical_tracks=tracks,
    )
    candidate = CatalogAlbum(
        collection_id=215638174,
        album="Van Halen III",
        artist="Van Halen",
        release_year=1998,
        artwork_url="https://is1-ssl.mzstatic.com/image/thumb/Music/example.jpg/100x100bb.jpg",
        track_count=12,
        tracks=tuple(
            CatalogTrack(title, "Van Halen", duration, 1, number)
            for number, (title, duration) in enumerate(remote_rows, start=1)
        ),
    )
    return group, candidate


def test_provider_omitted_instrumental_album_version_matches_complete_album() -> None:
    group, candidate = _van_halen_iii_case()

    score = score_candidate(group, candidate)
    decision = choose_match(group, [candidate])

    assert score.eligible is True
    assert score.components["track_coverage"] == 1.0
    assert score.components["track_title"] == 1.0
    assert decision.status == "matched"
    assert decision.match is not None
    assert decision.match.candidate.collection_id == 215638174


def test_provider_omitted_instrumental_requires_known_aligned_durations() -> None:
    group, candidate = _van_halen_iii_case()
    tracks = tuple(
        replace(track, duration_ms=None) if track.track_number == 2 else track
        for track in group.logical_tracks
    )
    incomplete_evidence = replace(
        group,
        files=tuple(track.path for track in tracks),
        logical_tracks=tracks,
    )

    decision = choose_match(incomplete_evidence, [candidate])

    assert decision.status == "no_match"
    assert decision.scores[0].components["track_coverage"] < 0.85


def test_provider_omitted_instrumental_rejects_incompatible_aligned_duration() -> None:
    group, candidate = _van_halen_iii_case()
    tracks = tuple(
        replace(track, duration_ms=track.duration_ms + 10_000)
        if track.track_number == 2 and track.duration_ms is not None
        else track
        for track in group.logical_tracks
    )
    incompatible_group = replace(
        group,
        files=tuple(track.path for track in tracks),
        logical_tracks=tracks,
    )

    decision = choose_match(incompatible_group, [candidate])

    assert decision.status == "no_match"
    assert decision.scores[0].components["track_coverage"] < 0.85


def test_provider_omitted_instrumental_requires_matching_topology() -> None:
    group, candidate = _van_halen_iii_case()
    remote_tracks = (
        replace(candidate.tracks[0], track_number=13),
        *candidate.tracks[1:],
    )

    decision = choose_match(group, [replace(candidate, tracks=remote_tracks)])

    assert decision.status == "no_match"
    assert "disc/track topology mismatch" in decision.scores[0].reasons


def test_provider_omitted_instrumental_rejects_reordered_equal_duration_tracks() -> None:
    group, candidate = _van_halen_iii_case()
    local_tracks = tuple(
        replace(track, duration_ms=100_000) if track.track_number in {1, 10} else track
        for track in group.logical_tracks
    )
    reordered_group = replace(
        group,
        files=tuple(track.path for track in local_tracks),
        logical_tracks=local_tracks,
    )
    remote_tracks = tuple(
        replace(track, title="Primary", duration_ms=100_000)
        if track.track_number == 1
        else replace(track, title="Neworld", duration_ms=100_000)
        if track.track_number == 10
        else track
        for track in candidate.tracks
    )

    decision = choose_match(reordered_group, [replace(candidate, tracks=remote_tracks)])

    assert decision.status == "no_match"
    assert decision.scores[0].components["track_coverage"] < 0.85


def test_provider_omitted_instrumental_never_strips_provider_explicit_label() -> None:
    group, candidate = _van_halen_iii_case()
    local_tracks = tuple(
        replace(track, title="Neworld")
        if track.track_number == 1
        else replace(track, title="Primary")
        if track.track_number == 10
        else track
        for track in group.logical_tracks
    )
    plain_group = replace(
        group,
        files=tuple(track.path for track in local_tracks),
        logical_tracks=local_tracks,
    )
    remote_tracks = tuple(
        replace(track, title=f"{track.title} (Instrumental)")
        if track.track_number in {1, 10}
        else track
        for track in candidate.tracks
    )

    decision = choose_match(plain_group, [replace(candidate, tracks=remote_tracks)])

    assert decision.status == "no_match"
    assert decision.scores[0].components["track_coverage"] < 0.85


def test_provider_omitted_instrumental_preserves_live_conflicts() -> None:
    group, candidate = _van_halen_iii_case()
    local_tracks = tuple(
        replace(track, title="Neworld (Live Instrumental Album Version)")
        if track.track_number == 1
        else track
        for track in group.logical_tracks
    )
    conflicting_group = replace(
        group,
        files=tuple(track.path for track in local_tracks),
        logical_tracks=local_tracks,
    )

    decision = choose_match(conflicting_group, [candidate])

    assert decision.status == "no_match"
    assert decision.scores[0].components["track_coverage"] < 0.85


def test_provider_omitted_instrumental_preserves_candidate_ambiguity() -> None:
    group, candidate = _van_halen_iii_case()

    decision = choose_match(group, [candidate, replace(candidate, collection_id=999_999)])

    assert decision.status == "ambiguous"
    assert decision.match is None


def test_uniform_trailing_remaster_annotations_match_plain_apple_track_titles() -> None:
    local_rows = (
        ("1984 (2015 Remaster)", 67_665),
        ("Jump (2015 Remaster)", 241_756),
        ("Panama (2015 Remaster)", 209_662),
        ("Top Jimmy (2015 Remaster)", 180_093),
        ("Drop Dead Legs (2015 Remaster)", 253_857),
        ("Hot for Teacher (2015 Remaster)", 282_577),
        ("I'll Wait (2015 Remaster)", 279_524),
        ("Girl Gone Bad (2015 Remaster)", 274_440),
        ("House of Pain (2015 Remaster)", 198_462),
    )
    remote_rows = (
        ("1984", 67_517),
        ("Jump", 241_643),
        ("Panama", 210_227),
        ("Top Jimmy", 179_907),
        ("Drop Dead Legs", 254_213),
        ("Hot for Teacher", 282_747),
        ("I'll Wait", 280_147),
        ("Girl Gone Bad", 273_907),
        ("House of Pain", 199_840),
    )
    tracks = tuple(
        TrackMetadata(
            path=Path(f"{number:02}.flac"),
            title=title,
            artist="Van Halen",
            album="1984",
            album_artist="Van Halen",
            year=1984,
            track_number=number,
            track_total=9,
            disc_number=None,
            duration_ms=duration,
        )
        for number, (title, duration) in enumerate(local_rows, start=1)
    )
    group = AlbumGroup(
        album="1984",
        album_artist="Van Halen",
        year=1984,
        files=tuple(track.path for track in tracks),
        logical_tracks=tracks,
    )
    candidate = CatalogAlbum(
        collection_id=976831013,
        album="1984",
        artist="Van Halen",
        release_year=1984,
        artwork_url="https://is1-ssl.mzstatic.com/image/thumb/Music125/example.jpg/100x100bb.jpg",
        track_count=9,
        tracks=tuple(
            CatalogTrack(title, "Van Halen", duration, 1, number)
            for number, (title, duration) in enumerate(remote_rows, start=1)
        ),
    )

    score = score_candidate(group, candidate)
    decision = choose_match(group, [candidate])

    assert score.eligible is True
    assert score.components["track_coverage"] == 1.0
    assert score.components["track_title"] == 1.0
    assert decision.status == "matched"
    assert decision.match is not None
    assert decision.match.candidate.collection_id == 976831013


def test_uniform_remaster_exception_requires_uniform_annotations() -> None:
    group = local_group()
    tracks = tuple(
        replace(track, title=f"{track.title} (2015 Remaster)") if number < 3 else track
        for number, track in enumerate(group.logical_tracks, start=1)
    )
    mixed_group = replace(
        group,
        files=tuple(track.path for track in tracks),
        logical_tracks=tracks,
    )

    score = score_candidate(mixed_group, catalog_album("Alpha"))

    assert score.eligible is False
    assert score.components["track_coverage"] < 0.85


def test_uniform_remaster_exception_requires_matching_track_topology() -> None:
    group = local_group()
    tracks = tuple(
        replace(track, title=f"{track.title} (2015 Remaster)") for track in group.logical_tracks
    )
    annotated_group = replace(
        group,
        files=tuple(track.path for track in tracks),
        logical_tracks=tracks,
    )
    candidate = catalog_album("Alpha")
    mismatched_candidate = replace(
        candidate,
        tracks=(replace(candidate.tracks[0], track_number=4), *candidate.tracks[1:]),
    )

    score = score_candidate(annotated_group, mismatched_candidate)

    assert score.eligible is False
    assert "disc/track topology mismatch" in score.reasons
    assert score.components["track_coverage"] == 0.0


def test_uniform_remaster_exception_preserves_conflicting_remaster_years() -> None:
    group = local_group()
    local_tracks = tuple(
        replace(track, title=f"{track.title} (2015 Remaster)") for track in group.logical_tracks
    )
    annotated_group = replace(
        group,
        files=tuple(track.path for track in local_tracks),
        logical_tracks=local_tracks,
    )
    candidate = catalog_album("Alpha")
    remote_tracks = tuple(
        replace(track, title=f"{track.title} (2009 Remaster)") for track in candidate.tracks
    )

    score = score_candidate(annotated_group, replace(candidate, tracks=remote_tracks))

    assert score.eligible is False
    assert score.components["track_coverage"] == 0.0


def test_uniform_remaster_exception_requires_known_compatible_durations() -> None:
    group = local_group()
    tracks = tuple(
        replace(
            track,
            title=f"{track.title} (2015 Remaster)",
            duration_ms=None,
        )
        for track in group.logical_tracks
    )
    annotated_group = replace(
        group,
        files=tuple(track.path for track in tracks),
        logical_tracks=tracks,
    )

    score = score_candidate(annotated_group, catalog_album("Alpha"))

    assert score.eligible is False
    assert score.components["track_coverage"] == 0.0


def test_uniform_remaster_exception_rejects_reordered_equal_duration_tracks() -> None:
    group = local_group()
    local_tracks = tuple(
        replace(
            track,
            title=f"{track.title} (2015 Remaster)",
            duration_ms=180_000 if number < 3 else track.duration_ms,
        )
        for number, track in enumerate(group.logical_tracks, start=1)
    )
    annotated_group = replace(
        group,
        files=tuple(track.path for track in local_tracks),
        logical_tracks=local_tracks,
    )
    candidate = catalog_album("Alpha")
    reordered_remote = (
        replace(candidate.tracks[0], title="Home Again", duration_ms=180_000),
        replace(candidate.tracks[1], title="First Light", duration_ms=180_000),
        candidate.tracks[2],
    )

    decision = choose_match(annotated_group, [replace(candidate, tracks=reordered_remote)])

    assert decision.status == "no_match"
    assert decision.scores[0].components["track_coverage"] < 0.85


def test_uniform_remaster_exception_rejects_near_identical_reordering() -> None:
    group = local_group()
    work = "String Quartet No. 14 in D Minor, D. 810 Death and the Maiden"
    local_titles = (
        f"{work}: I. Allegro (2015 Remaster)",
        f"{work}: II. Allegro (2015 Remaster)",
        f"{work}: III. Finale (2015 Remaster)",
    )
    local_tracks = tuple(
        replace(track, title=title, duration_ms=180_000)
        for track, title in zip(group.logical_tracks, local_titles, strict=True)
    )
    annotated_group = replace(
        group,
        files=tuple(track.path for track in local_tracks),
        logical_tracks=local_tracks,
    )
    candidate = catalog_album("Alpha")
    remote_titles = (
        f"{work}: II. Allegro",
        f"{work}: I. Allegro",
        f"{work}: III. Finale",
    )
    reordered_remote = tuple(
        replace(track, title=title, duration_ms=180_000)
        for track, title in zip(candidate.tracks, remote_titles, strict=True)
    )

    decision = choose_match(annotated_group, [replace(candidate, tracks=reordered_remote)])

    assert decision.status == "no_match"
    assert decision.scores[0].components["track_coverage"] < 0.85


def test_uniform_remaster_exception_never_strips_provider_explicit_remaster() -> None:
    group = local_group()
    candidate = catalog_album("Alpha")
    remastered_remote = tuple(
        replace(track, title=f"{track.title} (2015 Remaster)") for track in candidate.tracks
    )

    decision = choose_match(group, [replace(candidate, tracks=remastered_remote)])

    assert decision.status == "no_match"
    assert decision.scores[0].components["track_coverage"] == 0.0


def test_matching_artist_and_album_still_require_tracklist_evidence() -> None:
    candidate = CatalogAlbum(
        collection_id=2,
        album="Greatest Hits",
        artist="Alpha",
        release_year=2020,
        artwork_url="https://example.invalid/art.jpg",
        track_count=3,
        tracks=(
            CatalogTrack("Unrelated One", "Alpha", 130_000, 1, 1),
            CatalogTrack("Unrelated Two", "Alpha", 140_000, 1, 2),
            CatalogTrack("Unrelated Three", "Alpha", 150_000, 1, 3),
        ),
    )

    score = score_candidate(local_group(), candidate)

    assert score.eligible is False
    assert "tracklist mismatch" in score.reasons
    assert score.components["track_coverage"] == 0.0


def test_choose_match_uses_the_verified_tracklist_not_search_result_order() -> None:
    correct = catalog_album("Alpha", collection_id=22)
    wrong = catalog_album("Completely Different Artist", collection_id=11)

    decision = choose_match(local_group(), [wrong, correct])

    assert decision.status == "matched"
    assert decision.match is not None
    assert decision.match.candidate.collection_id == 22
    assert decision.match.components["track_coverage"] == 1.0


def test_choose_match_abstains_when_two_editions_are_indistinguishable() -> None:
    first = catalog_album("Alpha", collection_id=101)
    second = catalog_album("Alpha", collection_id=202)

    decision = choose_match(local_group(), [first, second])

    assert decision.status == "ambiguous"
    assert decision.match is None
    assert "margin" in decision.reason


def test_partial_tracklist_cannot_match_a_larger_deluxe_release() -> None:
    base = catalog_album("Alpha", collection_id=303)
    candidate = CatalogAlbum(
        collection_id=base.collection_id,
        album="Greatest Hits (Deluxe Edition)",
        artist=base.artist,
        release_year=base.release_year,
        artwork_url=base.artwork_url,
        track_count=5,
        tracks=(
            *base.tracks,
            CatalogTrack("Bonus One", "Alpha", 190_000, 1, 4),
            CatalogTrack("Bonus Two", "Alpha", 210_000, 1, 5),
        ),
    )

    score = score_candidate(local_group(), candidate)

    assert score.eligible is False
    assert "edition/version conflict" in score.reasons
    assert "tracklist coverage below 85%" in score.reasons


def test_five_second_duration_difference_is_not_a_strong_track_match() -> None:
    base = catalog_album("Alpha", collection_id=404)
    candidate = CatalogAlbum(
        collection_id=base.collection_id,
        album=base.album,
        artist=base.artist,
        release_year=base.release_year,
        artwork_url=base.artwork_url,
        track_count=base.track_count,
        tracks=(
            CatalogTrack("First Light", "Alpha", 185_000, 1, 1),
            *base.tracks[1:],
        ),
    )

    score = score_candidate(local_group(), candidate)

    assert score.eligible is False
    assert score.components["track_coverage"] < 0.85


def test_similar_but_different_track_title_is_not_counted_as_strong_evidence() -> None:
    base = catalog_album("Alpha", collection_id=505)
    candidate = CatalogAlbum(
        collection_id=base.collection_id,
        album=base.album,
        artist=base.artist,
        release_year=base.release_year,
        artwork_url=base.artwork_url,
        track_count=base.track_count,
        tracks=(
            CatalogTrack("First Light II", "Alpha", 180_100, 1, 1),
            *base.tracks[1:],
        ),
    )

    score = score_candidate(local_group(), candidate)

    assert score.eligible is False
    assert score.components["track_coverage"] < 0.85


def test_two_track_release_abstains_without_a_verified_identifier() -> None:
    full = local_group()
    tracks = full.logical_tracks[:2]
    short_group = AlbumGroup(
        album=full.album,
        album_artist=full.album_artist,
        year=full.year,
        files=tuple(track.path for track in tracks),
        logical_tracks=tracks,
    )
    candidate = CatalogAlbum(
        collection_id=606,
        album=full.album,
        artist=full.album_artist,
        release_year=full.year,
        artwork_url="https://example.invalid/art.jpg",
        track_count=2,
        tracks=tuple(
            CatalogTrack(
                track.title,
                track.artist,
                track.duration_ms,
                track.disc_number,
                track.track_number,
            )
            for track in tracks
        ),
    )

    decision = choose_match(short_group, [candidate])

    assert decision.status == "no_match"
    assert decision.scores
    assert "fewer than three strong tracks" in decision.scores[0].reasons


def test_declared_incomplete_local_album_is_never_auto_matched() -> None:
    full = local_group()
    incomplete_tracks = tuple(replace(track, track_total=5) for track in full.logical_tracks)
    incomplete = replace(
        full,
        files=tuple(track.path for track in incomplete_tracks),
        logical_tracks=incomplete_tracks,
    )

    score = score_candidate(incomplete, catalog_album("Alpha", collection_id=707))

    assert score.eligible is False
    assert "local tracklist appears incomplete" in score.reasons
