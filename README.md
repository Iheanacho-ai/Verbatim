# 🎙️ Verbatim

Verbatim turns a video or audio file into a readable transcript. Give it a
local file or paste a link and it does the rest. Everything runs on your own
machine, so there are no API keys, no per-minute fees, and nothing gets
uploaded anywhere.

It comes as a small web app with a drag-and-drop interface, and there's also a
command-line version if you'd rather script it. Both use the same engine.

## What it does

Drop in an MP4, MOV, WAV, or MP3, or paste a YouTube, Vimeo, Drive, or direct
media link. You can read the result two ways: as an article, where the text is
grouped into paragraphs at the natural pauses in speech, or as timestamped
segments. A toggle switches between them.

While it works, a progress bar and status line keep you posted (downloading,
extracting audio, then transcribing), and the transcript fills in segment by
segment as it goes rather than appearing all at once when it finishes.

There are a few settings. It can strip filler words like "um", "uh", and "you
know" and clean up the leftover punctuation. It auto-detects the language or you
can pick from fourteen. When you're done, copy the text or download it as SRT
subtitles or a plain TXT file.

Under the hood the speech-to-text is [faster-whisper](https://github.com/SYSTRAN/faster-whisper),
so the audio never leaves your computer. The only time Verbatim touches the
network is to fetch a link you paste.

## Setup

You'll need two system tools first:

```bash
brew install ffmpeg   # for pulling audio out of video
brew install deno      # a JS runtime yt-dlp needs to fetch from YouTube
```

Then set up the Python side:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

A quick note on deno: recent YouTube changes mean yt-dlp needs a JavaScript
runtime to fetch videos, and without one you'll get an `HTTP 403 Forbidden`
error. yt-dlp finds deno on its own once it's installed. If YouTube breaks again
down the line, updating yt-dlp usually sorts it out (`pip install -U yt-dlp`).

## Running the web app

```bash
./venv/bin/python server.py
```

Then open http://127.0.0.1:5001. Drop a file or paste a link, adjust the
settings if you want, and hit Transcribe (or Fetch & transcribe for a link). The
transcript shows up on the right as an article, and you can flip it to the
timecoded view whenever.

One gotcha: the server caches its HTML while it's running, so if you edit
`templates/index.html`, restart the server to see the change.

## Using the command line

The CLI is handy for batch jobs and scripting:

```bash
# from a link
./transcribe.py "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

# from a local file
./transcribe.py my_video.mp4

# bigger model, subtitle output
./transcribe.py talk.mov --model small --format srt

# every format, into a folder
./transcribe.py interview.mp4 --format all --output-dir ./out
```

If you didn't activate the venv, run it as `./venv/bin/python transcribe.py
my_video.mp4`.

The options:

| Flag | What it does | Default |
|------|-------------|---------|
| `source` | a URL or a local media file (required) | |
| `-m`, `--model` | `tiny`, `base`, `small`, `medium`, `large-v3` | `base` |
| `-l`, `--language` | language code (`en`, `es`, …), or auto-detect | auto |
| `-f`, `--format` | `txt`, `srt`, `vtt`, `json`, or `all` | `txt` |
| `-o`, `--output-dir` | where to write the files | `.` |
| `-n`, `--name` | base name for the output files | from source |

## Picking a model

Bigger models are more accurate but slower. Whichever you use downloads once on
first run and is cached after that. The web app uses `base` (change `MODEL_NAME`
in `server.py` if you want something else); the CLI takes `--model`.

| Model | Size | Speed | Accuracy |
|-------|------|-------|----------|
| `tiny` | 75 MB | fastest | basic |
| `base` | 145 MB | fast | good |
| `small` | 500 MB | medium | better |
| `medium` | 1.5 GB | slow | great |
| `large-v3` | 3 GB | slowest | best |

On an Apple Silicon Mac, `base` and `small` hit a good balance on CPU. If a GPU
is available it's used automatically.

## Output formats

The web app shows the article view (paragraphs grouped at natural pauses) and
exports Copy and TXT from it. The CLI writes files:

- `txt`: the plain transcript
- `srt`: subtitles with timestamps, for video players
- `vtt`: WebVTT subtitles, for the web
- `json`: everything, including segments, timings, and the detected language

## What's in the project

| File | What it is |
|------|------------|
| `server.py` | the Flask web app and its streaming transcribe endpoint |
| `templates/index.html` | the single-page frontend |
| `core.py` | the shared engine: downloading, audio extraction, transcription, exporters |
| `transcribe.py` | the command-line tool |
| `requirements.txt` | Python dependencies |

## How it works

When you paste a link, yt-dlp downloads the best available audio; a local file
is used as-is. ffmpeg then converts it to 16 kHz mono WAV, and faster-whisper
runs the speech-to-text locally, skipping silences as it goes. The segments come
back grouped into article paragraphs or timestamped lines. In the web app, the
progress and the segments stream to the browser live using Server-Sent Events.
