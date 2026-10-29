"""
MUJTABA Downloads - download engine.

Videos / MP3 : yt-dlp      (YouTube, Instagram, TikTok, Facebook, X, Reddit, Vimeo ... 1000+ sites)
Pictures     : gallery-dl  (Pinterest, Instagram, X, Pixiv, Flickr, Imgur ... many sites)
"""
import ipaddress
import os
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from pathlib import Path
from urllib.parse import urlparse

URL_RE = re.compile(r"^https?://[^\s]+$", re.I)
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif", ".heic"}
VID_EXT = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".flv", ".ts"}
AUD_EXT = {".mp3", ".m4a", ".opus", ".ogg", ".wav", ".aac", ".flac"}
IMAGE_HOSTS = ("pinterest.", "pin.it", "pixiv.net", "flickr.com", "deviantart.com", "imgur.com", "unsplash.com",
               "picsart.com", "artstation.com", "behance.net", "500px.com", "tumblr.com", "wallhaven.cc")
QUALITIES = {"best", "1080", "720", "480", "360"}

# ---- limits (override with environment variables) -------------------------------------------------------
MAX_URLS = int(os.environ.get("MAX_URLS", "5"))                 # links per request
MAX_FILE_MB = int(os.environ.get("MAX_FILE_MB", "700"))         # per video
JOB_TIMEOUT = int(os.environ.get("JOB_TIMEOUT", "900"))         # seconds per link
KEEP_MINUTES = int(os.environ.get("KEEP_MINUTES", "30"))        # files are deleted after this
MAX_PARALLEL = int(os.environ.get("MAX_PARALLEL", "3"))         # downloads running at the same time
JOBS_PER_HOUR = int(os.environ.get("JOBS_PER_HOUR", "20"))      # per visitor
PROXY_HOPS = int(os.environ.get("PROXY_HOPS", "1"))             # reverse proxies in front of the app
ALLOW_PRIVATE = os.environ.get("ALLOW_PRIVATE") == "1"          # testing only
COOKIES_FILE = None

JOBS = {}
LOCK = threading.Lock()
SLOTS = threading.BoundedSemaphore(MAX_PARALLEL)
HITS = {}


def setup(base):
    """Prepare folders and the optional cookies file. Returns the jobs folder."""
    global COOKIES_FILE
    jobs = Path(os.environ.get("JOBS_DIR", str(base / "jobs")))
    jobs.mkdir(parents=True, exist_ok=True)
    cookies = os.environ.get("YT_COOKIES", "").strip()  # optional secret: Netscape cookies.txt content
    if cookies:
        p = jobs.parent / ".cookies.txt"
        p.write_text(cookies + "\n", encoding="utf-8")
        try:
            p.chmod(0o600)
        except OSError:
            pass
        COOKIES_FILE = str(p)
    threading.Thread(target=_janitor, args=(jobs,), daemon=True).start()
    return jobs


def _janitor(jobs):
    import shutil
    while True:
        time.sleep(60)
        cutoff = time.time() - KEEP_MINUTES * 60
        try:
            for d in jobs.iterdir():
                if d.is_dir() and d.stat().st_mtime < cutoff:
                    with LOCK:
                        j = JOBS.get(d.name)
                        if j and not (j["done"] or j["error"]):
                            continue
                        JOBS.pop(d.name, None)
                    shutil.rmtree(d, ignore_errors=True)
            now = time.time()
            with LOCK:
                for ip in list(HITS):
                    HITS[ip] = [t for t in HITS[ip] if now - t < 3600]
                    if not HITS[ip]:
                        del HITS[ip]
        except Exception:
            pass


def client_ip(req):
    xff = [x.strip() for x in req.headers.get("X-Forwarded-For", "").split(",") if x.strip()]
    if xff and len(xff) >= PROXY_HOPS:
        return xff[-PROXY_HOPS]
    return req.remote_addr or "?"


