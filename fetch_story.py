"""Fetch a Reddit post's title and body text via Reddit's official OAuth API.

Reddit blocks unauthenticated scraping (the old `.json` URL trick returns 403 now),
so this uses the free OAuth "script app" flow. To set it up:

  1. Go to https://www.reddit.com/prefs/apps
  2. Click "create app" -> choose type "script"
  3. Set redirect uri to http://localhost:8080 (unused but required)
  4. Copy the client ID (under the app name) and the client secret
  5. Set environment variables (or create a .env file in this folder):
       REDDIT_CLIENT_ID=xxxx
       REDDIT_CLIENT_SECRET=xxxx
       REDDIT_USER_AGENT=windows:reddit-video-maker:v0.1 (by /u/your_username)

If you'd rather not set this up, use `story_from_text()` to paste a story directly.
"""
import html
import os
import random
import re

import requests
from dotenv import load_dotenv

load_dotenv()

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
API_BASE = "https://oauth.reddit.com"


def _get_access_token() -> str:
    client_id = os.environ["REDDIT_CLIENT_ID"]
    client_secret = os.environ["REDDIT_CLIENT_SECRET"]
    user_agent = os.environ.get("REDDIT_USER_AGENT", "windows:reddit-video-maker:v0.1")

    resp = requests.post(
        TOKEN_URL,
        auth=(client_id, client_secret),
        data={"grant_type": "client_credentials"},
        headers={"User-Agent": user_agent},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _post_id_from_url(url: str) -> str:
    match = re.search(r"/comments/([a-z0-9]+)/", url)
    if not match:
        raise ValueError(f"Couldn't find a post id in URL: {url}")
    return match.group(1)


def fetch_story(url: str) -> dict:
    """Given a Reddit post URL, return {"title": ..., "body": ..., "subreddit": ...} via OAuth API."""
    token = _get_access_token()
    user_agent = os.environ.get("REDDIT_USER_AGENT", "windows:reddit-video-maker:v0.1")
    post_id = _post_id_from_url(url)

    resp = requests.get(
        f"{API_BASE}/api/info",
        params={"id": f"t3_{post_id}"},
        headers={"Authorization": f"Bearer {token}", "User-Agent": user_agent},
        timeout=15,
    )
    resp.raise_for_status()
    children = resp.json()["data"]["children"]
    if not children:
        raise ValueError(f"No post found for {url}")

    post = children[0]["data"]
    return {
        "title": post.get("title", "").strip(),
        "body": post.get("selftext", "").strip(),
        "subreddit": post.get("subreddit", ""),
    }


def fetch_top_posts(subreddit: str, limit: int = 5, time_filter: str = "month") -> list[dict]:
    """Return the top N posts from a subreddit as a quick way to find stories to narrate."""
    token = _get_access_token()
    user_agent = os.environ.get("REDDIT_USER_AGENT", "windows:reddit-video-maker:v0.1")

    resp = requests.get(
        f"{API_BASE}/r/{subreddit}/top",
        params={"limit": limit, "t": time_filter},
        headers={"Authorization": f"Bearer {token}", "User-Agent": user_agent},
        timeout=15,
    )
    resp.raise_for_status()
    posts = []
    for child in resp.json()["data"]["children"]:
        post = child["data"]
        posts.append(
            {
                "title": post.get("title", "").strip(),
                "body": post.get("selftext", "").strip(),
                "subreddit": post.get("subreddit", ""),
                "url": f"https://www.reddit.com{post.get('permalink', '')}",
            }
        )
    return posts


DEFAULT_TRENDING_SUBREDDITS = [
    "AmItheAsshole",
    "confession",
    "TrueOffMyChest",
    "relationship_advice",
    "tifu",
    "AskReddit",
]

# Popular storytelling subreddits this style of channel draws from most often -
# offered as quick-pick options in the GUI's "paste manually" dialog so users
# don't have to remember/retype exact subreddit names.
COMMON_STORY_SUBREDDITS = [
    "AmItheAsshole",
    "tifu",
    "relationship_advice",
    "confession",
    "TrueOffMyChest",
    "AskReddit",
    "EntitledParents",
    "MaliciousCompliance",
    "ProRevenge",
    "pettyrevenge",
    "nosleep",
    "TalesFromRetail",
    "UnethicalLifeProTips",
    "BestofRedditorUpdates",
]


_RSS_ENTRY_RE = re.compile(r"<entry>(.*?)</entry>", re.S)
_RSS_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S)
_RSS_CONTENT_RE = re.compile(r"<content[^>]*>(.*?)</content>", re.S)
_RSS_LINK_RE = re.compile(r'<link href="([^"]+)"')
_RSS_CATEGORY_RE = re.compile(r'<category term="([^"]+)"')
_RSS_TAG_RE = re.compile(r"<[^>]+>")
_RSS_FOOTER_RE = re.compile(r"\s*submitted by\s+/u/\S+\s*\[link\]\s*\[comments\]\s*$", re.I)


