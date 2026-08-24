import argparse
import contextlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yt_archive


class BuildCommandTests(unittest.TestCase):
    def make_args(self, **overrides):
        values = {
            "urls": ["https://www.youtube.com/watch?v=abc123"],
            "output_dir": "archive",
            "audio_format": "mp3",
            "audio_bitrate": None,
            "audio_quality": "0",
            "download_archive": "downloaded.txt",
            "limit": None,
            "playlist_items": None,
            "no_sidecar_metadata": False,
            "dry_run": False,
            "yt_dlp": "yt-dlp",
            "normalize": "off",
            "normalize_target": -16.0,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_builds_mp3_archive_command_for_single_url(self):
        command = yt_archive.build_ytdlp_command(self.make_args())

        self.assertIn("--extract-audio", command)
        self.assertEqual(command[command.index("--audio-format") + 1], "mp3")
        self.assertEqual(command[command.index("--audio-quality") + 1], "0")
        self.assertEqual(command[command.index("--download-archive") + 1], "archive/downloaded.txt")
        self.assertEqual(command[-1], "https://www.youtube.com/watch?v=abc123")

    def test_includes_metadata_by_default(self):
        command = yt_archive.build_ytdlp_command(self.make_args())

        self.assertIn("--write-info-json", command)
        self.assertIn("--write-thumbnail", command)
        self.assertIn("--write-description", command)
        self.assertIn("--write-subs", command)

    def test_can_skip_sidecar_metadata(self):
        command = yt_archive.build_ytdlp_command(self.make_args(no_sidecar_metadata=True))

        self.assertNotIn("--write-info-json", command)
        self.assertNotIn("--write-thumbnail", command)
        self.assertNotIn("--write-description", command)
        self.assertNotIn("--write-subs", command)
        self.assertIn("--embed-metadata", command)

    def test_limit_maps_to_playlist_end(self):
        command = yt_archive.build_ytdlp_command(self.make_args(limit=10))

        self.assertEqual(command[command.index("--playlist-end") + 1], "10")

    def test_playlist_items(self):
        command = yt_archive.build_ytdlp_command(self.make_args(playlist_items="1-5,8,10-12"))

        self.assertEqual(command[command.index("--playlist-items") + 1], "1-5,8,10-12")

    def test_audio_format_m4a(self):
        command = yt_archive.build_ytdlp_command(self.make_args(audio_format="m4a"))

        self.assertEqual(command[command.index("--audio-format") + 1], "m4a")

    def test_audio_bitrate(self):
        command = yt_archive.build_ytdlp_command(self.make_args(audio_bitrate="320k"))

        self.assertIn("--postprocessor-args", command)
        idx = command.index("--postprocessor-args")
        self.assertEqual(command[idx + 1], "-ab 320k")

    def test_no_bitrate_by_default(self):
        command = yt_archive.build_ytdlp_command(self.make_args())

        self.assertNotIn("--postprocessor-args", command)

    def test_absolute_download_archive_is_preserved(self):
        command = yt_archive.build_ytdlp_command(
            self.make_args(download_archive="/tmp/youtube-downloaded.txt")
        )

        self.assertEqual(
            command[command.index("--download-archive") + 1],
            "/tmp/youtube-downloaded.txt",
        )


class RunTests(unittest.TestCase):
    def make_args(self, **overrides):
        values = {
            "urls": ["https://www.youtube.com/playlist?list=PL123"],
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
        return argparse.Namespace(**values)

    @mock.patch("yt_archive.ensure_runtime_dependencies")
    @mock.patch("yt_archive.subprocess.run")
    def test_run_invokes_ytdlp(self, mock_run, mock_dependencies):
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
        args = self.make_args()

        result = yt_archive.run(args)

        self.assertEqual(result, 0)
        mock_dependencies.assert_called_once_with("yt-dlp")
        mock_run.assert_called_once_with(yt_archive.build_ytdlp_command(args), check=False)

    @mock.patch("yt_archive.ensure_runtime_dependencies")
    @mock.patch("yt_archive.subprocess.run")
    def test_run_creates_nested_download_archive_parent(self, mock_run, _mock_dependencies):
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)

        with tempfile.TemporaryDirectory() as tmpdir:
            result = yt_archive.run(
                self.make_args(output_dir=tmpdir, download_archive="state/downloaded.txt")
            )

            self.assertEqual(result, 0)
            self.assertTrue((Path(tmpdir) / "state").is_dir())

    @mock.patch("yt_archive.ensure_runtime_dependencies")
    @mock.patch.object(Path, "mkdir")
    def test_dry_run_does_not_create_directory_or_check_dependencies(self, mock_mkdir, mock_dependencies):
        with contextlib.redirect_stdout(io.StringIO()):
            result = yt_archive.run(self.make_args(dry_run=True))

        self.assertEqual(result, 0)
        mock_mkdir.assert_not_called()
        mock_dependencies.assert_not_called()

    def test_dry_run_uses_posix_shell_quoting(self):
        url = "https://www.youtube.com/watch?v=abc&list=PL123"

        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            result = yt_archive.run(self.make_args(dry_run=True, urls=[url]))

        self.assertEqual(result, 0)
        self.assertIn("'https://www.youtube.com/watch?v=abc&list=PL123'", stdout.getvalue())


class ParserTests(unittest.TestCase):
    def test_rejects_non_positive_limit(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            yt_archive.positive_int("0")

    def test_rejects_invalid_mp3_quality(self):
        parser = yt_archive.build_parser()

        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(["-q", "10", "https://www.youtube.com/watch?v=abc123"])

    def test_rejects_invalid_audio_format(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            yt_archive.audio_format("ogg")

    def test_rejects_invalid_audio_bitrate(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            yt_archive.audio_bitrate("500k")


class NormalizeTests(unittest.TestCase):
    def make_args(self, **overrides):
        values = {
            "urls": ["https://www.youtube.com/watch?v=abc123"],
            "output_dir": "archive",
            "audio_format": "mp3",
            "audio_bitrate": None,
            "audio_quality": "0",
            "download_archive": "downloaded.txt",
            "limit": None,
            "playlist_items": None,
            "no_sidecar_metadata": False,
            "dry_run": False,
            "yt_dlp": "yt-dlp",
            "normalize": "off",
            "normalize_target": -16.0,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_no_normalize_by_default(self):
        command = yt_archive.build_ytdlp_command(self.make_args())
        self.assertNotIn("--postprocessor-args", command)

    def test_ebu_normalize_adds_loudnorm(self):
        command = yt_archive.build_ytdlp_command(
            self.make_args(normalize="ebu", normalize_target=-16.0)
        )
        idx = command.index("--postprocessor-args")
        self.assertEqual(command[idx + 1], "-af loudnorm=I=-16.0:TP=-1.5:LRA=11")

    def test_ebu_broadcast_normalize(self):
        command = yt_archive.build_ytdlp_command(
            self.make_args(normalize="ebu", normalize_target=-23.0)
        )
        idx = command.index("--postprocessor-args")
        self.assertEqual(command[idx + 1], "-af loudnorm=I=-23.0:TP=-1.5:LRA=11")

    def test_peak_normalize(self):
        command = yt_archive.build_ytdlp_command(
            self.make_args(normalize="peak", normalize_target=-1.5)
        )
        idx = command.index("--postprocessor-args")
        self.assertEqual(command[idx + 1], "-af loudnorm=I=-24:TP=-1.5:LRA=7")

    def test_rms_normalize(self):
        command = yt_archive.build_ytdlp_command(
            self.make_args(normalize="rms", normalize_target=-18.0)
        )
        idx = command.index("--postprocessor-args")
        self.assertEqual(command[idx + 1], "-af loudnorm=I=-18.0:TP=-1.5:LRA=7")

    def test_normalize_with_bitrate(self):
        command = yt_archive.build_ytdlp_command(
            self.make_args(audio_bitrate="192k", normalize="ebu", normalize_target=-16.0)
        )
        bitrate_idx = command.index("--postprocessor-args")
        self.assertEqual(command[bitrate_idx + 1], "-ab 192k")
        # loudnorm is added after bitrate
        loudnorm_idx = command.index("--postprocessor-args", bitrate_idx + 1)
        self.assertEqual(command[loudnorm_idx + 1], "-af loudnorm=I=-16.0:TP=-1.5:LRA=11")


class MachineProgressTests(unittest.TestCase):
    def make_args(self, **overrides):
        values = {
            "urls": ["https://www.youtube.com/watch?v=abc123"],
            "output_dir": "archive",
            "audio_format": "mp3",
            "audio_bitrate": None,
            "audio_quality": "0",
            "download_archive": "downloaded.txt",
            "limit": None,
            "playlist_items": None,
            "no_sidecar_metadata": False,
            "dry_run": False,
            "yt_dlp": "yt-dlp",
            "normalize": "off",
            "normalize_target": -16.0,
            "machine_progress": True,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_machine_progress_adds_template(self):
        command = yt_archive.build_ytdlp_command(self.make_args())
        idx = command.index("--progress-template")
        self.assertTrue(command[idx + 1].startswith("download:PROGRESS|"))
        self.assertIn("--newline", command)

    def test_no_machine_progress_for_cli_default(self):
        args = self.make_args()
        delattr(args, "machine_progress")  # CLI parser omits it unless flagged
        command = yt_archive.build_ytdlp_command(args)
        self.assertNotIn("--progress-template", command)


if __name__ == "__main__":
    unittest.main()
