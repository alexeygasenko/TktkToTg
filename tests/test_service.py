import json

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
    media_author_from_info,
    normalize_channel,
    username_from_info,
    validate_tiktok_url,
    validate_youtube_url,
    is_tiktok_video_url,
    is_tiktok_photo_url,
    is_instagram_url,
    validate_instagram_url,
    sanitize_telegram_html,
    tiktok_image_post_urls_from_data,
)


def test_normalize_channel() -> None:
    assert normalize_channel("https://www.tiktok.com/@example/") == (
        "example",
        "https://www.tiktok.com/@example",
    )
    assert normalize_channel("@example") == ("example", "https://www.tiktok.com/@example")


def test_tiktok_image_post_urls_from_data() -> None:
    data = {
        "ItemModule": {
            "123": {
                "imagePost": {
                    "images": [
                        {"imageURL": {"urlList": ["https://p16-common-sign.tiktokcdn-us.com/tos/image-one.jpeg?x=1"]}},
                        {"imageURL": {"urlList": ["https://cdn.test/two.jpeg"]}},
                        {"imageURL": {"urlList": ["https://p19-common-sign.tiktokcdn-us.com/tos/image-one.jpeg?x=2"]}},
                    ]
                }
            }
        }
    }

    assert tiktok_image_post_urls_from_data(data) == [
        "https://p16-common-sign.tiktokcdn-us.com/tos/image-one.jpeg?x=1",
        "https://cdn.test/two.jpeg",
    ]


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


def test_caption_can_omit_author_and_description() -> None:
    caption = build_caption(
        Video(
            "1",
            "creator",
            "description",
            "https://instagram.com/reel/1",
            0,
            "instagram",
            "https://instagram.com/creator/",
        ),
        before_text="Only text",
        include_author=False,
        include_description=False,
    )

    assert caption == "Only text"


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


def test_sanitize_telegram_html_keeps_supported_formatting() -> None:
    caption = sanitize_telegram_html(
        '<p><strong>Bold</strong> <em>Italic</em> <u>Under</u> '
        '<s>Strike</s> <tg-spoiler>Spoiler</tg-spoiler> '
        '<a href="https://example.com">Link</a></p>'
        '<blockquote expandable>Quote</blockquote><script>bad()</script>'
    )

    assert "<b>Bold</b>" in caption
    assert "<i>Italic</i>" in caption
    assert "<u>Under</u>" in caption
    assert "<s>Strike</s>" in caption
    assert "<tg-spoiler>Spoiler</tg-spoiler>" in caption
    assert '<a href="https://example.com">Link</a>' in caption
    assert "<blockquote expandable>Quote</blockquote>" in caption
    assert "<script>" not in caption


def test_manual_url_must_be_tiktok() -> None:
    assert validate_tiktok_url("https://vm.tiktok.com/example") == "https://vm.tiktok.com/example"
    with pytest.raises(ValueError):
        validate_tiktok_url("https://example.com/video")


def test_username_prefers_canonical_url() -> None:
    info = {"uploader_id": "107955", "uploader": "TikTok"}
    assert username_from_info(info, "https://www.tiktok.com/@tiktok/video/123") == "tiktok"


def test_instagram_author_prefers_public_username_over_numeric_id() -> None:
    username, author_url = media_author_from_info(
        {
            "uploader_id": "1234567890",
            "uploader": "creator.name",
            "uploader_url": "https://www.instagram.com/creator.name/",
        },
        "https://www.instagram.com/reel/abc/",
        "instagram",
    )

    assert username == "creator.name"
    assert author_url == "https://www.instagram.com/creator.name/"


def test_tiktok_video_and_channel_detection() -> None:
    assert is_tiktok_video_url("https://www.tiktok.com/@author/video/123")
    assert is_tiktok_video_url("https://www.tiktok.com/@author/photo/123")
    assert is_tiktok_photo_url("https://www.tiktok.com/@author/photo/123")
    assert not is_tiktok_video_url("https://www.tiktok.com/@author")
    assert normalize_channel("https://www.tiktok.com/@author?lang=en")[0] == "author"


def test_manual_youtube_url_must_be_youtube() -> None:
    assert validate_youtube_url("https://youtu.be/abc") == "https://youtu.be/abc"
    with pytest.raises(ValueError):
        validate_youtube_url("https://example.com/video")


