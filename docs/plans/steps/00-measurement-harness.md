# Step 00. Measurement and the equivalence harness

Plan for `### Step 0` of `docs/plans/incremental-cache.md`. Written against commit `d23b2d7` (0.11.2, 368 tests passing in about 3 s).

No product decision is needed for this step. The step ends with the pause the brief requires (see Verification, last item).

## 1. Outcome

- `CCTRACK_TIMING=1` makes `cctrack` print one block of `[timing]` lines to stderr when the report run ends, including when it is interrupted. The block gives wall time per phase, file and byte counts for each kind of log, and for each `--remote` host a breakdown measured on the host itself. Without the variable, stdout and stderr are byte-for-byte what they are today, and so is the program sent to remote hosts.
- `scripts/bench_cache.py` builds a reproducible synthetic home (Claude transcripts, Codex rollouts, hook files, and optionally a fake remote host reached through a fake `ssh`) of configurable size, runs `cctrack` against it N times with `CCTRACK_TIMING=1`, and reports wall time, the phase timings and a hash of stdout for each run.
- `tests/test_cache_equivalence.py` holds the section 5 fixture corpus and helpers that run `cctrack` as a subprocess in every mode and variant of section 5 and return stdout. For now it proves that two uncached runs give identical stdout, that the harness notices when a fixture changes, and that timing never touches stdout.

## 2. Findings

Everything below was checked in this sandbox (Python 3.12.15 from `.venv`, Linux aarch64, sqlite 3.53.1). The prototypes were in the scratchpad and are not part of the repo. The snippets needed are copied into this plan.

1. **Subprocess runs are cheap enough for the full matrix.** A prototype corpus (section 5 cases, local plus fake remote) run as `python -m cctrack` subprocesses took 3.8 s for 48 runs (8 modes × 6 variants), about 80 ms per run, remote variants included. 56 runs (adding the raw-stream variant) take about 4.5 s. The whole Step 0 test file should add about 15 s to the suite.
2. **Run `cctrack` as a subprocess, not as `main()` in-process.** Reasons:
   - `CCTRACK_DIR`, `HOOK_CONFIG_PATH`, `HOOK_SCRIPT_PATH` and `CLAUDE_SETTINGS_PATH` are module constants computed from `Path.home()` at import time. An in-process run would have to monkeypatch all of them.
   - Module-level caches (`_remote_bundle_cache`, `_remote_roots_cache`, `_sandy_state_cache`) would have to be reset between runs.
   - `tests/conftest.py` stubs `fetch_remote_bundle` in-process, so remote-parse mode would need the `real_remote_bundle` marker.
   - Later steps need runs that really are separate processes sharing an on-disk cache.

   A subprocess is `main()` behind `python -m cctrack` and avoids all of this.
3. **Output does not depend on the hash seed.** Every mode × variant was run with `PYTHONHASHSEED` 0–5, and stdout was identical for all six seeds. So the harness must **not** pin `PYTHONHASHSEED`: leaving it random lets a later cache step that changes set or dict ordering show up as a failure.
4. **Remote parse equals raw stream for every mode.** On the prototype corpus, `--remote fakehost` with and without `CCTRACK_REMOTE_PARSE=0` gave identical stdout in all 8 modes. (A first comparison seemed to show a JSON difference. It came from normalising `generated_at` to two different placeholders. Use one normaliser everywhere.)
5. **Rollup runs on the first run and changes the fixture home.** On the first local run, `rollup_old_hook_files` turns the >90-day `statusline-*.jsonl` into `monthly-YYYY-MM.json` plus `session-baselines.json` and deletes the daily file. Remote hosts never roll up: `_remote_collect` is read-only. On the prototype, stdout before and after rollup was identical for the default report, `--explore`, `--json` and `--remote`. The harness still does one **settle run** after building the corpus, so every compared run starts from the same files. `settle=False` is available for Step 5, which needs to watch a rollup happen.
6. **Some output depends on today's date** (UTC "month to date", `generated_at`, coverage "today", and the 90-day cutoff). Fixture timestamps are therefore built relative to the UTC date at build time. If the UTC date changes while a test runs, the test retries once (see 5.6). Freezing the clock was rejected. It would mean replacing `cctrack.datetime` with a subclass, and `_tag_json` uses `isinstance(o, datetime)`: a later cache step would then see a mix of real and patched datetimes and fail for reasons unrelated to the cache.
7. **zstd.** Python 3.12 has no `compression.zstd`, and this sandbox has no `zstd` CLI, so a `.jsonl.zst` rollout is **skipped and counted** here. A valid `.zst` can still be written with the stdlib by emitting raw (stored) blocks. I checked this frame writer against Python 3.14.10's `compression.zstd.decompress` and `zstd.open` (Python 3.14 installed by `uv python install 3.14` into the scratchpad) for payloads of 0, 10, 131,072 and 300,000 bytes:
   ```python
   def zstd_raw_frame(data: bytes) -> bytes:
       out = bytearray(b"\x28\xb5\x2f\xfd")      # magic
       out.append(0xA0)                          # FCS_flag=2 (4-byte size), Single_Segment=1
       out += struct.pack("<I", len(data))
       BLK = 128 * 1024
       chunks = [data[i:i + BLK] for i in range(0, len(data), BLK)] or [b""]
       for i, c in enumerate(chunks):
           hdr = (len(c) << 3) | (0 << 1) | (1 if i == len(chunks) - 1 else 0)  # Raw block
           out += hdr.to_bytes(3, "little") + c
       return bytes(out)
   ```
   With 3.14 as the interpreter, the local `.zst` was decoded (coverage `usage_records` 1, `unreadable_files` 0). With 3.12 it was counted unreadable (`unreadable_files` 1, stderr note). Both are deterministic within one environment, which is all the harness compares.
