# MeTube — Feature Specs & Implementation Plan

## What it is

A web UI for downloading videos/audio via yt-dlp. Runs in Docker; TLS is terminated upstream by nginx/caddy. The backend is a bottle.py WSGI app served by waitress. No async anywhere — concurrency is handled by threads.

## Frontend

Vanilla Alpine.js app (`ui/`). No build step. Polls the backend over HTTP — no WebSockets.

---

## API contract

### Polling

| Method | Path | Notes |
|--------|------|-------|
| GET | `/queue` | Returns `[queue[], done[]]` — polled every 2 s |
| GET | `/logs?id={id}` | Returns `string[]` — polled every 5 s when log panel open |

### Download management

| Method | Path | Body |
|--------|------|------|
| POST | `/add` | `{url, download_type, quality, format, codec?, folder?, custom_name_prefix?, playlist_item_limit?, auto_start?, subtitle_langs?, ytdl_options_presets?, ytdl_options_overrides?}` |
| POST | `/delete` | `{ids: string[], where: "queue" \| "done"}` |

### Cookie management

| Method | Path | Notes |
|--------|------|-------|
| GET    | `/cookies` | `{has_cookies: bool}` |
| POST   | `/cookies` | FormData `cookies` field, max 1 MB |
| DELETE | `/cookies` | — |

### Info

| Method | Path | Response |
|--------|------|----------|
| GET | `/configuration` | `{ALLOW_YTDL_OPTIONS_OVERRIDES, PUBLIC_HOST_URL, PUBLIC_HOST_AUDIO_URL, DEFAULT_OPTION_PLAYLIST_ITEM_LIMIT}` |
| GET | `/presets` | `{presets: string[]}` |

### Static files

- `/download/<path>` → `DOWNLOAD_DIR`
- `/audio_download/<path>` → `AUDIO_DOWNLOAD_DIR`
- `/` → `ui/`

---

## Download object shape

```json
{
  "id": "string",
  "title": "string",
  "url": "string",
  "download_type": "video | audio | captions | thumbnail",
  "status": "pending | preparing | downloading | finished | error | scheduled",
  "percent": null,
  "speed": null,
  "eta": null,
  "filename": null,
  "size": null,
  "timestamp": null,
  "error": null,
  "folder": "string",
  "custom_name_prefix": "string",
  "subtitle_files": [],
  "chapter_files": []
}
```

---

## Environment variables

| Variable | Default | Notes |
|----------|---------|-------|
| `DOWNLOAD_DIR` | `.` | Where video files are saved |
| `AUDIO_DOWNLOAD_DIR` | `DOWNLOAD_DIR` | Where audio files are saved |
| `STATE_DIR` | `.` | Queue/done/pending JSON files |
| `TEMP_DIR` | `DOWNLOAD_DIR` | In-progress files |
| `DOWNLOAD_DIRS_INDEXABLE` | `false` | Allow directory listing |
| `DELETE_FILE_ON_TRASHCAN` | `false` | Delete file when clearing a done item |
| `MAX_CONCURRENT_DOWNLOADS` | `3` | Parallel download limit |
| `CLEAR_COMPLETED_AFTER` | `0` | Auto-clear delay in seconds (0 = off) |
| `DEFAULT_OPTION_PLAYLIST_ITEM_LIMIT` | `0` | Max playlist items (0 = unlimited) |
| `YTDL_OPTIONS` | `{}` | Global yt-dlp options JSON |
| `YTDL_OPTIONS_FILE` | | Path to yt-dlp options JSON file |
| `YTDL_OPTIONS_PRESETS` | `{}` | Named preset map JSON |
| `ALLOW_YTDL_OPTIONS_OVERRIDES` | `false` | Enable per-download overrides field |
| `YTDL_NIGHTLY_UPDATE_TIME` | | HH:MM for nightly yt-dlp update + restart |
| `OUTPUT_TEMPLATE` | (uploader/title/date) | yt-dlp output filename template |
| `OUTPUT_TEMPLATE_PLAYLIST` | `%(playlist_title)s/%(title)s.%(ext)s` | |
| `OUTPUT_TEMPLATE_CHANNEL` | `%(channel)s/%(title)s.%(ext)s` | |
| `HOST` | `0.0.0.0` | Bind address |
| `PORT` | `8081` | Bind port |
| `URL_PREFIX` | | Sub-path for reverse proxy deployments |
| `PUBLIC_HOST_URL` | `download/` | Base URL for video file links |
| `PUBLIC_HOST_AUDIO_URL` | `audio_download/` | Base URL for audio file links |
| `CORS_ALLOWED_ORIGINS` | | Comma-separated origins or `*` |
| `ROBOTS_TXT` | | Path to custom robots.txt |
| `LOGLEVEL` | `INFO` | Python log level |
| `DEFAULT_THEME` | `auto` | Theme cookie set for new visitors |
| `PUID` / `PGID` / `UMASK` | `1000/1000/022` | File ownership in Docker |

