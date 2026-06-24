# MeTube

![Build Status](https://github.com/abdusco/metube/actions/workflows/main.yml/badge.svg)

MeTube is a self-hosted web UI for `yt-dlp`, for downloading media from YouTube and [dozens of other sites](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md).

Key capabilities:
* Download videos, audio from a browser UI.
* Download playlists and channels, with configurable output and download options.
* Fetch subtitles alongside video/audio downloads and save them as separate files.

## 🐳 Run using Docker

```bash
docker run -d -p 8081:8081 -v /path/to/downloads:/downloads ghcr.io/abdusco/metube
```

## 🐳 Run using docker-compose

```yaml
services:
  metube:
    image: ghcr.io/abdusco/metube
    container_name: metube
    restart: unless-stopped
    environment:
      - DELETE_FILE_ON_TRASHCAN=true
      - CLEAR_COMPLETED_AFTER=172800 # 2 days
    ports:
      - "8081:8081"
    volumes:
      - /path/to/downloads:/downloads
```

## ⚙️ Configuration via environment variables

Certain values can be set via environment variables, using the `-e` parameter on the docker command line, or the `environment:` section in docker-compose.

### ⬇️ Download Behavior

* __MAX_CONCURRENT_DOWNLOADS__: Maximum number of simultaneous downloads allowed. For example, if set to `5`, then at most five downloads will run concurrently, and any additional downloads will wait until one of the active downloads completes. Defaults to `3`. 
* __DELETE_FILE_ON_TRASHCAN__: if `true`, downloaded files are deleted on the server, when they are trashed from the "Completed" section of the UI. Defaults to `true`.
* __CLEAR_COMPLETED_AFTER__: Number of seconds after which completed (and failed) downloads are automatically removed from the "Completed" list when a new download is added. Defaults to `0` (disabled).

### 📁 Storage & Directories

* __DOWNLOAD_DIR__: Path to where the downloads will be saved. Defaults to `/downloads` in the Docker image, and `.` otherwise.
* __STATE_DIR__: Path to where MeTube will store its persistent state (`jobs.sqlite3`). Defaults to `/downloads/.metube` in the Docker image, and `.` otherwise.
* __TEMP_DIR__: Path where intermediary download files will be saved. Defaults to `/downloads` in the Docker image, and `.` otherwise.
  * Set this to an SSD or RAM filesystem (e.g., `tmpfs`) for better performance.
  * __Note__: Using a RAM filesystem may prevent downloads from being resumed.
* __CHOWN_DIRS__: If `false`, ownership of `DOWNLOAD_DIR`, `STATE_DIR`, and `TEMP_DIR` (and their contents) will not be set on container start. Ensure user under which MeTube runs has necessary access to these directories already. Defaults to `true`.

### 📝 File Naming & yt-dlp

* __OUTPUT_TEMPLATE__: The template for the filenames of the downloaded videos, formatted according to [this spec](https://github.com/yt-dlp/yt-dlp/blob/master/README.md#output-template). Defaults to `%(uploader)s -- @%(extractor)s -- %(title)s -- %(upload_date>%Y-%m-%d)s.%(ext)s`.
* __YTDL_OPTIONS__: Additional options to pass to yt-dlp, as a JSON object. See [Configuring yt-dlp options](#%EF%B8%8F-configuring-yt-dlp-options) for details, examples, and available options reference.
* __YTDL_NIGHTLY_UPDATE_TIME__: If set, will cause MeTube to use [nightly yt-dlp builds](https://github.com/yt-dlp/yt-dlp-nightly-builds) instead of the stable releases. Set to the time (`HH:MM`, 24-hour) when you want the daily upgrades and MeTube restart to happen. Defaults to empty (disabled).

### 🌐 Web Server & URLs

* __HOST__: The host address the web server will bind to. Defaults to `0.0.0.0` (all interfaces).
* __PORT__: The port number the web server will listen on. Defaults to `8081`.
* __PUBLIC_HOST_URL__: Base URL for the download links shown in the UI for completed files. By default, MeTube serves them under its own URL. If your download directory is accessible on another URL and you want the download links to be based there, use this variable to set it.
* __CORS_ALLOWED_ORIGINS__: Comma-separated list of origins permitted to make cross-origin requests to the MeTube API. When unset or empty, all cross-origin requests are denied. Set to `*` to allow all origins. This must be configured for [browser extensions](#-browser-extensions), [bookmarklets](#-bookmarklet), and any other browser-based tools that contact MeTube from a different origin. For browser extensions use `*` (see below); for bookmarklets you can list specific sites, e.g. `https://www.youtube.com,https://www.vimeo.com`.

### 🏠 Basic Setup

* __PUID__: User under which MeTube will run. Defaults to `1000` (legacy `UID` also supported).
* __PGID__: Group under which MeTube will run. Defaults to `1000` (legacy `GID` also supported).
* __UMASK__: Umask value used by MeTube. Defaults to `022`.
* __LOGLEVEL__: Log level, can be set to `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`, or `NONE`. Defaults to `INFO`.

## 🎛️ Configuring yt-dlp options

MeTube lets you customize how [yt-dlp](https://github.com/yt-dlp/yt-dlp) behaves with global options that apply to every download by default.

### Option format

yt-dlp options in MeTube are expressed as JSON objects. The keys are yt-dlp API option names, which roughly correspond to command-line flags with dashes replaced by underscores. For example, the command-line flag `--write-subs` becomes `"writesubtitles": true` in JSON.

> **Tip:** Some command-line flags don't have a direct single-key equivalent — for instance, `--embed-thumbnail` and `--recode-video` must be expressed via `"postprocessors"`. A full list of available API options can be found [in the yt-dlp source](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/YoutubeDL.py#L224), and [this conversion script](https://github.com/yt-dlp/yt-dlp/blob/master/devscripts/cli_to_api.py) can help translate command-line flags to their API equivalents.

### Global options

Global options form the baseline for every download. Pass a JSON object via `YTDL_OPTIONS`:

```yaml
environment:
  - 'YTDL_OPTIONS={"writesubtitles": true, "subtitleslangs": ["en", "de"], "updatetime": false, "writethumbnail": true}'
```

MeTube always forces its own flat-extract behaviour during the initial metadata fetch (`extract_flat`, `noplaylist`, etc.).

### Configuration cookbooks

The project's Wiki contains examples of useful configurations contributed by users of MeTube:
* [YTDL_OPTIONS Cookbook](https://github.com/alexta69/metube/wiki/YTDL_OPTIONS-Cookbook)
* [OUTPUT_TEMPLATE Cookbook](https://github.com/alexta69/metube/wiki/OUTPUT_TEMPLATE-Cookbook)

## 📝 Downloading subtitles

MeTube can fetch subtitles alongside video and audio downloads. Open **Advanced Options** before starting a download and enter comma-separated BCP-47 language codes in the **Subtitle languages** field (e.g. `en`, `de`, `zh-CN`). Leave the field empty to skip subtitle downloading.

When subtitles are found, both the manually-uploaded and auto-generated variants are requested for each language (e.g. `en` and `en-orig`). The downloaded `.vtt` or `.srt` files appear as separate download links under the completed item, next to the video file.

To embed subtitles directly into the video container instead of downloading them as sidecar files, use the `FFmpegEmbedSubtitle` postprocessor via `YTDL_OPTIONS` — see [Configuring yt-dlp options](#%EF%B8%8F-configuring-yt-dlp-options).

## 🍪 Using browser cookies

In case you need to use your browser's cookies with MeTube, for example to download restricted or private videos:

* Install in your browser an extension to extract cookies:
  * [Firefox](https://addons.mozilla.org/en-US/firefox/addon/export-cookies-txt/)
  * [Chrome](https://chrome.google.com/webstore/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)
* Extract the cookies you need with the extension and save/export them as `cookies.txt`.
* In MeTube, open **Advanced Options** and use the **Upload Cookies** button to upload the file.
* After upload, the cookie indicator should show as active.
* Use **Delete Cookies** in the same section to remove uploaded cookies.

## 🔒 Running behind a reverse proxy

MeTube can run behind a reverse proxy for HTTPS termination or authentication.

The [linuxserver/swag](https://docs.linuxserver.io/general/swag) image includes ready-made snippets for MeTube in [subfolder](https://github.com/linuxserver/reverse-proxy-confs/blob/master/metube.subfolder.conf.sample) and [subdomain](https://github.com/linuxserver/reverse-proxy-confs/blob/master/metube.subdomain.conf.sample) modes, plus Authelia for authentication.

### 🌐 NGINX

```nginx
location /metube/ {
        proxy_pass http://metube:8081;
        proxy_set_header Host $host;
}
```

### 🌐 Caddy

The following example Caddyfile gets a reverse proxy going behind [caddy](https://caddyserver.com).

```caddyfile
example.com {
  route /metube/* {
    uri strip_prefix metube
    reverse_proxy metube:8081
  }
}
```

## 🔄 Updating yt-dlp

MeTube is powered by [yt-dlp](https://github.com/yt-dlp/yt-dlp), which requires frequent updates as video sites change their layouts. A new MeTube Docker image is published automatically when a new yt-dlp stable release is available, so keep your container up to date — [watchtower](https://github.com/nicholas-fedor/watchtower) works well for this. To follow yt-dlp's nightly channel instead, set `YTDL_NIGHTLY_UPDATE_TIME`.

## 🔧 Troubleshooting and submitting issues

MeTube is only a UI for [yt-dlp](https://github.com/yt-dlp/yt-dlp). Issues with authentication, postprocessing, permissions, or `YTDL_OPTIONS` should be debugged with yt-dlp directly first — once working, import those options into MeTube. To test inside the container:

```bash
docker exec -ti metube sh
cd /downloads
```

## 🛠️ Building and running locally

Make sure you have Python 3.13 installed.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync
uv run python3 app/main.py
```

The UI is plain HTML/JS/CSS — no build step needed.

A Docker image can be built locally (it will build the UI too):

```bash
docker build -t metube .
```

Note that if you're running the server in VSCode, your downloads will go to your user's Downloads folder (this is configured via the environment in `.vscode/launch.json`).
