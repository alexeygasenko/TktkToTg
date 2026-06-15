import pytest

from app.config import Config, TelegramChannel
from app.service import (
    TikTokToTelegram,
    Video,
    YouTubeVideo,
    best_thumbnail_url,
    build_youtube_caption,
    has_youtube_auth_cookies,
    build_caption,
    normalize_channel,
    username_from_info,
    validate_tiktok_url,
    validate_youtube_url,
    is_tiktok_video_url,
)


def test_normalize_channel() -> None:
    assert normalize_channel("https://www.tiktok.com/@example/") == (
        "example",
        "https://www.tiktok.com/@example",
    )
    assert normalize_channel("@example") == ("example", "https://www.tiktok.com/@example")


def test_caption_escapes_html_and_uses_quote() -> None:
    caption = build_caption(
        Video("1", "example", "A < B & C", "https://example.test", 0)
    )
    assert '<a href="https://www.tiktok.com/@example">@example</a>' in caption
    assert "<blockquote>A &lt; B &amp; C</blockquote>" in caption
    assert len(caption) <= 1024


def test_caption_truncates_long_description() -> None:
    caption = build_caption(Video("1", "example", "x" * 2000, "https://example.test", 0))
    assert caption.endswith("…</blockquote>")
    assert len(caption) <= 1024


def test_caption_truncates_escaped_description() -> None:
    caption = build_caption(Video("1", "example", "&" * 2000, "https://example.test", 0))
    assert len(caption) <= 1024


def test_caption_supports_plain_text_before_and_after_quote() -> None:
    caption = build_caption(
        Video("1", "example", "original", "https://example.test", 0),
        "edited <quote>",
        "before & text",
        "after > text",
    )
    assert "before &amp; text" in caption
    assert "<blockquote>edited &lt;quote&gt;</blockquote>" in caption
    assert "after &gt; text" in caption
    assert caption.index("before") < caption.index("<blockquote>") < caption.index("after")


def test_caption_truncates_all_builder_sections() -> None:
    caption = build_caption(
        Video("1", "example", "original", "https://example.test", 0),
        "q" * 2000,
        "b" * 2000,
        "a" * 2000,
    )
    assert len(caption) <= 1024
    assert "<blockquote>" in caption


def test_youtube_caption_contains_link_and_custom_text() -> None:
    caption = build_youtube_caption(
        YouTubeVideo(
            "yt1",
            "Video <title>",
            "https://youtube.com/watch?v=yt1",
            "https://i.ytimg.com/yt1.jpg",
            60,
            "Channel & Co",
        ),
        "Before",
        "After",
    )
    assert "Before" in caption
    assert '<a href="https://youtube.com/watch?v=yt1">Video &lt;title&gt;</a>' in caption
    assert "Channel &amp; Co" in caption
    assert "After" in caption


def test_manual_url_must_be_tiktok() -> None:
    assert validate_tiktok_url("https://vm.tiktok.com/example") == "https://vm.tiktok.com/example"
    with pytest.raises(ValueError):
        validate_tiktok_url("https://example.com/video")


def test_username_prefers_canonical_url() -> None:
    info = {"uploader_id": "107955", "uploader": "TikTok"}
    assert username_from_info(info, "https://www.tiktok.com/@tiktok/video/123") == "tiktok"


def test_tiktok_video_and_channel_detection() -> None:
    assert is_tiktok_video_url("https://www.tiktok.com/@author/video/123")
    assert not is_tiktok_video_url("https://www.tiktok.com/@author")
    assert normalize_channel("https://www.tiktok.com/@author?lang=en")[0] == "author"


def test_manual_youtube_url_must_be_youtube() -> None:
    assert validate_youtube_url("https://youtu.be/abc") == "https://youtu.be/abc"
    with pytest.raises(ValueError):
        validate_youtube_url("https://example.com/video")


def test_best_thumbnail_uses_largest_resolution() -> None:
    info = {
        "thumbnail": "fallback",
        "thumbnails": [
            {"url": "small", "width": 320, "height": 180},
            {"url": "large", "width": 1280, "height": 720},
        ],
    }
    assert best_thumbnail_url(info) == "large"