def test_manual_instagram_url_must_be_instagram() -> None:
    assert is_instagram_url("https://www.instagram.com/reel/abc/")
    assert validate_instagram_url("https://instagram.com/p/abc/").endswith("/p/abc/")
    assert not is_instagram_url("https://example.com/video")


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
        instagram_cookies_file=source,
        youtube_cookies_file=source,
        youtube_po_token_provider_url=None,
        web_host="127.0.0.1",
        web_port=8080,
        web_username=None,
        web_password=None,
    )

    service = TikTokToTelegram(config)

    assert service.tiktok_cookies_file == data_dir / "users" / "1" / "tiktok-cookies.txt"
    assert service.instagram_cookies_file == data_dir / "users" / "1" / "instagram-cookies.txt"
    assert service.youtube_cookies_file == data_dir / "users" / "1" / "youtube-cookies.txt"


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
        instagram_cookies_file=None,
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

    service.publish_youtube(video, "Before", "After", "@second", "<b>Custom</b>")

    assert captured["url"].endswith("/sendPhoto")
    assert "botsecond-token" in captured["url"]
    assert captured["data"]["chat_id"] == "@second"
    assert captured["data"]["photo"] == video.thumbnail_url
    assert captured["data"]["caption"] == "<b>Custom</b>"


def test_publish_tiktok_image_post_sends_media_group(tmp_path, monkeypatch) -> None:
    config = Config(
        telegram_bot_token="token",
        telegram_chat_id="@main",
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
            TelegramChannel("Main", "@main"),
            TelegramChannel("Second", "@second"),
        ),
    )
    first = tmp_path / "first.jpg"
    second = tmp_path / "second.jpg"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    captured = {}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"ok": True}

    def fake_post(url, data, files, timeout):
        captured.update(url=url, data=data, files=set(files), timeout=timeout)
        return Response()

    monkeypatch.setattr("app.service.requests.post", fake_post)
    service = TikTokToTelegram(config)
    service.storage.add_telegram_destination("Second", "@second", "second-token")
    video = Video(
        "tt1",
        "author",
        "Caption",
        "https://www.tiktok.com/@author/video/tt1",
        0,
        media_type="image",
    )

    service.publish(video, (first, second), chat_id="@second")

    assert captured["url"].endswith("/sendMediaGroup")
    assert captured["data"]["chat_id"] == "@second"
    media = json.loads(captured["data"]["media"])
    assert [item["type"] for item in media] == ["photo", "photo"]
    assert media[0]["caption"]
    assert media[0]["parse_mode"] == "HTML"
    assert captured["files"] == {"photo0", "photo1"}


def test_download_images_skips_duplicate_signed_urls(tmp_path, monkeypatch) -> None:
    config = Config(
        telegram_bot_token="token",
        telegram_chat_id="@main",
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
    )
    service = TikTokToTelegram(config)
    requested: list[str] = []

    class Response:
        headers = {"Content-Type": "image/jpeg"}
        content = b"image"

        def raise_for_status(self):
            pass

    class Session:
        def get(self, url, timeout):
            requested.append(url)
            return Response()

    monkeypatch.setattr(service, "_cookie_session", lambda cookies_file=None: Session())

    paths = service._download_images(
        [
            "https://p16-common-sign.tiktokcdn-us.com/tos/same.jpeg?x=1",
            "https://p19-common-sign.tiktokcdn-us.com/tos/same.jpeg?x=2",
            "https://p16-common-sign.tiktokcdn-us.com/tos/other.jpeg?x=3",
        ],
        "post",
        None,
    )

    assert len(paths) == 2
    assert requested == [
        "https://p16-common-sign.tiktokcdn-us.com/tos/same.jpeg?x=1",
        "https://p16-common-sign.tiktokcdn-us.com/tos/other.jpeg?x=3",
    ]


def test_prepare_tiktok_photo_url_without_ytdlp(tmp_path, monkeypatch) -> None:
    config = Config(
        telegram_bot_token="token",
        telegram_chat_id="@main",
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
    )
    image_path = tmp_path / "image.jpg"
    image_path.write_bytes(b"image")
    service = TikTokToTelegram(config)

    def fail_extract_info(*args, **kwargs):
        raise AssertionError("yt-dlp must not be used for /photo/ URLs")

    monkeypatch.setattr(service, "_extract_info", fail_extract_info)
    monkeypatch.setattr(
        service,
        "_tiktok_image_post_urls",
        lambda url, cookies_file: ["https://cdn.test/image.jpg"],
    )
    monkeypatch.setattr(
        service,
        "_download_images",
        lambda urls, output_id, cookies_file: (image_path,),
    )

    video, paths = service.prepare_url(
        "https://www.tiktok.com/@knopkot_tiktok/photo/7653583264212471048"
    )

    assert video.video_id == "7653583264212471048"
    assert video.username == "knopkot_tiktok"
    assert video.media_type == "image"
    assert paths == (image_path,)


