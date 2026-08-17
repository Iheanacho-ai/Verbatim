#!/usr/bin/env python3
"""Verbatim — a tiny local web app for offline video/audio transcription.

Run:
    ./venv/bin/python server.py
Then open http://127.0.0.1:5001 in your browser.

Everything runs on your machine: uploads and URLs are processed locally with
faster-whisper. Nothing is sent to any third party except the media host when
you paste a URL (yt-dlp has to fetch it).
"""

from __future__ import annotations

import json
import queue
import shutil
import tempfile
import threading
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request, stream_with_context

import core

app = Flask(__name__)
# Allow large-ish uploads (2 GB). Adjust if you need bigger files.
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024

# faster-whisper model size. "base" is a good default; bump to "small"/"medium"
# for better accuracy at the cost of speed.
MODEL_NAME = "base"


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/transcribe", methods=["POST"])
def transcribe():
    """Stream progress + the transcript as Server-Sent Events.

    The heavy work runs in a worker thread that pushes events onto a queue; the
    response generator drains the queue and emits them as `data: {...}` lines.
    """
    language = request.form.get("language") or None
    if language == "auto":
        language = None
    remove_fillers = request.form.get("remove_fillers") == "true"
    url = (request.form.get("url") or "").strip()
    upload = request.files.get("file")

    if not url and (upload is None or not upload.filename):
        return jsonify(error="Give me a file or a URL to work with."), 400

    # The request context is gone once streaming starts, so persist the upload
    # to a temp dir now and clean it up when the stream finishes.
    tmpdir = Path(tempfile.mkdtemp(prefix="verbatim_"))
    saved_upload = None
    if upload is not None and upload.filename:
        saved_upload = tmpdir / Path(upload.filename).name
        upload.save(saved_upload)

    def worker(q: queue.Queue):
        last = {"p": -1.0}

        def on_download(frac: float):
            if frac - last["p"] >= 0.01 or frac >= 1.0:
                last["p"] = frac
                q.put({"stage": "download", "pct": frac})

        def on_segment(item, pct):
            q.put({"stage": "transcribe", "pct": pct or 0, "seg": item})

        try:
            if saved_upload is not None:
                media, source_name = saved_upload, saved_upload.name
            else:
                q.put({"stage": "download", "pct": 0})
                media, source_name = core.download_audio(url, tmpdir, progress=on_download)

            duration = core.media_duration(media)
            q.put({"stage": "extract"})
            audio = core.extract_audio(media, tmpdir)

            q.put({"stage": "model"})
            segments, info = core.transcribe_audio(
                audio,
                model_name=MODEL_NAME,
                language=language,
                remove_fillers=remove_fillers,
                on_segment=on_segment,
            )

            payload = core.to_json(segments, info)
            payload["source_name"] = source_name
            if duration is not None:
                payload["duration"] = duration
            payload["srt"] = core.to_srt(segments)
            payload["txt"] = core.to_txt(segments)
            q.put({"stage": "done", "payload": payload})
        except core.TranscribeError as exc:
            q.put({"stage": "error", "error": str(exc)})
        except Exception as exc:  # pragma: no cover - defensive
            q.put({"stage": "error", "error": f"Unexpected error: {exc}"})
        finally:
            q.put(None)  # sentinel: stream is done

    @stream_with_context
    def generate():
        q: queue.Queue = queue.Queue()
        threading.Thread(target=worker, args=(q,), daemon=True).start()
        try:
            while True:
                item = q.get()
                if item is None:
                    break
                yield f"data: {json.dumps(item)}\n\n"
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    print("\n  Verbatim running at  http://127.0.0.1:5001\n")
    app.run(host="127.0.0.1", port=5001, debug=False, threaded=True)
