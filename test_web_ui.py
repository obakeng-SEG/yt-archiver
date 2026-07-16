import argparse
import contextlib
import io
import unittest
from http import HTTPStatus
from types import SimpleNamespace
from unittest import mock

import web_ui


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


class DownloadQueueTests(unittest.TestCase):
    def test_enqueue_adds_to_queue(self):
        queue = web_ui.DownloadQueue()
        args = SimpleNamespace(
            urls=["https://www.youtube.com/watch?v=abc123"],
            output_dir="archive",
            audio_format="mp3",
            audio_bitrate=None,
            audio_quality="0",
            download_archive="downloaded.txt",
            limit=None,
            playlist_items=None,
            no_sidecar_metadata=True,
            dry_run=False,
            yt_dlp="yt-dlp",
            normalize="off",
            normalize_target=-16.0,
        )

        job = queue.enqueue(args, url="https://www.youtube.com/watch?v=abc123", name="Test")
        self.assertIsNotNone(job)
        self.assertEqual(len(queue.queue_snapshot()), 1)

    def test_cancel_queue_item(self):
        queue = web_ui.DownloadQueue()
        args = SimpleNamespace(
            urls=["https://www.youtube.com/watch?v=abc123"],
            output_dir="archive",
            audio_format="mp3",
            audio_bitrate=None,
            audio_quality="0",
            download_archive="downloaded.txt",
            limit=None,
            playlist_items=None,
            no_sidecar_metadata=True,
            dry_run=False,
            yt_dlp="yt-dlp",
            normalize="off",
            normalize_target=-16.0,
        )

        queue.enqueue(args)
        self.assertTrue(queue.cancel_queue_item(0))
        self.assertEqual(len(queue.queue_snapshot()), 0)

    def test_clear_queue(self):
        queue = web_ui.DownloadQueue()
        args = SimpleNamespace(
            urls=["https://www.youtube.com/watch?v=abc123"],
            output_dir="archive",
            audio_format="mp3",
            audio_bitrate=None,
            audio_quality="0",
            download_archive="downloaded.txt",
            limit=None,
            playlist_items=None,
            no_sidecar_metadata=True,
            dry_run=False,
            yt_dlp="yt-dlp",
            normalize="off",
            normalize_target=-16.0,
        )

        queue.enqueue(args)
        queue.enqueue(args)
        queue.clear_queue()
        self.assertEqual(len(queue.queue_snapshot()), 0)

    def test_quiet_hours(self):
        queue = web_ui.DownloadQueue()
        queue.set_quiet_hours(22, 6, True)
        self.assertEqual(queue.get_quiet_hours(), {"start": 22, "end": 6, "enabled": True})

    def test_stats(self):
        queue = web_ui.DownloadQueue()
        stats = queue.stats()
        self.assertEqual(stats["queue_length"], 0)
        self.assertFalse(stats["running"])


class HandlerTests(unittest.TestCase):
    def make_handler(self, csrf_token="good-token"):
        queue = web_ui.DownloadQueue()
        monitor = web_ui.ChannelMonitor.load()
        webhook = web_ui.WebhookManager()
        handler_class = web_ui.make_handler(queue, csrf_token, monitor, webhook)
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


if __name__ == "__main__":
    unittest.main()