def test_prepare_tiktok_photo_url_falls_back_to_tikwm(tmp_path, monkeypatch) -> None:
    config = Config(
        telegram_bot_token="token",
        telegram_chat_id="@main",
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
    )
    image_path = tmp_path / "image.jpg"
    image_path.write_bytes(b"image")
    service = TikTokToTelegram(config)

    monkeypatch.setattr(service, "_tiktok_image_post_urls", lambda url, cookies_file: [])
    monkeypatch.setattr(
        service,
        "_tikwm_image_post_info",
        lambda url: (
            {
                "id": "7653587728289877255",
                "description": "Forest",
                "uploader": "knopkot_tiktok",
                "webpage_url": url,
            },
            ["https://cdn.test/image.jpg"],
        ),
    )
    monkeypatch.setattr(
        service,
        "_download_images",
        lambda urls, output_id, cookies_file: (image_path,),
    )

    video, paths = service.prepare_url(
        "https://www.tiktok.com/@knopkot_tiktok/photo/7653587728289877255"
    )

    assert video.video_id == "7653587728289877255"
    assert video.username == "knopkot_tiktok"
    assert video.description == "Forest"
    assert video.media_type == "image"
    assert paths == (image_path,)


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
        instagram_cookies_file=None,
        youtube_cookies_file=None,
        youtube_po_token_provider_url=None,
        web_host="127.0.0.1",
        web_port=8080,
        web_username=None,
        web_password=None,
    )
    service = TikTokToTelegram(config)

    path = service.update_cookies("instagram", b"# Netscape HTTP Cookie File\n")

    assert path == tmp_path / "users" / "1" / "instagram-cookies.txt"
    assert service.instagram_cookies_file == path
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
        instagram_cookies_file=None,
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
        instagram_cookies_file=None,
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


def test_service_discovers_public_channel_by_username_without_duplicate(
    tmp_path, monkeypatch
) -> None:
    config = Config(
        telegram_bot_token="token",
        telegram_chat_id="@public",
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
                                "title": "Public channel",
                                "username": "public",
                                "type": "channel",
                            }
                        }
                    }
                ],
            }

    monkeypatch.setattr("app.service.requests.get", lambda *args, **kwargs: Response())
    service = TikTokToTelegram(config)
    service.storage.add_telegram_destination("Old duplicate", "-1001234567890", "token")

    found = service.discover_telegram_destinations("new-token")

    assert found == (TelegramChannel("Public channel", "@public"),)
    destinations = service.storage.telegram_destinations()
    assert [item.chat_id for item in destinations] == ["@public"]
    assert destinations[0].bot_token == "new-token"


def test_service_replaces_old_public_username_after_channel_tag_change(
    tmp_path, monkeypatch
) -> None:
    config = Config(
        telegram_bot_token="token",
        telegram_chat_id="@main",
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
    )

    class UpdatesResponse:
        def json(self):
            return {
                "ok": True,
                "result": [
                    {
                        "channel_post": {
                            "chat": {
                                "id": -1001234567890,
                                "title": "Public channel",
                                "username": "old_public",
                                "type": "channel",
                            }
                        }
                    }
                ],
            }

    class ChatResponse:
        def json(self):
            return {
                "ok": True,
                "result": {
                    "id": -1001234567890,
                    "title": "Public channel",
                    "username": "new_public",
                    "type": "channel",
                },
            }

    def fake_get(url, *args, **kwargs):
        return UpdatesResponse() if url.endswith("/getUpdates") else ChatResponse()

    monkeypatch.setattr("app.service.requests.get", fake_get)
    service = TikTokToTelegram(config)
    service.storage.add_telegram_destination("Public channel", "@old_public", "old-token")

    found = service.discover_telegram_destinations("new-token")

    assert found == (TelegramChannel("Public channel", "@new_public"),)
    destinations = service.storage.telegram_destinations()
    by_chat_id = {item.chat_id: item for item in destinations}
    assert "@old_public" not in by_chat_id
    assert by_chat_id["@new_public"].telegram_id == "-1001234567890"
