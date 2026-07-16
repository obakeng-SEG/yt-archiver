#!/usr/bin/env python3
"""Archive YouTube videos, playlists, or channels as audio files."""

from __future__ import annotations

import argparse
import shlex
import shutil
import subprocess
from pathlib import Path


DEFAULT_OUTPUT_TEMPLATE = "%(uploader|Unknown Uploader)s/%(playlist_title|Singles)s/%(playlist_index|000)s - %(title).200B [%(id)s].%(ext)s"

AUDIO_FORMATS = {"mp3", "m4a", "opus", "flac", "wav", "vorbis"}
AUDIO_BITRATES = {"128k", "192k", "256k", "320k"}
YT_DLP_PATH = "yt-dlp"
ARCHIVE_FILENAME = "downloaded.txt"


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc

    if parsed < 1:
        raise argparse.ArgumentTypeError("must be 1 or greater")

    return parsed


def audio_format(value: str) -> str:
    if value not in AUDIO_FORMATS:
        raise argparse.ArgumentTypeError(f"must be one of: {', '.join(sorted(AUDIO_FORMATS))}")
    return value


def audio_bitrate(value: str) -> str:
    if value not in AUDIO_BITRATES:
        raise argparse.ArgumentTypeError(f"must be one of: {', '.join(sorted(AUDIO_BITRATES))}")
    return value


def mp3_quality(value: str) -> str:
    if value not in {str(number) for number in range(10)}:
        raise argparse.ArgumentTypeError("must be an integer from 0 to 9")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Archive YouTube videos, playlists, and channels as audio files."
    )
    parser.add_argument(
        "urls",
        nargs="+",
        help="One or more YouTube video, playlist, or channel URLs.",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        default="archive",
        help="Directory where archived audio and metadata are stored. Default: archive",
    )
    parser.add_argument(
        "-f",
        "--audio-format",
        type=audio_format,
        default="mp3",
        help="Audio format: mp3, m4a, opus, flac, wav, vorbis. Default: mp3",
    )
    parser.add_argument(
        "-b",
        "--audio-bitrate",
        type=audio_bitrate,
        default=None,
        help="Audio bitrate: 128k, 192k, 256k, 320k. Default: best quality for format",
    )
    parser.add_argument(
        "-q",
        "--audio-quality",
        type=mp3_quality,
        default="0",
        help="FFmpeg VBR quality, 0 is best and 9 is worst. Default: 0",
    )
    parser.add_argument(
        "--download-archive",
        default="downloaded.txt",
        help="File used to skip previously downloaded videos. Relative paths live under output-dir. Default: downloaded.txt",
    )
    parser.add_argument(
        "--limit",
        type=positive_int,
        help="Limit the number of videos downloaded from each URL.",
    )
    parser.add_argument(
        "--playlist-items",
        dest="playlist_items",
        default=None,
        help="Comma-separated playlist item ranges to download, e.g. '1-5,8,10-12'.",
    )
    parser.add_argument(
        "--no-sidecar-metadata",
        dest="no_sidecar_metadata",
        action="store_true",
        help="Do not save sidecar metadata JSON, thumbnails, descriptions, or subtitles.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the yt-dlp command without downloading anything.",
    )
    parser.add_argument(
        "--yt-dlp",
        default="yt-dlp",
        help="Path to the yt-dlp executable. Default: yt-dlp",
    )
    parser.add_argument(
        "--normalize",
        choices=["off", "ebu", "peak", "rms"],
        default="off",
        help="Audio normalization: off, ebu (EBU R128), peak, rms. Default: off",
    )
    parser.add_argument(
        "--normalize-target",
        type=float,
        default=-16.0,
        help="Target level for normalization. EBU: LUFS (-70 to -5), Peak: dBTP (-9 to 0), RMS: dB (-70 to -5). Default: -16.0",
    )
    return parser


def resolve_archive_path(output_dir: Path, archive_arg: str) -> Path:
    archive_path = Path(archive_arg).expanduser()
    if archive_path.is_absolute():
        return archive_path
    return output_dir / archive_path


def build_ytdlp_command(args: argparse.Namespace) -> list[str]:
    output_dir = Path(args.output_dir).expanduser()
    archive_path = resolve_archive_path(output_dir, args.download_archive)

    # Only add --no-playlist for single video URLs to avoid breaking playlist/channel downloads
    has_playlist_url = any(
        "playlist?list=" in url or "/playlist" in url
        or "list=" in url
        for url in args.urls
    )
    no_playlist = [] if has_playlist_url else ["--no-playlist"]

    command = [
        args.yt_dlp,
        "--ignore-errors",
        "--no-overwrites",
        "--continue",
    ] + no_playlist + [
        "--download-archive",
        str(archive_path),
        "--extract-audio",
        "--audio-format",
        args.audio_format,
        "--audio-quality",
        args.audio_quality,
        "--embed-thumbnail",
        "--embed-metadata",
        "--output",
        str(output_dir / DEFAULT_OUTPUT_TEMPLATE),
    ]

    if args.audio_bitrate is not None:
        command.extend(["--postprocessor-args", f"-ab {args.audio_bitrate}"])

    if args.normalize != "off":
        target = args.normalize_target
        if args.normalize == "ebu":
            filter_str = f"loudnorm=I={target}:TP=-1.5:LRA=11"
        elif args.normalize == "peak":
            filter_str = f"loudnorm=I=-24:TP={target}:LRA=7"
        elif args.normalize == "rms":
            filter_str = f"loudnorm=I={target}:TP=-1.5:LRA=7"
        else:
            filter_str = None
        if filter_str:
            command.extend(["--postprocessor-args", f"-af {filter_str}"])

    if not args.no_sidecar_metadata:
        command.extend(
            [
                "--write-info-json",
                "--write-thumbnail",
                "--write-description",
                "--write-subs",
                "--write-auto-subs",
                "--sub-langs",
                "en.*",
            ]
        )

    if args.limit is not None:
        command.extend(["--playlist-end", str(args.limit)])

    if args.playlist_items is not None:
        command.extend(["--playlist-items", args.playlist_items])

    command.extend(args.urls)
    return command


def ensure_runtime_dependencies(ytdlp_executable: str) -> None:
    missing = [name for name in (ytdlp_executable, "ffmpeg") if shutil.which(name) is None]
    if missing:
        joined = ", ".join(missing)
        raise RuntimeError(
            f"Missing required executable(s): {joined}. Install yt-dlp and ffmpeg first."
        )


def run(args: argparse.Namespace) -> int:
    command = build_ytdlp_command(args)

    if args.dry_run:
        print(shlex.join(command))
        return 0

    ensure_runtime_dependencies(args.yt_dlp)
    output_dir = Path(args.output_dir).expanduser()
    archive_path = resolve_archive_path(output_dir, args.download_archive)
    output_dir.mkdir(parents=True, exist_ok=True)
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    return subprocess.run(command, check=False).returncode


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        return run(args)
    except RuntimeError as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
