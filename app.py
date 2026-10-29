
#!/usr/bin/env python3
"""MUJTABA Downloads - download videos, audio and pictures."""

import os
import re
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file, send_from_directory

import downloader as dl

BASE = Path(__file__).parent.resolve()

# Use the static folder if it exists; otherwise use the main folder.
STATIC = BASE / "static" if (BASE / "static" / "index.html").is_file() else BASE

JOBS_DIR = dl.setup(BASE)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024


@app.after_request
def headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"

    if request.path.startswith("/api/") and "file" not in request.path and "zip" not in request.path:
        response.headers["Cache-Control"] = "no-store"

    return response


@app.route("/")
def index():
    return send_from_directory(STATIC, "index.html")


@app.route("/<path:name>")
def static_files(name):
    allowed_files = {
        "sw.js",
        "manifest.webmanifest",
        "icon-192.png",
        "icon-512.png",
        "favicon.png",
    }

    if name in allowed_files:
        return send_from_directory(STATIC, name)

    abort(404)


@app.route("/healthz")
def healthz():
    return jsonify(ok=True)


@app.route("/api/start", methods=["POST"])
def api_start():
    data = request.get_json(force=True, silent=True) or {}
    raw_urls = data.get("urls") or []

    if isinstance(raw_urls, str):
        raw_urls = raw_urls.splitlines()

    urls = []

    for url in raw_urls:
        url = url.strip() if isinstance(url, str) else ""

        if url and url not in urls:
            urls.append(url)

    if not urls:
        return jsonify(error="Paste at least one link."), 400

    if len(urls) > dl.MAX_URLS:
        return jsonify(
            error=f"Please download at most {dl.MAX_URLS} links at a time."
        ), 400

    bad_urls = [
        url for url in urls
        if not dl.URL_RE.match(url) or len(url) > 2000
    ]

    if bad_urls:
        return jsonify(
            error="Please enter valid links starting with http:// or https://."
        ), 400

    if not all(dl.safe_host(url) for url in urls):
        return jsonify(error="That address is not allowed."), 400

    if not dl.rate_ok(dl.client_ip(request)):
        return jsonify(
            error=f"You have reached the hourly limit ({dl.JOBS_PER_HOUR}). Please try again later."
        ), 429

    kind = data.get("kind")
    if kind not in ("auto", "video", "audio", "image"):
        kind = "auto"

    quality = data.get("quality")
    if quality not in dl.QUALITIES:
        quality = "best"

    options = {
        "kind": kind,
        "quality": quality,
        "playlist": bool(data.get("playlist")),
    }

    return jsonify(id=dl.new_job(urls, options, JOBS_DIR))


@app.route("/api/status/<job_id>")
def api_status(job_id):
    with dl.LOCK:
        job = dl.JOBS.get(job_id)

        if not job:
            return jsonify(
                error=f"This download has expired. Files are removed after {dl.KEEP_MINUTES} minutes. Please start again.",
                expired=True,
            ), 404

        keys = (
            "id", "stage", "progress", "message",
            "done", "error", "files", "zip_url"
        )

        return jsonify({key: job[key] for key in keys})


@app.route("/api/file/<job_id>/<path:rel>")
def api_file(job_id, rel):
    if not re.match(r"^[0-9a-f]{16}$", job_id):
        abort(404)

    base = (JOBS_DIR / job_id / "dl").resolve()
    file_path = (base / rel).resolve()

    if base not in file_path.parents or not file_path.is_file() or not dl.kind_of(file_path):
        abort(404)

    return send_file(
        file_path,
        as_attachment=request.args.get("dl") == "1",
        download_name=file_path.name,
        conditional=True,
    )


@app.route("/api/zip/<job_id>")
def api_zip(job_id):
    if not re.match(r"^[0-9a-f]{16}$", job_id):
        abort(404)

    zip_path = JOBS_DIR / job_id / "all_files.zip"

    if not zip_path.is_file():
        abort(404)

    return send_file(
        zip_path,
        mimetype="application/zip",
        as_attachment=True,
        download_name="MUJTABA_Downloads.zip",
    )


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "7860")),
        threaded=True,
    )