8. **Real line sizes.** I measured this sandbox's own transcript (4,637 lines, 13.1 MB). The average line is 2,816 bytes, the average usage (assistant) line 3,534 bytes, and 1,434 of the 4,637 lines carry usage, so there are about 2.2 other lines per usage line. Real hook rows are about 240 bytes. These numbers set the bench defaults.
9. **F9 reproduced on realistic sizes.** A prototype bench (200 transcripts × 100 usage events, 2,800-byte lines, 2 filler lines per event, 173 MB) runs `cctrack --agent claude` in 1.69 s wall. Under cProfile, `aggregate` (3 calls: local, inside `reconcile_sources`, final) takes 3.86 s cumulative, `_find_rate_row` 3.09 s (120,023 calls), and `parse_events` 0.91 s. Pricing dominates, as F9 says, so the timing block must show `aggregate` and `reconcile` separately from parsing.
10. **Cost of the census.** `os.stat` over 5,000 files took 3.3 ms, and 100,000 `time.perf_counter()` calls took 3.5 ms. The census is still done only when timing is on, and after the clock stops.
11. **Discovery.** If `$SANDY_HOME/sandboxes` is missing, `sandbox_roots()` calls the real `sandy --print-state`. The corpus and the bench must therefore create `.sandy/sandboxes/` in every fake home. The fake `ssh` must come first on `PATH`.

**Dead ends to avoid**
- Do not pin `PYTHONHASHSEED` (finding 3), freeze the clock (finding 6), or run `main()` in-process for the matrix (finding 2).
- Do not put `tests/fixtures/ccusage-parity.json` cases into the corpus. Their request ids (`req_1`, `msg_1`) and fixed date collide across cases, so they would dedup against each other. Step 3 runs them separately.
- Do not pass a relative `PYTHONPATH` to the subprocesses. The test command uses `PYTHONPATH=src`, and the runs use a different cwd. Use the absolute `<repo>/src`.
- Do not use `>=` 90-day hook dates in the bench: rollup would change the bench corpus on its first run. Keep bench hook files within the last 89 days.

## 3. Files

| File | Change |
|---|---|
| `src/cctrack/__init__.py` | Add `import time` at the top. Add `timing_enabled`, `_file_census` and class `_Timing`. In `main`, add phase marks and wrap the report body in `try/finally`. Add a `timing` keyword to `_remote_program`, `_remote_main` and `_remote_collect`. `fetch_remote_bundle` passes the flag and records the reply size. No other behaviour changes. |
| `scripts/bench_cache.py` | New stdlib-only benchmark script (4.4). |
| `tests/test_cache_equivalence.py` | New. Corpus builder, run helpers and the tests in section 6. |
| `CLAUDE.md` | Commands: add `python3 scripts/bench_cache.py` and `CCTRACK_TIMING=1`. Architecture: one line on `_Timing` (phases, remote breakdown, stderr only). Mention `tests/test_cache_equivalence.py` as the harness every later step extends. |
| `README.md` | In "Remote aggregation", after the `CCTRACK_NO_PROGRESS` sentence, add: "`CCTRACK_TIMING=1` prints how long each step took (and, for each `--remote` host, how long it spent reading its logs) to stderr when the run ends." |

## 4. Interfaces

### 4.1 Environment

`CCTRACK_TIMING`: timing is on iff the value, stripped and lower-cased, is not in `("", "0", "no", "false", "off")`. Unset means off.

```python
def timing_enabled() -> bool
```

### 4.2 `_Timing` (module level, next to `Progress`)

```python
class _Timing:
    def __init__(self, enabled: bool): ...
        # t0 = time.perf_counter(); current phase = "startup"
    def phase(self, name: str) -> None
        # Close the current phase (add elapsed to its total) and start `name`.
        # Totals accumulate by name; print order = first time a name started.
        # No-op when disabled.
    def detail(self, name: str, text: str) -> None
        # Set the text shown after a phase's time. No-op when disabled.
    def sub(self, parent: str, name: str, secs: float, text: str = "") -> None
        # Add an indented sub-line under `parent` (remote host breakdown).
    def finish(self, interrupted: bool = False, census: dict | None = None,
               file=None) -> None
        # No-op when disabled. Close the current phase (append " (interrupted)"
        # to its detail if interrupted), take total = now - t0, THEN compute
        # census details, then write the block to `file` (default sys.stderr)
        # and flush.
```

