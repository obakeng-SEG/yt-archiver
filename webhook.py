#!/usr/bin/env python3
"""Webhook notifications for YouTube archiver events."""

import json
import logging
import threading
import urllib.request
import urllib.error
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

LOG = logging.getLogger("webhook")


@dataclass
class WebhookConfig:
    url: str
    events: list[str] = field(default_factory=lambda: ["download_complete", "download_failed", "new_playlist"])
    enabled: bool = True
    name: str = ""


class WebhookManager:
    def __init__(self):
        self._webhooks: list[WebhookConfig] = []
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        path = Path("webhooks.json")
        if path.exists():
            try:
                data = json.loads(path.read_text())
                self._webhooks = [WebhookConfig(**w) for w in data.get("webhooks", [])]
            except Exception as e:
                LOG.warning(f"Could not load webhooks.json: {e}")

    def _save(self):
        Path("webhooks.json").write_text(
            json.dumps({"webhooks": [asdict(w) for w in self._webhooks]}, indent=2)
        )

    def add_webhook(self, url: str, name: str = "", events: Optional[list[str]] = None) -> WebhookConfig:
        if not url:
            raise ValueError("URL is required")
        if events is None:
            events = ["download_complete", "download_failed", "new_playlist"]
        webhook = WebhookConfig(url=url, name=name or url, events=events)
        with self._lock:
            self._webhooks.append(webhook)
            self._save()
        LOG.info(f"Added webhook: {webhook.name}")
        return webhook

    def remove_webhook(self, url: str) -> bool:
        with self._lock:
            for i, w in enumerate(self._webhooks):
                if w.url == url:
                    self._webhooks.pop(i)
                    self._save()
                    LOG.info(f"Removed webhook: {w.name}")
                    return True
            return False

    def get_webhooks(self) -> list[dict]:
        with self._lock:
            return [asdict(w) for w in self._webhooks]

    def notify(self, event: str, data: dict):
        """Send notification to all webhooks subscribed to this event."""
        with self._lock:
            webhooks = [w for w in self._webhooks if w.enabled and event in w.events]

        for webhook in webhooks:
            threading.Thread(
                target=self._send_webhook,
                args=(webhook, event, data),
                daemon=True,
            ).start()

    def _send_webhook(self, webhook: WebhookConfig, event: str, data: dict):
        """Send a single webhook notification."""
        payload = {
            "event": event,
            "data": data,
        }
        try:
            req = urllib.request.Request(
                webhook.url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                LOG.info(f"Webhook sent to {webhook.name}: {resp.status}")
        except Exception as e:
            LOG.warning(f"Webhook failed for {webhook.name}: {e}")
