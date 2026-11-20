# Step 04. Local transcripts and rollouts through the cache

Plan for `### Step 4` of `docs/plans/incremental-cache.md`. Written against commit `0392f8c` (step 03 done; 457 passed, 1 skipped, about 16 s, Python 3.12.15).

No product decision is needed, and none of the section 7 decisions is needed (D2 is settled and this step follows it). One brief item cannot be met as written: the brief's mutation check "serialize after dedup → the fork-child mutation (5) must fail" does not fail with mutation 5. Mutation 3 does fail when the deleted file is a cached fork child, and so does a new direct test (finding 4, section 8). Sections 4.5 and 5 of the plan were edited (section 8).

## 1. Outcome

- `main()` opens the step 3 store once per run (`open_cache`) and closes it in its `finally`. It passes the store to `parse_events` and `parse_codex_events`. Both now call `_parse_log_file_cached` per file, keep discovery order, and serialize each file's payload at `put` time, before `dedup_events`.
- New options: `--no-cache` and `--rebuild-cache` (mutually exclusive), plus `CCTRACK_CACHE=0`. With the cache off, the cache file is never opened or created.
- A warm run opens no local transcript or rollout file. Hook files, remote hosts and the raw stream are still read every run (Steps 5 and 6).
- On a terminal, progress shows `N changed of M files`. With `CCTRACK_TIMING=1`, a `cache` phase is shown, and each parse phase's detail ends `; cache: N reused, M read`.
- `scripts/bench_cache.py` backdates the corpus it builds and starts each invocation with an empty cache, so run 1 is cold and later runs are warm.
- The harness gains a launcher that counts file opens through an `open` audit hook and makes `.zst` readable on every machine. New tests in `tests/test_cache_local.py` cover section 5 for local data (four cache modes × the whole matrix, and mutations 1–5 and 7–10), open counts, stats, options, progress and timing.
- stdout is byte-identical with the cache off, cold, warm or rebuilt.

## 2. Findings

All checks ran in this sandbox: Linux aarch64, Python 3.12.15, uid 501 (not root), no `zstd` CLI and no `compression.zstd`. `S=/tmp/claude-501/-home-sandy-dev-cctrack/b90020de-1841-4523-9564-a7f61b1fabce/scratchpad`. The scripts are in `$S/s4/`. The prototype is `$S/s4/proto/src`, which is HEAD plus the wiring in section 4 without progress or timing text. It was built by `$S/s4/patch_proto.py`. Large experiment files went to `/dev/shm` because `/tmp` has about 400 MB free.

1. **Baseline.** `PYTHONPATH=src python3 -m pytest tests -q` gives 457 passed, 1 skipped in 16 s. With the prototype wiring and the cache on by default, the unmodified suite also gives 457 passed, 1 skipped (`PYTHONPATH=$S/s4/proto/src`). No existing test depends on the cache being off. In-process tests run against conftest's per-test `CCTRACK_CACHE_DIR`. Their fixture files are freshly written, so they are racy and never stored.

2. **Equivalence and opens on the section 5 corpus** (`$S/s4/exp1.py`, `exp3.py`). I built the corpus, backdated every file by 1 h, and used `$S/s4/launch.py` (the launcher, section 4.6) with raw-frame `.zst` decoding. Then I ran all 7 variants × 8 modes, each with its own cache dir, four ways: `--no-cache`, cold, warm, `--rebuild-cache`.
   - 0 differences in 224 runs, which took 9.8 s in total (about 0.02–0.06 s per run).
   - Opens of local transcript and rollout files were as follows. The `local` and remote variants opened 9 with `--no-cache`, cold and rebuild, and 0 warm. `-claude` opened 5/5/5/0 and `-codex` opened 4/4/4/0.
   - The only other `.jsonl` a warm run opens is today's hook file (`.claude/cctrack/statusline-<day1>.jsonl`), which is Step 5's.
   - The fake remote's `python3 -` runs in another process, so its opens are not counted. That is correct here, because remote parsing is not cached in this step.

3. **Mutations** (`$S/s4/exp2.py`, `exp4.py`, `exp5.py`). Each mutation started from a fresh backdated corpus with one cold run. After the mutation, I compared `--no-cache` against the cached run for 3 local variants × 8 modes.
   - With the prototype, all 12 mutation cases gave 0 differences.
   - The first warm run after each mutation opened exactly the files below. The next run opened none, except with a read-only directory, which stays uncached.

     | Mutation | Opened on the first warm run | stderr note |
     |---|---|---|
     | 1 append, mtime restored | `s1-resumed.jsonl` | none |
     | 1b append, new mtime | `s1-resumed.jsonl` | none |
     | 2 add a file | the new file | none |
     | 3 delete (Claude `s1-resumed`, fork child `C`, fork parent `P`) | nothing | none |
     | 4 same size, new mtime | `lh.jsonl` | none |
     | 5 add fork child `C2` of cached `P` | `C2` | none |
     | 7 code hash (copy of `src` + one comment line) | all 9 | none |
     | 8 garbage store | all 9 | `cctrack: cache was unreadable and has been reset (…)` |
     | 9 read-only cache dir | all 9, and again on every run | `cctrack: cache unavailable (…: attempt to write a readonly database); reading all log files` |
     | 10 truncate, new mtime | host `s1.jsonl` | none |
     | 10s truncate, mtime restored | host `s1.jsonl` | none |

   - Every content mutation (1, 1b, 2, 3×3, 4, 5, 10, 10s) changes the `--json` output compared with before the mutation. So each case has teeth.
   - 10 and 10s do not change the default report. The truncated line (`r6`) still exists in the sandbox copy, so only `source_locations` change.

