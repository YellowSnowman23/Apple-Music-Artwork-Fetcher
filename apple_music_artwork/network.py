"""Allowlisted HTTP transport, bounded reads, redirects, and retry timing."""

from __future__ import annotations

import math
import random
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

from .constants import MAX_REDIRECTS, MAX_RETRY_DELAY


def _is_allowed_https_url(url: str, *, api: bool) -> bool:
    """Allow only HTTPS Apple API or mzstatic CDN destinations, including redirects."""
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    if parsed.scheme.casefold() != "https" or parsed.username or parsed.password:
        return False
    try:
        if parsed.port not in {None, 443}:
            return False
    except ValueError:
        return False
    if api:
        return hostname == "itunes.apple.com"
    return hostname == "mzstatic.com" or hostname.endswith(".mzstatic.com")


def _validate_remote_url(url: str, *, api: bool) -> None:
    if not _is_allowed_https_url(url, api=api):
        destination = "Apple API" if api else "Apple mzstatic CDN"
        raise ValueError(f"redirect or URL is not an allowlisted HTTPS {destination} destination")


def _close_response(response: object) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        close()


def _request_with_validated_redirects(
    session: object,
    url: str,
    *,
    timeout: float,
    api: bool,
    params: Mapping[str, object] | None = None,
) -> tuple[object, str]:
    """Follow a small redirect chain while validating every requested and reported URL."""
    current = url
    current_params = dict(params) if params is not None else None
    for redirect_count in range(MAX_REDIRECTS + 1):
        _validate_remote_url(current, api=api)
        kwargs: dict[str, object] = {
            "timeout": timeout,
            "allow_redirects": False,
            "stream": True,
        }
        if current_params is not None:
            kwargs["params"] = current_params
        response = session.get(current, **kwargs)  # type: ignore[attr-defined]
        reported_url = str(getattr(response, "url", current) or current)
        try:
            _validate_remote_url(reported_url, api=api)
        except ValueError:
            _close_response(response)
            raise
        status = int(getattr(response, "status_code", 0))
        if status not in {301, 302, 303, 307, 308}:
            return response, reported_url
        location = str(getattr(response, "headers", {}).get("Location") or "")
        _close_response(response)
        if not location:
            raise ValueError("Apple redirect response omitted Location")
        if redirect_count >= MAX_REDIRECTS:
            raise ValueError("Apple redirect limit exceeded")
        current = urljoin(reported_url, location)
        _validate_remote_url(current, api=api)
        current_params = None
    raise ValueError("Apple redirect limit exceeded")


def _read_bounded_body(response: object, *, maximum: int, timeout: float) -> bytes:
    headers = getattr(response, "headers", {})
    content_length = headers.get("Content-Length") if isinstance(headers, Mapping) else None
    if content_length is not None:
        try:
            declared = int(content_length)
        except (TypeError, ValueError) as exc:
            raise ValueError("response has an invalid Content-Length") from exc
        if declared < 0 or declared > maximum:
            raise ValueError(f"response body exceeds the {maximum}-byte limit")
    iterator = getattr(response, "iter_content", None)
    if not callable(iterator):
        raise ValueError("HTTP client did not provide a streaming response")
    deadline = time.monotonic() + max(1.0, float(timeout))
    chunks: list[bytes] = []
    total = 0
    for chunk in iterator(chunk_size=65_536):
        if time.monotonic() > deadline:
            raise TimeoutError("response transfer exceeded its total deadline")
        if not chunk:
            continue
        if not isinstance(chunk, bytes):
            chunk = bytes(chunk)
        total += len(chunk)
        if total > maximum:
            raise ValueError(f"response body exceeds the {maximum}-byte limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _retry_delay(value: object, attempt: int) -> float:
    fallback = min(MAX_RETRY_DELAY, 1.8**attempt)
    if value is None:
        base = fallback
    else:
        text = str(value).strip()
        try:
            base = float(text)
            if not math.isfinite(base):
                raise ValueError
        except ValueError:
            try:
                parsed = parsedate_to_datetime(text)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                base = (parsed - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                base = fallback
    base = min(MAX_RETRY_DELAY, max(0.0, base))
    return min(MAX_RETRY_DELAY, base + random.uniform(0.0, 0.25))
