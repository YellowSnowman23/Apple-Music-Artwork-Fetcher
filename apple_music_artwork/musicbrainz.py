"""Bounded MusicBrainz release resolution for identifier-first Apple discovery."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

import requests

from .constants import MAX_API_BYTES, MAX_REDIRECTS, USER_AGENT
from .filesystem import _atomic_write_bytes, _read_secure_file
from .matching import _normalize_barcode, _normalize_release_id
from .models import MusicBrainzRelease
from .network import _close_response, _read_bounded_body, _retry_delay

MUSICBRAINZ_RELEASE_URL = "https://musicbrainz.org/ws/2/release"


@dataclass(frozen=True, slots=True)
class _ResolvedMusicBrainzRelease:
    """A release plus client-validated evidence that the requested MBID was resolved."""

    release: MusicBrainzRelease
    requested_release_id: str


def _validate_musicbrainz_url(url: str) -> None:
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid MusicBrainz URL port") from exc
    if (
        parsed.scheme.casefold() != "https"
        or hostname != "musicbrainz.org"
        or parsed.username
        or parsed.password
        or port not in {None, 443}
    ):
        raise ValueError("redirect or URL is not an allowlisted HTTPS MusicBrainz destination")


def _query_values_preserving_plus(query: str) -> dict[str, list[str]]:
    """Parse a query without treating MusicBrainz's literal ``+`` as a space."""
    values: dict[str, list[str]] = {}
    for field in query.split("&"):
        if not field:
            continue
        raw_key, separator, raw_value = field.partition("=")
        key = unquote(raw_key)
        value = unquote(raw_value) if separator else ""
        values.setdefault(key, []).append(value)
    return values


def _request_with_musicbrainz_redirects(
    session: object,
    url: str,
    *,
    params: Mapping[str, object],
    timeout: float,
    before_request: Callable[[], None] | None = None,
) -> tuple[object, str]:
    current = url
    current_params: dict[str, object] | None = dict(params)
    for redirect_count in range(MAX_REDIRECTS + 1):
        _validate_musicbrainz_url(current)
        kwargs: dict[str, object] = {
            "timeout": timeout,
            "allow_redirects": False,
            "stream": True,
        }
        if current_params is not None:
            kwargs["params"] = current_params
        if before_request is not None:
            before_request()
        response = session.get(current, **kwargs)  # type: ignore[attr-defined]
        reported_url = str(getattr(response, "url", current) or current)
        try:
            _validate_musicbrainz_url(reported_url)
        except ValueError:
            _close_response(response)
            raise
        status = int(getattr(response, "status_code", 0))
        if status not in {301, 302, 303, 307, 308}:
            return response, reported_url
        location = str(getattr(response, "headers", {}).get("Location") or "")
        _close_response(response)
        if not location:
            raise ValueError("MusicBrainz redirect response omitted Location")
        if redirect_count >= MAX_REDIRECTS:
            raise ValueError("MusicBrainz redirect limit exceeded")
        current = urljoin(reported_url, location)
        _validate_musicbrainz_url(current)
        redirect_query = _query_values_preserving_plus(urlsplit(current).query)
        if any(
            key in redirect_query and redirect_query[key] != [str(value)]
            for key, value in params.items()
        ):
            raise ValueError("MusicBrainz redirect changed required API query parameters")
        missing_params = {key: value for key, value in params.items() if key not in redirect_query}
        current_params = missing_params or None
    raise ValueError("MusicBrainz redirect limit exceeded")


def _bounded_text(value: object) -> str:
    text = str(value or "").strip()
    return text if len(text) <= 4096 else ""


def _release_artist(payload: Mapping[str, object]) -> str:
    credit = payload.get("artist-credit")
    if not isinstance(credit, list) or len(credit) > 100:
        return ""
    parts: list[str] = []
    for item in credit:
        if not isinstance(item, Mapping):
            return ""
        name = _bounded_text(item.get("name"))
        if not name:
            artist = item.get("artist")
            name = _bounded_text(artist.get("name")) if isinstance(artist, Mapping) else ""
        if not name:
            return ""
        parts.append(name)
        parts.append(_bounded_text(item.get("joinphrase")))
    return "".join(parts).strip()