> **Removed from old backend**: `HTTPS`, `CERTFILE`, `KEYFILE` (TLS terminated upstream); `ENABLE_ACCESSLOG` (waitress logs unconditionally via stdlib `logging`).

---

## Architecture

- **Web server**: bottle.py WSGI app served by waitress (thread pool, default 4 threads)
- **Download execution**: one `multiprocessing.Process` per download; status is polled by a `threading.Thread` per active download
- **Concurrency limit**: `threading.Semaphore(MAX_CONCURRENT_DOWNLOADS)`
- **State protection**: `threading.Lock` on all `DownloadQueue` state mutations
- **State persistence**: atomic JSON writes to `STATE_DIR` (`queue.json`, `completed.json`, `pending.json`)
- **No asyncio anywhere**
- **No WebSockets**: frontend polls `/queue` every 2 s

### Thread map

| Thread | Role |
|--------|------|
| waitress pool (N threads) | serve HTTP requests |
| 1 per queued download | acquires semaphore, calls `download.start()`, runs cleanup |
| 1 per active download | polls `multiprocessing.Queue` for progress; updates `DownloadInfo`; calls notifier |
| 1 (daemon) | nightly yt-dlp update sleep + SIGTERM |
| 1 per auto-clear | sleeps `CLEAR_COMPLETED_AFTER` seconds, then removes from done list |

---

## Rewrite scope

### `app/ytdl.py` — drop asyncio, add threading

**`DownloadQueueNotifier`** — remove `async` from all five methods.

**`Download`**:
- Remove `self.loop`; add `self._status_thread`
- `async def start()` → `def start()`: spawn a daemon `threading.Thread` for status polling, call `self.proc.join()` (blocking — this thread is the per-download orchestration thread), then put `None` sentinel and join the status thread
- `async def update_status()` → `def _poll_status()`: same logic, `self.status_queue.get()` blocks naturally; notifier calls become direct sync calls

**`DownloadQueue`**:
- `asyncio.Semaphore` → `threading.Semaphore`
- Add `self._lock = threading.Lock()` — acquired in `add`, `cancel`, `clear`, `start_pending`, `_post_download_cleanup`
- `async def initialize()` → `def initialize()`: call `__add_download` directly for each saved queue item (no `create_task`); pending queue dropped
- `async def __start_download()` → `def _run_download()`: acquires `threading.Semaphore`; runs as daemon thread
- All `asyncio.create_task(self._run_download(dl))` → `threading.Thread(target=self._run_download, args=(dl,), daemon=True).start()`
- `async def __add_download()` → `def __add_download()` — `auto_start` param removed, always starts immediately
- `async def __add_entry()` → `def __add_entry()`
- `async def add()` → `def add()` (yt-dlp extract_info is blocking; fine since waitress uses threads)
- `async def cancel()` → `def cancel()`
- `async def clear()` → `def clear()`
- `async def __auto_clear_after_delay()` → daemon thread with `time.sleep(delay_seconds)` then `self.clear([url])` under lock
- **Drop**: `start_pending()`, `self.pending` queue, `pending.json` persistence, `cancel_add()`, `_add_generation` counter

### `app/main.py` — full rewrite with bottle

