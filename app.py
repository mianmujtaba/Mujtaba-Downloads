#!/usr/bin/env python3
"""MUJTABA Downloads - download videos, MP3 and pictures from social media links. Made by MUJTABA."""
import os
import re
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file, send_from_directory

import downloader as dl

BASE = Path(__file__).parent.resolve()
STATIC = BASE / "static"
JOBS_DIR = dl.setup(BASE)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024  # requests are tiny JSON bodies


@app.after_request
def headers(r):
    r.headers["X-Content-Type-Options"] = "nosniff"
    r.headers["Referrer-Policy"] = "no-referrer"
    if request.path.startswith("/api/") and "file" not in request.path and "zip" not in request.path:
        r.headers["Cache-Control"] = "no-store"
    return r


@app.route("/")
def index():
    return send_from_directory(STATIC, "index.html")


@app.route("/<path:name>")
def static_files(name):
    if name in ("sw.js", "manifest.webmanifest", "icon-192.png", "icon-512.png", "favicon.png"):
        return send_from_directory(STATIC, name)
    abort(404)


@app.route("/healthz")
def healthz():
    return jsonify(ok=True)


@app.route("/api/start", methods=["POST"])
def api_start():
    d = request.get_json(force=True, silent=True) or {}
    raw = d.get("urls") or []
    if isinstance(raw, str):
        raw = raw.splitlines()
    urls = []
    for u in raw:
        u = (u or "").strip() if isinstance(u, str) else ""
        if u and u not in urls:
            urls.append(u)
    if not urls:
        return jsonify(error="Paste at least one link."), 400
    if len(urls) > dl.MAX_URLS:
        return jsonify(error="Please download at most %d links at a time." % dl.MAX_URLS), 400
    bad = [u for u in urls if not dl.URL_RE.match(u) or len(u) > 2000]
    if bad:
        return jsonify(error="These do not look like web links (they must start with http:// or https://): "
                             + ", ".join(b[:80] for b in bad[:3])), 400
    if not all(dl.safe_host(u) for u in urls):
        return jsonify(error="That address is not allowed."), 400
    if not dl.rate_ok(dl.client_ip(request)):
        return jsonify(error="You have reached the hourly limit (%d). Please try again later." % dl.JOBS_PER_HOUR), 429
    kind = d.get("kind") if d.get("kind") in ("auto", "video", "audio", "image") else "auto"
    quality = d.get("quality") if d.get("quality") in dl.QUALITIES else "best"
    o = {"kind": kind, "quality": quality, "playlist": bool(d.get("playlist"))}
    return jsonify(id=dl.new_job(urls, o, JOBS_DIR))


@app.route("/api/status/<job_id>")
def api_status(job_id):
    with dl.LOCK:
        job = dl.JOBS.get(job_id)
        if not job:
            return jsonify(error="This download has expired. Files are removed after %d minutes. Please start again."
                                 % dl.KEEP_MINUTES, expired=True), 404
        return jsonify({k: job[k] for k in ("id", "stage", "progress", "message", "done", "error", "files", "zip_url")})


@app.route("/api/file/<job_id>/<path:rel>")
def api_file(job_id, rel):
    if not re.match(r"^[0-9a-f]{16}$", job_id):
        abort(404)
    base = (JOBS_DIR / job_id / "dl").resolve()
    p = (base / rel).resolve()
    if base not in p.parents or not p.is_file() or not dl.kind_of(p):
        abort(404)
    return send_file(p, as_attachment=request.args.get("dl") == "1", download_name=p.name, conditional=True)


@app.route("/api/zip/<job_id>")
def api_zip(job_id):
    if not re.match(r"^[0-9a-f]{16}$", job_id):
        abort(404)
    p = JOBS_DIR / job_id / "all_files.zip"
    if not p.exists():
        abort(404)
    return send_file(p, mimetype="application/zip", as_attachment=True, download_name="MUJTABA_Downloads.zip")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "7860")), threaded=True)
