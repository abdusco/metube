from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from threading import Event
from typing import Callable

import yt_dlp
import yt_dlp.networking.impersonate
from yt_dlp.compat import imghdr as _imghdr
from yt_dlp.postprocessor.common import PostProcessor
from yt_dlp.utils import replace_extension

from dl_formats import get_format, get_opts
from job_db import JobDB
from job_models import Job

log = logging.getLogger("job_worker")

_IMGHDR_TO_EXT = {"jpeg": "jpg", "png": "png", "webp": "webp", "gif": "gif"}


class _ThumbnailExtFixerPP(PostProcessor):
    """Rename thumbnails whose file extension doesn't match their actual format.

    yt-dlp's FFmpegThumbnailsConvertor forces '-f image2' which picks the
    decoder from the file extension. A JPEG served as .png (common on Reddit)
    trips the PNG decoder and causes 'Conversion failed!'. Running this PP
    first ensures the extension is correct before conversion is attempted.
    """

    def run(self, info: dict) -> tuple[list, dict]:
        for thumbnail in info.get("thumbnails") or []:
            path = thumbnail.get("filepath")
            if not path or not os.path.exists(path):
                continue
            _, ext = os.path.splitext(path)
            detected = _imghdr.what(path)
            correct_ext = _IMGHDR_TO_EXT.get(detected or "")
            if not correct_ext or ext.lower() == f".{correct_ext}":
                continue
            new_path = replace_extension(path, correct_ext)
            self.to_screen(f"Correcting thumbnail extension: {os.path.basename(path)} → .{correct_ext}")
            os.replace(path, new_path)
            thumbnail["filepath"] = new_path
            files_to_move = info.get("__files_to_move") or {}
            if path in files_to_move:
                files_to_move[new_path] = replace_extension(files_to_move.pop(path), correct_ext)
        return [], info


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
    cookies_content: str | None = None,
) -> None:
    opts = get_opts(job.download_type, job.format, job.quality, ytdl_options, subtitle_langs=job.subtitle_langs)
    fmt = get_format(job.download_type, job.codec, job.format, job.quality)

    if "impersonate" in opts:
        opts["impersonate"] = yt_dlp.networking.impersonate.ImpersonateTarget.from_str(opts["impersonate"])

    temp_files: set[str] = set()

    def progress_hook(status: dict) -> None:
        if cancel_event.is_set():
            raise RuntimeError("METUBE_JOB_CANCELED")
        for f in filter(None, (status.get("tmpfilename"), status.get("filename"))):
            if f not in temp_files:
                temp_files.add(f)
                db.add_temp_file(job.id, f)
        for t in ((status.get("info_dict") or {}).get("thumbnails") or []):
            fp = t.get("filepath")
            if fp and fp not in temp_files:
                temp_files.add(fp)
                db.add_temp_file(job.id, fp)
        if status.get("status") == "finished":
            tmpfile = status.get("tmpfilename")
            if tmpfile:
                temp_files.discard(tmpfile)
                db.remove_temp_file(job.id, tmpfile)
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
            temp_files.clear()
            db.clear_temp_files(job.id)
            info_dict = data.get("info_dict") or {}
            filepath = info_dict.get("filepath")
            if filepath:
                final_name = filepath
                if info_dict.get("__finaldir"):
                    final_name = str(Path(info_dict["__finaldir"]) / Path(filepath).name)
                rel = os.path.relpath(final_name, download_dir)
                size = Path(final_name).stat().st_size if Path(final_name).exists() else None
                log.debug("output file for job %s: %s (%s bytes)", job.id, rel, size)
                db.set_output_file(job.id, rel, size)
            requested_subtitles = info_dict.get("requested_subtitles") or {}
            final_dir = info_dict.get("__finaldir")
            for subtitle in requested_subtitles.values():
                if isinstance(subtitle, dict) and subtitle.get("filepath"):
                    subtitle_path = subtitle["filepath"]
                    if final_dir and not Path(subtitle_path).exists():
                        candidate = Path(final_dir) / Path(subtitle_path).name
                        if candidate.exists():
                            subtitle_path = str(candidate)
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

    def _download() -> int:
        ydl = yt_dlp.YoutubeDL(params=params)
        ydl._pps["before_dl"].insert(0, _ThumbnailExtFixerPP(ydl))
        return ydl.download([job.url])

    log.info("starting job %s: %r", job.id, job.title)
    try:
        if cookies_content:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", encoding="utf-8") as f:
                f.write(cookies_content)
                f.flush()
                params["cookiefile"] = f.name
                result = _download()
        else:
            result = _download()
    except Exception as exc:
        if cancel_event.is_set():
            log.info("canceled job %s", job.id)
            db.mark_canceled(job.id)
            return
        message = str(exc)
        log.error("job %s failed: %s", job.id, message)
        db.mark_error(job.id, message)
        return
    finally:
        for f in temp_files:
            try:
                Path(f).unlink(missing_ok=True)
                log.debug("deleted leftover temp file: %s", f)
            except OSError as exc:
                log.warning("could not delete temp file %s: %s", f, exc)
        db.clear_temp_files(job.id)

    if cancel_event.is_set():
        log.info("canceled job %s", job.id)
        db.mark_canceled(job.id)
        return
    if result == 0:
        log.info("finished job %s: %r", job.id, job.title)
        db.mark_finished(job.id)
    else:
        log.error("job %s exited with yt-dlp code %d", job.id, result)
        db.mark_error(job.id, f"yt-dlp exited with code {result}")