def test_service_copies_cookies_to_writable_data_dir(tmp_path) -> None:
    source = tmp_path / "source-cookies.txt"
    source.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    data_dir = tmp_path / "data"
    config = Config(
        telegram_bot_token="token",
        telegram_chat_id="@channel",
        tiktok_channels=(),
        poll_interval_seconds=300,
        scan_limit=15,
        post_existing=False,
        data_dir=data_dir,
        cookies_file=source,
        youtube_cookies_file=source,
        youtube_po_token_provider_url=None,
        web_host="127.0.0.1",
        web_port=8080,
        web_username=None,
        web_password=None,
    )

    service = TikTokToTelegram(config)

    assert service.tiktok_cookies_file == data_dir / "tiktok-cookies.txt"
    assert service.youtube_cookies_file == data_dir / "youtube-cookies.txt"


def test_detects_youtube_auth_cookies(tmp_path) -> None:
    cookies = tmp_path / "cookies.txt"
    cookies.write_text(
        "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tvalue\n",
        encoding="utf-8",
    )
    assert has_youtube_auth_cookies(cookies)


def test_publish_youtube_sends_photo_to_selected_channel(tmp_path, monkeypatch) -> None:
    config = Config(
        telegram_bot_token="token",
        telegram_chat_id="@main",
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
        telegram_channels=(
            TelegramChannel("Main", "@main"),
            TelegramChannel("Second", "@second"),
        ),
    )
    captured = {}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"ok": True}

    def fake_post(url, data, timeout):
        captured.update(url=url, data=data, timeout=timeout)
        return Response()

    monkeypatch.setattr("app.service.requests.post", fake_post)
    service = TikTokToTelegram(config)
    service.storage.add_telegram_destination("Second", "@second", "second-token")
    video = YouTubeVideo(
        "yt1",
        "Title",
        "https://youtube.com/watch?v=yt1",
        "https://i.ytimg.com/yt1.jpg",
        60,
        "Channel",
    )

    service.publish_youtube(video, "Before", "After", "@second")

    assert captured["url"].endswith("/sendPhoto")
    assert "botsecond-token" in captured["url"]
    assert captured["data"]["chat_id"] == "@second"
    assert captured["data"]["photo"] == video.thumbnail_url
    assert video.url in captured["data"]["caption"]


def test_service_updates_uploaded_cookies_without_restart(tmp_path) -> None:
    config = Config(
        telegram_bot_token="token",
        telegram_chat_id="@main",
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
    service = TikTokToTelegram(config)

    path = service.update_cookies("youtube", b"# Netscape HTTP Cookie File\n")

    assert path == tmp_path / "youtube-cookies.txt"
    assert service.youtube_cookies_file == path
    assert path.read_bytes() == b"# Netscape HTTP Cookie File\n"


def test_service_accepts_private_telegram_channel_id(tmp_path, monkeypatch) -> None:
    config = Config(
        telegram_bot_token="token",
        telegram_chat_id="@main",
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

    class Response:
        def json(self):
            return {"ok": True}

    captured = {}

    def fake_get(url, params, timeout):
        captured.update(url=url, params=params, timeout=timeout)
        return Response()

    monkeypatch.setattr("app.service.requests.get", fake_get)
    service = TikTokToTelegram(config)

    service.add_telegram_destination("Private", "-1001234567890", "private-token")

    assert captured["params"]["chat_id"] == "-1001234567890"
    assert service.storage.telegram_destination("-1001234567890").name == "Private"


def test_service_discovers_private_channel_from_bot_updates(tmp_path, monkeypatch) -> None:
    config = Config(
        telegram_bot_token="token",
        telegram_chat_id="@main",
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

    class Response:
        def json(self):
            return {
                "ok": True,
                "result": [
                    {
                        "channel_post": {
                            "chat": {
                                "id": -1001234567890,
                                "title": "Private channel",
                                "type": "channel",
                            }
                        }
                    }
                ],
            }

    monkeypatch.setattr("app.service.requests.get", lambda *args, **kwargs: Response())
    service = TikTokToTelegram(config)

    found = service.discover_telegram_destinations("private-token")

    assert found == (TelegramChannel("Private channel", "-1001234567890"),)
    destination = service.storage.telegram_destination("-1001234567890")
    assert destination.name == "Private channel"
    assert destination.bot_token == "private-token"
