from __future__ import annotations

import hmac
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import requests
from flask import Flask, Response, abort, jsonify, redirect, render_template, request, send_file, url_for
from werkzeug.utils import secure_filename

from app.config import Config
from app.service import TikTokToTelegram, Video, YouTubeVideo, is_tiktok_video_url

LOGGER = logging.getLogger(__name__)
JOB_TTL_SECONDS = 6 * 60 * 60


@dataclass
class PreparedJob:
    job_id: str
    video: Video
    path: Path
    created_at: float


@dataclass
class YouTubeJob:
    job_id: str
    video: YouTubeVideo
    path: Path | None
    created_at: float


class JobStore:
    def __init__(self) -> None:
        self.jobs: dict[str, PreparedJob] = {}
        self.lock = threading.Lock()

    def add(self, video: Video, path: Path) -> PreparedJob:
        job = PreparedJob(uuid.uuid4().hex, video, path, time.time())
        with self.lock:
            self._cleanup()
            self.jobs[job.job_id] = job
        return job

    def get(self, job_id: str) -> PreparedJob:
        with self.lock:
            self._cleanup()
            job = self.jobs.get(job_id)
        if not job:
            abort(404)
        return job

    def remove(self, job_id: str) -> None:
        with self.lock:
            job = self.jobs.pop(job_id, None)
        if job and job.path.exists():
            job.path.unlink()

    def _cleanup(self) -> None:
        expired = [
            job_id
            for job_id, job in self.jobs.items()
            if time.time() - job.created_at > JOB_TTL_SECONDS
        ]
        for job_id in expired:
            job = self.jobs.pop(job_id)
            if job.path.exists():
                job.path.unlink()


class YouTubeJobStore:
    def __init__(self) -> None:
        self.jobs: dict[str, YouTubeJob] = {}
        self.lock = threading.Lock()

    def add(self, video: YouTubeVideo) -> YouTubeJob:
        job = YouTubeJob(uuid.uuid4().hex, video, None, time.time())
        with self.lock:
            self._cleanup()
            self.jobs[job.job_id] = job
        return job

    def get(self, job_id: str) -> YouTubeJob:
        with self.lock:
            self._cleanup()
            job = self.jobs.get(job_id)
        if not job:
            abort(404)
        return job

    def set_path(self, job_id: str, path: Path) -> None:
        with self.lock:
            self.jobs[job_id].path = path

    def _cleanup(self) -> None:
        expired = [
            job_id
            for job_id, job in self.jobs.items()
            if time.time() - job.created_at > JOB_TTL_SECONDS
        ]
        for job_id in expired:
            job = self.jobs.pop(job_id)
            if job.path and job.path.exists():
                job.path.unlink()


def create_app(config: Config, service: TikTokToTelegram) -> Flask:
    app = Flask(__name__)
    jobs = JobStore()
    youtube_jobs = YouTubeJobStore()

    @app.before_request
    def require_auth() -> Response | None:
        if not config.web_username or not config.web_password:
            return None
        auth = request.authorization
        valid = (
            auth is not None
            and hmac.compare_digest(auth.username or "", config.web_username)
            and hmac.compare_digest(auth.password or "", config.web_password)
        )
        if valid:
            return None
        return Response(
            "Требуется авторизация",
            401,
            {"WWW-Authenticate": 'Basic realm="TikTok to Telegram"'},
        )

    @app.get("/")
    def index() -> str:
        return render_template("index.html")

    @app.post("/prepare")
    @app.post("/tiktok/prepare")
    def prepare():
        tiktok_url = request.form.get("tiktok_url", "").strip()
        try:
            if is_tiktok_video_url(tiktok_url):
                video, path = service.prepare_url(tiktok_url)
                job = jobs.add(video, path)
                return render_template("edit.html", job=job)
            post_existing = request.form.get("post_existing") == "on"
            found, published = service.import_channel(tiktok_url, post_existing)
            return render_template(
                "channel_result.html",
                channel=tiktok_url,
                found=found,
                published=published,
                post_existing=post_existing,
            )
        except Exception as error:
            LOGGER.exception("Failed to prepare URL %s", tiktok_url)
            return render_template("index.html", error=str(error), tiktok_url=tiktok_url), 400

    @app.post("/youtube/info")
    def youtube_info():
        youtube_url = request.form.get("youtube_url", "").strip()
        try:
            video = service.get_youtube_info(youtube_url)
            job = youtube_jobs.add(video)
            return jsonify(
                {
                    "job_id": job.job_id,
                    "title": video.title,
                    "channel": video.channel,
                    "duration": video.duration,
                    "thumbnail_url": video.thumbnail_url,
                    "thumbnail_download_url": url_for("youtube_thumbnail", job_id=job.job_id),
                    "video_download_url": url_for("youtube_video", job_id=job.job_id),
                }
            )
        except Exception as error:
            LOGGER.exception("Failed to inspect YouTube URL %s", youtube_url)
            return jsonify({"error": str(error)}), 400

    @app.get("/youtube/thumbnail/<job_id>")
    def youtube_thumbnail(job_id: str):
        job = youtube_jobs.get(job_id)
        response = requests.get(job.video.thumbnail_url, timeout=60)
        response.raise_for_status()
        filename = secure_filename(job.video.title) or "youtube-thumbnail"
        content_type = response.headers.get("Content-Type", "image/jpeg")
        extension = ".webp" if "webp" in content_type else ".jpg"
        return send_file(
            BytesIO(response.content),
            mimetype=content_type,
            as_attachment=True,
            download_name=f"{filename}{extension}",
        )

    @app.get("/youtube/video/<job_id>")
    def youtube_video(job_id: str):
        job = youtube_jobs.get(job_id)
        if not job.path or not job.path.exists():
            path = service.download_youtube(job.video, f"youtube-{job.job_id}")
            youtube_jobs.set_path(job.job_id, path)
            job.path = path
        filename = secure_filename(job.video.title) or "youtube-video"
        return send_file(
            job.path,
            as_attachment=True,
            download_name=f"{filename}{job.path.suffix}",
            conditional=True,
        )

    @app.get("/preview/<job_id>")
    def preview(job_id: str):
        job = jobs.get(job_id)
        return send_file(job.path, mimetype="video/mp4", conditional=True)

    @app.post("/send/<job_id>")
    def send(job_id: str):
        job = jobs.get(job_id)
        before_text = request.form.get("before_text", "")
        quote_text = request.form.get("quote_text", "")
        after_text = request.form.get("after_text", "")
        try:
            service.publish(job.video, job.path, quote_text, before_text, after_text)
            service.storage.mark(job.video.video_id, job.video.username)
            jobs.remove(job_id)
            return redirect(url_for("done"))
        except Exception as error:
            LOGGER.exception("Failed to publish prepared job %s", job_id)
            return render_template(
                "edit.html",
                job=job,
                before_text=before_text,
                quote_text=quote_text,
                after_text=after_text,
                error=str(error),
            ), 502

    @app.post("/cancel/<job_id>")
    def cancel(job_id: str):
        jobs.remove(job_id)
        return redirect(url_for("index"))

    @app.get("/done")
    def done() -> str:
        return render_template("done.html")

    return app
