"""Small shared helpers used across yt-archiver modules."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_text(path: str | os.PathLike, text: str) -> None:
    """Write text to a file atomically (temp file + rename).

    Prevents truncated/corrupt JSON state files if the process dies mid-write.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp_name, target)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
