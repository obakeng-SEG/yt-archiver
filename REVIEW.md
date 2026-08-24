# yt-archiver — Full Code Review

**Scope:** Every source file (`yt_archive.py`, `web_ui.py`, `monitor.py`, `telegram_monitor.py`, `webhook.py`), tests, docs, config, git state.
**Method:** Line-by-line read, runtime reproduction of suspected bugs, test-suite execution (`51 tests, OK`), git-history audit for leaked secrets.
**Verdict:** Solid foundation with good instincts (CSRF, path checks, CSP, atomic archive writes). But **two headline features are currently broken**, there are several latent crashes, one real XSS vector, and doc/config drift.

---

## 1. CRITICAL — Broken functionality

### C1. Channel Monitor auto-download never fires
`_trigger_download` (web_ui.py:3365–3386) builds an `argparse.Namespace` **without** `normalize` / `normalize_target`. But `yt_archive.build_ytdlp_command` reads `args.normalize` unconditionally (yt_archive.py:175):

```
AttributeError: 'Namespace' object has no attribute 'normalize'   # reproduced
```

The exception is swallowed by `except Exception` around `queue.enqueue(...)` and logged as *"Monitor: could not queue download"* — so **every new video detected by the monitor is silently dropped**.

**Fix:** add `normalize="off", normalize_target=-16.0` to the namespace — better, centralize a `default_archive_args()` factory so every call site gets new fields automatically.

### C2. “Rescan Missing” button always 500s when anything is missing
Same root cause: `DownloadQueue.rescan_and_queue` (web_ui.py:298–310) builds namespaces lacking `normalize`. First `enqueue()` raises `AttributeError`, which propagates to the `/rescan` handler → HTTP 500.

**Fix:** same as C1 (shared factory).

### C3. `TelegramMonitor.monitor_channel` references an attribute that doesn't exist
`self._stop_event` (telegram_monitor.py:337, 364) is never defined in `__init__` → guaranteed `AttributeError` on first loop iteration. The whole Telegram *auto-monitor* path is dead code today.

