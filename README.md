# Apple Music Artwork Fetcher and Embedder

Find high-resolution album artwork through Apple's public iTunes catalog and `mzstatic` CDN, verify the release against the tracks you actually have, and embed the image into supported audio files.

Dry-run is the default. Audio files change only when you pass `--apply`.

## Major updates from the original script

This project replaces the workflow described in the old README. The new program is a ground-up rewrite of that folder-cover tool.

| Area | Original script | Current version |
|---|---|---|
| Output | Saved `cover.jpg` or `cover.png` beside an album | Embeds validated front-cover artwork into supported audio files |
| Discovery | Expected a fixed `Artist/Album` layout | Reads embedded tags and scans eligible files recursively |
| Matching | Picked one fuzzy artist/title result | Requires artist identity, a complete local tracklist, verified Apple track evidence, and compatibility across a defined set of edition labels |
| Safety and reporting | Optional dry run and a terminal summary | Dry-run by default, local preflight, transactional writes, and a schema-versioned JSON report |

> [!IMPORTANT]
> This version does **not** create `cover.jpg` or `cover.png`. If you only want loose folder artwork, use the [original script][original-script].

> [!WARNING]
> `--apply` edits audio metadata. Take a fresh backup or filesystem snapshot, inspect a dry-run report, and test on copies of representative albums before using it on a full library.

## Quick start

`--apply` requires Linux with `renameat2(RENAME_EXCHANGE)` support. The tool embeds artwork in FLAC, MP3, audio-only M4A/MP4, Ogg Vorbis, Opus, WAVE, AIFF, and WavPack.

```bash
git clone https://github.com/YellowSnowman23/Apple-Music-Artwork-Fetcher.git
cd Apple-Music-Artwork-Fetcher

python3 -m venv .venv
source .venv/bin/activate
python -m pip install .

# Dry run: writes a report but does not modify audio
apple-artwork "/path/to/Music" -v
```

