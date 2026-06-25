# Agent Guidelines

## Tech stack

- **Backend:** Python 3.13+, Bottle + Waitress, yt-dlp[default,curl-cffi,deno], Pydantic v2, pydantic-settings
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

### Python
- Add type annotations, but be pragmatic — don't overcomplicate with `typing.Literal` or `typing.TypedDict` unless it adds value.
- Use `pathlib.Path` for filesystem paths, not `str`.
- Use `logging` for all logging, not `print()`.
- Use `with` context managers for file I/O and temp files.
- Use `tempfile.NamedTemporaryFile` for temporary files

### SQLite 
- Always use full type names instead of primitives, e.g. JSON over TEXT, DATETIME over TEXT, INTEGER over INT. This ensures compatibility with SQLite's type affinity rules.
- Use `jsonb` for JSON columns (requires SQLite ≥ 3.45). 

### No pre-commit hooks
Linting and tests are enforced in CI only. Run `uv run pytest app/tests/` before pushing.
