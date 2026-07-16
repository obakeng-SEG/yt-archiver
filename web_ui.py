#!/usr/bin/env python3
"""Local one-page web UI for the YouTube audio archiver."""

from __future__ import annotations

import argparse
import hmac
import html
import json
import logging
import os
import secrets
import shlex
import shutil
import subprocess
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import yt_archive
from monitor import ChannelMonitor, MonitorConfig, Channel
from webhook import WebhookManager
from telegram_monitor import TelegramMonitor, TelegramConfig, load_telegram_config, save_telegram_config

LOG = logging.getLogger("web_ui")


ALLOWED_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
}
MAX_LOG_LINES = 500
MAX_URLS = 50
MAX_BODY_SIZE = 1024 * 100
DEFAULT_PORT = 8765


class ArchiveJob:
    def __init__(self, command: list[str], output_dir: str, download_archive: str = "archive/downloaded.txt", url: str = "", name: str = "", args: argparse.Namespace | None = None):
        self.command = command
        self.output_dir = output_dir
        self.download_archive = download_archive
        self.args = args
        self.url = url
        self.name = name
        self.started_at = time.time()
        self.finished_at: float | None = None
        self.returncode: int | None = None
        self.error: str | None = None
        self._log_lines: list[str] = []
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None

    @property
    def running(self) -> bool:
        return self.finished_at is None

    def append_log(self, line: str) -> None:
        with self._lock:
            self._log_lines.append(line.rstrip())
            if len(self._log_lines) > MAX_LOG_LINES:
                self._log_lines = self._log_lines[-MAX_LOG_LINES:]

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            log_lines = list(self._log_lines)

        return {
            "running": self.running,
            "returncode": self.returncode,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "output_dir": self.output_dir,
            "command": shlex.join(self.command),
            "url": self.url,
            "name": self.name,
            "log": "\n".join(log_lines),
        }


