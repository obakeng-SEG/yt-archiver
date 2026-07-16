#!/usr/bin/env python3
"""Channel monitor for automatic YouTube downloads.

Checks channels periodically for new uploads and triggers downloads.
"""

import json
import logging
import subprocess
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable, Optional

LOG = logging.getLogger("monitor")


@dataclass
class Channel:
    url: str
    name: str
    last_check: float = 0.0
    seen_ids: list[str] = field(default_factory=list)
    known_playlists: list[str] = field(default_factory=list)
    enabled: bool = True
    audio_format: str = "mp3"
    audio_bitrate: str = "192k"
    # Schedule: list of hour ranges when this channel is allowed to check
    # Format: [[start_hour, end_hour], ...] e.g. [[9, 18]] = 9am-6pm
    # Empty list = use global interval (always check)
    schedule: list[list[int]] = field(default_factory=list)


@dataclass
class MonitorConfig:
    channels: list[Channel] = field(default_factory=list)
    check_interval: int = 900  # seconds (15 minutes)
    auto_download: bool = True
    yt_dlp: str = "yt-dlp"
    output_dir: str = "archive"
    download_archive: str = "archive/downloaded.txt"


class ChannelMonitor:
    def __init__(
        self,
        config: MonitorConfig,
        on_new_video: Optional[Callable[[str, Channel], None]] = None,
        on_new_playlist: Optional[Callable[[str, Channel], None]] = None,
    ):
        self.config = config
        self.on_new_video = on_new_video
        self.on_new_playlist = on_new_playlist
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._log_callback: Optional[Callable[[str], None]] = None

    def set_log_callback(self, callback: Callable[[str], None]):
        self._log_callback = callback

    def _log(self, msg: str):
        LOG.info(msg)
        if self._log_callback:
            self._log_callback(msg)

    def add_channel(self, url: str, name: str) -> Channel:
        channel = Channel(url=url, name=name)

        # Seed seen_ids BEFORE adding to config to prevent race with monitor loop
        try:
            cmd = [self.config.yt_dlp, "--flat-playlist", "--dump-json", "--no-warnings", url]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if result.returncode == 0:
                seen = []
                for line in result.stdout.strip().split("\n"):
                    if not line:
                        continue
                    try:
                        info = json.loads(line)
                        vid = info.get("id", "")
                        if vid:
                            seen.append(vid)
                    except json.JSONDecodeError:
                        continue
                channel.seen_ids = seen[-500:]
                self._log(f"Seeded {len(seen)} existing videos for {name}")
        except Exception as e:
            self._log(f"Could not seed seen_ids for {name}: {e}")

        # Seed known playlists
        try:
            cmd = [
                self.config.yt_dlp,
                "--flat-playlist",
                "--dump-json",
                "--no-warnings",
                "--extractor-args", "youtubetab:playlists",
                url,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if result.returncode == 0:
                pl_ids = []
                for line in result.stdout.strip().split("\n"):
                    if not line:
                        continue
                    try:
                        info = json.loads(line)
                        plid = info.get("id", "")
                        if plid and info.get("_type") == "playlist":
                            pl_ids.append(plid)
                    except json.JSONDecodeError:
                        continue
                channel.known_playlists = pl_ids
                self._log(f"Seeded {len(pl_ids)} existing playlists for {name}")
        except Exception as e:
            self._log(f"Could not seed known_playlists for {name}: {e}")

        with self._lock:
            for ch in self.config.channels:
                if ch.url == url:
                    raise ValueError(f"Channel already monitored: {name}")
            self.config.channels.append(channel)
            self._save()
            self._log(f"Added channel: {name} ({url})")

        return channel

    def remove_channel(self, url: str) -> bool:
        with self._lock:
            for i, ch in enumerate(self.config.channels):
                if ch.url == url:
                    removed = self.config.channels.pop(i)
                    self._save()
                    self._log(f"Removed channel: {removed.name}")
                    return True
            return False

    def toggle_channel(self, url: str, enabled: bool) -> bool:
        with self._lock:
            for ch in self.config.channels:
                if ch.url == url:
                    ch.enabled = enabled
                    self._save()
                    self._log(f"{'Enabled' if enabled else 'Disabled'} channel: {ch.name}")
                    return True
            return False

    def set_channel_schedule(self, url: str, schedule: list[list[int]]) -> bool:
        with self._lock:
            for ch in self.config.channels:
                if ch.url == url:
                    ch.schedule = schedule
                    self._save()
                    self._log(f"Updated schedule for {ch.name}: {schedule}")
                    return True
            return False

    def set_channel_quality(self, url: str, audio_format: str, audio_bitrate: str) -> bool:
        with self._lock:
            for ch in self.config.channels:
                if ch.url == url:
                    ch.audio_format = audio_format
                    ch.audio_bitrate = audio_bitrate
                    self._save()
                    self._log(f"Updated quality for {ch.name}: {audio_format} {audio_bitrate}")
                    return True
            return False

    def get_channels(self) -> list[dict]:
        with self._lock:
            return [asdict(ch) for ch in self.config.channels]

    def check_channel(self, channel: Channel) -> list[dict]:
        """Check a channel for new videos. Returns list of new video dicts."""
        self._log(f"Checking channel: {channel.name}")
        try:
            cmd = [
                self.config.yt_dlp,
                "--flat-playlist",
                "--dump-json",
                "--no-warnings",
                channel.url,
            ]
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120
            )
            if result.returncode != 0:
                self._log(f"Error checking {channel.name}: {result.stderr[:200]}")
                return []

            videos = []
            seen_set = set(channel.seen_ids)
            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue
                try:
                    info = json.loads(line)
                    vid = info.get("id", "")
                    if vid and vid not in seen_set:
                        videos.append({
                            "id": vid,
                            "title": info.get("title", "Unknown"),
                            "url": info.get("url", f"https://www.youtube.com/watch?v={vid}"),
                            "duration": info.get("duration", 0),
                        })
                        seen_set.add(vid)
                except json.JSONDecodeError:
                    continue

            # Update seen IDs (keep last 500 per channel to prevent unbounded growth)
            with self._lock:
                channel.seen_ids = list(seen_set)[-500:]
                channel.last_check = time.time()
                self._save()

            self._log(f"Found {len(videos)} new videos from {channel.name}")
            return videos

        except subprocess.TimeoutExpired:
            self._log(f"Timeout checking {channel.name}")
            return []
        except Exception as e:
            self._log(f"Error checking {channel.name}: {e}")
            return []

    def check_all(self) -> dict[str, list[dict]]:
        """Check all enabled channels respecting per-channel schedules. Returns {channel_name: [new_videos]}."""
        results = {}
        with self._lock:
            channels = [ch for ch in self.config.channels if ch.enabled]

        for channel in channels:
            if not self._is_in_schedule(channel.schedule):
                continue
            new_videos = self.check_channel(channel)
            if new_videos:
                results[channel.name] = new_videos

        return results

    def check_new_playlists(self, channel: Channel, mark_known: bool = True) -> list[dict]:
        """Check for new playlists on a channel. Returns list of new playlist dicts.
        If mark_known is True, updates known_playlists (default for monitor loop).
        Set mark_known=False for manual checks to avoid preventing future auto-downloads.
        """
        try:
            cmd = [
                self.config.yt_dlp,
                "--flat-playlist",
                "--dump-json",
                "--no-warnings",
                "--extractor-args", "youtubetab:playlists",
                channel.url,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if result.returncode != 0:
                return []

            known_set = set(channel.known_playlists)
            new_playlists = []
            all_ids = []
            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue
                try:
                    info = json.loads(line)
                    plid = info.get("id", "")
                    if plid and info.get("_type") == "playlist":
                        all_ids.append(plid)
                        if plid not in known_set:
                            new_playlists.append({
                                "id": plid,
                                "title": info.get("title", "Unknown Playlist"),
                                "url": info.get("url", f"https://www.youtube.com/playlist?list={plid}"),
                                "count": info.get("playlist_count", 0),
                            })
                except json.JSONDecodeError:
                    continue

            # Update known playlists preserving insertion order (keep last 200)
            if mark_known and all_ids:
                with self._lock:
                    seen = list(dict.fromkeys(channel.known_playlists + all_ids))
                    channel.known_playlists = seen[-200:]
                    self._save()

            if new_playlists:
                self._log(f"Found {len(new_playlists)} new playlists from {channel.name}")
            return new_playlists

        except subprocess.TimeoutExpired:
            self._log(f"Timeout checking playlists for {channel.name}")
            return []
        except Exception as e:
            self._log(f"Error checking playlists for {channel.name}: {e}")
            return []

    def check_all_playlists(self) -> dict[str, list[dict]]:
        """Check all enabled channels for new playlists respecting schedules. Returns {channel_name: [new_playlists]}."""
        results = {}
        with self._lock:
            channels = [ch for ch in self.config.channels if ch.enabled]

        for channel in channels:
            if not self._is_in_schedule(channel.schedule):
                continue
            new_playlists = self.check_new_playlists(channel)
            if new_playlists:
                results[channel.name] = new_playlists

        return results

    def mark_seen(self, channel_url: str, video_id: str):
        """Mark a video as seen (for manual downloads)."""
        with self._lock:
            for ch in self.config.channels:
                if ch.url == channel_url:
                    if video_id not in ch.seen_ids:
                        ch.seen_ids.append(video_id)
                        self._save()
                    return

    @staticmethod
    def _is_in_schedule(schedule: list[list[int]]) -> bool:
        """Check if current hour falls within any of the scheduled ranges."""
        if not schedule:
            return True  # Empty schedule = always check
        hour = time.localtime().tm_hour
        for start, end in schedule:
            if start <= end:
                if start <= hour < end:
                    return True
            else:
                # Wraps midnight: e.g. [[23, 6]] means 11pm-6am
                if hour >= start or hour < end:
                    return True
        return False

    def _run_loop(self):
        self._log("Channel monitor started")
        while not self._stop_event.is_set():
            try:
                results = self.check_all()
                for channel_name, videos in results.items():
                    for video in videos:
                        self._log(
                            f"New video: {video['title']} from {channel_name}"
                        )
                        if self.on_new_video and self.config.auto_download:
                            # Find the channel config
                            with self._lock:
                                channel = next(
                                    (ch for ch in self.config.channels if ch.name == channel_name),
                                    None,
                                )
                            if channel:
                                self.on_new_video(video["url"], channel)

                # Check for new playlists
                playlist_results = self.check_all_playlists()
                for channel_name, playlists in playlist_results.items():
                    for playlist in playlists:
                        self._log(
                            f"New playlist: {playlist['title']} from {channel_name}"
                        )
                        if self.on_new_playlist and self.config.auto_download:
                            with self._lock:
                                channel = next(
                                    (ch for ch in self.config.channels if ch.name == channel_name),
                                    None,
                                )
                            if channel:
                                self.on_new_playlist(playlist["url"], channel)
            except Exception as e:
                self._log(f"Monitor error: {e}")

            self._stop_event.wait(self.config.check_interval)

        self._log("Channel monitor stopped")

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _save(self):
        data = {
            "channels": [asdict(ch) for ch in self.config.channels],
            "check_interval": self.config.check_interval,
            "auto_download": self.config.auto_download,
            "yt_dlp": self.config.yt_dlp,
            "output_dir": self.config.output_dir,
            "download_archive": self.config.download_archive,
        }
        path = Path("channels.json")
        path.write_text(json.dumps(data, indent=2))

    @classmethod
    def load(
        cls,
        on_new_video: Optional[Callable] = None,
        on_new_playlist: Optional[Callable] = None,
    ) -> "ChannelMonitor":
        path = Path("channels.json")
        if path.exists():
            try:
                data = json.loads(path.read_text())
                channels = [Channel(**ch) for ch in data.get("channels", [])]
                config = MonitorConfig(
                    channels=channels,
                    check_interval=data.get("check_interval", 900),
                    auto_download=data.get("auto_download", True),
                    yt_dlp=data.get("yt_dlp", "yt-dlp"),
                    output_dir=data.get("output_dir", "archive"),
                    download_archive=data.get("download_archive", "archive/downloaded.txt"),
                )
            except Exception as e:
                LOG.warning(f"Could not load channels.json: {e}")
                config = MonitorConfig()
        else:
            config = MonitorConfig()
        return cls(config=config, on_new_video=on_new_video, on_new_playlist=on_new_playlist)
