# Apple Music Artwork Fetcher and Embedder

Find high-resolution album artwork through Apple's public iTunes catalog and `mzstatic` CDN, identify releases from embedded UPC and MusicBrainz tags first, and embed the image into supported audio files. When no usable identifier is present, the program falls back to conservative metadata and tracklist matching.

Dry-run is the default. Audio files change only when you pass `--apply`.

> [!NOTE]
> This README describes version 3.0.0. Review a dry-run report before using `--apply`.

## Major updates from the original script

This project replaces the workflow described in the [original README][original-readme] and implemented by [`fetch_mzstatic_covers.py`][original-script]. The new program is a ground-up rewrite of that folder-cover tool.

| Area | Original script | Current version |
|---|---|---|
| Output | Saved `cover.jpg` or `cover.png` beside an album | Embeds validated front-cover artwork into supported audio files |
| Discovery | Expected a fixed `Artist/Album` layout | Reads embedded tags and scans eligible files recursively |
| Matching | Picked one fuzzy artist/title result | Trusts embedded UPC and MusicBrainz release identity first; uses the strict metadata and tracklist matcher only when no valid identifier is available |
| Safety and reporting | Optional dry run and a terminal summary | Dry-run by default, local preflight, transactional writes, and a schema-versioned JSON report |

> [!IMPORTANT]
> This version does **not** create `cover.jpg` or `cover.png`. If you only want loose folder artwork, use the [original script][original-script].

