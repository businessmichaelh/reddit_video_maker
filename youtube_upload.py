"""Upload finished videos to YouTube (as Shorts) via the official Data API v3.

One-time setup:
  1. Go to https://console.cloud.google.com/ and create a project (or reuse one).
  2. Enable the "YouTube Data API v3" for that project.
  3. Go to "APIs & Services" -> "Credentials" -> "Create credentials" -> "OAuth client ID".
     - Application type: "Desktop app"
     - Download the JSON and save it next to this file as `youtube_client_secret.json`.
  4. Run this module directly once (`python youtube_upload.py`) - it'll open a browser
     window for you to sign in and grant upload access. The resulting token is cached
     in `youtube_token.json` so you only have to do this once.

After that, `upload_video(...)` (or the GUI's "Upload to YouTube" button) just works.
"""
import os

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

_HERE = os.path.dirname(__file__)
CLIENT_SECRET_PATH = os.path.join(_HERE, "youtube_client_secret.json")
TOKEN_PATH = os.path.join(_HERE, "youtube_token.json")

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


class YouTubeNotConfigured(RuntimeError):
    """Raised when the OAuth client secret hasn't been set up yet (see module docstring)."""


def _get_credentials() -> Credentials:
    if not os.path.exists(CLIENT_SECRET_PATH):
        raise YouTubeNotConfigured(
            f"Missing {CLIENT_SECRET_PATH}.\n"
            "Download an OAuth 'Desktop app' client secret from Google Cloud Console "
            "(APIs & Services -> Credentials) and save it at that path - "
            "see the youtube_upload.py module docstring for step-by-step instructions."
        )

    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_PATH, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_PATH, "w", encoding="utf-8") as f:
            f.write(creds.to_json())

    return creds


def is_configured() -> bool:
    """True once the OAuth client secret has been downloaded and saved."""
    return os.path.exists(CLIENT_SECRET_PATH)


def upload_video(
    file_path: str,
    title: str,
    description: str = "",
    tags: list[str] | None = None,
    category_id: str = "24",  # "Entertainment"
    privacy_status: str = "public",
    thumbnail_path: str | None = None,
    on_status=None,
) -> str:
    """Upload `file_path` to the authenticated channel as a Short. Returns the video URL.

    `category_id` defaults to 24 (Entertainment), the usual home for faceless story content.
    `privacy_status` is one of "public", "unlisted", "private".
    `thumbnail_path`, if given, is set as the video's custom thumbnail after upload.
    Note: custom thumbnails require the channel to be phone-verified - if it isn't,
    YouTube rejects the request and we just log a warning rather than failing the upload.
    """
    def report(message):
        if on_status:
            on_status(message)

    report("Signing in to YouTube...")
    creds = _get_credentials()
    youtube = build("youtube", "v3", credentials=creds)

    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:5000],
            "tags": (tags or [])[:500],
            "categoryId": category_id,
        },
        "status": {
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(file_path, chunksize=-1, resumable=True, mimetype="video/mp4")

    report("Uploading video to YouTube...")
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            report(f"Uploading to YouTube... {int(status.progress() * 100)}%")

    video_id = response["id"]
    url = f"https://youtube.com/shorts/{video_id}"
    report(f"Uploaded: {url}")

    if thumbnail_path and os.path.exists(thumbnail_path):
        report("Setting custom thumbnail...")
        try:
            youtube.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(thumbnail_path, mimetype="image/png"),
            ).execute()
        except HttpError as exc:
            report(
                "Couldn't set a custom thumbnail (this usually means the channel "
                "isn't phone-verified yet - YouTube uses the auto-generated one instead): "
                f"{exc}"
            )

    return url


if __name__ == "__main__":
    import sys

    if len(sys.argv) >= 2 and sys.argv[1] == "--auth":
        _get_credentials()
        print("Signed in - youtube_token.json saved. You're ready to upload.")
        raise SystemExit(0)

    if len(sys.argv) < 3:
        print("Usage: python youtube_upload.py <video_path> <title> [description]")
        print("\nFirst run with no arguments to just complete the OAuth sign-in:")
        print("    python youtube_upload.py --auth")
        raise SystemExit(1)

    video_path, title = sys.argv[1], sys.argv[2]
    description = sys.argv[3] if len(sys.argv) > 3 else ""
    print(upload_video(video_path, title, description, on_status=print))
