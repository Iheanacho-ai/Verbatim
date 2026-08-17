#!/usr/bin/env python3
"""Transcribe video (or audio) from a URL or a local file, fully offline.

Uses faster-whisper for speech-to-text, yt-dlp to fetch remote media, and
ffmpeg to extract audio. No API keys, no per-minute cost, nothing leaves the
machine.

Examples:
    ./transcribe.py https://www.youtube.com/watch?v=xxxx
    ./transcribe.py my_video.mp4 --model small --format srt
    ./transcribe.py talk.mov --language en --format all -o ./out
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

# Extensions we treat as "already a local media file" rather than a URL.
MEDIA_EXTS = {
    ".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".m4v", ".mpg", ".mpeg",
    ".wmv", ".ts", ".3gp",  # video
    ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma",  # audio
}

MODELS = ["tiny", "base", "small", "medium", "large-v3"]
FORMATS = ["txt", "srt", "vtt", "json", "all"]


def die(msg: str, code: int = 1) -> None:
    print(f"\033[31merror:\033[0m {msg}", file=sys.stderr)
    sys.exit(code)


def info(msg: str) -> None:
    print(f"\033[36m›\033[0m {msg}", file=sys.stderr)


def require(cmd: str, hint: str) -> None:
    if shutil.which(cmd) is None:
        die(f"`{cmd}` not found on PATH. {hint}")


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


def download_audio(url: str, workdir: Path) -> Path:
    """Download best audio from a URL and return the local file path."""
    require("yt-dlp", "Install with: pip install yt-dlp")
    out_tmpl = str(workdir / "%(id)s.%(ext)s")
    info(f"downloading audio from {url}")
    result = subprocess.run(
        [
            "yt-dlp",
            "-f", "bestaudio/best",
            "-x", "--audio-format", "mp3",
            "--no-playlist",
            "-o", out_tmpl,
            "--print", "after_move:filepath",
            "--no-simulate",
            url,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        die(f"yt-dlp failed:\n{result.stderr.strip()}")
    # --print after_move:filepath emits the final path on stdout.
    for line in reversed(result.stdout.strip().splitlines()):
        p = Path(line.strip())
        if p.exists():
            return p
    # Fallback: grab whatever landed in the workdir.
    files = list(workdir.iterdir())
    if not files:
        die("download produced no file")
    return files[0]


def extract_audio(media: Path, workdir: Path) -> Path:
    """Extract a clean 16kHz mono wav from a local media file via ffmpeg."""
    require("ffmpeg", "Install with: brew install ffmpeg")
    out = workdir / "audio.wav"
    info(f"extracting audio from {media.name}")
    result = subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(media),
            "-vn", "-ac", "1", "-ar", "16000",
            "-loglevel", "error",
            str(out),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        die(f"ffmpeg failed:\n{result.stderr.strip()}")
    return out


def transcribe(audio: Path, model_name: str, language: str | None):
    """Run faster-whisper and return (segments_list, info)."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        die("faster-whisper not installed. Install with: pip install faster-whisper")

    info(f"loading model '{model_name}' (first run downloads it, then it's cached)")
    # int8 keeps CPU memory/speed reasonable; auto device picks GPU if available.
    model = WhisperModel(model_name, device="auto", compute_type="int8")

    info("transcribing… (this can take a while on CPU)")
    segments, tinfo = model.transcribe(
        str(audio),
        language=language,
        vad_filter=True,  # skip long silences
        beam_size=5,
    )

    collected = []
    for seg in segments:
        collected.append({"start": seg.start, "end": seg.end, "text": seg.text.strip()})
        # Live progress to stderr so you can watch it work.
        print(
            f"  [{format_timestamp(seg.start)} -> {format_timestamp(seg.end)}] {seg.text.strip()}",
            file=sys.stderr,
        )
    return collected, tinfo


def write_txt(segments, path: Path) -> None:
    path.write_text("\n".join(s["text"] for s in segments) + "\n", encoding="utf-8")


def write_srt(segments, path: Path) -> None:
    lines = []
    for i, s in enumerate(segments, 1):
        lines.append(str(i))
        lines.append(
            f"{format_timestamp(s['start'], comma=True)} --> "
            f"{format_timestamp(s['end'], comma=True)}"
        )
        lines.append(s["text"])
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def write_vtt(segments, path: Path) -> None:
    lines = ["WEBVTT", ""]
    for s in segments:
        lines.append(f"{format_timestamp(s['start'])} --> {format_timestamp(s['end'])}")
        lines.append(s["text"])
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def write_json(segments, tinfo, path: Path) -> None:
    import json

    payload = {
        "language": getattr(tinfo, "language", None),
        "language_probability": getattr(tinfo, "language_probability", None),
        "duration": getattr(tinfo, "duration", None),
        "segments": segments,
        "text": " ".join(s["text"] for s in segments),
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Transcribe a video/audio file or URL, offline, with Whisper.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("source", help="A URL (YouTube, etc.) or a path to a local media file")
    parser.add_argument(
        "-m", "--model", default="base", choices=MODELS,
        help="Whisper model size (bigger = more accurate, slower). Default: base",
    )
    parser.add_argument(
        "-l", "--language", default=None,
        help="Language code (e.g. en, es, fr). Default: auto-detect",
    )
    parser.add_argument(
        "-f", "--format", default="txt", choices=FORMATS,
        help="Output format. 'all' writes every format. Default: txt",
    )
    parser.add_argument(
        "-o", "--output-dir", default=".",
        help="Directory to write output files. Default: current dir",
    )
    parser.add_argument(
        "-n", "--name", default=None,
        help="Base name for output files. Default: derived from the source",
    )
    args = parser.parse_args()

    out_dir = Path(args.output_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="transcribe_") as tmp:
        workdir = Path(tmp)

        if is_url(args.source):
            downloaded = download_audio(args.source, workdir)
            audio = extract_audio(downloaded, workdir)
            default_name = downloaded.stem
        else:
            media = Path(args.source).expanduser()
            if not media.exists():
                die(f"file not found: {media}")
            if media.suffix.lower() not in MEDIA_EXTS:
                info(f"warning: '{media.suffix}' isn't a recognized media type, trying anyway")
            audio = extract_audio(media, workdir)
            default_name = media.stem

        base = args.name or default_name
        segments, tinfo = transcribe(audio, args.model, args.language)

        if not segments:
            die("no speech detected — nothing to write")

        info(
            f"detected language: {getattr(tinfo, 'language', '?')} "
            f"({getattr(tinfo, 'language_probability', 0):.0%} confidence)"
        )

        wanted = FORMATS[:-1] if args.format == "all" else [args.format]
        written = []
        for fmt in wanted:
            path = out_dir / f"{base}.{fmt}"
            if fmt == "txt":
                write_txt(segments, path)
            elif fmt == "srt":
                write_srt(segments, path)
            elif fmt == "vtt":
                write_vtt(segments, path)
            elif fmt == "json":
                write_json(segments, tinfo, path)
            written.append(path)

    print(f"\n\033[32m✓\033[0m done — {len(segments)} segments", file=sys.stderr)
    for p in written:
        print(p)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        die("interrupted", code=130)
