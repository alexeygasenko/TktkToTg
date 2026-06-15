from pathlib import Path

from app.config import Config
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
        self, video: Video, path: Path, quote_text: str, before_text: str, after_text: str
    ) -> None:
        self.published = (video, path, quote_text, before_text, after_text)

    def import_channel(self, url: str, post_existing: bool):
        self.imported = (url, post_existing)
        return 15, 4 if post_existing else 0

    def get_youtube_info(self, url: str) -> YouTubeVideo:
        return YouTubeVideo("yt1", "YouTube title", url, "https://i.ytimg.com/test.jpg", 60, "Channel")


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
        youtube_cookies_file=None,
        youtube_po_token_provider_url=None,
        web_host="127.0.0.1",
        web_port=8080,
        web_username=None,
        web_password=None,
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
    assert "← На главную" in response.text
    job_id = response.text.split("/send/")[1].split('"')[0]

    response = client.post(
        f"/send/{job_id}",
        data={
            "before_text": "До цитаты",
            "quote_text": "Новая цитата",
            "after_text": "После цитаты",
        },
    )
    assert response.status_code == 302
    assert service.published[2:] == ("Новая цитата", "До цитаты", "После цитаты")
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
        data={"tiktok_url": "https://www.tiktok.com/@author", "post_existing": "on"},
    )

    assert response.status_code == 200
    assert service.imported == ("https://www.tiktok.com/@author", True)
    assert "4" in response.text


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


def test_home_has_post_builder_transition(tmp_path: Path) -> None:
    client = create_app(make_config(tmp_path), FakeService(tmp_path / "video.mp4")).test_client()

    response = client.get("/")

    assert response.status_code == 200
    assert "Подготовить пост для Telegram" in response.text
    assert 'id="page-transition"' in response.text
    assert 'name="tiktok_url"' in response.text
    assert 'autocomplete="off"' in response.text
