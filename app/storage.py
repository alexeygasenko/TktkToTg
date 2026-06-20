from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TelegramDestination:
    name: str
    chat_id: str
    bot_token: str
    is_default: bool = False
    destination_type: str = "channel"
    sort_order: int = 0
    telegram_id: str | None = None


@dataclass(frozen=True)
class User:
    id: int
    username: str
    password_hash: str | None
    is_admin: bool = False
    is_disabled: bool = False
    allow_tiktok: bool = True
    allow_instagram: bool = True
    allow_youtube: bool = True
    must_set_password: bool = False
    created_at: str = ""

    def allows(self, service_name: str) -> bool:
        if service_name == "tiktok":
            return self.allow_tiktok
        if service_name == "instagram":
            return self.allow_instagram
        if service_name == "youtube":
            return self.allow_youtube
        return False


class Storage:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.execute("PRAGMA foreign_keys = ON")
        self._ensure_schema()
        self.connection.commit()

    def _table_columns(self, table: str) -> set[str]:
        return {
            row[1]
            for row in self.connection.execute(f"PRAGMA table_info({table})").fetchall()
        }

    def _table_exists(self, table: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        return row is not None

    def _ensure_schema(self) -> None:
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT,
                is_admin INTEGER NOT NULL DEFAULT 0,
                is_disabled INTEGER NOT NULL DEFAULT 0,
                allow_tiktok INTEGER NOT NULL DEFAULT 1,
                allow_instagram INTEGER NOT NULL DEFAULT 1,
                allow_youtube INTEGER NOT NULL DEFAULT 1,
                must_set_password INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self.connection.execute(
            """
            INSERT OR IGNORE INTO users(
                id, username, password_hash, is_admin, is_disabled,
                allow_tiktok, allow_instagram, allow_youtube, must_set_password
            )
            VALUES (1, 'boyd', NULL, 1, 0, 1, 1, 1, 1)
            """
        )
        self._ensure_processed_videos()
        self._ensure_initialized_channels()
        self._ensure_telegram_destinations()
        self._ensure_deleted_telegram_destinations()
        self._ensure_monitored_tiktok_channels()
        self._ensure_app_settings()

    def _ensure_processed_videos(self) -> None:
        if (
            self._table_exists("processed_videos")
            and "user_id" not in self._table_columns("processed_videos")
        ):
            self.connection.execute(
                "ALTER TABLE processed_videos RENAME TO processed_videos_old"
            )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_videos (
                user_id INTEGER NOT NULL DEFAULT 1,
                video_id TEXT NOT NULL,
                channel TEXT NOT NULL,
                processed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(user_id, video_id)
            )
            """
        )
        if self._table_exists("processed_videos_old"):
            self.connection.execute(
                """
                INSERT OR IGNORE INTO processed_videos(user_id, video_id, channel, processed_at)
                SELECT 1, video_id, channel, processed_at FROM processed_videos_old
                """
            )
            self.connection.execute("DROP TABLE processed_videos_old")

    def _ensure_initialized_channels(self) -> None:
        if (
            self._table_exists("initialized_channels")
            and "user_id" not in self._table_columns("initialized_channels")
        ):
            self.connection.execute(
                "ALTER TABLE initialized_channels RENAME TO initialized_channels_old"
            )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS initialized_channels (
                user_id INTEGER NOT NULL DEFAULT 1,
                channel TEXT NOT NULL,
                initialized_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(user_id, channel)
            )
            """
        )
        if self._table_exists("initialized_channels_old"):
            self.connection.execute(
                """
                INSERT OR IGNORE INTO initialized_channels(user_id, channel, initialized_at)
                SELECT 1, channel, initialized_at FROM initialized_channels_old
                """
            )
            self.connection.execute("DROP TABLE initialized_channels_old")

    def _ensure_telegram_destinations(self) -> None:
        old_columns: set[str] = set()
        if self._table_exists("telegram_destinations"):
            columns = self._table_columns("telegram_destinations")
            if "user_id" not in columns or "id" not in columns:
                old_columns = columns
                self.connection.execute(
                    "ALTER TABLE telegram_destinations RENAME TO telegram_destinations_old"
                )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS telegram_destinations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL DEFAULT 1,
                chat_id TEXT NOT NULL,
                name TEXT NOT NULL,
                bot_token TEXT NOT NULL,
                is_default INTEGER NOT NULL DEFAULT 0,
                destination_type TEXT NOT NULL DEFAULT 'channel',
                sort_order INTEGER NOT NULL DEFAULT 0,
                telegram_id TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, chat_id)
            )
            """
        )
        if self._table_exists("telegram_destinations_old"):
            destination_type = (
                "destination_type" if "destination_type" in old_columns else "'channel'"
            )
            sort_order = "sort_order" if "sort_order" in old_columns else "0"
            telegram_id = "telegram_id" if "telegram_id" in old_columns else "NULL"
            self.connection.execute(
                f"""
                INSERT OR IGNORE INTO telegram_destinations(
                    user_id, chat_id, name, bot_token, is_default,
                    destination_type, sort_order, telegram_id, created_at
                )
                SELECT 1, chat_id, name, bot_token, is_default,
                       {destination_type}, {sort_order}, {telegram_id}, created_at
                FROM telegram_destinations_old
                """
            )
            self.connection.execute("DROP TABLE telegram_destinations_old")

    def _ensure_deleted_telegram_destinations(self) -> None:
        if (
            self._table_exists("deleted_telegram_destinations")
            and "user_id" not in self._table_columns("deleted_telegram_destinations")
        ):
            self.connection.execute(
                "ALTER TABLE deleted_telegram_destinations RENAME TO deleted_telegram_destinations_old"
            )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS deleted_telegram_destinations (
                user_id INTEGER NOT NULL DEFAULT 1,
                chat_id TEXT NOT NULL,
                deleted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(user_id, chat_id)
            )
            """
        )
        if self._table_exists("deleted_telegram_destinations_old"):
            self.connection.execute(
                """
                INSERT OR IGNORE INTO deleted_telegram_destinations(user_id, chat_id, deleted_at)
                SELECT 1, chat_id, deleted_at FROM deleted_telegram_destinations_old
                """
            )
            self.connection.execute("DROP TABLE deleted_telegram_destinations_old")

    def _ensure_monitored_tiktok_channels(self) -> None:
        if (
            self._table_exists("monitored_tiktok_channels")
            and "user_id" not in self._table_columns("monitored_tiktok_channels")
        ):
            self.connection.execute(
                "ALTER TABLE monitored_tiktok_channels RENAME TO monitored_tiktok_channels_old"
            )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS monitored_tiktok_channels (
                user_id INTEGER NOT NULL DEFAULT 1,
                channel TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(user_id, channel)
            )
            """
        )
        if self._table_exists("monitored_tiktok_channels_old"):
            self.connection.execute(
                """
                INSERT OR IGNORE INTO monitored_tiktok_channels(user_id, channel, created_at)
                SELECT 1, channel, created_at FROM monitored_tiktok_channels_old
                """
            )
            self.connection.execute("DROP TABLE monitored_tiktok_channels_old")

    def _ensure_app_settings(self) -> None:
        if (
            self._table_exists("app_settings")
            and "user_id" not in self._table_columns("app_settings")
        ):
            self.connection.execute("ALTER TABLE app_settings RENAME TO app_settings_old")
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS app_settings (
                user_id INTEGER NOT NULL DEFAULT 1,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                PRIMARY KEY(user_id, key)
            )
            """
        )
        if self._table_exists("app_settings_old"):
            self.connection.execute(
                """
                INSERT OR IGNORE INTO app_settings(user_id, key, value)
                SELECT 1, key, value FROM app_settings_old
                """
            )
            self.connection.execute("DROP TABLE app_settings_old")

    def _user_from_row(self, row) -> User:
        return User(
            int(row[0]),
            str(row[1]),
            row[2],
            bool(row[3]),
            bool(row[4]),
            bool(row[5]),
            bool(row[6]),
            bool(row[7]),
            bool(row[8]),
            str(row[9]),
        )

    def get_user(self, user_id: int) -> User | None:
        with self.lock:
            row = self.connection.execute(
                """
                SELECT id, username, password_hash, is_admin, is_disabled,
                       allow_tiktok, allow_instagram, allow_youtube,
                       must_set_password, created_at
                FROM users WHERE id = ?
                """,
                (user_id,),
            ).fetchone()
        return self._user_from_row(row) if row else None

    def get_user_by_username(self, username: str) -> User | None:
        with self.lock:
            row = self.connection.execute(
                """
                SELECT id, username, password_hash, is_admin, is_disabled,
                       allow_tiktok, allow_instagram, allow_youtube,
                       must_set_password, created_at
                FROM users WHERE username = ? COLLATE NOCASE
                """,
                (username.strip(),),
            ).fetchone()
        return self._user_from_row(row) if row else None

    def create_user(self, username: str, password_hash: str, *, is_admin: bool = False) -> User:
        with self.lock:
            cursor = self.connection.execute(
                """
                INSERT INTO users(username, password_hash, is_admin)
                VALUES (?, ?, ?)
                """,
                (username.strip(), password_hash, int(is_admin)),
            )
            self.connection.commit()
        user = self.get_user(int(cursor.lastrowid))
        assert user is not None
        return user

    def set_user_password(self, user_id: int, password_hash: str) -> None:
        with self.lock:
            self.connection.execute(
                """
                UPDATE users
                SET password_hash = ?, must_set_password = 0
                WHERE id = ?
                """,
                (password_hash, user_id),
            )
            self.connection.commit()

    def update_user(
        self,
        user_id: int,
        *,
        username: str,
        is_admin: bool,
        is_disabled: bool,
        allow_tiktok: bool,
        allow_instagram: bool,
        allow_youtube: bool,
    ) -> None:
        if user_id == 1:
            is_admin = True
        with self.lock:
            self.connection.execute(
                """
                UPDATE users
                SET username = ?, is_admin = ?, is_disabled = ?,
                    allow_tiktok = ?, allow_instagram = ?, allow_youtube = ?
                WHERE id = ?
                """,
                (
                    username.strip(),
                    int(is_admin),
                    int(is_disabled),
                    int(allow_tiktok),
                    int(allow_instagram),
                    int(allow_youtube),
                    user_id,
                ),
            )
            self.connection.commit()

    def user_count(self) -> int:
        with self.lock:
            return int(self.connection.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    def users(self, *, limit: int = 20, offset: int = 0) -> tuple[User, ...]:
        with self.lock:
            rows = self.connection.execute(
                """
                SELECT id, username, password_hash, is_admin, is_disabled,
                       allow_tiktok, allow_instagram, allow_youtube,
                       must_set_password, created_at
                FROM users
                ORDER BY id
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
        return tuple(self._user_from_row(row) for row in rows)

    def active_users(self) -> tuple[User, ...]:
        with self.lock:
            rows = self.connection.execute(
                """
                SELECT id, username, password_hash, is_admin, is_disabled,
                       allow_tiktok, allow_instagram, allow_youtube,
                       must_set_password, created_at
                FROM users
                WHERE is_disabled = 0
                ORDER BY id
                """
            ).fetchall()
        return tuple(self._user_from_row(row) for row in rows)

    def has(self, video_id: str, user_id: int = 1) -> bool:
        with self.lock:
            row = self.connection.execute(
                "SELECT 1 FROM processed_videos WHERE user_id = ? AND video_id = ?",
                (user_id, video_id),
            ).fetchone()
        return row is not None

    def mark(self, video_id: str, channel: str, user_id: int = 1) -> None:
        with self.lock:
            self.connection.execute(
                "INSERT OR IGNORE INTO processed_videos(user_id, video_id, channel) VALUES (?, ?, ?)",
                (user_id, video_id, channel),
            )
            self.connection.commit()

    def is_channel_initialized(self, channel: str, user_id: int = 1) -> bool:
        with self.lock:
            row = self.connection.execute(
                "SELECT 1 FROM initialized_channels WHERE user_id = ? AND channel = ?",
                (user_id, channel),
            ).fetchone()
        return row is not None

    def mark_channel_initialized(self, channel: str, user_id: int = 1) -> None:
        with self.lock:
            self.connection.execute(
                "INSERT OR IGNORE INTO initialized_channels(user_id, channel) VALUES (?, ?)",
                (user_id, channel),
            )
            self.connection.commit()

    def add_telegram_destination(
        self,
        name: str,
        chat_id: str,
        bot_token: str,
        *,
        is_default: bool = False,
        replace: bool = True,
        destination_type: str = "channel",
        telegram_id: str | None = None,
        user_id: int = 1,
    ) -> None:
        with self.lock:
            if not replace:
                deleted = self.connection.execute(
                    "SELECT 1 FROM deleted_telegram_destinations WHERE user_id = ? AND chat_id = ?",
                    (user_id, chat_id),
                ).fetchone()
                if deleted:
                    return
                existing = self.connection.execute(
                    "SELECT 1 FROM telegram_destinations WHERE user_id = ? AND chat_id = ?",
                    (user_id, chat_id),
                ).fetchone()
                if existing:
                    has_default = self.connection.execute(
                        "SELECT 1 FROM telegram_destinations WHERE user_id = ? AND is_default = 1 LIMIT 1",
                        (user_id,),
                    ).fetchone()
                    if is_default and not has_default:
                        self.connection.execute(
                            "UPDATE telegram_destinations SET is_default = 1 WHERE user_id = ? AND chat_id = ?",
                            (user_id, chat_id),
                        )
                        self.connection.commit()
                    return
            else:
                self.connection.execute(
                    "DELETE FROM deleted_telegram_destinations WHERE user_id = ? AND chat_id = ?",
                    (user_id, chat_id),
                )
            if is_default:
                self.connection.execute(
                    "UPDATE telegram_destinations SET is_default = 0 WHERE user_id = ?",
                    (user_id,),
                )
            query = (
                """
                INSERT INTO telegram_destinations(user_id, name, chat_id, bot_token, is_default, destination_type, telegram_id, sort_order)
                VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE((SELECT MAX(sort_order) + 1 FROM telegram_destinations WHERE user_id = ?), 0))
                ON CONFLICT(user_id, chat_id) DO UPDATE SET
                    name = excluded.name,
                    bot_token = excluded.bot_token,
                    destination_type = excluded.destination_type,
                    telegram_id = COALESCE(excluded.telegram_id, telegram_destinations.telegram_id),
                    is_default = CASE
                        WHEN excluded.is_default = 1 THEN 1
                        ELSE telegram_destinations.is_default
                    END
                """
                if replace
                else """
                INSERT OR IGNORE INTO telegram_destinations(user_id, name, chat_id, bot_token, is_default, destination_type, telegram_id, sort_order)
                VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE((SELECT MAX(sort_order) + 1 FROM telegram_destinations WHERE user_id = ?), 0))
                """
            )
            self.connection.execute(
                query,
                (
                    user_id,
                    name,
                    chat_id,
                    bot_token,
                    int(is_default),
                    destination_type,
                    telegram_id,
                    user_id,
                ),
            )
            self.connection.commit()

    def delete_telegram_destination(
        self, chat_id: str, *, remember: bool = True, user_id: int = 1
    ) -> None:
        with self.lock:
            count = self.connection.execute(
                "SELECT COUNT(*) FROM telegram_destinations WHERE user_id = ?",
                (user_id,),
            ).fetchone()[0]
            if count <= 1:
                raise ValueError("Нельзя удалить единственный Telegram-канал")
            row = self.connection.execute(
                "SELECT is_default FROM telegram_destinations WHERE user_id = ? AND chat_id = ?",
                (user_id, chat_id),
            ).fetchone()
            if not row:
                raise ValueError("Telegram-канал уже удалён")
            self.connection.execute(
                "DELETE FROM telegram_destinations WHERE user_id = ? AND chat_id = ?",
                (user_id, chat_id),
            )
            if remember:
                self.connection.execute(
                    "INSERT OR REPLACE INTO deleted_telegram_destinations(user_id, chat_id) VALUES (?, ?)",
                    (user_id, chat_id),
                )
            if row[0]:
                next_chat_id = self.connection.execute(
                    """
                    SELECT chat_id FROM telegram_destinations
                    WHERE user_id = ?
                    ORDER BY sort_order, created_at LIMIT 1
                    """,
                    (user_id,),
                ).fetchone()[0]
                self.connection.execute(
                    "UPDATE telegram_destinations SET is_default = 1 WHERE user_id = ? AND chat_id = ?",
                    (user_id, next_chat_id),
                )
            self.connection.commit()

    def canonicalize_telegram_destination(
        self,
        previous_chat_id: str,
        name: str,
        chat_id: str,
        bot_token: str,
        destination_type: str = "channel",
        telegram_id: str | None = None,
        user_id: int = 1,
    ) -> None:
        with self.lock:
            previous = self.connection.execute(
                """
                SELECT is_default, sort_order FROM telegram_destinations
                WHERE user_id = ? AND chat_id = ?
                """,
                (user_id, previous_chat_id),
            ).fetchone()
            existing = self.connection.execute(
                """
                SELECT is_default, sort_order FROM telegram_destinations
                WHERE user_id = ? AND chat_id = ?
                """,
                (user_id, chat_id),
            ).fetchone()
            same_telegram_id = (
                self.connection.execute(
                    """
                    SELECT is_default, sort_order FROM telegram_destinations
                    WHERE user_id = ? AND telegram_id = ?
                    """,
                    (user_id, telegram_id),
                ).fetchall()
                if telegram_id
                else []
            )
            same_public_name = self.connection.execute(
                """
                SELECT is_default, sort_order FROM telegram_destinations
                WHERE user_id = ? AND chat_id LIKE '@%' AND name = ? AND destination_type = ?
                """,
                (user_id, name, destination_type),
            ).fetchall()
            rows = [
                row
                for row in [previous, existing, *same_telegram_id, *same_public_name]
                if row is not None
            ]
            is_default = any(row[0] for row in rows)
            sort_order = min([row[1] for row in rows] or [0])
            self.connection.execute(
                """
                DELETE FROM telegram_destinations
                WHERE user_id = ?
                  AND (chat_id IN (?, ?)
                   OR (? IS NOT NULL AND telegram_id = ?)
                   OR (chat_id LIKE '@%' AND name = ? AND destination_type = ?))
                """,
                (
                    user_id,
                    previous_chat_id,
                    chat_id,
                    telegram_id,
                    telegram_id,
                    name,
                    destination_type,
                ),
            )
            self.connection.execute(
                """
                INSERT INTO telegram_destinations(user_id, name, chat_id, bot_token, is_default, destination_type, telegram_id, sort_order)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    name,
                    chat_id,
                    bot_token,
                    int(is_default),
                    destination_type,
                    telegram_id,
                    sort_order,
                ),
            )
            self.connection.execute(
                "DELETE FROM deleted_telegram_destinations WHERE user_id = ? AND chat_id = ?",
                (user_id, chat_id),
            )
            self.connection.commit()

    def telegram_destinations(self, user_id: int = 1) -> tuple[TelegramDestination, ...]:
        with self.lock:
            rows = self.connection.execute(
                """
                SELECT name, chat_id, bot_token, is_default, destination_type, sort_order, telegram_id
                FROM telegram_destinations
                WHERE user_id = ?
                ORDER BY sort_order, is_default DESC, name COLLATE NOCASE, chat_id
                """,
                (user_id,),
            ).fetchall()
        return tuple(
            TelegramDestination(
                name,
                chat_id,
                bot_token,
                bool(is_default),
                destination_type,
                sort_order,
                telegram_id,
            )
            for (
                name,
                chat_id,
                bot_token,
                is_default,
                destination_type,
                sort_order,
                telegram_id,
            ) in rows
        )

    def telegram_destination(
        self, chat_id: str | None = None, user_id: int = 1
    ) -> TelegramDestination:
        with self.lock:
            if chat_id:
                row = self.connection.execute(
                    """
                    SELECT name, chat_id, bot_token, is_default, destination_type, sort_order, telegram_id
                    FROM telegram_destinations WHERE user_id = ? AND chat_id = ?
                    """,
                    (user_id, chat_id),
                ).fetchone()
            else:
                row = self.connection.execute(
                    """
                    SELECT name, chat_id, bot_token, is_default, destination_type, sort_order, telegram_id
                    FROM telegram_destinations
                    WHERE user_id = ?
                    ORDER BY sort_order, is_default DESC, created_at
                    LIMIT 1
                    """,
                    (user_id,),
                ).fetchone()
        if not row:
            raise ValueError("Выбран неизвестный Telegram-канал")
        return TelegramDestination(
            row[0], row[1], row[2], bool(row[3]), row[4], row[5], row[6]
        )

    def move_telegram_destination(
        self, chat_id: str, direction: int, user_id: int = 1
    ) -> None:
        destinations = list(self.telegram_destinations(user_id))
        index = next(
            (position for position, item in enumerate(destinations) if item.chat_id == chat_id),
            None,
        )
        if index is None:
            raise ValueError("Telegram-направление не найдено")
        target = index + direction
        if target < 0 or target >= len(destinations):
            return
        destinations[index], destinations[target] = destinations[target], destinations[index]
        with self.lock:
            self.connection.executemany(
                "UPDATE telegram_destinations SET sort_order = ? WHERE user_id = ? AND chat_id = ?",
                [
                    (position, user_id, item.chat_id)
                    for position, item in enumerate(destinations)
                ],
            )
            self.connection.commit()

    def add_monitored_tiktok_channel(self, channel: str, user_id: int = 1) -> None:
        with self.lock:
            self.connection.execute(
                "INSERT OR IGNORE INTO monitored_tiktok_channels(user_id, channel) VALUES (?, ?)",
                (user_id, channel),
            )
            self.connection.commit()

    def delete_monitored_tiktok_channel(self, channel: str, user_id: int = 1) -> None:
        with self.lock:
            self.connection.execute(
                "DELETE FROM monitored_tiktok_channels WHERE user_id = ? AND channel = ?",
                (user_id, channel),
            )
            self.connection.commit()

    def monitored_tiktok_channels(self, user_id: int = 1) -> tuple[str, ...]:
        with self.lock:
            rows = self.connection.execute(
                """
                SELECT channel FROM monitored_tiktok_channels
                WHERE user_id = ?
                ORDER BY created_at, channel
                """,
                (user_id,),
            ).fetchall()
        return tuple(row[0] for row in rows)

    def setting(self, key: str, default: str, user_id: int = 1) -> str:
        with self.lock:
            row = self.connection.execute(
                "SELECT value FROM app_settings WHERE user_id = ? AND key = ?",
                (user_id, key),
            ).fetchone()
        return row[0] if row else default

    def set_setting(
        self,
        key: str,
        value: str,
        *,
        only_if_missing: bool = False,
        user_id: int = 1,
    ) -> None:
        query = (
            "INSERT OR IGNORE INTO app_settings(user_id, key, value) VALUES (?, ?, ?)"
            if only_if_missing
            else """
            INSERT INTO app_settings(user_id, key, value) VALUES (?, ?, ?)
            ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value
            """
        )
        with self.lock:
            self.connection.execute(query, (user_id, key, value))
            self.connection.commit()
