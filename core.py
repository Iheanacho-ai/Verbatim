"""Shared transcription core used by both the CLI and the web server.

Keeps all the ffmpeg / yt-dlp / faster-whisper logic in one place so the two
front-ends (transcribe.py CLI, server.py web app) stay thin.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path

MEDIA_EXTS = {
    ".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".m4v", ".mpg", ".mpeg",
    ".wmv", ".ts", ".3gp",  # video
    ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma",  # audio
}

# Standalone disfluencies + a few hedge phrases. Conservative on purpose so we
# don't mangle real words ("like" is left alone — too risky).
_FILLER_WORDS = r"\b(?:um+|uh+|uhh+|umm+|er+|ah+|erm|hmm+|mm+|mhm)\b"
_FILLER_PHRASES = r"\b(?:you know|i mean|sort of|kind of)\b"

_ONE_MODEL_CACHE: dict[str, object] = {}


class TranscribeError(RuntimeError):
    """Raised for user-facing failures (bad URL, ffmpeg missing, etc.)."""


def _require(cmd: str, hint: str) -> None:
    if shutil.which(cmd) is None:
        raise TranscribeError(f"`{cmd}` not found on PATH. {hint}")


def is_url(source: str) -> bool:
    return source.startswith(("http://", "https://", "www."))


def format_timestamp(seconds: float, comma: bool = False) -> str:
    """Seconds -> HH:MM:SS,mmm (srt) or HH:MM:SS.mmm (vtt)."""
    ms = int(round(seconds * 1000))
    td = timedelta(milliseconds=ms)
    hours, rem = divmod(td.seconds + td.days * 86400, 3600)
    minutes, secs = divmod(rem, 60)
    millis = td.microseconds // 1000
    sep = "," if comma else "."
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{sep}{millis:03d}"


def strip_fillers(text: str) -> str:
    """Remove common filler words/phrases and tidy the resulting whitespace."""
    out = re.sub(_FILLER_PHRASES, "", text, flags=re.IGNORECASE)
    out = re.sub(_FILLER_WORDS, "", out, flags=re.IGNORECASE)
    out = re.sub(r"\s+([,.!?])", r"\1", out)      # space before punctuation
    out = re.sub(r"(?:\s*,\s*){2,}", ", ", out)   # collapse orphaned/doubled commas
    out = re.sub(r"\s*,\s*([.!?])", r"\1", out)   # comma stranded before end punct
    out = re.sub(r"^[\s,]+", "", out)              # leading commas/space
    out = re.sub(r"\s{2,}", " ", out).strip()      # collapse doubled spaces
    return out


def download_audio(url: str, workdir: Path, progress=None) -> tuple[Path, str]:
    """Download best audio from a URL. Returns (audio_path, title).

    If `progress` is given, it's called with a float 0..1 as the download
    proceeds (using yt-dlp's Python API so we get real progress hooks).
    """
    try:
        import yt_dlp
    except ImportError as exc:
        raise TranscribeError("yt-dlp not installed. Run: pip install yt-dlp") from exc

    def hook(d):
        if progress and d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes") or 0
            if total:
                progress(min(done / total, 1.0))

    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(workdir / "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [hook],
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}],
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as exc:
        raise TranscribeError(f"Couldn't fetch that link.\n{str(exc)[:400]}") from exc

    if progress:
        progress(1.0)

    title = info.get("title") or "audio"
    path = None
    reqs = info.get("requested_downloads")
    if reqs:
        candidate = reqs[0].get("filepath") or reqs[0].get("_filename")
        if candidate:
            path = Path(candidate)
    if path is None or not path.exists():
        mp3s = list(workdir.glob("*.mp3"))
        files = mp3s or list(workdir.iterdir())
        if not files:
            raise TranscribeError("Download produced no file.")
        path = files[0]
    return path, title


def extract_audio(media: Path, workdir: Path) -> Path:
    """Extract a clean 16kHz mono wav from a media file via ffmpeg."""
    _require("ffmpeg", "Install with: brew install ffmpeg")
    out = workdir / "audio.wav"
    result = subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(media),
            "-vn", "-ac", "1", "-ar", "16000",
            "-loglevel", "error", str(out),
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise TranscribeError(f"ffmpeg failed:\n{result.stderr.strip()[:500]}")
    return out


def media_duration(media: Path) -> float | None:
    """Return duration in seconds via ffprobe, or None if unavailable."""
    if shutil.which("ffprobe") is None:
        return None
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(media),
        ],
        capture_output=True, text=True,
    )
    try:
        return float(result.stdout.strip())
    except (ValueError, AttributeError):
        return None


def load_model(model_name: str):
    """Load (and cache) a faster-whisper model."""
    if model_name in _ONE_MODEL_CACHE:
        return _ONE_MODEL_CACHE[model_name]
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:  # pragma: no cover
        raise TranscribeError(
            "faster-whisper not installed. Run: pip install faster-whisper"
        ) from exc
    model = WhisperModel(model_name, device="auto", compute_type="int8")
    _ONE_MODEL_CACHE[model_name] = model
    return model


def transcribe_audio(
    audio: Path,
    model_name: str = "base",
    language: str | None = None,
    remove_fillers: bool = False,
    on_segment=None,
):
    """Transcribe an audio file.

    Returns (segments, info) where segments is a list of
    {start, end, text} dicts. If on_segment is given, it's called as
    on_segment(item, pct) for each segment as it's produced, where pct is a
    0..1 progress estimate (segment end / total duration). Useful for
    streaming progress.
    """
    model = load_model(model_name)
    segments, info = model.transcribe(
        str(audio),
        language=language,
        vad_filter=True,
        beam_size=5,
    )
    duration = getattr(info, "duration", None) or 0

    collected = []
    for seg in segments:
        text = seg.text.strip()
        if remove_fillers:
            text = strip_fillers(text)
        if not text:
            continue
        item = {"start": seg.start, "end": seg.end, "text": text}
        collected.append(item)
        if on_segment is not None:
            pct = min(seg.end / duration, 1.0) if duration else None
            on_segment(item, pct)
    return collected, info


# ---- output writers --------------------------------------------------------

def to_txt(segments) -> str:
    return "\n".join(s["text"] for s in segments) + "\n"


def to_srt(segments) -> str:
    lines = []
    for i, s in enumerate(segments, 1):
        lines.append(str(i))
        lines.append(
            f"{format_timestamp(s['start'], comma=True)} --> "
            f"{format_timestamp(s['end'], comma=True)}"
        )
        lines.append(s["text"])
        lines.append("")
    return "\n".join(lines)


def to_vtt(segments) -> str:
    lines = ["WEBVTT", ""]
    for s in segments:
        lines.append(f"{format_timestamp(s['start'])} --> {format_timestamp(s['end'])}")
        lines.append(s["text"])
        lines.append("")
    return "\n".join(lines)


def to_json(segments, info) -> dict:
    return {
        "language": getattr(info, "language", None),
        "language_probability": getattr(info, "language_probability", None),
        "duration": getattr(info, "duration", None),
        "segments": segments,
        "text": " ".join(s["text"] for s in segments),
    }
