# Agent Guidelines

## README.md size constraint

The README.md is synced to Docker Hub, which has a **25,000 character limit**.
Any change to README.md **must** keep the file under 25,000 characters (`wc -c README.md`).
If an addition would exceed the limit, trim existing prose elsewhere — prefer tightening verbose descriptions over removing sections.

## Tech stack

- **Backend:** Python 3.13+, aiohttp, yt-dlp
- **Frontend:** Alpine.js 3 (CDN), plain HTML/CSS/JS — no build step, no framework
- **Package manager:** uv (Python only)
- **Container:** Single-stage Python 3.13 slim, uv installed via `COPY --from=ghcr.io/astral-sh/uv:latest`, multi-arch (amd64/arm64)

## Build & test commands

```bash
# Backend (run from repo root)
uv sync --frozen --group dev
python -m compileall app
uv run pytest app/tests/
```

No frontend build step — `ui/index.html`, `ui/main.js`, and `ui/styles.css` are served as-is.

All backend commands run in CI (`.github/workflows/main.yml`) on every push to master and must pass.

## Code style

Follow `.editorconfig`:
- Python: 4-space indent
- Everything else (YAML, JSON, HTML, JS, CSS): 2-space indent
- UTF-8, LF line endings, trim trailing whitespace, final newline

## Project structure

```
app/main.py        — HTTP server, REST API routes, Config class
app/ytdl.py        — Download queue logic, yt-dlp integration
app/state_store.py — JSON-based persistent storage with atomic writes
app/dl_formats.py  — Video/audio codec/quality mapping
app/tests/         — pytest tests (asyncio_mode=auto)
ui/index.html      — Single-page app (Alpine.js 3)
ui/main.js         — Alpine component, HTTP polling, JSDoc @typedef
ui/styles.css      — CSS custom properties, light/dark theme
```

## Key conventions

- Backend configuration lives in the `Config` class in `app/main.py` with env-var defaults in `_DEFAULTS`. New env vars go there.
- Real-time updates use HTTP polling: the frontend calls `GET /queue` every 2 seconds. There is no WebSocket or socket.io.
- `OUTPUT_TEMPLATE` always wins over any `outtmpl` key in `YTDL_OPTIONS`. In `ytdl.py` `_download()`, `**self.ytdl_opts` is spread first, then `outtmpl` is set after so it cannot be overridden.
- State is persisted as JSON files via `AtomicJsonStore` in `app/state_store.py`.
- No pre-commit hooks — linting and tests are enforced in CI only.
