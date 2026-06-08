# Reddit Story Video Maker

Turns a Reddit post into a finished, captioned, narrated 9:16 MP4 ready to upload to TikTok/Shorts/Reels.

## Pipeline
Reddit post -> script text -> AI voiceover (edge-tts) -> word-timed captions (Whisper) -> video assembly (ffmpeg/moviepy) -> MP4

## One-time setup

1. **ffmpeg** is already installed via winget. If `ffmpeg` isn't found in a new terminal, restart your terminal so the updated PATH takes effect (it was added by the installer).

2. **Reddit API access** (free): Reddit blocks unauthenticated scraping, so you need a free "script" app:
   - Go to https://www.reddit.com/prefs/apps -> "create app" -> type "script"
   - Redirect URI can be anything, e.g. `http://localhost:8080`
   - Create a `.env` file in this folder with:
     ```
     REDDIT_CLIENT_ID=your_client_id
     REDDIT_CLIENT_SECRET=your_client_secret
     REDDIT_USER_AGENT=windows:reddit-video-maker:v0.1 (by /u/your_username)
     ```
   - **Or skip this entirely**: paste a story manually using `story_from_text()` in `fetch_story.py` instead of `fetch_story(url)`.

3. **Background clips**: drop looping background videos (gameplay, satisfying clips, etc.) into the `backgrounds/` folder. Royalty-free sources: YouTube videos under Creative Commons, or record your own gameplay.

## Usage

### GUI (recommended)

```
python gui.py
```

Side-by-side layout:
- **Left**: paste a Reddit post URL and press Enter (or click **Preview**) to fetch and
  read the story/script before committing to a render — edit the script text if you
  want to trim or tweak it, then pick a voice and background clip
- **Right**: an embedded video player that automatically loads and plays your finished
  render so you can preview it before posting (with Play/Pause/Stop, or "Open in
  default player" to watch it full-screen in Movies & TV / VLC / etc.)

Check **"Open output folder when done"** if you want the output folder to pop open
automatically once the render finishes.

#### Performance notes
- Captioning runs on your GPU (CUDA) when available — roughly 4x faster than CPU
- Video rendering uses `libx264` with a fast preset; the bottleneck is Python-side
  caption compositing rather than encoding, so GPU video encoding (NVENC) doesn't
  help here and was intentionally left out
- A typical 60-90 second video takes roughly 3-5 minutes end-to-end on this machine

### Command line

```
python main.py <reddit_post_url> <path_to_background_clip.mp4> [--name my_video] [--voice male_us]
```

Voice presets: `male_us`, `female_us`, `male_uk`, `female_uk` (or pass any edge-tts voice ID directly, run `edge-tts --list-voices` to see all).

Output lands in `output/<name>.mp3` (voiceover) and `output/<name>.mp4` (final video).

## Using without Reddit API setup

Edit `main.py` (or write a small script) to build the story dict yourself:

```python
from fetch_story import story_from_text, build_script
from main import run

story = story_from_text(
    title="AITA for telling my roommate her cooking smells?",
    body="She's made the same fish curry every night for two weeks...",
)
# then feed build_script(story) into the same tts -> captions -> assemble steps
```

## Notes
- First run downloads the Whisper model (~140MB) — happens once, then it's cached.
- Render time is roughly 1-2x the video's length on CPU.
- Tweak caption look (font size, colors, words-per-chunk) in `assemble.py` / `captions.py`.
