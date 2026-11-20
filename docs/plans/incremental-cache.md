# Plan: incremental cache for cctrack (target release 0.12.0)

Status: proposed, not started. Written 2026-11-20 against commit `9323fa0` (0.11.2, 368 tests passing).

## Status

The `/step` loop reads this table for dependencies and progress, and sets a row to `done` (or `skipped`, with the reason) after the step is committed.

| Step | Title | Depends on | Status |
|---|---|---|---|
| 0 | Measurement and the equivalence harness | — | done |
| 1 | Make pricing cheap | 0 | done |
| 2 | One per-file parse seam (refactor only) | 0 | done |
| 3 | Cache store, standalone | 2 | done |
| 4 | Local transcripts and rollouts through the cache | 1, 3 | done |
| 5 | Local hook data | 4 | skipped (F10: hook read 3.3% of a run, under the 5% gate; local hook caching moved into Step 6 by Dan, 2026-11-20) |
| 6 | Remote protocol v2 | 5 | done |
| 7 | Documentation and release readiness | 6 | done |
| 8 | Resume inside a growing Claude transcript (optional, gated) | 7 | todo |

## 1. How to use this plan

Run `/step` (next `todo` step onward), `/step N` (from step N onward) or `/step N --one` (one step). Each step in section 6 is one pass of a Plan → Implement → Verify loop:

- **Plan (`step-planner`, Opus):** reads this file, `CLAUDE.md` and the code, and writes `docs/plans/steps/NN-<slug>.md`. Resolves every "Planner decides" item with evidence (a measurement or a test), not preference.
- **Implement (`step-implementer`, Sonnet):** builds exactly that plan and its tests.
- **Verify (`step-verifier`, Sonnet, read-only):** runs the step's acceptance and "Verify" checks and every mutation check (on a copy of `src/`), and reports anything that fails or could not be checked.

A passing step becomes one local commit, `step NN: <title>`, on branch `incremental-cache`. Nothing is pushed, tagged or released. The loop stops for the user on a product decision, a section 7 decision, two failed verify rounds, or a step that says "Then pause for the user".

Rules for every step:

1. `PYTHONPATH=src python3 -m pytest tests -q` passes before and after. No existing test is weakened or deleted to make a step pass.
2. Zero dependencies, stdlib only, Python 3.10+.
3. **All code stays in `src/cctrack/__init__.py`.** `_remote_program` ships that one file to remote hosts and `exec`s it there. A second module would break `--remote`.
4. Anything imported only for the cache (`sqlite3`) is imported lazily inside cache functions. The copy running on a remote host must never open a cache, and a Python built without `sqlite3` must still produce reports.
5. A cache problem never fails or changes a report. On any cache error: print one note to stderr, continue uncached.
6. Report output on stdout must be byte-identical with the cache on, off, cold, warm or rebuilt (section 5). Only stderr (progress, notes) may differ.
7. Update `CLAUDE.md` and `README.md` in the step that changes behaviour, not at the end.

## 2. Goal and non-goals

**Goal.** A repeat `cctrack` run re-reads only log files that changed since the last run, locally and on `--remote` hosts, and produces exactly the report a full read would. `--rebuild-cache` re-reads everything and replaces the cache. `--no-cache` re-reads everything and leaves the cache alone.

**Why.** Reports re-parse all history on every run. One user's host streams 1.8 GB of Codex rollouts per run; another run timed out at 60 s. 0.11.1 moved parsing onto the remote host, which fixed the transfer but not the re-reading.

**Non-goals.**
- No cached totals, costs or reports. Prices are applied at report time so a rate-card fix reprices all history at once.
- No cache on remote hosts. Remote hosts stay read-only (0.11.1 promise).
- No change to what a report contains.
- The raw-stream fallback (`CCTRACK_REMOTE_PARSE=0`, or a host without Python 3.10+) stays uncached.

## 3. Facts about the current code (verified)

These constrain the design. Function names are stable; line numbers are not.

