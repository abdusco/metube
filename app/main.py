#!/usr/bin/env python3

from __future__ import annotations

import json
import logging
import os
import re
import signal
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar, Literal
from urllib.parse import unquote

from bottle import Bottle, request, response, abort, static_file
import waitress
from pydantic import BaseModel, Field, PrivateAttr, ValidationError as PydanticValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic_settings.exceptions import SettingsError

from ytdl import DownloadQueueNotifier, DownloadQueue, Download

log = logging.getLogger("main")

_NIGHTLY_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_SUBTITLE_LANG_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?$")
_RESTART_FOR_UPDATE = False

VALID_DOWNLOAD_TYPES = frozenset({"video", "audio"})
VALID_VIDEO_CODECS = frozenset({"auto", "h264", "h265", "av1", "vp9"})
VALID_VIDEO_FORMATS = frozenset({"any", "mp4", "ios"})
VALID_AUDIO_FORMATS = frozenset({"m4a", "mp3", "opus", "wav", "flac"})
VALID_VIDEO_QUALITIES = frozenset({"best", "worst", "2160", "1440", "1080", "720", "480", "360", "240"})


def seconds_until_next_daily_time(time_hhmm: str, now: datetime | None = None) -> float:
    """Seconds until the next occurrence of HH:MM in local time."""
    now = now or datetime.now()
    hour, minute = map(int, time_hhmm.split(":"))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def parse_log_level(level: Any) -> int | None:
    if not isinstance(level, str):
        return None
    return getattr(logging, level.upper(), None)


if not logging.getLogger().hasHandlers():
    logging.basicConfig(level=parse_log_level(os.environ.get("LOGLEVEL", "INFO")) or logging.INFO)