def _release_track_count(payload: Mapping[str, object]) -> int | None:
    media = payload.get("media")
    if not isinstance(media, list) or not media or len(media) > 100:
        return None
    total = 0
    for medium in media:
        if not isinstance(medium, Mapping):
            return None
        tracks = medium.get("tracks")
        if tracks is not None and (not isinstance(tracks, list) or len(tracks) > 10_000):
            return None
        value = medium.get("track-count")
        try:
            count = int(value) if value is not None else len(tracks or [])
        except (TypeError, ValueError):
            return None
        if count < 1 or count > 10_000:
            return None
        if isinstance(tracks, list) and value is not None and count != len(tracks):
            raise ValueError("MusicBrainz returned an inconsistent medium track count")
        total += count
    return total or None


def _release_recording_ids(payload: Mapping[str, object]) -> tuple[str, ...]:
    media = payload.get("media")
    if not isinstance(media, list) or not media or len(media) > 100:
        return ()
    recording_ids: list[str] = []
    for medium in media:
        if not isinstance(medium, Mapping):
            return ()
        tracks = medium.get("tracks")
        if not isinstance(tracks, list) or not tracks or len(tracks) > 10_000:
            return ()
        for track in tracks:
            if not isinstance(track, Mapping):
                return ()
            recording = track.get("recording")
            recording_id = (
                _normalize_release_id(_bounded_text(recording.get("id")))
                if isinstance(recording, Mapping)
                else None
            )
            if recording_id is None:
                return ()
            recording_ids.append(recording_id)
    return tuple(recording_ids)


def _apple_collection_id(resource: str) -> int | None:
    parsed = urlsplit(resource)
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    try:
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() != "https"
        or hostname not in {"music.apple.com", "itunes.apple.com"}
        or parsed.username
        or parsed.password
        or port not in {None, 443}
    ):
        return None
    legacy_itunes_path = parsed.path == "/WebObjects/MZStore.woa/wa/viewAlbum"
    localized_legacy_path = re.fullmatch(
        r"/[A-Za-z]{2}/wa/viewAlbum",
        parsed.path,
    )
    if hostname == "itunes.apple.com" and (legacy_itunes_path or localized_legacy_path is not None):
        query = _query_values_preserving_plus(parsed.query)
        if parsed.fragment or set(query) != {"id", "s"}:
            return None
        collection_values = query["id"]
        storefront_values = query["s"]
        if len(collection_values) != 1 or len(storefront_values) != 1:
            return None
        candidate = collection_values[0]
        storefront = storefront_values[0]
        if (
            not candidate.isascii()
            or not candidate.isdigit()
            or not 1 <= len(candidate) <= 19
            or not storefront.isascii()
            or not storefront.isdigit()
            or len(storefront) != 6
        ):
            return None
        value = int(candidate)
        return value if value > 0 and int(storefront) > 0 else None

    path = parsed.path[:-1] if parsed.path.endswith("/") else parsed.path
    if not path.startswith("/"):
        return None
    path_segments = path[1:].split("/")
    if not path_segments or any(not segment for segment in path_segments):
        return None
    if path_segments and path_segments[0].casefold() != "album":
        if not re.fullmatch(r"[A-Za-z]{2}", path_segments[0]):
            return None
        path_segments = path_segments[1:]
    if not path_segments or path_segments[0].casefold() != "album":
        return None

    candidate = ""
    if len(path_segments) == 2:
        candidate = path_segments[1].removeprefix("id")
    elif len(path_segments) == 3:
        candidate = path_segments[2].removeprefix("id")
    if not candidate.isascii() or not candidate.isdigit():
        return None
    value = int(candidate)
    return value if value > 0 else None