**Fix:** add `self._stop_event = asyncio.Event()` in `__init__`, plus `start_monitor()/stop_monitor()` wrappers; wire to the existing `telegram_channels.json` entries (they're saved but never acted upon). Or remove until implemented.

### C4. Telegram downloads silently time out at 30 s
`run_async` (telegram_monitor.py:72–75) does `future.result(timeout=30)`. `/telegram/download` downloads all selected files **sequentially inside one coroutine**, so >2–3 large files exceed 30 s. The future times out → UI shows error → **the coroutine keeps downloading anyway** (files appear later, unannounced), and subsequent Telegram requests queue behind it.

**Fix:** make `/telegram/download` enqueue a background job (return job id immediately), report progress via a status endpoint, and/or parallelize with `asyncio.gather` and a longer/no timeout for downloads specifically.

### C5. README documents features that don't exist
- `python3 yt_archive.py --telegram-add/--telegram-list/--telegram-download/...` — none of these args exist in the parser → argparse exits with error (README.md:125–147, 247–252).
- Env vars `PORT` / `OUTPUT_DIR` (README.md:210–218) — never read anywhere.
- `requirements.txt` doesn't install `yt-dlp` (only `telethon>=1.36.0`) although it's a hard runtime dependency.

**Fix:** either implement the flags (thin wrappers calling `TelegramMonitor` methods) or delete the sections; read `PORT`/`OUTPUT_DIR` as defaults in `web_ui.build_parser`; add `yt-dlp` to requirements.txt.

---

## 2. HIGH — Security / correctness

### H1. XSS via attribute injection (attacker-controlled titles)
JS `esc()` (web_ui.py ≈:1680) escapes only `& < >` — **not quotes** — yet it's used inside double-quoted attributes and inline `onclick` strings:
- `title="' + esc(f.path) + '"` (file items)
- `esc(item.title)` inside playlist labels
- `retryFailed('...')` built by string concat with partial quote-escaping (web_ui.py ≈:2360)

A YouTube video title or Telegram filename containing `"><img src=x onerror=...>` executes script **inside the page that holds your CSRF token**. Content is attacker-controllable simply by archiving a maliciously-named video/channel.

**Fix:** make `esc()` also replace `"` → `&quot;` and `'` → `&#39;`; prefer `data-*` attributes + event delegation (you already do this correctly for the channels list) instead of inline `onclick` string building.

### H2. Directory-browser modal ignores its target field
`openBrowser('norm-dir')` (Normalize card, web_ui.py:2442) passes an element id, but `openBrowser()` takes no argument and `browserSelect()` always writes into `#output_dir` — **choosing a folder for normalization overwrites the Output Directory field**.

**Fix:** track `browserTargetEl`; `openBrowser(targetId)` stores it; `browserSelect()` writes back to the stored target.

### H3. Unvalidated bitrate → ffmpeg argument injection
`POST /channels/quality` (web_ui.py:3071–3082) validates `audio_format` but **not** `audio_bitrate`. The value is persisted and later interpolated into `["--postprocessor-args", f"-ab {bitrate}"]` — arbitrary ffmpeg arguments (e.g. writing files anywhere ffmpeg can).

**Fix:** validate against `yt_archive.AUDIO_BITRATES ∪ {""}` exactly as the form path does.

### H4. Tests can launch real yt-dlp and mutate real state
`DownloadQueueTests.test_enqueue_adds_to_queue` starts the **real worker thread**; if the test process lives past the 2 s poll, it runs an actual `yt-dlp` subprocess against `youtube.com/watch?v=abc123`. Tests also read/write repo-root state (`download_history.json`, `channels.json`). Currently you pass because the suite finishes in 9 ms — pure luck.

**Fix:** give `DownloadQueue`/`ChannelMonitor` an injectable state-dir + `autostart_worker=False` test hook (or mock `_ensure_worker`); wrap tests in `tempfile.TemporaryDirectory()` + `os.chdir`.

### H5. DNS-rebinding exposure of GET APIs
No `Host` header validation. A remote page can rebind DNS to `127.0.0.1:8765` and read JSON responses cross-origin: `/queue/history`, `/api/export`, `/files`, `/browse` (filesystem listing), `/api/normalize/list` (arbitrary absolute path listing — also inconsistent with `/browse`, which is home-restricted).

**Fix (pick any/all):** reject requests whose `Host` ≠ `host[:port]` you bound; require the CSRF token on sensitive GETs; restrict `/api/normalize/list` like `/browse`.

---

## 3. MEDIUM

| # | Issue | Location | Fix |
|---|-------|----------|-----|
| M1 | `scan_missing` uses substring match (`vid in fname`) → false positives/negatives; cleaned IDs cause duplicate re-downloads | web_ui.py:231–254 | Match exact `"[{vid}]..."` token from the output template |
| M2 | Single watch URL with `&list=` downloads the entire playlist (`"list=" in url` disables `--no-playlist`) | yt_archive.py:146–151 | Strip `list=` param for `/watch` URLs unless `/playlist` requested; document behavior |
| M3 | Webhook payload `{event,data}` is rejected by Discord (needs `content`) and Slack (needs `text`) | webhook.py:82–98 | Adapt payload per host (discord.com → `{"content": ...}`, hooks.slack.com → `{"text": ...}`); optionally add HMAC signature header |
| M4 | Quiet hours + queue contents are memory-only → lost on restart (channel schedules persist, these don't) | web_ui.py:98–102 | Persist to a small `state.json` |
| M5 | Non-atomic history save — crash mid-write corrupts `download_history.json` and loader resets to `[]` (total loss) | web_ui.py:112–113 | Temp-file + `os.replace` (copy pattern already used in `clean_archive`) |
| M6 | Normalize: `dest = out/p.name` — if output dir == source dir, ffmpeg fails ("Output same as Input") counted as failures; re-encode may drop embedded cover art | web_ui.py:501–532 | Skip/guard `dest.resolve()==p.resolve()`; add `-map_metadata 0 -c:a` explicit codec/bitrate |
| M7 | Duplicates allowed: `TelegramMonitor.add_channel` and `WebhookManager.add_webhook` don't dedupe (monitor.add_channel does) → double notifications/rows | telegram_monitor.py:410, webhook.py:44 | Reject or update-on-duplicate |
| M8 | `fetch_playlist_items` timeout=30 s → big playlists return "No items found" | web_ui.py:577–602 | Raise to 120 s, surface stderr in the response |
| M9 | Adding a channel blocks the HTTP thread up to ~180 s (two seeded `subprocess.run` calls, timeouts 60+120) — browser just hangs | monitor.py:68–128 | Seed in background after responding; or lower timeouts |
| M10 | Progress parsing: regex misses `of ~ 3.2MiB` (estimated sizes) so bar stalls; multi-URL batch has no true overall total; `countCompletedItems` counts both merge destinations → overcount | web_ui.js ≈1636–1662 | Use `--progress-template` / `--newline` machine-readable output instead of screen-scraping |
| M11 | Browse modal restricted to `$HOME` + `./archive` — cannot pick external volumes (`/Volumes/…`), yet typing a path manually bypasses the restriction (inconsistent UX) | web_ui.py:2860–2870 | Allow any resolved path lacking `..` (match `safe_path`), or drop the modal restriction |
| M12 | Dead/misleading code: `shutil` imported but unused (web_ui.py:14); `get_storage_stats` never called (:541); `NormalizeWorker.start` annotated `-> None` but returns bool (:486); `import asyncio`/`tempfile` mid-function; `redirect()` helper near-trivial | various | Clean up; hoist imports |

---

## 4. LOW / polish

1. **Singles named `000 – Title`** — `%(playlist_index|000)s` literal fallback. Use `%(playlist_index|)s` and conditional formatting, or drop the index for singles.
2. **Storage widget shows `0 KB` for empty dirs** (divides by 1024 unconditionally) — client JS ≈:2420.
3. **`loadDir` builds inline `onclick` with hand-rolled `'` escaping** — breaks on `"`/`\` in dir names; switch to `data-path` + delegation (same fix family as H1).
4. **`positive_int` accepts ports >65535** for `--port` (fails late at bind) — clamp 1–65535 (web_ui.py:3361).
5. **No socket timeout** on `ThreadingHTTPServer` — a stuck client pins a thread (local tool, low impact): `ThreadingHTTPServer(...); server.timeout = 30`.
6. **All HTTP logging silenced** (`log_message` no-op) — route to `LOG.debug` instead; helps debugging.
7. **Snapshot races are benign but real** — `ArchiveJob.returncode/finished_at/error` written without lock; GIL makes it safe today, but group under `_lock` for hygiene.
8. **`/playlist` GET checks netloc only, not scheme** (ftp://youtube.com passes) — harmless with yt-dlp but tighten alongside H5.
9. **Style drift:** `Optional[X]` vs `X | None` mixed across modules; lambda aliases `trigger_download = lambda ...` (PEP8 E731); 3.4k-line `web_ui.py` mixes HTML/CSS/JS/Python.
10. **State sprawl in repo root:** `channels.json`, `download_history.json`, `webhooks.json`, `*.session`, `telegram_config.json` all CWD-relative — run from another directory and you silently fork your state. Consider `~/.config/yt-archiver/` or an env-var data dir.
11. **Secrets posture (verified):** `telegram_config.json` (real api_id/api_hash) and `yt-archiver.session` exist on disk but are **gitignored and never committed** — good. If either ever left the machine, rotate the API hash and revoke the session.
12. **`/api/import` silently drops rejected URLs** (`except: pass`) — the toast reports only successes; return skipped count/reasons.

---

## 5. Test coverage gaps (current suite: 51 passing)

Untested today: `DownloadQueue._run_job` lifecycle, `stop_current`, quiet-hours gating, `scan_missing`/`clean_archive`/`rescan_and_queue` (would have caught C2), `NormalizeWorker`, `fetch_playlist_items`, all `/channels/*`, `/rescan`, `/queue/retry` endpoints, every Telegram endpoint, `ChannelMonitor` scheduling (`_is_in_schedule` wrap-midnight branch), webhook dispatch, and the JS layer (none). Suggest pytest + `pytest-cov`, target ≥80% on Python, plus a smoke test spinning the real server on an ephemeral port.

---

## 6. Improvement ideas (architecture/UX)

- **Machine-readable progress:** run yt-dlp with `--progress-template` and emit structured events over SSE/WebSocket instead of regexing logs — fixes M10 permanently and enables per-item ETA/speed.
- **Persistent, resumable queue** (SQLite): survives restarts, natural place for retries/dedupe/history (replaces 3 JSON files).
- **Concurrency setting:** worker pool of N downloads (currently strictly serial).
- **Post-download pipeline:** chain normalize → embed chapters (`--embed-chapters`) → move/copy rules per channel.
- **Per-channel output directory** override (format/bitrate already per-channel).
- **Library view:** searchable table with duration, bitrate, added-date; inline play via `<audio>`; duplicate detection (size hash first, fingerprint optional); tag editing via mutagen.
- **SponsorBlock integration:** `--sponsorblock-remove sponsor,selfpromo` at download time.
- **Config export/import of everything** (channels, webhooks, quiet hours, settings) — current export omits config.
- **First-run setup wizard** for Telegram (detect missing api_id and deep-link my.telegram.org) instead of manual JSON edit.
- **Packaging:** `pyproject.toml` with console scripts (`yt-archive`, `yt-archive-web`), pin `yt-dlp` minimum, add GitHub Actions CI running unittest + ruff.
- **Split the monolith:** extract `PAGE_HTML` to a static file (still servable from one file via `importlib.resources`), or move to FastAPI + htmx when feature growth continues.

## 7. Feature roadmap suggestion (personal-use priority order)

1. Fix C1/C2/C3 (broken core features) + H1/H2/H3 (small diffs)
2. Persisted queue + quiet hours (M4/M5)
3. Structured progress events (M10)
4. Library/search view + stats dashboard (downloads/week)
5. Telegram auto-monitor completion (C3 follow-through) + browse pagination beyond 50 messages
6. SponsorBlock + chapters
7. Packaging + CI

---

*Generated 2026-08-24. Line numbers reference working tree at commit `0e091e5` plus unstaged edits to `web_ui.py` / `telegram_monitor.py`.*
