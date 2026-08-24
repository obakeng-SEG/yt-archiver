#!/usr/bin/env python3
"""Telegram channel monitor for audio downloads.

Uses Telethon to connect to Telegram via MTProto API,
monitor channels for audio files, and download them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable, Optional

from utils import atomic_write_text

LOG = logging.getLogger("telegram_monitor")

CONFIG_FILE = "telegram_config.json"
CHANNELS_FILE = "telegram_channels.json"


@dataclass
class TelegramChannel:
    channel_id: int
    name: str
    username: str
    last_check: float = 0.0
    seen_ids: list[int] = field(default_factory=list)
    enabled: bool = True


@dataclass
class TelegramConfig:
    api_id: int = 0
    api_hash: str = ""
    session_name: str = "yt-archiver"


class TelegramMonitor:
    def __init__(
        self,
        config: TelegramConfig,
        output_dir: str = "archive",
        on_new_audio: Optional[Callable[[str, TelegramChannel], None]] = None,
        channels_path: str = CHANNELS_FILE,
    ):
        self.config = config
        self.output_dir = output_dir
        self.on_new_audio = on_new_audio
        self._channels_path = Path(channels_path)
        self._client = None
        self._channels: list[TelegramChannel] = []
        self._log_callback: Optional[Callable[[str], None]] = None
        self._running = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._phone_code_hash: Optional[str] = None
        self._phone: Optional[str] = None
        self._stop_event: Optional[asyncio.Event] = None

    def _ensure_stop_event(self) -> asyncio.Event:
        """Lazily create the stop event on the monitor's event loop."""
        if self._stop_event is None:
            self._stop_event = asyncio.Event()
        return self._stop_event

    def request_stop(self):
        """Signal any running channel monitor to stop (thread-safe)."""
        if self._loop and self._stop_event:
            self._loop.call_soon_threadsafe(self._stop_event.set)

    def start_loop(self):
        """Start a dedicated event loop in a background thread."""
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=self._run_loop, daemon=True)
        self._loop_thread.start()

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def run_async(self, coro):
        """Schedule a coroutine on the dedicated loop and wait for result."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=30)

    def set_log_callback(self, callback: Callable[[str], None]):
        self._log_callback = callback

    def _log(self, msg: str):
        LOG.info(msg)
        if self._log_callback:
            self._log_callback(msg)

    async def connect(self, phone: str = None, code: str = None) -> dict:
        """Multi-step connect to Telegram.

        Args:
            phone: Phone number (optional, triggers phone_code_hash request)
            code: Verification code (optional, completes auth)

        Returns:
            Dict with 'status' key: 'awaiting_phone', 'awaiting_code', or 'connected'
        """
        try:
            from telethon import TelegramClient

            if self._client and self._client.is_connected():
                if await self._client.is_user_authorized():
                    me = await self._client.get_me()
                    self._log(f"Already connected as {me.first_name}")
                    return {"status": "connected", "name": me.first_name}

            if not self._client:
                self._client = TelegramClient(
                    self.config.session_name,
                    self.config.api_id,
                    self.config.api_hash,
                )
                await self._client.connect()

            if not await self._client.is_user_authorized():
                if not phone:
                    return {"status": "awaiting_phone"}

                if phone and not code:
                    sent_code = await self._client.send_code_request(phone)
                    self._phone_code_hash = sent_code.phone_code_hash
                    self._phone = phone
                    return {"status": "awaiting_code"}

                if phone and code:
                    await self._client.sign_in(phone, code, phone_code_hash=self._phone_code_hash)
                    me = await self._client.get_me()
                    self._log(f"Connected to Telegram as {me.first_name}")
                    return {"status": "connected", "name": me.first_name}

            me = await self._client.get_me()
            self._log(f"Connected to Telegram as {me.first_name}")
            return {"status": "connected", "name": me.first_name}

        except Exception as e:
            self._log(f"Failed to connect to Telegram: {e}")
            self._client = None
            return {"status": "error", "error": str(e)}

    async def disconnect(self):
        """Disconnect from Telegram."""
        if self._client:
            await self._client.disconnect()
            self._client = None
            self._log("Disconnected from Telegram")

    async def get_me(self) -> dict:
        """Get current user info."""
        if not self._client:
            return {"error": "Not connected"}
        me = await self._client.get_me()
        return {
            "id": me.id,
            "first_name": me.first_name,
            "last_name": me.last_name,
            "username": me.username,
            "phone": me.phone,
        }

    async def list_channels(self) -> list[dict]:
        """List all channels/groups the user is in."""
        if not self._client:
            return []

        channels = []
        async for dialog in self._client.iter_dialogs():
            if dialog.is_channel or dialog.is_group:
                entity = dialog.entity
                channels.append({
                    "id": entity.id,
                    "name": dialog.name,
                    "username": getattr(entity, "username", None),
                    "type": "channel" if dialog.is_channel else "group",
                    "unread_count": dialog.unread_count,
                })
        return channels

    async def browse_channel_audio(
        self, channel: str | int, limit: int = 50
    ) -> list[dict]:
        """Browse audio files in a channel.

        Args:
            channel: Channel username, invite link, or ID
            limit: Maximum number of messages to check

        Returns:
            List of audio file info dicts
        """
        if not self._client:
            return []

        try:
            entity = await self._client.get_entity(channel)
        except Exception as e:
            self._log(f"Failed to get channel entity: {e}")
            return []

        audio_files = []
        async for message in self._client.iter_messages(entity, limit=limit):
            if message.audio or (message.document and message.document.mime_type and message.document.mime_type.startswith("audio/")):
                audio_info = self._extract_audio_info(message)
                if audio_info:
                    audio_files.append(audio_info)

        return audio_files

    def _extract_audio_info(self, message) -> Optional[dict]:
        """Extract audio file info from a message."""
        try:
            if message.audio:
                doc = message.audio
            elif message.document and message.document.mime_type and message.document.mime_type.startswith("audio/"):
                doc = message.document
            else:
                return None

            # Get filename from attributes
            filename = None
            if hasattr(doc, "attributes"):
                for attr in doc.attributes:
                    if hasattr(attr, "file_name"):
                        filename = attr.file_name
                        break

            if not filename:
                # Try to get from message text or generate from date
                if message.text:
                    # Clean the text for use as filename
                    filename = "".join(c for c in message.text[:100] if c.isalnum() or c in " -_.")
                    filename = filename.strip()
                if not filename:
                    filename = f"audio_{message.id}"

            # Ensure it has an extension
            if not any(filename.endswith(ext) for ext in [".mp3", ".m4a", ".opus", ".flac", ".wav", ".ogg"]):
                filename += ".mp3"

            return {
                "message_id": message.id,
                "filename": filename,
                "size": doc.size if hasattr(doc, "size") else 0,
                "date": message.date.timestamp() if message.date else 0,
                "text": message.text or "",
                "downloaded": False,
            }
        except Exception as e:
            LOG.error(f"Error extracting audio info: {e}")
            return None

    async def download_audio(
        self,
        channel: str | int,
        message_ids: list[int],
        output_dir: Optional[str] = None,
    ) -> list[str]:
        """Download audio files from a channel.

        Args:
            channel: Channel username, invite link, or ID
            message_ids: List of message IDs to download
            output_dir: Output directory (uses default if None)

        Returns:
            List of downloaded file paths
        """
        if not self._client:
            return []

        out_dir = Path(output_dir or self.output_dir).expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)

        try:
            entity = await self._client.get_entity(channel)
        except Exception as e:
            self._log(f"Failed to get channel entity: {e}")
            return []

        downloaded = []
        for msg_id in message_ids:
            try:
                message = await self._client.get_messages(entity, ids=msg_id)
                if not message:
                    continue

                audio_info = self._extract_audio_info(message)
                if not audio_info:
                    continue

                # Determine artist folder from channel name
                channel_name = getattr(entity, "title", "Unknown")
                artist_dir = out_dir / "Telegram" / self._sanitize_filename(channel_name)
                artist_dir.mkdir(parents=True, exist_ok=True)

                filepath = artist_dir / audio_info["filename"]

                # Download the file
                await self._client.download_media(message, file=str(filepath))
                downloaded.append(str(filepath))
                self._log(f"Downloaded: {audio_info['filename']}")

            except Exception as e:
                self._log(f"Failed to download message {msg_id}: {e}")

        return downloaded

    def _sanitize_filename(self, name: str) -> str:
        """Sanitize a string for use as a filename."""
        return "".join(c for c in name if c.isalnum() or c in " -_.")[:100]

    async def monitor_channel(
        self,
        channel: str | int,
        check_interval: int = 300,
        output_dir: Optional[str] = None,
    ):
        """Monitor a channel for new audio files.

        Args:
            channel: Channel username, invite link, or ID
            check_interval: Seconds between checks
            output_dir: Output directory for downloads
        """
        if not self._client:
            self._log("Not connected to Telegram")
            return

        try:
            entity = await self._client.get_entity(channel)
        except Exception as e:
            self._log(f"Failed to get channel entity: {e}")
            return

        channel_name = getattr(entity, "title", str(channel))
        self._log(f"Starting monitor for {channel_name}")

        # Load seen IDs
        seen_ids = set(self._load_seen_ids(channel))
        stop_event = self._ensure_stop_event()

        while not stop_event.is_set():
            try:
                async for message in self._client.iter_messages(entity, limit=20):
                    if message.id in seen_ids:
                        continue

                    audio_info = self._extract_audio_info(message)
                    if audio_info:
                        # Download the new audio
                        out_dir = Path(output_dir or self.output_dir).expanduser()
                        artist_dir = out_dir / "Telegram" / self._sanitize_filename(channel_name)
                        artist_dir.mkdir(parents=True, exist_ok=True)

                        filepath = artist_dir / audio_info["filename"]
                        await self._client.download_media(message, file=str(filepath))
                        self._log(f"New audio: {audio_info['filename']}")

                        seen_ids.add(message.id)

                # Save seen IDs
                self._save_seen_ids(channel, list(seen_ids))

            except Exception as e:
                self._log(f"Monitor error: {e}")

            # Wait for next check
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=check_interval)
                break  # Stop was requested
            except asyncio.TimeoutError:
                pass  # Continue monitoring

    def _load_seen_ids(self, channel: str | int) -> list[int]:
        """Load seen message IDs from file."""
        try:
            path = Path(self.output_dir) / f".telegram_seen_{channel}.json"
            if path.exists():
                with open(path) as f:
                    return json.load(f)
        except Exception:
            pass
        return []

    def _save_seen_ids(self, channel: str | int, ids: list[int]):
        """Save seen message IDs to file."""
        try:
            path = Path(self.output_dir) / f".telegram_seen_{channel}.json"
            atomic_write_text(path, json.dumps(ids))
        except Exception as e:
            self._log(f"Failed to save seen IDs: {e}")

    def load_channels(self) -> list[TelegramChannel]:
        """Load channels from config file."""
        try:
            if self._channels_path.exists():
                with open(self._channels_path) as f:
                    data = json.load(f)
                self._channels = [TelegramChannel(**ch) for ch in data]
        except Exception as e:
            LOG.error(f"Failed to load channels: {e}")
        return self._channels

    def save_channels(self):
        """Save channels to config file."""
        try:
            atomic_write_text(self._channels_path, json.dumps([asdict(ch) for ch in self._channels], indent=2))
        except Exception as e:
            LOG.error(f"Failed to save channels: {e}")

    def add_channel(self, channel_id: int, name: str, username: str = "") -> TelegramChannel:
        """Add a channel to monitor. Updates in place if the ID already exists."""
        for ch in self._channels:
            if ch.channel_id == channel_id:
                ch.name = name
                ch.username = username
                self.save_channels()
                return ch
        ch = TelegramChannel(channel_id=channel_id, name=name, username=username)
        self._channels.append(ch)
        self.save_channels()
        return ch

    def remove_channel(self, channel_id: int) -> bool:
        """Remove a channel from monitoring."""
        for i, ch in enumerate(self._channels):
            if ch.channel_id == channel_id:
                self._channels.pop(i)
                self.save_channels()
                return True
        return False

    def get_channels(self) -> list[dict]:
        """Get all monitored channels."""
        return [asdict(ch) for ch in self._channels]


def load_telegram_config() -> TelegramConfig:
    """Load Telegram configuration from file."""
    try:
        path = Path(CONFIG_FILE)
        if path.exists():
            with open(path) as f:
                data = json.load(f)
            return TelegramConfig(**data)
    except Exception as e:
        LOG.error(f"Failed to load Telegram config: {e}")
    return TelegramConfig()


def save_telegram_config(config: TelegramConfig):
    """Save Telegram configuration to file."""
    try:
        atomic_write_text(CONFIG_FILE, json.dumps(asdict(config), indent=2))
    except Exception as e:
        LOG.error(f"Failed to save Telegram config: {e}")
