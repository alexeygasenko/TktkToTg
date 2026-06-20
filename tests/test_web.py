from io import BytesIO
from pathlib import Path

from app.config import Config, TelegramChannel
from app.service import TikTokToTelegram
from app.service import Video, YouTubeVideo
from app.web import create_app


class FakeStorage:
    def mark(self, video_id: str, username: str) -> None:
        self.marked = (video_id, username)


class FakeService:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.storage = FakeStorage()
        self.published = None
        self.imported = None

    def prepare_url(self, url: str):
        return Video("123", "author", "Исходный текст", url, 0), self.path

    def publish(
        self,
        video: Video,
        path: Path,
        quote_text: str,
        before_text: str,
        after_text: str,
        chat_id: str,
        include_author: bool = True,
        include_description: bool = True,
        caption_html: str = "",
    ) -> None:
        self.published = (
            video,
            path,
            quote_text,
            before_text,
            after_text,
            chat_id,
            include_author,
            include_description,
            caption_html,
        )

    def import_channel(self, url: str, post_existing: bool, chat_id: str):
        self.imported = (url, post_existing, chat_id)
        return 15, 4 if post_existing else 0

    def get_youtube_info(self, url: str) -> YouTubeVideo:
        return YouTubeVideo("yt1", "YouTube title", url, "https://i.ytimg.com/test.jpg", 60, "Channel")

    def publish_youtube(
        self,
        video: YouTubeVideo,
        before_text: str,
        after_text: str,
        chat_id: str,
        caption_html: str = "",
    ) -> None:
        self.published_youtube = (video, before_text, after_text, chat_id, caption_html)

    def add_telegram_destination(self, name: str, chat_id: str, bot_token: str) -> None:
        self.added_destination = (name, chat_id, bot_token)

    def discover_telegram_destinations(self, bot_token: str):
        self.discovered_token = bot_token
        return (TelegramChannel("Private", "-1001234567890"),)

    def delete_telegram_destination(self, chat_id: str) -> None:
        self.deleted_destination = chat_id

    def move_telegram_destination(self, chat_id: str, direction: str) -> None:
        self.moved_destination = (chat_id, direction)

    def add_monitored_tiktok_channel(self, channel: str) -> None:
        self.added_monitor = channel

    def delete_monitored_tiktok_channel(self, channel: str) -> None:
        self.deleted_monitor = channel

    def set_poll_interval_seconds(self, value: str) -> None:
        self.updated_interval = value

    def update_cookies(self, service_name: str, content: bytes) -> None:
        self.updated_cookies = (service_name, content)


def make_config(tmp_path: Path) -> Config:
    return Config(
        telegram_bot_token="token",
        telegram_chat_id="@channel",
        tiktok_channels=(),
        poll_interval_seconds=300,
        scan_limit=15,
        post_existing=False,
        data_dir=tmp_path,
        cookies_file=None,
        instagram_cookies_file=None,
        youtube_cookies_file=None,
        youtube_po_token_provider_url=None,
        web_host="127.0.0.1",
        web_port=8080,
        web_username=None,
        web_password=None,
        telegram_channels=(
            TelegramChannel("Main", "@channel"),
            TelegramChannel("Second", "@second"),
        ),
    )


def test_web_flow_uses_caption_builder_texts(tmp_path: Path) -> None:
    path = tmp_path / "video.mp4"
    path.write_bytes(b"video")
    service = FakeService(path)
    client = create_app(make_config(tmp_path), service).test_client()

    response = client.post(
        "/prepare", data={"tiktok_url": "https://www.tiktok.com/@author/video/123"}
    )
    assert response.status_code == 200
    assert "Исходный текст" in response.text
    assert "← Назад" in response.text
    assert "На главную" in response.text
    assert "data-return-carousel" in response.text
    assert "data-return-home" in response.text
    assert "data-caption-builder" in response.text
    assert "data-caption-editor" in response.text
    assert "caption_builder.js" in response.text
    assert "нажмите ПКМ" in response.text
    job_id = response.text.split("/send/")[1].split('"')[0]

    response = client.post(
        f"/send/{job_id}",
        data={
            "before_text": "До цитаты",
            "quote_text": "Новая цитата",
            "after_text": "После цитаты",
            "caption_html": '<b>Готовый</b> <tg-spoiler>пост</tg-spoiler>',
        },
    )
    assert response.status_code == 302
    assert service.published[2:8] == (
        "Новая цитата",
        "До цитаты",
        "После цитаты",
        "@channel",
        True,
        True,
    )
    assert service.published[8] == '<b>Готовый</b> <tg-spoiler>пост</tg-spoiler>'
    assert not path.exists()


