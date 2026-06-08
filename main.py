"""End-to-end pipeline: Reddit URL -> narrated, captioned, ready-to-post MP4.

Usage:
    python main.py <reddit_post_url> <background_clip_path> [output_name] [--voice male_us]
"""
import argparse
import os
import traceback

from assemble import assemble_video
from captions import generate_captions, group_into_chunks
from fetch_story import build_script, fetch_story
from tts import generate_voiceover

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output")

# Once a narration runs longer than this, the story gets auto-split into multiple
# ~60s videos (each ending at a sentence boundary, with a "FOLLOW FOR PART N"
# call-to-action) instead of one long render - keeps every part comfortably
# inside YouTube/TikTok's "Shorts" sweet spot. Only kicks in when the user hasn't
# manually picked a Part # (that workflow is for hand-built continuations).
AUTO_SPLIT_THRESHOLD_SECONDS = 60.0

# Nobody wants a 12-second "part 3" - if cutting at the next sentence boundary
# would leave less than this much narration for the final part, fold it back
# into the previous part instead of spinning up a barely-there extra video.
MIN_TRAILING_PART_SECONDS = 25.0

_SENTENCE_END_CHARS = (".", "!", "?")


def _split_into_segments(words: list[dict], target_seconds: float = AUTO_SPLIT_THRESHOLD_SECONDS) -> list[tuple[int, int]]:
    """Split word-level timestamps into runs that each end at the first sentence
    boundary at or after `target_seconds` of narration ("let it finish what it's
    saying" rather than cutting mid-sentence). Returns a list of (start, end)
    index ranges (end exclusive) into `words`; the final run absorbs whatever's left."""
    if not words:
        return []

    segments = []
    seg_start = 0
    seg_start_time = words[0]["start"]
    for i, w in enumerate(words):
        if w["word"].endswith(_SENTENCE_END_CHARS) and (w["end"] - seg_start_time) >= target_seconds:
            segments.append((seg_start, i + 1))
            seg_start = i + 1
            if seg_start < len(words):
                seg_start_time = words[seg_start]["start"]
    if seg_start < len(words):
        segments.append((seg_start, len(words)))
    return segments


def _merge_short_trailing_segment(words: list[dict], segments: list[tuple[int, int]],
                                   min_seconds: float = MIN_TRAILING_PART_SECONDS) -> list[tuple[int, int]]:
    """If the final segment would render to less than `min_seconds` of video, fold
    it into the previous segment instead. Sentence-boundary splitting guarantees
    every segment but the last is already >= AUTO_SPLIT_THRESHOLD_SECONDS, so only
    the trailing one can ever come up short - a single merge is always enough."""
    if len(segments) < 2:
        return segments

    last_start, last_end = segments[-1]
    last_duration = words[last_end - 1]["end"] - words[last_start]["start"]
    if last_duration < min_seconds:
        return segments[:-2] + [(segments[-2][0], segments[-1][1])]
    return segments


def _shift_words(words: list[dict], offset: float) -> list[dict]:
    return [{"word": w["word"], "start": w["start"] - offset, "end": w["end"] - offset} for w in words]


def _render_in_parts(audio_path, bg_path, video_path, srt_path, continue_from,
                      title, title_end, body_words, segments, queue):
    """Render each segment of an auto-split long story as its own MP4, continuing
    the same backdrop across parts and capping every part but the last with a
    "FOLLOW FOR PART N" call-to-action. Returns (video_paths, final_segment_end)."""
    def report(message):
        queue.put(("status", message))

    base, ext = os.path.splitext(video_path)
    srt_base, srt_ext = os.path.splitext(srt_path)

    video_paths = []
    segment_end = continue_from
    for index, (start_idx, end_idx) in enumerate(segments):
        part_number = index + 1
        is_first = index == 0
        is_last = index == len(segments) - 1

        seg_words = body_words[start_idx:end_idx]
        # Part 1's audio starts at zero so the narrated title is included (it's
        # shown via the Reddit card); later parts trim the narration down to just
        # their own span and shift caption timestamps to start from zero.
        audio_start = 0.0 if is_first else seg_words[0]["start"]
        audio_end = seg_words[-1]["end"]
        chunk_words = seg_words if is_first else _shift_words(seg_words, audio_start)
        chunks = group_into_chunks(chunk_words, words_per_chunk=3)

        seg_video_path = f"{base}_part{part_number}{ext}"
        seg_srt_path = f"{srt_base}_part{part_number}{srt_ext}"
        write_srt(chunks, seg_srt_path)

        report(f"Assembling part {part_number} of {len(segments)}...")
        _, segment_end = assemble_video(
            audio_path, bg_path, chunks, seg_video_path,
            start_time=(continue_from if is_first else segment_end),
            title=(title if is_first else None),
            title_end=(title_end if is_first else 0.0),
            audio_start=audio_start, audio_end=audio_end,
            next_part=(None if is_last else part_number + 1),
        )
        video_paths.append(seg_video_path)

    return video_paths, segment_end