class Config(BaseSettings):
    model_config = SettingsConfigDict(case_sensitive=True, extra="ignore")

    DOWNLOAD_DIR: str = "."
    AUDIO_DOWNLOAD_DIR: str = ""
    TEMP_DIR: str = ""
    DELETE_FILE_ON_TRASHCAN: bool = False
    STATE_DIR: str = "."
    PUBLIC_HOST_URL: str = "download/"
    PUBLIC_HOST_AUDIO_URL: str = "audio_download/"
    OUTPUT_TEMPLATE: str = "%(uploader)s -- @%(extractor)s -- %(title)s -- %(upload_date>%Y-%m-%d)s.%(ext)s"
    OUTPUT_TEMPLATE_PLAYLIST: str = "%(playlist_title)s/%(title)s.%(ext)s"
    OUTPUT_TEMPLATE_CHANNEL: str = "%(channel)s/%(title)s.%(ext)s"
    CLEAR_COMPLETED_AFTER: int = 0
    YTDL_OPTIONS: dict[str, Any] = Field(default_factory=dict)
    YTDL_OPTIONS_FILE: str = ""
    ALLOW_YTDL_OPTIONS_OVERRIDES: bool = False
    CORS_ALLOWED_ORIGINS: str = ""
    HOST: str = "0.0.0.0"
    PORT: int = 8081
    BASE_DIR: str = ""
    MAX_CONCURRENT_DOWNLOADS: int = 3
    LOGLEVEL: str = "INFO"
    YTDL_NIGHTLY_UPDATE_TIME: str = ""

    _runtime_overrides: dict[str, Any] = PrivateAttr(default_factory=dict)
    _ytdl_options_base: dict[str, Any] = PrivateAttr(default_factory=dict)

    _FRONTEND_KEYS: ClassVar[tuple[str, ...]] = (
        "PUBLIC_HOST_URL",
        "PUBLIC_HOST_AUDIO_URL",
        "ALLOW_YTDL_OPTIONS_OVERRIDES",
    )

    def __init__(self, **kwargs: Any) -> None:
        try:
            super().__init__(**kwargs)
        except PydanticValidationError as exc:
            for err in exc.errors(include_url=False):
                field = ".".join(str(p) for p in err.get("loc", ())) or "unknown"
                log.error("Config error [%s]: %s", field, err["msg"])
            sys.exit(1)
        except SettingsError as exc:
            log.error("Config error: %s", exc)
            sys.exit(1)

    @field_validator("PORT")
    @classmethod
    def _valid_port(cls, v: int) -> int:
        if not (1 <= v <= 65535):
            raise ValueError("must be between 1 and 65535")
        return v

    @field_validator("MAX_CONCURRENT_DOWNLOADS")
    @classmethod
    def _positive_downloads(cls, v: int) -> int:
        if v < 1:
            raise ValueError("must be >= 1")
        return v

    @field_validator("CLEAR_COMPLETED_AFTER")
    @classmethod
    def _non_negative_clear(cls, v: int) -> int:
        if v < 0:
            raise ValueError("must be >= 0")
        return v

    @field_validator("YTDL_NIGHTLY_UPDATE_TIME")
    @classmethod
    def _valid_nightly_time(cls, v: str) -> str:
        if v and not _NIGHTLY_TIME_RE.match(v):
            raise ValueError("must be HH:MM (24-hour format)")
        return v

    @field_validator("YTDL_OPTIONS_FILE")
    @classmethod
    def _resolve_options_file(cls, v: str) -> str:
        if v and v.startswith("."):
            return str(Path(v).resolve())
        return v

    @model_validator(mode="after")
    def _post_init(self) -> "Config":
        if not self.AUDIO_DOWNLOAD_DIR:
            self.AUDIO_DOWNLOAD_DIR = self.DOWNLOAD_DIR
        if not self.TEMP_DIR:
            self.TEMP_DIR = self.DOWNLOAD_DIR
        if not self.PUBLIC_HOST_AUDIO_URL and self.PUBLIC_HOST_URL:
            self.PUBLIC_HOST_AUDIO_URL = "audio_download/"
        for attr in ("PUBLIC_HOST_URL", "PUBLIC_HOST_AUDIO_URL"):
            val = getattr(self, attr)
            if val and not val.endswith("/"):
                setattr(self, attr, val + "/")
        if self.YTDL_OPTIONS_FILE:
            log.info('Loading yt-dlp custom options from "%s"', self.YTDL_OPTIONS_FILE)
            path = Path(self.YTDL_OPTIONS_FILE)
            if not path.exists():
                raise ValueError(f'YTDL_OPTIONS_FILE: file "{self.YTDL_OPTIONS_FILE}" not found')
            try:
                with path.open() as f:
                    opts = json.load(f)
                if not isinstance(opts, dict):
                    raise ValueError("YTDL_OPTIONS_FILE: contents must be a JSON object")
            except json.JSONDecodeError as exc:
                raise ValueError("YTDL_OPTIONS_FILE: not valid JSON") from exc
            self.YTDL_OPTIONS.update(opts)
        self._ytdl_options_base = dict(self.YTDL_OPTIONS)
        return self

    def set_runtime_override(self, key: str, value: Any) -> None:
        self._runtime_overrides[key] = value
        self.YTDL_OPTIONS[key] = value

    def remove_runtime_override(self, key: str) -> None:
        self._runtime_overrides.pop(key, None)
        self.YTDL_OPTIONS.pop(key, None)

    def _apply_runtime_overrides(self) -> None:
        self.YTDL_OPTIONS.update(self._runtime_overrides)

    def load_ytdl_options(self) -> tuple[bool, str]:
        self.YTDL_OPTIONS = dict(self._ytdl_options_base)
        self._apply_runtime_overrides()
        return (True, "")

    def frontend_safe(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self._FRONTEND_KEYS}


config = Config()
logging.getLogger().setLevel(parse_log_level(str(config.LOGLEVEL)) or logging.INFO)


