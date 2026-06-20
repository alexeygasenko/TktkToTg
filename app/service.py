from __future__ import annotations

import html
import http.cookiejar
import json
import logging
import mimetypes
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
import yt_dlp

from app.config import Config, TelegramChannel
from app.storage import Storage, User

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
TELEGRAM_ALLOWED_TAGS = {
    "a",
    "b",
    "blockquote",
    "code",
    "i",
    "pre",
    "s",
    "tg-spoiler",
    "u",
}
TELEGRAM_TAG_ALIASES = {
    "strong": "b",
    "em": "i",
    "ins": "u",
    "strike": "s",
    "del": "s",
}


@dataclass(frozen=True)
class Video:
    video_id: str
    username: str
    description: str
    url: str
    timestamp: int
    platform: str = "tiktok"
    author_url: str = ""
    media_type: str = "video"


@dataclass(frozen=True)
class YouTubeVideo:
    video_id: str
    title: str
    url: str
    thumbnail_url: str
    duration: int
    channel: str


class TelegramHTMLSanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized = TELEGRAM_TAG_ALIASES.get(tag, tag)
        if tag in {"div", "p"}:
            self._soft_break()
            return
        if tag == "br":
            self.parts.append("\n")
            return
        if normalized not in TELEGRAM_ALLOWED_TAGS:
            return
        if normalized == "a":
            href = next((value for name, value in attrs if name == "href"), "")
            if not href or not self._safe_href(href):
                return
            self.parts.append(f'<a href="{html.escape(href, quote=True)}">')
        elif normalized == "blockquote":
            expandable = any(name == "expandable" for name, _ in attrs)
            self.parts.append("<blockquote expandable>" if expandable else "<blockquote>")
        else:
            self.parts.append(f"<{normalized}>")
        self.stack.append(normalized)

    def handle_endtag(self, tag: str) -> None:
        normalized = TELEGRAM_TAG_ALIASES.get(tag, tag)
        if tag in {"div", "p"}:
            self._soft_break()
            return
        if normalized not in TELEGRAM_ALLOWED_TAGS:
            return
        if normalized in self.stack:
            while self.stack:
                current = self.stack.pop()
                self.parts.append(f"</{current}>")
                if current == normalized:
                    break

    def handle_data(self, data: str) -> None:
        self.parts.append(html.escape(data))

    def close_open_tags(self) -> None:
        while self.stack:
            self.parts.append(f"</{self.stack.pop()}>")

    def _soft_break(self) -> None:
        text = "".join(self.parts)
        if text and not text.endswith("\n\n"):
            self.parts.append("\n\n" if not text.endswith("\n") else "\n")

    @staticmethod
    def _safe_href(href: str) -> bool:
        parsed = urlparse(href)
        return parsed.scheme in {"http", "https", "tg", "mailto"} and bool(
            parsed.netloc or parsed.scheme == "tg"
        )


def sanitize_telegram_html(value: str) -> str:
    parser = TelegramHTMLSanitizer()
    parser.feed(value or "")
    parser.close_open_tags()
    text = re.sub(r"\n{3,}", "\n\n", "".join(parser.parts)).strip()
    if len(text) > MAX_CAPTION_LENGTH:
        raise ValueError(
            f"Подпись слишком длинная: {len(text)} символов HTML при лимите {MAX_CAPTION_LENGTH}."
        )
    return text


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


def validate_instagram_url(url: str) -> str:
    url = url.strip()
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not (
        host == "instagram.com" or host.endswith(".instagram.com")
    ):
        raise ValueError("Нужна полная ссылка на видео Instagram")
    return url


def is_instagram_url(url: str) -> bool:
    try:
        validate_instagram_url(url)
        return True
    except ValueError:
        return False


def validate_youtube_url(url: str) -> str:
    url = url.strip()
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not (
        host in {"youtube.com", "youtu.be"} or host.endswith(".youtube.com")
    ):
        raise ValueError("Нужна полная ссылка на видео YouTube")
    return url


def is_tiktok_photo_url(url: str) -> bool:
    return "/photo/" in urlparse(validate_tiktok_url(url)).path


