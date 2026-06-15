from __future__ import annotations

import sqlite3
import threading
from pathlib import Path


class Storage:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_videos (
                video_id TEXT PRIMARY KEY,
                channel TEXT NOT NULL,
                processed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS initialized_channels (
                channel TEXT PRIMARY KEY,
                initialized_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self.connection.commit()

    def has(self, video_id: str) -> bool:
        with self.lock:
            row = self.connection.execute(
                "SELECT 1 FROM processed_videos WHERE video_id = ?", (video_id,)
            ).fetchone()
        return row is not None

    def mark(self, video_id: str, channel: str) -> None:
        with self.lock:
            self.connection.execute(
                "INSERT OR IGNORE INTO processed_videos(video_id, channel) VALUES (?, ?)",
                (video_id, channel),
            )
            self.connection.commit()

    def is_channel_initialized(self, channel: str) -> bool:
        with self.lock:
            row = self.connection.execute(
                "SELECT 1 FROM initialized_channels WHERE channel = ?", (channel,)
            ).fetchone()
        return row is not None

    def mark_channel_initialized(self, channel: str) -> None:
        with self.lock:
            self.connection.execute(
                "INSERT OR IGNORE INTO initialized_channels(channel) VALUES (?)", (channel,)
            )
            self.connection.commit()