The `census` argument is `{"claude parse": [paths], "codex parse": [paths], "hook read": [paths]}`. Each list becomes that phase's detail through `_file_census`.

```python
def _file_census(paths) -> list[int]
    # [files, bytes, files_changed_24h, bytes_changed_24h]; "changed" = st_mtime
    # >= time.time() - 86400. Files whose stat fails are skipped.
```

Census detail text: `f"{n:,} files, {format_bytes(b)}; {rn:,} changed in last 24 h ({format_bytes(rb)})"`.

**Output block**, all lines on stderr:

```
[timing] # cctrack 0.11.2, Python 3.12.15, linux
[timing] startup                         0.002 s
[timing] discovery                       0.012 s
[timing] claude parse                    0.340 s  3,480 files, 1.2 GB; 12 changed in last 24 h (80.1 MB)
[timing] aggregate                       2.100 s
[timing] hook read                       0.020 s  5 files, 160.0 KB; 1 changed in last 24 h (18.2 KB)
[timing] codex parse                     0.900 s  …
[timing] remote dgx                     41.200 s  1.5 MB received
[timing]   on dgx: discovery             1.100 s
[timing]   on dgx: claude parse         12.000 s  3,480 files, …
[timing]   on dgx: hook read             0.200 s  …
[timing]   on dgx: codex parse          25.000 s  …
[timing]   on dgx: other                 2.900 s  ssh, encode, transfer, local decode
[timing] reconcile                       1.400 s
[timing] report                          0.050 s
[timing] total                          48.000 s
```

Line format, exactly:

```python
f"[timing] {indent}{name:<{30 - len(indent)}} {secs:9.3f} s" + (f"  {detail}" if detail else "")
```

`indent` is `""` for top-level lines and `"  "` for sub-lines. The header line starts with `[timing] # ` and holds `cctrack_version()`, `sys.version.split()[0]` and `sys.platform`. Parse with:

```python
TIMING_RE = re.compile(r"^\[timing\] (?P<indent> *)(?P<name>[^ #].*?) +(?P<secs>\d+\.\d{3}) s(?:  (?P<detail>.*))?$")
```

Top-level phases (empty indent, `total` excluded) sum to `total`.

### 4.3 Phase marks in `main`

Create `timing = _Timing(timing_enabled())` as the **first statement** of `main()`, before `argparse`. The `--install-hook` and `--uninstall-hook` branches return without printing. Declare `files`, `codex_files` and `hook_dirs` as `[]` before the `try`, so `finish` can always read them. Wrap everything from `# ── Local source` to the end of `main` in:

```python
try:
    ...                      # existing body, plus the marks below
except BaseException:
    timing.finish(interrupted=True, census=...)
    raise
else:
    timing.finish(census=...)
```

(or an equivalent `try/finally` that knows whether it is unwinding). The detail-report and `--explore` early `return`s pass through it too.

Marks, in order. Each mark is placed immediately before the code it names:

| Mark | Covers |
|---|---|
| `phase("discovery")` | `dirs = …default_dirs()`, `files = discover_files(…)` |
| `phase("claude parse")` | `parse_events` |
| `phase("aggregate")` | `local_daily, _ = aggregate(local_events)` |
| `phase("discovery")` | `read_hook_config()` and the `local_install` parsing, `hook_dirs = discover_hook_data_dirs()` |
| `phase("hook read")` | the hook loop, `aggregate_hook_data`, the earliest-hook install date, building `all_events` and `sources` |
| `phase("discovery")` | `codex_files = discover_codex_files()`. Split today's inline call `parse_codex_events(discover_codex_files(), …)` into two statements. |
| `phase("codex parse")` | `parse_codex_events(codex_files, …)` and the `.zst` note |
| `phase("aggregate")` | inside `_add_codex_source`, right before its `aggregate(events)`. The early return for empty events stays before the mark. |
| `phase(f"remote {host}")` | per host: `fetch_remote_bundle`, `parse_remote_codex_events` (split out of the `_add_codex_source(...)` call), `parse_remote_events` and the remote hook block. Re-mark `phase(f"remote {host}")` after every `_add_codex_source(...)` call and after `aggregate(remote_events)`. |
| `phase("aggregate")` | `remote_daily, _ = aggregate(remote_events)` |
| `phase("reconcile")` | `reconcile_sources` |
| `phase("report")` | `run_detail_report`, hook prompt, `--explore`, `print_report` |
| `phase("aggregate")` | the final `daily, monthly = aggregate(all_events)`, with `phase("report")` again before `print_report` |

