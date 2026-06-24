#!/usr/bin/env python3

from __future__ import annotations

import logging
import os
import re
import signal
import sys
import threading
import time
import json
from collections import defaultdict
from datetime import datetime, timedelta
from functools import wraps
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

from bottle import Bottle, abort, request, response, static_file
from pydantic import ValidationError as PydanticValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic_settings.exceptions import SettingsError
import waitress

from job_manager import JobManager
from job_models import AddJobRequest, CookieStatusResponse, CreateJobResponse, StatusResponse

log = logging.getLogger("main")

_NIGHTLY_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_RESTART_FOR_UPDATE = False


def seconds_until_next_daily_time(time_hhmm: str, now: datetime | None = None) -> float:
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
    TEMP_DIR: str = ""
    DELETE_FILE_ON_TRASHCAN: bool = False
    STATE_DIR: str = "."
    PUBLIC_HOST_URL: str = "download/"
    OUTPUT_TEMPLATE: str = "%(uploader)s -- @%(extractor)s -- %(title)s -- %(upload_date>%Y-%m-%d)s.%(ext)s"
    YTDL_OPTIONS: dict[str, Any] = {}
    CORS_ALLOWED_ORIGINS: str = ""
    HOST: str = "0.0.0.0"
    PORT: int = 8081
    BASE_DIR: str = ""
    MAX_CONCURRENT_DOWNLOADS: int = 3
    LOGLEVEL: str = "INFO"
    YTDL_NIGHTLY_UPDATE_TIME: str = ""

    @field_validator("PORT")
    @classmethod
    def _valid_port(cls, value: int) -> int:
        if not (1 <= value <= 65535):
            raise ValueError("must be between 1 and 65535")
        return value

    @field_validator("MAX_CONCURRENT_DOWNLOADS")
    @classmethod
    def _valid_concurrency(cls, value: int) -> int:
        if value < 1:
            raise ValueError("must be >= 1")
        return value

    @field_validator("YTDL_NIGHTLY_UPDATE_TIME")
    @classmethod
    def _valid_nightly_time(cls, value: str) -> str:
        if value and not _NIGHTLY_TIME_RE.match(value):
            raise ValueError("must be HH:MM (24-hour format)")
        return value

    def model_post_init(self, __context: Any) -> None:
        if not self.TEMP_DIR:
            self.TEMP_DIR = self.DOWNLOAD_DIR
        if self.PUBLIC_HOST_URL and not self.PUBLIC_HOST_URL.endswith("/"):
            self.PUBLIC_HOST_URL += "/"

def _load_config() -> Config:
    try:
        return Config()
    except (PydanticValidationError, SettingsError) as exc:
        log.error("Config error: %s", exc)
        sys.exit(1)


config = _load_config()
logging.getLogger().setLevel(parse_log_level(str(config.LOGLEVEL)) or logging.INFO)

app = Bottle()
_cors_origins = [o.strip() for o in config.CORS_ALLOWED_ORIGINS.split(",") if o.strip()] if config.CORS_ALLOWED_ORIGINS else []
job_manager = JobManager(config)

def _parse_cookies_by_domain(content: str) -> dict[str, str]:
    by_domain: dict[str, list[str]] = defaultdict(list)
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split("\t")
        if len(parts) >= 7:
            domain = parts[0].lstrip(".")
            by_domain[domain].append(line)
    return {d: "\n".join(lines) for d, lines in by_domain.items()}


def _error_message(err: Any, fallback: str) -> str:
    body = getattr(err, "body", None)
    if isinstance(body, str) and body and not body.lstrip().startswith("<"):
        return body
    return fallback


def _json_error(message: str, status: int) -> dict[str, Any]:
    response.status = status
    response.content_type = "application/json"
    return json.dumps(StatusResponse(status="error", message=message).model_dump())


def _no_content() -> str:
    response.status = HTTPStatus.NO_CONTENT
    response.content_type = "application/json"
    return ""


def _require_json_object(model: Any | None = None):
    def _decorator(fn):
        @wraps(fn)
        def _wrapped(*args, **kwargs):
            payload = request.json
            if payload is None:
                abort(400, "Invalid JSON request body")
            if not isinstance(payload, dict):
                abort(400, "JSON request body must be an object")
            if model is None:
                kwargs["payload"] = payload
            else:
                try:
                    kwargs["payload"] = model.model_validate(payload)
                except PydanticValidationError as exc:
                    abort(400, _first_validation_error(exc))
            return fn(*args, **kwargs)

        return _wrapped

    return _decorator


def _first_validation_error(exc: PydanticValidationError) -> str:
    errors = exc.errors(include_url=False)
    return errors[0]["msg"] if errors else "invalid request"


