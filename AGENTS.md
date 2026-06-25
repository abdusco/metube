# Agent Guidelines

## README.md size constraint

The README.md is synced to Docker Hub, which has a **25,000 character limit**.
Any change to README.md **must** keep the file under 25,000 characters (`wc -c README.md`).
If an addition would exceed the limit, trim existing prose elsewhere — prefer tightening verbose descriptions over removing sections.

## Tech stack

- **Backend:** Python 3.13+, Bottle + Waitress (not aiohttp), yt-dlp[default,curl-cffi,deno], Pydantic v2, pydantic-settings
- **Database:** SQLite in WAL mode — all state in `jobs.sqlite3` (jobs, cookies, logs in separate tables). JSONB columns require SQLite ≥ 3.45; the Docker image bundles Python 3.13 with a compatible SQLite. On the host machine the system sqlite3 binary may be older — always run DB queries via Python inside the container.
- **Frontend:** Alpine.js 3 (CDN), plain HTML/CSS/JS — no build step, no framework
- **Package manager:** uv (Python only)
- **Container:** Single-stage Python 3.13 slim, uv installed via `COPY --from=ghcr.io/astral-sh/uv:latest`, multi-arch (amd64/arm64), published to `ghcr.io`

## Build & test commands

```bash
# Backend (run from repo root)
uv sync --frozen --group dev
python -m compileall app
uv run pytest app/tests/
```

No frontend build step — `ui/index.html`, `ui/main.js`, and `ui/styles.css` are served as-is.

All backend commands run in CI (`.github/workflows/main.yml`) on every push to master and must pass.
CI runs two jobs sequentially: `test` (pytest) then `publish` (Docker multi-arch build + push to ghcr.io).

## Code style

Follow `.editorconfig`:
- Python: 4-space indent
- Everything else (YAML, JSON, HTML, JS, CSS): 2-space indent
- UTF-8, LF line endings, trim trailing whitespace, final newline

## Project structure

```
app/main.py          — HTTP server (Bottle/Waitress), REST API routes, Config class
app/job_manager.py   — Job lifecycle: enqueue, schedule, cancel, retry; title extraction
app/job_worker.py    — yt-dlp invocation, progress/postprocessor hooks, cookie temp files
app/job_db.py        — SQLite job persistence (WAL, JSONB columns for arrays)
app/cookies_db.py    — Per-domain cookie storage in SQLite (Netscape format)
app/logs_db.py       — yt-dlp log line persistence in SQLite
app/job_models.py    — Pydantic models: Job, JobStatus, AddJobRequest, SubtitleFile, etc.
app/dl_formats.py    — yt-dlp format selectors and postprocessor config
app/tests/           — pytest tests (no asyncio; conftest.py sets up a temp filesystem env)
ui/index.html        — Single-page app (Alpine.js 3)
ui/main.js           — Alpine component, HTTP polling, JSDoc @typedef
ui/styles.css        — CSS custom properties, light/dark theme
```

## Key conventions

### Configuration
All config comes from environment variables via the `Config` class (`app/main.py`, Pydantic `BaseSettings`). New env vars go there. `TEMP_DIR` defaults to `DOWNLOAD_DIR` when blank.

### API & real-time updates
REST API served by Bottle. Real-time updates use HTTP polling: the frontend calls `GET /jobs` every ~2 seconds. There is no WebSocket.

### SQLite JSONB
Array columns (`subtitle_langs_json`, `subtitle_files_json`, `temp_files_json`) are stored as JSONB. Always:
- Read with `json(col)` (converts JSONB → text JSON) — see `_SELECT_COLUMNS` in `job_db.py`
- Write with `jsonb(?)` and pass a JSON string — see every `UPDATE` in `job_db.py`
- All three DB classes (`JobDB`, `CookieDB`, `LogsDB`) share the same `jobs.sqlite3` file and each hold their own `threading.RLock()`.

### Cookie handling
Cookies are stored per-domain in `CookieDB`. Both the title-extraction phase (`JobManager._extract_title`) and the actual download (`run_job` in `job_worker.py`) call `CookieDB.get_merged_content()`, write the result to a `NamedTemporaryFile`, and pass `cookiefile` to yt-dlp. The temp file is deleted automatically when the `with` block exits.

### yt-dlp postprocessors
`run_job` in `job_worker.py` inserts `_ThumbnailExtFixerPP` at position 0 of `ydl._pps["before_dl"]` before starting the download. This PP detects thumbnails whose file extension doesn't match their actual image format (e.g. JPEG content served as `.png`, common on Reddit) and renames them before `FFmpegThumbnailsConvertor` runs — which forces `-f image2` and would otherwise pick the wrong decoder.

### Subtitle files
`FFmpegEmbedSubtitle` is configured with `already_have_subtitle=True` (in `dl_formats.py`). This tells yt-dlp to embed subtitles without deleting the `.vtt` source files afterward, so the download URLs served by the API remain valid.

### Temp file tracking
In-progress download files (`.part`, thumbnails, subtitle intermediates) are tracked in `temp_files_json` in the jobs table. On job cancel, error, or server restart, `job_worker.py` deletes any listed files and `JobDB.reset_running_jobs_to_error()` marks interrupted jobs as errored.

### No pre-commit hooks
Linting and tests are enforced in CI only. Run `uv run pytest app/tests/` before pushing.
