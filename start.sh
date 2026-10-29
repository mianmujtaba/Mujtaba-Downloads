#!/usr/bin/env bash
# Update the download engines on every start (platforms change often), then run the web server.
pip install --no-cache-dir -q -U "yt-dlp[default]" gallery-dl 2>/dev/null || true
exec gunicorn app:app --bind 0.0.0.0:${PORT:-7860} --workers 1 --threads 24 --timeout 120 --access-logfile -