def rate_ok(ip):
    now = time.time()
    with LOCK:
        hits = [t for t in HITS.get(ip, []) if now - t < 3600]
        if len(hits) >= JOBS_PER_HOUR:
            HITS[ip] = hits
            return False
        hits.append(now)
        HITS[ip] = hits
        return True


def safe_host(url):
    """Refuse links that point at private/internal addresses (protects the server)."""
    if ALLOW_PRIVATE:
        return True
    host = urlparse(url).hostname
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return True  # unresolvable here; the downloader will report it
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global:
            return False
    return True


def classify(text):
    low = text.lower()
    if "drm" in low and "protected" in low:
        return "This video is DRM-protected. Such videos cannot be downloaded."
    if "unsupported url" in low:
        return "This link is not supported, or it is a page and not a post. Open the video or picture itself and copy that link."
    if "larger than max-filesize" in low or "max-filesize" in low:
        return "This file is too big for the free online version (limit %d MB). Try a lower quality." % MAX_FILE_MB
    if any(k in low for k in ("sign in to confirm", "confirm you", "not a bot")):
        return ("YouTube is blocking the server from anonymous downloads right now (it does this to cloud servers). "
                "Try again later, or try a link from another platform.")
    if any(k in low for k in ("login required", "log in", "sign in", "private", "cookies", "checkpoint", "age-restricted",
                              "age restricted", "confirm your age", "not available in your country", "authentication",
                              "empty media response", "rate-limit", "rate limit", "restricted video")):
        return ("This content needs a login or is restricted (private account, age/country limit, or the platform "
                "blocked anonymous access). Private content cannot be downloaded here.")
    if any(k in low for k in ("http error 403", "http error 429", "too many requests", "forbidden")):
        return "The platform refused or rate-limited the request (HTTP 403/429). Wait a few minutes and try again."
    if any(k in low for k in ("unable to extract", "extractor error", "unable to download webpage", "please report",
                              "unexpected response", "nsig", "player response")):
        return "The platform changed its site. The downloader updates itself on every restart; try again later."
    if "no video" in low or "there is no video" in low:
        return "This post has no video."
    if "timed out" in low or "timeout" in low:
        return "The download took too long and was stopped."
    return "The download failed. Details are shown below; the platform may be blocking downloads."


def kind_of(path):
    ext = Path(path).suffix.lower()
    if ext in IMG_EXT:
        return "image"
    if ext in VID_EXT:
        return "video"
    if ext in AUD_EXT:
        return "audio"
    return None


def collect(outdir):
    return [p for p in sorted(outdir.rglob("*")) if p.is_file() and kind_of(p)]


def update(job, **kw):
    with LOCK:
        job.update(kw)


def ytdlp_cmd(url, o, outdir):
    cmd = [sys.executable, "-m", "yt_dlp", "--newline", "--no-colors", "--ignore-config", "-R", "3",
           "--fragment-retries", "5", "--socket-timeout", "30", "--windows-filenames", "--no-exec",
           "--max-filesize", "%dM" % MAX_FILE_MB, "-o", str(outdir / "%(title).100B [%(id)s].%(ext)s")]
    if o["kind"] == "audio":
        cmd += ["-x", "--audio-format", "mp3", "--audio-quality", "0"]
    else:
        h = o["quality"]
        cmd += ["-f", "bv*+ba/b" if h == "best" else "bv*[height<=?%s]+ba/b[height<=?%s]" % (h, h)]
        cmd += ["-S", "vcodec:h264,res,acodec:m4a", "--merge-output-format", "mp4"]
    cmd += ["--yes-playlist", "--playlist-end", "20"] if o["playlist"] else ["--no-playlist"]
    if COOKIES_FILE:
        cmd += ["--cookies", COOKIES_FILE]
    return cmd + ["--", url]


def gallerydl_cmd(url, o, outdir):
    return [sys.executable, "-m", "gallery_dl", "-d", str(outdir), "--range", "1-50", "--", url]