def test_web_basic_auth(tmp_path: Path) -> None:
    base = make_config(tmp_path)
    config = Config(**{**base.__dict__, "web_username": "admin", "web_password": "secret"})
    client = create_app(config, FakeService(tmp_path / "video.mp4")).test_client()

    assert client.get("/").status_code == 401
    assert client.get("/", headers={"Authorization": "Basic YWRtaW46c2VjcmV0"}).status_code == 200


def test_cancel_removes_prepared_file(tmp_path: Path) -> None:
    path = tmp_path / "video.mp4"
    path.write_bytes(b"video")
    client = create_app(make_config(tmp_path), FakeService(path)).test_client()

    response = client.post(
        "/prepare", data={"tiktok_url": "https://www.tiktok.com/@author/video/123"}
    )
    job_id = response.text.split("/cancel/")[1].split('"')[0]
    assert client.post(f"/cancel/{job_id}").status_code == 302
    assert not path.exists()


def test_tiktok_channel_can_publish_existing(tmp_path: Path) -> None:
    service = FakeService(tmp_path / "video.mp4")
    client = create_app(make_config(tmp_path), service).test_client()

    response = client.post(
        "/tiktok/prepare",
        data={
            "tiktok_url": "https://www.tiktok.com/@author",
            "post_existing": "on",
            "chat_id": "@second",
        },
    )

    assert response.status_code == 200
    assert service.imported == ("https://www.tiktok.com/@author", True, "@second")
    assert "4" in response.text


def test_instagram_video_opens_shared_post_builder(tmp_path: Path) -> None:
    path = tmp_path / "video.mp4"
    path.write_bytes(b"video")
    client = create_app(make_config(tmp_path), FakeService(path)).test_client()

    response = client.post(
        "/prepare", data={"tiktok_url": "https://www.instagram.com/reel/abc/"}
    )

    assert response.status_code == 200
    assert "Конструктор Telegram-поста" in response.text
    assert "data-caption-editor" in response.text


def test_post_builder_can_disable_author_and_description(tmp_path: Path) -> None:
    path = tmp_path / "video.mp4"
    path.write_bytes(b"video")
    service = FakeService(path)
    client = create_app(make_config(tmp_path), service).test_client()
    response = client.post(
        "/prepare", data={"tiktok_url": "https://www.instagram.com/reel/abc/"}
    )
    job_id = response.text.split("/send/")[1].split('"')[0]

    response = client.post(
        f"/send/{job_id}",
        data={
            "caption_options_present": "1",
            "before_text": "Только текст",
            "quote_text": "Скрытое описание",
        },
    )

    assert response.status_code == 302
    assert service.published[6:8] == (False, False)


def test_youtube_info_returns_download_links(tmp_path: Path) -> None:
    client = create_app(make_config(tmp_path), FakeService(tmp_path / "video.mp4")).test_client()

    response = client.post(
        "/youtube/info", data={"youtube_url": "https://www.youtube.com/watch?v=abc"}
    )
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["title"] == "YouTube title"
    assert "/youtube/thumbnail/" in payload["thumbnail_download_url"]
    assert "/youtube/video/" in payload["video_download_url"]
    assert "/youtube/post/" in payload["post_url"]


