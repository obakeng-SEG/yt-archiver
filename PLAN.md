# yt-archiver — Fix & Improvement Plan

Companion to [REVIEW.md](REVIEW.md) (findings C1–C5, H1–H5, M1–M12, low items).
Strategy: fix broken core features first, then security, then quick mediums, then docs.
Every phase ends with the full test suite green. **Status: Phases 1–4 complete — 64/64 tests pass, live smoke test verified (see notes).**

## Phase 1 — Critical: restore broken features ✅
- [x] **C1+C2** Add `default_archive_args()` factory in `web_ui.py` supplying every field `build_ytdlp_command` needs (incl. `normalize`/`normalize_target`). Used in `_trigger_download`, `rescan_and_queue`, and the `/queue/retry` fallback so monitor auto-downloads and Rescan Missing work again.
- [x] **C3** `_stop_event` (`asyncio.Event`) now lazily created in `TelegramMonitor`; added thread-safe `request_stop()`; `monitor_channel` uses a loop-local reference.

## Phase 2 — High: security & correctness ✅
- [x] **H1** JS `esc()` escapes `"`, `'`, `<`, `>`, `&` (attribute-safe); file-browser and history-retry converted from inline `onclick` string-building to `data-*` attributes with delegated listeners.
- [x] **H2** `openBrowser(targetId)` remembers the invoking input; `browserSelect()` writes back to it — Normalize card no longer overwrites Output Directory.
- [x] **H3** `/channels/quality` strictly validates bitrate against `AUDIO_BITRATES` (empty → `192k`); covered by endpoint tests.
- [x] **H5** Strict `Host` header allowlist built in `main()` and passed to `make_handler(allowed_hosts=…)` (disabled when omitted, so tests stay simple); `/api/normalize/list` clamped through shared `resolve_browsable()` like `/browse`.
- [x] **H4** Test isolation: `DownloadQueue(history_path, state_path, autostart_worker=False)`, `ChannelMonitor(channels_path=…)`, `WebhookManager(webhooks_path=…)`, `TelegramMonitor(channels_path=…)`; queue/handler suites run in temp cwd; 13 new regression tests (C1, C2, M1, clean_archive, H3 ×2, Host ×2, webhook payloads ×4, Telegram dedupe).

## Phase 3 — Medium quick wins ✅
- [x] **M5** `utils.atomic_write_text()` used by history, channels.json, webhooks.json, telegram channels/config, seen-ID files, and `clean_archive`.
- [x] **M4** Quiet hours persist to `queue_state.json`; restored on startup; gitignored; documented in README table.
- [x] **M1** `scan_missing` matches exact `[video_id]` token (substring false-positives eliminated).
- [x] **M6** Normalize worker refuses `dest == src` in-place overwrites; adds `-map_metadata 0` to preserve tags/art.
- [x] **M7** Duplicate webhook URLs rejected (`ValueError` → 400); duplicate Telegram channel IDs update in place.
- [x] **M8** `fetch_playlist_items` timeout 30 s → 120 s.
- [x] **M3** `build_webhook_payload()` shapes Discord (`content`) / Slack (`text`) / raw payloads; unit-tested.
- [x] **M12** Removed unused `shutil` import and dead `get_storage_stats()`; fixed `NormalizeWorker.start -> bool` annotation; hoisted `asyncio` import.

## Phase 4 — Docs & packaging truthfulness ✅
- [x] **C5** README: non-existent Telegram CLI flags removed (Telegram is Web-UI managed), bogus `OUTPUT_DIR` env removed, real `$PORT`/`$HOST` support implemented in `web_ui.build_parser`, watch-URL-with-`list=` behavior documented, config-file table updated.
- [x] requirements.txt: added `yt-dlp>=2024.10.22`.

## Extra fixes discovered while implementing
- `DownloadQueue.__init__` ordering bug: quiet-hour defaults were applied *after* `_load_state()`, silently discarding persisted settings (caught by the new persistence test).
- Latent path doubling: rescan passed `archive/downloaded.txt` alongside `output_dir=archive`, producing `archive/archive/downloaded.txt` for yt-dlp while scan/clean read `./archive/downloaded.txt`. Now normalized before enqueue.