def run_cmd(job, cmd, base, span, label, tail):
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace")
    timer = threading.Timer(JOB_TIMEOUT, lambda: proc.kill())
    timer.start()
    count = 0
    try:
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            tail.append(line)
            del tail[:-40]
            m = re.search(r"\[download\]\s+(\d+(?:\.\d+)?)%", line)
            if m:
                update(job, progress=round(base + span * float(m.group(1)) / 100.0, 1), stage="Downloading",
                       message="%s - %s" % (label, line.replace("[download]", "").strip()[:110]))
            elif label.startswith("Pictures") and (line.startswith(("#", ".", "/")) or re.search(r"[\\/].+\.\w{3,4}$", line)):
                count += 1
                update(job, progress=round(base + span * min(0.95, count / 20.0), 1), stage="Downloading pictures",
                       message="%s - %d picture(s) so far" % (label, count))
            elif line.startswith("[") and "download" not in line.lower():
                update(job, stage="Preparing", message="%s - %s" % (label, line[:110]))
        proc.wait()
    finally:
        timer.cancel()
        if proc.poll() is None:
            proc.kill()
    if proc.returncode in (-9, 137):
        tail.append("timed out: the download was stopped after %d minutes" % (JOB_TIMEOUT // 60))
    return proc.returncode


def run_job(job, urls, o, outdir):
    with SLOTS:
        try:
            update(job, stage="Starting", message="")
            n = len(urls)
            errors = []
            for i, url in enumerate(urls):
                base, span = 100.0 * i / n, 100.0 / n
                host_name = (urlparse(url).hostname or "").lower()
                prefer_images = o["kind"] == "image" or (o["kind"] == "auto" and any(h in host_name for h in IMAGE_HOSTS))
                order = ["gallery", "yt"] if prefer_images else ["yt", "gallery"]
                if o["kind"] in ("video", "audio"):
                    order = ["yt"]
                before = len(collect(outdir))
                tail = []
                label = "Link %d of %d" % (i + 1, n)
                for engine in order:
                    update(job, stage="Starting", message="%s - trying the %s engine..." % (
                        label, "picture" if engine == "gallery" else "video"))
                    cmd = gallerydl_cmd(url, o, outdir) if engine == "gallery" else ytdlp_cmd(url, o, outdir)
                    rc = run_cmd(job, cmd, base, span, ("Pictures: " if engine == "gallery" else "") + label, tail)
                    if rc == 0 and len(collect(outdir)) > before:
                        break
                if len(collect(outdir)) <= before:
                    errors.append((url, "\n".join(tail)))
            files = collect(outdir)
            if not files:
                why, detail = "Nothing was downloaded.", ""
                if errors:
                    why = classify(errors[-1][1])
                    detail = "\n".join(errors[-1][1].splitlines()[-6:])
                raise RuntimeError(why + ("\n\n" + detail if detail else ""))
            items = []
            for p in files:
                rel = p.relative_to(outdir).as_posix()
                items.append({"name": p.name, "kind": kind_of(p), "size": p.stat().st_size,
                              "url": "/api/file/%s/%s" % (job["id"], rel)})
            zip_url = None
            if len(files) > 1:
                with zipfile.ZipFile(outdir.parent / "all_files.zip", "w", zipfile.ZIP_STORED) as z:
                    for p in files:
                        z.write(p, p.relative_to(outdir).as_posix())
                zip_url = "/api/zip/%s" % job["id"]
            note = ("%d of %d link(s) could not be downloaded." % (len(errors), n)) if errors else ""
            update(job, stage="Done", progress=100, done=True, files=items, zip_url=zip_url, message=note)
        except Exception as e:  # noqa
            update(job, error=str(e) or e.__class__.__name__, stage="Failed")


def new_job(urls, o, jobs_dir):
    job_id = uuid.uuid4().hex[:16]
    outdir = jobs_dir / job_id / "dl"
    outdir.mkdir(parents=True)
    job = {"id": job_id, "stage": "Queued", "progress": 0, "message": "Waiting for a free slot...",
           "done": False, "error": None, "files": [], "zip_url": None}
    with LOCK:
        JOBS[job_id] = job
    threading.Thread(target=run_job, args=(job, urls, o, outdir), daemon=True).start()
    return job_id