At the end of each host's iteration (including the `continue` for `--agent codex`), set the remote detail and sub-lines from `_remote_bundle_cache.get(host)`:

- `{"ok": True, …}`:
  - Detail is `f"{format_bytes(b['_wire_bytes'])} received"`.
  - If `b.get("timing")` is present, add one `sub(...)` per host phase, named `f"on {host}: {name}"`, in the host's order, with census details from `b["timing"]["census"]`.
  - Then add `f"on {host}: other"` = remote phase total so far minus the sum of host phases, clamped at 0, with text `"ssh, encode, transfer, local decode"`.
  - Because the remote phase can be re-entered, compute "other" in `finish` from the final remote total. Store the host's raw phase seconds on the `_Timing` and derive "other" at print time.
- `{"fallback": True}`: detail `"raw stream"`.
- `{"failed": True}`: detail `"failed"`.

### 4.4 Remote side

```python
def _remote_program(host: str, want_claude: bool, want_codex: bool, timing: bool = False) -> bytes
def _remote_main(host: str, want_claude: bool, want_codex: bool, timing: bool = False) -> None
def _remote_collect(host: str, want_claude: bool, want_codex: bool, timing: bool = False) -> dict
```

- `_remote_program`: when `timing` is False, the generated text is **byte-identical to today's**, ending in `mod._remote_main('h', True, True)\n`. When True, the last line is `mod._remote_main('h', True, True, timing=True)\n`.
- `_remote_collect` with `timing=True` adds:
  ```python
  out["timing"] = {
      "phases": {"discovery": s, "claude parse": s, "hook read": s, "codex parse": s},  # only those that ran, in run order
      "census": {"claude parse": [n, b, rn, rb], "hook read": [...], "codex parse": [...]},
  }
  ```
  - Phase boundaries: discovery is `discover_files(default_dirs())`, `discover_hook_data_dirs()` and `discover_codex_files()`, which today sit inside their blocks; time them as discovery and accumulate. "claude parse" is the `_parse_remote_files` call for Claude, which includes reading because `claude_items` is a generator. "hook read" is the hook block. "codex parse" is the Codex `_parse_remote_files` call.
  - The census runs after the phases, over the same file lists (`files`, the `statusline*.jsonl` files from `rglob` in the hook dirs, the Codex `files`).
  - Use `time.perf_counter`. Nothing else in `out` changes.
- `fetch_remote_bundle(host, want_claude, want_codex)`: its signature is unchanged. It calls `_remote_program(…, timing=timing_enabled())`, and on an `ok` reply sets `res["_wire_bytes"] = len(out)` (the raw stdout bytes, protocol header included). `REMOTE_PROTOCOL` stays `CCTRACK_REMOTE_V1`.

### 4.5 `scripts/bench_cache.py`

Stdlib only. It does not import `cctrack`: it runs `<python> -m cctrack` with `PYTHONPATH=<repo>/src`, where `<repo>` is `Path(__file__).resolve().parents[1]`.

```
python3 scripts/bench_cache.py [options] [-- extra cctrack args]
  --claude-files N    transcripts (default 200)
  --claude-events N   usage events per transcript (default 100)
  --line-bytes N      target bytes per JSONL line (default 2800, finding 8)
  --filler-lines N    non-usage lines per usage event (default 2)
  --codex-files N     rollouts (default 50)
  --codex-events N    usage events per rollout (default 100)
  --hook-files N      daily statusline files, newest first, capped at 89 (default 30)
  --hook-rows N       rows per hook file (default 300)
  --days N            spread event timestamps over the last N days (default 60)
  --remote            also build a fake host "benchhost" (same sizes, seed+1) and
                      pass --remote benchhost
  --runs N            runs to time (default 2)
  --dir DIR           build in DIR and keep it; reuse it if DIR/bench-corpus.json
                      matches the options and was built on the same UTC day
  --python PATH       interpreter for cctrack and the fake host (default sys.executable)
  --seed N            default 1
  --json              print results as one JSON document instead of text
```

