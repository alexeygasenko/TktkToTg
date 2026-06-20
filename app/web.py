from __future__ import annotations

import hmac
import logging
import math
import mimetypes
import re
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import requests
from flask import Flask, Response, abort, g, jsonify, redirect, render_template, request, send_file, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from app.config import Config, TelegramChannel
from app.service import TikTokToTelegram, Video, YouTubeVideo, is_instagram_url, is_tiktok_video_url
from app.storage import User

LOGGER = logging.getLogger(__name__)
JOB_TTL_SECONDS = 6 * 60 * 60


@dataclass
class PreparedJob:
    job_id: str
    video: Video
    paths: tuple[Path, ...]
    created_at: float
    selected_chat_id: str
    user_id: int = 1

    @property
    def path(self) -> Path:
        return self.paths[0]


@dataclass
class YouTubeJob:
    job_id: str
    video: YouTubeVideo
    path: Path | None
    created_at: float
    user_id: int = 1


class JobStore:
    def __init__(self) -> None:
        self.jobs: dict[str, PreparedJob] = {}
        self.lock = threading.Lock()

    def add(
        self, video: Video, path: Path | tuple[Path, ...], selected_chat_id: str, user_id: int = 1
    ) -> PreparedJob:
        paths = (path,) if isinstance(path, Path) else path
        job = PreparedJob(
            uuid.uuid4().hex, video, paths, time.time(), selected_chat_id, user_id
        )
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
        if job:
            for path in job.paths:
                if path.exists():
                    path.unlink()

    def _cleanup(self) -> None:
        expired = [
            job_id
            for job_id, job in self.jobs.items()
            if time.time() - job.created_at > JOB_TTL_SECONDS
        ]
        for job_id in expired:
            job = self.jobs.pop(job_id)
            for path in job.paths:
                if path.exists():
                    path.unlink()


