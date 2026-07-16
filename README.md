# yt-archiver

A powerful audio archiver for YouTube and Telegram with a modern web UI.

Download and organize audio from YouTube videos, playlists, and channels, plus monitor and download audio from Telegram channels.

## Features

- **YouTube Archiving**: Download videos, playlists, and channels as MP3/audio files
- **Telegram Integration**: Monitor and download audio from Telegram channels
- **Web UI**: Beautiful, responsive web interface for managing downloads
- **Audio Normalization**: Normalize volume using EBU R128, peak, or RMS
- **Channel Monitoring**: Automatically check for new uploads
- **Download Queue**: Manage multiple downloads with priority
- **Webhook Support**: Get notifications on Discord, Slack, etc.

## Requirements

- Python 3.10+
- `yt-dlp`
- `ffmpeg`
- `telethon` (for Telegram integration)

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/obakeng-SEG/yt-archiver.git
cd yt-archiver
```

### 2. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 3. Install external tools

**macOS (Homebrew):**
```bash
brew install yt-dlp ffmpeg
```

**Ubuntu/Debian:**
```bash
sudo apt install yt-dlp ffmpeg
```

**Windows (Chocolatey):**
```bash
choco install yt-dlp ffmpeg
```

## Quick Start

### Web UI (Recommended)

Start the web interface:

```bash
python3 web_ui.py
```

Open `http://127.0.0.1:8765` in your browser.

### CLI

Archive a single video:
```bash
python3 yt_archive.py "https://www.youtube.com/watch?v=VIDEO_ID"
```

Archive a playlist:
```bash
python3 yt_archive.py "https://www.youtube.com/playlist?list=PLAYLIST_ID"
```

Archive a channel:
```bash
python3 yt_archive.py "https://www.youtube.com/@ChannelName/videos"
```

## Telegram Setup

### Step 1: Get Telegram API Credentials

1. Go to https://my.telegram.org
2. Enter your phone number (e.g., `+14155552671`)
3. Enter the verification code sent to your Telegram app
4. Click "API development tools"
5. Fill in the form:
   - App title: `yt-archiver`
   - Short name: `ytarchiver`
   - URL: `https://github.com/obakeng-SEG/yt-archiver`
   - Platform: `Other`
6. Click "Create application"
7. Save your `API_ID` and `API_HASH`

### Step 2: Configure Telegram

Edit `telegram_config.json`:

```json
{
  "api_id": YOUR_API_ID,
  "api_hash": "YOUR_API_HASH",
  "session_name": "yt-archiver"
}
```

### Step 3: First Run

On first run, you'll be prompted to enter your phone number and verification code. This creates a session file that persists your login.

### Step 4: Add Telegram Channels

**Via Web UI:**
1. Open the web UI
2. Scroll to the "Telegram" card
3. Enter channel URL (e.g., `@channelname` or `https://t.me/channelname`)
4. Click "Add Channel"

**Via CLI:**
```bash
python3 yt_archive.py --telegram-add "@channelname"
```

### Step 5: Download Audio

**Via Web UI:**
1. Browse channel audio files
2. Select files to download
3. Click "Download"

**Via CLI:**
```bash
# List audio files
python3 yt_archive.py --telegram-list "@channelname"

# Download all audio
python3 yt_archive.py --telegram-download "@channelname"

# Download with limit
python3 yt_archive.py --telegram-download "@channelname" --limit 10
```

## Audio Normalization

### During Download

Select normalization in the Archive card:
- **Off**: No normalization
- **EBU R128 (-16 LUFS)**: Streaming standard
- **EBU R128 (-23 LUFS)**: Broadcast standard
- **Peak (-1.5 dBTP)**: Peak normalization

### Batch Normalization

Normalize existing files:
1. Go to the "Normalize Volume" card
2. Select directory containing audio files
3. Choose normalization type and target
4. Click "Scan" then "Start"

**Via CLI:**
```bash
python3 yt_archive.py --normalize ebu --normalize-target -16 "URL"
```

## Web UI Features

### Archive Card
- Paste multiple YouTube URLs
- Choose output directory, format, bitrate
- Set download limits
- Enable audio normalization

### Output Panel
- Real-time download progress
- Live log output
- Command preview

### Archived Files
- Browse all downloaded audio
- File size information

### Channel Monitor
- Add YouTube channels for auto-monitoring
- Configurable check intervals
- Schedule-based monitoring

### Telegram
- Add Telegram channels
- Browse and download audio
- Real-time monitoring

### Download Queue
- Queue multiple downloads
- Priority management
- Quiet hours support

### Download History
- View past downloads
- Retry failed downloads

## Configuration

### Environment Variables

```bash
# Web UI port
PORT=8765

# Default output directory
OUTPUT_DIR=archive
```

### Configuration Files

| File | Description |
|------|-------------|
| `telegram_config.json` | Telegram API credentials |
| `telegram_channels.json` | Monitored Telegram channels |
| `channels.json` | Monitored YouTube channels |
| `download_history.json` | Download history |
| `webhooks.json` | Webhook configurations |

## CLI Options

```bash
python3 yt_archive.py [options] URL [URL ...]

YouTube Options:
  -o, --output-dir DIR        Archive destination. Default: archive
  -f, --audio-format FORMAT   Audio format: mp3, m4a, opus, flac, wav. Default: mp3
  -b, --audio-bitrate RATE    Audio bitrate: 128k, 192k, 256k, 320k
  -q, --audio-quality VALUE   VBR quality, 0-9 (0=best). Default: 0
  --limit N                   Download only first N videos
  --download-archive FILE     Skip-list file
  --no-sidecar-metadata       Skip metadata, thumbnails, subs
  --normalize TYPE            Normalization: off, ebu, peak, rms
  --normalize-target VALUE    Target level (default: -16.0)
  --dry-run                   Print command without downloading

Telegram Options:
  --telegram-add CHANNEL      Add a Telegram channel to monitor
  --telegram-remove CHANNEL   Remove a Telegram channel
  --telegram-list CHANNEL     List audio files in a channel
  --telegram-download CHANNEL Download audio from a channel
  --telegram-monitor CHANNEL  Start monitoring a channel

Web UI Options:
  --host HOST                 Bind address. Default: 127.0.0.1
  --port PORT                 Bind port. Default: 8765
```

## Output Structure

```
archive/
  downloaded.txt
  Channel Name/
    Playlist Name/
      001 - Video Title [video_id].mp3
      001 - Video Title [video_id].info.json
      001 - Video Title [video_id].description
      001 - Video Title [video_id].webp
  Telegram/
    @channelname/
      2024-01-15 - Song Title.mp3
```

## Tests

Run the test suite:

```bash
python3 -m unittest
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Acknowledgments

- [yt-dlp](https://github.com/yt-dlp/yt-dlp) - YouTube downloader
- [ffmpeg](https://ffmpeg.org/) - Audio/video processing
- [Telethon](https://github.com/LonamiWebs/Telethon) - Telegram client
- [ffmpeg-normalize](https://github.com/slhck/ffmpeg-normalize) - Audio normalization

## Disclaimer

- Only archive content you have rights to download or preserve
- Respect YouTube's Terms of Service
- Respect Telegram's Terms of Service
- The authors are not responsible for misuse

## Support

- [GitHub Issues](https://github.com/obakeng-SEG/yt-archiver/issues)
- [Documentation](https://github.com/obakeng-SEG/yt-archiver/wiki)
