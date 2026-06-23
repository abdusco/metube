from __future__ import annotations

import logging
import os
from pathlib import Path
from threading import Event
from typing import Callable

import yt_dlp
import yt_dlp.networking.impersonate

from dl_formats import get_format, get_opts
from job_db import JobDB
from job_models import Job

log = logging.getLogger("job_worker")


def run_job(
    db: JobDB,
    job: Job,
    *,
    download_dir: Path,
    temp_dir: Path,
    output_template: str,
    ytdl_options: dict,
    cancel_event: Event,
    log_line: Callable[[str], None],
) -> None:
    opts = get_opts(job.download_type, job.format, job.quality, ytdl_options, subtitle_langs=job.subtitle_langs)
    fmt = get_format(job.download_type, job.codec, job.format, job.quality)

    if "impersonate" in opts:
        opts["impersonate"] = yt_dlp.networking.impersonate.ImpersonateTarget.from_str(opts["impersonate"])

    def progress_hook(status: dict) -> None:
        if cancel_event.is_set():
            raise RuntimeError("METUBE_JOB_CANCELED")
        if status.get("status") != "downloading":
            return
        downloaded = status.get("downloaded_bytes")
        total = status.get("total_bytes") or status.get("total_bytes_estimate")
        percent = None
        if downloaded is not None and total:
            percent = float(downloaded) / float(total) * 100
        db.update_progress(
            job.id,
            message=status.get("_default_template") or "downloading",
            percent=percent,
            speed=status.get("speed"),
            eta=status.get("eta"),
        )

    def postprocessor_hook(data: dict) -> None:
        if data.get("postprocessor") == "MoveFiles" and data.get("status") == "finished":
            info_dict = data.get("info_dict") or {}
            filepath = info_dict.get("filepath")
            if filepath:
                final_name = filepath
                if info_dict.get("__finaldir"):
                    final_name = str(Path(info_dict["__finaldir"]) / Path(filepath).name)
                rel = os.path.relpath(final_name, download_dir)
                size = Path(final_name).stat().st_size if Path(final_name).exists() else None
                db.set_output_file(job.id, rel, size)
            requested_subtitles = info_dict.get("requested_subtitles") or {}
            for subtitle in requested_subtitles.values():
                if isinstance(subtitle, dict) and subtitle.get("filepath"):
                    subtitle_path = subtitle["filepath"]
                    rel = os.path.relpath(subtitle_path, download_dir)
                    size = Path(subtitle_path).stat().st_size if Path(subtitle_path).exists() else None
                    db.add_subtitle_file(job.id, rel, size)

    class YtdlLogger:
        def debug(self, message: str) -> None:
            log_line(message)

        def info(self, message: str) -> None:
            log_line(message)

        def warning(self, message: str) -> None:
            log_line(f"[WARNING] {message}")

        def error(self, message: str) -> None:
            log_line(f"[ERROR] {message}")

    params = {
        **opts,
        "quiet": True,
        "verbose": False,
        "no_color": True,
        "paths": {"home": str(download_dir), "temp": str(temp_dir)},
        "outtmpl": {"default": output_template},
        "format": fmt,
        "socket_timeout": 30,
        "ignore_no_formats_error": True,
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [postprocessor_hook],
        "logger": YtdlLogger(),
    }

    try:
        result = yt_dlp.YoutubeDL(params=params).download([job.url])
    except Exception as exc:
        if cancel_event.is_set():
            db.mark_canceled(job.id)
            return
        message = str(exc)
        log.error("Job %s failed: %s", job.id, message)
        db.mark_error(job.id, message)
        return

    if cancel_event.is_set():
        db.mark_canceled(job.id)
        return
    if result == 0:
        db.mark_finished(job.id)
    else:
        db.mark_error(job.id, f"yt-dlp exited with code {result}")
