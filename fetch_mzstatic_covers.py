#!/usr/bin/env python3
"""
Fetch highest-quality Apple Music (mzstatic) album artwork for an Artist/Album folder structure,
with polite throttling and backoff to avoid 403s.

- Library layout expected:
    Root/
      Artist/
        Album/                -> cover.[jpg|png] is saved here
          (optional disc subfolders like "CD 01", "Digital Media 01", etc. are ignored)
        Album 2/
      Artist 2/
        Album/

- How it works:
    1) Scans Artist/Album directories two levels deep.
    2) Uses Apple's iTunes Search API to find candidate albums.
    3) Probes mzstatic for the largest artwork by rewriting artworkUrl100 to very large sizes,
       trying from biggest to smaller until one succeeds.
    4) Streams the successful image directly to cover.jpg or cover.png (no conversion).

Usage:
    python fetch_mzstatic_covers.py /path/to/music \
        [--country US] [--limit 200] [--force] [--dry-run]
        [--timeout 15] [--api-interval 1.0] [--cdn-interval 0.6]
        [--max-retries 5] [--backoff 1.8] [--jitter 0.3]

Notes:
    - Requires: requests
      pip install requests
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import random
import re
import sys
import time
from pathlib import Path
from typing import Iterable, Optional, Tuple, List, Dict
from urllib.parse import urlparse

import requests

ITUNES_SEARCH_URL = "https://itunes.apple.com/search"

# Preferred probe sizes (largest -> smaller). mzstatic caps to the master size.
PROBE_SIZES = [
    "100000x100000",
    "20000x20000",
    "10000x10000",
    "8000x8000",
    "6000x6000",
    "5000x5000",
    "4000x4000",
    "3000x3000",
    "2500x2500",
    "2000x2000",
    "1500x1500",
    "1200x1200",
    "1000x1000",
    "800x800",
    "600x600",
]

# Disc-folder name patterns we ignore
DISC_FOLDER_PATTERNS = [
    re.compile(r"^CD\s*\d+$", re.IGNORECASE),
    re.compile(r"^Disc\s*\d+$", re.IGNORECASE),
    re.compile(r"^Digital\s*Media\s*\d+$", re.IGNORECASE),
    re.compile(r"^Digital\s*Media\s*\[\d+\]$", re.IGNORECASE),
    re.compile(r"^\[\d+\]$", re.IGNORECASE),
]


def is_disc_subfolder(name: str) -> bool:
    name = name.strip()
    return any(p.search(name) for p in DISC_FOLDER_PATTERNS)


def find_album_dirs(root: Path) -> Iterable[Path]:
    """
    Yield Artist/Album directories two levels deep under root.
    Ignore deeper nesting; we only target Artist/Album.
    """
    if not root.is_dir():
        return
    for artist in sorted([p for p in root.iterdir() if p.is_dir()]):
        for album in sorted([p for p in artist.iterdir() if p.is_dir()]):
            yield album


def normalize_text(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[^\w\s]", "", s)
    return s


def similarity(a: str, b: str) -> float:
    a_n = normalize_text(a)
    b_n = normalize_text(b)
    return difflib.SequenceMatcher(None, a_n, b_n).ratio()


def best_album_match(
    results: List[Dict],
    artist: str,
    album: str,
    min_score: float = 0.55,
) -> Optional[Dict]:
    """
    Choose the best album from iTunes Search results using a weighted similarity.
    """
    best = None
    best_score = -1.0

    for r in results:
        kind_ok = (r.get("collectionType") == "Album") or (r.get("wrapperType") == "collection")
        entity_ok = "collectionName" in r and "artistName" in r and "artworkUrl100" in r
        if not (kind_ok and entity_ok):
            continue

        alb = r.get("collectionName", "")
        art = r.get("artistName", "")
        alb_score = similarity(album, alb)
        art_score = similarity(artist, art)
        score = 0.7 * alb_score + 0.3 * art_score

        if score > best_score or (abs(score - best_score) < 1e-6 and alb_score > (best or {}).get("_alb_score", 0)):
            r["_score"] = score
            r["_alb_score"] = alb_score
            r["_art_score"] = art_score
            best = r
            best_score = score

    if best and best_score >= min_score:
        return best
    return None


def build_upscaled_urls(artwork_url: str) -> List[str]:
    """
    Given artworkUrl100 (e.g., .../100x100bb.jpg, .../100x100-75.jpg),
    generate a list of candidate URLs with progressively smaller target sizes.
    """
    m = re.search(r"/(\d{2,5}x\d{2,5})(bb)?(-\d+)?\.(jpg|png)$", artwork_url, re.IGNORECASE)
    if not m:
        # Fallback: attach size blocks
        ext = "png" if artwork_url.lower().endswith(".png") else "jpg"
        return [f"{artwork_url}/{sz}.{ext}" for sz in PROBE_SIZES]

    bb = m.group(2) or ""    # 'bb' or ''
    q = m.group(3) or ""     # quality like '-75' or ''
    ext = m.group(4)         # 'jpg' or 'png'
    prefix = artwork_url[: m.start(1) - 1]  # up to the slash before size

    return [f"{prefix}/{sz}{bb}{q}.{ext}" for sz in PROBE_SIZES]


def derive_extension_from_content_type(ct: str) -> str:
    ct = (ct or "").lower()
    if "png" in ct:
        return ".png"
    return ".jpg"  # default for jpeg and anything else image/*


def album_has_cover(album_dir: Path) -> Optional[Path]:
    for name in ("cover.jpg", "cover.png"):
        p = album_dir / name
        if p.exists():
            return p
    return None


class Pacer:
    """
    Simple per-key pacer to enforce minimum spacing between requests.
    """
    def __init__(self) -> None:
        self._last: Dict[str, float] = {}

    def wait(self, key: str, min_interval: float, jitter: float = 0.0) -> None:
        now = time.monotonic()
        last = self._last.get(key, 0.0)
        to_wait = max(0.0, min_interval - (now - last))
        if to_wait > 0:
            # Add small random jitter to avoid thundering herd
            if jitter > 0:
                to_wait += random.uniform(0.0, jitter)
            time.sleep(to_wait)

    def mark(self, key: str) -> None:
        self._last[key] = time.monotonic()


def api_search_with_retries(
    session: requests.Session,
    pacer: Pacer,
    params: Dict[str, str],
    timeout: int,
    api_interval: float,
    max_retries: int,
    backoff: float,
    jitter: float,
) -> List[Dict]:
    """
    GET iTunes Search API with pacing and retry on 403/429/5xx or transient network errors.
    """
    key = "itunes_api"
    last_exc: Optional[Exception] = None
    for attempt in range(max_retries):
        pacer.wait(key, api_interval, jitter=jitter * 0.5)
        try:
            r = session.get(ITUNES_SEARCH_URL, params=params, timeout=timeout)
            pacer.mark(key)
            if r.status_code == 200:
                data = r.json()
                return data.get("results", [])
            # Retryable statuses
            if r.status_code in (403, 429) or 500 <= r.status_code < 600:
                retry_after = r.headers.get("Retry-After")
                if retry_after:
                    try:
                        delay = float(retry_after)
                    except ValueError:
                        delay = (backoff ** attempt)
                else:
                    delay = (backoff ** attempt)
                time.sleep(min(30.0, delay) + random.uniform(0.0, max(0.05, jitter)))
                continue
            # Non-retryable
            r.raise_for_status()
            return []
        except (requests.Timeout, requests.ConnectionError) as e:
            last_exc = e
            delay = (backoff ** attempt)
            time.sleep(min(30.0, delay) + random.uniform(0.0, max(0.05, jitter)))
            continue
    # If we reach here, give the last exception
    if last_exc:
        raise last_exc
    return []


def request_cdn_stream(
    session: requests.Session,
    pacer: Pacer,
    url: str,
    timeout: int,
    cdn_interval: float,
    max_retries: int,
    backoff: float,
    jitter: float,
) -> Optional[requests.Response]:
    """
    GET mzstatic (CDN) with stream=True. Returns the open response on 200 image/*, else None.
    Applies pacing and retry on 403/429/5xx and network errors, honoring Retry-After if present.
    """
    key = "mzstatic_cdn"
    for attempt in range(max_retries):
        pacer.wait(key, cdn_interval, jitter=jitter)
        try:
            r = session.get(url, allow_redirects=True, timeout=timeout, stream=True)
            # Always mark after the request to space subsequent calls
            pacer.mark(key)
            st = r.status_code
            if st == 200:
                ct = r.headers.get("Content-Type", "").lower()
                if "image" in ct:
                    return r  # caller must close or consume
                r.close()
                return None  # 200 but not an image; treat as terminal for this candidate
            # Retryable statuses
            if st in (403, 429) or 500 <= st < 600:
                retry_after = r.headers.get("Retry-After")
                r.close()
                if retry_after:
                    try:
                        delay = float(retry_after)
                    except ValueError:
                        delay = (backoff ** attempt)
                else:
                    delay = (backoff ** attempt)
                time.sleep(min(60.0, delay) + random.uniform(0.0, max(0.05, jitter)))
                continue
            # Non-retryable -> give up on this candidate
            r.close()
            return None
        except (requests.Timeout, requests.ConnectionError):
            delay = (backoff ** attempt)
            time.sleep(min(60.0, delay) + random.uniform(0.0, max(0.05, jitter)))
            continue
        except requests.RequestException:
            # Other request issues: treat as terminal for this candidate
            return None
    return None


def pick_largest_working_art_stream(
    session: requests.Session,
    pacer: Pacer,
    artwork_url_100: str,
    timeout: int,
    cdn_interval: float,
    max_retries: int,
    backoff: float,
    jitter: float,
) -> Optional[Tuple[requests.Response, str, str]]:
    """
    Try progressively smaller upscaled artwork URLs until one returns 200 image/*.
    Returns (open_response, url, content_type) or None.
    The returned response is stream=True and must be consumed or closed by the caller.
    """
    for candidate in build_upscaled_urls(artwork_url_100):
        resp = request_cdn_stream(
            session=session,
            pacer=pacer,
            url=candidate,
            timeout=timeout,
            cdn_interval=cdn_interval,
            max_retries=max_retries,
            backoff=backoff,
            jitter=jitter,
        )
        if resp:
            ct = resp.headers.get("Content-Type", "").lower()
            return resp, candidate, ct
    return None


def download_stream_to_file(resp: requests.Response, dest: Path) -> None:
    """
    Stream an already-open response (stream=True) to a temporary file, then move to dest.
    Closes the response when done.
    """
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with open(tmp, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 64):
                if chunk:
                    f.write(chunk)
        tmp.replace(dest)
    finally:
        try:
            resp.close()
        except Exception:
            pass


def search_itunes_albums(
    session: requests.Session,
    pacer: Pacer,
    artist: str,
    album: str,
    country: str,
    limit: int,
    timeout: int,
    api_interval: float,
    max_retries: int,
    backoff: float,
    jitter: float,
) -> List[Dict]:
    params = {
        "term": f"{artist} {album}",
        "media": "music",
        "entity": "album",
        "country": country,
        "limit": str(limit),
    }
    results = api_search_with_retries(session, pacer, params, timeout, api_interval, max_retries, backoff, jitter)
    return results


def process_album(
    session: requests.Session,
    pacer: Pacer,
    album_dir: Path,
    country: str,
    limit: int,
    overwrite: bool,
    dry_run: bool,
    timeout: int,
    api_interval: float,
    cdn_interval: float,
    max_retries: int,
    backoff: float,
    jitter: float,
) -> Tuple[bool, str]:
    """
    Returns (changed, message)
    """
    try:
        artist = album_dir.parent.name
        album = album_dir.name
    except Exception:
        return False, f"Skip (unusual path): {album_dir}"

    if is_disc_subfolder(album):
        return False, f"Skip disc subfolder: {album_dir}"

    existing = album_has_cover(album_dir)
    if existing and not overwrite:
        return False, f"Exists: {existing.relative_to(album_dir)}  — {artist} / {album}"

    # Query iTunes Search
    try:
        results = search_itunes_albums(
            session=session,
            pacer=pacer,
            artist=artist,
            album=album,
            country=country,
            limit=limit,
            timeout=timeout,
            api_interval=api_interval,
            max_retries=max_retries,
            backoff=backoff,
            jitter=jitter,
        )
    except requests.RequestException as e:
        return False, f"Network error: {artist} / {album} — {e}"

    if not results:
        # Second pass focusing on album term only
        try:
            params = {
                "term": album,
                "media": "music",
                "entity": "album",
                "country": country,
                "limit": str(limit),
                "attribute": "albumTerm",
            }
            results = api_search_with_retries(
                session=session,
                pacer=pacer,
                params=params,
                timeout=timeout,
                api_interval=api_interval,
                max_retries=max_retries,
                backoff=backoff,
                jitter=jitter,
            )
        except requests.RequestException as e:
            return False, f"Network error: {artist} / {album} — {e}"

    match = best_album_match(results, artist=artist, album=album, min_score=0.55)
    if not match:
        return False, f"No good match: {artist} / {album}"

    artwork_url_100 = match.get("artworkUrl100")
    if not artwork_url_100:
        return False, f"No artworkUrl100: {artist} / {album}"

    if dry_run:
        # Don't hit the CDN in dry-run; just show the top candidate we'd try first
        top_candidate = build_upscaled_urls(artwork_url_100)[0]
        ext_hint = ".png" if top_candidate.lower().endswith(".png") else ".jpg"
        dest = album_dir / f"cover{ext_hint}"
        return False, f"Would save -> {dest.relative_to(album_dir)}  from {top_candidate}"

    pick = pick_largest_working_art_stream(
        session=session,
        pacer=pacer,
        artwork_url_100=artwork_url_100,
        timeout=timeout,
        cdn_interval=cdn_interval,
        max_retries=max_retries,
        backoff=backoff,
        jitter=jitter,
    )
    if not pick:
        return False, f"No accessible artwork: {artist} / {album}"

    resp, url, content_type = pick
    ext = derive_extension_from_content_type(content_type)
    dest = album_dir / f"cover{ext}"

    # Respect existing other format if not overwrite
    other_ext = ".png" if ext == ".jpg" else ".jpg"
    other = album_dir / f"cover{other_ext}"
    if other.exists() and not overwrite:
        try:
            resp.close()
        except Exception:
            pass
        return False, f"Exists (other format): {other.relative_to(album_dir)} — {artist} / {album}"

    # Download directly from the open stream (no extra GET)
    try:
        download_stream_to_file(resp, dest)
    except requests.RequestException as e:
        return False, f"Download failed: {artist} / {album} — {e}"

    if overwrite and other.exists():
        try:
            other.unlink()
        except Exception:
            pass

    alb_name = match.get("collectionName", "")
    art_name = match.get("artistName", "")
    size_info = next((s for s in PROBE_SIZES if f"/{s}" in url), "unknown")
    return True, f"Saved {dest.relative_to(album_dir)}  [{size_info}] — matched '{art_name} – {alb_name}'"


def main():
    ap = argparse.ArgumentParser(description="Fetch highest-quality album artwork from mzstatic (Apple Music), with throttling/backoff.")
    ap.add_argument("root", type=str, help="Path to the music library root (contains Artist/Album folders).")
    ap.add_argument("--country", type=str, default="US", help="iTunes store country code (default: US).")
    ap.add_argument("--limit", type=int, default=200, help="Max results to fetch per search (default: 200).")
    ap.add_argument("--force", action="store_true", help="Overwrite existing cover.jpg/cover.png if present.")
    ap.add_argument("--dry-run", action="store_true", help="Show what would be done, without downloading.")
    ap.add_argument("--timeout", type=int, default=15, help="HTTP timeout in seconds (default: 15).")

    # Throttling/backoff controls
    ap.add_argument("--api-interval", type=float, default=1.0, help="Min seconds between iTunes API calls (default: 1.0).")
    ap.add_argument("--cdn-interval", type=float, default=0.6, help="Min seconds between mzstatic image requests (default: 0.6).")
    ap.add_argument("--max-retries", type=int, default=5, help="Max retries for transient HTTP errors (default: 5).")
    ap.add_argument("--backoff", type=float, default=1.8, help="Exponential backoff factor for retries (default: 1.8).")
    ap.add_argument("--jitter", type=float, default=0.3, help="Random jitter added to waits (seconds, default: 0.3).")

    args = ap.parse_args()

    root = Path(args.root).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        print(f"Root path does not exist or is not a directory: {root}", file=sys.stderr)
        sys.exit(1)

    session = requests.Session()
    session.headers.update({
        # Friendly UA + referer helps with some CDNs
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://music.apple.com/",
        "Connection": "keep-alive",
    })

    pacer = Pacer()

    album_dirs = list(find_album_dirs(root))
    total = len(album_dirs)
    if total == 0:
        print("No Artist/Album directories found.")
        return

    print(f"Scanning {total} album folders under: {root}\n")

    changed = 0
    skipped = 0
    errors = 0

    for idx, album_dir in enumerate(album_dirs, start=1):
        rel = album_dir.relative_to(root)
        try:
            did_change, msg = process_album(
                session=session,
                pacer=pacer,
                album_dir=album_dir,
                country=args.country,
                limit=args.limit,
                overwrite=args.force,
                dry_run=args.dry_run,
                timeout=args.timeout,
                api_interval=args.api_interval,
                cdn_interval=args.cdn_interval,
                max_retries=args.max_retries,
                backoff=args.backoff,
                jitter=args.jitter,
            )
            prefix = "[OK]" if did_change else "[SKIP]"
            print(f"{idx:4d}/{total:4d} {prefix} {rel} — {msg}")
            if did_change:
                changed += 1
            else:
                if any(k in msg.lower() for k in ["error", "failed", "no accessible artwork", "network"]):
                    errors += 1
                else:
                    skipped += 1
        except KeyboardInterrupt:
            print("\nInterrupted by user.")
            break
        except Exception as e:
            errors += 1
            print(f"{idx:4d}/{total:4d} [ERR] {rel} — Exception: {e}")

        # Optional tiny pause between folders (keeps things extra chill)
        time.sleep(0.05)

    print("\nDone.")
    print(f"Downloaded: {changed}  |  Skipped: {skipped}  |  Errors: {errors}")


if __name__ == "__main__":
    main()
