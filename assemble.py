"""Overlay narration audio + animated captions on a looping background clip, output a 9:16 MP4."""
import os
import random

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from moviepy import (
    AudioFileClip,
    CompositeVideoClip,
    ImageClip,
    TextClip,
    VideoFileClip,
    afx,
    vfx,
)

# 9:16 for TikTok/Shorts/Reels. This is a *floor*, not a fixed size: see
# _choose_target_size - a sharper source clip earns a sharper export instead of
# being needlessly downscaled to this.
EXPORT_FLOOR_SIZE = (1080, 1920)

# Cap how high we'll go even for very sharp sources, so file size/render time
# don't balloon for marginal-at-best gains past this on a phone screen.
MAX_TARGET_HEIGHT = 2560

# NOTE: NVENC (GPU) encoding was tested and found *slower* here (91s vs 53s) because
# the bottleneck is Python-side frame compositing (drawing captions per frame), not
# encoding - NVENC just adds pipe overhead on top of the same frame-generation cost.
# CPU encoding with a fast preset wins for this workload.
# crf 18 is visually near-lossless - libx264's default (~23) was compounding with
# the upscale from the source clip's 576x1280 to our 1080x1920 target and turning
# small HUD details (coin counts, stars) to mush. "veryfast" stays for speed; crf
# controls quality independently of preset (preset only trades encode speed for
# compression efficiency at a given quality).
ENCODE_KWARGS = {"codec": "libx264", "preset": "veryfast", "ffmpeg_params": ["-crf", "18"]}

# Stay this far away from the very start/end of a long background clip when picking
# a random segment - those edges are often black frames, intros, or loop seams.
BACKGROUND_EDGE_MARGIN_SECONDS = 3


def _choose_target_size(bg_w: int, bg_h: int) -> tuple[int, int]:
    """Pick the export resolution based on what the source clip can actually offer.

    A clip at or below the 1080x1920 floor (like the 576x1280 one currently in
    use - that's the highest YouTube has for it) gets upscaled to the floor as
    before. But a sharper source - true 1080x1920, 1440x2560, etc. - gets
    rendered at *its own* resolution (capped at MAX_TARGET_HEIGHT) so that
    detail is preserved instead of being thrown away by a forced downscale.
    """
    floor_w, floor_h = EXPORT_FLOOR_SIZE
    if bg_h <= floor_h:
        return EXPORT_FLOOR_SIZE
    height = min(bg_h, MAX_TARGET_HEIGHT)
    width = int(round(height * floor_w / floor_h))  # keep the 9:16 export ratio
    return width, height


def _pick_start_time(bg_duration: float, needed_duration: float, start_time: float | None) -> float:
    """Choose where in the background clip to start this segment.

    - start_time=None: pick a random spot (with margin from the clip's edges) so
      back-to-back renders from the same long clip don't all look identical.
    - start_time=<seconds>: continue from where a previous part's segment ended,
      for "Part 2" videos that should flow into the same backdrop. Wraps back to
      a fresh random spot if the clip doesn't have enough room left.
    """
    latest_start = bg_duration - needed_duration - BACKGROUND_EDGE_MARGIN_SECONDS
    if latest_start <= BACKGROUND_EDGE_MARGIN_SECONDS:
        return 0.0
    if start_time is None:
        return random.uniform(BACKGROUND_EDGE_MARGIN_SECONDS, latest_start)
    if start_time > latest_start:
        return random.uniform(BACKGROUND_EDGE_MARGIN_SECONDS, latest_start)
    return start_time