class AddRequest(BaseModel):
    url: str
    download_type: str
    quality: str
    format: str
    codec: str = "auto"
    folder: str | None = None
    subtitle_langs: list[str] = Field(default_factory=list)
    ytdl_options_overrides: dict[str, Any] = Field(default_factory=dict)

    @field_validator("url")
    @classmethod
    def _nonempty_url(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("must not be empty")
        return v.strip()

    @field_validator("download_type")
    @classmethod
    def _valid_type(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in VALID_DOWNLOAD_TYPES:
            raise ValueError(f"must be one of {sorted(VALID_DOWNLOAD_TYPES)}")
        return v

    @field_validator("codec")
    @classmethod
    def _valid_codec(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in VALID_VIDEO_CODECS:
            raise ValueError(f"must be one of {sorted(VALID_VIDEO_CODECS)}")
        return v

    @field_validator("subtitle_langs")
    @classmethod
    def _valid_langs(cls, v: list[str]) -> list[str]:
        for code in v:
            if not _SUBTITLE_LANG_RE.match(code):
                raise ValueError(f"invalid subtitle language code: {code!r}")
        return v

    @field_validator("ytdl_options_overrides", mode="before")
    @classmethod
    def _parse_overrides_json(cls, v: Any) -> dict:
        if v is None or v == "":
            return {}
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
            except json.JSONDecodeError as exc:
                raise ValueError("must be valid JSON") from exc
            if not isinstance(parsed, dict):
                raise ValueError("must be a JSON object")
            return parsed
        return v

    @model_validator(mode="after")
    def _validate_type_specific(self) -> "AddRequest":
        dt = self.download_type
        fmt = (self.format or "").strip().lower()
        qual = (self.quality or "").strip().lower()

        if dt == "video":
            if fmt not in VALID_VIDEO_FORMATS:
                raise ValueError(f"format must be one of {sorted(VALID_VIDEO_FORMATS)} for video")
            if qual not in VALID_VIDEO_QUALITIES:
                raise ValueError(f"quality must be one of {sorted(VALID_VIDEO_QUALITIES)} for video")
            self.format = fmt
            self.quality = qual
        elif dt == "audio":
            if fmt not in VALID_AUDIO_FORMATS:
                raise ValueError(f"format must be one of {sorted(VALID_AUDIO_FORMATS)} for audio")
            allowed = {"best"}
            if fmt == "mp3":
                allowed |= {"320", "192", "128"}
            elif fmt == "m4a":
                allowed |= {"192", "128"}
            if qual not in allowed:
                raise ValueError(f"quality must be one of {sorted(allowed)} for {fmt}")
            self.format = fmt
            self.quality = qual
            self.codec = "auto"
        return self


class DeleteRequest(BaseModel):
    ids: list[str]
    where: Literal["queue", "done"]


class StatusResponse(BaseModel):
    status: Literal["ok", "error"] = "ok"
    msg: str | None = None


class CookieStatusResponse(BaseModel):
    has_cookies: bool


class ConfigurationResponse(BaseModel):
    ALLOW_YTDL_OPTIONS_OVERRIDES: bool
    PUBLIC_HOST_URL: str
    PUBLIC_HOST_AUDIO_URL: str


def _is_within_state_dir(target: str | Path) -> bool:
    return Path(target).is_relative_to(Path(config.STATE_DIR).resolve())


app = Bottle()
_cors_origins = [o.strip() for o in config.CORS_ALLOWED_ORIGINS.split(",") if o.strip()] if config.CORS_ALLOWED_ORIGINS else []


def _json(data: Any, status: int = 200) -> str:
    if status != 200:
        response.status = status
    response.content_type = "application/json"
    return json.dumps(data)


def _read_json() -> dict:
    try:
        body = request.body.read()
        post = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        abort(400, "Invalid JSON request body")
    if not isinstance(post, dict):
        abort(400, "JSON request body must be an object")
    return post


def _first_validation_error(exc: PydanticValidationError) -> str:
    errors = exc.errors(include_url=False)
    return errors[0]["msg"] if errors else "invalid request"


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------


@app.hook("after_request")
def _add_cors_headers():
    origin = request.headers.get("Origin", "")
    if origin and _cors_origins and ("*" in _cors_origins or origin in _cors_origins):
        response.set_header("Access-Control-Allow-Origin", origin)
        response.set_header("Access-Control-Allow-Headers", "Content-Type")
        response.set_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")


@app.route("<path:path>", method="OPTIONS")
def _preflight(path):
    return _json({})


@app.route("/", method="OPTIONS")
def _preflight_root():
    return _json({})


# ---------------------------------------------------------------------------
# Notifier
# ---------------------------------------------------------------------------


class Notifier(DownloadQueueNotifier):
    def added(self, dl):
        log.info(f"Notifier: Download added - {dl.title}")

    def updated(self, dl):
        log.debug(f"Notifier: Download updated - {dl.title}")

    def completed(self, dl):
        log.info(f"Notifier: Download completed - {dl.title}")

    def canceled(self, id):
        log.info(f"Notifier: Download canceled - {id}")

    def cleared(self, id):
        log.info(f"Notifier: Download cleared - {id}")


dqueue = DownloadQueue(config, Notifier())

COOKIES_PATH = Path(config.STATE_DIR) / "cookies.txt"


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


@app.route("/add", method="POST")
def add():
    post = _read_json()
    try:
        req = AddRequest.model_validate(post)
    except PydanticValidationError as exc:
        abort(400, _first_validation_error(exc))

    if req.ytdl_options_overrides and not config.ALLOW_YTDL_OPTIONS_OVERRIDES:
        abort(400, "ytdl_options_overrides are disabled")

    log.info(
        "Add download request: type=%s quality=%s format=%s has_folder=%s",
        req.download_type,
        req.quality,
        req.format,
        bool(req.folder),
    )
    status = dqueue.add(
        url=req.url,
        download_type=req.download_type,
        codec=req.codec,
        format=req.format,
        quality=req.quality,
        folder=req.folder,
        subtitle_langs=req.subtitle_langs,
        ytdl_options_overrides=req.ytdl_options_overrides,
    )
    return _json(status)


@app.route("/delete", method="POST")
def delete():
    post = _read_json()
    try:
        req = DeleteRequest.model_validate(post)
    except PydanticValidationError as exc:
        abort(400, _first_validation_error(exc))
    status = dqueue.cancel(req.ids) if req.where == "queue" else dqueue.clear(req.ids)
    log.info(f"Download delete request processed for ids: {req.ids}, where: {req.where}")
    return _json(status)


@app.route("/cookies", method="POST")
def upload_cookies():
    upload = request.files.get("cookies")
    if upload is None:
        return _json(StatusResponse(status="error", msg="No cookies file provided").model_dump(), 400)

    content = upload.file.read()
    max_size = 1_000_000
    if len(content) > max_size:
        return _json(StatusResponse(status="error", msg="Cookie file too large (max 1MB)").model_dump(), 400)

    tmp_cookie_path = COOKIES_PATH.with_name(COOKIES_PATH.name + ".tmp")
    tmp_cookie_path.write_bytes(content)
    try:
        tmp_cookie_path.chmod(0o600)
    except OSError as exc:
        log.warning(f"Could not restrict permissions on cookies file: {exc}")
    tmp_cookie_path.replace(COOKIES_PATH)
    config.set_runtime_override("cookiefile", str(COOKIES_PATH))
    size = len(content)
    log.info(f"Cookies file uploaded ({size} bytes)")
    return _json(StatusResponse(msg=f"Cookies uploaded ({size} bytes)").model_dump())


@app.route("/cookies", method="DELETE")
def delete_cookies():
    has_uploaded_cookies = COOKIES_PATH.exists()
    configured_cookiefile = config.YTDL_OPTIONS.get("cookiefile")
    has_manual_cookiefile = isinstance(configured_cookiefile, str) and configured_cookiefile and configured_cookiefile != str(COOKIES_PATH)

    if not has_uploaded_cookies:
        if has_manual_cookiefile:
            return _json(
                StatusResponse(
                    status="error",
                    msg="Cookies are configured manually via YTDL_OPTIONS (cookiefile). Remove or change that setting manually; UI delete only removes uploaded cookies.",
                ).model_dump(),
                400,
            )
        return _json(StatusResponse(status="error", msg="No uploaded cookies to delete").model_dump(), 400)

    COOKIES_PATH.unlink()
    config.remove_runtime_override("cookiefile")
    success, msg = config.load_ytdl_options()
    if not success:
        log.error(f"Cookies file deleted, but failed to reload YTDL_OPTIONS: {msg}")
        return _json(StatusResponse(status="error", msg=f"Cookies file deleted, but failed to reload YTDL_OPTIONS: {msg}").model_dump(), 500)

    log.info("Cookies file deleted")
    return _json(StatusResponse().model_dump())


@app.route("/cookies")
def cookie_status():
    configured_cookiefile = config.YTDL_OPTIONS.get("cookiefile")
    has_configured_cookies = isinstance(configured_cookiefile, str) and Path(configured_cookiefile).exists()
    has_uploaded_cookies = COOKIES_PATH.exists()
    return _json(CookieStatusResponse(has_cookies=has_uploaded_cookies or has_configured_cookies).model_dump())


@app.route("/queue")
def queue_state():
    queue, done = dqueue.get()
    return _json(
        [
            [[k, info.to_public_dict()] for k, info in queue],
            [[k, info.to_public_dict()] for k, info in done],
        ]
    )


@app.route("/logs")
def get_logs():
    dl_id = request.query.get("id")
    if not dl_id:
        abort(400, "missing id")
    dl = dqueue.queue.dict.get(dl_id) or dqueue.done.dict.get(dl_id)
    lines = dl.info.logs if dl is not None else []
    return _json(lines)


@app.route("/configuration")
def configuration():
    return _json(ConfigurationResponse(**config.frontend_safe()).model_dump())


# ---------------------------------------------------------------------------
# Static file routes
# ---------------------------------------------------------------------------


@app.route("/download/<filepath:path>")
def serve_download(filepath):
    target = (Path(config.DOWNLOAD_DIR) / unquote(filepath)).resolve()
    if _is_within_state_dir(target):
        abort(404)
    return static_file(filepath, root=config.DOWNLOAD_DIR)


@app.route("/audio_download/<filepath:path>")
def serve_audio_download(filepath):
    target = (Path(config.AUDIO_DOWNLOAD_DIR) / unquote(filepath)).resolve()
    if _is_within_state_dir(target):
        abort(404)
    return static_file(filepath, root=config.AUDIO_DOWNLOAD_DIR)


@app.route("/")
def index():
    return static_file("index.html", root=str(Path(config.BASE_DIR) / "ui"))


@app.route("/<filepath:path>")
def static_ui(filepath):
    return static_file(filepath, root=str(Path(config.BASE_DIR) / "ui"))


# ---------------------------------------------------------------------------
# Nightly update
# ---------------------------------------------------------------------------


def _start_nightly_update_thread():
    global _RESTART_FOR_UPDATE

    def _run():
        global _RESTART_FOR_UPDATE
        time_hhmm = config.YTDL_NIGHTLY_UPDATE_TIME
        delay = seconds_until_next_daily_time(time_hhmm)
        log.info("Next yt-dlp nightly update in %.0f seconds (at %s local time)", delay, time_hhmm)
        time.sleep(delay)
        log.info("Scheduled yt-dlp nightly update: requesting restart")
        _RESTART_FOR_UPDATE = True
        os.kill(os.getpid(), signal.SIGTERM)

    t = threading.Thread(target=_run, daemon=True, name="nightly-update")
    t.start()


if __name__ == "__main__":
    logging.getLogger().setLevel(parse_log_level(config.LOGLEVEL) or logging.INFO)
    log.info(f"Listening on {config.HOST}:{config.PORT}")

    dqueue.initialize()

    if COOKIES_PATH.exists():
        config.set_runtime_override("cookiefile", str(COOKIES_PATH))
        log.info(f"Cookie file detected at {COOKIES_PATH}")

    if config.YTDL_NIGHTLY_UPDATE_TIME:
        _start_nightly_update_thread()

    try:
        waitress.serve(app, host=config.HOST, port=int(config.PORT), threads=8)
    finally:
        Download.shutdown_manager()

    if _RESTART_FOR_UPDATE:
        sys.exit(42)