def _srt_timestamp(seconds: float) -> str:
    millis = max(int(round(seconds * 1000)), 0)
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def write_srt(chunks: list[dict], out_path: str):
    """Export caption chunks as a standard .srt subtitle file alongside the video."""
    with open(out_path, "w", encoding="utf-8") as f:
        for i, chunk in enumerate(chunks, start=1):
            f.write(f"{i}\n")
            f.write(f"{_srt_timestamp(chunk['start'])} --> {_srt_timestamp(chunk['end'])}\n")
            f.write(f"{chunk['text']}\n\n")


def pipeline_worker(script, bg_path, audio_path, video_path, srt_path, voice, continue_from, queue, title=None, part=None):
    """Runs voiceover -> captions -> assembly to completion, reporting progress through
    `queue`. Meant to run in its own process (see gui.py) so the GUI can kill it outright
    with a Stop button instead of hanging forever if any stage of the pipeline stalls."""
    def report(message):
        queue.put(("status", message))

    try:
        report("Generating voiceover...")
        generate_voiceover(script, audio_path, voice=voice)

        words = generate_captions(audio_path, on_status=report)

        # Split the transcript at the title/body boundary so the title's words never
        # get word-by-word captioned underneath the Reddit card - they're shown there
        # instead. `title_end` marks exactly where the title's narration finishes.
        #
        # Whisper doesn't always tokenize the narration the same way the original
        # title text reads (it can merge "AITA for" into one token, split "Mom's"
        # into two, drop/add punctuation, etc.), so counting `len(title.split())`
        # words in is only an *estimate* of where the title ends - it can land a
        # word early and let a trailing title word slip into the first caption
        # (showing up smeared across the card, like "PASSED. BACKGROUND, MY").
        # A flat ~1s gap after the estimated boundary, plus dropping any word that
        # starts before that gap closes, makes that overlap impossible: captions
        # can only ever start with clean body content.
        TITLE_CAPTION_GAP_SECONDS = 1.0
        title_end = 0.0
        body_words = words
        if title:
            title_word_count = len(title.split())
            if title_word_count and len(words) > title_word_count:
                title_end = words[title_word_count - 1]["end"]
                caption_floor = title_end + TITLE_CAPTION_GAP_SECONDS
                body_words = [w for w in words[title_word_count:] if w["start"] >= caption_floor]

        # Long stories get auto-split into ~60s parts at sentence boundaries (only
        # when the user hasn't manually picked a Part # - that's for hand-built
        # continuations and shouldn't fight with the automatic version).
        segments = [] if part is not None else _split_into_segments(body_words)
        if len(segments) > 1:
            segments = _merge_short_trailing_segment(body_words, segments)

        if len(segments) > 1:
            video_paths, segment_end = _render_in_parts(
                audio_path, bg_path, video_path, srt_path, continue_from,
                title, title_end, body_words, segments, queue,
            )
        else:
            chunks = group_into_chunks(body_words, words_per_chunk=3)
            write_srt(chunks, srt_path)

            report("Assembling final video...")
            _, segment_end = assemble_video(
                audio_path, bg_path, chunks, video_path,
                start_time=continue_from, title=title, title_end=title_end, part=part,
            )
            video_paths = [video_path]

        queue.put(("done", {"video_paths": video_paths, "segment_end": segment_end}))
    except Exception:
        queue.put(("error", traceback.format_exc()))


def run(reddit_url: str, background_path: str, output_name: str = None, voice: str = "male_us"):
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print("[1/4] Fetching Reddit story...")
    story = fetch_story(reddit_url)
    script = build_script(story)
    print(f"    r/{story['subreddit']} - {story['title']!r} ({len(script)} chars)")

    name = output_name or _slugify(story["title"])
    audio_path = os.path.join(OUTPUT_DIR, f"{name}.mp3")
    video_path = os.path.join(OUTPUT_DIR, f"{name}.mp4")

    print("[2/4] Generating voiceover...")
    generate_voiceover(script, audio_path, voice=voice)

    print("[3/4] Generating captions...")
    words = generate_captions(audio_path)
    chunks = group_into_chunks(words, words_per_chunk=3)

    print("[4/4] Assembling video...")
    assemble_video(audio_path, background_path, chunks, video_path)

    print(f"\nDone! Video saved to: {video_path}")
    return video_path


def _slugify(text: str, max_len: int = 50) -> str:
    safe = "".join(c if c.isalnum() or c in " -_" else "" for c in text)
    safe = "_".join(safe.split())
    return safe[:max_len] or "video"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate a faceless Reddit-story video.")
    parser.add_argument("reddit_url", help="URL of the Reddit post to narrate")
    parser.add_argument("background", help="Path to a background video clip (e.g. gameplay loop)")
    parser.add_argument("--name", default=None, help="Output file name (without extension)")
    parser.add_argument("--voice", default="male_us", help="Voice preset: male_us, female_us, male_uk, female_uk")
    args = parser.parse_args()

    run(args.reddit_url, args.background, args.name, args.voice)