**F1. Parsing a file is a pure function of that file.** `parse_lines(lines, source, stats)` and `parse_codex_lines(lines, source, stats)` take one file's lines and return usage events. Each already ends with an in-file `dedup_events([entries])`. `_tag_event_file(e, path)` then stamps the path. Nothing from other files is consulted.

**F2. Everything cross-file happens afterwards, and must keep happening on every run.** `dedup_events(event_lists)` merges copies across files (resumed sessions, rsynced sandboxes), `_expand_codex_snapshot_copies` and `_remove_codex_fork_replays` match Codex forks against their parent thread in another file, and `reconcile_sources` dedups again across hosts and rebuilds per-source totals. A fork child parsed today may need a parent parsed last week.

**F3. Cross-file dedup mutates events in place.** `_remove_codex_fork_replays` sets `_fork_history`, `_unresolved_fork` and `_locations` on its inputs. A file's cache payload must therefore be serialized **before** its events are passed to any cross-file step.

**F4. Three call sites loop over files:** `parse_events` (local Claude), `parse_codex_events` (local Codex), `_parse_remote_files` (remote, both kinds; used by the raw stream and by `_remote_collect` on the host). They differ slightly:
- `parse_events` fills a missing subagent `agent_id` from an `agent-<id>.jsonl` filename. `_parse_remote_files` does not. This is a pre-existing difference. Preserve it; do not fix it in this work.
- Local passes `stats=None` unless a detail report asked for `file_stats`. Stats do not affect events.
- Unreadable files and undecodable `.zst` files produce no events (and `{"unreadable": True}` stats or a skipped count).

**F5. File order feeds dedup.** `discover_files` uses `rglob` order (unsorted); `discover_codex_files` sorts. Winner selection in `_merge_usage_copies` is by quality then a deterministic key, but list positions follow input order. The cache must hand dedup the same per-file lists in the same discovery order as an uncached run.

**F6. Events and stats hold non-JSON types.** Tuples (`_snapshot_aliases`, `_fork_history`), tuple dict keys (`stats["compactions"]`), datetimes (`stats["first_ts"]`). `_tag_json`/`_untag_json` round-trip these exactly; 0.11.1 tests rely on it. `_remove_codex_fork_replays` uses `id()` of shared `_fork_history` lists only to skip repeat `dict.update` calls, so losing object identity in a round trip is harmless by value. Step 3 must prove this with the existing fork fixtures.

**F7. Hook data.** `parse_hook_rows(dir)` concatenates all `statusline-*.jsonl` lines and calls `_hook_rows_from_lines`, which sorts by `(session_id, ts)` with a stable sort. `rollup_old_hook_files(dir)` runs first on every local run and deletes files older than 90 days. Only today's file (UTC) grows. Monthly and baseline JSON files are tiny.

**F8. Remote.** `fetch_remote_bundle` sends the module source to `ssh host python3 -`; `_remote_collect` returns per-host **already deduped** events plus stats and hook rows as zlib-compressed tagged JSON (`REMOTE_PROTOCOL` v1). Never pickle.

**F9. Measured on 200,000 synthetic events** (this container, Python 3.12):

| Stage | Time |
|---|---|
| Parse from JSONL (tiny synthetic lines; real lines are far larger) | 0.76 s |
| Decode from cache, tagged JSON + zlib (`json.loads` 0.29 s + `_untag_json` 0.27 s) | 0.56 s |
| Decode from cache, `marshal.loads` | 0.10 s |
| `dedup_events` | 0.15 s |
| **`aggregate`, one pass** | **2.96 s** |

About 80% of `aggregate` is `_find_rate_row`, called twice per event (cost and energy), each call re-scanning the rate card with regex work. `main` runs `aggregate` once per source before `reconcile_sources`, which then rebuilds every source's totals and discards the first results, and once more for the final report. So after a cache lands, pricing would dominate a warm run unless Step 1 is done.