class DownloadQueue:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queue: list[ArchiveJob] = []
        self._current: ArchiveJob | None = None
        self._history: list[dict] = []
        self._history_file = Path("download_history.json")
        self._load_history()
        self._quiet_start: int = 23  # hour (0-23)
        self._quiet_end: int = 7    # hour (0-23)
        self._quiet_enabled: bool = False
        self._worker_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def _load_history(self):
        if self._history_file.exists():
            try:
                self._history = json.loads(self._history_file.read_text())
            except Exception as e:
                LOG.warning(f"Could not load history: {e}")
                self._history = []

    def _save_history(self):
        self._history_file.write_text(json.dumps(self._history[-500:], indent=2))

    def _is_quiet_hours(self) -> bool:
        if not self._quiet_enabled:
            return False
        hour = time.localtime().tm_hour
        if self._quiet_start > self._quiet_end:
            return hour >= self._quiet_start or hour < self._quiet_end
        return self._quiet_start <= hour < self._quiet_end

    def set_quiet_hours(self, start: int, end: int, enabled: bool):
        with self._lock:
            self._quiet_start = max(0, min(23, start))
            self._quiet_end = max(0, min(23, end))
            self._quiet_enabled = enabled

    def get_quiet_hours(self) -> dict:
        with self._lock:
            return {
                "start": self._quiet_start,
                "end": self._quiet_end,
                "enabled": self._quiet_enabled,
            }

    def enqueue(self, args: argparse.Namespace, url: str = "", name: str = "") -> ArchiveJob:
        command = yt_archive.build_ytdlp_command(args)
        job = ArchiveJob(command, args.output_dir, download_archive=args.download_archive, url=url, name=name, args=args)
        with self._lock:
            self._queue.append(job)
        self._ensure_worker()
        return job

    def _ensure_worker(self):
        with self._lock:
            if self._worker_thread and self._worker_thread.is_alive():
                return
            self._stop_event.clear()
            self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
            self._worker_thread.start()

    def _worker_loop(self):
        while not self._stop_event.is_set():
            if self._is_quiet_hours():
                self._stop_event.wait(30)
                continue
            job = self._next_job()
            if job is None:
                self._stop_event.wait(2)
                continue
            self._run_job(job)

    def _next_job(self) -> ArchiveJob | None:
        with self._lock:
            if self._current and self._current.running:
                return None
            if self._queue:
                return self._queue.pop(0)
            return None

    def stop_current(self) -> bool:
        with self._lock:
            job = self._current
        if job is None or not job.running:
            return False
        proc = job._process
        if proc is None:
            return False
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            return True
        except Exception:
            return False

    def cancel_queue_item(self, index: int) -> bool:
        with self._lock:
            if 0 <= index < len(self._queue):
                self._queue.pop(index)
                return True
            return False

    def clear_queue(self):
        with self._lock:
            self._queue.clear()

    def current(self) -> ArchiveJob | None:
        with self._lock:
            return self._current

    def queue_snapshot(self) -> list[dict]:
        with self._lock:
            return [{"name": j.name, "url": j.url, "output_dir": j.output_dir} for j in self._queue]

    def history_snapshot(self) -> list[dict]:
        with self._lock:
            return list(self._history[-50:])

    def last_output_dir(self) -> str | None:
        with self._lock:
            for record in reversed(self._history):
                args = record.get("args")
                if args and args.get("output_dir"):
                    return args["output_dir"]
            return None

    def stats(self) -> dict:
        with self._lock:
            queue_len = len(self._queue)
            running = self._current is not None and self._current.running
            return {
                "queue_length": queue_len,
                "running": running,
                "history_count": len(self._history),
            }

    def scan_missing(self, output_dir: str = "archive", archive_file: str = "archive/downloaded.txt") -> list[str]:
        """Scan archive file and check if files exist on disk. Returns list of missing video IDs."""
        archive_path = Path(archive_file).expanduser()
        out_path = Path(output_dir).expanduser()
        if not archive_path.exists():
            return []

        # Build file index once for O(1) lookups
        file_index: set[str] = set()
        if out_path.exists():
            for p in out_path.rglob("*"):
                if p.is_file():
                    file_index.add(p.name)

        missing = []
        with open(archive_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line or not line.startswith("youtube "):
                    continue
                vid = line.split(" ", 1)[1]
                if not any(vid in fname for fname in file_index):
                    missing.append(vid)
        return missing

    def clean_archive(self, missing_ids: list[str], archive_file: str = "archive/downloaded.txt") -> int:
        """Remove missing video IDs from archive file atomically. Returns count removed."""
        import tempfile
        archive_path = Path(archive_file).expanduser()
        if not archive_path.exists():
            return 0

        missing_set = set(missing_ids)
        lines = archive_path.read_text().splitlines()
        kept = []
        removed = 0
        for line in lines:
            if line.strip().startswith("youtube "):
                vid = line.strip().split(" ", 1)[1]
                if vid in missing_set:
                    removed += 1
                    continue
            kept.append(line)

        # Atomic write via temp file
        tmp_fd, tmp_path = tempfile.mkstemp(dir=archive_path.parent, suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w") as tmp:
                tmp.write("\n".join(kept) + "\n" if kept else "")
            os.replace(tmp_path, archive_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        return removed

    def rescan_and_queue(self, output_dir: str = "archive", archive_file: str = "archive/downloaded.txt") -> int:
        """Scan for missing files, clean archive, queue re-downloads. Returns count of items re-queued."""
        missing = self.scan_missing(output_dir, archive_file)
        if not missing:
            return 0
        self.clean_archive(missing, archive_file)
        # Queue re-downloads for each missing video
        for vid in missing:
            url = f"https://www.youtube.com/watch?v={vid}"
            args = argparse.Namespace(
                urls=[url],
                output_dir=output_dir,
                audio_format="mp3",
                audio_bitrate="192k",
                audio_quality="0",
                download_archive=archive_file,
                limit=None,
                playlist_items=None,
                no_sidecar_metadata=False,
                dry_run=False,
                yt_dlp="yt-dlp",
            )
            self.enqueue(args, url=url, name=f"Rescan: {vid}")
        return len(missing)

    def _run_job(self, job: ArchiveJob) -> None:
        with self._lock:
            self._current = job
        try:
            # Use yt-dlp path from the command itself (first element)
            yt_dlp_path = job.command[0] if job.command else yt_archive.YT_DLP_PATH
            yt_archive.ensure_runtime_dependencies(yt_dlp_path)
            output_dir = Path(job.output_dir).expanduser()
            archive_path = yt_archive.resolve_archive_path(output_dir, job.download_archive)
            output_dir.mkdir(parents=True, exist_ok=True)
            archive_path.parent.mkdir(parents=True, exist_ok=True)

            job.append_log(f"$ {shlex.join(job.command)}")
            process = subprocess.Popen(
                job.command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            job._process = process

            assert process.stdout is not None
            for line in process.stdout:
                job.append_log(line)

            while True:
                ret = process.poll()
                if ret is not None:
                    job.returncode = ret
                    break
                time.sleep(0.25)

            if job.returncode == 0:
                job.append_log("\nArchive finished successfully.")
            else:
                job.append_log(f"\nArchive exited with status {job.returncode}.")
        except Exception as exc:
            job.error = str(exc)
            job.append_log(f"\nError: {exc}")
        finally:
            job.finished_at = time.time()
            job_args = vars(job.args) if job.args else None
            record = {
                "name": job.name,
                "url": job.url,
                "started_at": job.started_at,
                "finished_at": job.finished_at,
                "returncode": job.returncode,
                "error": job.error,
                "args": job_args,
            }
            with self._lock:
                self._history.append(record)
                self._save_history()
                self._current = None

            # Send webhook notification
            wh = _get_webhook()
            if wh:
                event = "download_complete" if job.returncode == 0 else "download_failed"
                wh.notify(event, record)


def split_urls(raw_urls: str) -> list[str]:
    urls = [line.strip() for line in raw_urls.splitlines() if line.strip()]
    if not urls:
        raise ValueError("Add at least one YouTube URL.")
    if len(urls) > MAX_URLS:
        raise ValueError(f"Add {MAX_URLS} URLs or fewer.")

    for url in urls:
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        if parsed.scheme not in {"http", "https"} or host not in ALLOWED_HOSTS:
            raise ValueError(f"Only YouTube URLs are supported: {url}")

    return urls


def safe_path(value: str, label: str) -> str:
    check = Path(value).expanduser()
    if ".." in check.parts:
        raise ValueError(f"{label} must not contain '..' segments.")
    return str(check)


def form_to_archive_args(form: dict[str, list[str]]) -> argparse.Namespace:
    output_dir = safe_path(first_value(form, "output_dir", "archive").strip() or "archive", "Output directory")
    archive_file = safe_path(first_value(form, "download_archive", "downloaded.txt").strip() or "downloaded.txt", "Download archive")
    audio_fmt = first_value(form, "audio_format", "mp3").strip() or "mp3"
    audio_br = first_value(form, "audio_bitrate", "").strip() or None
    audio_quality = yt_archive.mp3_quality(first_value(form, "audio_quality", "0").strip() or "0")
    limit_value = first_value(form, "limit", "").strip()
    limit = yt_archive.positive_int(limit_value) if limit_value else None
    playlist_items = first_value(form, "playlist_items", "").strip() or None

    normalize = first_value(form, "normalize", "off").strip() or "off"
    normalize_target = -16.0
    if normalize == "ebu":
        normalize = "ebu"
        normalize_target = -16.0
    elif normalize == "ebu-broadcast":
        normalize = "ebu"
        normalize_target = -23.0
    elif normalize == "peak":
        normalize = "peak"
        normalize_target = -1.5
    else:
        normalize = "off"

    return SimpleNamespace(
        urls=split_urls(first_value(form, "urls", "")),
        output_dir=output_dir,
        audio_format=yt_archive.audio_format(audio_fmt),
        audio_bitrate=yt_archive.audio_bitrate(audio_br) if audio_br else None,
        audio_quality=audio_quality,
        download_archive=archive_file,
        limit=limit,
        playlist_items=playlist_items,
        no_sidecar_metadata="no_sidecar_metadata" in form,
        dry_run=False,
        yt_dlp="yt-dlp",
        normalize=normalize,
        normalize_target=normalize_target,
    )


def first_value(form: dict[str, list[str]], name: str, default: str) -> str:
    values = form.get(name)
    if not values:
        return default
    return values[0]


def list_archive_files(output_dir: str) -> list[dict[str, object]]:
    root = Path(output_dir).expanduser()
    files: list[dict[str, object]] = []
    if not root.exists():
        return files
    exts = ("*.mp3", "*.m4a", "*.opus", "*.flac", "*.wav", "*.vorbis")
    seen: set[Path] = set()
    for ext in exts:
        for p in sorted(root.rglob(ext)):
            if p in seen:
                continue
            seen.add(p)
            try:
                files.append({"path": str(p.relative_to(root)), "size": p.stat().st_size})
            except OSError:
                continue
    return files


class NormalizeWorker:
    def __init__(self):
        self._thread: threading.Thread | None = None
        self._running = False
        self._stop_requested = False
        self._progress = ""
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    @property
    def progress(self) -> str:
        with self._lock:
            return self._progress

    def start(self, files: list[str], norm_type: str, target: float, output_dir: str) -> None:
        with self._lock:
            if self._running:
                return False
            self._running = True
            self._stop_requested = False
            self._progress = f"0/{len(files)}"
        self._thread = threading.Thread(target=self._run, args=(files, norm_type, target, output_dir), daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        with self._lock:
            self._stop_requested = True

    def _run(self, files: list[str], norm_type: str, target: float, output_dir: str) -> None:
        out = Path(output_dir).expanduser()
        out.mkdir(parents=True, exist_ok=True)
        total = len(files)
        succeeded = 0
        failed = 0
        for i, filepath in enumerate(files):
            with self._lock:
                if self._stop_requested:
                    self._progress = f"Stopped ({succeeded} done, {failed} failed)"
                    self._running = False
                    return
                self._progress = f"{i}/{total}"
            try:
                p = Path(filepath)
                dest = out / p.name
                if norm_type == "ebu":
                    af = f"loudnorm=I={target}:TP=-1.5:LRA=11"
                elif norm_type == "peak":
                    af = f"loudnorm=I=-24:TP={target}:LRA=7"
                elif norm_type == "rms":
                    af = f"loudnorm=I={target}:TP=-1.5:LRA=7"
                else:
                    af = None
                if af:
                    cmd = ["ffmpeg", "-y", "-i", str(p), "-af", af, str(dest)]
                else:
                    cmd = ["ffmpeg", "-y", "-i", str(p), "-c", "copy", str(dest)]
                subprocess.run(cmd, check=True, capture_output=True)
                succeeded += 1
            except Exception:
                failed += 1
        with self._lock:
            self._progress = f"Done ({succeeded} ok, {failed} failed)"
            self._running = False


_normalize_worker = NormalizeWorker()


def get_storage_stats(output_dir: str) -> dict[str, object]:
    root = Path(output_dir).expanduser()
    if not root.exists():
        return {"total_size": 0, "file_count": 0, "dir_count": 0}
    total_size = 0
    file_count = 0
    dir_count = 0
    for p in root.rglob("*"):
        if p.is_file():
            try:
                total_size += p.stat().st_size
                file_count += 1
            except OSError:
                pass
        elif p.is_dir():
            dir_count += 1
    return {"total_size": total_size, "file_count": file_count, "dir_count": dir_count}


def browse_filesystem(path: str) -> dict[str, object]:
    target = Path(path).expanduser().resolve()
    if not target.exists():
        return {"path": str(target), "exists": False, "dirs": [], "parent": None}
    if target.is_file():
        target = target.parent
    dirs: list[dict[str, str]] = []
    try:
        for entry in sorted(target.iterdir()):
            if entry.is_dir() and not entry.name.startswith("."):
                dirs.append({"name": entry.name, "path": str(entry)})
    except PermissionError:
        pass
    parent = str(target.parent) if target != target.parent else None
    return {"path": str(target), "exists": True, "dirs": dirs, "parent": parent}


def fetch_playlist_items(url: str) -> list[dict[str, str]]:
    """Fetch items from a YouTube playlist/URL."""
    try:
        result = subprocess.run(
            [yt_archive.YT_DLP_PATH, "--flat-playlist", "--dump-json", "--no-warnings", url],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return []
        items: list[dict[str, str]] = []
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            try:
                data = json.loads(line)
                items.append({
                    "id": data.get("id", ""),
                    "title": data.get("title", "Unknown"),
                    "url": data.get("url", data.get("webpage_url", "")),
                    "duration": str(data.get("duration", "")),
                })
            except json.JSONDecodeError:
                continue
        return items
    except Exception:
        return []


PAGE_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>yt-archiver</title>
  <style>
    :root {
      --bg: #f3f0eb;
      --bg-subtle: #eae5de;
      --surface: #ffffff;
      --surface-2: #faf8f5;
      --surface-3: #e8e4de;
      --border: #d6d0c8;
      --border-subtle: #e2ddd6;
      --text: #1a1714;
      --text-2: #6b6560;
      --text-3: #9a948e;
      --red: #c53d2e;
      --red-hover: #a8322a;
      --red-dim: rgba(197,61,46,.08);
      --red-glow: rgba(197,61,46,.12);
      --green: #1a7a5a;
      --green-dim: rgba(26,122,90,.08);
      --green-glow: rgba(26,122,90,.12);
      --amber: #b57d2a;
      --amber-dim: rgba(181,125,42,.08);
      --yellow: #8a7a20;
      --yellow-dim: rgba(138,122,32,.08);
      --blue: #2a6db5;
      --blue-dim: rgba(42,109,181,.08);
      --purple: #7a2a9b;
      --purple-dim: rgba(122,42,155,.08);
      --mono: "SF Mono", "Cascadia Code", "Fira Code", "JetBrains Mono", ui-monospace, Menlo, Consolas, monospace;
      --sans: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
      --radius: 10px;
      --radius-sm: 6px;
      --shadow: 0 1px 2px rgba(0,0,0,.04), 0 4px 12px rgba(0,0,0,.06);
      --shadow-lg: 0 2px 4px rgba(0,0,0,.04), 0 12px 32px rgba(0,0,0,.08);
    }
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      min-height: 100vh;
      background: var(--bg);
      color: var(--text);
      font-family: var(--sans);
      font-size: 14px;
      line-height: 1.5;
      -webkit-font-smoothing: antialiased;
    }
    .app { max-width: 1320px; margin: 0 auto; padding: 28px 32px 64px; }

    /* Header */
    .header {
      display: flex; align-items: center; justify-content: space-between;
      padding-bottom: 20px; border-bottom: 1px solid var(--border-subtle); margin-bottom: 24px;
    }
    .header-left { display: flex; align-items: center; gap: 14px; }
    .logo {
      width: 40px; height: 40px; background: var(--red); border-radius: 10px;
      display: grid; place-items: center; font-size: 16px; flex-shrink: 0;
      box-shadow: 0 2px 8px rgba(197,61,46,.2);
    }
    .header h1 { font-size: 22px; font-weight: 750; letter-spacing: -0.02em; color: var(--text); }
    .header h1 span { color: var(--green); font-weight: 600; }
    .header-right {
      display: flex; align-items: center; gap: 7px; font-size: 12px; color: var(--text-2);
      padding: 6px 12px; background: var(--surface); border: 1px solid var(--border-subtle); border-radius: 999px;
    }
    .header-right #header-status { font-weight: 650; color: var(--text); }
    .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--surface-3); display: inline-block; transition: all .3s; }
    .dot.live { background: var(--green); box-shadow: 0 0 0 3px var(--green-dim); }

    /* Grid */
    .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; align-items: start; }
    .card {
      background: var(--surface); border: 1px solid var(--border-subtle);
      border-radius: var(--radius); overflow: hidden;
      box-shadow: var(--shadow);
    }
    .grid > .card:nth-child(1) { box-shadow: var(--shadow-lg); border-color: var(--border); }
    .card-accent-file, .card-accent-monitor, .card-accent-webhook,
    .card-accent-queue, .card-accent-history, .card-accent-storage { border-top: 1px solid var(--border-subtle); }
    .card-head {
      display: flex; align-items: center; justify-content: space-between;
      padding: 14px 18px; border-bottom: 1px solid var(--border-subtle); background: var(--surface-2);
    }
    .card-head h2 {
      display: flex; align-items: center; gap: 8px;
      font-size: 12px; font-weight: 750; text-transform: uppercase;
      letter-spacing: 0.06em; color: var(--text-2);
    }
    .card-head h2::before {
      content: ''; width: 6px; height: 6px; border-radius: 50%; background: var(--green);
    }
    .grid > .card:nth-child(1) .card-head h2::before { background: var(--red); }
    .grid > .card:nth-child(2) .card-head h2::before { background: var(--blue); }
    .card-accent-file .card-head h2::before { background: var(--blue); }
    .card-accent-monitor .card-head h2::before { background: var(--green); }
    .card-accent-webhook .card-head h2::before { background: var(--amber); }
    .card-accent-queue .card-head h2::before { background: var(--yellow); }
    .card-accent-history .card-head h2::before { background: var(--text-3); }
    .card-accent-normalize .card-head h2::before { background: var(--purple); }
    .card-accent-telegram .card-head h2::before { background: #0088cc; }
    .badge {
      display: inline-flex; align-items: center; gap: 6px;
      font-size: 10px; font-weight: 800; padding: 3px 8px;
      border-radius: 999px; text-transform: uppercase; letter-spacing: 0.05em;
    }
    .badge::before { content: ''; width: 5px; height: 5px; border-radius: 50%; background: currentColor; }
    .badge-idle { background: var(--surface-3); color: var(--text-3); }
    .badge-count { background: var(--amber-dim); color: var(--amber); }
    .badge-running { background: var(--blue-dim); color: var(--blue); }
    .badge-ok { background: var(--green-dim); color: var(--green); }
    .badge-fail { background: var(--red-dim); color: var(--red); }
    .card-body { padding: 18px; }
    .grid > .card:nth-child(1) .card-body,
    .grid > .card:nth-child(2) .card-body { padding: 20px; }

    /* Form */
    .field { margin-bottom: 16px; }
    .field:last-child { margin-bottom: 0; }
    label {
      display: block; font-size: 11px; font-weight: 700; color: var(--text-2);
      margin-bottom: 6px; text-transform: uppercase; letter-spacing: 0.05em;
    }
    textarea, input[type="text"], input[type="number"], select {
      width: 100%; background: var(--surface-2); border: 1px solid var(--border);
      border-radius: var(--radius-sm); color: var(--text); font-family: var(--mono);
      font-size: 13px; padding: 10px 12px; outline: none;
      transition: border-color .15s, box-shadow .15s;
    }
    textarea:focus, input:focus, select:focus {
      border-color: var(--blue); box-shadow: 0 0 0 3px var(--blue-dim);
    }
    textarea { min-height: 120px; resize: vertical; line-height: 1.6; }
    textarea::placeholder, input::placeholder { color: var(--text-3); }
    select {
      cursor: pointer; appearance: none;
      background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%239a948e' stroke-width='2'%3E%3Cpath d='M6 9l6 6 6-6'/%3E%3C/svg%3E");
      background-repeat: no-repeat; background-position: right 10px center; padding-right: 30px;
    }

    .row { display: grid; gap: 12px; }
    .row-2 { grid-template-columns: 1fr 1fr; }
    .row-3 { grid-template-columns: 1fr 1fr 1fr; }

    .input-with-btn { display: flex; gap: 6px; }
    .input-with-btn input { flex: 1; }
    .input-with-btn .btn-browse {
      padding: 0 14px; background: var(--surface-2); border: 1px solid var(--border);
      border-radius: var(--radius-sm); color: var(--text-2); cursor: pointer; font-size: 13px;
      font-family: var(--sans); white-space: nowrap; transition: all .15s;
    }
    .input-with-btn .btn-browse:hover { background: var(--surface-3); color: var(--text); }

    .check-row {
      display: flex; align-items: center; gap: 10px; padding: 9px 12px;
      border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); background: var(--surface-2);
    }
    .check-row input[type="checkbox"] { width: 16px; height: 16px; accent-color: var(--red); flex-shrink: 0; }
    .check-row label { margin: 0; font-size: 13px; color: var(--text-2); text-transform: none; letter-spacing: 0; cursor: pointer; }

    /* Buttons */
    .btn-row { display: flex; gap: 10px; margin-top: 18px; }
    .btn {
      display: inline-flex; align-items: center; justify-content: center; gap: 8px;
      padding: 12px 22px; border: none; border-radius: var(--radius-sm);
      font-family: var(--sans); font-size: 14px; font-weight: 600;
      cursor: pointer; transition: all .15s; flex: 1;
    }
    .btn-primary { background: var(--red); color: #fff; box-shadow: 0 1px 3px rgba(197,61,46,.2); }
    .btn-primary:hover { background: var(--red-hover); box-shadow: 0 2px 8px rgba(197,61,46,.25); }
    .btn-primary:active { transform: scale(0.98); }
    .btn-primary:disabled { background: var(--surface-3); color: var(--text-3); cursor: not-allowed; transform: none; box-shadow: none; }
    .btn-stop { background: var(--amber-dim); color: var(--amber); border: 1px solid rgba(181,125,42,.2); flex: 0 0 auto; }
    .btn-stop:hover { background: var(--amber); color: #fff; border-color: var(--amber); }
    .btn-stop:disabled { background: var(--surface-3); color: var(--text-3); border-color: var(--border); cursor: not-allowed; }

    /* Stats */
    .stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin-bottom: 14px; }
    .stat { background: var(--surface-2); border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); padding: 11px; }
    .stat-label { font-size: 10px; font-weight: 700; color: var(--text-3); text-transform: uppercase; letter-spacing: 0.05em; }
    .stat-value { font-family: var(--mono); font-size: 13px; font-weight: 600; color: var(--text); margin-top: 4px; overflow-wrap: anywhere; word-break: break-all; }

    /* Progress */
    .progress-wrap { margin-bottom: 14px; display: none; }
    .progress-wrap.active { display: block; }
    .progress-bar-bg { width: 100%; height: 5px; background: var(--surface-3); border-radius: 3px; overflow: hidden; }
    .progress-bar { height: 100%; width: 0%; background: var(--blue); border-radius: 3px; transition: width .3s ease; }
    .progress-meta { display: flex; justify-content: space-between; margin-top: 6px; font-size: 11px; color: var(--text-3); font-family: var(--mono); }

    .overall-wrap { margin-bottom: 14px; display: none; }
    .overall-wrap.active { display: block; }
    .overall-bar-bg { width: 100%; height: 8px; background: var(--surface-3); border-radius: 4px; overflow: hidden; }
    .overall-bar { height: 100%; width: 0%; background: var(--green); border-radius: 4px; transition: width .5s ease; }
    .overall-meta { display: flex; justify-content: space-between; margin-top: 6px; font-size: 12px; font-weight: 600; color: var(--text-2); font-family: var(--mono); }

    /* Log */
    .log-box {
      background: #1a1714; border: 1px solid #2a2520; border-radius: var(--radius-sm);
      padding: 14px; min-height: 300px; max-height: 420px; overflow-y: auto;
      font-family: var(--mono); font-size: 12px; line-height: 1.85; color: #b5afa8;
      white-space: pre-wrap; word-break: break-all; scroll-behavior: smooth;
    }
    .log-box::-webkit-scrollbar { width: 6px; }
    .log-box::-webkit-scrollbar-track { background: transparent; }
    .log-box::-webkit-scrollbar-thumb { background: #3a3530; border-radius: 3px; }

    .log-line-download { color: #6aabeb; }
    .log-line-extract { color: #5ec49a; }
    .log-line-meta { color: #d4a84a; }
    .log-line-error { color: #e06050; }
    .log-line-cmd { color: #e8e4de; font-weight: 600; }
    .log-line-done { color: #5ec49a; font-weight: 600; }

    .cmd-preview {
      margin-top: 12px; padding: 10px 12px; background: var(--surface-2); border: 1px solid var(--border-subtle);
      border-radius: var(--radius-sm); font-family: var(--mono); font-size: 11px; color: var(--text-3);
      overflow-wrap: anywhere; word-break: break-all; max-height: 40px; overflow: hidden;
      cursor: pointer; transition: max-height .3s, color .15s;
      position: relative;
    }
    .cmd-preview::after {
      content: ''; position: absolute; bottom: 0; left: 0; right: 0; height: 16px;
      background: linear-gradient(to bottom, transparent, var(--surface-2));
      pointer-events: none; transition: opacity .3s;
    }
    .cmd-preview:hover { max-height: 200px; color: var(--text-2); }
    .cmd-preview:hover::after { opacity: 0; }

    /* Files */
    .files-list { max-height: 260px; overflow-y: auto; font-family: var(--mono); font-size: 12px; }
    .files-list::-webkit-scrollbar { width: 6px; }
    .files-list::-webkit-scrollbar-track { background: transparent; }
    .files-list::-webkit-scrollbar-thumb { background: var(--surface-3); border-radius: 3px; }
    .file-item {
      display: flex; align-items: center; gap: 10px; padding: 9px 18px;
      border-bottom: 1px solid var(--border-subtle); color: var(--text-2);
      transition: background .1s;
    }
    .file-item:last-child { border-bottom: none; }
    .file-item:hover { background: var(--surface-2); }
    .file-icon { color: var(--blue); font-size: 10px; flex-shrink: 0; }
    .file-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; }
    .file-size { margin-left: auto; color: var(--text-3); flex-shrink: 0; font-size: 11px; }
    .files-empty { padding: 28px; text-align: center; color: var(--text-3); font-size: 13px; }

    /* Normalize file list */
    .norm-file-list { max-height: 200px; overflow-y: auto; font-family: var(--mono); font-size: 12px; margin-top: 12px; border: 1px solid var(--border); border-radius: var(--radius-sm); }
    .norm-file-list::-webkit-scrollbar { width: 6px; }
    .norm-file-list::-webkit-scrollbar-track { background: transparent; }
    .norm-file-list::-webkit-scrollbar-thumb { background: var(--surface-3); border-radius: 3px; }
    .telegram-files { max-height: 200px; overflow-y: auto; font-family: var(--mono); font-size: 12px; margin-top: 12px; border: 1px solid var(--border); border-radius: var(--radius-sm); }
    .telegram-files::-webkit-scrollbar { width: 6px; }
    .telegram-files::-webkit-scrollbar-track { background: transparent; }
    .telegram-files::-webkit-scrollbar-thumb { background: var(--surface-3); border-radius: 3px; }
    .telegram-channels { max-height: 150px; overflow-y: auto; font-family: var(--mono); font-size: 12px; margin-top: 8px; border: 1px solid var(--border); border-radius: var(--radius-sm); }
    .norm-file-item {
      display: flex; align-items: center; gap: 10px; padding: 8px 12px;
      border-bottom: 1px solid var(--border-subtle); color: var(--text-2);
    }
    .norm-file-item:last-child { border-bottom: none; }
    .norm-file-item input[type="checkbox"] { flex-shrink: 0; }
    .norm-file-item .file-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; }
    .norm-file-item .file-size { margin-left: auto; color: var(--text-3); flex-shrink: 0; font-size: 11px; }

    /* Playlist */
    .playlist-items { max-height: 240px; overflow-y: auto; border: 1px solid var(--border); border-radius: var(--radius-sm); }
    .playlist-items::-webkit-scrollbar { width: 6px; }
    .playlist-items::-webkit-scrollbar-track { background: transparent; }
    .playlist-items::-webkit-scrollbar-thumb { background: var(--surface-3); border-radius: 3px; }
    .pl-item {
      display: flex; align-items: center; gap: 10px; padding: 8px 12px;
      border-bottom: 1px solid var(--border-subtle); font-size: 13px; cursor: pointer;
      transition: background .1s;
    }
    .pl-item:last-child { border-bottom: none; }
    .pl-item:hover { background: var(--surface-2); }
    .pl-item input[type="checkbox"] { accent-color: var(--red); flex-shrink: 0; }
    .pl-item-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; }
    .pl-item-dur { color: var(--text-3); font-family: var(--mono); font-size: 11px; flex-shrink: 0; }
    .pl-actions { display: flex; gap: 6px; margin-top: 8px; }
    .pl-actions button {
      padding: 6px 12px; background: var(--surface-2); border: 1px solid var(--border);
      border-radius: 4px; color: var(--text-2); cursor: pointer; font-size: 12px;
      font-family: var(--sans); transition: all .15s;
    }
    .pl-actions button:hover { background: var(--surface-3); color: var(--text); }
    .pl-loading { padding: 16px; text-align: center; color: var(--text-3); font-size: 13px; }

    /* Modal */
    .modal-overlay {
      display: none; position: fixed; inset: 0; background: rgba(26,23,20,.4);
      z-index: 9000; place-items: center; backdrop-filter: blur(6px);
    }
    .modal-overlay.active { display: grid; }
    .modal {
      background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
      width: min(520px, 90vw); max-height: 70vh; display: flex; flex-direction: column;
      box-shadow: var(--shadow-lg);
    }
    .modal-head {
      display: flex; align-items: center; justify-content: space-between;
      padding: 16px 20px; border-bottom: 1px solid var(--border-subtle); background: var(--surface-2);
    }
    .modal-head h3 { font-size: 14px; font-weight: 600; }
    .modal-close {
      background: none; border: none; color: var(--text-3); cursor: pointer;
      font-size: 20px; padding: 0 4px; line-height: 1; transition: color .15s;
    }
    .modal-close:hover { color: var(--text); }
    .modal-body { padding: 20px; overflow-y: auto; flex: 1; }
    .modal-foot { padding: 14px 20px; border-top: 1px solid var(--border-subtle); display: flex; justify-content: flex-end; gap: 8px; }
    .modal-foot .btn { flex: 0 0 auto; padding: 8px 16px; font-size: 13px; }
    .btn-ghost { background: var(--surface-3); color: var(--text-2); }
    .btn-ghost:hover { background: var(--border); color: var(--text); }

    .dir-item {
      display: flex; align-items: center; gap: 8px; padding: 8px 10px;
      border-radius: 4px; cursor: pointer; font-size: 13px; transition: background .1s;
    }
    .dir-item:hover { background: var(--surface-2); }
    .dir-icon { color: var(--amber); font-size: 12px; }
    .dir-path { font-family: var(--mono); font-size: 12px; color: var(--text-3); margin-bottom: 12px; word-break: break-all; }

    /* Toast */
    .toast-bar { max-width: 600px; margin: 0 auto 16px; }
    .toast-bar .toast { animation: toastIn .25s ease; display: block; text-align: center; }
    .toast-container { position: fixed; top: 24px; right: 24px; z-index: 9999; display: flex; flex-direction: column; gap: 10px; }
    .toast {
      padding: 12px 18px; border-radius: 8px; font-size: 13px; font-weight: 500;
      color: #fff; box-shadow: 0 8px 24px rgba(0,0,0,.15);
      animation: toastIn .25s ease, toastOut .3s ease 3.7s forwards; max-width: 380px;
    }
    .toast-ok { background: #166534; border: 1px solid #22c55e; }
    .toast-err { background: #7f1d1d; border: 1px solid #ef4444; }
    .toast-info { background: #1e3a5f; border: 1px solid #3b82f6; }
    @keyframes toastIn { from { opacity: 0; transform: translateX(20px); } to { opacity: 1; transform: translateX(0); } }
    @keyframes toastOut { to { opacity: 0; transform: translateX(20px); } }

    .state-idle { color: var(--text-3); }
    .state-running { color: var(--blue); }
    .state-ok { color: var(--green); }
    .state-fail { color: var(--red); }

    .empty-state { color: var(--text-3); font-size: 13px; padding: 10px 0; }

    /* Channel Monitor */
    .channels-list { max-height: 260px; overflow-y: auto; }
    .channels-list::-webkit-scrollbar { width: 6px; }
    .channels-list::-webkit-scrollbar-track { background: transparent; }
    .channels-list::-webkit-scrollbar-thumb { background: var(--surface-3); border-radius: 3px; }
    .channel-item {
      display: flex; align-items: center; gap: 14px; padding: 14px 18px;
      border-bottom: 1px solid var(--border-subtle); transition: background .1s;
    }
    .channel-item:last-child { border-bottom: none; }
    .channel-item:hover { background: var(--surface-2); }
    .channel-info { flex: 1; min-width: 0; }
    .channel-name { font-weight: 600; font-size: 14px; color: var(--text); margin-bottom: 3px; }
    .channel-url { font-family: var(--mono); font-size: 11px; color: var(--text-3); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .channel-last { font-size: 11px; color: var(--text-3); margin-top: 4px; }
    .channel-actions { display: flex; gap: 8px; flex-shrink: 0; }
    .channel-actions button {
      padding: 6px 12px; background: var(--surface-2); border: 1px solid var(--border);
      border-radius: 4px; color: var(--text-2); cursor: pointer; font-size: 11px;
      font-family: var(--sans); transition: all .15s;
    }
    .channel-actions button:hover { background: var(--surface-3); color: var(--text); }
    .channel-actions .btn-toggle { min-width: 56px; }
    .channel-actions .btn-toggle.on { background: var(--green-dim); color: var(--green); border-color: rgba(26,122,90,.25); }
    .channel-actions .btn-schedule { background: var(--surface-2); color: var(--yellow); border-color: rgba(138,122,32,.25); }
    .channel-actions .btn-schedule:hover { background: var(--yellow-dim); }
    .channel-actions .btn-remove { background: var(--red-dim); color: var(--red); border-color: rgba(197,61,46,.25); }
    .channel-actions .btn-remove:hover { background: var(--red); color: #fff; border-color: var(--red); }
    .channels-empty { padding: 28px; text-align: center; color: var(--text-3); font-size: 13px; }
    .channel-schedule { font-size: 11px; color: var(--yellow); margin-top: 3px; }
    .channel-quality { font-size: 11px; color: var(--blue); margin-top: 2px; }
    .channel-actions .btn-quality { background: var(--surface-2); color: var(--blue); border-color: rgba(42,109,181,.25); }
    .channel-actions .btn-quality:hover { background: var(--blue-dim); }
    .webhook-list { max-height: 220px; overflow-y: auto; }
    .webhook-list::-webkit-scrollbar { width: 6px; }
    .webhook-list::-webkit-scrollbar-track { background: transparent; }
    .webhook-list::-webkit-scrollbar-thumb { background: var(--surface-3); border-radius: 3px; }
    .webhook-item { display: flex; align-items: center; justify-content: space-between; padding: 11px 18px; border-bottom: 1px solid var(--border-subtle); }
    .webhook-item:last-child { border-bottom: none; }
    .webhook-item:hover { background: var(--surface-2); }
    .webhook-info { flex: 1; min-width: 0; }
    .webhook-name { font-weight: 600; font-size: 13px; color: var(--text); }
    .webhook-url { font-family: var(--mono); font-size: 11px; color: var(--text-3); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .webhook-events { font-size: 11px; color: var(--text-3); margin-top: 2px; }
    .webhook-actions { flex-shrink: 0; }
    .webhook-actions button { padding: 6px 12px; background: var(--surface-2); border: 1px solid var(--border); border-radius: 4px; color: var(--red); cursor: pointer; font-size: 11px; font-family: var(--sans); }
    .webhook-actions button:hover { background: var(--red-dim); border-color: rgba(197,61,46,.25); }

    .monitor-bar {
      display: flex; align-items: center; gap: 10px; padding: 13px 18px;
      border-top: 1px solid var(--border-subtle); background: var(--surface-2);
    }
    .monitor-bar .monitor-status { font-size: 12px; color: var(--text-3); flex: 1; }
    .monitor-bar .monitor-status .dot { width: 6px; height: 6px; border-radius: 50%; background: var(--surface-3); display: inline-block; margin-right: 8px; }
    .monitor-bar .monitor-status .dot.on { background: var(--green); box-shadow: 0 0 0 3px var(--green-dim); }
    .monitor-bar button {
      padding: 7px 12px; border-radius: 999px; border: 1px solid var(--border);
      font-size: 12px; font-family: var(--sans); cursor: pointer; transition: all .15s;
    }
    .monitor-bar .btn-m-start { background: var(--green-dim); color: var(--green); border-color: rgba(26,122,90,.25); }
    .monitor-bar .btn-m-start:hover { background: var(--green); color: #fff; border-color: var(--green); }
    .monitor-bar .btn-m-stop { background: var(--red-dim); color: var(--red); border-color: rgba(197,61,46,.25); }
    .monitor-bar .btn-m-stop:hover { background: var(--red); color: #fff; border-color: var(--red); }

    .add-channel-row { display: flex; gap: 9px; padding: 14px 18px; border-bottom: 1px solid var(--border-subtle); }
    .add-channel-row input {
      flex: 1; padding: 9px 12px; background: var(--surface-2); border: 1px solid var(--border);
      border-radius: var(--radius-sm); color: var(--text); font-size: 12px; font-family: var(--mono);
      transition: border-color .15s;
    }
    .add-channel-row input:focus { outline: none; border-color: var(--blue); }
    .add-channel-row .btn-add {
      padding: 9px 16px; background: var(--green); color: #fff; border: none;
      border-radius: var(--radius-sm); font-size: 12px; font-family: var(--sans); cursor: pointer;
      font-weight: 600; white-space: nowrap; transition: all .15s;
    }
    .add-channel-row .btn-add:hover { background: #15604a; }

    /* Queue */
    .queue-list { max-height: 220px; overflow-y: auto; }
    .queue-list::-webkit-scrollbar { width: 6px; }
    .queue-list::-webkit-scrollbar-track { background: transparent; }
    .queue-list::-webkit-scrollbar-thumb { background: var(--surface-3); border-radius: 3px; }
    .queue-item {
      display: flex; align-items: center; gap: 12px; padding: 10px 18px;
      border-bottom: 1px solid var(--border-subtle); font-size: 13px; transition: background .15s;
    }
    .queue-item:hover { background: var(--surface-2); }
    .queue-item:last-child { border-bottom: none; }
    .queue-item-num { color: var(--text-3); font-family: var(--mono); font-size: 11px; width: 28px; flex-shrink: 0; }
    .queue-item-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .queue-item-cancel {
      padding: 4px 10px; background: var(--red-dim); border: 1px solid rgba(197,61,46,.25);
      border-radius: 4px; color: var(--red); cursor: pointer; font-size: 11px;
      font-family: var(--sans); transition: all .15s;
    }
    .queue-item-cancel:hover { background: var(--red); color: #fff; border-color: var(--red); }
    .queue-empty { padding: 24px; text-align: center; color: var(--text-3); font-size: 13px; }
    .queue-actions { display: flex; gap: 8px; padding: 12px 18px; border-top: 1px solid var(--border-subtle); }
    .queue-actions button {
      padding: 7px 14px; background: var(--surface-2); border: 1px solid var(--border);
      border-radius: 4px; color: var(--text-2); cursor: pointer; font-size: 12px;
      font-family: var(--sans); transition: all .15s;
    }
    .queue-actions button:hover { background: var(--surface-3); color: var(--text); }

    /* History */
    .history-list { max-height: 220px; overflow-y: auto; font-family: var(--mono); font-size: 12px; }
    .history-list::-webkit-scrollbar { width: 6px; }
    .history-list::-webkit-scrollbar-track { background: transparent; }
    .history-list::-webkit-scrollbar-thumb { background: var(--surface-3); border-radius: 3px; }
    .history-item {
      display: flex; align-items: center; gap: 10px; padding: 9px 18px;
      border-bottom: 1px solid var(--border-subtle); color: var(--text-2);
    }
    .history-item:last-child { border-bottom: none; }
    .history-dot { width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; }
    .history-dot.ok { background: var(--green); }
    .history-dot.fail { background: var(--red); }
    .history-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .history-time { color: var(--text-3); font-size: 11px; flex-shrink: 0; }
    .history-empty { padding: 24px; text-align: center; color: var(--text-3); font-size: 13px; }
    .history-retry { margin-left: 8px; padding: 2px 8px; background: var(--surface-2); border: 1px solid var(--border); border-radius: 3px; color: var(--amber); cursor: pointer; font-size: 10px; font-family: var(--sans); }
    .history-retry:hover { background: var(--amber-dim); border-color: rgba(181,125,42,.25); }

    /* Quiet Hours */
    .quiet-bar {
      display: flex; align-items: center; gap: 10px; padding: 13px 18px;
      border-top: 1px solid var(--border-subtle); background: var(--surface-2);
    }
    .quiet-bar label { font-size: 12px; color: var(--text-3); text-transform: none; letter-spacing: 0; margin: 0; }
    .quiet-bar input[type="number"] {
      width: 52px; padding: 5px 6px; background: var(--surface); border: 1px solid var(--border);
      border-radius: 4px; color: var(--text); font-family: var(--mono); font-size: 12px; text-align: center;
    }
    .quiet-bar input[type="checkbox"] { accent-color: var(--red); }

    /* Storage */
    .stats-row { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
    .stat-card { background: var(--surface-2); border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); padding: 16px; }
    .stat-card-label { font-size: 10px; font-weight: 700; color: var(--text-3); text-transform: uppercase; letter-spacing: 0.05em; }
    .stat-card-value { font-family: var(--mono); font-size: 20px; font-weight: 700; color: var(--text); margin-top: 6px; }
    .stat-card-sub { font-size: 11px; color: var(--text-3); margin-top: 2px; }

    @media (max-width: 860px) {
      .app { padding: 20px 16px 48px; }
      .grid { grid-template-columns: 1fr; }
      .row-2, .row-3 { grid-template-columns: 1fr; }
      .stats { grid-template-columns: 1fr 1fr; }
      .header { flex-direction: column; align-items: flex-start; gap: 12px; }
      .add-channel-row { flex-direction: column; }
      .monitor-bar { flex-wrap: wrap; }
    }
  </style>
</head>
<body>
  <div class="toast-container" id="toasts"></div>

  <div class="app">
    <div class="header">
      <div class="header-left">
        <div class="logo">&#9654;</div>
        <h1>yt-archiver <span>/ archive</span></h1>
      </div>
      <div class="header-right">
        <span class="dot" id="live-dot"></span>
        <span id="header-status">idle</span>
      </div>
    </div>

    <div id="error-message" class="toast-bar" style="display:__ERROR_STYLE__;">
      <span class="toast toast-err">__ERROR__</span>
    </div>
    <div id="info-message" class="toast-bar" style="display:__MESSAGE_STYLE__;">
      <span class="toast toast-info">__MESSAGE__</span>
    </div>

    <div class="grid">
      <!-- Left: Form -->
      <div class="card">
        <div class="card-head">
          <h2>Archive</h2>
          <span class="badge badge-idle" id="form-badge">ready</span>
        </div>
        <div class="card-body">
          <form method="post" action="/start" id="archive-form">
            <input type="hidden" name="csrf_token" value="__CSRF__">

            <div class="field">
              <label for="urls">YouTube URLs</label>
              <textarea id="urls" name="urls" required
                placeholder="Paste URLs here, one per line...&#10;&#10;https://www.youtube.com/watch?v=...&#10;https://www.youtube.com/playlist?list=...&#10;https://www.youtube.com/@channel/videos"></textarea>
            </div>

            <div class="field" id="playlist-section" style="display:none;">
              <label>Playlist Items</label>
              <div class="pl-loading" id="pl-loading">Loading playlist...</div>
              <div class="playlist-items" id="pl-items" style="display:none;"></div>
              <div class="pl-actions" id="pl-actions" style="display:none;">
                <button type="button" onclick="plSelectAll()">Select All</button>
                <button type="button" onclick="plSelectNone()">None</button>
                <button type="button" onclick="plInvert()">Invert</button>
              </div>
              <input type="hidden" name="playlist_items" id="playlist_items_hidden">
            </div>

            <div class="row row-3">
              <div class="field">
                <label for="output_dir">Output Directory</label>
                <div class="input-with-btn">
                  <input type="text" id="output_dir" name="output_dir" value="archive">
                  <button type="button" class="btn-browse" onclick="openBrowser()">Browse</button>
                </div>
              </div>
              <div class="field">
                <label for="audio_format">Format</label>
                <select id="audio_format" name="audio_format">
                  <option value="mp3" selected>MP3</option>
                  <option value="m4a">M4A (AAC)</option>
                  <option value="opus">Opus</option>
                  <option value="flac">FLAC (lossless)</option>
                  <option value="wav">WAV</option>
                  <option value="vorbis">Vorbis</option>
                </select>
              </div>
              <div class="field">
                <label for="audio_bitrate">Bitrate</label>
                <select id="audio_bitrate" name="audio_bitrate">
                  <option value="" selected>Best</option>
                  <option value="320k">320k</option>
                  <option value="256k">256k</option>
                  <option value="192k">192k</option>
                  <option value="128k">128k</option>
                </select>
              </div>
            </div>

            <div class="row row-3">
              <div class="field">
                <label for="download_archive">Skip List</label>
                <input type="text" id="download_archive" name="download_archive" value="downloaded.txt">
              </div>
              <div class="field">
                <label for="limit">Limit</label>
                <input type="number" id="limit" name="limit" min="1" inputmode="numeric" placeholder="all">
              </div>
              <div class="field">
                <label>&nbsp;</label>
                <div class="check-row">
                  <input type="checkbox" id="no_sidecar_metadata" name="no_sidecar_metadata">
                  <label for="no_sidecar_metadata">Skip metadata</label>
                </div>
              </div>
            </div>

            <div class="row row-3">
              <div class="field">
                <label for="normalize">Normalize Volume</label>
                <select id="normalize" name="normalize">
                  <option value="off" selected>Off</option>
                  <option value="ebu">EBU R128 (-16 LUFS)</option>
                  <option value="ebu-broadcast">EBU R128 (-23 LUFS)</option>
                  <option value="peak">Peak (-1.5 dBTP)</option>
                </select>
              </div>
              <div class="field">
                <label>&nbsp;</label>
              </div>
              <div class="field">
                <label>&nbsp;</label>
              </div>
            </div>

            <div class="btn-row">
              <button type="submit" class="btn btn-primary" id="submit-btn">
                <span id="btn-label">Start Archive</span>
              </button>
              <button type="button" class="btn btn-stop" id="stop-btn" onclick="stopJob()" disabled>
                Stop
              </button>
            </div>
          </form>
        </div>
      </div>

      <!-- Right: Status -->
      <div class="card">
        <div class="card-head">
          <h2>Output</h2>
          <span class="badge badge-idle" id="status-badge">idle</span>
        </div>
        <div class="card-body">
          <div class="stats">
            <div class="stat">
              <div class="stat-label">State</div>
              <div class="stat-value" id="st-state">Idle</div>
            </div>
            <div class="stat">
              <div class="stat-label">Exit</div>
              <div class="stat-value" id="st-exit">&mdash;</div>
            </div>
            <div class="stat">
              <div class="stat-label">Duration</div>
              <div class="stat-value" id="st-duration">&mdash;</div>
            </div>
            <div class="stat">
              <div class="stat-label">Items</div>
              <div class="stat-value" id="st-items">&mdash;</div>
            </div>
          </div>

          <div class="overall-wrap" id="overall-wrap">
            <div class="overall-bar-bg">
              <div class="overall-bar" id="overall-bar"></div>
            </div>
            <div class="overall-meta">
              <span id="overall-label">Overall</span>
              <span id="overall-pct">0%</span>
            </div>
          </div>

          <div class="progress-wrap" id="progress-wrap">
            <div class="progress-bar-bg">
              <div class="progress-bar" id="progress-bar"></div>
            </div>
            <div class="progress-meta">
              <span id="progress-file">Downloading...</span>
              <span id="progress-pct">0%</span>
            </div>
          </div>

          <div class="log-box" id="log">No active job.</div>
          <div class="cmd-preview" id="cmd" title="Click to expand"></div>
        </div>
      </div>

      <div class="card card-accent-file">
        <div class="card-head">
          <h2>Archived Files</h2>
          <span class="badge badge-idle" id="files-badge">0 files</span>
        </div>
        <div class="card-body" style="padding: 0;">
          <div class="files-list" id="files-list">
            <div class="files-empty">No files archived yet.</div>
          </div>
        </div>
      </div>

      <!-- Normalize -->
      <div class="card card-accent-normalize">
        <div class="card-head">
          <h2>Normalize Volume</h2>
          <span class="badge badge-idle" id="normalize-badge">idle</span>
        </div>
        <div class="card-body">
          <div class="row row-3">
            <div class="field">
              <label for="norm-dir">Directory</label>
              <div class="input-with-btn">
                <input type="text" id="norm-dir" placeholder="Path to audio files">
                <button type="button" class="btn-browse" onclick="browseNormalize()">Browse</button>
              </div>
            </div>
            <div class="field">
              <label for="norm-type">Type</label>
              <select id="norm-type">
                <option value="ebu" selected>EBU R128 (LUFS)</option>
                <option value="peak">Peak (dBTP)</option>
                <option value="rms">RMS (dB)</option>
              </select>
            </div>
            <div class="field">
              <label for="norm-target">Target</label>
              <input type="text" id="norm-target" value="-16" placeholder="-16">
            </div>
          </div>
          <div class="row row-2">
            <div class="field">
              <label for="norm-output">Output Directory</label>
              <input type="text" id="norm-output" value="normalized" placeholder="normalized">
            </div>
            <div class="field">
              <label>&nbsp;</label>
              <div class="btn-row">
                <button type="button" class="btn btn-primary" onclick="listNormalizeFiles()">Scan</button>
                <button type="button" class="btn btn-primary" onclick="startNormalize()" id="norm-start-btn">Start</button>
                <button type="button" class="btn btn-stop" onclick="stopNormalize()" id="norm-stop-btn" disabled>Stop</button>
              </div>
            </div>
          </div>
          <div class="norm-file-list" id="norm-file-list">
            <div class="files-empty">Click Scan to list audio files.</div>
          </div>
        </div>
      </div>

      <!-- Telegram -->
      <div class="card card-accent-telegram">
        <div class="card-head">
          <h2>Telegram</h2>
          <span class="badge badge-idle" id="telegram-badge">disconnected</span>
        </div>
        <div class="card-body">
          <div class="row row-3">
            <div class="field">
              <label for="tg-channel">Channel</label>
              <div class="input-with-btn">
                <input type="text" id="tg-channel" placeholder="@channelname or https://t.me/...">
                <button type="button" class="btn-browse" onclick="addTelegramChannel()">Add</button>
              </div>
            </div>
            <div class="field">
              <label>&nbsp;</label>
              <div class="btn-row">
                <button type="button" class="btn btn-primary" onclick="connectTelegram()">Connect</button>
                <button type="button" class="btn btn-stop" onclick="disconnectTelegram()">Disconnect</button>
              </div>
            </div>
            <div class="field">
              <label>&nbsp;</label>
            </div>
          </div>
          <div class="telegram-channels" id="telegram-channels">
            <div class="channels-empty">No Telegram channels added.</div>
          </div>
          <div class="row row-3" style="margin-top: 12px;">
            <div class="field">
              <label for="tg-browse-channel">Browse Channel</label>
              <select id="tg-browse-channel">
                <option value="">Select channel...</option>
              </select>
            </div>
            <div class="field">
              <label>&nbsp;</label>
              <button type="button" class="btn btn-primary" onclick="browseTelegramAudio()">Browse Audio</button>
            </div>
            <div class="field">
              <label>&nbsp;</label>
              <button type="button" class="btn btn-primary" onclick="downloadSelectedTelegram()">Download Selected</button>
            </div>
          </div>
          <div class="telegram-files" id="telegram-files">
            <div class="files-empty">Select a channel to browse audio files.</div>
          </div>
        </div>
      </div>

      <div class="card card-accent-monitor">
        <div class="card-head">
          <h2>Channel Monitor</h2>
          <span class="badge badge-idle" id="monitor-badge">stopped</span>
        </div>
        <div class="card-body" style="padding: 0;">
          <div class="add-channel-row">
            <input type="text" id="ch-name" placeholder="Channel name (e.g. Dlala Thukzin)">
            <input type="text" id="ch-url" placeholder="Channel URL (e.g. https://www.youtube.com/@DlalaThukzin)">
            <button class="btn-add" onclick="addChannel()">Add</button>
          </div>
          <div class="channels-list" id="channels-list">
            <div class="channels-empty">No channels monitored.</div>
          </div>
        </div>
        <div class="monitor-bar">
          <div class="monitor-status">
            <span class="dot" id="monitor-dot"></span>
            <span id="monitor-text">Monitor stopped</span>
          </div>
          <button class="btn-m-start" id="monitor-start-btn" onclick="startMonitor()">Start Monitor</button>
          <button class="btn-m-stop" id="monitor-stop-btn" onclick="stopMonitor()" disabled>Stop</button>
          <button class="btn-m-start" onclick="checkPlaylists()">Check Playlists</button>
          <button class="btn-m-start" onclick="rescanMissing()">Rescan Missing</button>
        </div>
      </div>

      <!-- Webhooks -->
      <div class="card card-accent-webhook">
        <div class="card-head">
          <h2>Webhooks</h2>
          <span class="badge badge-idle" id="webhook-badge">0 webhooks</span>
        </div>
        <div class="card-body" style="padding: 0;">
          <div class="add-channel-row">
            <input type="text" id="wh-name" placeholder="Name (optional)">
            <input type="text" id="wh-url" placeholder="Webhook URL (e.g. Discord, Slack)">
            <button class="btn-add" onclick="addWebhook()">Add</button>
          </div>
          <div class="webhook-list" id="webhook-list">
            <div class="channels-empty">No webhooks configured.</div>
          </div>
        </div>
      </div>

      <!-- Queue & History -->
      <div class="card card-accent-queue">
        <div class="card-head">
          <h2>Download Queue</h2>
          <span class="badge badge-idle" id="queue-badge">0 items</span>
        </div>
        <div class="card-body" style="padding: 0;">
          <div class="queue-list" id="queue-list">
            <div class="queue-empty">Queue empty.</div>
          </div>
          <div class="queue-actions">
            <button onclick="clearQueue()">Clear All</button>
          </div>
        </div>
        <div class="quiet-bar">
          <input type="checkbox" id="quiet-enabled" onchange="saveQuietHours()">
          <label for="quiet-enabled">Quiet hours:</label>
          <input type="number" id="quiet-start" min="0" max="23" value="23" onchange="saveQuietHours()">
          <label>&mdash;</label>
          <input type="number" id="quiet-end" min="0" max="23" value="7" onchange="saveQuietHours()">
        </div>
      </div>

      <div class="card card-accent-history">
        <div class="card-head">
          <h2>Download History</h2>
          <span class="badge badge-idle" id="history-badge">0 done</span>
        </div>
        <div class="card-body" style="padding: 0;">
          <div class="history-list" id="history-list">
            <div class="history-empty">No downloads yet.</div>
          </div>
        </div>
      </div>

      <!-- Storage Stats -->
      <div class="card card-accent-storage" style="grid-column: 1 / -1;">
        <div class="card-head">
          <h2>Storage</h2>
        </div>
        <div class="card-body">
          <div class="stats-row">
            <div class="stat-card">
              <div class="stat-card-label">Total Size</div>
              <div class="stat-card-value" id="st-total-size">--</div>
            </div>
            <div class="stat-card">
              <div class="stat-card-label">Audio Files</div>
              <div class="stat-card-value" id="st-file-count">--</div>
            </div>
            <div class="stat-card">
              <div class="stat-card-label">Downloads</div>
              <div class="stat-card-value" id="st-dir-count">--</div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- File Browser Modal -->
  <div class="modal-overlay" id="browser-modal">
    <div class="modal">
      <div class="modal-head">
        <h3>Select Output Directory</h3>
        <button class="modal-close" onclick="closeBrowser()">&times;</button>
      </div>
      <div class="modal-body">
        <div class="dir-path" id="browser-path">/</div>
        <div id="browser-list"></div>
      </div>
      <div class="modal-foot">
        <button class="btn btn-ghost" onclick="closeBrowser()">Cancel</button>
        <button class="btn btn-primary" onclick="browserSelect()">Select This Folder</button>
      </div>
    </div>
  </div>

  <script>
    const $ = s => document.getElementById(s);
    const CSRF = '__CSRF__';
    const form = $('archive-form');
    const btn = $('submit-btn');
    const stopBtn = $('stop-btn');
    const btnLabel = $('btn-label');
    const liveDot = $('live-dot');
    const headerStatus = $('header-status');
    const formBadge = $('form-badge');
    const statusBadge = $('status-badge');
    const stState = $('st-state');
    const stExit = $('st-exit');
    const stDuration = $('st-duration');
    const stItems = $('st-items');
    const overallWrap = $('overall-wrap');
    const overallBar = $('overall-bar');
    const overallLabel = $('overall-label');
    const overallPct = $('overall-pct');
    const progressWrap = $('progress-wrap');
    const progressBar = $('progress-bar');
    const progressFile = $('progress-file');
    const progressPct = $('progress-pct');
    const logEl = $('log');
    const cmdEl = $('cmd');
    const filesList = $('files-list');
    const filesBadge = $('files-badge');
    const toasts = $('toasts');
    const plSection = $('playlist-section');
    const plLoading = $('pl-loading');
    const plItems = $('pl-items');
    const plActions = $('pl-actions');
    const plHidden = $('playlist_items_hidden');
    const urlsInput = $('urls');

    function toast(msg, type='info') {
      const el = document.createElement('div');
      el.className = 'toast toast-' + type;
      el.textContent = msg;
      toasts.appendChild(el);
      setTimeout(() => el.remove(), 4200);
    }

    function parseProgress(line) {
      const m = line.match(/\[download\]\s+([\d.]+)%\s+of\s+([\d.]+\w+)/);
      if (m) return { pct: parseFloat(m[1]), size: m[2] };
      return null;
    }

    function parseOverallProgress(line) {
      const m = line.match(/\[download\]\s+Downloading item (\d+) of (\d+)/);
      if (m) return { current: parseInt(m[1]), total: parseInt(m[2]) };
      return null;
    }

    function countCompletedItems(log) {
      // Count unique "Destination:" lines = items fully downloaded
      const dests = new Set();
      const lines = log.split('\n');
      for (const line of lines) {
        const m = line.match(/\[download\]\s+Destination:\s+(.+)/);
        if (m) dests.add(m[1]);
      }
      return dests.size;
    }

    function getTotalFromLog(log) {
      const m = log.match(/\[download\]\s+Downloading item \d+ of (\d+)/);
      if (m) return parseInt(m[1]);
      return null;
    }

    function parseCurrentFile(line) {
      const m = line.match(/\[download\]\s+Destination:\s+(.+)/);
      if (m) return m[1].split('/').pop();
      return null;
    }

    function colorLine(line) {
      if (line.startsWith('$ ')) return '<span class="log-line-cmd">' + esc(line) + '</span>';
      if (line.includes('[download]') && line.includes('% of')) return '<span class="log-line-download">' + esc(line) + '</span>';
      if (line.includes('[ExtractAudio]') || line.includes('[download] Destination:')) return '<span class="log-line-extract">' + esc(line) + '</span>';
      if (line.includes('[Metadata]') || line.includes('[Thumbnails') || line.includes('[EmbedThumbnail]') || line.includes('Writing ')) return '<span class="log-line-meta">' + esc(line) + '</span>';
      if (line.includes('Error:') || line.includes('ERROR')) return '<span class="log-line-error">' + esc(line) + '</span>';
      if (line.includes('Archive finished')) return '<span class="log-line-done">' + esc(line) + '</span>';
      return esc(line);
    }

    function esc(s) {
      return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    }

    let lastLogLen = 0;
    let lastProgressFile = '';
    let lastOverallTotal = 0;
    let lastOverallCurrent = 0;

    async function refresh() {
      try {
        const res = await fetch('/status', { cache: 'no-store' });
        const data = await res.json();
        if (!data.job) {
          btn.disabled = false;
          stopBtn.disabled = true;
          liveDot.classList.remove('live');
          return;
        }

        const j = data.job;
        const running = Boolean(j.running);

        btn.disabled = running;
        stopBtn.disabled = !running;
        btnLabel.textContent = running ? 'Archiving...' : 'Start Archive';

        const badgeClass = running ? 'badge-running' : (j.error ? 'badge-fail' : (j.returncode === 0 ? 'badge-ok' : 'badge-idle'));
        const badgeText = running ? 'running' : (j.error ? 'failed' : (j.returncode === 0 ? 'done' : 'idle'));
        statusBadge.className = 'badge ' + badgeClass;
        statusBadge.textContent = badgeText;
        formBadge.className = 'badge ' + badgeClass;
        formBadge.textContent = badgeText;

        liveDot.classList.toggle('live', running);
        headerStatus.textContent = running ? 'archiving' : (j.returncode === 0 ? 'complete' : 'idle');

        stState.textContent = running ? 'Running' : (j.error ? 'Failed' : (j.returncode === 0 ? 'Complete' : 'Idle'));
        stState.className = 'stat-value ' + (running ? 'state-running' : (j.error ? 'state-fail' : (j.returncode === 0 ? 'state-ok' : 'state-idle')));
        stExit.textContent = j.returncode === null ? '\u2014' : String(j.returncode);

        if (j.finished_at && j.started_at) {
          const secs = Math.round(j.finished_at - j.started_at);
          stDuration.textContent = secs >= 60 ? Math.floor(secs/60) + 'm ' + (secs%60) + 's' : secs + 's';
        } else if (running && j.started_at) {
          const secs = Math.round(Date.now()/1000 - j.started_at);
          stDuration.textContent = secs >= 60 ? Math.floor(secs/60) + 'm ' + (secs%60) + 's' : secs + 's';
        }

        // Parse progress from log
        if (running && j.log) {
          const lines = j.log.split('\n');
          let filePct = null;
          let overall = null;
          let currentFile = lastProgressFile;

          for (let i = lines.length - 1; i >= Math.max(0, lines.length - 50); i--) {
            if (!filePct) filePct = parseProgress(lines[i]);
            if (!overall) overall = parseOverallProgress(lines[i]);
            const f = parseCurrentFile(lines[i]);
            if (f) { currentFile = f; break; }
          }

          if (filePct) {
            progressWrap.classList.add('active');
            progressBar.style.width = filePct.pct + '%';
            progressPct.textContent = filePct.pct.toFixed(1) + '%';
          }
          // Don't remove active between files - keep last known progress visible

          if (currentFile) { lastProgressFile = currentFile; progressFile.textContent = currentFile; }

          // Overall progress: use "Downloading item X of Y" or count completed destinations
          if (overall) {
            lastOverallTotal = overall.total;
            lastOverallCurrent = overall.current;
          } else {
            // Fallback: count completed items from log
            const completed = countCompletedItems(j.log);
            if (lastOverallTotal) {
              lastOverallCurrent = completed;
            }
          }

          if (lastOverallTotal) {
            overallWrap.classList.add('active');
            const pct = Math.round((lastOverallCurrent / lastOverallTotal) * 100);
            overallBar.style.width = pct + '%';
            overallLabel.textContent = lastOverallCurrent + ' of ' + lastOverallTotal + ' items';
            overallPct.textContent = pct + '%';
            stItems.textContent = lastOverallCurrent + ' / ' + lastOverallTotal;
          }
        } else {
          progressWrap.classList.remove('active');
          if (!running) {
            overallWrap.classList.remove('active');
            lastOverallTotal = 0;
            lastOverallCurrent = 0;
          }
        }

        if (j.log) {
          const logLines = j.log.split('\n');
          if (logLines.length !== lastLogLen) {
            logEl.innerHTML = logLines.map(colorLine).join('\n');
            logEl.scrollTop = logEl.scrollHeight;
            lastLogLen = logLines.length;
          }
        }

        cmdEl.textContent = j.command || '';

        if (!running && j.finished_at) {
          if (j.returncode === 0 && !j._notified) {
            toast('Archive completed successfully', 'ok');
            j._notified = true;
            refreshFiles();
          } else if (j.error && !j._notified) {
            toast('Archive failed: ' + j.error, 'err');
            j._notified = true;
          }
        }
      } catch(e) {}
    }

    async function refreshFiles() {
      try {
        const res = await fetch('/files', { cache: 'no-store' });
        const data = await res.json();
        if (!data.files || data.files.length === 0) {
          filesList.innerHTML = '<div class="files-empty">No files archived yet.</div>';
          filesBadge.className = 'badge badge-idle';
          filesBadge.textContent = '0 files';
          return;
        }
        filesBadge.className = 'badge badge-count';
        filesBadge.textContent = data.files.length + ' file' + (data.files.length !== 1 ? 's' : '');
        filesList.innerHTML = data.files.map(f => {
          const name = f.path.split('/').pop();
          const size = f.size >= 1048576 ? (f.size/1048576).toFixed(1) + ' MB' : f.size >= 1024 ? (f.size/1024).toFixed(0) + ' KB' : f.size + ' B';
          return '<div class="file-item"><span class="file-icon">&#9835;</span><span class="file-name" title="' + esc(f.path) + '">' + esc(name) + '</span><span class="file-size">' + size + '</span></div>';
        }).join('');
      } catch(e) {}
    }

    // Playlist detection + fetch
    let plTimer = null;
    urlsInput.addEventListener('input', () => {
      clearTimeout(plTimer);
      const val = urlsInput.value.trim();
      if (val && (val.includes('/playlist') || val.includes('/@') || val.includes('/channel/'))) {
        plTimer = setTimeout(() => fetchPlaylist(val.split('\n')[0].trim()), 800);
      } else {
        plSection.style.display = 'none';
        plHidden.value = '';
      }
    });

    async function fetchPlaylist(url) {
      plSection.style.display = '';
      plLoading.style.display = '';
      plItems.style.display = 'none';
      plActions.style.display = 'none';
      try {
        const res = await fetch('/playlist?url=' + encodeURIComponent(url));
        const data = await res.json();
        if (!data.items || data.items.length === 0) {
          plLoading.textContent = 'No items found or not a playlist URL.';
          return;
        }
        plLoading.style.display = 'none';
        plItems.style.display = '';
        plActions.style.display = '';
        plItems.innerHTML = data.items.map((item, i) => {
          const dur = item.duration && item.duration !== 'None' ? item.duration : '';
          return '<label class="pl-item"><input type="checkbox" checked data-idx="' + (i+1) + '"><span class="pl-item-title">' + esc(item.title) + '</span><span class="pl-item-dur">' + esc(String(dur)) + '</span></label>';
        }).join('');
        updatePlaylistHidden();
      } catch(e) {
        plLoading.textContent = 'Failed to load playlist.';
      }
    }

    // Listen for playlist checkbox changes (added once, not per fetch)
    plItems.addEventListener('change', updatePlaylistHidden);

    function updatePlaylistHidden() {
      const checked = plItems.querySelectorAll('input[type="checkbox"]:checked');
      const all = plItems.querySelectorAll('input[type="checkbox"]');
      if (checked.length === all.length || checked.length === 0) {
        plHidden.value = '';
      } else {
        const indices = Array.from(checked).map(c => c.dataset.idx);
        plHidden.value = indices.join(',');
      }
    }

    function plSelectAll() { plItems.querySelectorAll('input').forEach(c => c.checked = true); updatePlaylistHidden(); }
    function plSelectNone() { plItems.querySelectorAll('input').forEach(c => c.checked = false); updatePlaylistHidden(); }
    function plInvert() { plItems.querySelectorAll('input').forEach(c => c.checked = !c.checked); updatePlaylistHidden(); }

    // Stop
    async function stopJob() {
      try {
        await fetch('/stop', {
          method: 'POST',
          headers: {'Content-Type': 'application/x-www-form-urlencoded'},
          body: 'csrf_token=' + encodeURIComponent(CSRF)
        });
        toast('Stopping archive...', 'info');
      } catch(e) {}
    }

    // File browser
    let browserCurrentPath = '/Users';

    async function openBrowser() {
      $('browser-modal').classList.add('active');
      await loadDir($('output_dir').value || '/');
    }

    function closeBrowser() { $('browser-modal').classList.remove('active'); }

    async function loadDir(path) {
      try {
        const res = await fetch('/browse?path=' + encodeURIComponent(path));
        const data = await res.json();
        browserCurrentPath = data.path;
        $('browser-path').textContent = data.path;
        let html = '';
        if (data.parent) {
          html += '<div class="dir-item" onclick="loadDir(' + "'" + data.parent.replace(/'/g, "\\'") + "'" + ')"><span class="dir-icon">&#128193;</span>..</div>';
        }
        for (const d of data.dirs) {
          const escaped = d.path.replace(/'/g, "\\'");
          html += '<div class="dir-item" onclick="loadDir(' + "'" + escaped + "'" + ')"><span class="dir-icon">&#128193;</span>' + esc(d.name) + '</div>';
        }
        if (!data.dirs.length && !data.parent) {
          html = '<div class="empty-state">No subdirectories found.</div>';
        }
        $('browser-list').innerHTML = html;
      } catch(e) {
        $('browser-list').innerHTML = '<div class="empty-state">Failed to load directory.</div>';
      }
    }

    function browserSelect() {
      $('output_dir').value = browserCurrentPath;
      closeBrowser();
    }

    form.addEventListener('submit', () => {
      btn.disabled = true;
      btnLabel.textContent = 'Starting...';
      lastLogLen = 0;
      lastProgressFile = '';
      lastOverallTotal = 0;
      lastOverallCurrent = 0;
      toast('Archive job started', 'info');
    });

    document.addEventListener('keydown', e => {
      if ((e.ctrlKey || e.metaKey) && e.key === 'Enter' && !btn.disabled) form.requestSubmit();
      if (e.key === 'Escape') closeBrowser();
    });

    refresh();
    refreshFiles();
    refreshChannels();
    refreshWebhooks();
    setInterval(refresh, 1200);
    setInterval(refreshFiles, 5000);
    setInterval(refreshChannels, 5000);
    setInterval(refreshWebhooks, 5000);

    // Channel monitor functions
    async function refreshChannels() {
      try {
        const res = await fetch('/channels', { cache: 'no-store' });
        const data = await res.json();
        const list = $('channels-list');
        if (!data.channels || data.channels.length === 0) {
          list.innerHTML = '<div class="channels-empty">No channels monitored. Add one above.</div>';
          $('monitor-badge').textContent = '0 channels';
          return;
        }
        $('monitor-badge').textContent = data.channels.length + ' channel' + (data.channels.length !== 1 ? 's' : '');
        list.innerHTML = data.channels.map(ch => {
          const lastCheck = ch.last_check ? new Date(ch.last_check * 1000).toLocaleString() : 'Never';
          const eurl = encodeURIComponent(ch.url);
          const sched = ch.schedule && ch.schedule.length > 0
            ? ch.schedule.map(s => s[0] + ':00-' + s[1] + ':00').join(', ')
            : 'Always';
          const quality = ch.audio_format + ' ' + ch.audio_bitrate;
          return '<div class="channel-item">' +
            '<div class="channel-info">' +
              '<div class="channel-name">' + esc(ch.name) + '</div>' +
              '<div class="channel-url">' + esc(ch.url) + '</div>' +
              '<div class="channel-last">Last check: ' + lastCheck + '</div>' +
              '<div class="channel-schedule">Schedule: ' + esc(sched) + '</div>' +
              '<div class="channel-quality">Quality: ' + esc(quality) + '</div>' +
            '</div>' +
            '<div class="channel-actions">' +
              '<button data-url="' + eurl + '" data-enabled="' + !ch.enabled + '" class="btn-toggle ' + (ch.enabled ? 'on' : '') + '">' + (ch.enabled ? 'ON' : 'OFF') + '</button>' +
              '<button data-url="' + eurl + '" data-name="' + esc(ch.name) + '" class="btn-schedule">Schedule</button>' +
              '<button data-url="' + eurl + '" data-name="' + esc(ch.name) + '" data-fmt="' + esc(ch.audio_format) + '" data-br="' + esc(ch.audio_bitrate) + '" class="btn-quality">Quality</button>' +
              '<button data-url="' + eurl + '" class="btn-remove">Remove</button>' +
            '</div>' +
          '</div>';
        }).join('');
        // Attach event delegation for channel actions
        list.onclick = function(e) {
          const btn = e.target.closest('button');
          if (!btn) return;
          const url = decodeURIComponent(btn.dataset.url);
          if (btn.classList.contains('btn-toggle')) {
            toggleChannel(url, btn.dataset.enabled === 'true');
          } else if (btn.classList.contains('btn-remove')) {
            removeChannel(url);
          } else if (btn.classList.contains('btn-schedule')) {
            editSchedule(url, btn.dataset.name);
          } else if (btn.classList.contains('btn-quality')) {
            editQuality(url, btn.dataset.name, btn.dataset.fmt, btn.dataset.br);
          }
        };
      } catch(e) {}
    }

    async function addChannel() {
      const name = $('ch-name').value.trim();
      const url = $('ch-url').value.trim();
      if (!name || !url) {
        toast('Enter both name and URL', 'err');
        return;
      }
      try {
        const res = await fetch('/channels/add', {
          method: 'POST',
          headers: {'Content-Type': 'application/x-www-form-urlencoded'},
          body: 'csrf_token=' + encodeURIComponent(CSRF) + '&url=' + encodeURIComponent(url) + '&name=' + encodeURIComponent(name)
        });
        const data = await res.json();
        if (data.ok) {
          toast('Channel added: ' + name, 'ok');
          $('ch-name').value = '';
          $('ch-url').value = '';
          refreshChannels();
        } else {
          toast(data.error || 'Failed to add channel', 'err');
        }
      } catch(e) {
        toast('Failed to add channel', 'err');
      }
    }

    async function removeChannel(url) {
      try {
        const res = await fetch('/channels/remove', {
          method: 'POST',
          headers: {'Content-Type': 'application/x-www-form-urlencoded'},
          body: 'csrf_token=' + encodeURIComponent(CSRF) + '&url=' + encodeURIComponent(url)
        });
        const data = await res.json();
        if (data.ok) {
          toast('Channel removed', 'ok');
          refreshChannels();
        }
      } catch(e) {}
    }

    async function toggleChannel(url, enabled) {
      try {
        await fetch('/channels/toggle', {
          method: 'POST',
          headers: {'Content-Type': 'application/x-www-form-urlencoded'},
          body: 'csrf_token=' + encodeURIComponent(CSRF) + '&url=' + encodeURIComponent(url) + '&enabled=' + (enabled ? '1' : '0')
        });
        refreshChannels();
      } catch(e) {}
    }

    function editSchedule(url, name) {
      const input = prompt('Schedule for ' + name + ' (e.g. "9-18" for 9am-6pm, "23-6" for 11pm-6am, blank for always):');
      if (input === null) return;
      let schedule = [];
      if (input.trim()) {
        const parts = input.split(',').map(s => s.trim());
        for (const part of parts) {
          const match = part.match(/^(\d{1,2})\s*-\s*(\d{1,2})$/);
          if (!match) {
            toast('Invalid format. Use: 9-18 or 23-6', 'err');
            return;
          }
          schedule.push([parseInt(match[1]), parseInt(match[2])]);
        }
      }
      fetch('/channels/schedule', {
        method: 'POST',
        headers: {'Content-Type': 'application/x-www-form-urlencoded'},
        body: 'csrf_token=' + encodeURIComponent(CSRF) + '&url=' + encodeURIComponent(url) + '&schedule=' + encodeURIComponent(JSON.stringify(schedule))
      }).then(r => r.json()).then(data => {
        if (data.ok) {
          toast('Schedule updated', 'ok');
          refreshChannels();
        } else {
          toast(data.error || 'Failed', 'err');
        }
      }).catch(() => {});
    }

    function editQuality(url, name, currentFormat, currentBitrate) {
      const formats = ['mp3', 'm4a', 'opus', 'flac', 'wav', 'vorbis'];
      const bitrates = ['64k', '96k', '128k', '192k', '256k', '320k'];
      const input = prompt(
        'Quality for ' + name + '\n' +
        'Format: ' + formats.join(', ') + '\n' +
        'Bitrate: ' + bitrates.join(', ') + '\n\n' +
        'Enter format bitrate (e.g. "mp3 192k"):',
        currentFormat + ' ' + currentBitrate
      );
      if (!input) return;
      const parts = input.trim().split(/\s+/);
      if (parts.length !== 2 || !formats.includes(parts[0]) || !bitrates.includes(parts[1])) {
        toast('Invalid. Use: mp3 192k', 'err');
        return;
      }
      fetch('/channels/quality', {
        method: 'POST',
        headers: {'Content-Type': 'application/x-www-form-urlencoded'},
        body: 'csrf_token=' + encodeURIComponent(CSRF) + '&url=' + encodeURIComponent(url) + '&audio_format=' + encodeURIComponent(parts[0]) + '&audio_bitrate=' + encodeURIComponent(parts[1])
      }).then(r => r.json()).then(data => {
        if (data.ok) {
          toast('Quality updated', 'ok');
          refreshChannels();
        } else {
          toast(data.error || 'Failed', 'err');
        }
      }).catch(() => {});
    }

    async function refreshWebhooks() {
      try {
        const res = await fetch('/webhooks', { cache: 'no-store' });
        const data = await res.json();
        const list = $('webhook-list');
        const badge = $('webhook-badge');
        if (!data.webhooks || data.webhooks.length === 0) {
          list.innerHTML = '<div class="channels-empty">No webhooks configured.</div>';
          badge.className = 'badge badge-idle';
          badge.textContent = '0 webhooks';
          return;
        }
        badge.className = 'badge badge-count';
        badge.textContent = data.webhooks.length + ' webhook' + (data.webhooks.length !== 1 ? 's' : '');
        list.innerHTML = data.webhooks.map(w => {
          const eurl = encodeURIComponent(w.url);
          return '<div class="webhook-item">' +
            '<div class="webhook-info">' +
              '<div class="webhook-name">' + esc(w.name) + '</div>' +
              '<div class="webhook-url">' + esc(w.url) + '</div>' +
              '<div class="webhook-events">Events: ' + esc(w.events.join(', ')) + '</div>' +
            '</div>' +
            '<div class="webhook-actions">' +
              '<button data-url="' + eurl + '" class="btn-wh-remove">Remove</button>' +
            '</div>' +
          '</div>';
        }).join('');
        list.onclick = function(e) {
          const btn = e.target.closest('button');
          if (!btn || !btn.classList.contains('btn-wh-remove')) return;
          removeWebhook(decodeURIComponent(btn.dataset.url));
        };
      } catch(e) {}
    }

    async function addWebhook() {
      const name = $('wh-name').value.trim();
      const url = $('wh-url').value.trim();
      if (!url) {
        toast('Enter a webhook URL', 'err');
        return;
      }
      try {
        const res = await fetch('/webhooks/add', {
          method: 'POST',
          headers: {'Content-Type': 'application/x-www-form-urlencoded'},
          body: 'csrf_token=' + encodeURIComponent(CSRF) + '&url=' + encodeURIComponent(url) + '&name=' + encodeURIComponent(name)
        });
        const data = await res.json();
        if (data.ok) {
          toast('Webhook added', 'ok');
          $('wh-name').value = '';
          $('wh-url').value = '';
          refreshWebhooks();
        } else {
          toast(data.error || 'Failed', 'err');
        }
      } catch(e) {}
    }

    async function removeWebhook(url) {
      try {
        const res = await fetch('/webhooks/remove', {
          method: 'POST',
          headers: {'Content-Type': 'application/x-www-form-urlencoded'},
          body: 'csrf_token=' + encodeURIComponent(CSRF) + '&url=' + encodeURIComponent(url)
        });
        const data = await res.json();
        if (data.ok) {
          toast('Webhook removed', 'ok');
          refreshWebhooks();
        }
      } catch(e) {}
    }

    async function startMonitor() {
      try {
        await fetch('/monitor/start', {
          method: 'POST',
          headers: {'Content-Type': 'application/x-www-form-urlencoded'},
          body: 'csrf_token=' + encodeURIComponent(CSRF)
        });
        toast('Monitor started', 'ok');
        refreshMonitorStatus();
      } catch(e) {}
    }

    async function stopMonitor() {
      try {
        await fetch('/monitor/stop', {
          method: 'POST',
          headers: {'Content-Type': 'application/x-www-form-urlencoded'},
          body: 'csrf_token=' + encodeURIComponent(CSRF)
        });
        toast('Monitor stopped', 'info');
        refreshMonitorStatus();
      } catch(e) {}
    }

    async function refreshMonitorStatus() {
      try {
        const res = await fetch('/monitor/status', { cache: 'no-store' });
        const data = await res.json();
        const dot = $('monitor-dot');
        const text = $('monitor-text');
        const badge = $('monitor-badge');
        const startBtn = $('monitor-start-btn');
        const stopBtn = $('monitor-stop-btn');
        if (data.running) {
          dot.classList.add('on');
          text.textContent = 'Checking every ' + data.check_interval + 's';
          badge.className = 'badge badge-running';
          badge.textContent = 'running';
          startBtn.disabled = true;
          stopBtn.disabled = false;
        } else {
          dot.classList.remove('on');
          text.textContent = 'Monitor stopped';
          badge.className = 'badge badge-idle';
          badge.textContent = 'stopped';
          startBtn.disabled = false;
          stopBtn.disabled = true;
        }
      } catch(e) {}
    }

    async function checkPlaylists() {
      try {
        toast('Checking for new playlists...', 'info');
        const res = await fetch('/playlists/check', {
          method: 'POST',
          headers: {'Content-Type': 'application/x-www-form-urlencoded'},
          body: 'csrf_token=' + encodeURIComponent(CSRF)
        });
        const data = await res.json();
        if (data.ok) {
          toast('Found ' + data.total + ' new playlists', 'ok');
        } else {
          toast('Error: ' + (data.error || 'Unknown'), 'err');
        }
      } catch(e) {
        toast('Failed to check playlists', 'err');
      }
    }

    async function rescanMissing() {
      try {
        toast('Scanning for missing files...', 'info');
        const res = await fetch('/rescan', {
          method: 'POST',
          headers: {'Content-Type': 'application/x-www-form-urlencoded'},
          body: 'csrf_token=' + encodeURIComponent(CSRF)
        });
        const data = await res.json();
        if (data.ok) {
          if (data.missing_count > 0) {
            toast('Cleaned ' + data.missing_count + ' missing entries from archive. Re-downloads queued.', 'ok');
          } else {
            toast('No missing files found', 'ok');
          }
        } else {
          toast('Error: ' + (data.error || 'Unknown'), 'err');
        }
      } catch(e) {
        toast('Failed to rescan', 'err');
      }
    }

    refreshMonitorStatus();
    setInterval(refreshMonitorStatus, 3000);

    // Queue functions
    async function refreshQueue() {
      try {
        const res = await fetch('/queue', { cache: 'no-store' });
        const data = await res.json();
        const list = $('queue-list');
        const badge = $('queue-badge');
        if (!data.queue || data.queue.length === 0) {
          list.innerHTML = '<div class="queue-empty">Queue empty.</div>';
          badge.className = 'badge badge-idle';
          badge.textContent = '0 items';
          return;
        }
        badge.className = 'badge badge-count';
        badge.textContent = data.queue.length + ' item' + (data.queue.length !== 1 ? 's' : '');
        list.innerHTML = data.queue.map((item, i) => {
          return '<div class="queue-item">' +
            '<span class="queue-item-num">#' + (i+1) + '</span>' +
            '<span class="queue-item-name" title="' + esc(item.url) + '">' + esc(item.name || item.url) + '</span>' +
            '<button class="queue-item-cancel" onclick="cancelQueueItem(' + i + ')">Cancel</button>' +
          '</div>';
        }).join('');
      } catch(e) {}
    }

    async function cancelQueueItem(index) {
      try {
        const res = await fetch('/queue/cancel', {
          method: 'POST',
          headers: {'Content-Type': 'application/x-www-form-urlencoded'},
          body: 'csrf_token=' + encodeURIComponent(CSRF) + '&index=' + index
        });
        const data = await res.json();
        if (data.ok) refreshQueue();
      } catch(e) {}
    }

    async function clearQueue() {
      try {
        const res = await fetch('/queue/clear', {
          method: 'POST',
          headers: {'Content-Type': 'application/x-www-form-urlencoded'},
          body: 'csrf_token=' + encodeURIComponent(CSRF)
        });
        const data = await res.json();
        if (data.ok) refreshQueue();
      } catch(e) {}
    }

    // History functions
    async function refreshHistory() {
      try {
        const res = await fetch('/queue/history', { cache: 'no-store' });
        const data = await res.json();
        const list = $('history-list');
        const badge = $('history-badge');
        if (!data.history || data.history.length === 0) {
          list.innerHTML = '<div class="history-empty">No downloads yet.</div>';
          badge.className = 'badge badge-idle';
          badge.textContent = '0 done';
          return;
        }
        badge.className = 'badge badge-count';
        badge.textContent = data.history.length + ' done';
        list.innerHTML = data.history.slice().reverse().map((item, idx) => {
          const time = new Date(item.finished_at * 1000).toLocaleString();
          const ok = item.returncode === 0;
          const realIdx = data.history.length - 1 - idx;
          const retryBtn = !ok && item.url
            ? ' <button class="history-retry" onclick="retryFailed(\'' + esc(item.url).replace(/'/g, "\\'") + '\', \'' + esc(item.name || '').replace(/'/g, "\\'") + '\')">Retry</button>'
            : '';
          return '<div class="history-item">' +
            '<span class="history-dot ' + (ok ? 'ok' : 'fail') + '"></span>' +
            '<span class="history-name" title="' + esc(item.url || '') + '">' + esc(item.name || 'Unknown') + retryBtn + '</span>' +
            '<span class="history-time">' + time + '</span>' +
          '</div>';
        }).join('');
      } catch(e) {}
    }

    async function retryFailed(url, name) {
      try {
        const res = await fetch('/queue/retry', {
          method: 'POST',
          headers: {'Content-Type': 'application/x-www-form-urlencoded'},
          body: 'csrf_token=' + encodeURIComponent(CSRF) + '&url=' + encodeURIComponent(url) + '&name=' + encodeURIComponent(name)
        });
        const data = await res.json();
        if (data.ok) {
          toast('Retry queued', 'ok');
          refreshHistory();
        } else {
          toast(data.error || 'Failed', 'err');
        }
      } catch(e) {}
    }

    // Quiet hours
    async function loadQuietHours() {
      try {
        const res = await fetch('/quiet-hours', { cache: 'no-store' });
        const data = await res.json();
        $('quiet-enabled').checked = data.enabled;
        $('quiet-start').value = data.start;
        $('quiet-end').value = data.end;
      } catch(e) {}
    }

    async function saveQuietHours() {
      try {
        await fetch('/quiet-hours', {
          method: 'POST',
          headers: {'Content-Type': 'application/x-www-form-urlencoded'},
          body: 'csrf_token=' + encodeURIComponent(CSRF) +
                '&enabled=' + ($('quiet-enabled').checked ? '1' : '0') +
                '&start=' + $('quiet-start').value +
                '&end=' + $('quiet-end').value
        });
      } catch(e) {}
    }

    // Storage stats
    async function refreshStorage() {
      try {
        const res = await fetch('/files', { cache: 'no-store' });
        const data = await res.json();
        if (data.files) {
          let totalSize = 0;
          for (const f of data.files) totalSize += f.size;
          $('st-total-size').textContent = totalSize >= 1073741824 ? (totalSize/1073741824).toFixed(1) + ' GB' : totalSize >= 1048576 ? (totalSize/1048576).toFixed(1) + ' MB' : (totalSize/1024).toFixed(0) + ' KB';
          $('st-file-count').textContent = data.files.length;
        }
      } catch(e) {}
      try {
        const res = await fetch('/queue/stats', { cache: 'no-store' });
        const data = await res.json();
        $('st-dir-count').textContent = data.history_count || '0';
      } catch(e) {}
    }

    refreshQueue();
    refreshHistory();
    loadQuietHours();
    refreshStorage();
    setInterval(refreshQueue, 2000);
    setInterval(refreshHistory, 5000);
    setInterval(refreshStorage, 10000);

    // Normalize functions
    let normFiles = [];

    function browseNormalize() {
      openBrowser('norm-dir');
    }

    async function listNormalizeFiles() {
      const dir = $('norm-dir').value.trim();
      if (!dir) { toast('Enter a directory path', 'err'); return; }
      try {
        const res = await fetch('/api/normalize/list?dir=' + encodeURIComponent(dir));
        const data = await res.json();
        const list = $('norm-file-list');
        const badge = $('normalize-badge');
        if (!data.files || data.files.length === 0) {
          list.innerHTML = '<div class="files-empty">No audio files found.</div>';
          badge.className = 'badge badge-idle';
          badge.textContent = '0 files';
          normFiles = [];
          return;
        }
        normFiles = data.files;
        badge.className = 'badge badge-count';
        badge.textContent = data.files.length + ' files';
        list.innerHTML = data.files.map((f, i) =>
          '<label class="norm-file-item">' +
          '<input type="checkbox" checked data-index="' + i + '">' +
          '<span class="file-name" title="' + esc(f.path) + '">' + esc(f.path) + '</span>' +
          '<span class="file-size">' + formatSize(f.size) + '</span>' +
          '</label>'
        ).join('');
      } catch(e) { toast('Failed to list files', 'err'); }
    }

    async function startNormalize() {
      const dir = $('norm-dir').value.trim();
      const outputDir = $('norm-output').value.trim() || 'normalized';
      const normType = $('norm-type').value;
      const target = parseFloat($('norm-target').value) || -16;
      const checkboxes = $('norm-file-list').querySelectorAll('input[type="checkbox"]:checked');
      const selected = Array.from(checkboxes).map(cb => normFiles[parseInt(cb.dataset.index)]?.path).filter(Boolean);
      if (selected.length === 0) { toast('Select files to normalize', 'err'); return; }
      try {
        const body = 'csrf_token=' + encodeURIComponent(CSRF) +
          '&files=' + encodeURIComponent(selected.join('\n')) +
          '&type=' + encodeURIComponent(normType) +
          '&target=' + target +
          '&output_dir=' + encodeURIComponent(outputDir);
        const res = await fetch('/api/normalize/start', {
          method: 'POST',
          headers: {'Content-Type': 'application/x-www-form-urlencoded'},
          body: body
        });
        const data = await res.json();
        if (data.ok) {
          toast('Normalization started', 'ok');
          $('norm-start-btn').disabled = true;
          $('norm-stop-btn').disabled = false;
          $('normalize-badge').className = 'badge badge-running';
          $('normalize-badge').textContent = 'running';
        } else {
          toast(data.error || 'Failed to start', 'err');
        }
      } catch(e) { toast('Failed to start normalization', 'err'); }
    }

    async function stopNormalize() {
      try {
        const res = await fetch('/api/normalize/stop', {
          method: 'POST',
          headers: {'Content-Type': 'application/x-www-form-urlencoded'},
          body: 'csrf_token=' + encodeURIComponent(CSRF)
        });
        const data = await res.json();
        if (data.ok) {
          toast('Normalization stopped', 'ok');
          $('norm-start-btn').disabled = false;
          $('norm-stop-btn').disabled = true;
          $('normalize-badge').className = 'badge badge-idle';
          $('normalize-badge').textContent = 'idle';
        }
      } catch(e) {}
    }

    setInterval(() => {
      fetch('/api/normalize/status', { cache: 'no-store' }).then(r => r.json()).then(data => {
        const badge = $('normalize-badge');
        if (data.running) {
          badge.className = 'badge badge-running';
          badge.textContent = data.progress || 'running';
          $('norm-start-btn').disabled = true;
          $('norm-stop-btn').disabled = false;
        } else {
          badge.className = 'badge badge-idle';
          badge.textContent = 'idle';
          $('norm-start-btn').disabled = false;
          $('norm-stop-btn').disabled = true;
        }
      }).catch(() => {});
    }, 2000);

    // Telegram functions
    let tgFiles = [];

    async function connectTelegram() {
      try {
        const res = await fetch('/telegram/connect', {
          method: 'POST',
          headers: {'Content-Type': 'application/x-www-form-urlencoded'},
          body: 'csrf_token=' + encodeURIComponent(CSRF)
        });
        const data = await res.json();
        if (data.ok) {
          toast('Connected to Telegram', 'ok');
          $('telegram-badge').className = 'badge badge-ok';
          $('telegram-badge').textContent = 'connected';
          refreshTelegramChannels();
        } else {
          toast(data.error || 'Failed to connect', 'err');
        }
      } catch(e) { toast('Failed to connect', 'err'); }
    }

    async function disconnectTelegram() {
      try {
        const res = await fetch('/telegram/disconnect', {
          method: 'POST',
          headers: {'Content-Type': 'application/x-www-form-urlencoded'},
          body: 'csrf_token=' + encodeURIComponent(CSRF)
        });
        const data = await res.json();
        if (data.ok) {
          toast('Disconnected from Telegram', 'ok');
          $('telegram-badge').className = 'badge badge-idle';
          $('telegram-badge').textContent = 'disconnected';
        }
      } catch(e) {}
    }

    async function addTelegramChannel() {
      const channel = $('tg-channel').value.trim();
      if (!channel) { toast('Enter a channel name or URL', 'err'); return; }
      try {
        const res = await fetch('/telegram/add', {
          method: 'POST',
          headers: {'Content-Type': 'application/x-www-form-urlencoded'},
          body: 'csrf_token=' + encodeURIComponent(CSRF) + '&channel=' + encodeURIComponent(channel)
        });
        const data = await res.json();
        if (data.ok) {
          toast('Channel added', 'ok');
          $('tg-channel').value = '';
          refreshTelegramChannels();
        } else {
          toast(data.error || 'Failed to add channel', 'err');
        }
      } catch(e) { toast('Failed to add channel', 'err'); }
    }

    async function refreshTelegramChannels() {
      try {
        const res = await fetch('/telegram/channels', { cache: 'no-store' });
        const data = await res.json();
        const list = $('telegram-channels');
        const select = $('tg-browse-channel');
        if (!data.channels || data.channels.length === 0) {
          list.innerHTML = '<div class="channels-empty">No Telegram channels added.</div>';
          select.innerHTML = '<option value="">Select channel...</option>';
          return;
        }
        list.innerHTML = data.channels.map(ch =>
          '<div class="channel-item">' +
          '<span class="channel-name">' + esc(ch.name) + '</span>' +
          '<span class="channel-username">@' + esc(ch.username || '') + '</span>' +
          '<button class="btn-remove" onclick="removeTelegramChannel(' + ch.channel_id + ')">Remove</button>' +
          '</div>'
        ).join('');
        select.innerHTML = '<option value="">Select channel...</option>' +
          data.channels.map(ch =>
            '<option value="' + ch.channel_id + '">' + esc(ch.name) + '</option>'
          ).join('');
      } catch(e) {}
    }

    async function removeTelegramChannel(channelId) {
      try {
        const res = await fetch('/telegram/remove', {
          method: 'POST',
          headers: {'Content-Type': 'application/x-www-form-urlencoded'},
          body: 'csrf_token=' + encodeURIComponent(CSRF) + '&channel_id=' + channelId
        });
        const data = await res.json();
        if (data.ok) {
          toast('Channel removed', 'ok');
          refreshTelegramChannels();
        }
      } catch(e) {}
    }

    async function browseTelegramAudio() {
      const channelId = $('tg-browse-channel').value;
      if (!channelId) { toast('Select a channel', 'err'); return; }
      try {
        const res = await fetch('/telegram/browse?channel=' + channelId, { cache: 'no-store' });
        const data = await res.json();
        const list = $('telegram-files');
        if (!data.files || data.files.length === 0) {
          list.innerHTML = '<div class="files-empty">No audio files found.</div>';
          tgFiles = [];
          return;
        }
        tgFiles = data.files;
        list.innerHTML = data.files.map((f, i) =>
          '<label class="norm-file-item">' +
          '<input type="checkbox" checked data-index="' + i + '">' +
          '<span class="file-name" title="' + esc(f.filename) + '">' + esc(f.filename) + '</span>' +
          '<span class="file-size">' + formatSize(f.size) + '</span>' +
          '</label>'
        ).join('');
      } catch(e) { toast('Failed to browse audio', 'err'); }
    }

    async function downloadSelectedTelegram() {
      const channelId = $('tg-browse-channel').value;
      if (!channelId) { toast('Select a channel', 'err'); return; }
      const checkboxes = $('telegram-files').querySelectorAll('input[type="checkbox"]:checked');
      const selected = Array.from(checkboxes).map(cb => {
        const idx = parseInt(cb.dataset.index);
        return tgFiles[idx]?.message_id;
      }).filter(Boolean);
      if (selected.length === 0) { toast('Select files to download', 'err'); return; }
      try {
        const body = 'csrf_token=' + encodeURIComponent(CSRF) +
          '&channel=' + encodeURIComponent(channelId) +
          '&message_ids=' + encodeURIComponent(selected.join(','));
        const res = await fetch('/telegram/download', {
          method: 'POST',
          headers: {'Content-Type': 'application/x-www-form-urlencoded'},
          body: body
        });
        const data = await res.json();
        if (data.ok) {
          toast('Download started: ' + (data.downloaded || 0) + ' files', 'ok');
        } else {
          toast(data.error || 'Failed to download', 'err');
        }
      } catch(e) { toast('Failed to download', 'err'); }
    }

    // Check Telegram status on load
    fetch('/telegram/status', { cache: 'no-store' }).then(r => r.json()).then(data => {
      const badge = $('telegram-badge');
      if (data.connected) {
        badge.className = 'badge badge-ok';
        badge.textContent = 'connected';
        refreshTelegramChannels();
      } else {
        badge.className = 'badge badge-idle';
        badge.textContent = 'disconnected';
      }
    }).catch(() => {});
  </script>
</body>
</html>
"""


def render_page(csrf_token: str = "", message: str = "", error: str = "") -> bytes:
    page = PAGE_HTML.replace("__CSRF__", html.escape(csrf_token, quote=True))
    page = page.replace("__ERROR__", html.escape(error, quote=True))
    page = page.replace("__ERROR_STYLE__", "block" if error else "none")
    page = page.replace("__MESSAGE__", html.escape(message, quote=True))
    page = page.replace("__MESSAGE_STYLE__", "block" if message else "none")
    return page.encode("utf-8")


def redirect(location: str) -> bytes:
    return f"Redirecting to {html.escape(location)}".encode("utf-8")


def make_handler(queue: DownloadQueue, csrf_token: str, monitor: ChannelMonitor, webhook: WebhookManager, telegram_monitor: TelegramMonitor):
    class ArchiveRequestHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/" or self.path.startswith("/?"):
                self.respond_html(render_page(csrf_token=csrf_token))
                return

            if self.path == "/status":
                job = queue.current()
                self.respond_json({"job": job.snapshot() if job else None})
                return

            if self.path == "/queue":
                self.respond_json({"queue": queue.queue_snapshot()})
                return

            if self.path == "/webhooks":
                self.respond_json({"webhooks": webhook.get_webhooks()})
                return

            if self.path == "/queue/stats":
                self.respond_json(queue.stats())
                return

            if self.path == "/queue/history":
                self.respond_json({"history": queue.history_snapshot()})
                return

            if self.path == "/quiet-hours":
                self.respond_json(queue.get_quiet_hours())
                return

            if self.path == "/files":
                job = queue.current()
                if job:
                    output_dir = job.output_dir
                else:
                    # Use last output dir from history
                    last = queue.last_output_dir()
                    output_dir = last or "archive"
                self.respond_json({"files": list_archive_files(output_dir)})
                return

            if self.path.startswith("/browse"):
                parsed = urlparse(self.path)
                qs = parse_qs(parsed.query)
                path = first_value(qs, "path", str(Path.home()))
                # Restrict browse to home directory and archive
                target = Path(path).expanduser().resolve()
                home = Path.home().resolve()
                archive = Path("archive").resolve()
                if not (target == home or home in target.parents or target == archive or archive in target.parents):
                    path = str(home)
                self.respond_json(browse_filesystem(path))
                return

            if self.path.startswith("/playlist"):
                parsed = urlparse(self.path)
                qs = parse_qs(parsed.query)
                url = first_value(qs, "url", "")
                if not url:
                    self.respond_json({"items": []})
                    return
                # Validate URL against allowed hosts
                url_parsed = urlparse(url)
                if url_parsed.netloc.lower() not in ALLOWED_HOSTS:
                    self.respond_json({"error": "Only YouTube URLs supported"}, status=HTTPStatus.BAD_REQUEST)
                    return
                items = fetch_playlist_items(url)
                self.respond_json({"items": items})
                return

            if self.path == "/channels":
                self.respond_json({"channels": monitor.get_channels()})
                return

            if self.path == "/monitor/status":
                self.respond_json({
                    "running": monitor.is_running,
                    "check_interval": monitor.config.check_interval,
                    "auto_download": monitor.config.auto_download,
                })
                return

            if self.path.startswith("/api/normalize/list"):
                parsed = urlparse(self.path)
                qs = parse_qs(parsed.query)
                dir_path = first_value(qs, "dir", "archive")
                dir_path = str(Path(dir_path).expanduser().resolve())
                exts = ("*.mp3", "*.m4a", "*.opus", "*.flac", "*.wav", "*.vorbis")
                files = []
                seen: set[Path] = set()
                for ext in exts:
                    for p in sorted(Path(dir_path).rglob(ext)):
                        if p in seen:
                            continue
                        seen.add(p)
                        try:
                            files.append({"path": str(p), "size": p.stat().st_size})
                        except OSError:
                            continue
                self.respond_json({"files": files})
                return

            if self.path == "/api/normalize/status":
                self.respond_json({
                    "running": _normalize_worker.running,
                    "progress": _normalize_worker.progress,
                })
                return

            if self.path == "/telegram/status":
                connected = telegram_monitor._client is not None and telegram_monitor._client.is_connected()
                self.respond_json({"connected": connected})
                return

            if self.path == "/telegram/channels":
                self.respond_json({"channels": telegram_monitor.get_channels()})
                return

            if self.path.startswith("/telegram/browse"):
                parsed = urlparse(self.path)
                qs = parse_qs(parsed.query)
                channel = first_value(qs, "channel", "")
                if not channel:
                    self.respond_json({"files": []})
                    return
                try:
                    import asyncio
                    loop = asyncio.new_event_loop()
                    files = loop.run_until_complete(
                        telegram_monitor.browse_channel_audio(int(channel))
                    )
                    loop.close()
                    self.respond_json({"files": files})
                except Exception as e:
                    self.respond_json({"error": str(e)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
                return

            if self.path == "/favicon.ico":
                self.respond_favicon()
                return

            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            if self.path == "/stop":
                length = int(self.headers.get("content-length", "0"))
                if length > MAX_BODY_SIZE:
                    self.send_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                    return
                body = self.rfile.read(length).decode("utf-8")
                form = parse_qs(body, keep_blank_values=True)
                if not hmac.compare_digest(first_value(form, "csrf_token", ""), csrf_token):
                    self.respond_json({"error": "Invalid CSRF token"}, status=HTTPStatus.FORBIDDEN)
                    return
                job = queue.current()
                if job and job.running:
                    job.append_log("\nStopped by user.")
                queue.stop_current()
                self.respond_json({"ok": True})
                return

            # All other POST endpoints require CSRF
            length = int(self.headers.get("content-length", "0"))
            if length > MAX_BODY_SIZE:
                self.send_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                return
            body = self.rfile.read(length).decode("utf-8")
            form = parse_qs(body, keep_blank_values=True)
            if not hmac.compare_digest(first_value(form, "csrf_token", ""), csrf_token):
                self.respond_json({"error": "Invalid CSRF token"}, status=HTTPStatus.FORBIDDEN)
                return

            if self.path == "/monitor/start":
                monitor.start()
                self.respond_json({"ok": True})
                return

            if self.path == "/monitor/stop":
                monitor.stop()
                self.respond_json({"ok": True})
                return

            if self.path == "/channels/add":
                url = first_value(form, "url", "").strip()
                name = first_value(form, "name", "").strip()
                if not url or not name:
                    self.respond_json({"error": "URL and name are required"}, status=HTTPStatus.BAD_REQUEST)
                    return
                parsed = urlparse(url)
                if parsed.netloc.lower() not in ALLOWED_HOSTS:
                    self.respond_json({"error": "Only YouTube URLs are supported"}, status=HTTPStatus.BAD_REQUEST)
                    return
                try:
                    monitor.add_channel(url, name)
                    self.respond_json({"ok": True})
                except ValueError as e:
                    self.respond_json({"error": str(e)}, status=HTTPStatus.BAD_REQUEST)
                return

            if self.path == "/channels/remove":
                url = first_value(form, "url", "")
                if monitor.remove_channel(url):
                    self.respond_json({"ok": True})
                else:
                    self.respond_json({"error": "Channel not found"}, status=HTTPStatus.NOT_FOUND)
                return

            if self.path == "/channels/toggle":
                url = first_value(form, "url", "")
                enabled = first_value(form, "enabled", "1") == "1"
                if monitor.toggle_channel(url, enabled):
                    self.respond_json({"ok": True})
                else:
                    self.respond_json({"error": "Channel not found"}, status=HTTPStatus.NOT_FOUND)
                return

            if self.path == "/channels/schedule":
                url = first_value(form, "url", "")
                schedule_json = first_value(form, "schedule", "[]")
                try:
                    schedule = json.loads(schedule_json)
                    if not isinstance(schedule, list):
                        raise ValueError("Schedule must be a list")
                    for entry in schedule:
                        if not isinstance(entry, list) or len(entry) != 2:
                            raise ValueError("Each entry must be [start, end]")
                        if not (0 <= entry[0] <= 23 and 0 <= entry[1] <= 23):
                            raise ValueError("Hours must be 0-23")
                except (json.JSONDecodeError, ValueError) as e:
                    self.respond_json({"error": f"Invalid schedule: {e}"}, status=HTTPStatus.BAD_REQUEST)
                    return
                if monitor.set_channel_schedule(url, schedule):
                    self.respond_json({"ok": True})
                else:
                    self.respond_json({"error": "Channel not found"}, status=HTTPStatus.NOT_FOUND)
                return

            if self.path == "/channels/quality":
                url = first_value(form, "url", "")
                audio_format = first_value(form, "audio_format", "mp3")
                audio_bitrate = first_value(form, "audio_bitrate", "192k")
                if audio_format not in yt_archive.AUDIO_FORMATS:
                    self.respond_json({"error": "Invalid format"}, status=HTTPStatus.BAD_REQUEST)
                    return
                if monitor.set_channel_quality(url, audio_format, audio_bitrate):
                    self.respond_json({"ok": True})
                else:
                    self.respond_json({"error": "Channel not found"}, status=HTTPStatus.NOT_FOUND)
                return

            if self.path == "/queue/cancel":
                index_str = first_value(form, "index", "-1")
                try:
                    index = int(index_str)
                except ValueError:
                    index = -1
                if queue.cancel_queue_item(index):
                    self.respond_json({"ok": True})
                else:
                    self.respond_json({"error": "Invalid queue index"}, status=HTTPStatus.BAD_REQUEST)
                return

            if self.path == "/queue/clear":
                queue.clear_queue()
                self.respond_json({"ok": True})
                return

            if self.path == "/quiet-hours":
                start_str = first_value(form, "start", "23")
                end_str = first_value(form, "end", "7")
                enabled = first_value(form, "enabled", "0") == "1"
                try:
                    start = int(start_str)
                    end = int(end_str)
                except ValueError:
                    self.respond_json({"error": "Invalid time"}, status=HTTPStatus.BAD_REQUEST)
                    return
                queue.set_quiet_hours(start, end, enabled)
                self.respond_json({"ok": True})
                return

            if self.path == "/rescan":
                # Block rescan while a download is running to prevent archive corruption
                current = queue.current()
                if current and current.running:
                    self.respond_json({"error": "Cannot rescan while a download is running"}, status=HTTPStatus.CONFLICT)
                    return
                try:
                    count = queue.rescan_and_queue()
                    self.respond_json({"ok": True, "missing_count": count})
                except Exception as e:
                    self.respond_json({"error": str(e)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
                return

            if self.path == "/playlists/check":
                try:
                    playlist_results = monitor.check_all_playlists()
                    total_new = sum(len(v) for v in playlist_results.values())
                    self.respond_json({"ok": True, "new_playlists": playlist_results, "total": total_new})
                except Exception as e:
                    self.respond_json({"error": str(e)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
                return

            if self.path == "/webhooks/add":
                url = first_value(form, "url", "").strip()
                name = first_value(form, "name", "").strip()
                events_str = first_value(form, "events", "download_complete,download_failed,new_playlist")
                events = [e.strip() for e in events_str.split(",") if e.strip()]
                if not url:
                    self.respond_json({"error": "URL is required"}, status=HTTPStatus.BAD_REQUEST)
                    return
                try:
                    webhook.add_webhook(url, name=name, events=events)
                    self.respond_json({"ok": True})
                except ValueError as e:
                    self.respond_json({"error": str(e)}, status=HTTPStatus.BAD_REQUEST)
                return

            if self.path == "/webhooks/remove":
                url = first_value(form, "url", "")
                if webhook.remove_webhook(url):
                    self.respond_json({"ok": True})
                else:
                    self.respond_json({"error": "Webhook not found"}, status=HTTPStatus.NOT_FOUND)
                return

            if self.path == "/queue/retry":
                url = first_value(form, "url", "")
                name = first_value(form, "name", "")
                if not url:
                    self.respond_json({"error": "URL is required"}, status=HTTPStatus.BAD_REQUEST)
                    return
                # Try to restore original args from history
                stored = None
                for item in queue.history_snapshot():
                    if item.get("url") == url and item.get("args"):
                        stored = item["args"]
                        break
                if stored:
                    stored.pop("urls", None)
                    args = argparse.Namespace(**stored, urls=[url])
                else:
                    args = argparse.Namespace(
                        urls=[url],
                        output_dir="archive",
                        audio_format="mp3",
                        audio_bitrate="192k",
                        audio_quality="0",
                        download_archive="archive/downloaded.txt",
                        limit=None,
                        playlist_items=None,
                        no_sidecar_metadata=False,
                        dry_run=False,
                        yt_dlp="yt-dlp",
                    )
                queue.enqueue(args, url=url, name=f"Retry: {name}" if name else "Retry")
                self.respond_json({"ok": True})
                return

            if self.path == "/api/normalize/start":
                files_str = first_value(form, "files", "")
                norm_type = first_value(form, "type", "ebu")
                target_str = first_value(form, "target", "-16")
                output_dir = first_value(form, "output_dir", "normalized")
                files = [f.strip() for f in files_str.split("\n") if f.strip()]
                if not files:
                    self.respond_json({"error": "No files selected"}, status=HTTPStatus.BAD_REQUEST)
                    return
                try:
                    target = float(target_str)
                except ValueError:
                    self.respond_json({"error": "Invalid target value"}, status=HTTPStatus.BAD_REQUEST)
                    return
                if _normalize_worker.start(files, norm_type, target, output_dir):
                    self.respond_json({"ok": True})
                else:
                    self.respond_json({"error": "Normalization already running"}, status=HTTPStatus.CONFLICT)
                return

            if self.path == "/api/normalize/stop":
                _normalize_worker.stop()
                self.respond_json({"ok": True})
                return

            if self.path == "/telegram/connect":
                try:
                    import asyncio
                    loop = asyncio.new_event_loop()
                    result = loop.run_until_complete(telegram_monitor.connect())
                    loop.close()
                    if result:
                        self.respond_json({"ok": True})
                    else:
                        self.respond_json({"error": "Failed to connect"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
                except Exception as e:
                    self.respond_json({"error": str(e)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
                return

            if self.path == "/telegram/disconnect":
                try:
                    import asyncio
                    loop = asyncio.new_event_loop()
                    loop.run_until_complete(telegram_monitor.disconnect())
                    loop.close()
                    self.respond_json({"ok": True})
                except Exception as e:
                    self.respond_json({"error": str(e)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
                return

            if self.path == "/telegram/add":
                channel = first_value(form, "channel", "").strip()
                if not channel:
                    self.respond_json({"error": "Channel is required"}, status=HTTPStatus.BAD_REQUEST)
                    return
                try:
                    import asyncio
                    loop = asyncio.new_event_loop()
                    # Get channel info
                    if telegram_monitor._client:
                        entity = loop.run_until_complete(telegram_monitor._client.get_entity(channel))
                        ch = telegram_monitor.add_channel(
                            channel_id=entity.id,
                            name=getattr(entity, "title", channel),
                            username=getattr(entity, "username", ""),
                        )
                        self.respond_json({"ok": True, "channel": {"id": ch.channel_id, "name": ch.name}})
                    else:
                        self.respond_json({"error": "Not connected to Telegram"}, status=HTTPStatus.BAD_REQUEST)
                    loop.close()
                except Exception as e:
                    self.respond_json({"error": str(e)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
                return

            if self.path == "/telegram/remove":
                channel_id_str = first_value(form, "channel_id", "0")
                try:
                    channel_id = int(channel_id_str)
                except ValueError:
                    self.respond_json({"error": "Invalid channel ID"}, status=HTTPStatus.BAD_REQUEST)
                    return
                if telegram_monitor.remove_channel(channel_id):
                    self.respond_json({"ok": True})
                else:
                    self.respond_json({"error": "Channel not found"}, status=HTTPStatus.NOT_FOUND)
                return

            if self.path == "/telegram/download":
                channel = first_value(form, "channel", "")
                message_ids_str = first_value(form, "message_ids", "")
                if not channel or not message_ids_str:
                    self.respond_json({"error": "Channel and message IDs required"}, status=HTTPStatus.BAD_REQUEST)
                    return
                try:
                    message_ids = [int(mid) for mid in message_ids_str.split(",") if mid.strip()]
                except ValueError:
                    self.respond_json({"error": "Invalid message IDs"}, status=HTTPStatus.BAD_REQUEST)
                    return
                try:
                    import asyncio
                    loop = asyncio.new_event_loop()
                    downloaded = loop.run_until_complete(
                        telegram_monitor.download_audio(int(channel), message_ids)
                    )
                    loop.close()
                    self.respond_json({"ok": True, "downloaded": len(downloaded), "files": downloaded})
                except Exception as e:
                    self.respond_json({"error": str(e)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
                return

            if self.path != "/start":
                self.send_error(HTTPStatus.NOT_FOUND)
                return

            try:
                args = form_to_archive_args(form)
                queue.enqueue(args)
            except (RuntimeError, ValueError, argparse.ArgumentTypeError) as exc:
                self.respond_html(render_page(csrf_token=csrf_token, error=str(exc)), status=HTTPStatus.BAD_REQUEST)
                return

            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/")
            self.end_headers()
            self.wfile.write(redirect("/"))

        def respond_html(self, body: bytes, status: HTTPStatus = HTTPStatus.OK) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def respond_json(self, payload: dict[str, object], status: HTTPStatus = HTTPStatus.OK) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def respond_favicon(self) -> None:
            svg = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="6" fill="#ef4444"/><polygon points="12,8 12,24 26,16" fill="#fff"/></svg>'
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("Content-Length", str(len(svg)))
            self.end_headers()
            self.wfile.write(svg)

        def log_message(self, format: str, *args: object) -> None:
            return

    return ArchiveRequestHandler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the local YouTube archiver web UI.")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind. Default: 127.0.0.1")
    parser.add_argument("--port", type=yt_archive.positive_int, default=DEFAULT_PORT, help=f"Port to bind. Default: {DEFAULT_PORT}")
    return parser


def _trigger_download(url: str, channel: Channel, prefix: str = "Auto"):
    """Shared callback to trigger a download when a new video/playlist is detected."""
    queue = _get_queue()
    if queue:
        args = argparse.Namespace(
            urls=[url],
            output_dir="archive",
            audio_format=channel.audio_format,
            audio_bitrate=channel.audio_bitrate,
            audio_quality="0",
            download_archive="archive/downloaded.txt",
            limit=None,
            playlist_items=None,
            no_sidecar_metadata=False,
            dry_run=False,
            yt_dlp="yt-dlp",
        )
        try:
            queue.enqueue(args, url=url, name=f"{prefix}: {channel.name}")
            LOG.info(f"{prefix} download queued: {url} from {channel.name}")
        except Exception as e:
            LOG.warning(f"Monitor: could not queue download for {url}: {e}")


trigger_download = lambda url, ch: _trigger_download(url, ch, "Auto")
trigger_playlist_download = lambda url, ch: _trigger_download(url, ch, "Auto Playlist")


_queue_ref: DownloadQueue | None = None
_webhook_ref: WebhookManager | None = None


def _get_queue() -> DownloadQueue | None:
    return _queue_ref


def _get_webhook() -> WebhookManager | None:
    return _webhook_ref


def main(argv: list[str] | None = None) -> int:
    global _queue_ref, _webhook_ref
    args = build_parser().parse_args(argv)
    queue = DownloadQueue()
    _queue_ref = queue
    webhook = WebhookManager()
    _webhook_ref = webhook

    def on_new_video(url: str, channel: Channel):
        trigger_download(url, channel)

    def on_new_playlist(url: str, channel: Channel):
        trigger_playlist_download(url, channel)
        webhook.notify("new_playlist", {"url": url, "channel": channel.name})

    monitor = ChannelMonitor.load(on_new_video=on_new_video, on_new_playlist=on_new_playlist)
    monitor.set_log_callback(lambda msg: LOG.info(f"[monitor] {msg}"))

    # Initialize Telegram monitor
    tg_config = load_telegram_config()
    telegram_monitor = TelegramMonitor(tg_config)
    telegram_monitor.load_channels()

    server = ThreadingHTTPServer((args.host, args.port), make_handler(queue, secrets.token_urlsafe(32), monitor, webhook, telegram_monitor))
    print(f"Open http://{args.host}:{args.port}")
    print(f"Channel monitor: {'running' if monitor.is_running else 'stopped'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        monitor.stop()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
