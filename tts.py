"""Generate a narrated voiceover from text using edge-tts (free, local, Microsoft voices)."""
import asyncio
import edge_tts

# A few solid "storytime" style voices. Run `edge-tts --list-voices` for the full list.
VOICES = {
    "male_us": "en-US-GuyNeural",
    "female_us": "en-US-AriaNeural",
    "male_uk": "en-GB-RyanNeural",
    "female_uk": "en-GB-SoniaNeural",
}


async def _generate(text: str, voice: str, out_path: str, rate: str = "+0%"):
    communicate = edge_tts.Communicate(text, voice, rate=rate)
    await communicate.save(out_path)


def generate_voiceover(text: str, out_path: str, voice: str = "male_us", rate: str = "+0%"):
    """Render `text` to an mp3 file at `out_path` using the named voice preset or a raw edge-tts voice id."""
    voice_id = VOICES.get(voice, voice)
    asyncio.run(_generate(text, voice_id, out_path, rate))
    return out_path


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        print("Usage: python tts.py <output_mp3_path> <text...>")
        raise SystemExit(1)

    out_path = sys.argv[1]
    text = " ".join(sys.argv[2:])
    generate_voiceover(text, out_path)
    print(f"Saved voiceover to {out_path}")