**Framework translation**:

| aiohttp | bottle |
|---------|--------|
| `@routes.get('/path')` | `@app.route('/path')` |
| `@routes.post('/path')` | `@app.route('/path', method='POST')` |
| `await request.json()` | `bottle.request.json` |
| `request.query.get('id')` | `bottle.request.query.get('id')` |
| `await request.multipart()` | `bottle.request.files.get('cookies')` |
| `request.cookies` | `bottle.request.cookies` |
| `web.Response(text=..., content_type='application/json')` | `bottle.response.content_type = 'application/json'; return ...` |
| `web.json_response({...})` | `return {...}` (bottle auto-serializes dicts) |
| `web.FileResponse(path)` | `return bottle.static_file(name, root=dir)` |
| `web.HTTPFound(url)` | `bottle.redirect(url)` |
| `web.HTTPBadRequest(reason=...)` | `bottle.abort(400, reason)` |
| `web.HTTPNotFound()` | `bottle.abort(404)` |
| `@web.middleware` | `@app.hook('before_request')` or explicit check in handler |
| `app.on_startup` | explicit call before `waitress.serve()` |
| `app.on_cleanup` | `try/finally` around `waitress.serve()` |

**CORS**:
```python
@app.hook('after_request')
def _cors_headers():
    origin = bottle.request.headers.get('Origin', '')
    if origin and ('*' in _cors_origins or origin in _cors_origins):
        bottle.response.headers['Access-Control-Allow-Origin'] = origin
        bottle.response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        bottle.response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'

@app.route('<path:path>', method='OPTIONS')
def _preflight(path):
    return {}
```

**State-dir guard** — inline check in `/download/` and `/audio_download/` handlers:
```python
@app.route('/download/<filepath:path>')
def serve_download(filepath):
    if _is_within_state_dir((Path(config.DOWNLOAD_DIR) / filepath).resolve()):
        bottle.abort(404)
    return bottle.static_file(filepath, root=config.DOWNLOAD_DIR)
```

**Startup / shutdown**:
```python
if __name__ == '__main__':
    dqueue.initialize()
    if COOKIES_PATH.exists():
        config.set_runtime_override('cookiefile', str(COOKIES_PATH))
    _start_nightly_update_thread()
    try:
        waitress.serve(app, host=config.HOST, port=int(config.PORT), threads=8)
    finally:
        Download.shutdown_manager()
    if _RESTART_FOR_UPDATE:
        sys.exit(42)
```

**Nightly restart** — daemon thread with `time.sleep`, then `os.kill(os.getpid(), signal.SIGTERM)`.

**`Notifier`** — implement `DownloadQueueNotifier` with plain `def` (just logging).

### `app/tests/` — drop asyncio, adapt for bottle

| File | Change |
|------|--------|
| `test_api.py` | Replace `web.Request` mocks with plain call + `pytest.raises(bottle.HTTPError)`; replace integration test with `webtest.TestApp(app)` |
| `test_download_queue.py` | Rewrite: remove `@pytest.mark.asyncio`, replace `await dqueue.add(...)` with `dqueue.add(...)`; mock notifier as plain sync class; remove pending/cancel-add/start-pending test cases |
| `test_nightly_update.py` | Rewrite: replace asyncio sleep mock with `threading` approach |
| `test_state_store.py` | Check for async — likely already sync, minimal change |
| `test_persistent_queue.py` | Check for async — likely already sync, minimal change |
| `test_main_helpers.py` | Replace `main.web.HTTPBadRequest` with `bottle.HTTPError` |
| `test_config.py` | No changes |
| `test_dl_formats.py` | No changes |
| `test_ytdl_utils.py` | No changes |

### `pyproject.toml`

Remove: `aiohttp`, `pytest-aiohttp`, `pytest-asyncio`  
Add: `bottle`, `waitress`, `webtest` (dev)  
Remove from `[tool.pytest.ini_options]`: `asyncio_mode = "auto"`

### Files with no changes

`state_store.py`, `dl_formats.py`, `ui/`, `Dockerfile`, `docker-entrypoint.sh`