def _prepare_background(bg: VideoFileClip, duration: float, target_size: tuple[int, int],
                         start_time: float | None = None) -> tuple[VideoFileClip, float]:
    # Loop the background if it's shorter than the narration; otherwise grab a
    # randomized (or continued) slice so long source clips give each video variety.
    if bg.duration < duration:
        bg = bg.with_effects([vfx.Loop(duration=duration)])
        segment_end = duration
    else:
        start = _pick_start_time(bg.duration, duration, start_time)
        bg = bg.subclipped(start, start + duration)
        segment_end = start + duration

    # Crop to fill a 9:16 frame at `target_size`'s aspect ratio (always 9:16,
    # but computing the ratio from target_size keeps this correct if that ever changes).
    target_w, target_h = target_size
    target_ratio = target_w / target_h
    bg_ratio = bg.w / bg.h

    if bg_ratio > target_ratio:
        new_width = int(bg.h * target_ratio)
        x_center = bg.w / 2
        bg = bg.cropped(x_center=x_center, width=new_width)
    else:
        new_height = int(bg.w / target_ratio)
        # Anchor near the top rather than centering the crop: mobile gameplay
        # footage (Subway Surfers etc.) keeps its score/coin/streak HUD pinned to
        # the top of the frame, and a centered crop was slicing right through it.
        # Trimming from the bottom instead keeps that HUD fully visible.
        y1 = min(int(bg.h * 0.05), bg.h - new_height)
        y1 = max(y1, 0)
        bg = bg.cropped(y1=y1, height=new_height)

    return bg.resized(target_size), segment_end


# ---------------------------------------------------------------------------
# Reddit post card: drawn once as a still image and shown over the opening of
# the video so the title reads as "a post", separate from the narrated body
# captions that follow. Segoe UI ships with every Windows install and matches
# the clean sans-serif look of Reddit's own UI closely enough to read as one.
# ---------------------------------------------------------------------------
_FONTS_DIR = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
CARD_FONT_REGULAR = os.path.join(_FONTS_DIR, "segoeui.ttf")
CARD_FONT_BOLD = os.path.join(_FONTS_DIR, "segoeuib.ttf")

CARD_WIDTH_FRACTION = 0.85  # how wide the card is relative to the frame
CARD_TAIL_SECONDS = 0.6     # let the card linger a beat after the title finishes narrating

_CARD_BG = (255, 255, 255, 255)
_CARD_BORDER = (225, 227, 228, 255)
_CARD_TEXT_DARK = (26, 26, 27, 255)
_CARD_TEXT_GRAY = (120, 124, 126, 255)
_CARD_AVATAR_GRAY = (200, 202, 204, 255)
_CARD_PART_RED = (220, 38, 38, 255)


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    words = text.split()
    lines, current = [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=font) <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _draw_upvote_icon(draw, cx, cy, size, color):
    half = size / 2
    draw.polygon([(cx, cy - half), (cx - half, cy + half), (cx + half, cy + half)], outline=color, width=3)


def _draw_comment_icon(draw, cx, cy, size, color):
    w, h = size * 1.15, size * 0.85
    x0, y0, x1, y1 = cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2
    draw.rounded_rectangle([x0, y0, x1, y1], radius=h * 0.3, outline=color, width=3)
    draw.polygon([(cx - w * 0.18, y1 - 2), (cx - w * 0.05, y1 + h * 0.28), (cx + w * 0.08, y1 - 2)], fill=color)


def _draw_share_icon(draw, cx, cy, size, color):
    half = size / 2
    draw.line([(cx - half * 0.55, cy + half * 0.6), (cx + half * 0.65, cy - half * 0.65)], fill=color, width=3)
    draw.polygon(
        [(cx + half * 0.65, cy - half * 0.65), (cx + half * 0.05, cy - half * 0.65), (cx + half * 0.65, cy - half * 0.05)],
        fill=color,
    )