class YouTubeJobStore:
    def __init__(self) -> None:
        self.jobs: dict[str, YouTubeJob] = {}
        self.lock = threading.Lock()

    def add(self, video: YouTubeVideo, user_id: int = 1) -> YouTubeJob:
        job = YouTubeJob(uuid.uuid4().hex, video, None, time.time(), user_id)
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

    def remove(self, job_id: str) -> None:
        with self.lock:
            job = self.jobs.pop(job_id, None)
        if job and job.path and job.path.exists():
            job.path.unlink()

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
    app.config["MAX_CONTENT_LENGTH"] = 6 * 1024 * 1024
    auth_storage = getattr(service, "storage", None)
    auth_supported = all(
        hasattr(auth_storage, name)
        for name in ("get_user", "get_user_by_username", "create_user")
    )
    if auth_supported:
        secret = auth_storage.setting("session_secret", "", 1)
        if not secret:
            secret = secrets.token_hex(32)
            auth_storage.set_setting("session_secret", secret, user_id=1)
        app.secret_key = secret
    else:
        app.secret_key = config.web_password or secrets.token_hex(32)
    jobs = JobStore()
    youtube_jobs = YouTubeJobStore()
    fallback_telegram_channels = config.telegram_channels or (
        TelegramChannel(config.telegram_chat_id, config.telegram_chat_id),
    )

    def current_user() -> User | None:
        if not auth_supported:
            return None
        user_id = session.get("user_id")
        if not user_id:
            return None
        return auth_storage.get_user(int(user_id))

    def login_user(user: User) -> None:
        session.clear()
        session["user_id"] = user.id

    def active_user_id() -> int:
        user = getattr(g, "current_user", None)
        return user.id if user else 1

    def settings_user_id() -> int:
        requested = request.values.get("settings_user_id")
        user = getattr(g, "current_user", None)
        if requested and user and user.is_admin:
            return int(requested)
        return active_user_id()

    def service_permissions(user_id: int | None = None) -> dict[str, bool]:
        if not auth_supported:
            return {"tiktok": True, "instagram": True, "youtube": True}
        user = auth_storage.get_user(user_id or active_user_id())
        if not user or user.is_disabled:
            return {"tiktok": False, "instagram": False, "youtube": False}
        return {
            "tiktok": user.allow_tiktok,
            "instagram": user.allow_instagram,
            "youtube": user.allow_youtube,
        }

    def require_service(service_name: str, user_id: int | None = None) -> None:
        if not service_permissions(user_id).get(service_name, False):
            raise PermissionError(f"Сервис {service_name} отключён для пользователя")

    def with_user_arg(args: tuple, user_id: int) -> tuple:
        return (*args, user_id) if auth_supported else args

    def telegram_channels(user_id: int | None = None) -> tuple[TelegramChannel, ...]:
        if hasattr(service, "telegram_channels"):
            args = with_user_arg((), user_id or active_user_id())
            return service.telegram_channels(*args)
        return fallback_telegram_channels

    def validate_chat_id(chat_id: str | None, user_id: int | None = None) -> str:
        channels = telegram_channels(user_id)
        if not channels:
            raise ValueError("Сначала добавьте Telegram-канал в настройках")
        selected = (chat_id or channels[0].chat_id).strip()
        if selected not in {channel.chat_id for channel in channels}:
            raise ValueError("Выбран неизвестный Telegram-канал")
        return selected

    def monitored_tiktok_channels(user_id: int | None = None) -> tuple[str, ...]:
        if hasattr(service, "monitored_tiktok_channels"):
            args = with_user_arg((), user_id or active_user_id())
            return service.monitored_tiktok_channels(*args)
        return config.tiktok_channels

    def poll_interval_seconds(user_id: int | None = None) -> int:
        if hasattr(service, "poll_interval_seconds"):
            args = with_user_arg((), user_id or active_user_id())
            return service.poll_interval_seconds(*args)
        return config.poll_interval_seconds

    def index_context(**extra):
        user_id = int(extra.pop("settings_user_id", active_user_id()))
        settings_user = auth_storage.get_user(user_id) if auth_supported else None
        return {
            "telegram_channels": telegram_channels(user_id),
            "monitored_tiktok_channels": monitored_tiktok_channels(user_id),
            "poll_interval_seconds": poll_interval_seconds(user_id),
            "service_permissions": service_permissions(user_id),
            "settings_user": settings_user,
            "admin_settings_mode": bool(
                settings_user and settings_user.id != active_user_id()
            ),
            **extra,
        }

    @app.before_request
    def require_auth() -> Response | None:
        if auth_supported:
            if request.endpoint in {
                "login",
                "login_post",
                "logout",
                "register",
                "setup_admin_password",
                "static",
            }:
                return None
            user = current_user()
            if not user or user.is_disabled:
                session.clear()
                return redirect(url_for("login", next=request.full_path))
            g.current_user = user
            if user.must_set_password or not user.password_hash:
                return redirect(url_for("setup_admin_password"))
            return None
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

    @app.get("/login")
    def login():
        if not auth_supported:
            return redirect(url_for("index"))
        admin = auth_storage.get_user(1)
        if admin and (admin.must_set_password or not admin.password_hash):
            return redirect(url_for("setup_admin_password"))
        return render_template("login.html", next=request.args.get("next", ""))

    @app.post("/login")
    def login_post():
        if not auth_supported:
            return redirect(url_for("index"))
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = auth_storage.get_user_by_username(username)
        if not user or user.is_disabled or not user.password_hash:
            return render_template("login.html", error="Неверный логин или пароль"), 401
        if not check_password_hash(user.password_hash, password):
            return render_template("login.html", error="Неверный логин или пароль"), 401
        login_user(user)
        if user.must_set_password:
            return redirect(url_for("setup_admin_password"))
        return redirect(request.form.get("next") or url_for("index"))

    @app.route("/setup-admin", methods=["GET", "POST"])
    def setup_admin_password():
        if not auth_supported:
            return redirect(url_for("index"))
        admin = auth_storage.get_user(1)
        if not admin:
            abort(404)
        logged_user = current_user()
        if admin.password_hash and not (logged_user and logged_user.id == admin.id):
            return redirect(url_for("login"))
        if request.method == "POST":
            password = request.form.get("password", "")
            confirm = request.form.get("confirm_password", "")
            if len(password) < 8:
                return render_template(
                    "setup_admin.html", error="Пароль должен быть не короче 8 символов"
                ), 400
            if password != confirm:
                return render_template(
                    "setup_admin.html", error="Пароли не совпадают"
                ), 400
            auth_storage.set_user_password(admin.id, generate_password_hash(password))
            admin = auth_storage.get_user(admin.id)
            assert admin is not None
            login_user(admin)
            return redirect(url_for("index"))
        return render_template("setup_admin.html")

    @app.route("/register", methods=["GET", "POST"])
    def register():
        if not auth_supported:
            return redirect(url_for("index"))
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            confirm = request.form.get("confirm_password", "")
            if not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", username):
                return render_template(
                    "register.html",
                    error="Логин: 3-32 символа, латиница, цифры, точка, дефис или подчёркивание",
                ), 400
            if len(password) < 8:
                return render_template(
                    "register.html", error="Пароль должен быть не короче 8 символов"
                ), 400
            if password != confirm:
                return render_template("register.html", error="Пароли не совпадают"), 400
            try:
                user = auth_storage.create_user(username, generate_password_hash(password))
            except Exception:
                return render_template("register.html", error="Логин уже занят"), 400
            login_user(user)
            return redirect(url_for("index"))
        return render_template("register.html")

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    def require_admin() -> User:
        user = getattr(g, "current_user", None)
        if not user or not user.is_admin:
            abort(403)
        return user

    @app.get("/admin/users")
    def admin_users():
        require_admin()
        page = max(1, int(request.args.get("page", "1") or "1"))
        per_page = 20
        total = auth_storage.user_count()
        pages = max(1, math.ceil(total / per_page))
        page = min(page, pages)
        users = auth_storage.users(limit=per_page, offset=(page - 1) * per_page)
        return render_template(
            "admin_users.html",
            users=users,
            page=page,
            pages=pages,
            total=total,
        )

    @app.get("/admin/users/<int:user_id>")
    def admin_user_detail(user_id: int):
        require_admin()
        user = auth_storage.get_user(user_id)
        if not user:
            abort(404)
        return render_template("admin_user.html", user=user)

    @app.post("/admin/users/<int:user_id>")
    def admin_user_update(user_id: int):
        require_admin()
        user = auth_storage.get_user(user_id)
        if not user:
            abort(404)
        auth_storage.update_user(
            user_id,
            username=request.form.get("username", user.username),
            is_admin=request.form.get("is_admin") == "on",
            is_disabled=request.form.get("is_disabled") == "on",
            allow_tiktok=request.form.get("allow_tiktok") == "on",
            allow_instagram=request.form.get("allow_instagram") == "on",
            allow_youtube=request.form.get("allow_youtube") == "on",
        )
        return redirect(url_for("admin_user_detail", user_id=user_id, saved="1"))

    @app.post("/admin/users/<int:user_id>/toggle-disabled")
    def admin_user_toggle_disabled(user_id: int):
        admin = require_admin()
        user = auth_storage.get_user(user_id)
        if not user:
            abort(404)
        if user.id == admin.id:
            abort(400)
        auth_storage.update_user(
            user_id,
            username=user.username,
            is_admin=user.is_admin,
            is_disabled=not user.is_disabled,
            allow_tiktok=user.allow_tiktok,
            allow_instagram=user.allow_instagram,
            allow_youtube=user.allow_youtube,
        )
        return redirect(url_for("admin_users", page=request.args.get("page", "1")))

    @app.get("/admin/users/<int:user_id>/settings")
    def admin_user_settings(user_id: int):
        require_admin()
        user = auth_storage.get_user(user_id)
        if not user:
            abort(404)
        return render_template(
            "settings.html",
            **index_context(
                settings_user_id=user_id,
                settings_open=True,
                admin_settings_mode=True,
            ),
        )

    @app.get("/")
    def index() -> str:
        return render_template("index.html", **index_context())

    @app.get("/settings")
    def settings() -> str:
        return render_template("settings.html", **index_context())

    @app.post("/settings/telegram")
    def add_telegram_channel():
        user_id = settings_user_id()
        try:
            service.add_telegram_destination(
                *with_user_arg(
                    (
                        request.form.get("name", ""),
                        request.form.get("chat_id", ""),
                        request.form.get("bot_token", ""),
                    ),
                    user_id,
                )
            )
            if user_id != active_user_id():
                return redirect(url_for("admin_user_settings", user_id=user_id, settings="telegram-added"))
            return redirect(url_for("settings", settings="telegram-added"))
        except Exception as error:
            LOGGER.warning("Failed to add Telegram destination: %s", type(error).__name__)
            return render_template(
                "settings.html",
                **index_context(
                    settings_user_id=user_id,
                    settings_error=str(error),
                    settings_open=True,
                ),
            ), 400

    @app.post("/settings/telegram/discover")
    def discover_telegram_channels():
        user_id = settings_user_id()
        try:
            found = service.discover_telegram_destinations(
                *with_user_arg((request.form.get("bot_token", ""),), user_id)
            )
            if user_id != active_user_id():
                return redirect(
                    url_for(
                        "admin_user_settings",
                        user_id=user_id,
                        settings="telegram-discovered",
                        found=len(found),
                    )
                )
            return redirect(url_for("settings", settings="telegram-discovered", found=len(found)))
        except Exception as error:
            LOGGER.warning(
                "Failed to discover Telegram destinations: %s", type(error).__name__
            )
            return render_template(
                "settings.html",
                **index_context(
                    settings_user_id=user_id,
                    settings_error=str(error),
                    settings_open=True,
                ),
            ), 400

    @app.post("/settings/telegram/delete")
    def delete_telegram_channel():
        user_id = settings_user_id()
        try:
            service.delete_telegram_destination(
                *with_user_arg((request.form.get("chat_id", ""),), user_id)
            )
            if user_id != active_user_id():
                return redirect(url_for("admin_user_settings", user_id=user_id, settings="telegram-deleted"))
            return redirect(url_for("settings", settings="telegram-deleted"))
        except Exception as error:
            return render_template(
                "settings.html",
                **index_context(
                    settings_user_id=user_id,
                    settings_error=str(error),
                    settings_open=True,
                ),
            ), 400

    @app.post("/settings/telegram/move")
    def move_telegram_channel():
        user_id = settings_user_id()
        try:
            service.move_telegram_destination(
                *with_user_arg(
                    (
                        request.form.get("chat_id", ""),
                        request.form.get("direction", ""),
                    ),
                    user_id,
                )
            )
            if request.headers.get("X-Requested-With") == "fetch":
                return jsonify(
                    {
                        "channels": [
                            {"chat_id": channel.chat_id}
                            for channel in telegram_channels(user_id)
                        ]
                    }
                )
            if user_id != active_user_id():
                return redirect(url_for("admin_user_settings", user_id=user_id, settings="telegram-moved"))
            return redirect(url_for("settings", settings="telegram-moved"))
        except Exception as error:
            if request.headers.get("X-Requested-With") == "fetch":
                return jsonify({"error": str(error)}), 400
            return render_template(
                "settings.html",
                **index_context(
                    settings_user_id=user_id,
                    settings_error=str(error),
                    settings_open=True,
                )
            ), 400

    @app.post("/settings/tiktok/monitor")
    def add_monitored_tiktok_channel():
        user_id = settings_user_id()
        try:
            service.add_monitored_tiktok_channel(
                *with_user_arg((request.form.get("channel", ""),), user_id)
            )
            if user_id != active_user_id():
                return redirect(url_for("admin_user_settings", user_id=user_id, settings="tiktok-monitor-added"))
            return redirect(url_for("settings", settings="tiktok-monitor-added"))
        except Exception as error:
            return render_template(
                "settings.html",
                **index_context(
                    settings_user_id=user_id,
                    settings_error=str(error),
                    settings_open=True,
                )
            ), 400

    @app.post("/settings/tiktok/monitor/delete")
    def delete_monitored_tiktok_channel():
        user_id = settings_user_id()
        try:
            service.delete_monitored_tiktok_channel(
                *with_user_arg((request.form.get("channel", ""),), user_id)
            )
            if user_id != active_user_id():
                return redirect(url_for("admin_user_settings", user_id=user_id, settings="tiktok-monitor-deleted"))
            return redirect(url_for("settings", settings="tiktok-monitor-deleted"))
        except Exception as error:
            return render_template(
                "settings.html",
                **index_context(
                    settings_user_id=user_id,
                    settings_error=str(error),
                    settings_open=True,
                )
            ), 400

    @app.post("/settings/tiktok/interval")
    def update_tiktok_interval():
        user_id = settings_user_id()
        try:
            service.set_poll_interval_seconds(
                *with_user_arg((request.form.get("poll_interval_seconds", ""),), user_id)
            )
            if user_id != active_user_id():
                return redirect(url_for("admin_user_settings", user_id=user_id, settings="tiktok-interval-updated"))
            return redirect(url_for("settings", settings="tiktok-interval-updated"))
        except Exception as error:
            return render_template(
                "settings.html",
                **index_context(
                    settings_user_id=user_id,
                    settings_error=str(error),
                    settings_open=True,
                )
            ), 400

    @app.post("/settings/cookies/<service_name>")
    def update_cookies(service_name: str):
        user_id = settings_user_id()
        try:
            require_service(service_name, user_id)
            uploaded = request.files.get("cookies_file")
            if not uploaded or not uploaded.filename:
                raise ValueError("Выберите cookies.txt")
            service.update_cookies(*with_user_arg((service_name, uploaded.read()), user_id))
            if user_id != active_user_id():
                return redirect(url_for("admin_user_settings", user_id=user_id, settings=f"{service_name}-cookies-updated"))
            return redirect(url_for("settings", settings=f"{service_name}-cookies-updated"))
        except Exception as error:
            LOGGER.exception("Failed to update %s cookies", service_name)
            return render_template(
                "settings.html",
                **index_context(
                    settings_user_id=user_id,
                    settings_error=str(error),
                    settings_open=True,
                ),
            ), 400

    @app.post("/prepare")
    @app.post("/tiktok/prepare")
    def prepare():
        user_id = active_user_id()
        tiktok_url = request.form.get("tiktok_url", "").strip()
        selected_chat_id = request.form.get("chat_id", "")
        try:
            selected_chat_id = validate_chat_id(selected_chat_id, user_id)
            if is_instagram_url(tiktok_url) or is_tiktok_video_url(tiktok_url):
                platform = "instagram" if is_instagram_url(tiktok_url) else "tiktok"
                require_service(platform, user_id)
                video, path = service.prepare_url(
                    *with_user_arg((tiktok_url,), user_id)
                )
                job = jobs.add(video, path, selected_chat_id, user_id)
                return render_template(
                    "edit.html",
                    job=job,
                    telegram_channels=telegram_channels(user_id),
                    service_permissions=service_permissions(user_id),
                )
            require_service("tiktok", user_id)
            post_existing = request.form.get("post_existing") == "on"
            found, published = service.import_channel(
                *with_user_arg((tiktok_url, post_existing, selected_chat_id), user_id)
            )
            return render_template(
                "channel_result.html",
                channel=tiktok_url,
                found=found,
                published=published,
                post_existing=post_existing,
            )
        except Exception as error:
            LOGGER.exception("Failed to prepare URL %s", tiktok_url)
            return render_template(
                "index.html",
                error=str(error),
                tiktok_url=tiktok_url,
                telegram_channels=telegram_channels(user_id),
                selected_chat_id=selected_chat_id,
                service_permissions=service_permissions(user_id),
            ), 400

    @app.post("/media/info")
    def media_info():
        user_id = active_user_id()
        media_url = request.form.get("media_url", "").strip()
        try:
            platform = "instagram" if is_instagram_url(media_url) else "tiktok"
            require_service(platform, user_id)
            video, path = service.prepare_url(*with_user_arg((media_url,), user_id))
            selected_chat_id = validate_chat_id(request.form.get("chat_id", ""), user_id)
            job = jobs.add(video, path, selected_chat_id, user_id)
            return jsonify(
                {
                    "job_id": job.job_id,
                    "description": video.description,
                    "author": f"@{video.username}",
                    "preview_url": url_for("preview", job_id=job.job_id),
                    "video_download_url": url_for("media_video", job_id=job.job_id),
                    "post_url": url_for(
                        "media_post", job_id=job.job_id, chat_id=selected_chat_id
                    ),
                    "media_type": video.media_type,
                    "image_count": len(job.paths) if video.media_type == "image" else 0,
                }
            )
        except Exception as error:
            LOGGER.exception("Failed to inspect media URL %s", media_url)
            return jsonify({"error": str(error)}), 400

    @app.get("/media/video/<job_id>")
    def media_video(job_id: str):
        job = jobs.get(job_id)
        if job.user_id != active_user_id():
            abort(404)
        if job.video.media_type == "image" and len(job.paths) > 1:
            archive = BytesIO()
            with ZipFile(archive, "w", ZIP_DEFLATED) as zip_file:
                for index, path in enumerate(job.paths, start=1):
                    zip_file.write(path, f"{job.video.video_id}-{index:02d}{path.suffix}")
            archive.seek(0)
            return send_file(
                archive,
                mimetype="application/zip",
                as_attachment=True,
                download_name=f"{secure_filename(job.video.username)}-{job.video.video_id}-images.zip",
            )
        return send_file(
            job.path,
            as_attachment=True,
            download_name=f"{secure_filename(job.video.username)}-{job.video.video_id}{job.path.suffix}",
            conditional=True,
        )

    @app.get("/media/post/<job_id>")
    def media_post(job_id: str):
        job = jobs.get(job_id)
        if job.user_id != active_user_id():
            abort(404)
        selected_chat_id = request.args.get("chat_id") or job.selected_chat_id
        return render_template(
            "edit.html",
            job=job,
            telegram_channels=telegram_channels(job.user_id),
            selected_chat_id=selected_chat_id,
            service_permissions=service_permissions(job.user_id),
        )

    @app.post("/youtube/info")
    def youtube_info():
        user_id = active_user_id()
        youtube_url = request.form.get("youtube_url", "").strip()
        try:
            require_service("youtube", user_id)
            video = service.get_youtube_info(*with_user_arg((youtube_url,), user_id))
            job = youtube_jobs.add(video, user_id)
            return jsonify(
                {
                    "job_id": job.job_id,
                    "title": video.title,
                    "channel": video.channel,
                    "duration": video.duration,
                    "thumbnail_url": video.thumbnail_url,
                    "thumbnail_download_url": url_for("youtube_thumbnail", job_id=job.job_id),
                    "video_download_url": url_for("youtube_video", job_id=job.job_id),
                    "post_url": url_for("youtube_post", job_id=job.job_id),
                }
            )
        except Exception as error:
            LOGGER.exception("Failed to inspect YouTube URL %s", youtube_url)
            return jsonify({"error": str(error)}), 400

    @app.get("/youtube/thumbnail/<job_id>")
    def youtube_thumbnail(job_id: str):
        job = youtube_jobs.get(job_id)
        if job.user_id != active_user_id():
            abort(404)
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
        if job.user_id != active_user_id():
            abort(404)
        if not job.path or not job.path.exists():
            path = service.download_youtube(
                *with_user_arg((job.video, f"youtube-{job.job_id}"), job.user_id)
            )
            youtube_jobs.set_path(job.job_id, path)
            job.path = path
        filename = secure_filename(job.video.title) or "youtube-video"
        return send_file(
            job.path,
            as_attachment=True,
            download_name=f"{filename}{job.path.suffix}",
            conditional=True,
        )

    @app.get("/youtube/post/<job_id>")
    def youtube_post(job_id: str):
        job = youtube_jobs.get(job_id)
        if job.user_id != active_user_id():
            abort(404)
        return render_template(
            "youtube_edit.html",
            job=job,
            telegram_channels=telegram_channels(job.user_id),
            service_permissions=service_permissions(job.user_id),
        )

    @app.post("/youtube/send/<job_id>")
    def youtube_send(job_id: str):
        job = youtube_jobs.get(job_id)
        if job.user_id != active_user_id():
            abort(404)
        before_text = request.form.get("before_text", "")
        after_text = request.form.get("after_text", "")
        caption_html = request.form.get("caption_html", "")
        selected_chat_id = request.form.get("chat_id", "")
        try:
            service.publish_youtube(
                *with_user_arg(
                    (
                        job.video,
                        before_text,
                        after_text,
                        selected_chat_id,
                        caption_html,
                    ),
                    job.user_id,
                )
            )
            youtube_jobs.remove(job_id)
            return redirect(url_for("done", source="youtube"))
        except Exception as error:
            LOGGER.exception("Failed to publish YouTube job %s", job_id)
            return render_template(
                "youtube_edit.html",
                job=job,
                telegram_channels=telegram_channels(job.user_id),
                selected_chat_id=selected_chat_id,
                before_text=before_text,
                after_text=after_text,
                caption_html=caption_html,
                error=str(error),
            ), 502

    @app.get("/preview/<job_id>")
    def preview(job_id: str):
        return preview_item(job_id, 0)

    @app.get("/preview/<job_id>/<int:item_index>")
    def preview_item(job_id: str, item_index: int):
        job = jobs.get(job_id)
        if job.user_id != active_user_id():
            abort(404)
        if item_index < 0 or item_index >= len(job.paths):
            abort(404)
        path = job.paths[item_index]
        mimetype = mimetypes.guess_type(path.name)[0]
        if not mimetype:
            mimetype = "image/jpeg" if job.video.media_type == "image" else "video/mp4"
        return send_file(path, mimetype=mimetype, conditional=True)

    @app.post("/send/<job_id>")
    def send(job_id: str):
        job = jobs.get(job_id)
        if job.user_id != active_user_id():
            abort(404)
        before_text = request.form.get("before_text", "")
        quote_text = request.form.get("quote_text", "")
        after_text = request.form.get("after_text", "")
        caption_html = request.form.get("caption_html", "")
        options_present = request.form.get("caption_options_present") == "1"
        include_author = (
            request.form.get("include_author") == "on" if options_present else True
        )
        include_description = (
            request.form.get("include_description") == "on" if options_present else True
        )
        selected_chat_id = request.form.get("chat_id", job.selected_chat_id)
        try:
            service.publish(
                *with_user_arg(
                    (
                        job.video,
                        job.paths,
                        quote_text,
                        before_text,
                        after_text,
                        selected_chat_id,
                        include_author,
                        include_description,
                        caption_html,
                    ),
                    job.user_id,
                )
            )
            service.storage.mark(*with_user_arg((job.video.video_id, job.video.username), job.user_id))
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
                caption_html=caption_html,
                include_author=include_author,
                include_description=include_description,
                selected_chat_id=selected_chat_id,
                telegram_channels=telegram_channels(job.user_id),
                error=str(error),
            ), 502

    @app.post("/cancel/<job_id>")
    def cancel(job_id: str):
        job = jobs.get(job_id)
        if job.user_id != active_user_id():
            abort(404)
        jobs.remove(job_id)
        return redirect(url_for("index"))

    @app.get("/done")
    def done() -> str:
        return render_template("done.html", source=request.args.get("source", "tiktok"))

    return app