**Corpus.** Deterministic from `random.Random(seed)`. Under `<dir>/home`: `.sandy/sandboxes/` (empty), `.claude/projects/-bench-<k>/` transcripts, `.codex/sessions/YYYY/MM/DD/rollout-…-<id>.jsonl`, and `.claude/cctrack/statusline-YYYY-MM-DD.jsonl`. With `--remote`, the same layout goes under `<dir>/remote-home`, and `<dir>/bin/ssh` is a fake ssh (the same script as `tests/test_remote_parse.py::_fake_ssh`, exec'ing `--python`).

- **Claude transcripts.** Each usage line is an `assistant` event with unique `requestId` and `message.id`, model `claude-opus-5-5`, realistic `usage` (input, output, cache read, 1h cache write), and `message.content[0].text` padded so the line is about `--line-bytes`. The filler lines are `user` events of the same size.
- **Codex rollouts.** Each starts with `session_meta` and `turn_context` (model `gpt-5.4`). Each event is one `token_usage_record` (unique `response_id`) plus its matching `event_msg`/`token_count` (`last_token_usage` = the usage, `total_token_usage` = the running sum), plus filler `response_item` lines of `--line-bytes`.
- **Hook rows.** Each row is `{"_ts", "session_id", "cost_usd" (rising from 0.0 per session), "model"}`.
- **Safety.** If `--dir` exists, is non-empty and has no `bench-corpus.json`, exit 2 with a message. Never delete a directory the script did not create. Without `--dir`, use `tempfile.mkdtemp()` and remove it at exit.

**Run environment**, built from scratch and not inherited: `HOME`, `SANDY_HOME=<home>/.sandy`, `PATH=<dir>/bin:<os.environ PATH>`, `PYTHONPATH`, `CCTRACK_TIMING=1`, `CCTRACK_NO_PROGRESS=1`, `PYTHONIOENCODING=utf-8`, and `XDG_CACHE_HOME=<dir>/xdg-cache`, which keeps later cache steps away from the real cache. Each run uses `stdin=DEVNULL` and `cwd=<dir>`, captures stdout and stderr, and a non-zero exit code is an error.

**Text output:**
- First, one line describing the corpus: files, MB and events per kind.
- Per run: `run 1: wall 1.69 s  stdout sha256 3f2a9c1d…  (rc 0)`, followed by that run's `[timing]` lines.
- With `--json`: `{"corpus": {...}, "runs": [{"wall": s, "stdout_sha256": h, "phases": {name: secs}, "sub": {parent: {name: secs}}, "details": {name: text}}]}`. Phases come from `TIMING_RE`. Sub-lines are grouped under the top-level line before them.

## 5. Behaviour and edge cases

1. **Stdout is untouched.** All timing output goes to stderr. Nothing is printed when timing is off, not even the header.
2. **Interrupts.** On `KeyboardInterrupt`, a `subprocess.TimeoutExpired` that escapes, or any other exception in the report body, the block is still printed. The phase that was running gets `(interrupted)` appended to its detail, and then the exception propagates unchanged. This covers the case that motivated the step (a run that timed out at 60 s).
3. **`parser.error` and the install/uninstall paths** print nothing: they leave before the `try`.
4. **Phases that do not run.** Phases that never started are not printed. With `--agent codex`, `parse_events([])` still runs and prints a near-zero `claude parse`; leave that as it is.
5. **Repeated names accumulate.** `--remote dgx dgx` gives one `remote dgx` line, because the second fetch hits `_remote_bundle_cache`. The sub-lines for a host are added once (guard on host name).
6. **Midnight guard (tests).** The corpus records `anchor = <UTC date at build>`. A comparison test checks that the UTC date after its last run still equals `anchor`. If it does not, the test rebuilds the corpus in a fresh directory and repeats once. A second crossing would mean a run lasting over a day; fail in that case.
7. **Normalisation.** The only normalisation is in `--json` stdout: `re.sub(r'"generated_at": "[^"]*"', '"generated_at": "<normalised>"', out)`. Nothing else is masked. Absolute tmp paths are identical within one corpus.
8. **Fixture timestamps never lie in the future.** "Today" events use `T00:00:01Z`. All other days use fixed hours of past days.
9. **Remote byte count.** `_wire_bytes` is internal. Nothing else reads it, and it is not part of any report.

### 5.1 The corpus (`build_corpus`)

`day(n)` is the UTC date n days before the build. The line builders come from `tests/test_detail.py` (`claude_line`, `compact_line`, `codex_line`, `codex_usage`). `token_count(ts, last, total)` builds an `event_msg` with `info.last_token_usage` and `info.total_token_usage`.

**Local home** `<root>/local-home`:

- `.claude/projects/-repo/s1.jsonl`:
  - `r1` on day 1 01:00, opus-5-5, with `inp=10_000, out=5_000, cr=100_000, cw1h=500`;
  - the line `{not json`;
  - `r2` on day 1 02:00;
  - `compact_line` on day 1 02:30;
  - a line whose `usage.iterations` holds one `{"type": "advisor_message", "model": "claude-opus-4-6", "input_tokens": 1000, "output_tokens": 100}` (request `r6`, day 1 02:40).
- `.claude/projects/-repo/s1/subagents/agent-a1.jsonl`: `r3`, `sidechain=True`, **no `agentId`**.
- `.claude/projects/-repo/s1-resumed.jsonl`: `r2` again (the resumed copy), plus `r5` today at 00:00:01.
- `.claude/projects/-repo/lh.jsonl`: one `claude-sonnet-5` request on day 2, and one request with model `claude-unknown-9` (priced by fallback, flagged `?`).
- `.sandy/sandboxes/box-1a2b3c4d/WORKSPACE.json` (`{}`), and `…/claude/projects/-repo/s1.jsonl`: a byte copy of the host `s1.jsonl` (the sandbox copy).
- `.codex/sessions/<day1 path>/rollout-<day1>T05-00-00-T1.jsonl`: `session_meta` T1, `turn_context` `gpt-6.1-sol`, and one `token_usage_record` (`resp1`, `codex_usage(1000, cached=800, out=20)`).
- `.codex/sessions/<day2 path>/rollout-<day2>T06-00-00-P.jsonl`: a legacy thread. `session_meta` `{"id": "P"}`, `turn_context` `gpt-5.4`, and `token_count` (last = total = 1000 in / 100 out).
- `.codex/sessions/<day1 path>/rollout-<day1>T07-00-00-C.jsonl`: a fork child. `session_meta` `{"id": "C", "forked_from_id": "P"}`, `turn_context` `gpt-5.4`, then the replayed `token_count` (1000/100, total 1000/100) and a new one (last 500/50, total 1500/150).
- `.codex/archived_sessions/rollout-<day3>T08-00-00-Z.jsonl.zst`: `zstd_raw_frame` of a three-line legacy thread Z.
- `.claude/cctrack/statusline-<day1>.jsonl`: session `s1` rows at 00:00, 01:00 and 02:00 with `cost_usd` 0.0, 0.10, 0.25.
- `.claude/cctrack/statusline-<day120>.jsonl`: session `olds` with 0.0, 2.0, 3.0.
- `.claude/cctrack/config.json`: `{"installed_at": "<day150>T00:00:00+00:00"}`.

**Remote home** `<root>/remote-home`:
- The same layout, built by the same function with tag `rh` instead of `lh`. That makes `s1`, `T1`, `P`, `C` and the day-1 hook file rsynced copies of the local ones.
- Plus its own `rh.jsonl` and its own >90-day hook file. The remote file is never rolled up.

**`<root>/bin`**: `ssh` from `test_remote_parse._fake_ssh(root, {"HOME": remote_home, "SANDY_HOME": remote_home/.sandy}, python)`. Import it, as that file already imports from `test_detail`.

After building, unless `settle=False`, run `cctrack` once with no options, discard the output and assert exit code 0.

### 5.2 Helpers in `tests/test_cache_equivalence.py`

```python
@dataclass
class Corpus:
    root: Path; home: Path; remote_home: Path; bin_dir: Path
    anchor: str            # UTC date at build
    explore_date: str      # day(1)
    python: str

def build_corpus(root: Path, *, settle: bool = True, python: str = sys.executable) -> Corpus
def run_env(corpus: Corpus, extra: dict | None = None) -> dict
    # from scratch: HOME, SANDY_HOME, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
    # PYTHONPATH=<abs repo>/src, CCTRACK_NO_PROGRESS=1, PYTHONIOENCODING=utf-8,
    # XDG_CACHE_HOME=<root>/xdg-cache, then `extra`. No PYTHONHASHSEED.
def run_cctrack(corpus: Corpus, argv: list[str], env: dict | None = None) -> subprocess.CompletedProcess
    # [python, "-m", "cctrack", *argv], cwd=corpus.root, stdin=DEVNULL, text, utf-8,
    # timeout=120; asserts returncode == 0 with stderr in the message.
def normalise(stdout: str) -> str
def modes(corpus) -> dict[str, list[str]]
    # report: []; explore: ["--explore", explore_date];
    # detail: ["--sessions", "--bucket", "hour", "--coverage"]; json: ["--json"];
    # csv-sessions / csv-buckets / csv-coverage / csv-rates: ["--csv", X]
VARIANTS: dict[str, tuple[list[str], dict]] = {
    "local": ([], {}), "local-claude": (["--agent", "claude"], {}),
    "local-codex": (["--agent", "codex"], {}),
    "remote": (["--remote", "fakehost"], {}),
    "remote-claude": (["--agent", "claude", "--remote", "fakehost"], {}),
    "remote-codex": (["--agent", "codex", "--remote", "fakehost"], {}),
    "remote-raw": (["--remote", "fakehost"], {"CCTRACK_REMOTE_PARSE": "0"}),
}
def run_matrix(corpus, *, variants=None, modes_=None, env=None) -> dict[tuple[str, str], str]
    # (variant, mode) -> normalise(stdout); serial, in VARIANTS × modes order
def diff_keys(a: dict, b: dict) -> list[tuple[str, str]]
```

Later steps add cache options through `env` (or through argv and a new variant). Keep the helpers general.

## 6. Tests (`tests/test_cache_equivalence.py`)

Use a module-scoped fixture `baseline` (via `tmp_path_factory`) that builds one corpus and runs the full matrix once. It returns `(corpus, outputs)`. Tests that change files build their own corpus.

| Criterion | Test |
|---|---|
| Corpus holds every section 5 case (my addition: stops later edits from dropping cases) | `test_corpus_covers_section5_cases`. Takes `--json --remote fakehost` stdout and asserts: a Claude `s1` row whose `source_locations` span hosts `local` and `fakehost` with ≥3 locations; a row with `is_subagent` and `subagent_id == "a1"`; a row with `compactions >= 1`; some coverage source with `unparseable_lines >= 1`; Codex row `C` with `parent_session_id == "P"`; a model priced by fallback; the local `archived_sessions` source has either `usage_records >= 1` (decoder available) or `unreadable_files == 1`. On disk after the settle run: local `.claude/cctrack` has a `monthly-*.json` and no `statusline-<day120>.jsonl`, while the remote home still has its day-120 file. |
| Harness is deterministic: two uncached runs match | `test_two_uncached_runs_match`. Runs the matrix again on the `baseline` corpus and asserts `diff_keys(baseline, again) == []` and that all 56 outputs are non-empty. Applies the midnight guard (5.6). |
| Verify: the harness fails if a fixture file changes between its two runs | `test_harness_detects_an_altered_fixture[local-claude, remote-codex]`. On its own corpus: matrix over variant `remote` × all modes, then change one file, then the matrix again, then assert that `("remote", "report")` and `("remote", "json")` are in `diff_keys`. Changes: append a new `claude_line` (`r99`, day 1, `inp=1_000_000`) to local `s1-resumed.jsonl`; append a `token_usage_record` (`resp99`, `codex_usage(1_000_000, out=10)`) to the remote T1 rollout. |
| Verify: timing lines only with the env var | `test_timing_off_by_default`. Runs `--remote fakehost` with no `CCTRACK_TIMING`, then with `CCTRACK_TIMING=0`: no line of stderr starts with `[timing]`. |
| Verify: stdout unchanged with timing | `test_timing_does_not_change_stdout`. Matrix over variants `remote`, `remote-raw` and `local-codex`, all modes, with `env={"CCTRACK_TIMING": "1"}`, compared with the same keys of `baseline`: no differences. Also asserts that `[timing] total` is in every stderr. To get stderr, use a variant of `run_matrix` that returns `(stdout, stderr)`, or call `run_cctrack` directly. |
| Phases cover the run | `test_timing_lines_cover_the_run`. Runs `CCTRACK_TIMING=1 --remote fakehost`. Every `[timing]` line except the header matches `TIMING_RE`. Top-level names include `startup, discovery, claude parse, aggregate, hook read, codex parse, remote fakehost, reconcile, report, total`. The sum of the top-level lines other than total is within 0.02 s of total. Sub-lines `on fakehost: discovery / claude parse / hook read / codex parse / other` are present. The `remote fakehost` detail ends in `received`. The `claude parse` detail matches `^\d[\d,]* files, `. `--explore` and `--json` runs also end with `report` and `total`. |
| Remote states | `test_timing_remote_raw_and_failed`. With `CCTRACK_REMOTE_PARSE=0`, the remote detail is `raw stream` and there are no `on fakehost:` lines. With a `bin` whose `ssh` prints an error and exits 255, it is `failed`. |
| Interrupt still prints | `test_timing_printed_when_interrupted` (in-process). `monkeypatch`: `default_dirs → []`, `discover_hook_data_dirs → []`, `read_hook_config → None`, `discover_codex_files → []`, `parse_codex_events` raises `KeyboardInterrupt`; `CCTRACK_TIMING=1`; `sys.argv = ["cctrack"]`. Under `pytest.raises(KeyboardInterrupt)`, stderr contains `[timing] total` and a `codex parse` line with `(interrupted)`. |
| Remote program unchanged when off | `test_remote_program_unchanged_without_timing`. `_remote_program("h", True, False)` ends with `b"mod._remote_main('h', True, False)\n"`. With `timing=True` it ends with `b"mod._remote_main('h', True, False, timing=True)\n"`. |
| Bench works | `test_bench_script_smoke`. Runs `scripts/bench_cache.py --claude-files 2 --claude-events 3 --codex-files 1 --codex-events 2 --hook-files 1 --hook-rows 3 --remote --runs 2 --json` (subprocess, `sys.executable`). Asserts exit code 0, two runs with equal `stdout_sha256`, and phases including `total`, `claude parse`, `codex parse` and `remote benchhost`. |
| zstd helper is valid | `test_zstd_raw_frame_decodes`. Write `zstd_raw_frame(b"a\nb\n")` to a tmp `.jsonl.zst`. If `cctrack._read_zstd` returns `None` (no decoder), `pytest.skip`. Otherwise it must return `"a\nb\n"`. |

The whole existing suite must still pass unchanged (rule 1).

### Mutation checks (run each on a copy of `src/` or `tests/`, one at a time)

| # | Change | Test expected to fail |
|---|---|---|
| M1 | In `_Timing.finish`, default `file` to `sys.stdout` | `test_timing_does_not_change_stdout` |
| M2 | `timing_enabled()` returns `True` always | `test_timing_off_by_default` |
| M3 | Remove the exception path, so `finish` runs only on normal completion | `test_timing_printed_when_interrupted` |
| M4 | Delete the `timing.phase("reconcile")` mark | `test_timing_lines_cover_the_run` |
| M5 | `_remote_program` always emits `timing=True` | `test_remote_program_unchanged_without_timing` |
| M6 (brief's Verify) | In `test_two_uncached_runs_match`, append a line to local `s1-resumed.jsonl` between the two matrix runs | `test_two_uncached_runs_match` |
| M7 | `normalise` returns `""` | `test_harness_detects_an_altered_fixture` (both params) |
| M8 | Do not add the `on {host}:` sub-lines (skip the `sub(...)` calls) | `test_timing_lines_cover_the_run` |

## 7. Verification

```bash
cd /home/sandy/dev/cctrack
PYTHONPATH=src python3 -m pytest tests -q                       # all pass, 368 + new
PYTHONPATH=src python3 -m pytest tests/test_cache_equivalence.py -q -v
git diff --stat -- src tests scripts README.md CLAUDE.md        # only the files in section 3
```

Smoke on the bench corpus (both runs must show the same `stdout sha256`, and the `[timing]` lines must include `remote benchhost` with its `on benchhost:` sub-lines):

```bash
python3 scripts/bench_cache.py --remote --runs 2
python3 scripts/bench_cache.py --claude-files 20 --runs 1 --json | python3 -m json.tool | head -40
```

Smoke on this sandbox's real logs. Do not diff stdout between runs: the live transcript of the running session grows between runs.

```bash
CCTRACK_TIMING=1 PYTHONPATH=src python3 -m cctrack >/dev/null      # [timing] block on stderr
PYTHONPATH=src python3 -m cctrack 2>&1 >/dev/null | grep -c '^\[timing\]'   # prints 0
CCTRACK_TIMING=1 PYTHONPATH=src python3 -m cctrack --json 2>/dev/null | python3 -m json.tool >/dev/null
```

Optional zstd decode path: run `test_zstd_raw_frame_decodes` and `test_corpus_covers_section5_cases` under a Python 3.14 interpreter if one is available, for example `uv python install 3.14` into a temp `UV_PYTHON_INSTALL_DIR` plus `uv run --python 3.14 --with pytest`. Here, on 3.12, the zstd test skips.

Run each mutation in section 6 and confirm that the named test fails.

**Then pause for the user**, as the brief says. Ask Dan to run this on the Mac and paste the `[timing]` lines; they set the priority of Steps 5 and 8:

```bash
CCTRACK_TIMING=1 uvx --from . cctrack --remote dgx
```

Step 5 may be skipped if `hook read` (local) plus `on dgx: hook read` is under 5% of `total`.

## 8. Deviations

- **Extra phases: `startup` and `aggregate`.** The brief lists discovery, Claude parse, Codex parse, hook read, each remote host, reconcile and report. I added `aggregate` as its own phase because F9 and finding 9 show that pricing, not parsing, dominates a run. Without it, Step 1's gain and the cache's gain would be hidden inside the parse phases. `startup` (argparse) makes the phases sum exactly to `total`.
- **Breakdown measured on the host.** The brief asks for one figure per remote host. I added the host's own phase times, file and byte counts, and the reply size, carried as an optional `timing` key in the v1 reply. It is requested only when `CCTRACK_TIMING` is on, so with timing off the program sent is byte-identical. The reason: Dan's `--remote dgx` paste is what sets Step 5 and Step 8 priorities, and one number cannot tell remote parsing from transfer.
- **File, byte and changed-in-24-h counts per kind** are an addition. They give Step 8 a first estimate of how much of each warm run would still be re-reading active files.
- **README and CLAUDE.md get one sentence each** for `CCTRACK_TIMING` and the bench script (rule 7), although the brief says nothing user-visible changes. Report output is unchanged.
- No change to sections 3–5 of `docs/plans/incremental-cache.md`.

## 9. Out of scope

- Any cache code, `--no-cache`, `--rebuild-cache`, `CCTRACK_CACHE*`. The harness and bench already isolate `XDG_CACHE_HOME`, and Step 3 adds `CCTRACK_CACHE_DIR` to `conftest.py`.
- Cold, warm and rebuilt runs, and the section 5 mutation list (Steps 4 and 6 add them to `run_matrix` callers).
- Counting file opens (Step 4). Hint: run the subprocess as `python -c "import sys; sys.addaudithook(...); import runpy; runpy.run_module('cctrack', run_name='__main__')"` with an `open` audit hook that writes paths to a file.
- Any speed-up: memoising rates and dropping redundant `aggregate` passes is Step 1.
- Fixing the F4 difference (remote does not fill a subagent's `agent_id` from its filename). The corpus contains that case on purpose.
