from __future__ import annotations

import html
import logging
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
import yt_dlp

from app.config import Config, TelegramChannel
from app.storage import Storage

LOGGER = logging.getLogger(__name__)
MAX_CAPTION_LENGTH = 1024
MAX_VIDEO_BYTES = 49 * 1024 * 1024
YOUTUBE_AUTH_COOKIE_NAMES = {
    "SID",
    "HSID",
    "SSID",
    "APISID",
    "SAPISID",
    "LOGIN_INFO",
    "__Secure-1PSID",
    "__Secure-3PSID",
}


@dataclass(frozen=True)
class Video:
    video_id: str
    username: str
    description: str
    url: str
    timestamp: int


@dataclass(frozen=True)
class YouTubeVideo:
    video_id: str
    title: str
    url: str
    thumbnail_url: str
    duration: int
    channel: str


def normalize_channel(channel: str) -> tuple[str, str]:
    channel = channel.strip()
    if "tiktok.com" in channel:
        path = urlparse(channel).path.rstrip("/")
        username = path.split("/")[-1].lstrip("@")
    else:
        username = channel.rstrip("/").split("/")[-1].lstrip("@")
    if not re.fullmatch(r"[\w.]+", username):
        raise ValueError(f"Invalid TikTok channel: {channel}")
    return username, f"https://www.tiktok.com/@{username}"


def validate_tiktok_url(url: str) -> str:
    url = url.strip()
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not (
        host == "tiktok.com" or host.endswith(".tiktok.com")
    ):
        raise ValueError("Нужна полная ссылка на видео с домена tiktok.com")
    return url


def validate_youtube_url(url: str) -> str:
    url = url.strip()
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not (
        host in {"youtube.com", "youtu.be"} or host.endswith(".youtube.com")
    ):
        raise ValueError("Нужна полная ссылка на видео YouTube")
    return url


def is_tiktok_video_url(url: str) -> bool:
    return "/video/" in urlparse(validate_tiktok_url(url)).path


def username_from_info(info: dict[str, Any], webpage_url: str) -> str:
    username_match = re.search(r"tiktok\.com/@([^/?]+)", webpage_url)
    return str(
        (username_match.group(1) if username_match else None)
        or info.get("uploader")
        or info.get("channel")
        or info.get("channel_id")
        or info.get("uploader_id")
        or "tiktok"
    ).lstrip("@")


def best_thumbnail_url(info: dict[str, Any]) -> str:
    thumbnails = [item for item in info.get("thumbnails") or [] if item.get("url")]
    if thumbnails:
        best = max(
            thumbnails,
            key=lambda item: int(item.get("width") or 0) * int(item.get("height") or 0),
        )
        return str(best["url"])
    return str(info.get("thumbnail") or "")


def has_youtube_auth_cookies(path: Path | None) -> bool:
    if not path or not path.is_file():
        return False
    for line in path.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7 and parts[5] in YOUTUBE_AUTH_COOKIE_NAMES:
            return True
    return False


def build_caption(
    video: Video,
    quote_text: str | None = None,
    before_text: str = "",
    after_text: str = "",
) -> str:
    author = html.escape(f"@{video.username}")
    author_url = html.escape(f"https://www.tiktok.com/@{video.username}", quote=True)
    author_link = f'<a href="{author_url}">{author}</a>'
    texts = [
        before_text.strip(),
        video.description.strip() if quote_text is None else quote_text.strip(),
        after_text.strip(),
    ]

    def render(parts: list[str], truncated: bool = False) -> str:
        before, quote, after = (
            html.escape(part) + ("…" if truncated and part else "") for part in parts
        )
        sections = [author_link]
        if before:
            sections.append(before)
        if quote:
            sections.append(f"<blockquote>{quote}</blockquote>")
        if after:
            sections.append(after)
        return "\n\n".join(sections)

    caption = render(texts)
    if len(caption) <= MAX_CAPTION_LENGTH:
        return caption

    low, high = 0, max(len(text) for text in texts)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = render([text[:middle] for text in texts], truncated=True)
        if len(candidate) <= MAX_CAPTION_LENGTH:
            low = middle
        else:
            high = middle - 1
    return render([text[:low] for text in texts], truncated=True)


