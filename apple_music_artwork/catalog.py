"""Apple Search/Lookup catalog client, caching, and response parsing."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import replace
from pathlib import Path

import requests

from .constants import ITUNES_LOOKUP_URL, ITUNES_SEARCH_URL, MAX_API_BYTES, USER_AGENT
from .filesystem import _atomic_write_bytes, _read_secure_file
from .matching import (
    _artists_equivalent,
    _barcodes_equivalent,
    _explicit_remaster_years_conflict,
    _musicbrainz_album_identity,
    _musicbrainz_search_artist_and_features_match,
    _musicbrainz_search_identity_matches,
    _normalize_barcode,
    _normalize_release_id,
    _split_trailing_remaster,
    _without_trailing_album_version,
    matching_basis,
    normalize_text,
    score_candidate,
    text_similarity,
)
from .metadata import _year
from .models import AlbumGroup, CatalogAlbum, CatalogTrack, MusicBrainzRelease
from .musicbrainz import MusicBrainzClient, _ResolvedMusicBrainzRelease
from .network import (
    _close_response,
    _read_bounded_body,
    _request_with_validated_redirects,
    _retry_delay,
    _validate_remote_url,
)


def _as_int(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def candidate_ids_from_album_search(
    rows: Iterable[Mapping[str, object]],
    artist: str,
    album: str,
    *,
    track_count: int | None = None,
    release_year: int | None = None,
    identifier_first: bool = False,
    limit: int = 12,
) -> list[int]:
    ranked: list[tuple[float, int]] = []
    for row in rows:
        collection_id = _as_int(row.get("collectionId"))
        if collection_id is None or not row.get("artworkUrl100"):
            continue
        row_album = str(row.get("collectionName") or "")
        row_artist = str(row.get("collectionArtistName") or row.get("artistName") or "")
        album_score = text_similarity(album, row_album)
        artist_score = text_similarity(artist, row_artist)
        exact_count = track_count is not None and _as_int(row.get("trackCount")) == track_count
        row_year = _year(str(row.get("releaseDate") or ""))
        compatible_year = not (
            release_year is not None and row_year is not None and abs(release_year - row_year) > 1
        )
        if identifier_first:
            if (
                not _musicbrainz_search_artist_and_features_match(
                    album,
                    artist,
                    row_album,
                    row_artist,
                )
                or _musicbrainz_album_identity(album) != _musicbrainz_album_identity(row_album)
                or _explicit_remaster_years_conflict(album, row_album)
            ):
                continue
        elif not _artists_equivalent(artist, row_artist) or (
            album_score < 0.62 and not exact_count
        ):
            continue
        ranked.append(
            (
                0.52 * album_score
                + 0.33 * artist_score
                + 0.10 * float(exact_count)
                + 0.05 * float(compatible_year),
                collection_id,
            )
        )
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return list(dict.fromkeys(collection_id for _, collection_id in ranked[:limit]))


def _album_search_diagnostics(
    rows: Iterable[Mapping[str, object]],
    selected_ids: Iterable[int],
    artist: str,
    album: str,
    *,
    track_count: int | None,
    identifier_first: bool,
) -> dict[str, object]:
    """Explain discovery filtering without retaining raw provider metadata."""
    materialized = tuple(rows)
    selected = set(selected_ids)
    by_collection: dict[int, Mapping[str, object]] = {}
    invalid_id_rows = 0
    for row in materialized:
        collection_id = _as_int(row.get("collectionId"))
        if collection_id is None or collection_id < 1:
            invalid_id_rows += 1
            continue
        by_collection[collection_id] = row

    rejection_reasons: dict[str, int] = {}

    def record(reason: str) -> None:
        rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1

    if invalid_id_rows:
        rejection_reasons["missing or invalid collection ID"] = invalid_id_rows
    for collection_id, row in by_collection.items():
        if collection_id in selected:
            continue
        if not row.get("artworkUrl100"):
            record("missing artwork URL")
            continue
        row_album = str(row.get("collectionName") or "")
        row_artist = str(row.get("collectionArtistName") or row.get("artistName") or "")
        if identifier_first:
            if not _musicbrainz_search_artist_and_features_match(
                album,
                artist,
                row_album,
                row_artist,
            ):
                record("resolved MusicBrainz artist or feature-credit mismatch")
            elif _musicbrainz_album_identity(album) != _musicbrainz_album_identity(row_album):
                record("resolved MusicBrainz album identity mismatch")
            elif _explicit_remaster_years_conflict(album, row_album):
                record("explicit remaster-year conflict")
            else:
                record("bounded search candidate limit")
            continue
        exact_count = track_count is not None and _as_int(row.get("trackCount")) == track_count
        if not _artists_equivalent(artist, row_artist):
            record("artist mismatch")
        elif text_similarity(album, row_album) < 0.62 and not exact_count:
            record("album similarity below discovery threshold")
        else:
            record("bounded search candidate limit")
    return {
        "raw_rows": len(materialized),
        "raw_collections": len(by_collection),
        "selected_collections": len(selected),
        "rejected_collections": len(set(by_collection) - selected),
        "rejection_reasons": rejection_reasons,
        "selected_collection_ids": sorted(selected),
    }


def _search_title_base(value: str) -> str:
    value = _without_trailing_album_version(value)
    remaster = _split_trailing_remaster(value)
    return remaster[0] if remaster is not None else value


def candidate_ids_from_song_search(
    rows: Iterable[Mapping[str, object]],
    *,
    artist: str,
    album: str,
    title: str,
) -> list[int]:
    comparison_title = _search_title_base(title)
    ranked: list[tuple[float, int]] = []
    for row in rows:
        collection_id = _as_int(row.get("collectionId"))
        if collection_id is None or row.get("kind") != "song":
            continue
        title_score = text_similarity(
            comparison_title,
            _search_title_base(str(row.get("trackName") or "")),
        )
        row_artist = str(row.get("artistName") or "")
        artist_score = text_similarity(artist, row_artist)
        album_score = text_similarity(album, str(row.get("collectionName") or ""))
        exact_title = title_score == 1.0
        if (
            title_score < 0.78
            or not _artists_equivalent(artist, row_artist)
            or (album_score < 0.50 and not exact_title)
        ):
            continue
        ranked.append(
            (0.46 * title_score + 0.34 * artist_score + 0.20 * album_score, collection_id)
        )
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return list(dict.fromkeys(collection_id for _, collection_id in ranked[:8]))


def catalog_albums_from_lookup(rows: Iterable[Mapping[str, object]]) -> list[CatalogAlbum]:
    """Convert complete iTunes collection+song rows into verified album tracklists."""
    collection_rows: dict[int, Mapping[str, object]] = {}
    track_rows: dict[int, list[Mapping[str, object]]] = defaultdict(list)
    for row in rows:
        collection_id = _as_int(row.get("collectionId"))
        if collection_id is None or collection_id < 1:
            continue
        if row.get("wrapperType") == "collection":
            collection_rows[collection_id] = row
        elif row.get("wrapperType") == "track" and row.get("kind") == "song":
            track_rows[collection_id].append(row)

    albums: list[CatalogAlbum] = []
    for collection_id, collection in collection_rows.items():
        tracks_data = track_rows.get(collection_id, [])
        declared_count = _as_int(collection.get("trackCount"))
        if declared_count is None or declared_count < 1 or not tracks_data:
            continue
        seen_positions: set[tuple[int, int]] = set()
        positions_by_disc: dict[int, set[int]] = defaultdict(set)
        tracks: list[CatalogTrack] = []
        valid = True
        for row in tracks_data:
            title = str(row.get("trackName") or "").strip()
            artist = str(row.get("artistName") or "").strip()
            disc = _as_int(row.get("discNumber"))
            number = _as_int(row.get("trackNumber"))
            if not title or not artist or disc is None or disc < 1 or number is None or number < 1:
                valid = False
                break
            position = (disc, number)
            if position in seen_positions:
                valid = False
                break
            seen_positions.add(position)
            positions_by_disc[disc].add(number)
            tracks.append(
                CatalogTrack(
                    title=title,
                    artist=artist,
                    duration_ms=_as_int(row.get("trackTimeMillis")),
                    disc_number=disc,
                    track_number=number,
                )
            )
        if not valid or set(positions_by_disc) != set(
            range(1, max(positions_by_disc, default=0) + 1)
        ):
            continue
        if any(
            numbers != set(range(1, max(numbers) + 1)) for numbers in positions_by_disc.values()
        ):
            continue
        effective_count = declared_count
        if len(tracks_data) != declared_count:
            # Apple's collection-level ``trackCount`` can include a music video even
            # when an ``entity=song`` lookup returned the complete song tracklist.
            # In that case every song row independently declares the complete size of
            # its disc, which lets us prove completeness without trusting the mixed-
            # media collection count.
            counts_by_disc: dict[int, set[int]] = defaultdict(set)
            row_counts_complete = True
            for row in tracks_data:
                disc = _as_int(row.get("discNumber"))
                row_count = _as_int(row.get("trackCount"))
                if disc is None or row_count is None or row_count < 1:
                    row_counts_complete = False
                    break
                counts_by_disc[disc].add(row_count)
            if not row_counts_complete or any(
                len(counts_by_disc.get(disc, set())) != 1
                or next(iter(counts_by_disc[disc])) != len(numbers)
                for disc, numbers in positions_by_disc.items()
            ):
                continue
            effective_count = len(tracks_data)
        album_name = str(collection.get("collectionName") or "").strip()
        album_artist = str(
            collection.get("collectionArtistName") or collection.get("artistName") or ""
        ).strip()
        artwork_url = str(collection.get("artworkUrl100") or "").strip()
        if not album_name or not album_artist or not artwork_url:
            continue
        tracks.sort(
            key=lambda track: (track.disc_number or 0, track.track_number or 0, track.title)
        )
        albums.append(
            CatalogAlbum(
                collection_id=collection_id,
                album=album_name,
                artist=album_artist,
                release_year=_year(str(collection.get("releaseDate") or "")),
                artwork_url=artwork_url,
                track_count=effective_count,
                tracks=tuple(tracks),
            )
        )
    return sorted(albums, key=lambda album: album.collection_id)


def _identifier_albums_from_lookup(
    rows: Iterable[Mapping[str, object]],
) -> tuple[list[CatalogAlbum], tuple[str, ...]]:
    """Retain artwork-bearing collections reached by an exact external identifier.

    Strict catalog matching still requires a reconstructable Apple song tracklist.
    An exact UPC, MusicBrainz Apple relationship, or MusicBrainz barcode is different:
    the collection relationship itself establishes identity, so provider omissions and
    topology presentation must remain auditable warnings instead of erasing the cover.
    """

    materialized = tuple(rows)
    strict_by_id = {
        album.collection_id: album for album in catalog_albums_from_lookup(materialized)
    }
    collection_rows: dict[int, Mapping[str, object]] = {}
    song_rows: dict[int, list[Mapping[str, object]]] = defaultdict(list)
    for row in materialized:
        collection_id = _as_int(row.get("collectionId"))
        if collection_id is None or collection_id < 1:
            continue
        if row.get("wrapperType") == "collection":
            collection_rows[collection_id] = row
        elif row.get("wrapperType") == "track" and row.get("kind") == "song":
            song_rows[collection_id].append(row)

    albums: list[CatalogAlbum] = []
    diagnostics: list[str] = []
    for collection_id in sorted(collection_rows):
        collection = collection_rows[collection_id]
        album_name = str(collection.get("collectionName") or "").strip()
        album_artist = str(
            collection.get("collectionArtistName") or collection.get("artistName") or ""
        ).strip()
        artwork_url = str(collection.get("artworkUrl100") or "").strip()
        if not album_name or not album_artist or not artwork_url:
            continue

        tracks_data = song_rows.get(collection_id, [])
        declared_count = _as_int(collection.get("trackCount"))
        strict = strict_by_id.get(collection_id)
        if strict is not None:
            albums.append(strict)
            if declared_count != len(tracks_data):
                diagnostics.append(
                    f"Apple collection {collection_id} declared {declared_count!r} catalog "
                    f"items but returned {len(tracks_data)} complete song rows; direct "
                    "identifier evidence retained the artwork-bearing collection"
                )
            continue

        tracks: list[CatalogTrack] = []
        seen_positions: set[tuple[int, int]] = set()
        positions_by_disc: dict[int, set[int]] = defaultdict(set)
        valid_tracks = True
        for row in tracks_data:
            title = str(row.get("trackName") or "").strip()
            artist = str(row.get("artistName") or "").strip()
            disc = _as_int(row.get("discNumber"))
            number = _as_int(row.get("trackNumber"))
            if not title or not artist or disc is None or disc < 1 or number is None or number < 1:
                valid_tracks = False
                break
            position = (disc, number)
            if position in seen_positions:
                valid_tracks = False
                break
            seen_positions.add(position)
            positions_by_disc[disc].add(number)
            tracks.append(
                CatalogTrack(
                    title=title,
                    artist=artist,
                    duration_ms=_as_int(row.get("trackTimeMillis")),
                    disc_number=disc,
                    track_number=number,
                )
            )

        if not valid_tracks:
            tracks = []
            diagnostics.append(
                f"Apple collection {collection_id} returned malformed or duplicate song rows; "
                "direct identifier evidence retained only the artwork-bearing collection"
            )
        elif not tracks:
            diagnostics.append(
                f"Apple collection {collection_id} returned no song rows; direct identifier "
                "evidence retained the artwork-bearing collection"
            )
        else:
            contiguous_discs = set(positions_by_disc) == set(
                range(1, max(positions_by_disc, default=0) + 1)
            )
            contiguous_tracks = all(
                numbers == set(range(1, max(numbers) + 1)) for numbers in positions_by_disc.values()
            )
            if not contiguous_discs or not contiguous_tracks:
                diagnostics.append(
                    f"Apple collection {collection_id} returned non-contiguous disc/track "
                    "topology; direct identifier evidence retained the provider presentation"
                )
            elif declared_count != len(tracks):
                diagnostics.append(
                    f"Apple collection {collection_id} declared {declared_count!r} catalog "
                    f"items but returned {len(tracks)} song rows; direct identifier evidence "
                    "retained the artwork-bearing collection"
                )

        tracks.sort(
            key=lambda track: (
                track.disc_number or 0,
                track.track_number or 0,
                track.title,
            )
        )
        # Preserve Apple's declaration when strict parsing could not prove that the
        # returned song rows are complete.  Matching can then treat the disagreement
        # as incomplete provider presentation instead of mistaking a partial row set
        # for an exact release-size fingerprint.
        effective_count = (
            declared_count if declared_count is not None and declared_count > 0 else None
        )
        albums.append(
            CatalogAlbum(
                collection_id=collection_id,
                album=album_name,
                artist=album_artist,
                release_year=_year(str(collection.get("releaseDate") or "")),
                artwork_url=artwork_url,
                track_count=effective_count,
                tracks=tuple(tracks),
            )
        )
    return albums, tuple(dict.fromkeys(diagnostics))


def _lookup_response_diagnostics(
    rows: Iterable[Mapping[str, object]],
    albums: Iterable[CatalogAlbum],
    *,
    identifier_authoritative: bool,
    requested_collection_ids: Iterable[int] | None = None,
    parser_warnings: Iterable[str] = (),
) -> dict[str, object]:
    """Summarize one lookup path using only JSON-serializable values."""
    materialized = tuple(rows)
    parsed_ids = {album.collection_id for album in albums}
    collection_rows = [row for row in materialized if row.get("wrapperType") == "collection"]
    song_rows = [
        row
        for row in materialized
        if row.get("wrapperType") == "track" and row.get("kind") == "song"
    ]
    response_collection_ids = {
        collection_id
        for row in collection_rows
        if (collection_id := _as_int(row.get("collectionId"))) is not None and collection_id > 0
    }
    requested_ids = (
        set(requested_collection_ids)
        if requested_collection_ids is not None
        else set(response_collection_ids)
    )
    rejected_ids = requested_ids - parsed_ids
    valid_identity_ids = {
        collection_id
        for row in collection_rows
        if (collection_id := _as_int(row.get("collectionId"))) is not None
        and collection_id > 0
        and str(row.get("collectionName") or "").strip()
        and str(row.get("collectionArtistName") or row.get("artistName") or "").strip()
        and str(row.get("artworkUrl100") or "").strip()
    }
    rejection_reasons: dict[str, int] = {}

    def record(reason: str, count: int) -> None:
        if count:
            rejection_reasons[reason] = count

    record(
        "requested collection absent from Apple response",
        len(requested_ids - response_collection_ids),
    )
    record(
        "collection missing title, artist, or artwork",
        len((requested_ids & response_collection_ids) - valid_identity_ids),
    )
    structurally_rejected = rejected_ids & valid_identity_ids
    if identifier_authoritative:
        record(
            "identifier response lacked a retainable collection row",
            len(structurally_rejected),
        )
    else:
        record(
            "strict complete-song-tracklist validation",
            len(structurally_rejected),
        )
    song_collection_ids = {
        collection_id
        for row in song_rows
        if (collection_id := _as_int(row.get("collectionId"))) is not None and collection_id > 0
    }
    record(
        "song rows without a collection row",
        len(song_collection_ids - response_collection_ids),
    )
    return {
        "raw_rows": len(materialized),
        "collection_rows": len(collection_rows),
        "song_rows": len(song_rows),
        "requested_collections": len(requested_ids),
        "parsed_collections": len(parsed_ids),
        "rejected_collections": len(rejected_ids),
        "rejection_reasons": rejection_reasons,
        "parser_warnings": list(dict.fromkeys(parser_warnings)),
    }


class AppleCatalogClient:
    """Polite, cached client for Apple's public iTunes Search/Lookup API."""

    def __init__(
        self,
        *,
        country: str = "US",
        cache_dir: Path = Path(".apple-artwork-cache"),
        session: object | None = None,
        timeout: float = 20.0,
        api_interval: float = 3.1,
        max_retries: int = 4,
        cache_ttl_days: int = 30,
        max_response_bytes: int = MAX_API_BYTES,
        musicbrainz_client: object | None = None,
    ) -> None:
        country = country.upper()
        if not re.fullmatch(r"[A-Z]{2}", country):
            raise ValueError("country must be a two-letter storefront code")
        self.country = country
        self.cache_dir = cache_dir / "api"
        self.musicbrainz_client = musicbrainz_client or MusicBrainzClient(cache_dir=cache_dir)
        self.session = session or requests.Session()
        headers = getattr(self.session, "headers", None)
        if headers is not None:
            headers.update(
                {
                    "Accept": "application/json",
                    "User-Agent": USER_AGENT,
                }
            )
        self.timeout = max(1.0, float(timeout))
        self.api_interval = max(0.0, api_interval)
        self.max_retries = max(1, max_retries)
        self.cache_ttl_seconds = max(0, cache_ttl_days) * 86_400
        self.max_response_bytes = max(1, min(int(max_response_bytes), MAX_API_BYTES))
        self._last_request = 0.0
        self.last_identifier_warnings: tuple[str, ...] = ()
        self.last_discovery_diagnostics: dict[str, object] = {}

    @staticmethod
    def _cache_key(url: str, params: Mapping[str, object]) -> str:
        canonical = json.dumps(
            [url, sorted(params.items())],
            ensure_ascii=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("ascii")).hexdigest()

    def _cache_path(self, url: str, params: Mapping[str, object]) -> Path:
        return self.cache_dir / f"{self._cache_key(url, params)}.json"

    def _read_cache(self, path: Path, *, expected_key: str) -> list[Mapping[str, object]] | None:
        try:
            info = path.lstat()
            if time.time() - info.st_mtime > self.cache_ttl_seconds:
                return None
            envelope = json.loads(_read_secure_file(path, self.max_response_bytes).decode("utf-8"))
            if (
                not isinstance(envelope, dict)
                or envelope.get("schema_version") != 1
                or envelope.get("cache_key") != expected_key
            ):
                return None
            payload = envelope.get("response")
            results = payload.get("results") if isinstance(payload, dict) else None
            if not isinstance(results, list) or not all(isinstance(row, dict) for row in results):
                return None
            return results
        except (OSError, ValueError, UnicodeError, TypeError):
            return None

    def _write_cache(
        self,
        path: Path,
        payload: Mapping[str, object],
        *,
        cache_key: str,
    ) -> None:
        envelope = {
            "schema_version": 1,
            "cache_key": cache_key,
            "response": payload,
        }
        encoded = json.dumps(envelope, ensure_ascii=True, sort_keys=True).encode("ascii")
        if len(encoded) > self.max_response_bytes:
            raise ValueError("Apple JSON cache payload exceeds the configured limit")
        _atomic_write_bytes(path, encoded)

    def _pace(self) -> None:
        remaining = self.api_interval - (time.monotonic() - self._last_request)
        if remaining > 0:
            time.sleep(remaining)

    def _request_results(self, url: str, params: dict[str, object]) -> list[Mapping[str, object]]:
        _validate_remote_url(url, api=True)
        cache_key = self._cache_key(url, params)
        cache_path = self._cache_path(url, params)
        cached = self._read_cache(cache_path, expected_key=cache_key)
        if cached is not None:
            return cached

        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            self._pace()
            response: object | None = None
            try:
                response, _final_url = _request_with_validated_redirects(
                    self.session,
                    url,
                    params=params,
                    timeout=self.timeout,
                    api=True,
                )
                self._last_request = time.monotonic()
                status = int(getattr(response, "status_code", 0))
                if status == 200:
                    content_type = (
                        str(getattr(response, "headers", {}).get("Content-Type") or "")
                        .split(";", 1)[0]
                        .strip()
                        .casefold()
                    )
                    if content_type not in {
                        "application/json",
                        "text/json",
                        "application/javascript",
                        "text/javascript",
                    }:
                        raise ValueError(
                            f"Apple API returned unsupported Content-Type {content_type!r}"
                        )
                    body = _read_bounded_body(
                        response,
                        maximum=self.max_response_bytes,
                        timeout=self.timeout,
                    )
                    payload = json.loads(body.decode("utf-8"))
                    if not isinstance(payload, dict) or not isinstance(
                        payload.get("results"), list
                    ):
                        raise ValueError("Apple returned malformed JSON")
                    if not all(isinstance(row, dict) for row in payload["results"]):
                        raise ValueError("Apple returned malformed JSON rows")
                    self._write_cache(cache_path, payload, cache_key=cache_key)
                    return payload["results"]
                if status not in {403, 429} and status < 500:
                    response.raise_for_status()  # type: ignore[attr-defined]
                last_error = requests.RequestException(f"Apple API returned HTTP {status}")
                if attempt + 1 < self.max_retries:
                    retry_after = getattr(response, "headers", {}).get("Retry-After")
                    time.sleep(_retry_delay(retry_after, attempt))
            except (
                requests.RequestException,
                OSError,
                TimeoutError,
                UnicodeError,
                ValueError,
            ) as exc:
                last_error = exc
                if attempt + 1 < self.max_retries:
                    time.sleep(_retry_delay(None, attempt))
            finally:
                if response is not None:
                    _close_response(response)
        if last_error:
            raise last_error
        raise requests.RequestException("Apple API request failed after retries")

    def _lookup_collection_ids_with_parser(
        self,
        collection_ids: Iterable[int],
        *,
        identifier_authoritative: bool,
    ) -> tuple[list[CatalogAlbum], tuple[str, ...], dict[str, object]]:
        ordered_ids = list(dict.fromkeys(collection_ids))[:24]
        albums: list[CatalogAlbum] = []
        diagnostics: list[str] = []
        raw_rows: list[Mapping[str, object]] = []
        request_count = 0
        unrequested_rows = 0

        def parse(
            rows: Iterable[Mapping[str, object]],
        ) -> tuple[list[CatalogAlbum], tuple[str, ...]]:
            if identifier_authoritative:
                return _identifier_albums_from_lookup(rows)
            return catalog_albums_from_lookup(rows), ()

        for start in range(0, len(ordered_ids), 8):
            chunk = ordered_ids[start : start + 8]
            request_count += 1
            lookup_rows = self._request_results(
                ITUNES_LOOKUP_URL,
                {
                    "id": ",".join(str(collection_id) for collection_id in chunk),
                    "country": self.country,
                    "entity": "song",
                    "limit": 200,
                },
            )
            raw_rows.extend(lookup_rows)
            chunk_ids = set(chunk)
            requested_rows = [
                row for row in lookup_rows if _as_int(row.get("collectionId")) in chunk_ids
            ]
            unrequested_rows += len(lookup_rows) - len(requested_rows)
            parsed, parsed_diagnostics = parse(requested_rows)
            parsed = [album for album in parsed if album.collection_id in chunk_ids]
            diagnostics.extend(parsed_diagnostics)
            albums.extend(parsed)
            returned = {album.collection_id for album in parsed}
            for missing_id in (
                collection_id for collection_id in chunk if collection_id not in returned
            ):
                request_count += 1
                individual_rows = self._request_results(
                    ITUNES_LOOKUP_URL,
                    {
                        "id": str(missing_id),
                        "country": self.country,
                        "entity": "song",
                        "limit": 200,
                    },
                )
                raw_rows.extend(individual_rows)
                requested_individual_rows = [
                    row for row in individual_rows if _as_int(row.get("collectionId")) == missing_id
                ]
                unrequested_rows += len(individual_rows) - len(requested_individual_rows)
                individual_albums, individual_diagnostics = parse(requested_individual_rows)
                diagnostics.extend(individual_diagnostics)
                albums.extend(
                    album for album in individual_albums if album.collection_id == missing_id
                )
        unique_albums = list({album.collection_id: album for album in albums}.values())
        unique_diagnostics = tuple(dict.fromkeys(diagnostics))
        lookup_diagnostics = _lookup_response_diagnostics(
            raw_rows,
            unique_albums,
            identifier_authoritative=identifier_authoritative,
            requested_collection_ids=ordered_ids,
            parser_warnings=unique_diagnostics,
        )
        lookup_diagnostics["requests"] = request_count
        lookup_diagnostics["unrequested_rows"] = unrequested_rows
        return unique_albums, unique_diagnostics, lookup_diagnostics

    def _lookup_collection_ids(self, collection_ids: Iterable[int]) -> list[CatalogAlbum]:
        albums, _diagnostics, _lookup_diagnostics = self._lookup_collection_ids_with_parser(
            collection_ids,
            identifier_authoritative=False,
        )
        return albums

    def _lookup_identifier_collection_ids(
        self,
        collection_ids: Iterable[int],
    ) -> tuple[list[CatalogAlbum], tuple[str, ...], dict[str, object]]:
        return self._lookup_collection_ids_with_parser(
            collection_ids,
            identifier_authoritative=True,
        )

    def _song_fallback_ids(self, group: AlbumGroup) -> list[int]:
        ranked_tracks = sorted(
            group.logical_tracks,
            key=lambda track: (len(normalize_text(track.title)), track.duration_ms or 0),
            reverse=True,
        )
        anchors = ranked_tracks[:1]
        if group.logical_tracks and group.logical_tracks[0] not in anchors:
            anchors.append(group.logical_tracks[0])
        fallback_ids: list[int] = []
        for anchor in anchors:
            song_rows = self._request_results(
                ITUNES_SEARCH_URL,
                {
                    "term": f"{group.album_artist} {_search_title_base(anchor.title)}",
                    "country": self.country,
                    "media": "music",
                    "entity": "song",
                    "limit": 25,
                },
            )
            fallback_ids.extend(
                candidate_ids_from_song_search(
                    song_rows,
                    artist=group.album_artist,
                    album=group.album,
                    title=anchor.title,
                )
            )
        return list(dict.fromkeys(fallback_ids))[:8]

    def find_candidates(self, group: AlbumGroup) -> list[CatalogAlbum]:
        warnings: list[str] = list(group.identifier_warnings)
        self.last_identifier_warnings = ()
        discovery: dict[str, object] = {
            "match_basis": matching_basis(group),
            "resolved_musicbrainz": None,
            "stages": {},
        }
        self.last_discovery_diagnostics = discovery

        def finish(albums: list[CatalogAlbum]) -> list[CatalogAlbum]:
            self.last_identifier_warnings = tuple(warnings)
            discovery["candidate_count"] = len(albums)
            discovery["warning_count"] = len(self.last_identifier_warnings)
            self.last_discovery_diagnostics = discovery
            return albums

        if group.identifier_conflicts:
            warnings.extend(group.identifier_conflicts)
            return finish([])

        local_barcode = _normalize_barcode(group.barcode)
        release_id = _normalize_release_id(group.musicbrainz_release_id)
        upc_albums: list[CatalogAlbum] = []
        if local_barcode:
            upc_rows = self._request_results(
                ITUNES_LOOKUP_URL,
                {
                    "upc": local_barcode,
                    "country": self.country,
                    "entity": "song",
                    "limit": 200,
                },
            )
            parsed_upc_albums, upc_diagnostics = _identifier_albums_from_lookup(upc_rows)
            stages = discovery["stages"]
            assert isinstance(stages, dict)
            stages["embedded_upc"] = _lookup_response_diagnostics(
                upc_rows,
                parsed_upc_albums,
                identifier_authoritative=True,
                parser_warnings=upc_diagnostics,
            )
            warnings.extend(f"embedded UPC: {warning}" for warning in upc_diagnostics)
            upc_albums = [
                replace(
                    album,
                    verified_barcode=local_barcode,
                    identifier_resolution="embedded_upc",
                )
                for album in parsed_upc_albums
            ]
            if upc_albums and release_id is None:
                return finish(upc_albums)
            if not upc_albums:
                warnings.append(
                    "the embedded UPC returned no usable artwork-bearing Apple collection"
                )
            if not upc_albums and release_id is None:
                return finish([])

        resolution = None
        if release_id is not None:
            discovery["resolved_musicbrainz"] = {
                "status": "pending",
                "requested_release_id": release_id,
            }
            try:
                resolved = self.musicbrainz_client.resolve(release_id)  # type: ignore[attr-defined]
            except Exception:
                discovery["resolved_musicbrainz"] = {
                    "status": "lookup_failed",
                    "requested_release_id": release_id,
                }
                if upc_albums:
                    warnings.append(
                        "the MusicBrainz release lookup failed; the exact UPC match was "
                        "retained but the release MBID could not be cross-validated"
                    )
                    return finish(upc_albums)
                warnings.append(
                    "the MusicBrainz release lookup failed; no unverified Apple candidate "
                    "was trusted"
                )
                return finish([])
            if resolved is None:
                discovery["resolved_musicbrainz"] = {
                    "status": "unresolved",
                    "requested_release_id": release_id,
                }
                if upc_albums:
                    warnings.append(
                        "MusicBrainz did not resolve the embedded release MBID; the exact "
                        "UPC match was retained but the MBID could not be cross-validated"
                    )
                    return finish(upc_albums)
                warnings.append(
                    "MusicBrainz did not resolve the embedded release MBID; no unverified "
                    "Apple candidate was trusted"
                )
                return finish([])
            if isinstance(resolved, _ResolvedMusicBrainzRelease):
                if type(self.musicbrainz_client) is not MusicBrainzClient:
                    discovery["resolved_musicbrainz"] = {
                        "status": "untrusted_alias_provenance",
                        "requested_release_id": release_id,
                    }
                    warnings.append(
                        "a custom MusicBrainz resolver cannot assert merged-release "
                        "alias provenance"
                    )
                    return finish([])
                if _normalize_release_id(resolved.requested_release_id) != release_id:
                    discovery["resolved_musicbrainz"] = {
                        "status": "requested_release_mismatch",
                        "requested_release_id": release_id,
                    }
                    warnings.append(
                        "MusicBrainz returned resolution evidence for a different release MBID"
                    )
                    return finish([])
                resolution = resolved.release
                alias_provenance = True
            elif isinstance(resolved, MusicBrainzRelease):
                resolution = resolved
                alias_provenance = False
            else:
                discovery["resolved_musicbrainz"] = {
                    "status": "malformed",
                    "requested_release_id": release_id,
                }
                warnings.append("MusicBrainz returned malformed release resolution evidence")
                return finish([])
            resolved_release_id = _normalize_release_id(resolution.release_id)
            if resolved_release_id is None:
                discovery["resolved_musicbrainz"] = {
                    "status": "invalid_release_id",
                    "requested_release_id": release_id,
                }
                warnings.append(
                    "MusicBrainz returned an invalid release MBID for the embedded identifier"
                )
                return finish([])
            if resolved_release_id != release_id:
                if not alias_provenance:
                    discovery["resolved_musicbrainz"] = {
                        "status": "unsolicited_release_redirect",
                        "requested_release_id": release_id,
                        "resolved_release_id": resolved_release_id,
                    }
                    warnings.append(
                        "MusicBrainz returned a different release MBID than the embedded identifier"
                    )
                    return finish([])
                warnings.append(
                    "MusicBrainz resolved the embedded release MBID to canonical release MBID "
                    f"{resolved_release_id}"
                )
            if (
                not isinstance(resolution.title, str)
                or not resolution.title.strip()
                or not isinstance(resolution.artist, str)
                or not resolution.artist.strip()
                or not isinstance(resolution.recording_ids, tuple)
                or not isinstance(resolution.apple_collection_ids, tuple)
                or (resolution.barcode is not None and not isinstance(resolution.barcode, str))
                or (
                    resolution.release_year is not None
                    and (
                        not isinstance(resolution.release_year, int)
                        or isinstance(resolution.release_year, bool)
                        or not 1800 <= resolution.release_year <= 2199
                    )
                )
                or (
                    resolution.track_count is not None
                    and (
                        not isinstance(resolution.track_count, int)
                        or isinstance(resolution.track_count, bool)
                        or not 0 < resolution.track_count <= 10_000
                    )
                )
            ):
                discovery["resolved_musicbrainz"] = {
                    "status": "malformed",
                    "requested_release_id": release_id,
                    "resolved_release_id": resolved_release_id,
                }
                warnings.append("MusicBrainz returned malformed release resolution evidence")
                return finish([])
            normalized_barcode = _normalize_barcode(resolution.barcode)
            normalized_recording_ids = tuple(
                _normalize_release_id(raw_recording_id)
                for raw_recording_id in resolution.recording_ids
            )
            collection_ids_are_valid = all(
                isinstance(raw_collection_id, int)
                and not isinstance(raw_collection_id, bool)
                and raw_collection_id > 0
                for raw_collection_id in resolution.apple_collection_ids
            )
            if (
                (resolution.barcode is not None and normalized_barcode is None)
                or any(recording_id is None for recording_id in normalized_recording_ids)
                or not collection_ids_are_valid
            ):
                discovery["resolved_musicbrainz"] = {
                    "status": "malformed",
                    "requested_release_id": release_id,
                    "resolved_release_id": resolved_release_id,
                }
                warnings.append("MusicBrainz returned malformed release resolution evidence")
                return finish([])
            normalized_collection_ids = tuple(sorted(set(resolution.apple_collection_ids)))
            resolution = MusicBrainzRelease(
                release_id=resolved_release_id,
                title=str(resolution.title).strip(),
                artist=str(resolution.artist).strip(),
                release_year=(
                    resolution.release_year
                    if isinstance(resolution.release_year, int)
                    and not isinstance(resolution.release_year, bool)
                    and 1800 <= resolution.release_year <= 2199
                    else None
                ),
                track_count=(
                    resolution.track_count
                    if isinstance(resolution.track_count, int)
                    and not isinstance(resolution.track_count, bool)
                    and 0 < resolution.track_count <= 10_000
                    else None
                ),
                barcode=normalized_barcode,
                apple_collection_ids=normalized_collection_ids,
                recording_ids=tuple(
                    recording_id
                    for recording_id in normalized_recording_ids
                    if recording_id is not None
                ),
            )
            discovery["resolved_musicbrainz"] = {
                "status": "resolved",
                "requested_release_id": release_id,
                "resolved_release_id": resolution.release_id,
                "canonical_alias": resolution.release_id != release_id,
                "title": resolution.title,
                "artist": resolution.artist,
                "track_count": resolution.track_count,
                "release_year": resolution.release_year,
                "barcode": resolution.barcode,
                "apple_collection_ids": list(resolution.apple_collection_ids),
                "recording_id_count": len(resolution.recording_ids),
            }
        if (
            resolution is not None
            and local_barcode is not None
            and resolution.barcode is not None
            and not _barcodes_equivalent(local_barcode, resolution.barcode)
        ):
            warnings.append(
                "the embedded UPC conflicts with the resolved MusicBrainz release barcode"
            )
            return finish([])

        recordings_verified = False
        if resolution is not None and resolution.recording_ids:
            local_recording_ids = tuple(
                recording_id
                for track in group.logical_tracks
                if (recording_id := _normalize_release_id(track.musicbrainz_recording_id))
                is not None
            )
            if local_recording_ids:
                local_recordings = Counter(local_recording_ids)
                resolved_recordings = Counter(resolution.recording_ids)
                if any(
                    count > resolved_recordings[recording_id]
                    for recording_id, count in local_recordings.items()
                ):
                    warnings.append(
                        "the embedded recording MBIDs could not be verified against the "
                        "resolved MusicBrainz release (including possible merged aliases)"
                    )
                    return finish([])
                recordings_verified = group.musicbrainz_provenance_complete
        if upc_albums and resolution is not None:
            upc_ids = {album.collection_id for album in upc_albums}
            related_ids = set(resolution.apple_collection_ids)
            barcode_cross_validated = bool(
                resolution.barcode
                and local_barcode
                and _barcodes_equivalent(resolution.barcode, local_barcode)
            )
            relations_disjoint = bool(related_ids and upc_ids.isdisjoint(related_ids))
            if relations_disjoint and not barcode_cross_validated:
                warnings.append(
                    "the embedded UPC and MusicBrainz release point to different Apple collections"
                )
                return finish([])
            if relations_disjoint:
                warnings.append(
                    "the MusicBrainz Apple relationship points to a different storefront "
                    "collection; the barcode-consistent exact UPC result was retained"
                )
            elif related_ids:
                upc_albums = [album for album in upc_albums if album.collection_id in related_ids]
            if resolution.barcode is None and not related_ids:
                warnings.append(
                    "the MusicBrainz release supplied no direct Apple or barcode crosswalk; "
                    "the exact UPC match was retained"
                )
            cross_validated = bool(related_ids or barcode_cross_validated)
            return finish(
                [
                    replace(
                        album,
                        verified_musicbrainz_release_id=(release_id if cross_validated else None),
                        musicbrainz_recordings_verified=(
                            recordings_verified if cross_validated else False
                        ),
                    )
                    for album in upc_albums
                ]
            )
        if resolution is not None and resolution.apple_collection_ids:
            related, relation_diagnostics, relation_lookup_diagnostics = (
                self._lookup_identifier_collection_ids(resolution.apple_collection_ids)
            )
            stages = discovery["stages"]
            assert isinstance(stages, dict)
            stages["musicbrainz_apple_relation"] = relation_lookup_diagnostics
            warnings.extend(
                f"MusicBrainz Apple relationship: {warning}" for warning in relation_diagnostics
            )
            if related:
                return finish(
                    [
                        replace(
                            album,
                            verified_musicbrainz_release_id=release_id,
                            identifier_resolution="musicbrainz_apple_relation",
                            musicbrainz_recordings_verified=recordings_verified,
                        )
                        for album in related
                    ]
                )
            warnings.append(
                "MusicBrainz Apple relationship returned no usable artwork-bearing Apple collection"
            )
        if resolution is not None and resolution.barcode:
            resolved_upc_rows = self._request_results(
                ITUNES_LOOKUP_URL,
                {
                    "upc": resolution.barcode,
                    "country": self.country,
                    "entity": "song",
                    "limit": 200,
                },
            )
            parsed_resolved_upc_albums, barcode_diagnostics = _identifier_albums_from_lookup(
                resolved_upc_rows
            )
            stages = discovery["stages"]
            assert isinstance(stages, dict)
            stages["musicbrainz_barcode"] = _lookup_response_diagnostics(
                resolved_upc_rows,
                parsed_resolved_upc_albums,
                identifier_authoritative=True,
                parser_warnings=barcode_diagnostics,
            )
            warnings.extend(f"MusicBrainz barcode: {warning}" for warning in barcode_diagnostics)
            resolved_upc_albums = [
                replace(
                    album,
                    verified_barcode=resolution.barcode,
                    verified_musicbrainz_release_id=release_id,
                    identifier_resolution="musicbrainz_barcode",
                    musicbrainz_recordings_verified=recordings_verified,
                )
                for album in parsed_resolved_upc_albums
            ]
            if resolved_upc_albums:
                return finish(resolved_upc_albums)
            warnings.append(
                "the MusicBrainz barcode returned no usable artwork-bearing Apple collection"
            )

        search_artist = resolution.artist if resolution is not None else group.album_artist
        search_album = resolution.title if resolution is not None else group.album
        search_count = (
            resolution.track_count
            if resolution is not None and resolution.track_count is not None
            else len(group.logical_tracks)
        )
        search_rows = self._request_results(
            ITUNES_SEARCH_URL,
            {
                "term": f"{search_artist} {search_album}",
                "country": self.country,
                "media": "music",
                "entity": "album",
                "limit": 50,
            },
        )
        collection_ids = candidate_ids_from_album_search(
            search_rows,
            search_artist,
            search_album,
            track_count=search_count,
            release_year=resolution.release_year if resolution is not None else group.year,
            identifier_first=matching_basis(group) != "legacy",
        )
        search_diagnostics = _album_search_diagnostics(
            search_rows,
            collection_ids,
            search_artist,
            search_album,
            track_count=search_count,
            identifier_first=matching_basis(group) != "legacy",
        )
        if collection_ids:
            albums, _lookup_warnings, lookup_diagnostics = self._lookup_collection_ids_with_parser(
                collection_ids,
                identifier_authoritative=False,
            )
        else:
            albums = []
            lookup_diagnostics = _lookup_response_diagnostics(
                (),
                (),
                identifier_authoritative=False,
                requested_collection_ids=(),
            )
            lookup_diagnostics["requests"] = 0
            lookup_diagnostics["unrequested_rows"] = 0
        search_diagnostics["lookup"] = lookup_diagnostics
        stages = discovery["stages"]
        assert isinstance(stages, dict)
        stages["album_search"] = search_diagnostics
        if resolution is not None:
            lookup_albums = albums
            postlookup_rejection_reasons: dict[str, int] = {}

            def record_postlookup_rejection(reason: str) -> None:
                postlookup_rejection_reasons[reason] = (
                    postlookup_rejection_reasons.get(reason, 0) + 1
                )

            albums = [
                replace(
                    album,
                    verified_musicbrainz_release_id=release_id,
                    identifier_resolution="musicbrainz_search",
                    musicbrainz_recordings_verified=recordings_verified,
                    resolved_musicbrainz_title=resolution.title,
                    resolved_musicbrainz_artist=resolution.artist,
                    resolved_musicbrainz_track_count=resolution.track_count,
                    resolved_musicbrainz_release_year=resolution.release_year,
                    musicbrainz_search_track_count=search_count,
                    musicbrainz_search_track_count_source=(
                        "musicbrainz" if resolution.track_count is not None else "local"
                    ),
                )
                for album in lookup_albums
                if _musicbrainz_search_identity_matches(
                    resolution.title,
                    resolution.artist,
                    search_count,
                    album.album,
                    album.artist,
                    album.track_count,
                    resolution.release_year,
                    album.release_year,
                )
            ]
            accepted_ids = {album.collection_id for album in albums}
            for album in lookup_albums:
                if album.collection_id in accepted_ids:
                    continue
                if album.track_count != search_count:
                    record_postlookup_rejection("resolved MusicBrainz track-count mismatch")
                if (
                    resolution.release_year is not None
                    and album.release_year is not None
                    and abs(resolution.release_year - album.release_year) > 1
                ):
                    record_postlookup_rejection("resolved MusicBrainz release-year mismatch")
                if album.track_count == search_count and not (
                    resolution.release_year is not None
                    and album.release_year is not None
                    and abs(resolution.release_year - album.release_year) > 1
                ):
                    record_postlookup_rejection(
                        "resolved MusicBrainz title, artist, or edition revalidation"
                    )
            search_diagnostics["postlookup_accepted_collections"] = len(albums)
            search_diagnostics["postlookup_rejected_collections"] = len(lookup_albums) - len(albums)
            search_diagnostics["postlookup_rejection_reasons"] = postlookup_rejection_reasons
        has_verified_tracklist = any(
            score_candidate(group, album, allow_short_releases=True).eligible for album in albums
        )
        if not has_verified_tracklist and resolution is None:
            fallback_ids = [
                collection_id
                for collection_id in self._song_fallback_ids(group)
                if collection_id not in collection_ids
            ]
            fallback_albums = self._lookup_collection_ids(fallback_ids)
            albums.extend(fallback_albums)
        candidates = list({album.collection_id: album for album in albums}.values())
        if matching_basis(group) != "legacy" and not candidates:
            warnings.append(
                "the identifier-authoritative Apple search returned no usable complete album"
            )
        return finish(candidates)


__all__ = (
    "AppleCatalogClient",
    "candidate_ids_from_album_search",
    "candidate_ids_from_song_search",
    "catalog_albums_from_lookup",
)
