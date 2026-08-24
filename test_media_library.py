import os
import shutil
import tempfile
import unittest
from pathlib import Path

from media_library import MediaLibrary, ScanReport, TrackInfo, sanitize_component


class TempCwdTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        old_cwd = os.getcwd()
        os.chdir(self._tmp.name)
        self.addCleanup(os.chdir, old_cwd)
        self.tmpdir = self._tmp.name

    def make_lib(self, **overrides) -> MediaLibrary:
        values = {
            "sources": ("archive",),
            "library_dir": os.path.join(self.tmpdir, "library"),
            "index_path": os.path.join(self.tmpdir, "library_index.json"),
            "poll_interval": 3600.0,
        }
        values.update(overrides)
        return MediaLibrary(**values)

    def touch(self, rel: str, content: bytes = b"x" * 2048) -> Path:
        p = Path(self.tmpdir) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        return p


class ExtractionTests(TempCwdTestCase):
    def test_ytdlp_filename_fallback_parse(self):
        self.touch("archive/Dlala Thukzin/Summer Mixes/03 - Ikhandle [dQw4w9WgXcQ].mp3")
        lib = self.make_lib()
        lib.scan_now(full=True)
        snap = lib.snapshot()

        self.assertEqual(snap["stats"]["total_tracks"], 1)
        entry = snap["artists"][0]
        self.assertEqual(entry["name"], "Dlala Thukzin")
        album = entry["albums"][0]
        self.assertEqual(album["name"], "Summer Mixes")
        track = album["tracks"][0]
        self.assertEqual(track["title"], "Ikhandle")
        self.assertEqual(track["track_no"], 3)
        self.assertEqual(track["video_id"], "dQw4w9WgXcQ")

    def test_sidecar_metadata_overrides_heuristics(self):
        self.touch("archive/Chan/Playlist/01 - Raw [abcdefghijk].mp3")
        sidecar = Path(self.tmpdir) / "archive/Chan/Playlist/01 - Raw [abcdefghijk].info.json"
        sidecar.write_text(
            '{"uploader": "Real Artist", "playlist_title": "The LP",'
            ' "title": "Polished", "upload_date": "20240315"}'
        )
        lib = self.make_lib()
        lib.scan_now(full=True)
        snap = lib.snapshot()

        self.assertEqual(snap["artists"][0]["name"], "Real Artist")
        album = snap["artists"][0]["albums"][0]
        self.assertEqual(album["name"], "The LP")
        track = album["tracks"][0]
        self.assertEqual(track["title"], "Polished")
        self.assertEqual(track["year"], "2024")

    def test_telegram_layout_maps_channel_to_artist(self):
        self.touch("archive/Telegram/@synthwave/audio_42.mp3")
        lib = self.make_lib()
        lib.scan_now(full=True)
        snap = lib.snapshot()

        artist = snap["artists"][0]
        self.assertEqual(artist["name"], "@synthwave")
        self.assertEqual(artist["albums"][0]["name"], "Telegram")
        self.assertEqual(artist["albums"][0]["tracks"][0]["title"], "audio_42")