## Phase 5 — Automated media library (user-requested) ✅
- [x] **`media_library.py`** — `MediaLibrary` class: background watcher thread polls sources (default `archive/`) every 20 s and reacts instantly to `notify_changes()` triggers.
- [x] **Per-file review** — metadata priority: mutagen tags (optional dep, in requirements) → yt-dlp sidecar `.info.json` (uploader/playlist/upload_date) → folder + `NNN - Title [id]` filename heuristics. Zero-byte/unreadable files are rejected and reported.
- [x] **Auto-organization** — copies (non-destructive) into `library/Artist/Album/NN - Song.ext`; Telegram layout maps `@channel → artist`; unsafe chars sanitized; same-title collisions suffixed with video ID; content-hash dedupe keeps a single copy.
- [x] **Triggers** — after every successful yt-dlp job (`_run_job`), after every Telegram batch download, on manual drops via the poller, plus "Rescan Library" button (full re-index) in the web UI.
- [x] **Web UI** — new Library card: collapsible Artist → Album → Track tree with counts/scanning badge; `GET /api/library`, `POST /api/library/rescan`.
- [x] **Standalone mode** — `python3 media_library.py [source ...] --library-dir DIR`.
- [x] **Tests** — 10 new cases (extraction fallbacks/sidecar overrides, organize layout, incremental skip, dedupe, collision suffixing, sanitization, live watcher pickup, prune-on-delete). Suite total: **74 passing**.
- [x] Verified end-to-end against the running server: dropped file appeared in `/api/library` and on disk under `library/…` within one scan.

## Verification
```
$ python3 -m compileall -q *.py && python3 -m unittest discover -s . -p "test_*.py"
Ran 64 tests ... OK
```
Live smoke test (port 8799): `/` → 200, `/status` → 200, forged `Host:` → 403 `{"error": "Invalid Host header"}`.
Known trade-off: `/api/normalize/list?dir=` outside home now clamps to `$HOME`, so scanning a huge home root can be slow on first Scan — same cost profile as the pre-existing unrestricted behavior on big trees; acceptable for local use.

## Phase 6 — UI completion pass (user-requested) ✅
- [x] **Library search** — live filter box; matching tracks shown as flat rows with Artist / Album sub-labels (client-side, capped at 300 hits).
- [x] **Inline audio preview** — per-track play buttons drive a persistent player bar; `GET /audio?src=…` streams bytes with full HTTP **Range** support (seeking works), served only for paths present in the library index (exact allowlist — no traversal surface). Playing row highlights red with pause glyph.
- [x] **Large-library scaling** — polls compare a stats signature and skip DOM rebuilds when unchanged (preserves open sections/scroll/playback); tree paginates at 30 artists with "Show more" (+50); re-renders restore open `<details>` state.
- [x] **Machine-readable progress (M10)** — `yt_archive` gains `--machine-progress` flag (web paths enable it): yt-dlp emits `PROGRESS|pct|speed|eta|id`, parsed server-side in `ArchiveJob` and kept out of the visible log; UI shows exact %, speed and ETA. Legacy regex parsing retained as fallback.
- [x] **Retry hardening** — `/queue/retry` merges stored history args over current factory defaults, so old history entries missing newer fields can never crash enqueue again.
- [x] **Cosmetics** — Storage widget reads `0 B` instead of `0 KB` when empty; `--port` strictly clamped to 1–65535.
- [x] Decision recorded: archive filenames keep the `000 - ` singles prefix (changing the output template would churn existing libraries); the library layer already presents clean names.
- [x] **Tests** — 8 new (progress template on/off, audio 200/206/404 incl. allowlist rejection, retry hardening, port clamp). Suite total: **82 passing**.

## Phase 7 — Quality-preference dedupe (user-requested) ✅
- [x] **Identity grouping** — tracks sharing casefolded artist+album+title are treated as one song; multiple encodes/rips collapse into a single library copy.
- [x] **Larger/higher-quality wins** — when a bigger copy arrives, the old library file is removed and replaced (logged as an upgrade); smaller latecomers are dropped as duplicates. Order-independent.
- [x] **Byte-identical fast path retained** — content-hash twins stay collapsed even under different labels; flagged `content_dup_of` so the consistency sweep can't resurrect them.
- [x] **Promotion safety net** — if the winning source is later deleted from the archive, the best surviving copy is re-promoted into the library (orphaned library files with no remaining owner are reclaimed cleanly). Songs never silently disappear.
- [x] Snapshot/tree lists only held copies; superseded and duplicate entries are hidden.
- [x] **Tests** — order-independence (both arrival orders), upgrade-on-larger-arrival, delete-winner-promotes-runner-up, collision-suffix safety net via direct `_place`, updated byte-twin expectations. Suite total: **84 passing**.

## Deferred (documented, not scheduled)
- **C4** Telegram downloads as background jobs with progress polling (needs job-runner refactor).
- **M9** Non-blocking channel seeding.  **M10** yt-dlp `--progress-template` structured progress.
- Queue persistence across restarts; concurrency; library view; SponsorBlock; packaging/CI.
