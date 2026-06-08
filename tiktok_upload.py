"""Upload finished videos to TikTok via the official Content Posting API (v2).

One-time setup:
  1. Go to https://developers.tiktok.com/ and sign up for a developer account,
     then create an app.
  2. Add the "Content Posting API" product to the app and request the
     `video.publish` and `video.upload` scopes.
  3. Under the app's "Login Kit" settings, add a redirect URI of
     `http://localhost:8943/callback` (TikTok requires the redirect domain to
     be verified for apps that have gone through review, but `localhost` is
     accepted for apps still in sandbox/development mode - which is all a
     single personal channel needs).
  4. Copy the app's "Client key" and "Client secret" into a `.env` file (or set
     them as environment variables) next to this script:
       TIKTOK_CLIENT_KEY=xxxx
       TIKTOK_CLIENT_SECRET=xxxx
  5. Run this module directly once (`python tiktok_upload.py --auth`) - it
     opens a browser window for you to sign in and grant posting access. The
     resulting token is cached in `tiktok_token.json` (and silently refreshed
     after that), so you only have to do this once.

After that, `upload_video(...)` (or the GUI's "Upload to TikTok" button) just works.

IMPORTANT - the "unaudited app" limitation:
  Brand-new TikTok apps are "unaudited". Until yours passes TikTok's app
  review (which requires a working demo and can take days), the Content
  Posting API will only let it either (a) post videos as private/"only me", or
  (b) send the video to the user's TikTok inbox as a draft for them to review
  and publish by hand from their phone. There's no way around this - it's a
  platform-side restriction on every unaudited app, not a limitation of this
  script. `upload_video()` therefore defaults to the inbox/draft flow
  (`direct_post=False`), which always succeeds regardless of audit status.
  Once your app is audited and approved for public posting, pass
  `direct_post=True` (and set `privacy_level="PUBLIC_TO_EVERYONE"`) to publish
  straight to the profile feed instead.
"""
import json
import os
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from dotenv import load_dotenv

load_dotenv()

_HERE = os.path.dirname(__file__)
TOKEN_PATH = os.path.join(_HERE, "tiktok_token.json")

AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
PUBLISH_INIT_URL = "https://open.tiktokapis.com/v2/post/publish/video/init/"
INBOX_INIT_URL = "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/"

SCOPES = "video.publish,video.upload"
REDIRECT_PORT = 8943
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"

# TikTok requires upload chunks between 5MB and 64MB; these short vertical
# clips are small enough that a single ~10MB chunk always covers the whole file.
CHUNK_SIZE = 10 * 1024 * 1024


class TikTokNotConfigured(RuntimeError):
    """Raised when the developer app's client key/secret haven't been set up yet (see module docstring)."""


def _client_credentials() -> tuple[str, str]:
    key = os.environ.get("TIKTOK_CLIENT_KEY")
    secret = os.environ.get("TIKTOK_CLIENT_SECRET")
    if not key or not secret:
        raise TikTokNotConfigured(
            "Missing TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET.\n"
            "Create a TikTok developer app and add the Content Posting API to it "
            "(see the tiktok_upload.py module docstring for step-by-step "
            "instructions), then save the client key and secret in a .env file "
            "next to this script. Then try again - you'll get a one-time "
            "browser sign-in prompt."
        )
    return key, secret


def is_configured() -> bool:
    """True once the developer app's client key/secret have been saved."""
    return bool(os.environ.get("TIKTOK_CLIENT_KEY")) and bool(os.environ.get("TIKTOK_CLIENT_SECRET"))


class _CallbackHandler(BaseHTTPRequestHandler):
    """Tiny one-shot local server that captures the `?code=...` TikTok redirects
    back to after the user signs in - the desktop-app equivalent of Google's
    `InstalledAppFlow.run_local_server()`."""

    def do_GET(self):
        params = parse_qs(urlparse(self.path).query)
        self.server.auth_code = params.get("code", [None])[0]
        self.server.auth_error = (params.get("error_description") or params.get("error") or [None])[0]

        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        message = (
            "Signed in! You can close this tab and go back to the app."
            if self.server.auth_code
            else f"Sign-in failed: {self.server.auth_error}"
        )
        self.wfile.write(f"<html><body><h2>{message}</h2></body></html>".encode("utf-8"))

    def log_message(self, *args):
        pass  # silence BaseHTTPRequestHandler's default per-request console spam