**F10. Real timings (Step 0, Dan's Mac with `--remote dgx`, 2026-11-20, cctrack 0.11.2 + Step 0, Python 3.11, total 220.7 s):**

| Phase | Time | Data |
|---|---|---|
| discovery | 0.8 s | |
| claude parse (Mac) | 30.6 s | 2,460 files, 5.8 GB; 71 changed in last 24 h (743.9 MB) |
| aggregate | 21.1 s | |
| hook read (Mac) | 1.9 s | 646 files, 96.7 MB; 15 changed in 24 h (415.6 KB) |
| codex parse (Mac) | 22.5 s | 115 files, 2.6 GB; 0 changed in 24 h |
| remote dgx, total | 127.3 s | 28.7 MB received |
| — on dgx: claude parse | 59.8 s | 1,472 files, 5.3 GB; 32 changed in 24 h (**3.5 GB**) |
| — on dgx: hook read | 5.3 s | 581 files, 101.0 MB; 19 changed in 24 h (1.6 MB) |
| — on dgx: codex parse | 32.3 s | 94 files, 2.9 GB; 1 changed in 24 h (**2.6 GB**) |
| — on dgx: other | 29.9 s | ssh, encode, transfer, local decode |
| reconcile | 16.5 s | |
| report | 0.0 s | |

Readings for later steps:
- Hook reading is 7.2 s of 220.7 s (3.3%) across both machines, under Step 5's 5% threshold.
- Pricing and merging (aggregate 21.1 s + reconcile 16.5 s, which itself re-aggregates) are 17% of the run and are not removed by any cache: Step 1 matters.
- Files changed within 24 h are a large share of the bytes on dgx: 3.5 GB of 5.3 GB of Claude transcripts, and one 2.6 GB Codex rollout. A per-file cache re-reads those in full, so on dgx a warm run would still parse roughly 6 GB. This is the evidence Step 8 asks for (for Claude); Codex is excluded from Step 8 as briefed, so the 2.6 GB rollout would stay a full re-read.
- "on dgx: other" (30 s) covers encoding, transfer and local decode of 28.7 MB; Step 6's planner should check how much of it is local `_untag_json` decode.
  *Step 6 finding:* a synthetic 27.7 MB reply decodes locally in 1.9–2.4 s on the planner's sandbox (zlib 0.2–0.3 s, `json.loads` 0.5–0.6 s, `_untag_json` 1.2–1.5 s) and takes 2.9 s to encode on the remote side. Real ids compress better, so estimate 2–4 s decode and 3–5 s encode for the real reply. That leaves roughly 20 s of ssh and transfer, which only the real machines can measure. Step 6 adds `on <host>: encode`, `local: decode` and `local: merge` timing sub-lines to split it.

**F11. Real timings after Step 7 (Dan's Mac with `--remote dgx`, 2026-11-20, cctrack 0.12.0 at `103abfd`, Python 3.11.15):**

| Phase | `--no-cache` | cold (`--rebuild-cache`) | warm (straight after) |
|---|---|---|---|
| claude parse (Mac) | 24.3 s | 25.2 s | 1.3 s (2,509 reused, 2 read) |
| hook read (Mac) | 1.5 s | 1.8 s | 0.3 s (648 reused) |
| codex parse (Mac) | 19.6 s | 19.9 s | 0.3 s (115 reused) |
| remote dgx, total | 120.0 s, 29.2 MB | 109.5 s, 29.2 MB | 34.6 s, 9.5 MB (2,168 reused, 6 read) |
| — on dgx: claude parse | 59.9 s | 51.7 s | **24.6 s** |
| — on dgx: hook read | 5.3 s | 4.0 s | 0.05 s |
| — on dgx: codex parse | 29.8 s | 28.0 s | 0.001 s |
| — on dgx: encode | 16.8 s | 15.9 s | 5.2 s |
| — local: decode / merge | 4.3 / 0.5 s | 4.6 / 1.6 s | 1.2 / 1.8 s |
| — on dgx: other (ssh, transfer) | 3.3 s | 3.4 s | 1.7 s |
| reconcile + aggregate | 6.3 s | 6.6 s | 6.6 s |
| total | 172.4 s | 163.5 s | **43.8 s** |

Against F10 (220.7 s): pricing and merging fell from 37.6 s to about 6.3 s (Step 1), and F10's 30 s "other" was mostly encoding on dgx (16.8 s), not transfer (3.3 s). For D3: in the warm run, re-reading grown Claude transcripts is (1.3 + 24.6) / 43.8 = **59%** of the run, nearly all of it on dgx (6 files that grew between runs, out of 3.7 GB changed in 24 h). The dgx Codex rollout (2.7 GB) did not change between these runs; when it does, it is re-read in full (about 28 s), which Step 8 does not address.

Report check: `nocache.out` vs `warm.out` differed only in today's live figures and by a few cents in some closed months (2026-05 to 2026-09) with identical tokens. Codex totals were identical. This is the existing accuracy factor, measured over every hook-covered day including today, rescaling estimated days as today's usage changes between runs, not the cache (tokens for those months are unchanged).

## 4. Design

### 4.1 Unit and watermark

One cache entry per log file. The watermark for a file is the `(size, mtime_ns)` it had when it was parsed.

- **Hit:** the file's current `(size, mtime_ns)` equals the stored pair, and the entry was written by the same parser code (4.3).
- **Miss:** anything else. The file is re-parsed from the start and the entry replaced.
- **Racy files:** if a file's mtime is within 2 s of the clock when it is parsed, or its `stat` differs before and after reading, parse it but do not store it. This is git's "racy timestamp" rule; it stops a file written during the read from being cached short. Use the clock of the machine the file is on.
- **Failures are never cached:** unreadable files, undecodable `.zst`. A cached `.zst` entry is used only while a zstd decoder is available, so a cached run skips exactly the files an uncached run skips. (Step 3.)
- Inode is not part of the key (unstable on some mounts; rsync replaces inodes without changing content). A same-size, same-mtime rewrite of an append-only log is not a realistic case, and `--rebuild-cache` is the escape hatch.

A byte-offset watermark inside a growing file is Step 8, optional and gated.

### 4.2 What an entry holds

`{events, stats}` for one file: the exact return value of the per-file parse after `_tag_event_file`, plus that file's `new_parse_stats()` (always collected when the cache is on). No prices, no costs, no aggregates.

### 4.3 Parser-code key

Every entry records a hash of the running module's source (`Path(__file__).read_bytes()`). A different hash is a miss. Consequence: the first run after any cctrack upgrade is a full read. This is deliberate: a hand-bumped version number that someone forgets would silently serve stale numbers. (Decision D2.)

*Step 3 addition.* The key also names what else decides a parse: the interpreter (`sys.implementation.name` and `sys.version_info[:3]`), `marshal.version` (the payload format, 4 on 3.12 and 5 on 3.14) and the default text encoding `open()` uses (`codecs.lookup(io.TextIOWrapper(io.BytesIO()).encoding).name`). The encoding matters: with `LC_ALL=C PYTHONCOERCECLOCALE=0 PYTHONUTF8=0` the same file parses `cwd` `/w/café` as `/w/caf\ufffd\ufffd`. Each entry's `code` column also carries the parse parameters (`source`, `label` when it differs from `path`, `agent_id_from_name`), so one path parsed two ways never shares an entry.

### 4.4 Store

- One SQLite file, `entries(host, kind, path, size, mtime_ns, code, payload BLOB, stored_at, last_used)` with primary key `(host, kind, path)`; `host` is `local` or `remote:<--remote name>` (Step 6: the prefix keeps a host literally named `local` apart from this machine's rows); `kind` is `claude`, `codex` or `hook`.
- Location: `$CCTRACK_CACHE_DIR`, else `$XDG_CACHE_HOME/cctrack`, else `~/.cache/cctrack`. Directory mode 0700, file 0600. Contents are ids, paths, working directories and counts, the same sensitivity as the logs' metadata; no prompt or response content.
- Commit in batches during a run so an interrupted cold run keeps its progress.
- Two cctrack runs at once must both produce correct reports; one may run uncached. Use a short busy timeout.
- A corrupt or unreadable store is deleted and recreated once; if that fails, run uncached.
- Entries not used for 30 days are pruned. Entries are only ever looked up by a path discovered in the current run, so stale entries cannot affect a report.
- **Payload encoding (decided in Step 3):** `zlib.compress(marshal.dumps(payload), 1)`, with the two stats datetimes stored as ISO strings. At 1M realistic events, decode took 2.0 s against 5.3 s for tagged JSON + zlib (budget 3 s). zlib's checksum turns a corrupt blob into a miss: 20,000 random bit flips all failed to decode, while about a quarter of mutated raw marshal blobs loaded as silently different values. Any decode error or wrong shape is a miss. Not pickle.
- Rollback journal (SQLite's default `DELETE` mode), not WAL: WAL needs a shared-memory `-shm` file, which fails on network filesystems and in read-only directories, and its advantage (readers never wait for a writer) is not needed when every write is short. Three concurrent simulated cold runs got no lock errors even with a 0.1 s busy timeout. Rows waiting to be written are held as encoded bytes and written in one short `BEGIN IMMEDIATE` transaction per batch, so no write lock is held while files are parsed. Opening runs `PRAGMA quick_check` and a write probe (`PRAGMA user_version`). The store is all or nothing for a run: the first SQLite error disables it for the rest of that run.

### 4.5 Command line

| Option | Effect |
|---|---|
| (default) | Use the cache. |
| `--rebuild-cache` | Read everything, local and remote, and replace the cache. |
| `--no-cache` or `CCTRACK_CACHE=0` | Read everything; neither read nor write the cache. |
| `CCTRACK_CACHE_DIR` | Cache location. |

Progress text says how much was reused, for example `Reading Claude transcripts [...] 12 changed of 3,480 files`.

*Step 4 addition.* `--no-cache` and `--rebuild-cache` are mutually exclusive (argparse error). A flag beats the environment: `--rebuild-cache` rebuilds even with `CCTRACK_CACHE=0`. `CCTRACK_CACHE` is off for `0`, `no`, `false` or `off` (case-insensitive, surrounding spaces ignored) and on for anything else, including unset or empty. With the cache off, cctrack never opens or creates the cache file, so the file stays byte-identical (opening it always writes: the probe and prune at open change its bytes). With `CCTRACK_TIMING=1`, a `cache` phase times opening and closing the store, and each parse phase's detail ends with `; cache: N reused, M read`.

### 4.6 Remote

Protocol v2, still one SSH call, remote still read-only:

1. Local sends, inside the program, a compressed manifest of what it holds for that host: `{kind: {path: [size, mtime_ns]}}` for entries with the current code hash. `--rebuild-cache`/`--no-cache` send an empty manifest.
2. Remote runs its normal discovery. For each file, in discovery order: if `(size, mtime_ns)` matches the manifest and the file is not racy by the remote clock, emit a hit marker; otherwise parse it and emit that file's `{events, stats}`. **Per file, before any cross-file dedup.**
3. Local walks the response in order, loading hits from the cache and storing misses, then runs `dedup_events` over the per-file lists itself. That is the same function over the same lists in the same order as `_parse_remote_files` runs today, so the per-host result is unchanged.
4. A hit the local cache cannot supply (pruned or corrupt between steps) triggers one full refetch with an empty manifest.

*Step 6 additions* (see `docs/plans/steps/06-remote-protocol-v2.md`):
- **Rows.** Remote rows live under host key `remote:<name>`. Their code is the local code key followed by `["remote", host, rkey]`. The local key covers the shipped source and the local payload format. `rkey` is the remote's interpreter and default text encoding. The manifest is `{rkey, files}`, and the remote ignores it when its own rkey differs.
- **The manifest is skipped** when the module on disk no longer matches the open store's key.
- **Hit marker.** It carries the file's current `(size, mtime_ns)`, and local `get` re-checks it, so a wrong remote hit costs a refetch, never a wrong report.
- **Misses.** A miss carries a storable flag set by the remote's racy and changed-during-read rules.
- **Hook files** are per-file items parsed with the remote blob's line splitting (`_hook_rows_from_blob_text`), in v1's file order across all hook dirs. Local joins them and does the one stable sort. Monthly and baseline files are always read. If a missed statusline file contains the monthly or baseline separator text, the remote returns v1's whole-blob hook result for that run, because the blob's partitioning then crosses file boundaries.
- **Local hook files** are cached per file too (host `local`, after `rollup_old_hook_files`).

An unreachable or timed-out host behaves as today: its data is missing and the warning says so. Cached data for that host is not used. (Decision D1.)

## 5. Definition of "exactly the same"

For a fixture corpus, stdout of each of these must be byte-identical across four runs: `--no-cache`, cold cache, warm cache, `--rebuild-cache`:

- default report; `--explore <date>`; `--sessions --bucket hour --coverage`
- `--json` (ignoring `generated_at`); `--csv sessions`, `buckets`, `coverage`, `rates`
- each of the above with `--remote <fakehost>`, and with `--agent claude` / `--agent codex`

The corpus must include, at minimum: a resumed Claude session (same request in two files); a subagent transcript without `agentId`; a compaction; an unparseable line; a Codex thread with `token_usage_record`s; a legacy `token_count` thread; a Codex fork whose parent is in another file; a `.jsonl.zst` rollout; a sandbox copy of a host session; the same session rsynced to the remote host; hook files on both machines including one older than 90 days. `tests/test_remote_parse.py` (`_fake_host`, `_fake_ssh`) and `tests/fixtures/ccusage-parity.json` are the starting points.

Mutations between runs, each followed by a warm run compared against `--no-cache`:

1. append lines to one transcript; 2. add a file; 3. delete a file; 4. replace a file's content keeping its size but changing mtime; 5. add a fork child whose parent is already cached; 6. add an rsynced copy of a cached session on the other machine; 7. change the code hash; 8. corrupt the store; 9. make the cache directory read-only; 10. truncate a file.

*Step 4 finding.* Cross-file dedup mutates its inputs only in `_remove_codex_fork_replays` (a parent's `_locations` gains the suppressed child replay's location; children get `_fork_history` and `_unresolved_fork`), and those mutations are idempotent when files are **added**. So a cache that wrongly stored post-dedup events passes mutation 5 and fails only when a cached fork child is **deleted** (a mutation 3 case: the parent's stale `_locations` still names the deleted file in `--json` and `--csv sessions`). Every step that tests "serialize before dedup" must include that deletion, and should also compare each stored entry directly with a fresh per-file parse.

## 6. Steps

### Step 0. Measurement and the equivalence harness

Nothing user-visible changes.

- `CCTRACK_TIMING=1` prints wall time per phase to stderr at exit: discovery, Claude parse, Codex parse, hook read, each remote host, reconcile, report. No effect on stdout.
- `scripts/bench_cache.py`: builds a synthetic corpus of configurable size (files, events, bytes per line) in a temp home and times a run.
- `tests/test_cache_equivalence.py`: the fixture corpus from section 5 and a helper that runs `main()` in every listed mode and returns stdout. For now it asserts only that two uncached runs match (proving the harness is deterministic).

Verify: timing lines appear only with the env var; stdout unchanged with it; the harness test fails if one fixture file is altered between its two runs.

**Then pause for the user:** ask Dan to run `CCTRACK_TIMING=1 uvx --from . cctrack --remote dgx` on the Mac and paste the timing lines. They set the priority of Steps 5 and 8.

### Step 1. Make pricing cheap

Independent of the cache; helps every run.

- Memoize the model → rate-row lookup (`_find_rate_row`, `_canonical_model`). The result depends only on the model string and `RATES`. **Planner decides** how invalidation works: tests replace `cctrack.RATES` with `monkeypatch.setattr` (`tests/test_cctrack.py`), so a memo keyed only on the model string would leak between tests.
- Remove `aggregate` passes whose results `reconcile_sources` overwrites. **Planner confirms** which values computed before `reconcile_sources` are still read afterwards (`local_install` is derived from the pre-reconcile hook totals).

Accept: all report output unchanged; `aggregate` on the bench corpus at least 5× faster than F9; full test suite unchanged.
Mutation: make the memo ignore `RATES` changes → an existing rate test must fail.

### Step 2. One per-file parse seam (refactor only)

Introduce a single function that turns one file into `{events, stats}` or "unreadable", and make `parse_events`, `parse_codex_events` and `_parse_remote_files` call it. Keep F4's differences as explicit parameters. No cache yet.

Accept: suite passes untouched; equivalence harness unchanged; `tests/test_remote_parse.py` still proves remote parse equals raw stream.
Mutation: drop the local-only `agent_id` filename fallback → `tests/test_detail.py` must fail.

### Step 3. Cache store, standalone

The store from 4.4 and the validity rules from 4.1 and 4.3, with no caller yet.

Accept:
- Round trip: for every case in `tests/fixtures/ccusage-parity.json` and the section 5 corpus, `parse → encode → decode → dedup_events` equals `parse → dedup_events` (F6).
- Hit/miss table: unchanged file hits; changed size, changed mtime, changed code hash, racy mtime, changed-during-read each miss; failures are not stored.
- Robustness: corrupt blob is a miss; corrupt database is recreated; read-only directory, missing `sqlite3` and a locked database each give a working uncached path and one stderr note.
- Budget: decoding 1M events takes under 3 s on the bench machine. Record the measured figure and the encoding chosen.
- `tests/conftest.py` points `CCTRACK_CACHE_DIR` at a per-test temp directory so no test can touch a real cache.

Mutation: skip the racy rule; skip the code-hash check; each must fail a test.

### Step 4. Local transcripts and rollouts through the cache

Wire Step 3 into the seam from Step 2 for local Claude and Codex files. Add `--rebuild-cache`, `--no-cache`, `CCTRACK_CACHE`. Serialize before cross-file dedup (F3). Keep discovery order (F5).

Accept:
- Section 5 equivalence for local runs, including mutations 1–5 and 7–10.
- **A fully warm run opens no transcript or rollout file.** After appending to one file, exactly that file is opened. Assert by counting opens in a test.
- `--rebuild-cache` opens every file and leaves a warm cache behind; `--no-cache` opens every file and leaves the cache byte-identical.
- `file_stats` for detail reports comes from the cache and matches an uncached run, including `--coverage`.
- Bench: warm run parse phase at least 10× faster than cold on a corpus with realistic line sizes.

Mutation: serialize after dedup instead of before → the fork-child mutation (5) must fail; reuse an entry without checking size → mutation 1 must fail.

### Step 5. Local hook data

Cache `_hook_rows_from_lines` per `statusline-*.jsonl` file (kind `hook`), then concatenate in the current file order and apply the same stable sort. `rollup_old_hook_files` still runs first. Monthly and baseline files are always read.

**Planner proves** that per-file-sorted lists, concatenated in file order and stable-sorted again, equal today's single sort, including rows with identical `(session_id, ts)` in different files.

Accept: hook totals, `hook_cost_usd` fields and `--coverage` hook findings identical cached and uncached; a file removed by rollup leaves no effect; only today's hook file is opened on a warm run.
Skip this step if Step 0 timings show hook reading is under 5% of a run.

### Step 6. Remote protocol v2

Section 4.6. `REMOTE_PROTOCOL` becomes v2. Covers Claude transcripts, Codex rollouts and hook files on the remote host.

**Also local hook files (moved here from Step 5, Dan's decision, 2026-11-20).** Step 5 was skipped, but its local half is included here because this step builds per-file hook entries anyway: cache each local `statusline-*.jsonl` file's rows (kind `hook`, host `local`) as Step 5's brief described, with `rollup_old_hook_files` still run first and monthly and baseline files always read. `docs/plans/steps/05-local-hook-data.md` holds the measurements and the proof that per-file sorted rows, joined in file order and stable-sorted again, equal today's single sort. It also lists how the remote hook read differs from the local one: no rollup, recursive glob, legacy file in sorted position, a single sort across folders, `splitlines()`. Remote hook entries must reproduce the remote's behaviour and local entries the local behaviour, exactly.

Accept (local hooks): hook totals, `hook_cost_usd` fields and `--coverage` hook findings identical cached and uncached; a file removed by rollup leaves no effect; only today's hook file is opened on a warm local run.

Accept:
- `tests/test_remote_parse.py` extended: remote-parse results still equal the raw stream; with a warm cache the results are still equal.
- Section 5 equivalence with `--remote`, including mutation 6 (rsynced copy) and a fork child added on the remote.
- On a warm run the remote opens only changed files (the response reports how many it parsed; assert it) and the response is smaller than the cold one.
- Remote host still unmodified after a run (compare a recursive listing with mtimes before and after).
- Hit the cache cannot supply → one automatic full refetch, correct result.
- Unreachable host: same warning and same report as today.
- Fallback path (`CCTRACK_REMOTE_PARSE=0`, old Python) unchanged and uncached.

Mutation: have the remote dedup across files before returning → a cross-file fixture must fail; ignore the manifest's mtime → mutation 1 on the remote must fail.

### Step 7. Documentation and release readiness

README section on the cache (what is stored, where, the three options, first run after an upgrade is a full read). `CLAUDE.md` architecture entry. `--help` text. Version 0.12.0. Do not release; hand back to Dan with the Step 0 timings before and after.

### Step 8. Resume inside a growing Claude transcript (optional, gated)

Only if Step 0 timings, re-measured after Step 6, show that re-reading active files is a meaningful share of a warm run.

Idea: for Claude transcripts only, keep the per-line entries from before the in-file dedup, the byte offset of the last complete line, and a fingerprint of the bytes before that offset. On a file that grew, verify the fingerprint, parse only the new bytes, and run the in-file dedup over old + new entries. That is exact by construction because Claude entries are a per-line map (F1), at the price of a larger payload.

**Planner must first show**, with a property test over the fixture corpus split at every line boundary, that resumed output equals a full parse, including stats and the advisor-iteration case. Codex rollouts are excluded: `parse_codex_lines` carries state across lines (turn context, running totals, legacy sequence).

## 7. Decisions for Dan

- **D1. Unreachable remote host.** Recommended: unchanged, its data is missing and the warning says so. Alternative: use that host's last cached data and print how old it is.
- **D2. Invalidation on upgrade.** Recommended: any code change invalidates the cache, so the first run after each upgrade reads everything. Alternative: a hand-maintained parser version, faster after rate-card-only releases, with the risk of stale numbers if a bump is forgotten.
  **Decided by Dan, 2026-11-20:** any change to cctrack's code, the Python version or the text encoding invalidates the cache (the Step 3 code key). The first run after each of these reads everything.
- **D3. Step 8.** Recommended: decide from the timings after Step 6.

## 8. Risks

| Risk | Guard |
|---|---|
| A cached file's events differ from a fresh parse | Code-hash key; racy rule; section 5 equivalence in every step |
| Cross-file dedup sees different input | Per-file payloads serialized before dedup (F3); discovery order kept (F5); fork and rsync mutations in the harness |
| Stale data after a parser fix | Whole-source hash (D2) |
| Cache breaks a report | Rule 5; robustness tests in Step 3 |
| Warm runs still slow | Step 1 before the cache; timings in Step 0 and Step 7 |
| Remote host modified | Listing comparison in Step 6 |
| Tests pollute a real cache | Per-test `CCTRACK_CACHE_DIR` in `conftest.py` (Step 3) |
