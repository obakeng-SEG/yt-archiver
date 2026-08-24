import argparse
import contextlib
import io
import os
import tempfile
import unittest
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import web_ui
import webhook as webhook_module


def make_test_queue(tmpdir: str) -> web_ui.DownloadQueue:
    """Queue isolated from repo state: tmp files, worker never auto-starts."""
    return web_ui.DownloadQueue(
        history_path=os.path.join(tmpdir, "download_history.json"),
        state_path=os.path.join(tmpdir, "queue_state.json"),
        autostart_worker=False,
    )


def make_test_args(**overrides) -> SimpleNamespace:
    values = {
        "urls": ["https://www.youtube.com/watch?v=abc123"],
        "output_dir": "archive",
        "audio_format": "mp3",
        "audio_bitrate": None,
        "audio_quality": "0",
        "download_archive": "downloaded.txt",
        "limit": None,
        "playlist_items": None,
        "no_sidecar_metadata": True,
        "dry_run": False,
        "yt_dlp": "yt-dlp",
        "normalize": "off",
        "normalize_target": -16.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class TempCwdTestCase(unittest.TestCase):
    """Runs each test inside a throwaway directory so no repo state is touched."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        old_cwd = os.getcwd()
        os.chdir(self._tmp.name)
        self.addCleanup(os.chdir, old_cwd)
        self.tmpdir = self._tmp.name


class IsolatedHandlerTestCase(TempCwdTestCase):
    """Handler tests whose components read/write only inside tmpdir."""

    def build_handler(self, csrf_token="tok"):
        queue = make_test_queue(self.tmpdir)
        monitor = web_ui.ChannelMonitor.load(
            channels_path=os.path.join(self.tmpdir, "channels.json")
        )
        webhook = web_ui.WebhookManager(os.path.join(self.tmpdir, "webhooks.json"))
        telegram = web_ui.TelegramMonitor(
            web_ui.TelegramConfig(),
            output_dir=self.tmpdir,
            channels_path=os.path.join(self.tmpdir, "telegram_channels.json"),
        )
        handler_class = web_ui.make_handler(queue, csrf_token, monitor, webhook, telegram)
        return handler_class, queue, monitor

    def post(self, handler_class, path, fields, csrf_token="tok"):
        handler = handler_class.__new__(handler_class)
        handler.path = path
        handler.requestline = f"POST {path} HTTP/1.1"
        handler.request_version = "HTTP/1.1"
        pairs = [f"csrf_token={csrf_token}"] + [f"{k}={v}" for k, v in fields.items()]
        body = "&".join(pairs)
        handler.headers = {"content-length": str(len(body))}
        handler.rfile = io.BytesIO(body.encode())
        with mock.patch.object(handler, "respond_json") as respond_json:
            handler.do_POST()
            return respond_json.call_args


class FormParsingTests(unittest.TestCase):
    def test_splits_and_validates_youtube_urls(self):
        urls = web_ui.split_urls(
            "https://www.youtube.com/watch?v=abc123\nhttps://youtu.be/def456\n"
        )

        self.assertEqual(
            urls,
            ["https://www.youtube.com/watch?v=abc123", "https://youtu.be/def456"],
        )

    def test_rejects_non_youtube_urls(self):
        with self.assertRaises(ValueError):
            web_ui.split_urls("https://example.com/video")

    def test_requires_at_least_one_url(self):
        with self.assertRaises(ValueError):
            web_ui.split_urls("\n  \n")

    def test_rejects_too_many_urls(self):
        with self.assertRaises(ValueError):
            web_ui.split_urls("\n".join(f"https://www.youtube.com/watch?v=x{i}" for i in range(51)))

    def test_builds_archive_args_from_form(self):
        args = web_ui.form_to_archive_args(
            {
                "urls": ["https://www.youtube.com/playlist?list=PL123"],
                "output_dir": ["music"],
                "audio_quality": ["3"],
                "download_archive": ["state/downloaded.txt"],
                "limit": ["12"],
                "no_sidecar_metadata": ["on"],
            }
        )

        self.assertEqual(args.urls, ["https://www.youtube.com/playlist?list=PL123"])
        self.assertEqual(args.output_dir, "music")
        self.assertEqual(args.audio_quality, "3")
        self.assertEqual(args.download_archive, "state/downloaded.txt")
        self.assertEqual(args.limit, 12)
        self.assertTrue(args.no_sidecar_metadata)
        self.assertEqual(args.yt_dlp, "yt-dlp")

    def test_ignores_downloader_path_from_form(self):
        args = web_ui.form_to_archive_args(
            {
                "urls": ["https://www.youtube.com/watch?v=abc123"],
                "yt_dlp": ["/bin/echo"],
            }
        )

        self.assertEqual(args.yt_dlp, "yt-dlp")

    def test_rejects_invalid_quality_from_form(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            web_ui.form_to_archive_args(
                {
                    "urls": ["https://www.youtube.com/watch?v=abc123"],
                    "audio_quality": ["10"],
                }
            )

    def test_rejects_path_traversal_in_output_dir(self):
        with self.assertRaises(ValueError):
            web_ui.form_to_archive_args(
                {
                    "urls": ["https://www.youtube.com/watch?v=abc123"],
                    "output_dir": ["../../etc"],
                }
            )

    def test_rejects_path_traversal_in_download_archive(self):
        with self.assertRaises(ValueError):
            web_ui.form_to_archive_args(
                {
                    "urls": ["https://www.youtube.com/watch?v=abc123"],
                    "download_archive": ["../../etc/passwd"],
                }
            )


class DownloadQueueTests(TempCwdTestCase):
    def test_enqueue_adds_to_queue(self):
        queue = make_test_queue(self.tmpdir)
        job = queue.enqueue(
            make_test_args(),
            url="https://www.youtube.com/watch?v=abc123",
            name="Test",
        )
        self.assertIsNotNone(job)
        self.assertEqual(len(queue.queue_snapshot()), 1)

    def test_cancel_queue_item(self):
        queue = make_test_queue(self.tmpdir)
        queue.enqueue(make_test_args())
        self.assertTrue(queue.cancel_queue_item(0))
        self.assertEqual(len(queue.queue_snapshot()), 0)

    def test_clear_queue(self):
        queue = make_test_queue(self.tmpdir)
        queue.enqueue(make_test_args())
        queue.enqueue(make_test_args())
        queue.clear_queue()
        self.assertEqual(len(queue.queue_snapshot()), 0)

    def test_quiet_hours_persist_and_reload(self):
        queue1 = make_test_queue(self.tmpdir)
        queue1.set_quiet_hours(1, 5, True)
        self.assertEqual(queue1.get_quiet_hours(), {"start": 1, "end": 5, "enabled": True})

        # A fresh instance restores settings from the state file
        queue2 = make_test_queue(self.tmpdir)
        self.assertEqual(queue2.get_quiet_hours(), {"start": 1, "end": 5, "enabled": True})

    def test_stats(self):
        queue = make_test_queue(self.tmpdir)
        stats = queue.stats()
        self.assertEqual(stats["queue_length"], 0)
        self.assertFalse(stats["running"])


class HandlerTests(IsolatedHandlerTestCase):
    def make_handler(self, csrf_token="good-token"):
        handler_class, queue, _monitor = self.build_handler(csrf_token)
        return handler_class, queue

    def test_get_root_returns_200_with_csrf(self):
        handler_class, _ = self.make_handler()
        handler = handler_class.__new__(handler_class)
        handler.path = "/"
        handler.headers = {}
        output = io.BytesIO()
        handler.wfile = output

        with mock.patch.object(handler, "send_response"), \
             mock.patch.object(handler, "send_header"), \
             mock.patch.object(handler, "end_headers"), \
             mock.patch.object(handler, "respond_html") as mock_html:
            handler.do_GET()
            body = mock_html.call_args[0][0].decode("utf-8")
            self.assertIn("name=\"csrf_token\" value=\"good-token\"", body)

    def test_get_status_returns_json(self):
        handler_class, _ = self.make_handler()
        handler = handler_class.__new__(handler_class)
        handler.path = "/status"
        handler.headers = {}
        output = io.BytesIO()
        handler.wfile = output

        with mock.patch.object(handler, "send_response"), \
             mock.patch.object(handler, "send_header"), \
             mock.patch.object(handler, "end_headers"), \
             mock.patch.object(handler, "respond_json") as mock_json:
            handler.do_GET()
            mock_json.assert_called_once()
            payload = mock_json.call_args[0][0]
            self.assertIn("job", payload)

    def test_post_rejects_missing_csrf_token(self):
        handler_class, _ = self.make_handler("real-token")
        handler = handler_class.__new__(handler_class)
        handler.path = "/start"
        handler.requestline = "POST /start HTTP/1.1"
        handler.request_version = "HTTP/1.1"
        handler.headers = {"content-length": "50"}
        handler.rfile = io.BytesIO(b"urls=https://www.youtube.com/watch?v=abc123")

        with mock.patch.object(handler, "respond_json") as mock_json:
            handler.do_POST()
            status = mock_json.call_args[1]["status"]
            self.assertEqual(status, HTTPStatus.FORBIDDEN)

    def test_post_rejects_wrong_csrf_token(self):
        handler_class, _ = self.make_handler("real-token")
        handler = handler_class.__new__(handler_class)
        handler.path = "/start"
        handler.requestline = "POST /start HTTP/1.1"
        handler.request_version = "HTTP/1.1"
        handler.headers = {"content-length": "70"}
        handler.rfile = io.BytesIO(b"csrf_token=wrong&urls=https://www.youtube.com/watch?v=abc123")

        with mock.patch.object(handler, "respond_json") as mock_json:
            handler.do_POST()
            status = mock_json.call_args[1]["status"]
            self.assertEqual(status, HTTPStatus.FORBIDDEN)

    def test_post_rejects_oversized_body(self):
        handler_class, _ = self.make_handler("real-token")
        handler = handler_class.__new__(handler_class)
        handler.path = "/start"
        handler.requestline = "POST /start HTTP/1.1"
        handler.request_version = "HTTP/1.1"
        handler.headers = {"content-length": str(web_ui.MAX_BODY_SIZE + 1)}

        with mock.patch.object(handler, "send_error") as mock_error:
            handler.do_POST()
            mock_error.assert_called_with(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)

    def test_post_rejects_unknown_path(self):
        handler_class, _ = self.make_handler("valid-token")
        handler = handler_class.__new__(handler_class)
        handler.path = "/unknown"
        handler.requestline = "POST /unknown HTTP/1.1"
        handler.request_version = "HTTP/1.1"
        handler.headers = {"content-length": "100"}
        handler.rfile = io.BytesIO(b"csrf_token=valid-token")

        with mock.patch.object(handler, "send_error") as mock_error:
            handler.do_POST()
            mock_error.assert_called_with(HTTPStatus.NOT_FOUND)

    def test_respond_html_includes_csp_header(self):
        handler_class, _ = self.make_handler()
        handler = handler_class.__new__(handler_class)
        handler.wfile = io.BytesIO()
        handler.request_version = "HTTP/1.1"
        handler._headers_buffer = []

        with mock.patch.object(handler, "send_response"), \
             mock.patch.object(handler, "send_header") as mock_header, \
             mock.patch.object(handler, "end_headers"):
            handler.respond_html(b"ok")
            headers = {call.args[0]: call.args[1] for call in mock_header.call_args_list}
            self.assertIn("Content-Security-Policy", headers)
            self.assertIn("default-src 'self'", headers["Content-Security-Policy"])


class RenderTests(unittest.TestCase):
    def test_render_page_csrf_token_is_embedded(self):
        page = web_ui.render_page(csrf_token="test-token").decode("utf-8")

        self.assertIn("name=\"csrf_token\" value=\"test-token\"", page)
        self.assertIn("id=\"log\"", page)
        self.assertIn("id=\"archive-form\"", page)

    def test_render_page_has_dark_theme(self):
        page = web_ui.render_page(csrf_token="t").decode("utf-8")

        self.assertIn("--bg: #f3f0eb", page)
        self.assertIn("class=\"log-box\"", page)

    def test_render_page_includes_files_panel(self):
        page = web_ui.render_page(csrf_token="t").decode("utf-8")

        self.assertIn("id=\"files-list\"", page)
        self.assertIn("/files", page)


class SafePathTests(unittest.TestCase):
    def test_rejects_dotdot_segments(self):
        with self.assertRaises(ValueError):
            web_ui.safe_path("../../etc", "test")

    def test_rejects_hidden_dotdot(self):
        with self.assertRaises(ValueError):
            web_ui.safe_path("foo/../../bar", "test")

    def test_accepts_simple_relative_path(self):
        result = web_ui.safe_path("archive", "test")
        self.assertEqual(result, "archive")

    def test_expands_user_home(self):
        result = web_ui.safe_path("~/music", "test")
        self.assertTrue(result.startswith("~/") or result.startswith("/"))
        self.assertIn("music", result)


class RegressionTests(IsolatedHandlerTestCase):
    def test_monitor_style_args_build_command(self):
        # C1: monitor-triggered downloads used to raise AttributeError('normalize')
        args = web_ui.default_archive_args(
            urls=["https://www.youtube.com/watch?v=abc123"],
            audio_format="opus",
            audio_bitrate="128k",
        )
        command = web_ui.yt_archive.build_ytdlp_command(args)
        self.assertIn("--extract-audio", command)
        # normalize defaults off: only the -ab bitrate postprocessor arg appears
        self.assertNotIn("-af loudnorm", " ".join(command))

    def test_rescan_and_queue_restores_missing_videos(self):
        # C2: /rescan used to 500 whenever anything was missing
        queue = make_test_queue(self.tmpdir)
        archive_dir = Path(self.tmpdir) / "archive"
        archive_dir.mkdir()
        (archive_dir / "downloaded.txt").write_text(
            "youtube missing00001\nyoutube present0002\n"
        )
        (archive_dir / "001 - Song [present0002].mp3").write_bytes(b"x")

        count = queue.rescan_and_queue(str(archive_dir), str(archive_dir / "downloaded.txt"))

        self.assertEqual(count, 1)
        self.assertEqual(len(queue.queue_snapshot()), 1)
        remaining = (archive_dir / "downloaded.txt").read_text()
        self.assertNotIn("missing00001", remaining)
        self.assertIn("present0002", remaining)

    def test_scan_missing_requires_exact_id_token(self):
        # M1: substring match wrongly treated renamed files as present
        queue = make_test_queue(self.tmpdir)
        archive_dir = Path(self.tmpdir) / "archive"
        archive_dir.mkdir()
        # Contains id as plain substring but NOT as "[id]" -> genuinely missing
        (archive_dir / "renamed dup1234567890 file.mp3").write_bytes(b"x")
        (archive_dir / "001 - Song [exactid0001].mp3").write_bytes(b"x")
        (archive_dir / "downloaded.txt").write_text(
            "youtube dup123456789\nyoutube exactid0001\n"
        )

        missing = queue.scan_missing(str(archive_dir), str(archive_dir / "downloaded.txt"))

        self.assertEqual(missing, ["dup123456789"])

    def test_clean_archive_removes_only_listed_ids(self):
        queue = make_test_queue(self.tmpdir)
        archive_file = Path(self.tmpdir) / "downloaded.txt"
        archive_file.write_text(
            "youtube killme00001\nyoutube keepme00002\ntwitter 12345\n"
        )

        removed = queue.clean_archive(["killme00001"], str(archive_file))

        self.assertEqual(removed, 1)
        content = archive_file.read_text()
        self.assertNotIn("killme00001", content)
        self.assertIn("keepme00002", content)
        self.assertIn("twitter 12345", content)


class ChannelsQualityEndpointTests(IsolatedHandlerTestCase):
    def test_rejects_invalid_bitrate(self):
        # H3: unvalidated bitrate reached yt-dlp postprocessor args verbatim
        handler_class, _, monitor = self.build_handler()
        monitor.config.channels.append(
            web_ui.Channel(url="https://www.youtube.com/@test", name="Test")
        )
        call = self.post(
            handler_class,
            "/channels/quality",
            {"url": "https://www.youtube.com/@test", "audio_format": "mp3", "audio_bitrate": "999k; evil"},
        )
        self.assertEqual(call[1]["status"], HTTPStatus.BAD_REQUEST)

    def test_accepts_valid_bitrate_and_defaults_empty(self):
        handler_class, _, monitor = self.build_handler()
        monitor.config.channels.append(
            web_ui.Channel(url="https://www.youtube.com/@test", name="Test")
        )
        call = self.post(
            handler_class,
            "/channels/quality",
            {"url": "https://www.youtube.com/@test", "audio_format": "mp3", "audio_bitrate": "192k"},
        )
        self.assertTrue(call[0][0]["ok"])

        call = self.post(
            handler_class,
            "/channels/quality",
            {"url": "https://www.youtube.com/@test", "audio_format": "opus", "audio_bitrate": ""},
        )
        self.assertTrue(call[0][0]["ok"])
        channel = monitor.config.channels[0]
        self.assertEqual(channel.audio_format, "opus")
        self.assertEqual(channel.audio_bitrate, "192k")


class HostHeaderTests(IsolatedHandlerTestCase):
    def build_handler_with_hosts(self, allowed_hosts):
        queue = make_test_queue(self.tmpdir)
        monitor = web_ui.ChannelMonitor.load(
            channels_path=os.path.join(self.tmpdir, "channels.json")
        )
        webhook = web_ui.WebhookManager(os.path.join(self.tmpdir, "webhooks.json"))
        telegram = web_ui.TelegramMonitor(
            web_ui.TelegramConfig(),
            output_dir=self.tmpdir,
            channels_path=os.path.join(self.tmpdir, "telegram_channels.json"),
        )
        return web_ui.make_handler(
            queue, "tok", monitor, webhook, telegram, allowed_hosts=allowed_hosts
        )

    def test_mismatched_host_is_rejected(self):
        # H5: DNS-rebinding defense
        handler_class = self.build_handler_with_hosts({"127.0.0.1:8765", "localhost:8765"})
        handler = handler_class.__new__(handler_class)
        handler.path = "/status"
        handler.headers = {"Host": "evil.example.com"}
        with mock.patch.object(handler, "send_response"), \
             mock.patch.object(handler, "send_header"), \
             mock.patch.object(handler, "end_headers"), \
             mock.patch.object(handler, "respond_json") as respond_json:
            handler.do_GET()
            self.assertEqual(respond_json.call_args[1]["status"], HTTPStatus.FORBIDDEN)

    def test_matching_host_passes_through_to_route(self):
        handler_class = self.build_handler_with_hosts({"127.0.0.1:8765"})
        handler = handler_class.__new__(handler_class)
        handler.path = "/status"
        handler.headers = {"Host": "127.0.0.1:8765"}
        with mock.patch.object(handler, "respond_json") as respond_json:
            handler.do_GET()
            self.assertIn("job", respond_json.call_args[0][0])


class WebhookPayloadTests(TempCwdTestCase):
    def test_discord_gets_content_field(self):
        payload = webhook_module.build_webhook_payload(
            "https://discord.com/api/webhooks/1/token", "download_complete", {"url": "u"}
        )
        self.assertIn("content", payload)

    def test_slack_gets_text_field(self):
        payload = webhook_module.build_webhook_payload(
            "https://hooks.slack.com/services/T/B/X", "download_failed", {"url": "u"}
        )
        self.assertIn("text", payload)

    def test_unknown_host_gets_raw_event_payload(self):
        payload = webhook_module.build_webhook_payload(
            "https://example.com/hook", "new_playlist", {"url": "u"}
        )
        self.assertEqual(payload, {"event": "new_playlist", "data": {"url": "u"}})

    def test_duplicate_webhook_url_rejected(self):
        manager = web_ui.WebhookManager(os.path.join(self.tmpdir, "webhooks.json"))
        manager.add_webhook("https://example.com/hook", name="one")
        with self.assertRaises(ValueError):
            manager.add_webhook("https://example.com/hook", name="two")


class TelegramDedupeTests(TempCwdTestCase):
    def test_add_channel_updates_existing_id(self):
        from telegram_monitor import TelegramConfig, TelegramMonitor

        monitor = TelegramMonitor(
            TelegramConfig(),
            output_dir=self.tmpdir,
            channels_path=os.path.join(self.tmpdir, "telegram_channels.json"),
        )
        first = monitor.add_channel(channel_id=42, name="Original", username="orig")
        second = monitor.add_channel(channel_id=42, name="Renamed", username="new")

        self.assertIs(first, second)
        self.assertEqual(len(monitor.get_channels()), 1)
        self.assertEqual(second.name, "Renamed")


if __name__ == "__main__":
    unittest.main()