Inspect `apple-artwork-report.json` before adding `--apply`. The [recommended workflow](#recommended-workflow) covers replacement mode, repeated reports, and special-edition folders.

## What it does

1. Recursively discovers eligible supported audio files beneath the selected root.
2. Reads embedded title, album, artist, album artist, date, disc, track, barcode, and release-ID metadata.
3. Groups physical files into logical releases without relying on directory names.
4. Rejects unsafe files and unsupported metadata layouts before contacting Apple.
5. Searches Apple's unauthenticated iTunes Search/Lookup API and expands candidate albums into complete tracklists.
6. Scores candidates using artist, album, recognized edition labels, track title, duration, disc/track position, track count, and year.
7. Abstains when no candidate clears the identity, coverage, confidence, and ambiguity gates.
8. Downloads and fully validates JPEG or PNG artwork from Apple's CDN.
9. In `--apply` mode, replaces only front-cover artwork while checking that unrelated metadata and encoded audio remain unchanged.

A valid barcode can trigger an exact Apple lookup and provide identifier evidence. A MusicBrainz release ID is used only to keep local release groups separate and to document them in the report; it is not sent to Apple and does not affect candidate scoring.

The tool does not upload, fingerprint, or decode audio for catalog matching.

## Requirements

- Python 3.10 or newer
- Linux with `renameat2(RENAME_EXCHANGE)` support for `--apply`
- Internet access to `itunes.apple.com` and `*.mzstatic.com`
- Correct embedded `title`, `album`, per-track `artist`, and `track number` tags; set `album artist` and `disc number` where applicable

Runtime dependencies:

- Mutagen `>=1.47,<2`
- Pillow `>=12.3.0,<13`
- Requests `>=2.34.2,<3`

FFmpeg is used only by the test suite to build disposable fixtures. It is not required to run the program.

## Other installation options

Use a virtual environment. This avoids Fedora's system-Python restrictions and keeps the dependencies isolated. The quick start above is the recommended source installation.

### Install a release wheel

Download the wheel attached to the corresponding [GitHub release][releases], then run:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install "/path/to/apple_music_artwork_embedder-2.0.3-py3-none-any.whl"
apple-artwork --version
```

### Run the script directly

```bash
git clone https://github.com/YellowSnowman23/Apple-Music-Artwork-Fetcher.git
cd Apple-Music-Artwork-Fetcher

python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python apple_artwork.py --version
```

### Install for development and tests

```bash
git clone https://github.com/YellowSnowman23/Apple-Music-Artwork-Fetcher.git
cd Apple-Music-Artwork-Fetcher

python -m pip install -e '.[dev]'
```

## Recommended workflow

### 1. Take a backup or snapshot

Bulk metadata changes deserve a recovery point even when the write path is transactional. A ZFS snapshot or tested backup is ideal.

### 2. Run a verbose dry scan

```bash
apple-artwork "/path/to/Music" -v --overwrite-report
```

With no `--apply`, the tool:

- reads metadata;
- performs non-mutating container and artwork-store preflight;
- queries Apple only for locally safe albums;
- scores and reports candidates;
- writes API cache entries and `apple-artwork-report.json`;
- does not download the full artwork or change audio files.

The root argument is optional. If omitted, the current directory is used:

```bash
cd "/path/to/Music"
apple-artwork -v --overwrite-report
```

The report path defaults to `apple-artwork-report.json` inside the library root. The program refuses to replace an existing report unless you pass `--overwrite-report`, choose another `--report` path, or use `--no-report`.

### 3. Inspect `apple-artwork-report.json`

For every album, check:

- the local artist, album, year, and track count;
- component scores and candidate rejection reasons;
- local and Apple tracklist coverage;
- recognized edition-label or topology conflicts;
- the selected Apple collection ID when a candidate was accepted.

Albums that reach local adapter preflight also include per-file results. `no_match`, `low_confidence`, and `ambiguous` albums stop before that stage and therefore do not have `file_results`.

If a release is weak or ambiguous, the program leaves it alone and explains why in the report.

### 4. Apply accepted matches

Add artwork only where no front cover exists:

```bash
apple-artwork "/path/to/Music" --apply --overwrite-report -v
```

Replace existing front artwork:

```bash
apple-artwork "/path/to/Music" \
  --apply \
  --replace-existing \
  --overwrite-report \
  -v
```

`--replace-existing` requires `--apply`. It cannot accidentally turn a dry run into a write.

## Special-edition folders beginning with `00`

Files beneath any relative folder component whose name starts with `00` are omitted by default. This protects special-mastering collections whose artwork should not be replaced with an ordinary Apple edition.

The scanner applies this rule before reading embedded tags or making Apple requests.

Examples skipped by default:

```text
Music/00 AF-AFZ/...
Music/00 DCC-GZS/...
Music/Artist/00 Special Edition/...
```

A filename such as `00 Intro.flac` is not omitted. The selected root's own name also does not trigger the rule.

Use `--apply-dcc` to include those directories deliberately:

```bash
# Still a dry run
apple-artwork "/path/to/Music" --apply-dcc -v --overwrite-report

# Include them during a real apply operation
apple-artwork "/path/to/Music" \
  --apply \
  --apply-dcc \
  --replace-existing \
  --overwrite-report \
  -v
```

Despite the name, `--apply-dcc` does not enable `--apply`.

## Verbose output

`-v` and `--verbose` are aliases. Verbose mode adds sanitized progress lines for:

- scan mode, root, country, and DCC policy;
- discovery and omission counts;
- paths omitted by `00`, `--include`, or `--exclude` rules;
- local metadata-adapter preflight;
- album grouping;
- Apple candidate IDs, scores, eligibility, and rejection reasons;
- per-file preflight and apply results;
- validated artwork MIME type, dimensions, and SHA-256.

Verbose mode does not alter matching, network requests, or mutation behavior.

## Folder layouts

The scanner uses embedded metadata, so directory depth does not define an album. These layouts all work:

```text
Music/
├── Artist/Album/01 Track.flac
├── Artist/Album/FLAC/01 Track.flac
├── Artist/Album/MP3/01 Track.mp3
└── Artist/Album/MultiFormat1/Disc 1/01 Track.m4a
```

Duplicate encodes with the same disc number, track number, and normalized title count as one logical track for matching. Artwork is still applied to every physical file.

Discovery silently omits symlinked files, hard-linked files, dot-prefixed filenames, and every file beneath a relative dot-prefixed directory component. These paths never reach metadata processing, verbose omission output, or the JSON report, and `--include` cannot restore them.

## Matching rules

Candidate discovery runs in this order:

1. Exact numeric UPC lookup when a barcode is embedded.
2. Album search using album artist and album title.
3. Distinctive-song fallback when album search finds no gated candidate.
4. Apple Lookup expansion of collection IDs into song rows.

A candidate must then pass the release and tracklist gates:

- Artist identity is mandatory.
- The hard edition gate recognizes: deluxe, expanded, anniversary, special edition, collector's edition, extended, soundtrack, live, mono, stereo, acoustic, instrumental, radio edit, drumless, demo, bonus, remaster, and remix. Conflicting recognized qualifier sets are rejected.
- Other edition wording is handled only by title similarity; it does not trigger a categorical edition rejection. Inspect unusual labels carefully in the dry-run report.
- A trailing bracketed `Album Version` annotation is treated as neutral for track-title comparison.
- Complete local and Apple releases must have the same ordered `(disc number, track number)` topology.
- Strong track pairs require very high title similarity and a duration difference no greater than `max(2 seconds, 0.5%)`, capped at 4 seconds.
- At least 85% track coverage is required using the larger of the local and Apple track counts.
- The default total-score threshold is 0.92.
- The winner must beat the runner-up by at least 0.10.
- Albums whose embedded totals indicate missing tracks are not treated as complete releases.
- Releases normally need at least three strongly aligned tracks unless exact UPC evidence exists or `--allow-short-releases` is supplied.

These thresholds favor false negatives over false positives. A `no_match` or `ambiguous` result is safer than embedding artwork from the wrong edition.

## Artwork selection and validation

Apple's API usually exposes an `artworkUrl100` thumbnail URL rather than a documented original-image endpoint. The downloader tries a small set of source/native-size URL forms and falls back to the API thumbnail when needed.

Every response is checked before use:

- trusted Apple host and redirect policy;
- response-size limit;
- JPEG or PNG magic and content type;
- complete image decode;
- dimensions, pixel count, format, and bit depth;
- SHA-256 integrity for cache and post-write verification.

HTML error pages, truncated images, unsupported formats, oversized images, decoder bombs, and cross-key cache entries are rejected.

Accepted JPEG or PNG bytes are embedded as downloaded after validation; the program does not re-encode the artwork.

`--max-dimension PX` limits requested artwork dimensions to a value from 100 through 10,000 pixels. Apple may return the native dimensions rather than upscaling the image.

## Supported embedding formats

| Container | Artwork mechanism | Replacement behavior |
|---|---|---|
| FLAC | Native FLAC `Picture` | Replaces picture type 3 only; preserves other picture roles |
| MP3 | ID3 `APIC` | Supports ID3v2.3/v2.4; preserves non-front APIC and exact ID3v1 bytes |
| Audio-only M4A/MP4 | MP4 `covr` | Adds when absent; explicit replacement replaces every `covr` value |
| Ogg Vorbis / Opus | `METADATA_BLOCK_PICTURE` | Replaces picture type 3; preserves other valid pictures |
| WAVE / AIFF | Embedded ID3 `APIC` | Replaces picture type 3 only |
| WavPack | APEv2 `Cover Art (Front)` | Replaces only the de-facto front-cover key |

M4A/MP4 `covr` values do not have front/back roles. For those formats, `--replace-existing` replaces all current `covr` entries with the selected Apple image.

The program refuses malformed or mixed artwork stores, leading ID3 metadata on FLAC, APEv2 front art on MP3, legacy role-less `COVERART`, unsafe WavPack tag layouts, ID3v2.2, and video-bearing, fragmented, encrypted, or multi-track MP4 containers. Untested formats are not modified merely because their extension looks familiar.

## Write and data-preservation model

Before an Apple request or write, the program checks path containment, source identity, file type, link count, container support, and artwork-store structure. Discovery omits symlinked and hard-linked audio; later checks refuse a file if its path or link state changes after discovery.

For an accepted album in `--apply` mode:

1. Every file receives an artwork-specific preflight before the first write.
2. Each source is read through an already-open descriptor and copied to a unique staging file in the same directory.
3. Only front-cover artwork is changed.
4. The staged file is checked against the source for unrelated tags, non-front artwork, encoded audio payload, permissions, ownership, extended attributes, and nanosecond timestamps where supported.
5. The source identity is checked again for ordinary concurrent edits.
6. Linux `renameat2(RENAME_EXCHANGE)` swaps the staged and original paths atomically.
7. The displaced inode is verified before its backup is released.

If an error occurs before commit, the original path remains untouched. If an interruption or durability error happens after commit, the report uses an explicit committed status rather than claiming that no mutation occurred.

This protects against realistic accidental data loss, malformed inputs, interruptions, unsafe links, and ordinary concurrent edits. It is not a security boundary against a malicious process already running as the same OS user.

## Reports and statuses

The JSON report uses schema version 2 and records:

- scan mode, root, storefront, discovery counts, and DCC omissions;
- local album identity and physical files;
- Apple candidates and component scores;
- eligibility and rejection reasons;
- selected artwork facts;
- per-file preflight, embed, verification, and failure results when processing reaches that stage.

Statuses have different scopes:

| Status | Scope | Meaning |
|---|---|---|
| `dry-run` | Album | A candidate matched and files were only preflighted |
| `no_match` | Album | No candidate passed the identity and tracklist gates |
| `low_confidence` | Album | The best candidate did not clear the score requirements |
| `ambiguous` | Album | Competing candidates were too close to choose safely |
| `preflight_failed` | Album | Local validation failed before any album file changed |
| `applied` | Album | Artwork was embedded and verified |
| `unchanged` | Album | No file required a change |
| `partial_failure` | Album | At least one file committed, and at least one operation failed or remained unverified |
| `failed` | Album or file | Processing failed without being reported as successfully applied |
| `committed_unverified` | File | Replacement occurred, but a post-commit verification or durability check failed; the album becomes `partial_failure` |
| `committed_interrupted` | File | An interrupt arrived after this file was committed |
| `interrupted_committed` | Album and top-level report | An interrupt arrived after a file committed; the run stops and records the committed result |

## Cache and request behavior

Catalog responses and validated images are cached under `ROOT/.apple-artwork-cache/` by default. Use `--cache-dir` to choose another location.

Cache and report directories are opened component by component without following symlinks. Catalog entries are bound to canonical request parameters. Artwork entries are bound to the Apple collection ID, API artwork URL, requested dimension, byte hash, and decoded image properties.

API and CDN calls are paced. Transient failures are retried with backoff, and cached API responses expire. Use `--refresh-artwork` to ignore cached image bytes while retaining normal catalog-cache behavior. Delete the cache when you need a completely fresh catalog lookup.

## CLI reference

Run `apple-artwork --help` for the parser's authoritative usage text.

The optional positional `root` is the library root; it defaults to the current directory.

| Option | Purpose |
|---|---|
| `--apply` | Atomically embed verified artwork |
| `-v`, `--verbose` | Show discovery, candidate-score, and per-file details |
| `--replace-existing` | Replace existing front covers; requires `--apply` |
| `--country CC` | Select the Apple storefront; default `US` |
| `--cache-dir PATH` | Override `ROOT/.apple-artwork-cache` |
| `--report PATH` | Set an in-root JSON report path |
| `--no-report` | Do not write a report |
| `--overwrite-report` | Replace an existing regular in-root `.json` report |
| `--include GLOB` | Include matching relative paths; repeatable |
| `--exclude GLOB` | Exclude matching relative paths; repeatable |
| `--apply-dcc` | Include relative directories beginning with `00`; does not enable `--apply` |
| `--max-dimension PX` | Request artwork from 100 through 10,000 pixels |
| `--allow-short-releases` | Allow one- or two-track matches without UPC evidence |
| `--refresh-artwork` | Ignore cached artwork bytes and revalidate CDN candidates |
| `--version` | Print the program version |

Quote globs so the shell does not expand them first:

```bash
apple-artwork "/path/to/Music" \
  --include 'Radiohead/**' \
  --exclude '**/Singles/**' \
  -v
```

## Troubleshooting

### `no_match`

Check the embedded album artist, album title, track titles, disc/track numbers, totals, and year. Folder names do not fix incorrect embedded tags.

Run with `-v` and inspect each candidate's reasons in `apple-artwork-report.json`.

### `local tracklist appears incomplete`

An embedded `n/total` value says tracks or discs are missing. Scan the complete release or correct stale totals.

### `ambiguous`

Apple has multiple editions that the local metadata cannot distinguish safely. The tool will not guess. A correct UPC can provide direct Apple catalog provenance. A MusicBrainz release ID strengthens local grouping but is not looked up through Apple's API.

### One-track or two-track release is skipped

Use a correct UPC when available. `--allow-short-releases` relaxes the minimum-track rule, but artist, album, duration, coverage, score, and ambiguity gates still apply.

### Existing artwork is skipped

After reviewing the dry run, use:

```bash
apple-artwork "/path/to/Music" --apply --replace-existing --overwrite-report
```

### Unsupported or malformed container

Do not rename the extension. The path suffix and detected container must agree, and the metadata layout must be one the program can preserve safely.

### Report already exists

Pass `--overwrite-report`, choose another in-root `.json` path with `--report`, or use `--no-report`.

### Apple returns 403, 429, or a temporary server error

The client already paces requests and retries transient failures. Wait and rerun rather than launching multiple copies against the same library.

## Data and privacy

Only release metadata needed for catalog search is sent to Apple's public API, and only after local adapter preflight succeeds. Depending on the search path, this can include album artist, album title, UPC, and selected track titles.

Audio files are never uploaded. During `--apply`, the program calculates local SHA-256 digests over container-specific encoded-audio regions before and after staging to detect unintended changes. Those transient audio hashes are not cached, reported, or transmitted.

Downloaded artwork bytes are hashed for cache integrity and exact post-write verification. The report records the selected artwork hash.

## Testing

Run the offline suite:

```bash
python -m pytest -q
ruff check .
ruff format --check .
```

The release matrix covers CPython 3.10, 3.11, and 3.13 on Linux.

Run the opt-in Apple API/CDN smoke test:

```bash
APPLE_ARTWORK_LIVE_TEST=1 python -m pytest tests/test_live.py -q
```

The live test contacts Apple's public iTunes and `mzstatic` endpoints.

## Project scope

I built this for my own music library and am sharing it in case it helps someone else. It comes without support or warranty. The code is designed around practical protection from accidental corruption, not hostile same-user processes, debugger attacks, or deliberate syscall races.

## Use of AI/LLM Transparency

This project was developed with substantial assistance from large language models (LLMs) (specifically GPT-5.6 Sol on Ultra), including code drafting, test design, documentation, and review. Automated tests, linting, package validation, and targeted safety checks were run, but AI-generated code and documentation can still contain mistakes.

This is a personal-use tool provided **AS IS**. Review the code, test on disposable copies, and keep a current backup or filesystem snapshot before using `--apply` on a real music library.

## Acknowledgements

The original folder-cover script was inspired by [paambaati/itunes-artwork](https://github.com/paambaati/itunes-artwork) and [Ben Dodson's iTunes Artwork Finder](https://bendodson.com/projects/itunes-artwork-finder/), which document useful Apple artwork URL patterns.

This project contacts Apple directly through its public iTunes Search/Lookup API and `mzstatic` CDN. It does not query or scrape Ben Dodson's service and is not affiliated with Apple.

[original-script]: https://github.com/YellowSnowman23/Apple-Music-Artwork-Fetcher/blob/1b1fc0d52e6e57d4fb1d523e8aeec89b4d1f92f9/fetch_mzstatic_covers.py
[original-readme]: https://github.com/YellowSnowman23/Apple-Music-Artwork-Fetcher/blob/1b1fc0d52e6e57d4fb1d523e8aeec89b4d1f92f9/README.md
[releases]: https://github.com/YellowSnowman23/Apple-Music-Artwork-Fetcher/releases
