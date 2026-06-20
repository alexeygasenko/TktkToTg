# ClipRelay

Docker service for relaying social video posts to Telegram.

- Publishes individual TikTok videos to Telegram after caption editing.
- Downloads Instagram videos and publishes them to Telegram after caption editing.
- Accepts TikTok channels and lets you publish or skip existing videos.
- Automatically monitors configured TikTok channels.
- Downloads YouTube videos and thumbnails in the best available quality.
- Publishes YouTube links with thumbnails and prepared Telegram captions.

## Setup

Initial settings are stored in the local `config.yaml`. On first startup, the
Telegram token and channel list are imported into `data/state.sqlite3` for the
administrator account `boyd`. The first time you open the web interface, set the
password for `boyd`. After that, users can sign in with a username and password
or create their own account from the registration page.

```bash
cp config.example.yaml config.yaml
```

Fill at least these values in `config.yaml`:

```yaml
telegram:
  bot_token: "123456789:bot_token"
  chat_id: "@my_channel"
  channels:
    - name: "Main channel"
      chat_id: "@my_channel"
    - name: "Test channel"
      chat_id: "@my_test_channel"
```

`telegram.chat_id` is used as the default destination for automatic monitoring.
`telegram.channels` defines the initial channel list. Add the bot as an
administrator to every channel, then start the service:

```bash
docker compose up -d --build
docker compose logs -f
```

Open `http://127.0.0.1:6767`. For TikTok, paste a video or channel link. For
Instagram, paste a video, reel, or post link. For YouTube, paste a video link:
the thumbnail preview appears automatically, followed by buttons for downloading
the video, downloading the thumbnail, and preparing the Telegram post.

Each user has separate Telegram destinations, TikTok monitoring settings, and
cookies. In the "Telegram settings and cookies" section, you can:

- add a public channel by `@handle` or a private channel by numeric ID such as
  `-1001234567890`, with a display name and bot token;
- automatically discover and add channels for a bot token after the bot is made
  an administrator and a new post is published in the channel;
- replace TikTok cookies;
- replace Instagram cookies;
- replace YouTube cookies.

Public Telegram channels are stored and displayed by `@handle`. Private
channels are stored by numeric ID, but only the display name is shown in the UI.
Channels can be searched and removed in settings. Search is also available when
choosing a destination for publishing.

The TikTok, Instagram, and YouTube post builder supports Telegram HTML captions,
including bold, italic, underline, strikethrough, spoiler, links, inline code,
code blocks, quotes, and expandable quotes. For TikTok and Instagram posts, the
author and description can also be disabled separately.

Tokens and cookies uploaded through the web interface are stored inside the
`data` directory, grouped by user, and excluded from Git.

`config.yaml` is excluded from Git. Do not share it because it contains the
Telegram bot token.

## Web Interface

The web interface uses built-in username/password accounts. The initial
administrator is `boyd`; set the password on the first visit. Admin users can
open the admin panel, view users with pagination, disable users, edit each
user's settings, and disable access to TikTok, Instagram, or YouTube. If a
service is disabled for a user, its upload/download UI is hidden and cookies for
that service cannot be uploaded.

By default, Docker exposes the interface only on `127.0.0.1:6767`.

## TikTok Cookies

Public TikTok videos usually do not require credentials. If TikTok requires
authorization, export browser cookies in Netscape format to `cookies.txt`,
uncomment the volume in `compose.yaml`, and configure:

```yaml
tiktok:
  cookies_file: tiktok-cookies.txt
```

The service does not need your TikTok login or password.

## Instagram Cookies

Instagram often restricts downloads without authorization. If you see
`Requested content is not available, rate-limit reached or login required`,
export browser cookies for Instagram in Netscape format and configure:

```yaml
instagram:
  cookies_file: instagram-cookies.txt
```

The file can also be updated from the web interface settings without manually
restarting the service.

## YouTube Cookies

YouTube can require a signed-in session to confirm that the request is not from
a bot. In that case, export browser cookies for YouTube to
`youtube-cookies.txt` and configure:

```yaml
youtube:
  cookies_file: youtube-cookies.txt
```

YouTube often rotates cookies for open tabs. For a stable export:

1. Open a separate incognito window and sign in to YouTube.
2. In that same single incognito tab, open `https://www.youtube.com/robots.txt`.
3. Export cookies for the `youtube.com` domain to `youtube-cookies.txt`.
4. Close the incognito window immediately and do not reuse that session.
5. Recreate the container with `docker compose up -d --force-recreate`.

## Automatic Monitoring

State, temporary downloads, and working cookie copies are stored in the local
`data` directory. The directory is mounted into the container as a bind mount
and excluded from Git, so videos are not published again after restart.
`tiktok.channels` can be left empty if automatic monitoring is not needed.

TikTok does not provide an accessible webhook for new videos, so the service
uses periodic polling. Telegram Bot API accepts bot-uploaded videos up to 50 MB.

YouTube videos are downloaded in the best available quality. If the best video
and audio tracks are separate, the service merges them with `ffmpeg`.

For videos where YouTube requires a PO Token, Compose automatically starts the
internal `bgutil-provider`. You do not need to obtain or refresh PO Tokens
manually.