class OrganizeTests(TempCwdTestCase):
    def test_copies_into_artist_album_structure(self):
        src = self.touch("archive/Artist A/Great Album/02 - Song B [vid000000001].mp3", b"audio-bytes")
        lib = self.make_lib()
        report = lib.scan_now(full=True)

        self.assertEqual(report["added"], 1)
        expected = Path(self.tmpdir) / "library/Artist A/Great Album/02 - Song B.mp3"
        self.assertTrue(expected.exists())
        self.assertEqual(expected.read_bytes(), b"audio-bytes")
        self.assertEqual(lib.snapshot()["artists"][0]["albums"][0]["tracks"][0]["dest"], str(expected))
        del src

    def test_incremental_scan_skips_unchanged_files(self):
        self.touch("archive/A/B/01 - T [vid000000002].mp3")
        lib = self.make_lib()
        first = lib.scan_now(full=True)
        second = lib.scan_now(full=False)

        self.assertEqual(first["added"], 1)
        self.assertEqual(second["added"], 0)
        self.assertEqual(second["unchanged"], 1)

    def test_identical_content_in_two_places_is_deduplicated(self):
        data = b"same-bytes"
        self.touch("archive/A/B/01 - One [vid000000003].mp3", data)
        self.touch("archive/C/D/09 - Two [vid000000004].mp3", data)
        lib = self.make_lib()
        report = lib.scan_now(full=True)

        self.assertEqual(report["added"], 1)
        self.assertEqual(report["duplicates"], 1)
        # Only the labeled original appears; the byte-identical twin is hidden
        self.assertEqual(lib.snapshot()["stats"]["total_tracks"], 1)
        lib_files = [p for p in (Path(self.tmpdir) / "library").rglob("*") if p.is_file()]
        self.assertEqual(len(lib_files), 1)

    def test_same_song_keeps_larger_copy_regardless_of_order(self):
        # Same artist/album/title, different encodes (sizes): the larger file
        # must be the single survivor — whether it arrives first or second.
        for order in ((b"small", b"much-larger-content"), (b"much-larger-content", b"small")):
            with self.subTest(order=order[0][:5]):
                shutil.rmtree(Path(self.tmpdir) / "library", ignore_errors=True)
                shutil.rmtree(Path(self.tmpdir) / "archive", ignore_errors=True)
                index = Path(self.tmpdir) / "library_index.json"
                if index.exists():
                    index.unlink()
                self.touch("archive/A/B/01 - Same [aaaaaaaaaaa].mp3", order[0])
                self.touch("archive/A/B/01 - Same [bbbbbbbbbbb].mp3", order[1])
                lib = self.make_lib()
                report = lib.scan_now(full=True)

                files = list((Path(self.tmpdir) / "library/A/B").iterdir())
                self.assertEqual(len(files), 1)
                self.assertEqual(files[0].read_bytes(), b"much-larger-content")
                self.assertEqual(lib.snapshot()["stats"]["total_tracks"], 1)
                if report["replaced"]:
                    # Larger arrived second: counted as an upgrade, not a drop
                    self.assertEqual(report["duplicates"], 0)
                else:
                    # Larger arrived first: smaller one dropped as duplicate
                    self.assertGreaterEqual(report["duplicates"], 1)

    def test_deleting_larger_source_promotes_remaining_copy(self):
        small = self.touch("archive/A/B/01 - Same [aaaaaaaaaaa].mp3", b"small")
        big = self.touch("archive/A/B/01 - Same [bbbbbbbbbbb].mp3", b"much-larger-content")
        lib = self.make_lib()
        report = lib.scan_now(full=True)
        self.assertEqual(report["replaced"], 1)  # big arrived second, upgraded
        self.assertEqual(report["duplicates"], 0)

        # The winning source vanishes: the smaller copy must be re-promoted
        # so the song doesn't disappear from the library.
        Path(big).unlink()
        report = lib.scan_now(full=False)

        self.assertEqual(report["pruned"], 1)
        self.assertEqual(report["added"], 1)  # promoted back into the library
        files = list((Path(self.tmpdir) / "library/A/B").iterdir())
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].read_bytes(), b"small")
        del small

    def test_collision_suffix_still_guards_place(self):
        # Safety net: two distinct identities that compute to the same dest
        # name (e.g. unicode edge cases) get disambiguated, never overwritten.
        lib = self.make_lib()
        base = {
            "src": str(Path(self.tmpdir) / "x.mp3"), "artist": "A", "album": "B",
            "title": "T", "track_no": 1, "year": None, "ext": ".mp3",
            "size": 3, "mtime_ns": 0, "video_id": "", "source": "manual",
        }
        info1 = TrackInfo(hash="h1", **base)
        info2 = TrackInfo(hash="h2", **base)
        src_file = Path(self.tmpdir) / "x.mp3"
        src_file.write_bytes(b"one")
        d1, ok1 = lib._place(info1, ScanReport())
        src_file.write_bytes(b"two-longer")
        d2, ok2 = lib._place(info2, ScanReport())

        self.assertTrue(ok1 and ok2)
        self.assertNotEqual(d1, d2)
        self.assertTrue(d2.name.startswith("01 - T ["))

    def test_sanitizes_unsafe_components(self):
        name = sanitize_component('AC/DC: "Back In Black"? <>*|')
        self.assertNotIn("/", name)
        self.assertNotIn('"', name)
        self.assertNotIn(":", name)
        self.assertTrue(name.startswith("AC_DC") or "/" not in name)


class WatcherTests(TempCwdTestCase):
    def test_new_file_picked_up_by_notify(self):
        lib = self.make_lib(poll_interval=0.05)
        lib.start()
        self.addCleanup(lib.stop)

        import time
        deadline = time.time() + 10
        while time.time() < deadline and not lib.snapshot()["stats"].get("last_scan"):
            time.sleep(0.05)  # wait for the initial full scan
        self.touch("archive/Late/Late Album/01 - Late Arrival [vid000000007].mp3")
        lib.notify_changes()

        while time.time() < deadline and lib.snapshot()["stats"]["total_tracks"] == 0:
            time.sleep(0.05)
        self.assertGreaterEqual(lib.snapshot()["stats"]["total_tracks"], 1)

    def test_full_rescan_prunes_vanished_sources(self):
        src = self.touch("archive/A/B/01 - Gone [vid000000008].mp3")
        lib = self.make_lib()
        lib.scan_now(full=True)
        self.assertEqual(lib.snapshot()["stats"]["total_tracks"], 1)

        src.unlink()
        report = lib.scan_now(full=True)

        self.assertEqual(report["pruned"], 1)
        self.assertEqual(lib.snapshot()["stats"]["total_tracks"], 0)


if __name__ == "__main__":
    unittest.main()