> [!IMPORTANT]
> Tagging your collection with [MusicBrainz Picard](https://picard.musicbrainz.org/) before using this program is **highly recommended**. Version 3.0 relies heavily on correct release MBIDs, recording MBIDs, and UPC/barcode tags. Picard's Lookup and Scan/AcoustID workflows are an effective way to establish that provenance.

> [!WARNING]
> `--apply` edits audio metadata. Take a fresh backup or filesystem snapshot, inspect a dry-run report, and test on copies of representative albums before using it on a full library.

## Quick start

`--apply` requires Linux and a filesystem that supports directory `fsync`. The preferred path uses `renameat2(RENAME_EXCHANGE)`. A capability-triggered fallback additionally requires same-directory hard links and `renameat2(RENAME_NOREPLACE)`, as supported by the tested SMB 3.1.1/CIFS mount. The tool embeds artwork in FLAC, MP3, audio-only M4A/MP4, Ogg Vorbis, Opus, WAVE, AIFF, and WavPack.

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
2. Reads embedded title, album, artist, album artist, date, disc, track, UPC/barcode, MusicBrainz release MBID, and per-track recording MBID metadata.
3. Groups physical files into logical releases without relying on directory names.
4. Rejects unsafe files and unsupported metadata layouts before contacting Apple.
5. Resolves identifiers in priority order: an embedded UPC through Apple's exact lookup; a release MBID through MusicBrainz's Apple URL relationships or barcode; otherwise an MBID-authoritative, order-independent Apple search. When both identifiers exist, positive cross-source contradictions block the match.
6. Uses the conservative title, artist, duration, topology, and edition scorer only when the release has no valid UPC or release MBID.
7. Records the match basis and any identifier or metadata warnings, and abstains when neither identifier resolution nor fallback matching establishes a candidate.
8. Downloads and fully validates JPEG or PNG artwork from Apple's CDN.
9. In `--apply` mode, replaces only front-cover artwork while checking that unrelated metadata and encoded audio remain unchanged.

A valid embedded UPC receives the first exact Apple lookup. A valid release MBID is then looked up at MusicBrainz when the UPC does not resolve or when both identifiers are present and need consistency validation. A direct Apple Music/iTunes URL relationship is used when available; otherwise a MusicBrainz barcode receives an exact Apple lookup. If neither direct mapping is available, the release MBID remains authoritative local provenance for an order-independent Apple candidate search. When every local track has a recording MBID, those IDs are checked against the recordings in the resolved MusicBrainz release before any Apple candidate is trusted. An exact UPC result is retained with a prominent warning when a second MBID cannot be resolved or cross-validated; a positive conflict between their barcodes, Apple collections, or recording identities stops matching.

When a UPC or MusicBrainz mapping directly identifies the release, differences such as `(Bonus Track)`, `(Explicit)`, brackets, punctuation, hyphen style, disc splitting or flattening, and track order do not reject it. This is intentional: providers often present the same purchased release differently.

> [!WARNING]
> Identifier-first matching trusts your tags. An incorrect UPC or MusicBrainz release MBID can select the wrong Apple artwork even when local titles look plausible. Correct inconsistent tags in Picard, run a dry scan, and inspect each report's match basis and warnings before applying changes.

The tool does not upload, fingerprint, or decode audio for catalog matching.

## Architecture

The implementation is an importable `apple_music_artwork` package. The small top-level `apple_artwork.py` file remains as a compatibility facade and direct-script launcher.

Format-specific metadata handling is intentionally isolated under `apple_music_artwork/adapters/`:

- `flac.py` — native FLAC picture blocks;
- `xiph.py` — Ogg Vorbis and Opus `METADATA_BLOCK_PICTURE` fields;
- `mp4.py` — M4A/MP4 `covr` atoms and audio-only container validation;
- `id3.py` — MP3, WAVE, and AIFF APIC frames;
- `wavpack.py` — WavPack APEv2 front-cover fields.

Each adapter owns its format-family preflight, front-art inspection, mutation, and post-write artwork verification. The shared embedding layer owns staging, encoded-audio and unrelated-metadata preservation checks, compare-and-swap replacement, rollback, and durability. Keeping those responsibilities separate makes a format bug less likely to leak into unrelated containers.

## Requirements

- Python 3.10 or newer
- Linux with directory `fsync` and either `RENAME_EXCHANGE`, or both same-directory hard links and `RENAME_NOREPLACE`, for `--apply`
- Internet access to `itunes.apple.com`, `*.mzstatic.com`, and `musicbrainz.org`
- Correct embedded `title`, `album`, per-track `artist`, and `track number` tags; set `album artist` and `disc number` where applicable
- Strongly recommended: a MusicBrainz Picard-tagged collection with a release MBID and per-track recording MBIDs; retain accurate `BARCODE` or `UPC` tags from the source release when available

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
python -m pip install "/path/to/apple_music_artwork_embedder-3.0.0-py3-none-any.whl"
apple-artwork --version
```

### Run the script directly

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python apple_artwork.py --version
```

### Install for development and tests

```bash
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
- resolves embedded identifiers through Apple and, when present, MusicBrainz, including dual-ID consistency checks;
- runs identifier-authoritative selection or the identifier-absent fallback matcher and reports candidates;
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
- the `match_basis`: `upc`, `musicbrainz`, `musicbrainz+upc`, or `legacy`, which records the usable local identifier inputs; `identifier_resolution` records the actual winning path as `embedded_upc`, `musicbrainz_apple_relation`, `musicbrainz_barcode`, or `musicbrainz_search`;
- the candidate's verified UPC, verified release MBID, and `musicbrainz_recordings_verified` result;
- the local and resolved identifiers and any conflict, fallback, or presentation warnings;
- component scores, coverage, and candidate rejection reasons when fallback scoring was needed;
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

## Protected special-mastering folders

Files beneath any relative folder component whose name starts with `00`, `DCC`, or `GZS` (case-insensitive) are omitted by default. This protects special-mastering collections whose artwork should not be replaced with an ordinary Apple edition.

The scanner applies this rule before reading embedded tags or making Apple requests.

Examples skipped by default:

```text
Music/00 AF-AFZ/...
Music/00 DCC-GZS/...
Music/Artist/00 Special Edition/...
Music/Artist/DCC Gold/...
Music/Artist/GZS-1001/...
```

A filename such as `00 Intro.flac` or `DCC Track.flac` is not omitted. The selected root's own name also does not trigger the rule.

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

`--apply-dcc` is the backward-compatible override for all three protected prefixes. Despite the name, it does not enable `--apply`.

## Verbose output

`-v` and `--verbose` are aliases. Verbose mode adds sanitized progress lines for:

- scan mode, root, country, and DCC policy;
- discovery and omission counts;
- paths omitted by the `00`/`DCC`/`GZS`, `--include`, or `--exclude` rules;
- local metadata-adapter preflight;
- album grouping;
- identifier-resolution basis and warnings;
- Apple candidate IDs, scores, eligibility, and rejection reasons where applicable;
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

Duplicate encodes with the same recording MBID and position, or the same disc number, track number, and normalized title, count as one logical track. Shared release MBIDs and UPCs are grouped transitively; contradictory identifiers stop before network matching and are reported. Artwork is still applied to every physical file.

Discovery silently omits symlinked files, hard-linked files, dot-prefixed filenames, and every file beneath a relative dot-prefixed directory component. These paths never reach metadata processing, verbose omission output, or the JSON report, and `--include` cannot restore them.

## Identifier-first matching

Version 3.0 treats valid embedded release identifiers as the primary source of truth. Candidate resolution runs in this order:

1. **Embedded UPC/barcode.** A valid local UPC receives an exact Apple lookup. If Apple returns the release for that UPC, the candidate is identifier-verified. When a release MBID is also present, MusicBrainz is consulted to detect positive identifier contradictions; an unavailable or unresolvable MBID leaves the exact UPC match intact with a warning.
2. **MusicBrainz direct Apple relationship.** If the embedded UPC does not resolve and a valid release MBID exists, the program follows the release's Apple Music/iTunes URL relationship. When an exact UPC result already exists, this relationship cross-validates and narrows it to the common Apple collection.
3. **MusicBrainz barcode.** If the MusicBrainz release has no Apple relationship but provides a valid barcode, that barcode receives an exact Apple lookup.
4. **MBID-authoritative search.** If MusicBrainz establishes the release but exposes neither usable direct mapping, its canonical artist, album, track count, and release year constrain an order-independent Apple search. Search candidates retain those resolved fields in the report, and Apple rows are rechecked after lookup before MBID provenance is attached. Complete local recording-MBID sets are checked against the resolved release, and recording IDs that are not members of that resolved release stop matching. If MusicBrainz has merged an older release MBID, a validated MusicBrainz redirect to the canonical release is retained as auditable alias evidence instead of discarding the valid older Picard tag. MusicBrainz recording-ID aliases are not resolved individually in 3.0, so an older merged recording ID can conservatively produce `no_match`; retagging the album with current Picard data resolves that case without weakening release safety.
5. **Identifier-absent fallback.** Only when the local release has neither a valid UPC nor a valid release MBID does the program use the conservative 2.5-style album and distinctive-song searches followed by fuzzy scoring.

### Identifier-authoritative behavior

Direct UPC, MusicBrainz Apple-relationship, and MusicBrainz-barcode matches do not require Apple and local providers to spell or lay out the release identically. The following are non-blocking presentation differences when direct identifiers establish the release:

- `(Bonus)`, `(Bonus Track)`, `(Explicit)`, `Album Version`, and similar provider annotations;
- album and track-title wording differences between the local store, MusicBrainz, and Apple;
- parentheses versus brackets, punctuation, capitalization, apostrophes, and random hyphen differences;
- explicitly marked features (`feat.`, `ft.`, bracketed `with`/`w/`) written in the title on one
  provider and the artist field on another; ambiguous unmarked joint-artist punctuation remains
  conservative so established group names are not split into invented feature credits;
- a multi-disc release flattened to one disc, or the reverse;
- a different track order, including provider-specific sequencing such as the Qobuz edition of a release versus Apple's presentation.

The MBID-authoritative search is likewise order-independent: it does not reject an Apple candidate merely because disc/track positions differ. It uses the MusicBrainz release as authoritative evidence instead of treating local filename or provider presentation as the identity key.

An identifier match can still fail when the identifier is malformed, MusicBrainz or Apple cannot resolve the only usable identifier, the returned object is not a usable Apple album, or release MBIDs, recording MBIDs, barcodes, and direct Apple mappings contradict one another. An exact UPC is not discarded merely because a second MBID lookup is temporarily unavailable, but that incomplete cross-validation is reported prominently. Unresolved identifiers are never silently sent through a looser text-only matcher. These conditions are reported as identifier warnings rather than disguised as ordinary title mismatches.

### Identifier-absent fallback

The strict matcher remains available for untagged collections, but it is a fallback rather than the primary architecture. In this mode:

- artist identity and compatible edition semantics are required;
- complete local and Apple releases normally need the same ordered disc/track topology;
- strong track pairs require very high title similarity and a duration difference no greater than `max(2 seconds, 0.5%)`, capped at 4 seconds;
- at least 85% track coverage and a default total score of 0.92 are required;
- the winner normally must beat the runner-up by at least 0.10, with conservative duration tie-breaking only for genuinely equivalent duplicate catalog releases;
- embedded totals indicating missing tracks remain a blocker;
- one- and two-track fallback matches require `--allow-short-releases`.

The fallback continues to understand bounded provider wording differences such as `Album Version`, remaster/expanded/deluxe packaging labels, bracket style, and feature-credit spelling. It still abstains on unresolved ties, incompatible semantic editions, incomplete topology, and reordered tracks. Those fallback restrictions do **not** override a valid identifier-authoritative match.

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

DSD containers, including `.dsf` and `.dff`, are intentionally not discovered or modified.

The program refuses malformed or mixed artwork stores, leading ID3 metadata on FLAC, APEv2 front art on MP3, legacy role-less `COVERART`, unsafe WavPack tag layouts, ID3v2.2, and video-bearing, fragmented, encrypted, or multi-track MP4 containers. Untested formats are not modified merely because their extension looks familiar.

## Write and data-preservation model

Before an Apple request or write, the program checks path containment, source identity, file type, link count, container support, and artwork-store structure. Discovery omits symlinked and hard-linked audio; later checks refuse a file if its path or link state changes after discovery.

For an accepted album in `--apply` mode:

1. Every file receives an artwork-specific preflight before the first write.
2. Each source is read through an already-open descriptor and copied to a unique staging file in the same directory.
3. Only front-cover artwork is changed.
4. The staged file is checked against the source for unrelated tags, non-front artwork, encoded audio payload, permissions, ownership, extended attributes, and nanosecond timestamps where supported.
5. The source identity is checked again for ordinary concurrent edits.
6. Linux `renameat2(RENAME_EXCHANGE)` swaps the staged and original paths atomically when the filesystem supports it. This remains the journal-free fast path.
7. Only when the filesystem rejects exchange with an unsupported-operation error, the fallback durably records a same-directory recovery journal and creates a verified private hard link to the original.
8. The fallback moves the current visible entry aside with `RENAME_NOREPLACE`, verifies whether it is still the expected original, then installs the staged entry with `RENAME_NOREPLACE`. A concurrent editor save is never overwritten.
9. The original and journal are released only after content, metadata, namespace state, and directory durability are verified. An interrupted fallback is rolled back automatically when safe; otherwise every version and the journal are retained for fail-closed recovery.

On startup, the normal library walk also detects incomplete journals. Dry-run reports them without mutating recovery state; `--apply` recovers them before metadata parsing or network access and rescans only when recovery was needed. During the fallback's two same-directory renames, the visible name is briefly absent; the durable journal and verified recovery link cover process or machine interruption in that interval.

If an error occurs before a namespace transition, the original path remains untouched. If an interruption or durability error happens during or after a transition, the tool either restores the verified original durably or reports explicit committed uncertainty while retaining recovery material.

This protects against realistic accidental data loss, malformed inputs, interruptions, unsafe links, and ordinary concurrent edits. It is not a security boundary against a malicious process already running as the same OS user.

## Reports and statuses

The JSON report uses schema version 3 and records:

- scan mode, root, storefront, discovery counts, and DCC omissions;
- local album identity, physical files, UPC, release MBID, grouped identifier conflicts, and whether per-track MusicBrainz provenance is complete;
- the local-input match basis (`upc`, `musicbrainz`, `musicbrainz+upc`, or `legacy`), the actual `identifier_resolution` path, the candidate's verified UPC and release MBID, whether its recording MBIDs were verified, and canonical MusicBrainz artist/title/count/year fields used by inferred MBID searches; when MusicBrainz omits a release count, `musicbrainz_search_track_count` and its `local` source explicitly record the bounded local-count fallback without mislabeling it as resolved MusicBrainz data;
- identifier-resolution and presentation-difference warnings, including order or topology differences intentionally accepted by an authoritative match;
- Apple candidates and component scores;
- eligibility and rejection reasons;
- selected artwork facts;
- per-file preflight, embed, verification, and failure results when processing reaches that stage.

Statuses have different scopes:

| Status | Scope | Meaning |
|---|---|---|
| `in_progress` | Top-level report | Durable checkpoint written before each successful mutation call returns; if checkpoint persistence itself fails after commit, the CLI explicitly reports that it was not confirmed |
| `complete` | Top-level report | Processing and final report serialization completed successfully |
| `dry-run` | Album | A candidate matched and files were only preflighted |
| `no_match` | Album | No identifier path resolved a usable Apple release and no fallback candidate passed its gates |
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
| `--apply-dcc` | Include protected relative directories beginning with `00`, `DCC`, or `GZS`; does not enable `--apply` |
| `--max-dimension PX` | Request artwork from 100 through 10,000 pixels |
| `--allow-short-releases` | Allow one- or two-track matches in the identifier-absent fallback path |
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

First inspect the report's match basis and identifier warnings. Confirm the embedded UPC and MusicBrainz release MBID in Picard. If the release has no valid identifier, check the embedded album artist, album title, track titles, disc/track numbers, totals, and year. Folder names do not fix incorrect embedded tags.

Run with `-v` and inspect each candidate's reasons in `apple-artwork-report.json`.

### `local tracklist appears incomplete`

An embedded `n/total` value says tracks or discs are missing. Scan the complete release or correct stale totals.

### `ambiguous`

No direct UPC, MusicBrainz Apple relationship, or MusicBrainz barcode established one Apple collection, and the remaining MBID-authoritative or fallback candidates could not be distinguished. Correct or add the release identifiers in Picard instead of changing harmless title punctuation merely to influence a fuzzy score.

### One-track or two-track release is skipped

Add a correct UPC or MusicBrainz release MBID when available. `--allow-short-releases` relaxes the minimum-track rule only for the identifier-absent fallback; its other fallback identity and ambiguity gates still apply.

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

Only release metadata needed for catalog search is sent after local adapter preflight succeeds. Apple requests can include album artist, album title, UPC, and selected track titles. Whenever a valid release MBID is used—either after an unresolved UPC or to cross-check an exact UPC result—that MBID is sent to the public MusicBrainz API to retrieve release metadata, recording identities, Apple Music/iTunes URL relationships, and barcodes. Local recording MBIDs are not sent separately; they are compared locally with the recording IDs returned for that release.

Audio files are never uploaded. During `--apply`, the program calculates local SHA-256 digests over container-specific encoded-audio regions before and after staging to detect unintended changes. Those transient audio hashes are not cached, reported, or transmitted.

Downloaded artwork bytes are hashed for cache integrity and exact post-write verification. The report records the selected artwork hash.

## Testing

Run the offline suite:

```bash
python -m pytest -q
ruff check .
ruff format --check .
```

The project declares and syntax-checks compatibility with Python 3.10 and newer. Before publishing a release, run the full suite on the supported CPython release matrix; the current local development validation runtime is reported with the test results.

Run the opt-in catalog/API/CDN smoke test:

```bash
APPLE_ARTWORK_LIVE_TEST=1 python -m pytest tests/test_live.py -q
```

The live test contacts the public catalog services and Apple's `mzstatic` endpoint used by the current matching paths.

## Project scope

I built this for my own Fedora music library and am sharing it in case it helps someone else. It comes without support or warranty. The code is designed around practical protection from accidental corruption, not hostile same-user processes, debugger attacks, or deliberate syscall races.

## Use of AI/LLM Transparency

This project was developed with substantial assistance from large language models (LLMs) (specifically GPT-5.6 Sol on Ultra), including code drafting, test design, documentation, and review. Automated tests, linting, package validation, and targeted safety checks were run, but AI-generated code and documentation can still contain mistakes.

This is a personal-use tool provided **AS IS**. Review the code, test on disposable copies, and keep a current backup or filesystem snapshot before using `--apply` on a real music library.

## Acknowledgements

The original folder-cover script was inspired by [paambaati/itunes-artwork](https://github.com/paambaati/itunes-artwork) and [Ben Dodson's iTunes Artwork Finder](https://bendodson.com/projects/itunes-artwork-finder/), which document useful Apple artwork URL patterns.

This project contacts Apple directly through its public iTunes Search/Lookup API and `mzstatic` CDN. It does not query or scrape Ben Dodson's service and is not affiliated with Apple.

[original-script]: https://github.com/YellowSnowman23/Apple-Music-Artwork-Fetcher/blob/1b1fc0d52e6e57d4fb1d523e8aeec89b4d1f92f9/fetch_mzstatic_covers.py
[original-readme]: https://github.com/YellowSnowman23/Apple-Music-Artwork-Fetcher/blob/1b1fc0d52e6e57d4fb1d523e8aeec89b4d1f92f9/README.md
[releases]: https://github.com/YellowSnowman23/Apple-Music-Artwork-Fetcher/releases