def _html_to_text(raw_html: str) -> str:
    text = html.unescape(raw_html)
    text = _RSS_TAG_RE.sub(" ", text)
    text = _RSS_FOOTER_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def _fetch_rss_posts(subreddit: str, limit: int) -> list[dict]:
    """Reddit's RSS feeds are public and need no OAuth app/credentials - just a
    User-Agent. They carry the post title, full body (as HTML) and permalink,
    which is everything build_script() needs."""
    user_agent = os.environ.get("REDDIT_USER_AGENT", "windows:reddit-video-maker:v0.1")
    resp = requests.get(
        f"https://www.reddit.com/r/{subreddit}/top/.rss",
        params={"t": "day", "limit": limit},
        headers={"User-Agent": user_agent},
        timeout=15,
    )
    resp.raise_for_status()

    posts = []
    for raw_entry in _RSS_ENTRY_RE.findall(resp.text):
        title_m = _RSS_TITLE_RE.search(raw_entry)
        content_m = _RSS_CONTENT_RE.search(raw_entry)
        link_m = _RSS_LINK_RE.search(raw_entry)
        category_m = _RSS_CATEGORY_RE.search(raw_entry)
        if not (title_m and content_m and link_m):
            continue
        posts.append(
            {
                "title": html.unescape(title_m.group(1)).strip(),
                "body": _html_to_text(content_m.group(1)),
                "subreddit": category_m.group(1) if category_m else subreddit,
                "url": link_m.group(1),
            }
        )
    return posts


def fetch_trending_post(subreddits: list[str] | None = None, min_chars: int = 200, limit: int = 8) -> dict:
    """Scan a handful of story-friendly subreddits' top-of-day posts and return a
    random one long enough to narrate - a quick way to grab whatever's currently
    popular without picking a URL by hand or needing Reddit API app credentials
    (this uses Reddit's public RSS feeds, not the OAuth API `fetch_story` needs).
    Raises if nothing suitable is found."""
    candidates = []
    for subreddit in subreddits or DEFAULT_TRENDING_SUBREDDITS:
        for post in _fetch_rss_posts(subreddit, limit):
            if len(post["body"]) >= min_chars:
                candidates.append(post)
                break  # take the top-ranked qualifying post per subreddit

    if not candidates:
        raise ValueError("No suitable trending text posts found - try again later or widen the subreddit list.")

    return random.choice(candidates)


def story_from_text(title: str, body: str = "", subreddit: str = "") -> dict:
    """Build a story dict by hand if you'd rather paste text than hit the Reddit API."""
    return {"title": title.strip(), "body": body.strip(), "subreddit": subreddit}


def build_script(story: dict, max_chars: int = 1800) -> str:
    """Combine title + body into a single narration script, trimmed to a safe length."""
    text = f"{story['title']}. {story['body']}".strip()
    text = re.sub(r"\s+", " ", text)

    if len(text) > max_chars:
        text = text[:max_chars]
        last_period = text.rfind(".")
        if last_period > max_chars * 0.6:
            text = text[: last_period + 1]

    return text


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python fetch_story.py <reddit_post_url>")
        raise SystemExit(1)

    story = fetch_story(sys.argv[1])
    script = build_script(story)
    print(f"Subreddit: r/{story['subreddit']}")
    print(f"Title: {story['title']}")
    print(f"\nScript ({len(script)} chars):\n{script}")