def build_youtube_caption(
    video: YouTubeVideo, before_text: str = "", after_text: str = ""
) -> str:
    video_url = html.escape(video.url, quote=True)
    texts = [before_text.strip(), video.title.strip(), video.channel.strip(), after_text.strip()]

    def render(parts: list[str], truncated: bool = False) -> str:
        before, title, channel, after = (
            html.escape(part) + ("…" if truncated and part else "") for part in parts
        )
        sections: list[str] = []
        if before:
            sections.append(before)
        sections.append(f'<a href="{video_url}">{title or "Смотреть на YouTube"}</a>')
        if channel:
            sections.append(channel)
        if after:
            sections.append(after)
        return "\n\n".join(sections)

    caption = render(texts)
    if len(caption) <= MAX_CAPTION_LENGTH:
        return caption

    low, high = 0, max(len(text) for text in texts)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = render([text[:middle] for text in texts], truncated=True)
        if len(candidate) <= MAX_CAPTION_LENGTH:
            low = middle
        else:
            high = middle - 1
    return render([text[:low] for text in texts], truncated=True)


class TikTokToTelegram:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.config.data_dir.mkdir(parents=True, exist_ok=True)
        self.download_dir = self.config.data_dir / "downloads"
        self.download_dir.mkdir(exist_ok=True)
        for pattern in ("manual-*", "youtube-*"):
            for stale_file in self.download_dir.glob(pattern):
                stale_file.unlink()
        self.storage = Storage(self.config.data_dir / "state.sqlite3")
        configured_destinations = self.config.telegram_channels or (
            TelegramChannel(self.config.telegram_chat_id, self.config.telegram_chat_id),
        )
        for channel in configured_destinations:
            self.storage.add_telegram_destination(
                channel.name,
                channel.chat_id,
                self.config.telegram_bot_token,
                is_default=channel.chat_id == self.config.telegram_chat_id,
                replace=False,
            )
        self.ydl_lock = threading.Lock()
        self.tiktok_cookies_file = self._writable_cookie_copy(
            self.config.cookies_file, "tiktok-cookies.txt"
        )
        self.youtube_cookies_file = self._writable_cookie_copy(
            self.config.youtube_cookies_file, "youtube-cookies.txt"
        )

    def _writable_cookie_copy(self, source: Path | None, filename: str) -> Path | None:
        destination = self.config.data_dir / filename
        if destination.is_file():
            return destination
        if not source:
            return None
        if not source.is_file():
            raise FileNotFoundError(f"Cookies file not found: {source}")
        shutil.copyfile(source, destination)
        return destination

    def telegram_channels(self) -> tuple[TelegramChannel, ...]:
        return tuple(
            TelegramChannel(destination.name, destination.chat_id)
            for destination in self.storage.telegram_destinations()
        )

    def add_telegram_destination(
        self, name: str, chat_id: str, bot_token: str
    ) -> None:
        name = name.strip()
        chat_id = chat_id.strip()
        bot_token = bot_token.strip()
        if not name or not chat_id or not bot_token:
            raise ValueError("Укажите название, @тег канала и токен бота")
        if not chat_id.startswith("@"):
            raise ValueError("Тег Telegram-канала должен начинаться с @")
        try:
            response = requests.get(
                f"https://api.telegram.org/bot{bot_token}/getChat",
                params={"chat_id": chat_id},
                timeout=30,
            )
        except requests.RequestException:
            raise ValueError("Не удалось проверить бота через Telegram API") from None
        try:
            payload = response.json()
        except requests.JSONDecodeError as error:
            raise ValueError("Telegram не ответил корректно на проверку бота") from error
        if not payload.get("ok"):
            raise ValueError(
                "Telegram не подтвердил доступ. Проверьте токен, @тег и права бота."
            )
        self.storage.add_telegram_destination(name, chat_id, bot_token)

    def update_cookies(self, service_name: str, content: bytes) -> Path:
        if len(content) > 5 * 1024 * 1024:
            raise ValueError("Файл cookies не должен превышать 5 МБ")
        if b"Netscape HTTP Cookie File" not in content[:256]:
            raise ValueError("Нужен cookies.txt в Netscape-формате")
        if service_name == "tiktok":
            destination = self.config.data_dir / "tiktok-cookies.txt"
            attribute = "tiktok_cookies_file"
        elif service_name == "youtube":
            destination = self.config.data_dir / "youtube-cookies.txt"
            attribute = "youtube_cookies_file"
        else:
            raise ValueError("Неизвестный сервис cookies")
        temporary = destination.with_suffix(".tmp")
        temporary.write_bytes(content)
        temporary.replace(destination)
        setattr(self, attribute, destination)
        return destination

    def _ydl_options(self, cookies_file: Path | None = None) -> dict[str, Any]:
        options: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": 30,
            "js_runtimes": {"deno": {}},
        }
        if cookies_file:
            options["cookiefile"] = str(cookies_file)
        return options

    def _youtube_options(self, cookies_file: Path | None = None) -> dict[str, Any]:
        options = self._ydl_options(cookies_file)
        if self.config.youtube_po_token_provider_url:
            options["extractor_args"] = {
                "youtube": {"player_client": ["mweb"]},
                "youtubepot-bgutilhttp": {
                    "base_url": [self.config.youtube_po_token_provider_url]
                },
            }
        return options

    def scan(self, channel: str) -> list[Video]:
        username, channel_url = normalize_channel(channel)
        options = {
            **self._ydl_options(self.tiktok_cookies_file),
            "extract_flat": True,
            "playlistend": self.config.scan_limit,
        }
        with self.ydl_lock, yt_dlp.YoutubeDL(options) as ydl:
            result = ydl.extract_info(channel_url, download=False)

        videos: list[Video] = []
        for entry in (result or {}).get("entries") or []:
            if not entry:
                continue
            video_id = str(entry.get("id") or "").strip()
            if not video_id:
                continue
            videos.append(
                Video(
                    video_id=video_id,
                    username=username,
                    description=str(entry.get("description") or entry.get("title") or ""),
                    url=str(
                        entry.get("webpage_url")
                        or f"https://www.tiktok.com/@{username}/video/{video_id}"
                    ),
                    timestamp=int(entry.get("timestamp") or 0),
                )
            )
        return sorted(videos, key=lambda item: (item.timestamp, item.video_id))

    def download(self, video: Video, output_id: str | None = None) -> Path:
        output_id = output_id or video.video_id
        output_template = str(self.download_dir / f"{output_id}.%(ext)s")
        options = {
            **self._ydl_options(self.tiktok_cookies_file),
            "format": "best[ext=mp4][filesize<49M]/best[filesize<49M]/best[ext=mp4]/best",
            "merge_output_format": "mp4",
            "outtmpl": output_template,
            "noplaylist": True,
        }
        with self.ydl_lock, yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(video.url, download=True)
            path = Path(ydl.prepare_filename(info))

        if not path.exists():
            matches = list(self.download_dir.glob(f"{output_id}.*"))
            if not matches:
                raise FileNotFoundError(f"Downloaded file for {video.video_id} was not found")
            path = matches[0]
        if path.stat().st_size > MAX_VIDEO_BYTES:
            path.unlink()
            raise ValueError(f"Video {video.video_id} is larger than Telegram Bot API limit")
        return path

    def prepare_url(self, url: str) -> tuple[Video, Path]:
        url = validate_tiktok_url(url)
        output_id = f"manual-{uuid.uuid4().hex}"
        options = {
            **self._ydl_options(self.tiktok_cookies_file),
            "format": "best[ext=mp4][filesize<49M]/best[filesize<49M]/best[ext=mp4]/best",
            "merge_output_format": "mp4",
            "outtmpl": str(self.download_dir / f"{output_id}.%(ext)s"),
            "noplaylist": True,
        }
        try:
            with self.ydl_lock, yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=True)
                path = Path(ydl.prepare_filename(info))
        except Exception:
            for partial_file in self.download_dir.glob(f"{output_id}.*"):
                partial_file.unlink()
            raise
        if not path.exists():
            matches = list(self.download_dir.glob(f"{output_id}.*"))
            if not matches:
                raise FileNotFoundError("Скачанное видео не найдено")
            path = matches[0]
        if path.stat().st_size > MAX_VIDEO_BYTES:
            path.unlink()
            raise ValueError("Видео больше лимита Telegram Bot API в 50 МБ")

        webpage_url = str(info.get("webpage_url") or url)
        username = username_from_info(info, webpage_url)
        video = Video(
            video_id=str(info.get("id") or output_id),
            username=username,
            description=str(info.get("description") or info.get("title") or ""),
            url=webpage_url,
            timestamp=int(info.get("timestamp") or 0),
        )
        return video, path

    def get_youtube_info(self, url: str) -> YouTubeVideo:
        url = validate_youtube_url(url)
        errors: list[Exception] = []
        info: dict[str, Any] | None = None
        cookie_candidates = [self.youtube_cookies_file, None] if self.youtube_cookies_file else [None]
        for cookies_file in cookie_candidates:
            options = {
                **self._youtube_options(cookies_file),
                "noplaylist": True,
                "skip_download": True,
            }
            try:
                with self.ydl_lock, yt_dlp.YoutubeDL(options) as ydl:
                    info = ydl.extract_info(url, download=False)
                break
            except Exception as error:
                errors.append(error)
        if not info:
            if self.youtube_cookies_file and not has_youtube_auth_cookies(
                self.youtube_cookies_file
            ):
                raise ValueError(
                    "youtube-cookies.txt содержит только гостевые cookies. "
                    "Экспортируйте cookies после входа в аккаунт YouTube; в файле "
                    "должны присутствовать SID, SSID, SAPISID или LOGIN_INFO."
                ) from errors[-1]
            if self.youtube_cookies_file and self.config.youtube_po_token_provider_url:
                raise ValueError(
                    "PO Token provider подключён, но YouTube отклонил account cookies. "
                    "Переэкспортируйте cookies из отдельной incognito-сессии и сразу "
                    "закройте её, чтобы YouTube не ротировал cookies."
                ) from errors[-1]
            raise ValueError(
                "YouTube не отдал видеоформаты. Cookies загружены, но для этого "
                "ролика может требоваться отдельный PO Token."
            ) from errors[-1]
        thumbnail_url = best_thumbnail_url(info)
        if not thumbnail_url:
            raise ValueError("YouTube не вернул превью для этого видео")
        return YouTubeVideo(
            video_id=str(info.get("id") or ""),
            title=str(info.get("title") or "youtube-video"),
            url=str(info.get("webpage_url") or url),
            thumbnail_url=thumbnail_url,
            duration=int(info.get("duration") or 0),
            channel=str(info.get("channel") or info.get("uploader") or ""),
        )

    def download_youtube(self, video: YouTubeVideo, output_id: str) -> Path:
        errors: list[Exception] = []
        cookie_candidates = [self.youtube_cookies_file, None] if self.youtube_cookies_file else [None]
        for cookies_file in cookie_candidates:
            for partial_file in self.download_dir.glob(f"{output_id}.*"):
                partial_file.unlink()
            options = {
                **self._youtube_options(cookies_file),
                "format": "bestvideo+bestaudio/best",
                "merge_output_format": "mkv",
                "outtmpl": str(self.download_dir / f"{output_id}.%(ext)s"),
                "noplaylist": True,
            }
            try:
                with self.ydl_lock, yt_dlp.YoutubeDL(options) as ydl:
                    ydl.extract_info(video.url, download=True)
                break
            except Exception as error:
                errors.append(error)
        else:
            if self.youtube_cookies_file and not has_youtube_auth_cookies(
                self.youtube_cookies_file
            ):
                raise ValueError(
                    "youtube-cookies.txt содержит только гостевые cookies. "
                    "Экспортируйте cookies после входа в аккаунт YouTube; в файле "
                    "должны присутствовать SID, SSID, SAPISID или LOGIN_INFO."
                ) from errors[-1]
            if self.youtube_cookies_file and self.config.youtube_po_token_provider_url:
                raise ValueError(
                    "PO Token provider подключён, но YouTube отклонил account cookies. "
                    "Переэкспортируйте cookies из отдельной incognito-сессии и сразу "
                    "закройте её, чтобы YouTube не ротировал cookies."
                ) from errors[-1]
            raise ValueError(
                "YouTube не отдал видеоформаты. Cookies загружены, но для этого "
                "ролика может требоваться отдельный PO Token."
            ) from errors[-1]
        matches = [
            path
            for path in self.download_dir.glob(f"{output_id}.*")
            if path.suffix not in {".part", ".ytdl"}
        ]
        if not matches:
            raise FileNotFoundError("Скачанное YouTube-видео не найдено")
        return max(matches, key=lambda path: path.stat().st_size)

    def import_channel(
        self, channel: str, post_existing: bool, chat_id: str | None = None
    ) -> tuple[int, int]:
        username, _ = normalize_channel(channel)
        destination = self.storage.telegram_destination(chat_id).chat_id
        videos = self.scan(channel)
        if not post_existing:
            for video in videos:
                self.storage.mark(video.video_id, username)
            self.storage.mark_channel_initialized(username)
            return len(videos), 0

        published = 0
        for video in videos:
            if self.storage.has(video.video_id):
                continue
            path: Path | None = None
            try:
                path = self.download(video)
                self.publish(video, path, chat_id=destination)
                self.storage.mark(video.video_id, username)
                published += 1
            finally:
                if path and path.exists():
                    path.unlink()
        self.storage.mark_channel_initialized(username)
        return len(videos), published

    def publish(
        self,
        video: Video,
        path: Path,
        quote_text: str | None = None,
        before_text: str = "",
        after_text: str = "",
        chat_id: str | None = None,
    ) -> None:
        target = self.storage.telegram_destination(chat_id)
        url = f"https://api.telegram.org/bot{target.bot_token}/sendVideo"
        with path.open("rb") as video_file:
            response = requests.post(
                url,
                data={
                    "chat_id": target.chat_id,
                    "caption": build_caption(video, quote_text, before_text, after_text),
                    "parse_mode": "HTML",
                    "supports_streaming": "true",
                },
                files={"video": (path.name, video_file, "video/mp4")},
                timeout=180,
            )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram API error: {payload}")

    def publish_youtube(
        self,
        video: YouTubeVideo,
        before_text: str = "",
        after_text: str = "",
        chat_id: str | None = None,
    ) -> None:
        target = self.storage.telegram_destination(chat_id)
        url = f"https://api.telegram.org/bot{target.bot_token}/sendPhoto"
        response = requests.post(
            url,
            data={
                "chat_id": target.chat_id,
                "photo": video.thumbnail_url,
                "caption": build_youtube_caption(video, before_text, after_text),
                "parse_mode": "HTML",
            },
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram API error: {payload}")

    def process_channel(self, channel: str) -> None:
        username, _ = normalize_channel(channel)
        videos = self.scan(channel)
        LOGGER.info("Found %d recent videos for @%s", len(videos), username)

        if not self.config.post_existing and not self.storage.is_channel_initialized(username):
            for video in videos:
                self.storage.mark(video.video_id, username)
            self.storage.mark_channel_initialized(username)
            LOGGER.info("Initial videos for @%s marked as processed", username)
            return

        for video in videos:
            if self.storage.has(video.video_id):
                continue
            path: Path | None = None
            try:
                LOGGER.info("Processing %s", video.url)
                path = self.download(video)
                self.publish(video, path)
                self.storage.mark(video.video_id, username)
                LOGGER.info("Published %s", video.url)
            finally:
                if path and path.exists():
                    path.unlink()
        self.storage.mark_channel_initialized(username)

    def run_forever(self) -> None:
        LOGGER.info("Started; polling every %d seconds", self.config.poll_interval_seconds)
        while True:
            for channel in self.config.tiktok_channels:
                try:
                    self.process_channel(channel)
                except Exception:
                    LOGGER.exception("Failed to process channel %s", channel)
            time.sleep(self.config.poll_interval_seconds)