def _build_reddit_card_image(title: str, card_width: int, part: int | None = None) -> Image.Image:
    """Draw a Reddit-post-style card (avatar, "Anonymous", title, vote/comment/
    share row) as an RGBA image, sized to fit its wrapped title text. When `part`
    is given (2, 3, ...), a bold red "PART N" label is drawn at the top-right
    corner so viewers immediately know this is a continuation of a longer story."""
    padding = int(card_width * 0.06)
    avatar_size = int(card_width * 0.105)

    username_font = ImageFont.truetype(CARD_FONT_BOLD, int(card_width * 0.05))
    title_font = ImageFont.truetype(CARD_FONT_BOLD, int(card_width * 0.07))
    meta_font = ImageFont.truetype(CARD_FONT_REGULAR, int(card_width * 0.046))
    part_font = ImageFont.truetype(CARD_FONT_BOLD, int(card_width * 0.052))

    scratch = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    text_max_width = card_width - 2 * padding
    title_lines = _wrap_text(scratch, title, title_font, text_max_width)
    line_height = int(title_font.size * 1.28)

    footer_height = int(card_width * 0.085)
    card_height = (
        padding + avatar_size + int(padding * 0.75)
        + line_height * len(title_lines) + int(padding * 0.6)
        + footer_height + padding
    )

    img = Image.new("RGBA", (card_width, card_height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    radius = int(card_width * 0.04)
    draw.rounded_rectangle([1, 1, card_width - 2, card_height - 2], radius=radius,
                           fill=_CARD_BG, outline=_CARD_BORDER, width=2)

    x = y = padding
    draw.ellipse([x, y, x + avatar_size, y + avatar_size], fill=_CARD_AVATAR_GRAY)
    name_x = x + avatar_size + int(padding * 0.55)
    draw.text((name_x, y + avatar_size / 2), "Anonymous", font=username_font, fill=_CARD_TEXT_DARK, anchor="lm")

    if part and part > 1:
        part_label = f"PART {part}"
        part_x = card_width - padding - draw.textlength(part_label, font=part_font)
        draw.text((part_x, y + avatar_size / 2), part_label, font=part_font, fill=_CARD_PART_RED, anchor="lm")

    y += avatar_size + int(padding * 0.75)
    for line in title_lines:
        draw.text((x, y), line, font=title_font, fill=_CARD_TEXT_DARK)
        y += line_height

    y += int(padding * 0.6)
    icon_size = int(footer_height * 0.5)
    icon_y = y + footer_height / 2
    icon_x = x + icon_size / 2
    gap = int(card_width * 0.16)

    _draw_upvote_icon(draw, icon_x, icon_y, icon_size, _CARD_TEXT_GRAY)
    draw.text((icon_x + icon_size * 0.75, icon_y), "99+", font=meta_font, fill=_CARD_TEXT_GRAY, anchor="lm")
    icon_x += gap
    _draw_comment_icon(draw, icon_x, icon_y, icon_size, _CARD_TEXT_GRAY)
    draw.text((icon_x + icon_size * 0.85, icon_y), "99+", font=meta_font, fill=_CARD_TEXT_GRAY, anchor="lm")
    icon_x += gap
    _draw_share_icon(draw, icon_x, icon_y, icon_size, _CARD_TEXT_GRAY)
    draw.text((icon_x + icon_size * 0.75, icon_y), "Share", font=meta_font, fill=_CARD_TEXT_GRAY, anchor="lm")

    return img


def _reddit_card_clip(title: str, duration: float, target_size: tuple[int, int], part: int | None = None) -> ImageClip:
    card_width = int(target_size[0] * CARD_WIDTH_FRACTION)
    image = _build_reddit_card_image(title, card_width, part=part)
    clip = ImageClip(np.array(image), duration=duration)
    return clip.with_position(("center", "center"))


# ---------------------------------------------------------------------------
# "Follow for part N" call-to-action: a pill-shaped banner shown over the last
# few seconds of every part but the last, in a long story that's been
# auto-split (see main.py's pipeline_worker / _render_in_parts). It tells
# viewers exactly where the rest of the story is so they open the channel
# instead of scrolling past.
# ---------------------------------------------------------------------------
CTA_DURATION_SECONDS = 4.0
_CTA_BG = (220, 38, 38, 235)
_CTA_TEXT = (255, 255, 255, 255)


def _build_follow_cta_image(label: str, frame_width: int) -> Image.Image:
    font = ImageFont.truetype(CARD_FONT_BOLD, int(frame_width * 0.06))
    padding_x = int(frame_width * 0.08)
    padding_y = int(frame_width * 0.045)

    scratch = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    text_w = scratch.textlength(label, font=font)
    height = int(font.size + 2 * padding_y)
    width = int(text_w + 2 * padding_x)

    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([0, 0, width - 1, height - 1], radius=height // 2, fill=_CTA_BG)
    draw.text((width / 2, height / 2), label, font=font, fill=_CTA_TEXT, anchor="mm")
    return img


def _follow_cta_clip(next_part: int, total_duration: float, target_size: tuple[int, int]):
    """A "FOLLOW FOR PART N ->" banner shown over the last few seconds of a clip,
    pinned above the caption band so the two never overlap. Returns None if the
    clip is too short to fit even a trimmed-down banner."""
    cta_duration = min(CTA_DURATION_SECONDS, total_duration)
    if cta_duration <= 0:
        return None

    label = f"FOLLOW FOR PART {next_part} →"
    image = _build_follow_cta_image(label, target_size[0])
    clip = ImageClip(np.array(image), duration=cta_duration)
    clip = clip.with_start(total_duration - cta_duration)
    y = int(target_size[1] * 0.12)
    return clip.with_position(("center", y))


THUMBNAIL_SIZE = (1080, 1920)
_THUMB_BG = (18, 18, 20, 255)
_THUMB_TEXT = (255, 255, 255, 255)


def build_thumbnail_image(title: str, part: int | None = None, size: tuple[int, int] = THUMBNAIL_SIZE) -> Image.Image:
    """Render a custom YouTube thumbnail: the post title in large bold white text,
    centered on a dark background, with a bold red "PART N" line underneath when
    `part` is given (2, 3, ...) so a multi-part series reads clearly at a glance."""
    width, height = size
    padding = int(width * 0.09)
    text_max_width = width - 2 * padding

    title_font = ImageFont.truetype(CARD_FONT_BOLD, int(width * 0.095))
    part_font = ImageFont.truetype(CARD_FONT_BOLD, int(width * 0.11))

    img = Image.new("RGB", size, _THUMB_BG[:3])
    draw = ImageDraw.Draw(img)

    title_lines = _wrap_text(draw, title, title_font, text_max_width)
    line_height = int(title_font.size * 1.22)

    part_label = f"PART {part}" if part and part > 1 else None
    part_gap = int(height * 0.045)
    part_height = int(part_font.size * 1.2) if part_label else 0

    block_height = line_height * len(title_lines) + (part_gap + part_height if part_label else 0)
    y = (height - block_height) / 2

    for line in title_lines:
        line_x = (width - draw.textlength(line, font=title_font)) / 2
        draw.text((line_x, y), line, font=title_font, fill=_THUMB_TEXT)
        y += line_height

    if part_label:
        y += part_gap
        label_x = (width - draw.textlength(part_label, font=part_font)) / 2
        draw.text((label_x, y), part_label, font=part_font, fill=_CARD_PART_RED)

    return img


# MoviePy renders captions with PIL's tiny built-in bitmap font when no `font` is
# given - that's what was making them look "horrible". Impact is the classic bold,
# heavy-stroke caption font this style of video uses, and ships with every Windows install.
CAPTION_FONT = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "impact.ttf")


# Vertical anchor for the *center* of every caption block, as a fraction of frame
# height. Fixed (not "center","center"!) on purpose: chunks wrap to different
# numbers of lines, so centering each clip by its own bounding box made the text
# visibly jump up and down between chunks. Anchoring to a constant point keeps
# every caption locked in the same place. ~50% lands roughly mid-screen.
CAPTION_CENTER_Y_FRACTION = 0.50

# PIL's caption-mode height estimate runs short for multi-line text with a thick
# stroke - the bottom line's descenders and outline were getting sliced off by
# the canvas edge. A bottom margin pads the canvas so nothing gets clipped.
CAPTION_BOTTOM_PADDING = 30


def _caption_clips(chunks: list[dict], target_size: tuple[int, int]) -> list[TextClip]:
    """Build word-chunk caption clips for the given chunks. The caller is responsible
    for excluding any chunk that belongs to the title - see assemble_video, which
    only ever passes body chunks (the title is shown via the Reddit card instead)."""
    target_w, target_h = target_size
    # Scale font size with resolution so captions stay proportionally sized whether
    # we're rendering at the 1080x1920 floor or a sharper source's native resolution.
    font_size = int(round(72 * target_h / EXPORT_FLOOR_SIZE[1]))
    stroke_width = max(1, int(round(4 * target_h / EXPORT_FLOOR_SIZE[1])))
    bottom_padding = int(round(CAPTION_BOTTOM_PADDING * target_h / EXPORT_FLOOR_SIZE[1]))
    anchor_y = int(target_h * CAPTION_CENTER_Y_FRACTION)
    clips = []
    for chunk in chunks:
        txt = TextClip(
            font=CAPTION_FONT,
            text=chunk["text"].upper(),
            font_size=font_size,
            color="white",
            stroke_color="black",
            stroke_width=stroke_width,
            method="caption",
            size=(int(target_w * 0.85), None),
            text_align="center",
            margin=(0, 0, 0, bottom_padding),
        )
        txt = txt.with_start(chunk["start"]).with_duration(chunk["end"] - chunk["start"])
        txt = txt.with_position(("center", anchor_y - txt.h // 2))
        clips.append(txt)
    return clips


def assemble_video(audio_path: str, bg_path: str, caption_chunks: list[dict], out_path: str,
                   start_time: float | None = None, title: str | None = None, title_end: float = 0.0,
                   part: int | None = None, audio_start: float = 0.0, audio_end: float | None = None,
                   next_part: int | None = None):
    """Render the final video. `start_time` lets you continue a "Part 2" video from
    where the previous part's background segment left off; pass None for a fresh
    random segment. Returns (out_path, segment_end_time) - feed segment_end_time
    back in as start_time for the next part to keep the backdrop flowing.

    `title`, if given, is shown as a Reddit-post-style card over the opening of the
    video instead of being captioned word-by-word - `caption_chunks` should already
    exclude the title's words (see pipeline_worker, which splits the transcript at
    the title/body boundary before grouping chunks) and `title_end` is the timestamp
    where the title's narration ends, used to size the card's on-screen duration.

    `part`, if 2 or higher, draws a bold red "PART N" badge on the card so viewers
    can immediately tell this video continues a longer story.

    `audio_start`/`audio_end`, if given, trim the narration to just that span of
    `audio_path` - used to carve a single long narration into multiple ~60s videos
    (see main.py's _render_in_parts). `caption_chunks` and `title_end` should already
    be relative to `audio_start` (i.e. as if the trimmed clip started at zero).

    `next_part`, if given, overlays a "FOLLOW FOR PART N" banner over the last few
    seconds so viewers know where to find the rest of a multi-part story."""
    audio = AudioFileClip(audio_path)
    if audio_end is not None:
        audio = audio.subclipped(audio_start, audio_end)
    elif audio_start:
        audio = audio.subclipped(audio_start)

    bg = VideoFileClip(bg_path)

    # Render at the source clip's own resolution when it's sharper than our
    # 1080x1920 floor, instead of always forcing it down to that - see _choose_target_size.
    target_size = _choose_target_size(bg.w, bg.h)

    bg, segment_end = _prepare_background(bg, audio.duration, target_size, start_time=start_time)
    bg = bg.with_audio(audio)

    overlays = []
    if title and title_end > 0:
        card_duration = min(title_end + CARD_TAIL_SECONDS, audio.duration)
        overlays.append(_reddit_card_clip(title, card_duration, target_size, part=part))

    overlays.extend(_caption_clips(caption_chunks, target_size))

    if next_part:
        cta = _follow_cta_clip(next_part, audio.duration, target_size)
        if cta:
            overlays.append(cta)

    final = CompositeVideoClip([bg, *overlays], size=target_size).with_duration(audio.duration)

    # 24fps cuts ~20% of the frames Python has to composite+encode vs 30fps with no
    # visible difference for talking/caption-style content. threads matches available
    # cores better than the old default of 4 (this machine has 24 logical cores).
    # logger=None disables moviepy's tqdm/proglog progress bars - they try to print
    # Unicode block characters ("▉") which crash with UnicodeEncodeError on
    # Windows consoles/subprocess pipes using the legacy cp1252 codec.
    final.write_videofile(out_path, fps=24, audio_codec="aac", threads=8, logger=None, **ENCODE_KWARGS)

    audio.close()
    bg.close()
    final.close()
    return out_path, segment_end