def test_home_has_post_builder_transition(tmp_path: Path) -> None:
    client = create_app(make_config(tmp_path), FakeService(tmp_path / "video.mp4")).test_client()

    response = client.get("/")

    assert response.status_code == 200
    assert response.text.count("Подготовить пост") >= 2
    assert 'id="page-transition"' in response.text
    assert 'name="tiktok_url"' in response.text
    assert 'autocomplete="off"' in response.text
    assert "Second" in response.text
    assert "@second" in response.text
    assert 'data-source-tab="tiktok"' in response.text
    assert 'data-source-tab="instagram"' in response.text
    assert 'data-source-tab="youtube"' in response.text
    assert "Вставьте ссылку на видео" in response.text
    assert "async function loadPostBuilder" in response.text
    assert "async function runInlineScripts" in response.text
    assert 'fetch(targetUrl, { headers: { "X-Requested-With": "fetch" } })' in response.text
    assert 'data-channel-picker' in response.text
    assert 'data-account-trigger' in response.text
    assert "Telegram-каналы и чаты" not in response.text

    response = client.get("/settings")
    assert response.status_code == 200
    assert "Telegram-каналы и чаты" in response.text
    assert "Cookies" in response.text
    assert "Найти каналы и чаты" in response.text
    assert "Поиск по названию или тегу" in response.text
    assert "Найти канал" in response.text
    assert 'autocomplete="off" data-lpignore="true"' in response.text


