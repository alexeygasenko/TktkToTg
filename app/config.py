from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


def _nested(data: dict[str, Any], path: str, default: Any = None) -> Any:
    value: Any = data
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return default
        value = value[part]
    return value


def _value(data: dict[str, Any], env_name: str, yaml_path: str, default: Any = None) -> Any:
    env_value = os.getenv(env_name)
    return env_value if env_value is not None else _nested(data, yaml_path, default)


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _channels(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        value = value.split(",")
    return tuple(str(channel).strip() for channel in (value or []) if str(channel).strip())


def _config_path(value: str, config_file: Path) -> Path | None:
    if not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else config_file.parent / path


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str
    telegram_chat_id: str
    tiktok_channels: tuple[str, ...]
    poll_interval_seconds: int
    scan_limit: int
    post_existing: bool
    data_dir: Path
    cookies_file: Path | None
    youtube_cookies_file: Path | None
    youtube_po_token_provider_url: str | None
    web_host: str
    web_port: int
    web_username: str | None
    web_password: str | None

    @classmethod
    def from_sources(cls) -> "Config":
        default_config = Path("config.yaml") if Path("config.yaml").is_file() else Path("/config/config.yaml")
        config_file = Path(os.getenv("CONFIG_FILE", str(default_config)))
        data: dict[str, Any] = {}
        if config_file.is_file():
            loaded = yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
            if not isinstance(loaded, dict):
                raise ValueError("Config file root must be a YAML mapping")
            data = loaded

        bot_token = str(
            _value(data, "TELEGRAM_BOT_TOKEN", "telegram.bot_token", "")
        ).strip()
        chat_id = str(_value(data, "TELEGRAM_CHAT_ID", "telegram.chat_id", "")).strip()
        if not bot_token:
            raise ValueError("telegram.bot_token is required in config.yaml or environment")
        if not chat_id:
            raise ValueError("telegram.chat_id is required in config.yaml or environment")

        cookies = str(
            _value(data, "TIKTOK_COOKIES_FILE", "tiktok.cookies_file", "")
        ).strip()
        youtube_cookies = str(
            _value(data, "YOUTUBE_COOKIES_FILE", "youtube.cookies_file", "")
        ).strip()
        youtube_po_token_provider_url = str(
            _value(
                data,
                "YOUTUBE_PO_TOKEN_PROVIDER_URL",
                "youtube.po_token_provider_url",
                "http://bgutil-provider:4416",
            )
        ).strip()
        web_username = str(_value(data, "WEB_USERNAME", "web.username", "")).strip()
        web_password = str(_value(data, "WEB_PASSWORD", "web.password", "")).strip()
        return cls(
            telegram_bot_token=bot_token,
            telegram_chat_id=chat_id,
            tiktok_channels=_channels(
                _value(data, "TIKTOK_CHANNELS", "tiktok.channels", [])
            ),
            poll_interval_seconds=max(
                30,
                int(_value(data, "POLL_INTERVAL_SECONDS", "tiktok.poll_interval_seconds", 300)),
            ),
            scan_limit=max(1, int(_value(data, "SCAN_LIMIT", "tiktok.scan_limit", 15))),
            post_existing=_as_bool(
                _value(data, "POST_EXISTING", "tiktok.post_existing", False)
            ),
            data_dir=Path(str(_value(data, "DATA_DIR", "data_dir", "/data"))),
            cookies_file=_config_path(cookies, config_file),
            youtube_cookies_file=_config_path(youtube_cookies, config_file),
            youtube_po_token_provider_url=youtube_po_token_provider_url or None,
            web_host=str(_value(data, "WEB_HOST", "web.host", "0.0.0.0")),
            web_port=int(_value(data, "WEB_PORT", "web.port", 8080)),
            web_username=web_username or None,
            web_password=web_password or None,
        )