def _is_within_state_dir(target: str | Path) -> bool:
    return Path(target).is_relative_to(Path(config.STATE_DIR).resolve())


@app.hook("after_request")
def _add_cors_headers() -> None:
    origin = request.headers.get("Origin", "")
    if origin and _cors_origins and ("*" in _cors_origins or origin in _cors_origins):
        response.set_header("Access-Control-Allow-Origin", origin)
        response.set_header("Access-Control-Allow-Headers", "Content-Type")
        response.set_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")


def _default_error_handler(err: Any) -> dict[str, Any]:
    code = int(getattr(err, "status_code", 500) or 500)
    fallback = HTTPStatus(code).phrase if code in HTTPStatus._value2member_map_ else "Internal Server Error"
    return _json_error(_error_message(err, fallback), status=code)


app.default_error_handler = _default_error_handler


@app.route("<path:path>", method="OPTIONS")
def _preflight(path: str) -> dict[str, Any]:
    del path
    return {}


@app.route("/", method="OPTIONS")
def _preflight_root() -> dict[str, Any]:
    return {}


@app.route("/jobs", method="POST")
@_require_json_object(AddJobRequest)
def create_job(payload: AddJobRequest) -> dict[str, Any]:
    result = job_manager.enqueue(payload.to_job_create())
    return CreateJobResponse(id=result.id).model_dump()


@app.route("/jobs/<job_id>", method="DELETE")
def cancel_job(job_id: str) -> str:
    job_manager.cancel(job_id)
    return _no_content()


@app.route("/jobs/clear", method="POST")
def clear_jobs() -> str:
    job_manager.clear()
    return _no_content()


@app.route("/jobs")
def jobs_state() -> dict[str, Any]:
    base = config.PUBLIC_HOST_URL
    job_list = job_manager.get_jobs()
    for job in job_list.queued + job_list.done:
        if job.filename:
            job.download_url = base + quote(job.filename)
            for sf in job.subtitle_files:
                sf.download_url = base + quote(sf.filename)
    return job_list.model_dump(mode="json")


@app.route("/logs")
def get_logs() -> list[str]:
    job_id = request.query.get("id")
    if not job_id:
        abort(400, "missing id")
    return job_manager.get_logs(job_id)



@app.route("/cookies", method="POST")
def upload_cookies() -> dict[str, Any]:
    upload = request.files.get("cookies")
    if upload is None:
        abort(400, "No cookies file provided")

    content = upload.file.read()
    if len(content) > 1_000_000:
        abort(400, "Cookie file too large (max 1MB)")

    text = content.decode("utf-8", errors="replace")
    by_domain = _parse_cookies_by_domain(text)
    if not by_domain:
        abort(400, "No valid cookie entries found")

    for domain, domain_content in by_domain.items():
        job_manager.upsert_cookies_for_domain(domain, domain_content)

    return StatusResponse(message=f"Cookies saved for {len(by_domain)} domain(s)").model_dump()


@app.route("/cookies/<domain>", method="DELETE")
def delete_cookies_for_domain(domain: str) -> str:
    job_manager.delete_cookies_for_domain(unquote(domain))
    return _no_content()


@app.route("/cookies", method="DELETE")
def delete_all_cookies() -> str:
    job_manager.delete_all_cookies()
    return _no_content()


@app.route("/cookies")
def cookie_status() -> dict[str, Any]:
    return CookieStatusResponse(domains=job_manager.list_cookie_domains()).model_dump()


@app.route("/download/<filepath:path>")
def serve_download(filepath: str):
    target = (Path(config.DOWNLOAD_DIR) / unquote(filepath)).resolve()
    if _is_within_state_dir(target):
        abort(404)
    return static_file(filepath, root=config.DOWNLOAD_DIR)


@app.route("/")
def index():
    return static_file("index.html", root=str(Path(config.BASE_DIR) / "ui"))


@app.route("/<filepath:path>")
def static_ui(filepath: str):
    return static_file(filepath, root=str(Path(config.BASE_DIR) / "ui"))


def _start_nightly_update_thread() -> None:
    global _RESTART_FOR_UPDATE

    def _run() -> None:
        global _RESTART_FOR_UPDATE
        delay = seconds_until_next_daily_time(config.YTDL_NIGHTLY_UPDATE_TIME)
        time.sleep(delay)
        _RESTART_FOR_UPDATE = True
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=_run, daemon=True, name="nightly-update").start()


if __name__ == "__main__":
    log.info("Listening on %s:%s", config.HOST, config.PORT)
    if config.YTDL_NIGHTLY_UPDATE_TIME:
        _start_nightly_update_thread()
    try:
        waitress.serve(app, host=config.HOST, port=int(config.PORT), threads=8)
    finally:
        job_manager.shutdown()

    if _RESTART_FOR_UPDATE:
        sys.exit(42)