def is_tiktok_video_url(url: str) -> bool:
    return "/video/" in urlparse(validate_tiktok_url(url)).path or is_tiktok_photo_url(url)


def tiktok_post_id_from_url(url: str) -> str:
    match = re.search(r"/(?:video|photo)/([^/?#]+)", urlparse(url).path)
    return match.group(1) if match else ""


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


def media_author_from_info(
    info: dict[str, Any], webpage_url: str, platform: str
) -> tuple[str, str]:
    if platform == "instagram":
        username = ""
        for url_key in ("uploader_url", "channel_url"):
            parsed = urlparse(str(info.get(url_key) or ""))
            if parsed.netloc.endswith("instagram.com"):
                candidate = parsed.path.strip("/").split("/", 1)[0]
                if (
                    candidate
                    and candidate not in {"p", "reel", "tv", "stories"}
                    and re.fullmatch(r"[\w.]+", candidate)
                ):
                    username = candidate.lstrip("@")
                    break
        if not username:
            for key in ("uploader", "channel", "creator", "uploader_id"):
                candidate = str(info.get(key) or "").strip().lstrip("@")
                if candidate and not candidate.isdigit() and re.fullmatch(r"[\w.]+", candidate):
                    username = candidate
                    break
        if not username:
            username = "instagram"
        return username, f"https://www.instagram.com/{username}/"
    username = username_from_info(info, webpage_url)
    return username, f"https://www.tiktok.com/@{username}"


def best_thumbnail_url(info: dict[str, Any]) -> str:
    thumbnails = [item for item in info.get("thumbnails") or [] if item.get("url")]
    if thumbnails:
        best = max(
            thumbnails,
            key=lambda item: int(item.get("width") or 0) * int(item.get("height") or 0),
        )
        return str(best["url"])
    return str(info.get("thumbnail") or "")


