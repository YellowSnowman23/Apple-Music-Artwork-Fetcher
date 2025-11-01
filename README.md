**Apple Music (mzstatic) Artwork Fetcher**

Fetch the highest‑quality album artwork from Apple’s mzstatic CDN and save it as cover.jpg or cover.png inside each Artist/Album folder. No format conversion; whatever the CDN serves is what gets saved.

- Saves to: Artist/Album/cover.jpg (or cover.png)
- Skips disc subfolders (CD 01, Disc 1, Digital Media 02, etc.)
- Fuzzy matches albums via the iTunes Search API, then probes mzstatic for the largest available image
- Polite throttling, retries, and backoff to avoid 403s and 429s


**Optimal folder structure (naming matters!)**
- The script relies on folder names to search Apple’s catalog. Accurate Artist and Album names produce the best matches.
- Expected layout under your library root:
  - Root/
    - Artist/
      - Album/      -> cover.[jpg|png] is saved here
      - Another Album/
    - Another Artist/
      - Album/      -> cover.[jpg|png] is saved here

- For multi‑disc or multi‑release layouts like Artist/Album/CD 01 or Artist/Album/Digital Media [02], the script ignores those inner subfolders and only writes cover.* to Artist/Album.

Tips for naming:
- Use the canonical album title when possible (e.g., "Abbey Road (2019 Mix)" instead of a rip label).
- Avoid extra tags in folder names (e.g., “[FLAC]”, “WEB”, release years in braces) if you can.
- Make the Artist folder match the primary artist Apple uses (compilations may be “Various Artists”).


**Features**
- Highest‑res artwork: Rewrites artworkUrl100 to probe very large sizes (e.g., 100000x100000 → 6000x6000 → …) and downloads the largest that exists.
- No conversions: File extension is chosen from the Content‑Type (JPEG or PNG).
- Skips safely: Doesn’t overwrite cover.jpg/png unless you pass --force.
- Throttled and retry-safe: Separate pacing for iTunes API and mzstatic CDN with exponential backoff and jitter to avoid 403/429.
- Dry run: See what would be saved without downloading.


**Requirements**
- Python 3.8+
- requests
  - pip install requests


**Installation**
1) Save the script as fetch_mzstatic_covers.py in your repo.
2) Install the dependency:
```bash
pip install requests
```

Optional: Make it executable on macOS/Linux:
```bash
chmod +x fetch_mzstatic_covers.py
```


**Usage**
Basic run (United States store, skip existing covers):
```bash
python fetch_mzstatic_covers.py "/path/to/Your Music Library"
```

Preview without downloading:
```bash
python fetch_mzstatic_covers.py "/path/to/Your Music Library" --dry-run
```

Overwrite existing cover.jpg/.png:
```bash
python fetch_mzstatic_covers.py "/path/to/Your Music Library" --force
```

Be extra gentle with Apple’s rate limits:
```bash
python fetch_mzstatic_covers.py "/path/to/Your Music Library" \
  --api-interval 1.5 --cdn-interval 1.2 --max-retries 8 --backoff 2.0 --jitter 0.5
```

Use a different country store (affects matches and availability):
```bash
python fetch_mzstatic_covers.py "/path/to/Your Music Library" --country GB
```


**CLI options**
```text
positional arguments:
  root                    Path to the music library root (contains Artist/Album folders)

optional arguments:
  --country COUNTRY       iTunes store country code (default: US)
  --limit N               Max results to fetch per search (default: 200)
  --force                 Overwrite existing cover.jpg/cover.png if present
  --dry-run               Show what would be done, without downloading
  --timeout SECONDS       HTTP timeout in seconds (default: 15)

throttling / retries:
  --api-interval SEC      Min seconds between iTunes API calls (default: 1.0)
  --cdn-interval SEC      Min seconds between mzstatic image requests (default: 0.6)
  --max-retries N         Max retries for transient HTTP errors (default: 5)
  --backoff F             Exponential backoff factor for retries (default: 1.8)
  --jitter SEC            Random jitter added to waits (default: 0.3)
```


**How it works**
- Search: Queries Apple’s iTunes Search API for albums using “Artist Album” and picks the best match with a fuzzy score (weighted 70% album title, 30% artist).
- Upscale probe: Takes the API’s artworkUrl100 (…/100x100bb.jpg) and tries larger dimensions in descending order until an image is served.
- Save: Streams the first successful image directly to cover.jpg or cover.png in Artist/Album. Extension is based on the server’s Content‑Type. No conversion or re-encoding.
- Safety: Skips if a cover already exists unless --force is used. Never writes inside disc subfolders.


**Matching accuracy (naming tips)**
- Keep Artist and Album names as close to Apple Music’s naming as you can.
- For deluxe/expanded editions, reflect the “(Deluxe)” or similar tag if that’s the target you want.
- Compilations: Consider “Various Artists” if that’s how Apple lists it.
- If you frequently see wrong matches, raise the match threshold in the code (best_album_match(min_score=0.55 → 0.65)). It will trade recall for precision.


**Handling 403/429 (rate limiting)**
- The script already spaces requests and retries with exponential backoff and jitter.
- If you still hit limits, increase:
  - --cdn-interval and --api-interval
  - --max-retries and --backoff
  - Keep --jitter > 0 so requests aren’t perfectly regular


**Sample output**
```text
Scanning 312 album folders under: /Music

  1/312 [OK] Beatles/Abbey Road — Saved cover.jpg [10000x10000] — matched 'The Beatles – Abbey Road (2019 Mix)'
  2/312 [SKIP] Beatles/Revolver — Exists: cover.jpg  — Beatles / Revolver
  3/312 [SKIP] Pink Floyd/The Dark Side of the Moon — No good match: Pink Floyd / The Dark Side of the Moon
...
Done.
Downloaded: 157  |  Skipped: 151  |  Errors: 4
```


**Troubleshooting**
- “No good match”: Check folder names; try the correct country store; consider simplifying the Album name.
- “No accessible artwork”: Some items lack high-res art or CDN blocks the size requested. Try again later or allow smaller sizes naturally via probing.
- “Exists (other format)”: If cover.png exists and mzstatic returns JPEG (or vice‑versa), use --force to replace it. The script does not convert formats.
- 403/429 errors: Raise --cdn-interval/--api-interval and retry counts; ensure your connection isn’t opening too many parallel requests (this script is single-threaded on purpose).
- Paths with special characters: The script handles Unicode, but avoid unusual brackets or decorations in folder names when possible.


**Notes and limitations**
- Library root must be the directory whose immediate children are Artist folders. Don’t pass a single Album folder as the root.
- Only albums are searched (entity=album). Singles/EPs sometimes appear as albums, but very short releases might be harder to match.
- The script doesn’t embed artwork into audio files; it only writes cover.jpg/png into folders.
- Data sent: Only the artist and album names (as strings) are sent to Apple’s public search endpoint.


**Acknowledgements**
- Inspired by paambaati/itunes-artwork and Ben Dodson’s iTunes Artwork Finder, which document Apple artwork URL patterns.
- Artwork served by Apple’s mzstatic CDN; catalog queries via the public iTunes Search API.

---
**NOTE**
I will not be updating this, this was vibe-coded using GPT-5-Pro for my use case, and I decided to upload it to GitHub incase anyone else can make use of it
