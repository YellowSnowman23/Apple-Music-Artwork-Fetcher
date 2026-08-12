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
                or (track_count is not None and not exact_count)
                or (
                    release_year is not None
                    and row_year is not None
                    and abs(release_year - row_year) > 1
                )
            ):
                continue
        elif not _artists_equivalent(artist, row_artist) or (
            album_score < 0.62 and not exact_count
        ):
            continue
        ranked.append(
            (0.57 * album_score + 0.38 * artist_score + 0.05 * float(exact_count), collection_id)
        )
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return list(dict.fromkeys(collection_id for _, collection_id in ranked[:limit]))


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
        if declared_count is None or declared_count < 1 or len(tracks_data) != declared_count:
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
                track_count=declared_count,
                tracks=tuple(tracks),
            )
        )
    return sorted(albums, key=lambda album: album.collection_id)


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

    def _lookup_collection_ids(self, collection_ids: Iterable[int]) -> list[CatalogAlbum]:
        ordered_ids = list(dict.fromkeys(collection_ids))[:24]
        albums: list[CatalogAlbum] = []
        for start in range(0, len(ordered_ids), 8):
            chunk = ordered_ids[start : start + 8]
            lookup_rows = self._request_results(
                ITUNES_LOOKUP_URL,
                {
                    "id": ",".join(str(collection_id) for collection_id in chunk),
                    "country": self.country,
                    "entity": "song",
                    "limit": 200,
                },
            )
            chunk_ids = set(chunk)
            parsed = [
                album
                for album in catalog_albums_from_lookup(lookup_rows)
                if album.collection_id in chunk_ids
            ]
            albums.extend(parsed)
            returned = {album.collection_id for album in parsed}
            for missing_id in (
                collection_id for collection_id in chunk if collection_id not in returned
            ):
                individual_rows = self._request_results(
                    ITUNES_LOOKUP_URL,
                    {
                        "id": str(missing_id),
                        "country": self.country,
                        "entity": "song",
                        "limit": 200,
                    },
                )
                albums.extend(
                    album
                    for album in catalog_albums_from_lookup(individual_rows)
                    if album.collection_id == missing_id
                )
        return list({album.collection_id: album for album in albums}.values())

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

        def finish(albums: list[CatalogAlbum]) -> list[CatalogAlbum]:
            self.last_identifier_warnings = tuple(warnings)
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
            upc_albums = [
                replace(
                    album,
                    verified_barcode=local_barcode,
                    identifier_resolution="embedded_upc",
                )
                for album in catalog_albums_from_lookup(upc_rows)
            ]
            if upc_albums and release_id is None:
                return finish(upc_albums)
            if not upc_albums:
                warnings.append("the embedded UPC returned no usable complete Apple album")
            if not upc_albums and release_id is None:
                return finish([])

        resolution = None
        if release_id is not None:
            try:
                resolved = self.musicbrainz_client.resolve(release_id)  # type: ignore[attr-defined]
            except Exception:
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
                    warnings.append(
                        "a custom MusicBrainz resolver cannot assert merged-release "
                        "alias provenance"
                    )
                    return finish([])
                if _normalize_release_id(resolved.requested_release_id) != release_id:
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
                warnings.append("MusicBrainz returned malformed release resolution evidence")
                return finish([])
            resolved_release_id = _normalize_release_id(resolution.release_id)
            if resolved_release_id is None:
                warnings.append(
                    "MusicBrainz returned an invalid release MBID for the embedded identifier"
                )
                return finish([])
            if resolved_release_id != release_id:
                if not alias_provenance:
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
            related = self._lookup_collection_ids(resolution.apple_collection_ids)
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
            warnings.append("MusicBrainz Apple relationship returned no usable complete album")
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
            resolved_upc_albums = [
                replace(
                    album,
                    verified_barcode=resolution.barcode,
                    verified_musicbrainz_release_id=release_id,
                    identifier_resolution="musicbrainz_barcode",
                    musicbrainz_recordings_verified=recordings_verified,
                )
                for album in catalog_albums_from_lookup(resolved_upc_rows)
            ]
            if resolved_upc_albums:
                return finish(resolved_upc_albums)
            warnings.append("the MusicBrainz barcode returned no usable complete Apple album")

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
        albums = self._lookup_collection_ids(collection_ids) if collection_ids else []
        if resolution is not None:
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
                for album in albums
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