def _release_id_from_musicbrainz_url(url: str) -> str | None:
    """Extract a canonical release UUID only from an already allowlisted release URL."""
    _validate_musicbrainz_url(url)
    path_segments = [segment for segment in urlsplit(url).path.split("/") if segment]
    if len(path_segments) < 2 or path_segments[-2].casefold() != "release":
        return None
    return _normalize_release_id(path_segments[-1])


def parse_musicbrainz_release(
    payload: Mapping[str, object],
    *,
    expected_release_id: str,
) -> MusicBrainzRelease:
    release_id = _normalize_release_id(_bounded_text(payload.get("id")))
    expected = _normalize_release_id(expected_release_id)
    if release_id is None or release_id != expected:
        raise ValueError("MusicBrainz returned a different or invalid release ID")
    title = _bounded_text(payload.get("title"))
    artist = _release_artist(payload)
    if not title or not artist:
        raise ValueError("MusicBrainz returned incomplete release identity")
    date = _bounded_text(payload.get("date"))
    year_match = re.search(r"(?<!\d)(?:18|19|20|21)\d{2}(?!\d)", date)
    track_count = _release_track_count(payload)
    recording_ids = _release_recording_ids(payload)
    media = payload.get("media")
    expanded_recordings_present = isinstance(media, list) and any(
        isinstance(medium, Mapping)
        and isinstance(medium.get("tracks"), list)
        and bool(medium["tracks"])
        for medium in media
    )
    if expanded_recordings_present and (track_count is None or len(recording_ids) != track_count):
        raise ValueError("MusicBrainz returned inconsistent release track topology")
    relations = payload.get("relations")
    collection_ids: set[int] = set()
    if isinstance(relations, list) and len(relations) <= 500:
        for relation in relations:
            if not isinstance(relation, Mapping):
                continue
            target = relation.get("url")
            resource = _bounded_text(target.get("resource")) if isinstance(target, Mapping) else ""
            collection_id = _apple_collection_id(resource)
            if collection_id is not None:
                collection_ids.add(collection_id)
    return MusicBrainzRelease(
        release_id=release_id,
        title=title,
        artist=artist,
        release_year=int(year_match.group()) if year_match else None,
        track_count=track_count,
        barcode=_normalize_barcode(_bounded_text(payload.get("barcode"))),
        apple_collection_ids=tuple(sorted(collection_ids)),
        recording_ids=recording_ids,
    )