def test_settings_can_add_telegram_channel_and_update_cookies(tmp_path: Path) -> None:
    service = FakeService(tmp_path / "video.mp4")
    client = create_app(make_config(tmp_path), service).test_client()

    response = client.post(
        "/settings/telegram",
        data={"name": "News", "chat_id": "@news", "bot_token": "123:secret"},
    )
    assert response.status_code == 302
    assert service.added_destination == ("News", "@news", "123:secret")

    response = client.post(
        "/settings/telegram/discover", data={"bot_token": "123:secret"}
    )
    assert response.status_code == 302
    assert service.discovered_token == "123:secret"

    response = client.post("/settings/telegram/delete", data={"chat_id": "@second"})
    assert response.status_code == 302
    assert service.deleted_destination == "@second"

    response = client.post(
        "/settings/cookies/youtube",
        data={"cookies_file": (BytesIO(b"# Netscape HTTP Cookie File\n"), "cookies.txt")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 302
    assert service.updated_cookies == (
        "youtube",
        b"# Netscape HTTP Cookie File\n",
    )


def test_settings_can_order_destinations_and_manage_monitoring(tmp_path: Path) -> None:
    service = FakeService(tmp_path / "video.mp4")
    client = create_app(make_config(tmp_path), service).test_client()

    assert client.post(
        "/settings/telegram/move", data={"chat_id": "@second", "direction": "up"}
    ).status_code == 302
    assert service.moved_destination == ("@second", "up")

    response = client.post(
        "/settings/telegram/move",
        data={"chat_id": "@second", "direction": "down"},
        headers={"X-Requested-With": "fetch"},
    )
    assert response.status_code == 200
    assert response.get_json()["channels"] == [
        {"chat_id": "@channel"},
        {"chat_id": "@second"},
    ]
    assert service.moved_destination == ("@second", "down")

    assert client.post(
        "/settings/tiktok/monitor", data={"channel": "@author"}
    ).status_code == 302
    assert service.added_monitor == "@author"

    assert client.post(
        "/settings/tiktok/interval", data={"poll_interval_seconds": "120"}
    ).status_code == 302
    assert service.updated_interval == "120"


def test_tiktok_and_instagram_media_info_returns_download_and_post_links(
    tmp_path: Path,
) -> None:
    path = tmp_path / "video.mp4"
    path.write_bytes(b"video")
    client = create_app(make_config(tmp_path), FakeService(path)).test_client()

    response = client.post(
        "/media/info",
        data={"media_url": "https://www.instagram.com/reel/abc/", "chat_id": "@second"},
    )
    payload = response.get_json()

    assert response.status_code == 200
    assert "/media/video/" in payload["video_download_url"]
    assert "/media/post/" in payload["post_url"]
    assert "chat_id=" in payload["post_url"]
    assert "second" in payload["post_url"]
    assert "/preview/" in payload["preview_url"]

    response = client.get(payload["post_url"])
    assert response.status_code == 200
    assert 'name="chat_id" value="@second"' in response.text
    assert "<span data-channel-label>Second</span>" in response.text


def test_youtube_post_can_be_prepared_and_sent(tmp_path: Path) -> None:
    service = FakeService(tmp_path / "video.mp4")
    client = create_app(make_config(tmp_path), service).test_client()
    info = client.post(
        "/youtube/info", data={"youtube_url": "https://www.youtube.com/watch?v=abc"}
    ).get_json()

    response = client.get(info["post_url"])
    assert response.status_code == 200
    assert "Подготовьте YouTube-пост" in response.text
    assert "data-caption-builder" in response.text
    job_id = info["post_url"].rsplit("/", 1)[-1]

    response = client.post(
        f"/youtube/send/{job_id}",
        data={
            "before_text": "До",
            "after_text": "После",
            "chat_id": "@second",
            "caption_html": '<a href="https://youtube.com/watch?v=abc">Ссылка</a>',
        },
    )
    assert response.status_code == 302
    assert service.published_youtube[1:4] == ("До", "После", "@second")
    assert service.published_youtube[4] == '<a href="https://youtube.com/watch?v=abc">Ссылка</a>'


def test_admin_password_setup_registration_and_service_permissions(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    service = TikTokToTelegram(config)
    client = create_app(config, service).test_client()

    response = client.get("/")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]
    response = client.get("/login")
    assert response.status_code == 302
    assert "/setup-admin" in response.headers["Location"]

    response = client.post(
        "/setup-admin",
        data={"password": "admin-password", "confirm_password": "admin-password"},
    )
    assert response.status_code == 302
    assert service.storage.get_user(1).username == "boyd"
    assert not service.storage.get_user(1).must_set_password

    client.post("/logout")
    response = client.post(
        "/register",
        data={
            "username": "alice",
            "password": "alice-password",
            "confirm_password": "alice-password",
        },
    )
    assert response.status_code == 302
    alice = service.storage.get_user_by_username("alice")
    assert alice is not None

    client.post("/logout")
    assert client.post(
        "/login", data={"username": "boyd", "password": "admin-password"}
    ).status_code == 302
    response = client.get("/admin/users")
    assert response.status_code == 200
    assert "alice" in response.text
    assert "На главную" in response.text
    assert "Отключить" in response.text

    response = client.post(f"/admin/users/{alice.id}/toggle-disabled")
    assert response.status_code == 302
    assert service.storage.get_user(alice.id).is_disabled

    response = client.post(f"/admin/users/{alice.id}/toggle-disabled")
    assert response.status_code == 302
    assert not service.storage.get_user(alice.id).is_disabled

    response = client.get("/")
    assert response.status_code == 200
    assert 'href="/admin/users"' in response.text
    assert 'href="/settings"' in response.text

    response = client.post(
        f"/admin/users/{alice.id}",
        data={
            "username": "alice",
            "allow_tiktok": "on",
            "allow_instagram": "on",
        },
    )
    assert response.status_code == 302
    assert not service.storage.get_user(alice.id).allow_youtube

    client.post("/logout")
    assert client.post(
        "/login", data={"username": "alice", "password": "alice-password"}
    ).status_code == 302
    response = client.get("/")
    assert response.status_code == 200
    assert 'data-source-tab="youtube"' not in response.text
    assert "YouTube cookies" not in response.text

    response = client.post(
        "/settings/cookies/youtube",
        data={"cookies_file": (BytesIO(b"# Netscape HTTP Cookie File\n"), "cookies.txt")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 400
    assert not (tmp_path / "users" / str(alice.id) / "youtube-cookies.txt").exists()