4. **"Serialize after dedup" is not caught by mutation 5.** I built a mutant (`$S/s4/ma`) that defers each `put` until after `dedup_events` returns.
   - Mutation 5 gave 0 differences. So did 1, 2, 3 (Claude), 3 (fork parent), 4, 8, 9 and 10.
   - Only **mutation 3 applied to the fork child `C`** differed, in 4 outputs: `local`/`local-codex` × `json`/`csv-sessions`.
   - The reason is in the code. `dedup_events` mutates its inputs only in `_remove_codex_fork_replays`:
     - the parent's `_locations` gains each suppressed child replay's location;
     - the children get `_fork_history` and lose or gain `_unresolved_fork`.

     `_merge_usage_copies` and `_expand_codex_snapshot_copies` build new dicts. All three mutations are idempotent when files are added or re-merged (locations are a set, `_fork_history` holds the child's own items, and `_unresolved_fork` is recomputed). They show only when a child that contributed a location disappears.
   - So the plan keeps mutation 5 in the harness, maps the brief's check to `3-fork-child`, and adds T6 (each stored entry equals a fresh per-file parse). T6 catches the mutant directly: `P`'s stored events carry `_locations`.

5. **The size check is caught end to end.** In the mutant `$S/s4/mz`, `CacheStore.get` does not compare `size`. Mutation 1 with the mtime restored differed in 14 outputs, and 10s in 10. With a new mtime (1b, 10), the mtime check alone catches the change. That is why mutation 1 must restore the mtime (the brief's check names mutation 1).

6. **Code-hash change** (`exp3.py`, `exp4.py`). A copy of `src` with one appended comment line opened all 9 files, then 0 on its next run. The output matched and nothing was printed. Alternating two source trees on one cache dir makes each run a full read, because there is one entry per path. I saw this by accident in the bench (finding 9). It is D2's documented cost.

7. **A `.zst` entry is used only while a decoder exists, end to end** (`exp4.py`).
   - With raw-frame decoding: cold and warm gave equal `--json`, and the warm run opened nothing (the `.zst` was served from the cache).
   - Then with no decoder: the cached run equalled `--no-cache` (`--json` and the default report), and both printed `Note: skipped 1 compressed Codex rollout(s)`.
   - The no-decoder output differs from the raw output, so the case has teeth.

8. **`--no-cache` must not even open the store.** Opening an existing store and closing it straight away changed the file's sha256 and mtime: the write probe `PRAGMA user_version = 1` and the prune commit rewrite the header. So "leaves the cache byte-identical" means `main` must not call `open_cache` when the cache is off.

9. **Bench** (`$S/s4/bench.py` is `scripts/bench_cache.py` with `PYTHONPATH` from `$BENCH_SRC`; corpus in `/dev/shm`, mtimes backdated by hand).
   - Default options (`--filler-lines 2`: 200 Claude files, 20,000 events, 160 MB; 50 Codex files, 30 MB):
     - cold claude parse 0.342 s and codex 0.142 s;
     - warm 0.042/0.030 and 0.037/0.017;
     - so the parse phases (Claude + Codex) are only **7–9× faster**.
   - `--filler-lines 6` (374 MB Claude, 84 MB Codex, 25,000 events, 55 events/MB):
     - cold 0.51–0.59 + 0.19–0.24 s;
     - warm 0.037–0.041 + 0.016–0.019 s;
     - so **12–14×**.
   - Real logs have about 48 events/MB (step 03, finding 2), so `--filler-lines 6` is the bench setting with realistic line sizes. The default's 125 events/MB is not.
   - The bench lines are also simpler JSON than real lines, so real logs gain more. On this sandbox's real logs (14 files, 24 MB, copied with mtimes kept) claude parse took 0.081 s cold and 0.003 s warm, about **27×**.
   - The bench must backdate the files it writes. Otherwise the files written in the last 2 s of the build are racy and are re-read on the warm run.

10. **GC tuning is a dead end at this scale.** I put `gc.disable()` before the cache opens (`$S/s4/pgc`, its own copy of the bench dir). The warm parse phases went from 0.054 s to 0.047–0.050 s. Do not add GC tuning.

11. **The interrupted-phase marker.** `test_timing_printed_when_interrupted` runs `main()` in-process, so with this step the cache is on there. It expects `(interrupted)` on the `codex parse` line. `_Timing.finish` puts the marker on the current phase. If `finally` switches to a `cache` phase before closing the store, the marker moves and that existing test fails. So switch to the `cache` phase in `finally` only when the run is not unwinding.

12. **Old vs new.** `$S/s4/compare4.py OLD NEW` builds and backdates the corpus, then runs the whole matrix with the old tree (uncached) and three times with the new tree: `CCTRACK_CACHE=0`, cold and warm (the last two on one cache dir). Old HEAD against the prototype gave 56 entries, 0 differences in all three, and the cache file existed afterwards.

13. **Live smoke** (`$S/s4/live_cache.sh`). It snapshots `~/.claude/projects` into `/dev/shm` with `cp -a`, which keeps mtimes, and sets `CCTRACK_CACHE_DIR` there. Nine modes were run four ways each: all the same. The warm timing showed claude parse at 0.003 s. `~/.cache/cctrack` did not exist before or after.

14. **The harness's reference runs must stay uncached.** `run_env` builds a fresh environment, with `XDG_CACHE_HOME=<corpus>/xdg-cache` and no `CCTRACK_CACHE_DIR`. Without a change, every existing harness test (`baseline`, `test_two_uncached_runs_match`, the timing tests) would silently use a cache that warms up partway through the module, as files stop being racy. That would make `baseline` a mix of cold and warm runs, not a reference. Section 4.6 sets `CCTRACK_CACHE=0` in `run_env`'s defaults.

## 3. Files

| File | Change |
|---|---|
| `src/cctrack/__init__.py` | Add `cache_mode`, `_cache_hits`, `_note_cache_use` and `_Timing.note` (plus `notes` in `__init__`/`finish`). Give `parse_events` and `parse_codex_events` a `cache` keyword. Add the two options in `main` and open/close the store there. Update the "Parse cache" comment block ("Not called by any report path yet" goes). |
| `tests/test_cache_equivalence.py` | `run_env` defaults gain `"CCTRACK_CACHE": "0"` (finding 14). Add the launcher text and the helpers `backdate`, `run_launched`, `Launched` and `local_log_files` (section 4.6). No test is removed or weakened. |
| `tests/test_cache_local.py` (new) | Tests T1–T17 (section 6). |
| `scripts/bench_cache.py` | Backdate the built corpus (`--age`, default 3600 s). Clear `<root>/xdg-cache` before the first run (`--keep-cache` opts out). Update the docstring and `--help`. |
| `CLAUDE.md` | Rewrite item 4a (wired in, options, `cache_mode`, progress and timing), add `--no-cache`/`--rebuild-cache` to Commands, and note the launcher in the harness sentence. Suggested text in section 4.7. |
| `README.md` | New short "Parse cache" section after "Remote aggregation" (section 4.7). |
| `docs/plans/incremental-cache.md` | Already edited by the planner (section 8). |

## 4. Interfaces

### 4.1 Cache mode

```python
_CACHE_OFF_VALUES = ("0", "no", "false", "off")

def cache_mode(no_cache: bool = False, rebuild: bool = False) -> str:
    """'off', 'rebuild' or 'on' for this run. --no-cache wins; then --rebuild-cache (a flag
    beats the environment); then CCTRACK_CACHE, which is off when its stripped, lower-cased
    value is one of _CACHE_OFF_VALUES; otherwise on (unset and empty included)."""
```
Place it in the "Parse cache" section after `open_cache`. Read `os.environ` at call time.

```python
def _cache_hits(store) -> int:
    """store.counts["hits"], or 0 for None."""

def _note_cache_use(timing: "_Timing", phase: str, store, hits_before: int, n_files: int) -> None:
    """With a store: timing.note(phase, f"cache: {reused:,} reused, {n_files - reused:,} read"),
    where reused = _cache_hits(store) - hits_before. Nothing without a store."""
```

### 4.2 Parsers

```python
def parse_events(files: list[str], file_stats: dict | None = None,
                 cache: "CacheStore | None" = None) -> list[dict]: ...

def parse_codex_events(files: list[str], source: str = "local",
                       file_stats: dict | None = None,
                       cache: "CacheStore | None" = None) -> tuple[list[dict], int]: ...
```
The per-file call becomes:
- `_parse_log_file_cached(cache, "claude", path, want_stats=file_stats is not None, agent_id_from_name=True)`;
- `_parse_log_file_cached(cache, "codex", path, source=source, want_stats=file_stats is not None)`.

Everything else in the loops stays as it is: unreadable stats, `.zst` skip counting, the append order and `dedup_events`. With `cache=None`, `_parse_log_file_cached` goes straight to `_parse_log_file`, so the step 2 seam tests still see the same calls.

Progress, in both functions:
- `hits0 = _cache_hits(cache)` before the loop.
- At the top of each iteration, without a cache, the text stays `f"{i:,}/{n:,} files"`. With a cache it is `f"{i:,}/{n:,} files, {(i - 1) - (_cache_hits(cache) - hits0):,} changed"`.
- After the loop, and only with a cache, one forced update: `progress.update(f"{changed:,} changed of {n:,} files", force=True)`, where `changed = n - (_cache_hits(cache) - hits0)`. Then, as today, `"removing duplicates"` (Claude) or `done()` (Codex).

Here `n = len(files)`.

### 4.3 `main`

Options. Add these to the main parser right after `--agent`, in a mutually exclusive group:
```python
cache_opts = parser.add_mutually_exclusive_group()
cache_opts.add_argument("--no-cache", action="store_true",
    help="Read every log file; don't read or write the parse cache (or set CCTRACK_CACHE=0).")
cache_opts.add_argument("--rebuild-cache", action="store_true",
    help="Read every log file and replace the parse cache.")
```
Both flags together make argparse exit 2 with `argument --rebuild-cache: not allowed with argument --no-cache`.

Lifecycle:
- Define `store = None` next to `files`, `codex_files`, `hook_dirs` and `unwinding`, before the `try`.
- Inside the `try`, **after** the `--install-hook`/`--uninstall-hook` returns (those come before the `try` today, which is fine) and before the first `timing.phase("discovery")`:
  ```python
  mode = cache_mode(args.no_cache, args.rebuild_cache)
  if mode != "off":
      timing.phase("cache")
      store = open_cache(rebuild=mode == "rebuild")
  timing.phase("discovery")
  ```
- Claude:
  ```python
  h0 = _cache_hits(store)
  local_events = parse_events(files, file_stats=file_stats, cache=store)
  _note_cache_use(timing, "claude parse", store, h0, len(files))
  ```
- Codex: the same, around `parse_codex_events(codex_files, file_stats=file_stats, cache=store)` with phase `"codex parse"`.
- Remote fetches, the raw stream, hook reading and reconcile are unchanged.
- In `finally`, before `timing.finish(...)`:
  ```python
  if store is not None:
      if not unwinding:
          timing.phase("cache")     # finding 11: never while unwinding
      store.close()                 # never raises; flushes pending rows
  ```
  With `unwinding`, closing is booked to the interrupted phase. `close()` flushes, so files parsed before a Ctrl-C are kept (plan 4.4).

### 4.4 `_Timing.note`

```python
def note(self, name: str, text: str) -> None:
    """Text appended to a phase's detail at finish (after the census text)."""
```
`__init__` (enabled branch) sets `self.notes: dict[str, str] = {}`. In `finish`, the order is:
1. the census loop, as today;
2. `for name, text in self.notes.items(): d = self.details.get(name, ""); self.details[name] = f"{d}; {text}" if d else text`;
3. the interrupted marker.

When `enabled` is false, `note` does nothing. Example line:
`[timing] claude parse   0.041 s  200 files, 374.0 MB; 200 changed in last 24 h (374.0 MB); cache: 200 reused, 0 read`.
That line still matches `TIMING_RE` and `^\d[\d,]* files, `.

### 4.5 `scripts/bench_cache.py`

- `--age SECONDS` (default `3600`). Right after building a corpus (not when reusing one), set every file under `root/home` and `root/remote-home` to `os.utime(f, ns=(t, t))`, with `t = time.time_ns() - age * 10**9`. `--age 0` skips this. Add `age` to the saved `opts`, so a change rebuilds the corpus.
- `--keep-cache`. Without it, `shutil.rmtree(root / "xdg-cache", ignore_errors=True)` runs before the first run, so run 1 is always cold and runs 2… are warm. Extra cctrack args still pass through after `--`, for example `-- --no-cache`.
- Docstring and `--help`: "Run 1 is cold, later runs are warm; `--filler-lines 6` gives about real logs' 48 events/MB."
- Stdout, JSON fields and exit codes are unchanged otherwise.

### 4.6 Harness helpers (`tests/test_cache_equivalence.py`)

- `run_env` defaults gain `"CCTRACK_CACHE": "0"`, with the comment "reference runs are uncached; cache tests pass CCTRACK_CACHE=1". The `env` argument still overrides it.
- `backdate(corpus, secs=3600) -> int`: one `t = time.time_ns() - secs * 10**9`, then `os.utime(f, ns=(t, t))` for every file under `corpus.home` and `corpus.remote_home`. Returns `t`. Needed because freshly written files are racy and never stored.
- `local_log_files(corpus, agent="all") -> list[str]`: sorted paths, relative to `corpus.root`, of every `*.jsonl`/`*.jsonl.zst` under `corpus.home` whose relative path has no `cctrack` component (hook dirs). `agent="claude"` keeps the `projects` paths and `"codex"` the `.codex` paths. On the corpus this gives 9 paths, 5 Claude and 4 Codex.
- `LAUNCHER`: Python source written once to `corpus.root / "launch" / "cctrack_launch.py"`. It does three things:
  1. If `CCTRACK_TEST_OPEN_LOG` is set, it opens that file with `os.open(..., O_WRONLY|O_CREAT|O_APPEND)` and `sys.addaudithook`s a hook. On event `"open"` with a `str`/`bytes` path ending `.jsonl` or `.jsonl.zst`, the hook writes `os.fsdecode(path) + "\n"` with `os.write`. It must not call `open()` itself, which would recurse.
  2. It imports `cctrack`. With `CCTRACK_TEST_ZSTD=raw`, it sets `cctrack._read_zstd` to a raw-frame reader (the same logic as `test_cache_store._raw_frame_text`; it must read the file with `open(path, "rb")` so the hook sees it) and `cctrack._zstd_available = lambda: True`. With `none`, it sets `_read_zstd = lambda p: None` and `_zstd_available = lambda: False`. With anything else, it changes nothing.
  3. It runs `sys.argv = ["cctrack", *sys.argv[1:]]; cctrack.main()`.

  The prototype is `$S/s4/launch.py`. The `CCTRACK_TEST_*` names are read only by the launcher, never by product code.
- `Launched` dataclass: `stdout` (after `normalise`), `stderr`, and `opens`. `opens` is the sorted list, duplicates kept, of logged paths under `corpus.root`, relative to it, that have no `cctrack` component.
- `run_launched(corpus, argv, env=None, *, zstd="raw", src=None, cache_home=None) -> Launched`:
  - runs `[corpus.python, <launcher>, *argv]` with `cwd=corpus.root`, `stdin=DEVNULL` and a 120 s timeout;
  - the environment is `run_env(corpus, {"CCTRACK_CACHE": "1", "PYTHONPATH": str(src or SRC), "CCTRACK_TEST_ZSTD": zstd, "CCTRACK_TEST_OPEN_LOG": <fresh temp file under corpus.root/"launch">, **({"XDG_CACHE_HOME": str(cache_home)} if cache_home else {}), **(env or {})})`;
  - it asserts exit code 0, reads and deletes the log, and returns.
- `cache_db(cache_home) -> Path`: `Path(cache_home) / "cctrack" / "cache.sqlite3"`. The default `cache_home` is `corpus.root / "xdg-cache"`.

### 4.7 Documentation text

`CLAUDE.md` item 4a, replacing the current text:
> 4a. **Parse cache** (`open_cache`, `CacheStore`, `_parse_log_file_cached`, `_encode_cache_payload`, `cache_code_key`, `cache_mode`) - Local Claude transcripts and Codex rollouts go through it: `main` opens the store once (unless `cache_mode` says off), passes it to `parse_events`/`parse_codex_events`, and closes it in its `finally` (never switching timing phase while unwinding). One SQLite file `cache.sqlite3` under `$CCTRACK_CACHE_DIR` / `$XDG_CACHE_HOME/cctrack` / `~/.cache/cctrack` (dir 0700, file 0600), one entry per log file keyed `(host, kind, path)`, valid while `(size, mtime_ns)` and the code key (module sha256 + interpreter + marshal version + default text encoding + parse params) match. Payload is `zlib(marshal({events, stats}))` encoded at `put` time, before any cross-file dedup (dedup mutates fork parents' `_locations`); zlib's checksum makes a corrupt blob a miss. Racy (mtime within 2 s, or changed during the read) and failed parses are never stored; a `.zst` hit needs a decoder. `--no-cache`/`CCTRACK_CACHE=0` never opens the file (opening writes); `--rebuild-cache` deletes and refills it; the flags are exclusive and beat the env var. Hook files, `--remote` hosts and the raw stream are not cached yet. `sqlite3` is imported lazily; any store error prints one `cctrack: cache ...` note and the run continues uncached. `tests/conftest.py` gives every test its own `CCTRACK_CACHE_DIR`; the harness's `run_env` defaults to `CCTRACK_CACHE=0`, and `run_launched` (an `open` audit hook) counts the log files a run opens.

Commands block: add `cctrack --no-cache` and `cctrack --rebuild-cache` comment lines, and `python3 scripts/bench_cache.py --filler-lines 6 --runs 3` ("run 1 cold, later runs warm").

`README.md`, a new section after "Remote aggregation":
> ## Parse cache
>
> cctrack remembers what it read from each log file, so a repeat run re-reads only the files that changed. The cache is one file, `~/.cache/cctrack/cache.sqlite3` (or under `$XDG_CACHE_HOME/cctrack`, or `$CCTRACK_CACHE_DIR`), readable only by you. It holds ids, file paths, working directories, timestamps, models and token counts, never prompt or response text, and never prices: costs are worked out fresh on every run, so a rate-card fix applies to all history. After any change to cctrack, the Python version or the text encoding, the next run reads everything again.
>
> ```bash
> cctrack --rebuild-cache   # read everything and replace the cache
> cctrack --no-cache        # read everything; leave the cache alone (or CCTRACK_CACHE=0)
> ```
>
> Statusline hook files and `--remote` hosts are still read in full on every run.

## 5. Behaviour and edge cases

- **Order (F5).** Hits and misses go into `event_lists` in discovery order, interleaved, exactly where an uncached run puts them. Never collect hits and misses separately (T5, M4).
- **F3.** `put` encodes at the moment of the call inside `_parse_log_file_cached`, before `dedup_events`. Never defer `put` until after dedup (finding 4; T6 and `T2[3-fork-child]`, MB1).
- **Off means untouched.** With `mode == "off"`, do not call `open_cache`, `cache_dir().mkdir` or anything else that touches the cache path (finding 8). No `cache` timing phase either.
- **Open after the hook commands.** `--install-hook`/`--uninstall-hook` return before the store is opened, so they never create a cache (T13).
- **Precedence.** `--no-cache` > `--rebuild-cache` > `CCTRACK_CACHE`. Argparse rejects both flags together. `CCTRACK_CACHE=1`, `yes`, `` or unset all mean on.
- **Open failures.** `open_cache` returns `None` after one note (read-only dir, missing sqlite3, lock, unrecoverable corruption). The run then continues uncached, and the progress text and timing note use the no-cache forms. `--rebuild-cache` with an unwritable dir is the same: one note, uncached.
- **Mid-run store errors** disable the store (step 3). Later files bypass it, and the "changed" count includes them. The output is unaffected.
- **Stats.** With a store, every parse collects stats (plan 4.2), and the caller gets `stats=None` unless it asked. Detail reports get `file_stats` from cache hits, which are fresh objects per decode.
- **Unreadable files** (missing, a directory, an undecodable `.zst`) are attempted every run, are never stored, and keep today's `{"unreadable": True}` stats and skip count.
- **Interrupt.** The `finally` closes the store without changing the timing phase while unwinding. `close()` flushes, so rows for files parsed before the interrupt are kept (T14). An exception inside `close()` is impossible (it swallows everything).
- **Remote and raw stream.** `_remote_collect`, `_parse_remote_files` and `_remote_parse` are untouched and uncached. A `--remote` run stores only `host='local'` rows (T10). `--rebuild-cache` deletes the whole file, which today holds only local rows.
- **`--dirs`.** Custom transcript dirs are cached like the defaults (key is the path).
- **The active session's transcript** is usually racy (written within 2 s), so it is re-read every run and not stored. That is intended (plan 4.1).
- **Two runs at once** are handled by step 3's store (short `BEGIN IMMEDIATE` batches, busy timeout).

## 6. Tests

New file `tests/test_cache_local.py`. Import `build_corpus`, `run_launched`, `backdate`, `local_log_files`, `cache_db`, `modes`, `VARIANTS`, `token_count`, `TIMING_RE`, `_stable` and `_utc_today` from `test_cache_equivalence`, and `claude_line`, `codex_line`, `codex_usage` from `test_detail`. Use the `sys.path.insert(0, str(Path(__file__).parent))` pattern. `_canon(x) = json.dumps(cctrack._tag_json(x))` without `sort_keys`, as in `test_cache_store`.

Fixtures:
- `corpus` (module scope): `build_corpus(tmp_path_factory.mktemp("c4"))` followed by `backdate(corpus)`. Tests that write to it use their own `cache_home` under `corpus.root` and must not modify the corpus files. Mutation tests build their own corpus.
- In-process tests use `build_corpus(tmp_path, settle=False)` + `backdate`. They set `HOME` and `SANDY_HOME` to the corpus home with `monkeypatch.setenv`, call `monkeypatch.delenv("CODEX_HOME", raising=False)`, and `monkeypatch.setattr` `cctrack._read_zstd` to the raw-frame reader (import `_raw_frame_text` from `test_cache_store`) and `cctrack._zstd_available` to `lambda: True`. They open stores through `cctrack.open_cache()`, which uses conftest's per-test `CCTRACK_CACHE_DIR`.

Wrap the subprocess tests (T1–T4, T9, T10, T16) in the `_stable` midnight guard where they compare outputs.

| # | Test | Proves |
|---|---|---|
| T1 | `test_cache_modes_match_uncached(corpus)` | For every `(v, m)` in `VARIANTS` × `modes(corpus)` (56), with `cache_home = corpus.root / "c1" / f"{v}-{m}"`, run five times: `--no-cache`, cold, warm, `--rebuild-cache`, then warm again. Each run gets `vargs + margs (+ flag)` and the variant's env. All five stdouts are equal. `opens` is `local_log_files(corpus, agent)` for `--no-cache`, cold and rebuild, and `[]` for both warm runs. `agent` is `claude`/`codex` for the `-claude`/`-codex` variants, else `all`. No stderr line starts `cctrack: cache`. (About 280 runs, about 12 s.) |
| T2 | `test_mutation_then_warm_matches_uncached(tmp_path, name)`, parametrized over the 13 cases below | Build a corpus, `t = backdate(c)`, take a `--json --no-cache` snapshot `before`, do one cold run, then apply the mutation. Then: (a) the first cached default-report run's `opens` equals the expected list, and its `cctrack: cache` stderr lines are as expected; (b) for `v` in `local`, `local-claude`, `local-codex` and `m` in `report`, `explore`, `detail`, `json`, `--no-cache` and cached stdout are equal; (c) for content mutations, `--json --no-cache` now differs from `before`; (d) a further cached run opens `[]`, except `9-read-only`, which opens all 9 again. |
| T3 | `test_no_cache_leaves_cache_untouched(corpus)` | A cold run into `cache_home = corpus.root / "c3"`. Snapshot `{name: (size, st_mtime_ns, sha256)}` of every file in `cache_home/cctrack`. Then three runs: `--no-cache`; `CCTRACK_CACHE=0`; `CCTRACK_CACHE=" Off "`. Each opens all 9, and the snapshot is unchanged after each. A `--no-cache` run with a fresh `cache_home` that doesn't exist leaves it non-existent. |
| T4 | `test_rebuild_replaces_the_cache(tmp_path)` | Own corpus, backdated, cold run. Delete `s1-resumed.jsonl`. `--rebuild-cache` opens all 8 remaining, prints no `cctrack: cache` line, and leaves exactly the 8 current paths in `entries` (read with `sqlite3`). The next run opens `[]`. Then overwrite the db with garbage: `--rebuild-cache` prints no note, opens 8, and leaves a working cache (the next run opens `[]`). |
| T5 | `test_parse_through_cache_keeps_order_and_stats(tmp_path, monkeypatch)` | In-process. `files = discover_files(default_dirs())` and `cx = discover_codex_files()`. Reference: `parse_events(files, file_stats=fs)` and `parse_codex_events(cx, file_stats=fsc)` with no cache. With one store: (1) cold; `store.flush()`; (2) warm; (3) append a usage line to `files[-1]` and a `token_count` line to `cx[-1]`, set both mtimes to `t + 5 s`, recompute the uncached reference, then run cached. At each stage, `_canon([events, fs])` (Claude) and `_canon([events, skipped, fsc])` (Codex) equal the uncached reference of the same moment. A wrapped `cctrack._read_log_lines` counts reads: stage 1 = `len(files) + len(cx)`, stage 2 = 0, stage 3 = 2. Also, `parse_events(files, cache=store)` with no `file_stats` equals the uncached `parse_events(files)` by `_canon`. |
| T6 | `test_stored_entries_equal_fresh_parse(tmp_path, monkeypatch)` | In-process, same setup. A cold `parse_events(files, cache=store)` and `parse_codex_events(cx, cache=store)` (the corpus has fork parent `P` and child `C` in different files), then `store.close()`. Read every row through a separate `sqlite3` connection. There are `len(files) + len(cx)` rows, all `host == "local"`. Each row's `code == f"{store.code_key} {cctrack._cache_params('local', path, path, kind == 'claude')}"`. `_canon(_decode_cache_payload(payload)) == _canon(_parse_log_file(kind, path, want_stats=True, agent_id_from_name=kind == "claude"))`. None of the decoded events has `_locations`, `_fork_history` or `_unresolved_fork`. |
| T7 | `test_warm_run_after_append_opens_only_that_file` | This is `T2[1-append]` and `T2[1b-append-new-mtime]`. List them in the mapping table; do not write a separate function. |
| T8 | `test_zst_entry_needs_a_decoder(tmp_path)` | Own corpus, backdated. With `zstd="raw"`: cold `--json`, then warm `--json` (`opens == []`, same stdout). With `zstd="none"`: `--no-cache --json` and cached `--json` have equal stdout, both stderrs contain `skipped 1 compressed Codex rollout`, and the stdout differs from the raw one. The same holds for the default report. |
| T9 | `test_remote_runs_cache_only_local_files(tmp_path)` | Own corpus, backdated. A cold `--remote fakehost` run, then: `SELECT DISTINCT host FROM entries` is `[("local",)]`, the row count is 9, and every path starts with `str(corpus.home)`. `corpus.remote_home / ".cache"` does not exist. A warm `--remote fakehost --json` equals `--no-cache --remote fakehost --json`, and the warm `opens == []`. |
| T10 | `test_cache_mode(monkeypatch)` | Unit. Env unset, `""`, `"1"`, `"yes"` → `on`. `"0"`, `"no"`, `"FALSE"`, `" off "` → `off`. `cache_mode(True, False)` → `off` for any env. `cache_mode(False, True)` → `rebuild`, also with `CCTRACK_CACHE=0`. |
| T11 | `test_cache_flags_are_exclusive(monkeypatch, capsys)` | `sys.argv = ["cctrack", "--no-cache", "--rebuild-cache"]`: `main()` raises `SystemExit` with code 2, and stderr contains `not allowed with argument --no-cache`. `cctrack.cache_dir()` does not exist afterwards. |
| T12 | `test_help_lists_cache_options(capsys)` | `main()` with `--help` exits 0, and stdout contains `--no-cache`, `--rebuild-cache` and `CCTRACK_CACHE=0`. |
| T13 | `test_hook_commands_do_not_open_the_cache(monkeypatch)` | Monkeypatch `cctrack.install_hook` and `cctrack.uninstall_hook` to no-ops. `main()` with `--install-hook`, then with `--uninstall-hook`: `cctrack.cache_dir()` does not exist. |
| T14 | `test_interrupted_run_keeps_parsed_files(tmp_path, monkeypatch, capsys)` | In-process corpus home (backdated). Set `CACHE_FLUSH_SECONDS = 3600` and `CACHE_FLUSH_BYTES = 1 << 40`, so nothing flushes before `close`. Set `parse_codex_events` to raise `KeyboardInterrupt` (signature `*a, **k`), and `CCTRACK_TIMING=1`. `main()` raises `KeyboardInterrupt`. Afterwards the db holds one row per Claude file (5), and stderr has `(interrupted)` on the `[timing] codex parse` line and not on a `cache` line. |
| T15 | `test_progress_reports_changed_files(tmp_path, monkeypatch)` | Set `cctrack.progress_enabled` to `lambda: True` and `sys.stderr` to a `StringIO`. With three backdated Claude files and one store: the cold `parse_events` output contains `3 changed of 3 files`. After `flush`, the warm one contains `0 changed of 3 files`. Without a store, the output never contains `changed`. The same for `parse_codex_events` with two rollouts (`2 changed of 2 files`, then `0 changed of 2 files`). |
| T16 | `test_timing_reports_cache_use(tmp_path)` | Own corpus, backdated, `CCTRACK_TIMING=1`. Parse the stderr with `TIMING_RE`. Cold: a top-level `cache` phase exists, `claude parse` detail ends `; cache: 0 reused, 5 read`, and `codex parse` ends `; cache: 0 reused, 4 read`. Warm: `5 reused, 0 read` and `4 reused, 0 read`. With `--no-cache`: no `cache` phase, and no detail contains `cache:`. The last two top-level lines are still `report`, `total`. |
| T17 | `test_reference_runs_are_uncached(tmp_path)` | `c = build_corpus(tmp_path, settle=False)`, `backdate(c)`, then `run_cctrack(c, [])` twice: `c.root / "xdg-cache"` does not exist. This guards finding 14. |

**T2 cases.** Paths are relative to `corpus.root`. `P1 = local-home/.claude/projects/-repo`. `t` is the backdate time. `day1 = c.explore_date`.

| Case | Mutation | First-warm `opens` | Note | Content |
|---|---|---|---|---|
| `1-append` | Append `claude_line(f"{day1}T04:00:00Z", "r7", sid="s1", inp=777)` to `P1/s1-resumed.jsonl`, then restore its mtime to `t` | `[P1/s1-resumed.jsonl]` | none | yes |
| `1b-append-new-mtime` | Same append, mtime `t + 3 s` | same | none | yes |
| `2-add` | New `P1/new.jsonl` with `claude_line(f"{day1}T04:00:00Z", "rn", sid="sn", inp=999)`, mtime `t` | `[P1/new.jsonl]` | none | yes |
| `3-delete-claude` | Delete `P1/s1-resumed.jsonl` | `[]` | none | yes |
| `3-fork-child` | Delete the rollout `*-C.jsonl` | `[]` | none | yes |
| `3-fork-parent` | Delete the rollout `*-P.jsonl` | `[]` | none | yes |
| `4-same-size` | In `P1/lh.jsonl` replace the first `"input_tokens": 500` with `"input_tokens": 600` (assert the size is unchanged), mtime `t + 5 s` | `[P1/lh.jsonl]` | none | yes |
| `5-fork-child-added` | In `*-P.jsonl`'s directory write `rollout-{day1}T09-00-00-C2.jsonl`: `session_meta {"id": "C2", "session_id": "C2", "forked_from_id": "P", "cwd": "/w"}`, `turn_context {"model": "gpt-5.4"}`, `token_count(…, codex_usage(1000, out=100), codex_usage(1000, out=100))`, `token_count(…, codex_usage(700, out=70), codex_usage(1700, out=170))`. mtime `t` | `[<that path>]` | none | yes |
| `7-code-hash` | Cached runs use `src = tmp_path/"src2"`, a `shutil.copytree(SRC, …)` with `"\n# step 4 code-hash mutation\n"` appended to `cctrack/__init__.py` | all 9 | none | no |
| `8-corrupt` | Write `b"garbage" * 1000` over `cache_db(...)` | all 9 | exactly one, starting `cctrack: cache was unreadable and has been reset` | no |
| `9-read-only` | `chmod 0o500` the `xdg-cache/cctrack` dir (skip if `os.geteuid() == 0`; restore `0o700` in a `finally`) | all 9, on every run | exactly one per cached run, starting `cctrack: cache unavailable` | no |
| `10-truncate` | Drop the last line of `P1/s1.jsonl`, mtime `t + 7 s` | `[P1/s1.jsonl]` | none | yes (`--json`) |
| `10s-truncate-same-mtime` | Same, but restore mtime `t` | `[P1/s1.jsonl]` | none | yes (`--json`) |

### Brief criteria → tests

| Criterion | Proof |
|---|---|
| Section 5 equivalence for local runs (`--no-cache`, cold, warm, `--rebuild-cache`) | T1 (all 56 matrix entries, including the remote variants, whose local half is cached) |
| … including mutations 1–5 and 7–10 | T2: 1, 1b, 2, 3×3, 4, 5, 7, 8, 9, 10, 10s |
| A fully warm run opens no transcript or rollout file | T1 (warm `opens == []` for every entry), T2 (d), T9 |
| After appending to one file, exactly that file is opened | T2 `1-append`, `1b-append-new-mtime` (T7) |
| `--rebuild-cache` opens every file and leaves a warm cache behind | T1 (rebuild opens all; the following warm opens `[]`), T4 |
| `--no-cache` opens every file and leaves the cache byte-identical | T3 (and `CCTRACK_CACHE=0`/`off`) |
| `file_stats` comes from the cache and matches an uncached run, including `--coverage` | T1 (`detail` = `--sessions --bucket hour --coverage`, `json`, `csv-coverage` with warm `opens == []`), T5 (strict `_canon` of `file_stats`) |
| Bench: warm parse phase ≥ 10× faster than cold with realistic line sizes | Verification 5 (a measurement, as in steps 1 and 3). Planner: 12–14× (finding 9) |
| Serialize before cross-file dedup (F3) | T6, `T2[3-fork-child]` |
| Keep discovery order (F5) | T5 |
| Added: options, precedence, env values | T10, T11, T12 |
| Added: `.zst` decoder rule end to end | T8 |
| Added: remote stays uncached in this step | T9 |
| Added: hook commands don't touch the cache | T13 |
| Added: interrupt keeps progress; timing marker stays put | T14, plus the existing `test_timing_printed_when_interrupted` |
| Added: progress and timing say how much was reused | T15, T16 |
| Added: harness reference runs stay uncached | T17 |

### Mutation checks

Run each on a copy of `src/`, one at a time (`rm -rf $S/m4 && cp -r src $S/m4 && <edit> && PYTHONPATH=$S/m4 python3 -m pytest tests -q -x -k '<tests>'`). The subprocess helpers follow `PYTHONPATH` through `SRC`.

| # | Change | Must fail |
|---|---|---|
| MB1 (brief) | Serialize after dedup. In `_parse_log_file_cached`, replace `store.put(host, kind, path, sig, r, params)` with `_DEFERRED_PUTS.append((store, host, kind, path, sig, r, params))` (a new module-level list). In `parse_events` and in `parse_codex_events`, right after `dedup_events(event_lists)` returns, run `for a in _DEFERRED_PUTS: a[0].put(*a[1:])` and then `_DEFERRED_PUTS.clear()` | `T2[3-fork-child]`, T6. (`T2[5-fork-child-added]` passes: finding 4) |
| MB2 (brief) | Reuse without checking size: in `CacheStore.get`, drop `size != sig[0] or ` | `T2[1-append]`, `T2[10s-truncate-same-mtime]` |
| M3 | `--no-cache` still opens the store: always run `store = open_cache(rebuild=mode == "rebuild")`, and pass `store if mode != "off" else None` to both parsers | T3 |
| M4 | Order: in `parse_events`, append results that were cache hits to a separate `hits` list and use `event_lists + hits` for dedup | T5 |
| M5 | Delete the `store.close()` call in `main`'s `finally` | T1 (warm opens all), T14 |
| M6 | Ignore rebuild: `open_cache(rebuild=False)` | T1 (rebuild opens `[]`), T4 |
| M7 | `cache_mode` ignores `CCTRACK_CACHE` | T10, T3 |
| M8 | In `finally`, call `timing.phase("cache")` even when `unwinding` | `test_cache_equivalence.py::test_timing_printed_when_interrupted`, T14 |
| M9 | Drop the forced `changed of` progress update | T15 |
| M10 | Drop the `_note_cache_use` calls | T16 |
| M11 | Open the store before the `--install-hook`/`--uninstall-hook` handling | T13 |
| M12 | Pass `cache=None` from `main` to `parse_codex_events` | T1 (`local-codex` warm opens 4), T16 |
| M13 | In `_parse_log_file_cached`, drop the `_zstd_available()` condition (`zst_ok = True`) | T8 |
| M14 | In a copy of `tests/`, remove `"CCTRACK_CACHE": "0"` from `run_env`. Run with `PYTHONPATH=src python3 -m pytest $S/t4/tests/test_cache_local.py -k reference` | T17 |

The step 2 and step 3 mutation checks still apply unchanged. With `cache=None`, the seam calls are the same as before.

## 7. Verification

`S=/tmp/claude-501/-home-sandy-dev-cctrack/b90020de-1841-4523-9564-a7f61b1fabce/scratchpad`. Put big files in `/dev/shm`. **Every manual `cctrack` run must set `CCTRACK_CACHE_DIR` or `HOME` to a scratch place**, because a plain `python3 -m cctrack` now creates `~/.cache/cctrack`.

1. **Suite and diff.**
   ```bash
   cd /home/sandy/dev/cctrack
   ls -d ~/.cache/cctrack 2>/dev/null                       # must print nothing (before)
   PYTHONPATH=src python3 -m pytest tests -q                 # 457 + new passed, 1 skipped
   PYTHONPATH=src python3 -m pytest tests/test_cache_local.py -q -v --durations=5
   ls -d ~/.cache/cctrack 2>/dev/null                       # must print nothing (after)
   git diff --stat 0392f8c -- src tests scripts CLAUDE.md README.md docs
   git diff 0392f8c -- tests | grep '^-[^-]'                 # only the run_env default line context; no removed tests
   ```
   Expected changed files: `src/cctrack/__init__.py`, `tests/test_cache_equivalence.py`, `tests/test_cache_local.py`, `scripts/bench_cache.py`, `CLAUDE.md`, `README.md`, `docs/plans/…`. The suite should take about 35–50 s (about 16 s before).

2. **Old vs new over the whole matrix.** `mkdir -p $S/old04 && git -C /home/sandy/dev/cctrack archive 0392f8c src | tar -x -C $S/old04`, then `python3 $S/s4/compare4.py $S/old04/src /home/sandy/dev/cctrack/src`. Expected: `entries: 56 cache file: True differences: {'new cache off': [], 'new cold': [], 'new warm': []}` and exit 0. The step 2 script `python3 $S/compare.py $S/old04/src /home/sandy/dev/cctrack/src` must still report `matrix entries: 57 differences: []`. Its matrix now runs uncached through `run_env`.

3. **Live smoke on this sandbox's real logs.** `bash $S/s4/live_cache.sh /home/sandy/dev/cctrack/src`. Expected:
   - every line `same`;
   - the timing lines show a `cache` phase and `claude parse … ; cache: N reused, 0 read` (or `1 read` if the copied active transcript is racy);
   - no `WARNING`;
   - exit 0.

4. **CLI smoke** (scratch cache):
   ```bash
   export CCTRACK_CACHE_DIR=/dev/shm/smoke4-cache
   PYTHONPATH=src python3 -m cctrack --help | grep -E -- '--no-cache|--rebuild-cache'
   PYTHONPATH=src python3 -m cctrack --no-cache --rebuild-cache; echo "exit $?"     # exit 2
   CCTRACK_TIMING=1 PYTHONPATH=src python3 -m cctrack >/dev/null                    # cold
   CCTRACK_TIMING=1 PYTHONPATH=src python3 -m cctrack 2>&1 >/dev/null | grep -E 'cache|parse'   # warm: "N reused"
   PYTHONPATH=src python3 -m cctrack --sessions --coverage >/dev/null && echo ok
   rm -rf /dev/shm/smoke4-cache; unset CCTRACK_CACHE_DIR
   ```

5. **Bench (the 10× criterion).**
   ```bash
   cd /home/sandy/dev/cctrack
   python3 scripts/bench_cache.py --filler-lines 6 --runs 4 --dir /dev/shm/bench4 --json > /dev/shm/bench4.json
   python3 -c "import json; r=json.load(open('/dev/shm/bench4.json'))['runs']; p=[x['phases']['claude parse']+x['phases']['codex parse'] for x in r]; print([round(v,3) for v in p], 'ratio', round(p[0]/min(p[1:]),1)); assert len({x['stdout_sha256'] for x in r})==1"
   rm -rf /dev/shm/bench4 /dev/shm/bench4.json
   ```
   Pass if the ratio is **≥ 10** and the stdout hashes are all equal. The planner's prototype gave 12–14×. Report the default-options ratio too (`--filler-lines 2`, expected about 7–9×), with the note that its 125 events/MB is not realistic (finding 9). If the realistic ratio is under 10, report the figures and the real-log ratio from verification 3 rather than tuning code.

6. **Mutations.** Run MB1, MB2 and M3–M14 (section 6) and confirm that each named test fails. For MB1, also confirm that `T2[5-fork-child-added]` passes. That confirms finding 4 and is not a failure.

## 8. Deviations

- **MB1 maps to mutation 3 on a fork child, not mutation 5** (finding 4). With the brief's mutation applied, mutation 5 does not fail, because dedup's in-place changes are idempotent when files are added. Deleting a cached fork child does fail, and so does T6, which compares every stored entry with a fresh parse. Plan section 5 now records this for later steps.
- **The bench criterion uses `--filler-lines 6`.** The bench's default gives 125 events/MB, about 2.6× denser than real logs, and only 7–9× (finding 9). The brief says "realistic line sizes", which `--filler-lines 6` (55 events/MB) matches. The bench also had to learn to backdate its files and start each invocation cold.
- **Plan section 4.5 edited**: exclusive flags, flag beats env, the `CCTRACK_CACHE` value set, off never opens the file, and the timing output.
- **The harness's `run_env` now defaults to `CCTRACK_CACHE=0`** (finding 14). The existing harness tests keep testing what they tested before, uncached. Cached behaviour is tested by the new tests through `run_launched`.
- **Added beyond the brief:**
  - the `cache` timing phase and the per-phase `cache: N reused, M read` note (Dan's timing runs will show reuse, which Step 8's gate needs);
  - T6, T8–T17;
  - mutation cases 1b, 3 (×3) and 10s;
  - the open-counting launcher (an audit hook in a subprocess, as step 0's plan suggested).
- No GC tuning (finding 10).

## 9. Out of scope

- Hook files (`statusline-*.jsonl`, kind `hook`): Step 5. A warm run still opens today's hook file.
- Remote protocol v2, manifests, remote rows, and mutation 6 (rsynced copy on the other machine): Step 6. The raw-stream fallback stays uncached for good.
- Full README/`--help` polish and the 0.12.0 version: Step 7. This step adds only the short README section and the option help.
- Byte-offset resume of growing transcripts: Step 8. The active transcript is still re-read in full.
- Fixing F4's local/remote differences: never, in this work.
