#!/usr/bin/env python3
"""Automated media library organizer.

Watches the archive sources for audio files, inspects each one (mutagen tags
when available, yt-dlp sidecar JSON and filename patterns as fallback), and
copies it into an ordered Artist/Album/NN - Song structure under the library
directory. Runs continuously in a background thread and reacts to new files
within one poll interval (plus explicit triggers from the download pipeline).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Callable, Optional

from utils import atomic_write_text

LOG = logging.getLogger("media_library")

AUDIO_EXTS = {".mp3", ".m4a", ".opus", ".flac", ".wav", ".vorbis", ".ogg"}
HASH_CHUNK = 256 * 1024

# yt-dlp DEFAULT_OUTPUT_TEMPLATE leaf name: "001 - Title [video_id].mp3"
YTDLP_NAME_RE = re.compile(
    r"^(?P<idx>\d+)\s*-\s*(?P<title>.+?)\s*\[(?P<vid>[A-Za-z0-9_-]{6,20})\]$"
)

try:  # Optional dependency: real music tags win over heuristics when present.
    from mutagen import File as _MutagenFile
except ImportError:  # pragma: no cover - exercised only without mutagen
    _MutagenFile = None


def sanitize_component(name: str, max_len: int = 120) -> str:
    """Make a string safe for use as a single path component."""
    cleaned = "".join(c if (c.isalnum() or c in " -_.()&',@") else "_" for c in name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ._")
    return cleaned[:max_len] or "Unknown"


def quick_hash(path: Path) -> str:
    """Cheap content fingerprint: head bytes + file size. Sufficient locally."""
    h = hashlib.sha1()
    size = path.stat().st_size
    with path.open("rb") as f:
        h.update(f.read(HASH_CHUNK))
    h.update(str(size).encode())
    return h.hexdigest()


@dataclass
class TrackInfo:
    src: str
    artist: str = "Unknown Artist"
    album: str = "Singles"
    title: str = ""
    track_no: Optional[int] = None
    year: Optional[str] = None
    ext: str = ".mp3"
    size: int = 0
    mtime_ns: int = 0
    hash: str = ""
    video_id: str = ""
    dest: str = ""
    source: str = "manual"  # youtube | telegram | manual


@dataclass
class ScanReport:
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    duplicates: int = 0
    replaced: int = 0
    failed: int = 0
    pruned: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["errors"] = self.errors[-20:]
        return d


class MediaLibrary:
    """Indexes and organizes audio files into Artist/Album/NN - Song layout."""

    def __init__(
        self,
        sources: tuple[str, ...] = ("archive",),
        library_dir: str = "library",
        index_path: str = "library_index.json",
        poll_interval: float = 20.0,
        log_callback: Optional[Callable[[str], None]] = None,
    ):
        self.sources = [Path(s) for s in sources]
        self.library_dir = Path(library_dir)
        self.index_path = Path(index_path)
        self.poll_interval = poll_interval
        self._log_callback = log_callback
        self._lock = threading.Lock()
        self._entries: dict[str, dict] = {}   # abs src path -> TrackInfo dict
        self._last_report: dict = {}
        self._scanning = False
        self._stop_event = threading.Event()
        self._wake = threading.Event()
        self._full_rescan_requested = False
        self._thread: threading.Thread | None = None
        self._load_index()

    # ------------------------------------------------------------- persistence
    def _load_index(self):
        if not self.index_path.exists():
            return
        try:
            data = json.loads(self.index_path.read_text())
            entries = data.get("entries", {})
            if isinstance(entries, dict):
                self._entries = entries
        except Exception as e:
            LOG.warning(f"Could not load library index: {e}")

    def _save_index(self):
        try:
            atomic_write_text(
                self.index_path,
                json.dumps({"version": 1, "entries": self._entries}, indent=2),
            )
        except Exception as e:
            LOG.warning(f"Could not save library index: {e}")

    def _log(self, msg: str):
        LOG.info(msg)
        if self._log_callback:
            self._log_callback(msg)

    # ------------------------------------------------------------ extraction
    @staticmethod
    def _read_sidecar(p: Path) -> dict:
        sidecar = p.with_suffix(".info.json")
        if sidecar.exists():
            try:
                return json.loads(sidecar.read_text())
            except Exception:
                pass
        return {}

    @staticmethod
    def _read_tags(p: Path) -> dict:
        if _MutagenFile is None:
            return {}
        try:
            audio = _MutagenFile(p, easy=True)
            if audio is None or not audio.tags:
                return {}
            get = lambda k: (audio.get(k) or [None])[0]
            return {
                "artist": get("artist"),
                "album": get("album"),
                "title": get("title"),
                "date": get("date"),
                "tracknumber": get("tracknumber"),
            }
        except Exception:
            return {}

    def extract_info(self, p: Path, source_root: Path) -> TrackInfo:
        rel = p.relative_to(source_root)
        parts = rel.parts
        stem = p.stem
        idx = None
        vid = ""
        m = YTDLP_NAME_RE.match(stem)
        if m:
            idx = int(m.group("idx"))
            stem = m.group("title")
            vid = m.group("vid")

        sidecar = self._read_sidecar(p)
        tags = self._read_tags(p)

        is_telegram = parts[0] == "Telegram" and len(parts) > 2
        channel_dir = parts[1] if is_telegram else (parts[0] if len(parts) > 1 else source_root.name)

        artist = tags.get("artist") or sidecar.get("uploader") or channel_dir or "Unknown Artist"
        if is_telegram:
            album = tags.get("album") or "Telegram"
        else:
            playlist_title = sidecar.get("playlist_title")
            if tags.get("album"):
                album = tags["album"]
            elif playlist_title:
                album = playlist_title
            elif len(parts) > 2:
                album = parts[-2]
            else:
                album = "Singles"
        title = tags.get("title") or sidecar.get("title") or stem or p.stem
        track_no = idx
        raw_track = tags.get("tracknumber")
        if raw_track:
            try:
                track_no = int(str(raw_track).split("/")[0])
            except ValueError:
                pass
        year = None
        for date_src in (tags.get("date"), str(sidecar.get("upload_date") or "")):
            digits = re.sub(r"\D", "", date_src or "")
            if len(digits) >= 4:
                year = digits[:4]
                break

        st = p.stat()
        return TrackInfo(
            src=str(p),
            artist=sanitize_component(artist),
            album=sanitize_component(album),
            title=sanitize_component(title),
            track_no=track_no,
            year=year,
            ext=p.suffix.lower(),
            size=st.st_size,
            mtime_ns=st.st_mtime_ns,
            video_id=vid,
            source="telegram" if is_telegram else ("youtube" if sidecar or vid else "manual"),
        )

    # -------------------------------------------------------------- organizing
    def _dest_path(self, info: TrackInfo) -> Path:
        lead = f"{info.track_no:02d} - " if info.track_no else ""
        name = sanitize_component(f"{lead}{info.title}") + info.ext
        return self.library_dir / info.artist / info.album / name

    def _place(self, info: TrackInfo, report: ScanReport) -> tuple[Path, bool]:
        """Copy the file into the library. Returns (dest_path, copied_ok)."""
        dest = self._dest_path(info)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            if quick_hash(dest) == info.hash:
                return dest, True  # already organized, identical content
            suffix = f" [{info.video_id or info.hash[:10]}]"
            dest = dest.with_name(f"{dest.stem}{suffix}{dest.suffix}")
            i = 2
            while dest.exists():
                dest = dest.with_name(f"{dest.stem.rsplit(' [', 1)[0]}{suffix}~{i}{dest.suffix}")
                i += 1
        try:
            dest.write_bytes(Path(info.src).read_bytes())
        except OSError as e:
            report.failed += 1
            report.errors.append(f"copy failed {info.src}: {e}")
            return dest, False
        return dest, True

    # ------------------------------------------------------------------ scans
    @staticmethod
    def _identity(entry: dict) -> tuple:
        """Musical identity of a track: same artist+album+title = same song.

        Multiple encodes/rips of one song collapse into a single library copy;
        the largest file (proxy for highest quality) wins.
        """
        return (
            str(entry.get("artist", "")).casefold(),
            str(entry.get("album", "")).casefold(),
            str(entry.get("title", "")).casefold(),
        )

    @staticmethod
    def _entry_to_info(e: dict) -> TrackInfo:
        fields = {f for f in TrackInfo.__dataclass_fields__}
        return TrackInfo(**{k: v for k, v in e.items() if k in fields})

    def scan_now(self, full: bool = False) -> dict:
        """Synchronous scan. Incremental skips files whose signature is unchanged."""
        with self._lock:
            self._scanning = True
        report = ScanReport()
        try:
            seen_paths: set[str] = set()
            known_hashes: dict[str, str] = {
                e["hash"]: src for src, e in self._entries.items() if e.get("hash")
            }
            # Current library holder per musical identity (largest wins if
            # state ever got inconsistent).
            holders: dict[tuple, str] = {}
            for src, e in self._entries.items():
                if not e.get("dest"):
                    continue
                k = self._identity(e)
                cur = holders.get(k)
                if cur is None or e["size"] > self._entries[cur]["size"]:
                    holders[k] = src

            for source_root in self.sources:
                root = Path(source_root).expanduser()
                if not root.exists():
                    continue
                for p in sorted(root.rglob("*")):
                    if self._stop_event.is_set():
                        break
                    if not p.is_file() or p.suffix.lower() not in AUDIO_EXTS:
                        continue
                    sp = str(p.resolve())
                    seen_paths.add(sp)
                    try:
                        st = p.stat()
                    except OSError:
                        continue
                    if st.st_size == 0:
                        report.failed += 1
                        report.errors.append(f"empty file skipped: {p}")
                        continue
                    sig = (st.st_size, st.st_mtime_ns)
                    prev = self._entries.get(sp)
                    if not full and prev and (prev.get("size"), prev.get("mtime_ns")) == sig:
                        report.unchanged += 1
                        continue

                    info = self.extract_info(p, root)
                    info.hash = quick_hash(p)

                    # Fast path: byte-identical content already tracked.
                    if info.hash in known_hashes and known_hashes[info.hash] != sp:
                        report.duplicates += 1
                        entry = asdict(info)
                        # Keep collapsed even though the labels differ, so the
                        # consistency sweep never resurrects it as its own song.
                        entry["content_dup_of"] = known_hashes[info.hash]
                        with self._lock:
                            self._entries[sp] = entry
                        continue

                    k = self._identity(asdict(info))
                    holder = holders.get(k)
                    if holder and holder != sp and self._entries.get(holder, {}).get("dest"):
                        holder_size = self._entries[holder].get("size", 0)
                        if info.size <= holder_size:
                            # Same song, lower/equal quality than what we have: drop.
                            report.duplicates += 1
                            entry = asdict(info)
                            entry["superseded_by"] = holder
                            with self._lock:
                                self._entries[sp] = entry
                            continue
                        # Higher quality than the current copy: replace it.
                        old_dest = self._entries[holder].get("dest", "")
                        if old_dest:
                            try:
                                Path(old_dest).unlink(missing_ok=True)
                            except OSError as e:
                                report.errors.append(f"could not remove old copy {old_dest}: {e}")
                        with self._lock:
                            self._entries[holder]["dest"] = ""
                            self._entries[holder]["superseded_by"] = sp
                        holders[k] = sp
                        was_new = sp not in self._entries
                        dest, ok = self._place(info, report)
                        info.dest = str(dest)
                        if ok:
                            report.replaced += 1
                            self._log(
                                f"Library: upgraded {info.artist} / {info.album} / {info.title} "
                                f"(larger copy replaces {Path(holder).name})"
                            )
                        with self._lock:
                            self._entries[sp] = asdict(info)
                        continue

                    was_new = sp not in self._entries
                    dest, ok = self._place(info, report)
                    info.dest = str(dest)
                    if ok:
                        known_hashes.setdefault(info.hash, sp)
                        holders.setdefault(k, sp)
                        if was_new:
                            report.added += 1
                            self._log(f"Library: + {info.artist} / {info.album} / {info.title}")
                        else:
                            report.updated += 1
                    with self._lock:
                        self._entries[sp] = asdict(info)

            # Prune index entries whose source files disappeared.
            with self._lock:
                gone = [k for k in self._entries if k not in seen_paths]
                for k in gone:
                    del self._entries[k]
                report.pruned = len(gone)

            # Consistency sweep per identity: exactly one library copy — the
            # largest surviving source. Promotes after deletions, demotes
            # leftovers, and repairs partial states from interrupted scans.
            best_by_id: dict[tuple, str] = {}
            for src, e in self._entries.items():
                if e.get("content_dup_of"):
                    continue
                k2 = self._identity(e)
                cur = best_by_id.get(k2)
                if cur is None or e["size"] > self._entries[cur]["size"]:
                    best_by_id[k2] = src
            claimed_dests = {e["dest"] for e in self._entries.values() if e.get("dest")}
            for k2, bsrc in best_by_id.items():
                holder_srcs = [s for s, e in self._entries.items()
                               if e.get("dest") and self._identity(e) == k2]
                if holder_srcs == [bsrc]:
                    continue
                for hs in holder_srcs:
                    d = self._entries[hs].get("dest")
                    if d:
                        try:
                            Path(d).unlink(missing_ok=True)
                        except OSError:
                            pass
                    self._entries[hs]["dest"] = ""
                promoted_from = bool(holder_srcs)
                be = self._entry_to_info(self._entries[bsrc])
                be.hash = self._entries[bsrc].get("hash", "")
                # Reclaim an orphaned library file left behind by a pruned
                # source: nothing in the index owns it anymore, and keeping it
                # would force the promoted copy into a suffixed filename.
                intended = self._dest_path(be)
                if intended.exists() and str(intended) not in claimed_dests:
                    try:
                        intended.unlink()
                    except OSError:
                        pass
                dest, ok = self._place(be, report)
                if ok:
                    self._entries[bsrc]["dest"] = str(dest)
                    self._log(f"Library: now using {Path(bsrc).name} for "
                              f"{be.artist} / {be.album} / {be.title}")
                    if promoted_from:
                        report.replaced += 1
                    else:
                        report.added += 1

            with self._lock:
                self._save_index()
                self._last_report = report.as_dict()
        finally:
            with self._lock:
                self._scanning = False
        return report.as_dict()

    # ---------------------------------------------------------------- snapshot
    def lookup_entry(self, src: str) -> Optional[dict]:
        """Return the index entry for an absolute source path, or None."""
        try:
            key = str(Path(src).expanduser().resolve())
        except OSError:
            return None
        with self._lock:
            entry = self._entries.get(key)
            return dict(entry) if entry else None

    def snapshot(self) -> dict:
        with self._lock:
            artists: dict[str, dict[str, list[dict]]] = {}
            for e in self._entries.values():
                if not e.get("dest"):
                    continue  # superseded/duplicate copies stay out of the tree
                artists.setdefault(e["artist"], {}).setdefault(e["album"], []).append(e)

            def track_key(t: dict):
                return (t.get("track_no") if t.get("track_no") is not None else 9999,
                        (t.get("year") or ""), t["title"])

            tree = []
            for artist in sorted(artists, key=str.lower):
                albums = []
                for album in sorted(artists[artist], key=str.lower):
                    tracks = sorted(artists[artist][album], key=track_key)
                    albums.append({
                        "name": album,
                        "tracks": [
                            {"title": t["title"], "track_no": t.get("track_no"),
                             "ext": t["ext"], "size": t["size"], "src": t["src"],
                             "dest": t.get("dest", ""), "video_id": t.get("video_id", ""),
                             "year": t.get("year")}
                            for t in tracks
                        ],
                    })
                tree.append({"name": artist, "albums": albums})

            n_tracks = sum(len(al["tracks"]) for a in tree for al in a["albums"])
            n_albums = sum(len(a["albums"]) for a in tree)
            return {
                "stats": {
                    "total_tracks": n_tracks,
                    "total_artists": len(tree),
                    "total_albums": n_albums,
                    "scanning": self._scanning,
                    "last_scan": self._last_report,
                },
                "artists": tree,
            }

    # ----------------------------------------------------------------- watcher
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="media-library")
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        self._wake.set()

    def request_full_rescan(self):
        self._full_rescan_requested = True
        self._wake.set()

    def notify_changes(self):
        """Call after new audio lands anywhere (job finished, Telegram batch...)."""
        self._wake.set()

    def _loop(self):
        self._log("Media library watcher started")
        self.scan_now(full=True)
        while not self._stop_event.is_set():
            woken = self._wake.wait(timeout=self.poll_interval)
            self._wake.clear()
            if self._stop_event.is_set():
                break
            full = self._full_rescan_requested
            self._full_rescan_requested = False
            if woken or full:
                self.scan_now(full=full)
        self._log("Media library watcher stopped")


def main(argv: list[str] | None = None) -> int:
    """Standalone runner: python3 media_library.py [source ...]"""
    import argparse

    parser = argparse.ArgumentParser(description="Organize archived audio into Artist/Album/Song library.")
    parser.add_argument("sources", nargs="*", default=["archive"], help="Directories to watch. Default: archive")
    parser.add_argument("--library-dir", default="library", help="Library output directory. Default: library")
    args_ns = parser.parse_args(argv)

    lib = MediaLibrary(sources=tuple(args_ns.sources), library_dir=args_ns.library_dir)
    lib.start()
    try:
        while lib._thread and lib._thread.is_alive():
            time.sleep(1)
    except KeyboardInterrupt:
        lib.stop()
    print(f"Library written to {args_ns.library_dir}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