def image_url_dedupe_key(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if "tiktokcdn" in host or "muscdn" in host or "ttwstatic" in host:
        return parsed.path
    return f"{host}{parsed.path}"


def unique_image_urls(urls: list[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for url in urls:
        key = image_url_dedupe_key(url)
        if key in seen:
            continue
        seen.add(key)
        unique.append(url)
    return unique


def tiktok_image_post_urls_from_data(data: Any) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()

    def add_url(value: Any) -> None:
        if not isinstance(value, str) or not value.startswith(("http://", "https://")):
            return
        key = image_url_dedupe_key(value)
        if key in seen:
            return
        seen.add(key)
        urls.append(value)

    def collect_from_image(value: Any) -> None:
        if isinstance(value, dict):
            for key in ("urlList", "url_list"):
                url_list = value.get(key)
                if isinstance(url_list, list):
                    for item in url_list:
                        add_url(item)
            for item in value.values():
                collect_from_image(item)
        elif isinstance(value, list):
            for item in value:
                collect_from_image(item)

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key in ("imagePost", "image_post_info"):
                image_post = value.get(key)
                if image_post:
                    images = image_post.get("images") if isinstance(image_post, dict) else None
                    collect_from_image(images if images else image_post)
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(data)
    return urls


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
    include_author: bool = True,
    include_description: bool = True,
) -> str:
    author = html.escape(f"@{video.username}")
    author_url = html.escape(
        video.author_url or f"https://www.tiktok.com/@{video.username}", quote=True
    )
    author_link = f'<a href="{author_url}">{author}</a>'
    texts = [
        before_text.strip(),
        (
            video.description.strip() if quote_text is None else quote_text.strip()
        )
        if include_description
        else "",
        after_text.strip(),
    ]

    def render(parts: list[str], truncated: bool = False) -> str:
        before, quote, after = (
            html.escape(part) + ("…" if truncated and part else "") for part in parts
        )
        sections = [author_link] if include_author else []
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


def resolve_caption_html(
    caption_html: str = "",
    fallback: str = "",
) -> str:
    caption = sanitize_telegram_html(caption_html)
    return caption or fallback


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
                user_id=1,
            )
        for channel in self.config.tiktok_channels:
            username, _ = normalize_channel(channel)
            self.storage.add_monitored_tiktok_channel(username, user_id=1)
        self.storage.set_setting(
            "poll_interval_seconds",
            str(self.config.poll_interval_seconds),
            only_if_missing=True,
            user_id=1,
        )
        self.ydl_lock = threading.Lock()
        self.tiktok_cookies_file = self._writable_cookie_copy(
            self.config.cookies_file, "tiktok-cookies.txt", user_id=1
        )
        self.instagram_cookies_file = self._writable_cookie_copy(
            self.config.instagram_cookies_file, "instagram-cookies.txt", user_id=1
        )
        self.youtube_cookies_file = self._writable_cookie_copy(
            self.config.youtube_cookies_file, "youtube-cookies.txt", user_id=1
        )

    def _user_data_dir(self, user_id: int) -> Path:
        destination = self.config.data_dir / "users" / str(user_id)
        destination.mkdir(parents=True, exist_ok=True)
        return destination

    def _writable_cookie_copy(
        self, source: Path | None, filename: str, user_id: int
    ) -> Path | None:
        destination = self._user_data_dir(user_id) / filename
        if destination.is_file():
            return destination
        legacy = self.config.data_dir / filename
        if user_id == 1 and legacy.is_file():
            shutil.copyfile(legacy, destination)
            return destination
        if not source:
            return None
        if not source.is_file():
            raise FileNotFoundError(f"Cookies file not found: {source}")
        shutil.copyfile(source, destination)
        return destination

    def _cookie_file(self, service_name: str, user_id: int = 1) -> Path | None:
        filenames = {
            "tiktok": "tiktok-cookies.txt",
            "instagram": "instagram-cookies.txt",
            "youtube": "youtube-cookies.txt",
        }
        filename = filenames.get(service_name)
        if not filename:
            return None
        path = self._user_data_dir(user_id) / filename
        return path if path.is_file() else None

    def user(self, user_id: int) -> User:
        user = self.storage.get_user(user_id)
        if not user:
            raise ValueError("Пользователь не найден")
        return user

    def ensure_service_allowed(self, service_name: str, user_id: int = 1) -> None:
        user = self.user(user_id)
        if user.is_disabled:
            raise PermissionError("Пользователь отключён")
        if not user.allows(service_name):
            raise PermissionError(f"Сервис {service_name} отключён для пользователя")

    def telegram_channels(self, user_id: int = 1) -> tuple[TelegramChannel, ...]:
        return tuple(
            TelegramChannel(
                destination.name, destination.chat_id, destination.destination_type
            )
            for destination in self.storage.telegram_destinations(user_id)
        )

    def _telegram_chat(self, bot_token: str, chat_id: str) -> dict[str, Any]:
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
                "Telegram не подтвердил доступ. Проверьте токен, ID канала и права бота."
            )
        return dict(payload.get("result") or {})

    def delete_telegram_destination(self, chat_id: str, user_id: int = 1) -> None:
        self.storage.delete_telegram_destination(chat_id.strip(), user_id=user_id)

    def move_telegram_destination(
        self, chat_id: str, direction: str, user_id: int = 1
    ) -> None:
        self.storage.move_telegram_destination(
            chat_id.strip(), -1 if direction == "up" else 1, user_id=user_id
        )

    def add_telegram_destination(
        self, name: str, chat_id: str, bot_token: str, user_id: int = 1
    ) -> None:
        name = name.strip()
        chat_id = chat_id.strip()
        bot_token = bot_token.strip()
        if not name or not chat_id or not bot_token:
            raise ValueError("Укажите название, ID канала и токен бота")
        if not chat_id.startswith("@") and not re.fullmatch(r"-\d+", chat_id):
            raise ValueError(
                "Укажите публичный @тег или отрицательный числовой ID канала или чата"
            )
        result = self._telegram_chat(bot_token, chat_id)
        destination_type = str(result.get("type") or "channel")
        username = str(result.get("username") or "").strip().lstrip("@")
        telegram_id = str(result.get("id") or "").strip() or None
        canonical_chat_id = f"@{username}" if username else chat_id
        if canonical_chat_id != chat_id:
            self.storage.canonicalize_telegram_destination(
                chat_id,
                name,
                canonical_chat_id,
                bot_token,
                destination_type,
                telegram_id,
                user_id=user_id,
            )
        else:
            self.storage.add_telegram_destination(
                name,
                canonical_chat_id,
                bot_token,
                destination_type=destination_type,
                telegram_id=telegram_id,
                user_id=user_id,
            )

    def discover_telegram_destinations(
        self, bot_token: str, user_id: int = 1
    ) -> tuple[TelegramChannel, ...]:
        bot_token = bot_token.strip()
        if not bot_token:
            raise ValueError("Укажите токен бота")
        try:
            response = requests.get(
                f"https://api.telegram.org/bot{bot_token}/getUpdates",
                params={
                    "limit": 100,
                    "timeout": 0,
                    "allowed_updates": '["channel_post","edited_channel_post","my_chat_member"]',
                },
                timeout=30,
            )
            payload = response.json()
        except (requests.RequestException, requests.JSONDecodeError):
            raise ValueError("Не удалось получить каналы через Telegram API") from None
        if not payload.get("ok"):
            raise ValueError(
                "Telegram не разрешил поиск каналов. Проверьте токен и отсутствие webhook."
            )
        found: dict[str, TelegramChannel] = {}
        for update in payload.get("result") or []:
            chat = (
                (update.get("channel_post") or {}).get("chat")
                or (update.get("edited_channel_post") or {}).get("chat")
                or (update.get("my_chat_member") or {}).get("chat")
                or {}
            )
            destination_type = str(chat.get("type") or "")
            if destination_type not in {"channel", "group", "supergroup"} or not chat.get("id"):
                continue
            numeric_chat_id = str(chat["id"])
            try:
                chat = {**chat, **self._telegram_chat(bot_token, numeric_chat_id)}
            except ValueError:
                LOGGER.warning("Could not refresh Telegram chat %s", numeric_chat_id)
            username = str(chat.get("username") or "").strip().lstrip("@")
            chat_id = f"@{username}" if username else numeric_chat_id
            name = str(chat.get("title") or username or numeric_chat_id)
            telegram_id = str(chat.get("id") or numeric_chat_id)
            if username:
                self.storage.canonicalize_telegram_destination(
                    numeric_chat_id,
                    name,
                    chat_id,
                    bot_token,
                    destination_type,
                    telegram_id,
                    user_id=user_id,
                )
            found[chat_id] = TelegramChannel(name, chat_id, destination_type)
        if not found:
            raise ValueError(
                "Каналы не найдены. Добавьте бота администратором и опубликуйте новый пост."
            )
        for channel in found.values():
            if channel.chat_id.startswith("@"):
                continue
            self.storage.add_telegram_destination(
                channel.name,
                channel.chat_id,
                bot_token,
                destination_type=channel.destination_type,
                telegram_id=channel.chat_id if channel.chat_id.startswith("-") else None,
                user_id=user_id,
            )
        return tuple(found.values())

    def monitored_tiktok_channels(self, user_id: int = 1) -> tuple[str, ...]:
        return self.storage.monitored_tiktok_channels(user_id)

    def add_monitored_tiktok_channel(self, channel: str, user_id: int = 1) -> str:
        self.ensure_service_allowed("tiktok", user_id)
        username, _ = normalize_channel(channel)
        self.storage.add_monitored_tiktok_channel(username, user_id)
        return username

    def delete_monitored_tiktok_channel(self, channel: str, user_id: int = 1) -> None:
        self.storage.delete_monitored_tiktok_channel(normalize_channel(channel)[0], user_id)

    def poll_interval_seconds(self, user_id: int = 1) -> int:
        return max(30, int(self.storage.setting("poll_interval_seconds", "300", user_id)))

    def set_poll_interval_seconds(self, value: str, user_id: int = 1) -> int:
        interval = max(30, int(value))
        self.storage.set_setting("poll_interval_seconds", str(interval), user_id=user_id)
        return interval

    def update_cookies(self, service_name: str, content: bytes, user_id: int = 1) -> Path:
        self.ensure_service_allowed(service_name, user_id)
        if len(content) > 5 * 1024 * 1024:
            raise ValueError("Файл cookies не должен превышать 5 МБ")
        if b"Netscape HTTP Cookie File" not in content[:256]:
            raise ValueError("Нужен cookies.txt в Netscape-формате")
        if service_name == "tiktok":
            destination = self._user_data_dir(user_id) / "tiktok-cookies.txt"
            attribute = "tiktok_cookies_file"
        elif service_name == "instagram":
            destination = self._user_data_dir(user_id) / "instagram-cookies.txt"
            attribute = "instagram_cookies_file"
        elif service_name == "youtube":
            destination = self._user_data_dir(user_id) / "youtube-cookies.txt"
            attribute = "youtube_cookies_file"
        else:
            raise ValueError("Неизвестный сервис cookies")
        temporary = destination.with_suffix(".tmp")
        temporary.write_bytes(content)
        temporary.replace(destination)
        if user_id == 1:
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

    def _cookie_session(self, cookies_file: Path | None = None) -> requests.Session:
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0 Safari/537.36"
                )
            }
        )
        if cookies_file and cookies_file.exists():
            jar = http.cookiejar.MozillaCookieJar(str(cookies_file))
            try:
                jar.load(ignore_discard=True, ignore_expires=True)
                session.cookies.update(jar)
            except Exception:
                LOGGER.warning("Could not load cookies from %s", cookies_file)
        return session

    def _tiktok_image_post_urls(self, url: str, cookies_file: Path | None) -> list[str]:
        response = self._cookie_session(cookies_file).get(url, timeout=60)
        response.raise_for_status()
        script_matches = re.findall(
            r'<script[^>]+id="(?:SIGI_STATE|__UNIVERSAL_DATA_FOR_REHYDRATION__)"[^>]*>(.*?)</script>',
            response.text,
            flags=re.DOTALL,
        )
        urls: list[str] = []
        seen: set[str] = set()
        for script in script_matches:
            try:
                data = json.loads(html.unescape(script))
            except json.JSONDecodeError:
                continue
            for image_url in tiktok_image_post_urls_from_data(data):
                key = image_url_dedupe_key(image_url)
                if key in seen:
                    continue
                seen.add(key)
                urls.append(image_url)
        return urls

    def _tikwm_image_post_info(self, url: str) -> tuple[dict[str, Any], list[str]]:
        response = requests.get(
            "https://www.tikwm.com/api/",
            params={"url": url},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return {}, []
        image_urls = unique_image_urls(
            [
                str(image_url)
                for image_url in data.get("images") or []
                if str(image_url).startswith(("http://", "https://"))
            ]
        )
        author = data.get("author") if isinstance(data.get("author"), dict) else {}
        info = {
            "id": str(data.get("id") or tiktok_post_id_from_url(url)),
            "description": str(data.get("title") or data.get("content_desc") or ""),
            "title": str(data.get("title") or data.get("content_desc") or ""),
            "uploader": str(author.get("unique_id") or ""),
            "channel": str(author.get("nickname") or ""),
            "timestamp": int(data.get("create_time") or 0),
            "webpage_url": url,
        }
        return info, image_urls

    def _download_images(
        self, urls: list[str], output_id: str, cookies_file: Path | None
    ) -> tuple[Path, ...]:
        urls = unique_image_urls(urls)
        if not urls:
            raise ValueError("TikTok-коллаж не содержит картинок")
        session = self._cookie_session(cookies_file)
        paths: list[Path] = []
        try:
            for index, image_url in enumerate(urls, start=1):
                response = session.get(image_url, timeout=90)
                response.raise_for_status()
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0]
                extension = mimetypes.guess_extension(content_type) or ".jpg"
                if extension == ".jpe":
                    extension = ".jpg"
                path = self.download_dir / f"{output_id}-{index:02d}{extension}"
                path.write_bytes(response.content)
                paths.append(path)
        except Exception:
            for path in paths:
                if path.exists():
                    path.unlink()
            raise
        return tuple(paths)

    def _download_video_file(
        self, url: str, platform: str, output_id: str, user_id: int
    ) -> tuple[dict[str, Any], Path]:
        output_template = str(self.download_dir / f"{output_id}.%(ext)s")
        options = {
            **self._ydl_options(self._cookie_file(platform, user_id)),
            "format": "best[ext=mp4][filesize<49M]/best[filesize<49M]/best[ext=mp4]/best",
            "merge_output_format": "mp4",
            "outtmpl": output_template,
            "noplaylist": True,
        }
        with self.ydl_lock, yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
            path = Path(ydl.prepare_filename(info))

        if not path.exists():
            matches = list(self.download_dir.glob(f"{output_id}.*"))
            if not matches:
                raise FileNotFoundError("Скачанное видео не найдено")
            path = matches[0]
        if path.stat().st_size > MAX_VIDEO_BYTES:
            path.unlink()
            raise ValueError("Видео больше лимита Telegram Bot API в 50 МБ")
        return info, path

    def _extract_info(self, url: str, platform: str, user_id: int) -> dict[str, Any]:
        options = {
            **self._ydl_options(self._cookie_file(platform, user_id)),
            "noplaylist": True,
            "skip_download": True,
        }
        with self.ydl_lock, yt_dlp.YoutubeDL(options) as ydl:
            return ydl.extract_info(url, download=False)

    def _prepare_media(
        self, url: str, platform: str, output_id: str, user_id: int
    ) -> tuple[Video, tuple[Path, ...]]:
        info: dict[str, Any] = {}
        webpage_url = url
        media_type = "video"
        paths: tuple[Path, ...]

        if platform == "tiktok" and is_tiktok_photo_url(url):
            image_urls = self._tiktok_image_post_urls(url, self._cookie_file(platform, user_id))
            if not image_urls:
                info, image_urls = self._tikwm_image_post_info(url)
            if not image_urls:
                raise ValueError("Не удалось получить картинки TikTok-коллажа")
            media_type = "image"
            paths = self._download_images(
                image_urls, output_id, self._cookie_file(platform, user_id)
            )
        elif platform == "tiktok":
            info = self._extract_info(url, platform, user_id)
            webpage_url = str(info.get("webpage_url") or url)
            try:
                image_urls = self._tiktok_image_post_urls(
                    webpage_url, self._cookie_file(platform, user_id)
                )
            except Exception as error:
                LOGGER.info("Could not inspect TikTok image post %s: %s", webpage_url, error)
                image_urls = []
            if image_urls:
                media_type = "image"
                paths = self._download_images(
                    image_urls, output_id, self._cookie_file(platform, user_id)
                )
            else:
                info, path = self._download_video_file(url, platform, output_id, user_id)
                paths = (path,)
        else:
            info = self._extract_info(url, platform, user_id)
            webpage_url = str(info.get("webpage_url") or url)
            info, path = self._download_video_file(url, platform, output_id, user_id)
            paths = (path,)

        webpage_url = str(info.get("webpage_url") or webpage_url or url)
        username, author_url = media_author_from_info(info, webpage_url, platform)
        video = Video(
            video_id=str(info.get("id") or tiktok_post_id_from_url(webpage_url) or output_id),
            username=username,
            description=str(info.get("description") or info.get("title") or ""),
            url=webpage_url,
            timestamp=int(info.get("timestamp") or 0),
            platform=platform,
            author_url=author_url,
            media_type=media_type,
        )
        return video, paths

    def scan(self, channel: str, user_id: int = 1) -> list[Video]:
        self.ensure_service_allowed("tiktok", user_id)
        username, channel_url = normalize_channel(channel)
        options = {
            **self._ydl_options(self._cookie_file("tiktok", user_id)),
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

    def download(
        self, video: Video, output_id: str | None = None, user_id: int = 1
    ) -> tuple[Path, ...]:
        self.ensure_service_allowed(video.platform, user_id)
        output_id = output_id or video.video_id
        _, paths = self._prepare_media(video.url, video.platform, output_id, user_id)
        return paths

    def prepare_url(self, url: str, user_id: int = 1) -> tuple[Video, tuple[Path, ...]]:
        platform = "instagram" if is_instagram_url(url) else "tiktok"
        self.ensure_service_allowed(platform, user_id)
        url = validate_instagram_url(url) if platform == "instagram" else validate_tiktok_url(url)
        output_id = f"manual-{uuid.uuid4().hex}"
        try:
            return self._prepare_media(url, platform, output_id, user_id)
        except Exception:
            for partial_file in self.download_dir.glob(f"{output_id}.*"):
                partial_file.unlink()
            raise

    def get_youtube_info(self, url: str, user_id: int = 1) -> YouTubeVideo:
        self.ensure_service_allowed("youtube", user_id)
        url = validate_youtube_url(url)
        errors: list[Exception] = []
        info: dict[str, Any] | None = None
        youtube_cookies_file = self._cookie_file("youtube", user_id)
        cookie_candidates = [youtube_cookies_file, None] if youtube_cookies_file else [None]
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
            if youtube_cookies_file and not has_youtube_auth_cookies(
                youtube_cookies_file
            ):
                raise ValueError(
                    "youtube-cookies.txt содержит только гостевые cookies. "
                    "Экспортируйте cookies после входа в аккаунт YouTube; в файле "
                    "должны присутствовать SID, SSID, SAPISID или LOGIN_INFO."
                ) from errors[-1]
            if youtube_cookies_file and self.config.youtube_po_token_provider_url:
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

    def download_youtube(
        self, video: YouTubeVideo, output_id: str, user_id: int = 1
    ) -> Path:
        self.ensure_service_allowed("youtube", user_id)
        errors: list[Exception] = []
        youtube_cookies_file = self._cookie_file("youtube", user_id)
        cookie_candidates = [youtube_cookies_file, None] if youtube_cookies_file else [None]
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
            if youtube_cookies_file and not has_youtube_auth_cookies(
                youtube_cookies_file
            ):
                raise ValueError(
                    "youtube-cookies.txt содержит только гостевые cookies. "
                    "Экспортируйте cookies после входа в аккаунт YouTube; в файле "
                    "должны присутствовать SID, SSID, SAPISID или LOGIN_INFO."
                ) from errors[-1]
            if youtube_cookies_file and self.config.youtube_po_token_provider_url:
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
        self,
        channel: str,
        post_existing: bool,
        chat_id: str | None = None,
        user_id: int = 1,
    ) -> tuple[int, int]:
        self.ensure_service_allowed("tiktok", user_id)
        username, _ = normalize_channel(channel)
        self.storage.add_monitored_tiktok_channel(username, user_id)
        destination = self.storage.telegram_destination(chat_id, user_id).chat_id
        videos = self.scan(channel, user_id)
        if not post_existing:
            for video in videos:
                self.storage.mark(video.video_id, username, user_id)
            self.storage.mark_channel_initialized(username, user_id)
            return len(videos), 0

        published = 0
        for video in videos:
            if self.storage.has(video.video_id, user_id):
                continue
            paths: tuple[Path, ...] = ()
            try:
                prepared_video, paths = self._prepare_media(
                    video.url, video.platform, video.video_id, user_id
                )
                self.publish(prepared_video, paths, chat_id=destination, user_id=user_id)
                self.storage.mark(video.video_id, username, user_id)
                published += 1
            finally:
                for path in paths:
                    if path.exists():
                        path.unlink()
        self.storage.mark_channel_initialized(username, user_id)
        return len(videos), published

    def publish(
        self,
        video: Video,
        path: Path | tuple[Path, ...],
        quote_text: str | None = None,
        before_text: str = "",
        after_text: str = "",
        chat_id: str | None = None,
        include_author: bool = True,
        include_description: bool = True,
        caption_html: str = "",
        user_id: int = 1,
    ) -> None:
        self.ensure_service_allowed(video.platform, user_id)
        target = self.storage.telegram_destination(chat_id, user_id)
        fallback_caption = build_caption(
            video,
            quote_text,
            before_text,
            after_text,
            include_author,
            include_description,
        )
        caption = resolve_caption_html(caption_html, fallback_caption)
        paths = (path,) if isinstance(path, Path) else path
        if not paths:
            raise ValueError("Медиафайлы не найдены")

        if video.media_type == "image":
            if len(paths) == 1:
                url = f"https://api.telegram.org/bot{target.bot_token}/sendPhoto"
                with paths[0].open("rb") as photo_file:
                    response = requests.post(
                        url,
                        data={
                            "chat_id": target.chat_id,
                            "caption": caption,
                            "parse_mode": "HTML",
                        },
                        files={"photo": (paths[0].name, photo_file, "image/jpeg")},
                        timeout=180,
                    )
                response.raise_for_status()
                payload = response.json()
                if not payload.get("ok"):
                    raise RuntimeError(f"Telegram API error: {payload}")
                return

            url = f"https://api.telegram.org/bot{target.bot_token}/sendMediaGroup"
            first_chunk = True
            for chunk_start in range(0, len(paths), 10):
                chunk = paths[chunk_start : chunk_start + 10]
                files = {}
                open_files = []
                media = []
                try:
                    for index, image_path in enumerate(chunk):
                        field_name = f"photo{index}"
                        file_handle = image_path.open("rb")
                        open_files.append(file_handle)
                        files[field_name] = (image_path.name, file_handle, "image/jpeg")
                        item: dict[str, str] = {
                            "type": "photo",
                            "media": f"attach://{field_name}",
                        }
                        if first_chunk and index == 0:
                            item["caption"] = caption
                            item["parse_mode"] = "HTML"
                        media.append(item)
                    response = requests.post(
                        url,
                        data={"chat_id": target.chat_id, "media": json.dumps(media)},
                        files=files,
                        timeout=180,
                    )
                finally:
                    for file_handle in open_files:
                        file_handle.close()
                response.raise_for_status()
                payload = response.json()
                if not payload.get("ok"):
                    raise RuntimeError(f"Telegram API error: {payload}")
                first_chunk = False
            return

        video_path = paths[0]
        url = f"https://api.telegram.org/bot{target.bot_token}/sendVideo"
        with video_path.open("rb") as video_file:
            response = requests.post(
                url,
                data={
                    "chat_id": target.chat_id,
                    "caption": caption,
                    "parse_mode": "HTML",
                    "supports_streaming": "true",
                },
                files={"video": (video_path.name, video_file, "video/mp4")},
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
        caption_html: str = "",
        user_id: int = 1,
    ) -> None:
        self.ensure_service_allowed("youtube", user_id)
        target = self.storage.telegram_destination(chat_id, user_id)
        url = f"https://api.telegram.org/bot{target.bot_token}/sendPhoto"
        response = requests.post(
            url,
            data={
                "chat_id": target.chat_id,
                "photo": video.thumbnail_url,
                "caption": resolve_caption_html(
                    caption_html, build_youtube_caption(video, before_text, after_text)
                ),
                "parse_mode": "HTML",
            },
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram API error: {payload}")

    def process_channel(self, channel: str, user_id: int = 1) -> None:
        username, _ = normalize_channel(channel)
        videos = self.scan(channel, user_id)
        LOGGER.info("Found %d recent videos for @%s", len(videos), username)

        if not self.config.post_existing and not self.storage.is_channel_initialized(username, user_id):
            for video in videos:
                self.storage.mark(video.video_id, username, user_id)
            self.storage.mark_channel_initialized(username, user_id)
            LOGGER.info("Initial videos for @%s marked as processed", username)
            return

        for video in videos:
            if self.storage.has(video.video_id, user_id):
                continue
            paths: tuple[Path, ...] = ()
            try:
                LOGGER.info("Processing %s", video.url)
                prepared_video, paths = self._prepare_media(
                    video.url, video.platform, video.video_id, user_id
                )
                self.publish(prepared_video, paths, user_id=user_id)
                self.storage.mark(video.video_id, username, user_id)
                LOGGER.info("Published %s", video.url)
            finally:
                for path in paths:
                    if path.exists():
                        path.unlink()
        self.storage.mark_channel_initialized(username, user_id)

    def run_forever(self) -> None:
        LOGGER.info("TikTok monitor started")
        while True:
            sleep_seconds = self.poll_interval_seconds()
            for user in self.storage.active_users():
                if not user.allow_tiktok:
                    continue
                sleep_seconds = min(sleep_seconds, self.poll_interval_seconds(user.id))
                for channel in self.monitored_tiktok_channels(user.id):
                    try:
                        self.process_channel(channel, user.id)
                    except Exception:
                        LOGGER.exception(
                            "Failed to process channel %s for user %s",
                            channel,
                            user.username,
                        )
            time.sleep(sleep_seconds)
