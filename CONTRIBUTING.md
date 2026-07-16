# Contributing to yt-archiver

Thank you for considering contributing to yt-archiver! This document provides guidelines and information for contributors.

## Development Setup

### Prerequisites

- Python 3.10+
- `yt-dlp`
- `ffmpeg`
- Git

### Installation

1. Clone the repository:
```bash
git clone https://github.com/obakeng-SEG/yt-archiver.git
cd yt-archiver
```

2. Create a virtual environment:
```bash
python3 -m venv venv
source venv/bin/activate  # On macOS/Linux
```

3. Install dependencies:
```bash
pip install -r requirements.txt
```

4. Install external tools:
```bash
# macOS
brew install yt-dlp ffmpeg

# Ubuntu/Debian
sudo apt install yt-dlp ffmpeg

# Windows (with Chocolatey)
choco install yt-dlp ffmpeg
```

### Running Tests

```bash
python3 -m unittest
```

### Running the Application

```bash
# Start web UI
python3 web_ui.py

# Or use CLI
python3 yt_archive.py "https://www.youtube.com/watch?v=VIDEO_ID"
```

## Code Style

### Python

- Follow PEP 8 guidelines
- Use type hints where appropriate
- Keep functions focused and concise
- Use descriptive variable names
- Add docstrings for public functions

### Example

```python
def download_video(url: str, output_dir: str = "archive") -> bool:
    """Download a YouTube video as audio.
    
    Args:
        url: YouTube video URL
        output_dir: Output directory path
        
    Returns:
        True if download succeeded, False otherwise
    """
    # Implementation here
    pass
```

## Pull Request Process

1. **Fork the repository** and create a feature branch
2. **Make your changes** following the code style guidelines
3. **Add tests** for new functionality
4. **Run the test suite** to ensure nothing is broken
5. **Update documentation** if needed
6. **Submit a pull request** with a clear description

### Commit Messages

Use clear, descriptive commit messages:

```
Add Telegram channel monitoring

- Add telegram_monitor.py for Telethon integration
- Update web_ui.py with Telegram card
- Add API endpoints for channel management
- Update README with Telegram setup instructions
```

### PR Description

Include:
- What the PR does
- Why the change is needed
- How to test the changes
- Any breaking changes

## Reporting Issues

When reporting issues, please include:

- **Environment**: OS, Python version
- **Steps to reproduce**: Clear steps to trigger the issue
- **Expected behavior**: What should happen
- **Actual behavior**: What actually happens
- **Error messages**: Full error output if applicable

## Feature Requests

We welcome feature requests! Please:

1. Check existing issues to avoid duplicates
2. Provide a clear description of the feature
3. Explain the use case
4. Consider implementation details

## License

By contributing, you agree that your contributions will be licensed under the MIT License.

## Questions?

If you have questions, feel free to open an issue or reach out to the maintainers.