class MusicBrainzClient:
    """Polite cached client for resolving a release MBID into Apple-linking evidence."""

    def __init__(
        self,
        *,
        cache_dir: Path = Path(".apple-artwork-cache"),
        session: object | None = None,
        timeout: float = 20.0,
        api_interval: float = 1.1,
        max_retries: int = 4,
        cache_ttl_days: int = 30,
        max_response_bytes: int = MAX_API_BYTES,
    ) -> None:
        self.cache_dir = cache_dir / "musicbrainz"
        self.session = session or requests.Session()
        headers = getattr(self.session, "headers", None)
        if headers is not None:
            headers.update({"Accept": "application/json", "User-Agent": USER_AGENT})
        self.timeout = max(1.0, float(timeout))
        self.api_interval = max(1.0, float(api_interval))
        self.max_retries = max(1, int(max_retries))
        self.cache_ttl_seconds = max(0, int(cache_ttl_days)) * 86_400
        self.max_response_bytes = max(1, min(int(max_response_bytes), MAX_API_BYTES))
        self._last_request = 0.0

    @staticmethod
    def _cache_key(release_id: str) -> str:
        return hashlib.sha256(release_id.encode("ascii")).hexdigest()

    def _cache_path(self, release_id: str) -> Path:
        return self.cache_dir / f"{self._cache_key(release_id)}.json"

    def _load_cache(self, release_id: str) -> _ResolvedMusicBrainzRelease | None:
        path = self._cache_path(release_id)
        try:
            if time.time() - path.lstat().st_mtime > self.cache_ttl_seconds:
                return None
            envelope = json.loads(_read_secure_file(path, self.max_response_bytes).decode("utf-8"))
            if (
                not isinstance(envelope, Mapping)
                or envelope.get("schema_version") not in {1, 2}
                or envelope.get("release_id") != release_id
                or not isinstance(envelope.get("response"), Mapping)
            ):
                return None
            canonical_release_id = (
                _normalize_release_id(_bounded_text(envelope.get("canonical_release_id")))
                or release_id
            )
            release = parse_musicbrainz_release(
                envelope["response"],
                expected_release_id=canonical_release_id,
            )
            return _ResolvedMusicBrainzRelease(
                release=release,
                requested_release_id=release_id,
            )
        except (OSError, TypeError, ValueError, UnicodeError):
            return None

    def _save_cache(
        self,
        release_id: str,
        payload: Mapping[str, object],
        *,
        canonical_release_id: str,
    ) -> None:
        encoded = json.dumps(
            {
                "schema_version": 2,
                "release_id": release_id,
                "canonical_release_id": canonical_release_id,
                "response": payload,
            },
            ensure_ascii=True,
            sort_keys=True,
        ).encode("ascii")
        if len(encoded) > self.max_response_bytes:
            raise ValueError("MusicBrainz JSON cache payload exceeds the configured limit")
        _atomic_write_bytes(self._cache_path(release_id), encoded)

    def _pace(self) -> None:
        remaining = self.api_interval - (time.monotonic() - self._last_request)
        if remaining > 0:
            time.sleep(remaining)

    def _pace_request(self) -> None:
        self._pace()
        self._last_request = time.monotonic()

    def resolve(self, release_id: str) -> _ResolvedMusicBrainzRelease | None:
        normalized = _normalize_release_id(release_id)
        if normalized is None:
            return None
        cached = self._load_cache(normalized)
        if cached is not None:
            return cached

        url = f"{MUSICBRAINZ_RELEASE_URL}/{normalized}"
        params = {"fmt": "json", "inc": "url-rels+artist-credits+recordings"}
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            response: object | None = None
            try:
                response, final_url = _request_with_musicbrainz_redirects(
                    self.session,
                    url,
                    params=params,
                    timeout=self.timeout,
                    before_request=self._pace_request,
                )
                status = int(getattr(response, "status_code", 0))
                if status == 404:
                    return None
                if status == 200:
                    content_type = (
                        str(getattr(response, "headers", {}).get("Content-Type") or "")
                        .split(";", 1)[0]
                        .strip()
                        .casefold()
                    )
                    if content_type not in {"application/json", "text/json"}:
                        raise ValueError(
                            f"MusicBrainz returned unsupported Content-Type {content_type!r}"
                        )
                    body = _read_bounded_body(
                        response,
                        maximum=self.max_response_bytes,
                        timeout=self.timeout,
                    )
                    payload = json.loads(body.decode("utf-8"))
                    if not isinstance(payload, Mapping):
                        raise ValueError("MusicBrainz returned malformed JSON")
                    canonical_release_id = _release_id_from_musicbrainz_url(final_url)
                    if canonical_release_id is None:
                        raise ValueError("MusicBrainz returned a non-release response URL")
                    parsed = parse_musicbrainz_release(
                        payload,
                        expected_release_id=canonical_release_id,
                    )
                    self._save_cache(
                        normalized,
                        payload,
                        canonical_release_id=canonical_release_id,
                    )
                    return _ResolvedMusicBrainzRelease(
                        release=parsed,
                        requested_release_id=normalized,
                    )
                if status not in {403, 429} and status < 500:
                    response.raise_for_status()  # type: ignore[attr-defined]
                last_error = requests.RequestException(f"MusicBrainz API returned HTTP {status}")
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
        if last_error is not None:
            raise last_error
        return None


__all__ = (
    "MUSICBRAINZ_RELEASE_URL",
    "MusicBrainzClient",
    "parse_musicbrainz_release",
)
