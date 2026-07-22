# AO3 Downloader

A self-hosted web app that searches [Archive of Our Own](https://archiveofourown.org) by **author** or **tag/fandom**, shows the results in a browser UI, and downloads selected works as e-books (EPUB, PDF, MOBI, HTML, AZW3) using AO3's own download endpoints — with a built-in global rate limiter so it stays polite to AO3's servers.

## Quick start

```bash
cp .env.example .env      # optional — edit paths, port, PUID/PGID, timezone
docker compose up -d --build
```

Then open **http://localhost:8067**.

Every setting has a default, so `docker compose up -d --build` works straight after a clone even without a `.env` file. `.env` is git-ignored, which means **`git pull` never overwrites your local paths** — handy when the same repo runs on both a laptop and a NAS. See [`.env.example`](.env.example) for every option.

Downloaded files appear on the host in the `./downloads/` bind mount, named `Title - Author.ext`. With the default `docker-compose.yml` (Calibre mode, see below) all files land directly in the downloads root; set `FLAT_DOWNLOADS: "false"` to instead get per-search subfolders (`./downloads/<search query>/...`).

**`./downloads/` only ever contains e-books.** The app's record of what it has downloaded (`metadata.json` — title, authors, word count, tags, fandoms, stats and summary per work, keyed by AO3 work ID) lives in a separate `./config/` volume, so nothing unexpected sits in a folder Calibre is watching.

## Using the app

1. Enter an author username (e.g. `missyuki1990`), a tag/fandom (e.g. `Harry Potter`), **or paste an AO3 work link** — any work or chapter URL such as `https://archiveofourown.org/works/2755349/chapters/6177083` fetches exactly that story and ignores the Author/Tag toggle. The built-in **Search guide** panel explains each mode and AO3's advanced query operators (`"exact phrase"`, `AND`/`OR`, `-exclude`, `*` wildcard).
2. Choose a format (EPUB is default), a **Sort by** order (date updated, kudos, hits, bookmarks or word count — applied server-side by AO3) and a max result count (default 100, server cap 500).
3. Optionally open **Filters**: complete works only, word count min/max, and comma-separated tags to exclude. Filters work for both author and tag searches (and survive the tag-search fallback, with slight approximations noted in the UI code).
4. Click **Search** — results stream in page by page (narrated in the activity log). Result rows show word count, kudos, hits, a rating badge and a Complete/WIP badge; click the **Words / Kudos / Hits** column headers to re-sort locally without touching AO3.
5. Untick anything you don't want, then click **Download selected**.
6. Watch the progress bar and the activity log. Each row gets a ✓ Done / Skipped / Error badge.

Already-downloaded files are **skipped automatically** (dedup checks the file on disk), so re-running a search + download is cheap and safe.

### Library

The **Library** tab lists **everything the app has downloaded**, according to `./config/metadata.json` — title, authors, word count, badges, format, size and download date. You can filter by text and sort locally.

Entries whose file is still in `./downloads/` offer **Download** (saves it to your browser) and **Delete** (removes the file and the record). Entries Calibre has already imported are marked **Imported** — their file is gone from the watch folder, so only **Forget** is offered, which drops the record and lets the work be downloaded again. Files without a metadata entry (e.g. dropped in manually) still show up by filename.

## Politeness & rate limiting

All AO3 traffic — search pages and file downloads alike — goes through a single global rate limiter:

- Random **3–6 s delay** between every request (configurable).
- On **HTTP 429**, the app honors the `Retry-After` header (waits can be minutes — the wait is shown in the activity log) or falls back to exponential backoff, then retries up to `MAX_RETRIES` times.
- A realistic browser User-Agent and a persistent cookie session are used; `view_adult=true` bypasses the adult-content interstitial.
- Downloads use AO3's **pre-built e-book files** (`/downloads/...`), never re-rendered HTML.

Because of this, large searches and downloads are intentionally slow (~4–5 s per request). That's the point.

## Configuration

Set in `docker-compose.yml` (defaults shown):

| Variable | Default | Description |
|---|---|---|
| `AO3_MIN_DELAY` / `AO3_MAX_DELAY` | `3` / `6` | Random delay range (seconds) between AO3 requests |
| `MAX_RETRIES` | `5` | Retries per request on 429/5xx/network errors |
| `RETRY_BACKOFF_BASE` | `30` | Backoff base (seconds) when no `Retry-After` header |
| `RETRY_BACKOFF_CAP` | `600` | Max backoff wait (seconds) |
| `RETRY_AFTER_CAP` | `1800` | Sanity cap on honored `Retry-After` values |
| `MAX_RESULTS_CAP` | `500` | Server-side hard cap on results per search |
| `MAX_PAGES` | `50` | Max listing pages fetched per search |
| `EPUB_TAG` | `Fanfiction` | Tag embedded in every downloaded EPUB's metadata (imported by Calibre); `""` disables |
| `FLAT_DOWNLOADS` | `false` (set `true` in compose) | Save files in the downloads root instead of per-search subfolders |
| `USER_AGENT` | Chrome UA | Outgoing User-Agent header |
| `DOWNLOADS_PATH` | `./downloads` | **Host** folder for e-books — point at Calibre's watch folder |
| `CONFIG_PATH` | `./config` | **Host** folder for `metadata.json`; keep outside the watch folder |
| `PORT` | `8067` | Host port for the web UI |
| `PUID` / `PGID` | `1000` / `1000` | User/group the app runs as — match `id -u` / `id -g` so files are owned by you |
| `TZ` | `Europe/Stockholm` | Container timezone |
| `DOWNLOADS_DIR` | `/app/downloads` | Download target *inside* the container (rarely changed) |
| `CONFIG_DIR` | `/app/config` | State location *inside* the container (rarely changed) |

## Calibre integration

The app is designed to feed [Calibre](https://calibre-ebook.com/)'s **automatic adding** feature: point Calibre's watched folder at the same directory as the app's downloads volume, and every downloaded work flows straight into your Calibre library tagged `Fanfiction`.

### 1. Point both apps at the same folder

In `.env`, point `DOWNLOADS_PATH` at the folder Calibre watches (do **not** edit `docker-compose.yml` — that would be overwritten by the next `git pull`):

```bash
DOWNLOADS_PATH=/Users/you/CalibreAutoAdd
CONFIG_PATH=./config          # app state — keep OUT of the watched folder
```

In Calibre: **Preferences → Import/export → Adding books → Automatic adding tab** → set "Specify a folder..." to that same folder.

Two settings in the compose file make this work (both on by default):

- `FLAT_DOWNLOADS: "true"` — files are saved directly in the downloads root instead of per-search subfolders. This is **required**: Calibre's automatic adding does not scan subfolders (and per its developer, never will).
- `EPUB_TAG: "Fanfiction"` — the tag is embedded inside every downloaded EPUB's metadata (as a `dc:subject` in the OPF). Set to `""` to disable.

### 2. How the tag reaches Calibre

Calibre's default behavior is to **read metadata from file contents** (Preferences → Import/export → Adding books — leave "Read metadata from file contents rather than file name" ticked). On import it picks up the embedded `dc:subject` entries and turns them into tags — so every book arrives tagged `Fanfiction`, with **no Calibre configuration needed**. AO3's EPUBs also carry the work's own tags (fandom, relationships, freeform tags) as subjects, so those import as Calibre tags too.

Note: setting tags from the *filename* is not possible in Calibre — its filename-pattern engine only supports title, author, series and a few other fields, not tags. Embedded metadata is the mechanism that works.

### 3. Housekeeping notes

- Calibre **removes files from the watched folder** after importing them. The app keeps its own memory in `./config/metadata.json`, so already-imported works are skipped ("downloaded earlier, per metadata") instead of re-downloaded. To deliberately re-download a work, delete its entry via the Library tab (or edit the file).
- **`metadata.json` is no longer written to the watched folder** — it lives in the separate `./config/` volume, so Calibre only ever finds e-books there.
- In-progress downloads briefly use a temporary `.part` file in the download folder before being atomically renamed into place, so a half-written book can never be imported. Calibre's automatic adding only picks up known e-book extensions by default and ignores these. If you enable **"add all file types"** in the Automatic adding tab, add `part` to the ignored extensions.
- The Library tab keeps listing works after Calibre has imported them (marked **Imported**), because it is driven by `metadata.json` rather than by what is currently in the watch folder.
- The embedded tag only works for **EPUB** (the other formats are passed through untouched), so keep EPUB as the download format for the Calibre workflow.

## API (used by the UI, handy for scripting)

- `POST /api/search` — `{query, search_type, max_results, sort_by, complete_only, words_from, words_to, exclude_tags}`
- `POST /api/download` — enqueue selected works
- `GET /api/downloads` — library listing incl. full metadata entries
- `GET /api/downloads/{category}/{filename}` — download a stored file
- `DELETE /api/downloads/{category}/{filename}` — delete a file **and** its metadata entry
- `GET /api/events` — SSE stream (log, progress, item_done, job_done, snapshot)

`metadata.json` entries for newly downloaded works also record `kudos`, `hits`, `bookmarks`, `chapters`, `rating` and `complete`; entries from older versions simply lack these fields and keep working.

## Notes & limitations

- **Public works only.** Restricted works (login required) are skipped with a clear log message.
- **The job queue is in-memory.** If the container restarts mid-job, the queue is lost — but downloaded files persist, and dedup makes re-enqueueing the same selection cheap.
- **Single process by design.** The rate limiter and job state live in one process; never run uvicorn with `--workers N`.
- **File ownership:** set `PUID`/`PGID` in `.env` to your own `id -u`/`id -g` so downloaded files belong to you. The container starts as root only to apply them, then drops to an unprivileged user via `gosu` — uvicorn never runs as root. To skip that entirely, add `user: "1000:1000"` to the compose service; the entrypoint detects it and steps aside.
- **Tailwind via CDN:** the UI loads Tailwind from `cdn.tailwindcss.com`, so the browser needs internet access at page load. Vendor a built CSS file into `app/static/` if you need fully offline operation.
- Filename collisions (same title + author, different work) are disambiguated with a ` [work_id]` suffix.

## Respect creators

This tool is intended for **personal archiving** of works you have access to. Please respect [AO3's Terms of Service](https://archiveofourown.org/tos) and the wishes of the creators whose works you download. Keep the rate limits generous — AO3 is a donation-funded nonprofit.

## License

Copyright (C) 2026 Henrik Engström

This program is free software: you can redistribute it and/or modify it under the terms of the **GNU General Public License version 3** as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the [GNU General Public License](LICENSE) for more details.

The full license text is in [LICENSE](LICENSE), or at <https://www.gnu.org/licenses/gpl-3.0.txt>.
