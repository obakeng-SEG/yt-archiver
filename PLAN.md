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

## Verification
```
$ python3 -m compileall -q *.py && python3 -m unittest discover -s . -p "test_*.py"
Ran 64 tests ... OK
```
Live smoke test (port 8799): `/` → 200, `/status` → 200, forged `Host:` → 403 `{"error": "Invalid Host header"}`.
Known trade-off: `/api/normalize/list?dir=` outside home now clamps to `$HOME`, so scanning a huge home root can be slow on first Scan — same cost profile as the pre-existing unrestricted behavior on big trees; acceptable for local use.

## Deferred (documented, not scheduled)
- **C4** Telegram downloads as background jobs with progress polling (needs job-runner refactor).
- **M9** Non-blocking channel seeding.  **M10** yt-dlp `--progress-template` structured progress.
- Queue persistence across restarts; concurrency; library view; SponsorBlock; packaging/CI.