def _run_oauth_flow(key: str, secret: str) -> dict:
    server = HTTPServer(("localhost", REDIRECT_PORT), _CallbackHandler)
    server.auth_code = None
    server.auth_error = None

    query = urlencode({
        "client_key": key,
        "scope": SCOPES,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "state": "reddit_video_maker",
    })
    webbrowser.open(f"{AUTH_URL}?{query}")

    try:
        while server.auth_code is None and server.auth_error is None:
            server.handle_request()
    finally:
        server.server_close()

    if server.auth_error:
        raise RuntimeError(f"TikTok sign-in failed: {server.auth_error}")

    resp = requests.post(
        TOKEN_URL,
        data={
            "client_key": key,
            "client_secret": secret,
            "code": server.auth_code,
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    resp.raise_for_status()
    token = resp.json()
    if "access_token" not in token:
        raise RuntimeError(f"TikTok sign-in failed: {token}")
    return token


def _refresh_token(key: str, secret: str, refresh_token: str) -> dict:
    resp = requests.post(
        TOKEN_URL,
        data={
            "client_key": key,
            "client_secret": secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _get_access_token(on_status=None) -> str:
    def report(message):
        if on_status:
            on_status(message)

    key, secret = _client_credentials()

    token = None
    if os.path.exists(TOKEN_PATH):
        with open(TOKEN_PATH, "r", encoding="utf-8") as f:
            token = json.load(f)

    if token and token.get("refresh_token"):
        try:
            report("Refreshing TikTok sign-in...")
            token = _refresh_token(key, secret, token["refresh_token"])
        except requests.HTTPError:
            token = None  # refresh token expired/revoked - fall through to a fresh sign-in

    if not token or "access_token" not in token:
        report("Opening a browser window to sign in to TikTok...")
        token = _run_oauth_flow(key, secret)

    with open(TOKEN_PATH, "w", encoding="utf-8") as f:
        json.dump(token, f)

    return token["access_token"]


def upload_video(
    file_path: str,
    title: str,
    description: str = "",
    privacy_level: str = "SELF_ONLY",
    direct_post: bool = False,
    on_status=None,
) -> str:
    """Upload `file_path` to TikTok via the Content Posting API.

    `privacy_level` is one of "PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS",
    "FOLLOWER_OF_CREATOR", "SELF_ONLY" - unaudited apps may only use SELF_ONLY
    for `direct_post`.

    `direct_post`: if True, publishes straight to the signed-in user's profile
    (requires an *audited* app approved for public posting - see the module
    docstring's "unaudited app" note). If False (the default), the video is
    sent to the user's TikTok inbox as a draft they tap to review and post
    from their phone - this always works, even for brand-new unaudited apps.

    Returns a short human-readable status string - unlike YouTube, TikTok
    doesn't hand back a public video URL at upload time; the user finishes the
    post (and gets the link) from the TikTok app.
    """
    def report(message):
        if on_status:
            on_status(message)

    report("Signing in to TikTok...")
    access_token = _get_access_token(on_status=on_status)

    video_size = os.path.getsize(file_path)
    chunk_size = min(CHUNK_SIZE, video_size)
    total_chunks = max((video_size + chunk_size - 1) // chunk_size, 1)

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json; charset=UTF-8",
    }
    source_info = {
        "source": "FILE_UPLOAD",
        "video_size": video_size,
        "chunk_size": chunk_size,
        "total_chunk_count": total_chunks,
    }

    if direct_post:
        init_url = PUBLISH_INIT_URL
        body = {
            "post_info": {
                "title": title[:2200],
                "description": description[:2200],
                "privacy_level": privacy_level,
                "disable_duet": False,
                "disable_comment": False,
                "disable_stitch": False,
            },
            "source_info": source_info,
        }
    else:
        init_url = INBOX_INIT_URL
        body = {"source_info": source_info}

    report("Starting TikTok upload...")
    resp = requests.post(init_url, headers=headers, json=body, timeout=30)
    resp.raise_for_status()
    payload = resp.json()
    error = payload.get("error") or {}
    if error.get("code") not in (None, "ok"):
        raise RuntimeError(f"TikTok upload couldn't start: {error.get('code')} - {error.get('message')}")

    data = payload["data"]
    publish_id = data["publish_id"]
    upload_url = data["upload_url"]

    report("Uploading video to TikTok...")
    with open(file_path, "rb") as f:
        for chunk_index in range(total_chunks):
            start = chunk_index * chunk_size
            this_chunk_size = min(chunk_size, video_size - start)
            chunk_bytes = f.read(this_chunk_size)
            end = start + this_chunk_size - 1

            put_resp = requests.put(
                upload_url,
                data=chunk_bytes,
                headers={
                    "Content-Range": f"bytes {start}-{end}/{video_size}",
                    "Content-Type": "video/mp4",
                },
                timeout=120,
            )
            put_resp.raise_for_status()
            report(f"Uploading to TikTok... {int((chunk_index + 1) / total_chunks * 100)}%")

    if direct_post:
        report("Posted to TikTok - it'll appear on your profile once processing finishes.")
        return f"Posted directly to your TikTok profile (publish id: {publish_id})"
    else:
        report("Sent to your TikTok inbox - open the TikTok app to review and finish posting.")
        return f"Sent to your TikTok inbox for review (publish id: {publish_id})"


if __name__ == "__main__":
    import sys

    if len(sys.argv) >= 2 and sys.argv[1] == "--auth":
        _get_access_token(on_status=print)
        print("Signed in - tiktok_token.json saved. You're ready to upload.")
        raise SystemExit(0)

    if len(sys.argv) < 3:
        print("Usage: python tiktok_upload.py <video_path> <title> [description]")
        print("\nFirst run with no arguments to just complete the OAuth sign-in:")
        print("    python tiktok_upload.py --auth")
        raise SystemExit(1)

    video_path, title = sys.argv[1], sys.argv[2]
    description = sys.argv[3] if len(sys.argv) > 3 else ""
    print(upload_video(video_path, title, description, on_status=print))
