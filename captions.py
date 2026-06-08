"""Transcribe a voiceover into word-level timed captions using local Whisper.

This machine's RTX 5070 (Blackwell / sm_120) only works with a *nightly* CUDA build
of PyTorch, and that combination can deadlock mid-transcription - it reports CUDA
available, then hangs forever instead of computing or raising an error. So GPU
transcription runs in a throwaway subprocess with a timeout: if it finishes in time
we get the fast result, and if it hangs we kill the subprocess and fall back to a
plain CPU transcription (slower, but it always completes).
"""
import multiprocessing as mp

import torch
import whisper

_model_cache = {}

# Generous for a ~60-90s narration on GPU (whisper "base" normally takes single-digit
# seconds there); if it's still running after this, treat it as hung rather than slow.
GPU_TRANSCRIBE_TIMEOUT_SECONDS = 60


def _get_model(size: str, device: str):
    key = (size, device)
    if key not in _model_cache:
        _model_cache[key] = whisper.load_model(size, device=device)
    return _model_cache[key]


def _words_from_result(result: dict) -> list[dict]:
    words = []
    for segment in result["segments"]:
        for w in segment.get("words", []):
            words.append({"word": w["word"].strip(), "start": w["start"], "end": w["end"]})
    return words


def _transcribe_on_gpu(audio_path: str, model_size: str, queue: "mp.Queue"):
    """Runs in a subprocess so a CUDA deadlock can be killed without hanging the whole app."""
    try:
        model = whisper.load_model(model_size, device="cuda")
        result = model.transcribe(audio_path, word_timestamps=True, fp16=True)
        queue.put(("ok", _words_from_result(result)))
    except Exception as exc:
        queue.put(("error", str(exc)))


def generate_captions(audio_path: str, model_size: str = "base", on_status=None) -> list[dict]:
    """Return a list of {"word": str, "start": float, "end": float} for the given audio file.

    Tries fast GPU transcription first (with a timeout in case it deadlocks), and
    transparently falls back to CPU transcription if the GPU attempt hangs or errors.
    Pass `on_status(message)` to get a play-by-play of which stage is currently running
    (handy for showing live progress in a GUI instead of one static "generating..." label).
    """
    def report(message):
        if on_status:
            on_status(message)

    if torch.cuda.is_available():
        report("Transcribing captions on GPU...")
        queue = mp.Queue()
        proc = mp.Process(target=_transcribe_on_gpu, args=(audio_path, model_size, queue), daemon=True)
        proc.start()
        proc.join(GPU_TRANSCRIBE_TIMEOUT_SECONDS)

        if proc.is_alive():
            report("GPU transcription is taking too long - switching to CPU...")
            proc.terminate()
            proc.join()
        else:
            status, payload = queue.get() if not queue.empty() else ("error", "no result")
            if status == "ok":
                return payload
            report("GPU transcription failed - switching to CPU...")

    report("Transcribing captions on CPU (slower, but reliable)...")
    model = _get_model(model_size, "cpu")
    result = model.transcribe(audio_path, word_timestamps=True, fp16=False)
    return _words_from_result(result)


def group_into_chunks(words: list[dict], words_per_chunk: int = 3) -> list[dict]:
    """Group consecutive words into short on-screen caption chunks (e.g. 2-4 words each)."""
    chunks = []
    for i in range(0, len(words), words_per_chunk):
        group = words[i : i + words_per_chunk]
        if not group:
            continue
        chunks.append(
            {
                "text": " ".join(w["word"] for w in group),
                "start": group[0]["start"],
                "end": group[-1]["end"],
            }
        )
    return chunks


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python captions.py <audio_path>")
        raise SystemExit(1)

    words = generate_captions(sys.argv[1])
    for chunk in group_into_chunks(words):
        print(f"[{chunk['start']:.2f}-{chunk['end']:.2f}] {chunk['text']}")
