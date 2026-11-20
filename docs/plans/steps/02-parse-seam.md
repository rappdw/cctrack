# Step 02. One per-file parse seam (refactor only)

Plan for `### Step 2` of `docs/plans/incremental-cache.md`. Written against commit `a5eed3e` (step 01 done; 394 passed, 1 skipped, about 15 s, Python 3.12.15).

No product decision is needed. No section 7 decision is needed. Sections 3–5 of the plan are unchanged.

## 1. Outcome

- Three new private functions in `src/cctrack/__init__.py` form the one seam that turns one log file into `{"events", "stats"}` or `None` (unreadable):
  - `_read_log_lines(kind, path)` reads a file,
  - `_parse_log_lines(kind, lines, label, ...)` parses lines that are already in memory,
  - `_parse_log_file(kind, path, ...)` runs the first and then the second.
- `parse_events` (local Claude) and `parse_codex_events` (local Codex) call `_parse_log_file`. `_parse_remote_files`, which serves both the raw stream and `_remote_collect`, calls `_parse_log_lines`. `_remote_collect` reads its files through `_read_log_lines`.
- F4's local/remote differences become explicit parameters or stay in the callers. One is the `agent-<id>.jsonl` subagent fallback, which only local Claude applies. The others are unreadable-file stats and the `.zst` skip count.
- Every event list, stats dict, report and remote reply is identical to what it was before. A new test file pins the seam's contract and the F4 differences. Two of those differences have no test today.

## 2. Findings

All checks ran in this sandbox (Python 3.12.15, Linux aarch64, no `zstd` CLI and no `compression.zstd`, so `.zst` files are unreadable here). The prototype scripts are in the scratchpad and are copied into section 7 so the verifier can run them.

1. **Baseline.** `PYTHONPATH=src python3 -m pytest tests -q` gives 394 passed, 1 skipped (`test_zstd_raw_frame_decodes`, which needs zstd).

2. **The four read sites and how they differ today** (read from the code):

   | Site | Read | Parse call | Tag label | Agent-id fallback | Unreadable | Stats key |
   |---|---|---|---|---|---|---|
   | `parse_events` | `open(path, "r", errors="replace").readlines()` | `parse_lines(lines, stats=st)` | `path` | yes | `file_stats[path] = {**new_parse_stats(), "unreadable": True}`; no list appended | `path`, only if `file_stats` is not None |
   | `parse_codex_events` | `.zst`: `_read_zstd(path).splitlines()`; else `readlines()` | `parse_codex_lines(lines, source=source, stats=st)` | `path` | no | as above; **only** a `.zst` failure adds to `skipped` | `path` |
   | `_remote_collect` → `_parse_remote_files` | Claude: `_read_text_lines`; Codex: as local | `parser(lines, source=host, stats=st)` | `f"{host}:{path}"` | no | silently skipped, no stats; Codex `.zst` failure counted in `skipped[0]` | `label`, only if `path` |
   | raw stream `_remote_parse` → `_parse_remote_files` | `_remote_chunks(raw.splitlines())` | same | `f"{host}:{path}"`, or `f"(remote {host})"` when the stream has no path | no | n/a | `label`, only if `path` |

   `_read_text_lines` is exactly `open(path, "r", errors="replace").readlines()` with `except OSError: return None`. The local Claude reader catches `(OSError, IOError)`, and `IOError is OSError` is `True` on this Python. So one reader serves all four sites.

3. **Line splitting must stay per site. Dead end: do not normalise it.** Plain files are split by `readlines()`, `.zst` text and the raw stream by `str.splitlines()`. These differ: `splitlines()` also breaks on U+2028, U+0085, `\x0c` and similar characters, and Claude Code's `JSON.stringify` writes U+2028 raw inside strings. Experiment: one Claude usage line whose `cwd` contains a raw U+2028 (`json.dumps(..., ensure_ascii=False)`):
   - through `parse_events`: 1 event, `lines=1`, `bad_lines=0`;
   - through `parse_lines(text.splitlines())`: 0 events, `lines=2`, `bad_lines=2`.

   The reader must use `readlines()` for plain files and `_read_zstd(...).splitlines()` for `.zst` files, exactly as today. The raw-stream path keeps its own `splitlines()` inside `_remote_chunks`. That is a pre-existing local/raw difference and is not touched.

4. **Parsers do no I/O.** A grep of `parse_lines` and `parse_codex_lines` finds no `open`, `os.`, `Path`, `.resolve`, `stat` or `subprocess`. So moving `parse_lines` out of `parse_events`'s `try` block (today the parse runs inside the `with open(...)`) cannot turn a parse error into "unreadable" or the reverse.

5. **Stats do not affect events (F4, now measured).** I built the step-0 corpus (`build_corpus`, both homes, 16 readable files) and parsed every file with `stats=None` and with `stats=new_parse_stats()`. The events were identical in canonical tagged JSON for every file. The cost of collecting stats: 100,000 tiny Claude lines took 0.377/0.386 s without stats and 0.394/0.410 s with them, about 5 %. Step 4 can collect stats whenever the cache is on (plan 4.2). For this step, `want_stats` stays off unless the caller asks, as today.

6. **Tests pin the parser-callable API of `_remote_parse`.** `tests/test_detail.py` calls `cctrack._remote_parse("dgx", raw, sep, cctrack.parse_lines, stats)` twice. "Suite passes untouched" means `_remote_parse(host, raw, sep, parser, file_stats)` and `_parse_remote_files(host, items, parser, file_stats)` must keep taking a parser function as their fourth argument. Inside, the function is mapped to a kind string (section 4).

7. **Tests patch the zstd decoder through modules, not cctrack globals.** `test_zst_skipped_and_counted_without_decompressor` and `test_zst_decompressed_via_cli_when_available` patch `sys.modules["compression"]`, `shutil.which` and `subprocess.run`. These patches work as long as the new reader calls `_read_zstd` (unchanged). No test monkeypatches `parse_lines`, `parse_codex_lines` or `_read_text_lines` on the module. The seam must still look the parsers up by global name **at call time** (no table built at import time), so that a later test can patch them.

8. **The brief's mutation is caught today.** I removed the `agent-<id>` fallback block from `parse_events` in a copy of `src/`. `tests/test_detail.py::test_claude_subagent_gets_own_row_linked_to_parent` and `tests/test_cache_equivalence.py::test_corpus_covers_section5_cases` failed, and 14 of the 56 report outputs in the old-vs-new comparison (section 7) changed.

9. **Gap: nothing pins the remote side of F4.** I added the same fallback to `_parse_remote_files` in a copy of `src/`. The full suite still passed (394), and every one of the 56 stdout outputs of the section 5 matrix was unchanged. Only the in-process event dump differed. So "remote does not fill `agent_id` from the filename" has no test. This step adds one (section 6, T5), because the seam makes that difference a single boolean argument.

10. **Old-vs-new comparison works and is deterministic.** `compare.py` (section 7) builds the step-0 corpus once. It runs the whole section 5 matrix (7 variants × 8 modes = 56 stdout outputs) with an old and a new source tree, plus an in-process dump of `parse_events` / `parse_codex_events` (with and without stats) and `_remote_collect` output. HEAD against HEAD: 57 entries, 0 differences, about 7 s. With the mutation from finding 8 applied: 15 differences. So it detects a seam change. `live_compare.sh` (section 7) does the same against a snapshot of this sandbox's real logs (22 MB under `~/.claude/projects`, no `~/.codex`, no sandboxes). HEAD against HEAD gave all 9 modes the same.

11. **Use `git archive`, not `git worktree`, to get the old source.** `git -C /home/sandy/dev/cctrack archive HEAD src | tar -x -C <dir>` gives a pristine old tree without writing to `.git` (the verifier is read-only). A worktree also works but leaves metadata behind.

## 3. Files

| File | Change |
|---|---|
| `src/cctrack/__init__.py` | Add `_read_log_lines`, `_parse_log_lines`, `_parse_log_file`, `_log_kind`. Rewrite the per-file bodies of `parse_events`, `parse_codex_events`, `_parse_remote_files` and the two item generators in `_remote_collect` to use them. Nothing else changes. |
| `tests/test_parse_seam.py` (new) | Tests T1–T9 in section 6. |
| `CLAUDE.md` | Architecture item 4 (JSONL parsing): one sentence naming the seam. Suggested: "`_parse_log_file`/`_parse_log_lines`/`_read_log_lines` are the one per-file seam (one file → `{events, stats}` or `None` = unreadable) used by `parse_events`, `parse_codex_events` and `_parse_remote_files`; the local-only `agent-<id>.jsonl` fallback is its `agent_id_from_name` flag." No README change: nothing user-visible changes. |

No existing test file is edited.

## 4. Interfaces

Place the four functions just above `parse_events`. `_read_zstd` is defined later in the module, which is fine because it is resolved at call time.

```python
LOG_KINDS = ("claude", "codex")   # optional; or inline the check

def _read_log_lines(kind: str, path: str) -> list[str] | None:
    """One log file's lines exactly as the parsers have always received them, or
    None if unreadable. Codex `.jsonl.zst`: `_read_zstd(path).splitlines()` (None
    if it can't be decoded). Everything else: `_read_text_lines(path)`
    (`readlines()`, errors="replace", None on OSError)."""
```
- `kind` is `"claude"` or `"codex"`. Anything else raises `ValueError`.
- `.zst` handling applies only when `kind == "codex"` and `path.endswith(".zst")`. A Claude path is always read as text. `discover_files` only yields `*.jsonl`, so this matches today's behaviour.

```python
def _parse_log_lines(kind: str, lines: list[str], label: str, *, source: str = "local",
                     want_stats: bool = False, agent_id_from_name: bool = False) -> dict:
    """Parse one file's lines → {"events": [...], "stats": dict | None}.
    Events are the parser's in-file-deduped output, each tagged with
    `_tag_event_file(e, label)`; no cross-file step has seen them."""
```
- Parser choice: `parse_lines` for `"claude"`, `parse_codex_lines` for `"codex"`. Look it up at call time, for example `parser = {"claude": parse_lines, "codex": parse_codex_lines}.get(kind)` inside the function body, with `ValueError` when it is missing.
- Call `parser(lines, source=source, stats=st)`, where `st = new_parse_stats() if want_stats else None`.
- For each event, in order: `_tag_event_file(e, label)`. Then, only if `agent_id_from_name`, the existing fallback moved here verbatim except that it uses `label`: `if e.get("is_sidechain") and not e.get("agent_id"): name = Path(label).name; if name.startswith("agent-") and name.endswith(".jsonl"): e["agent_id"] = name[len("agent-"):-len(".jsonl")]`.
- Returns a new dict, `{"events": events, "stats": st}`. `stats` is `None` when `want_stats` is false.

```python
def _parse_log_file(kind: str, path: str, *, source: str = "local", label: str | None = None,
                    want_stats: bool = False, agent_id_from_name: bool = False) -> dict | None:
    """The per-file seam: read `path` and parse it. None = unreadable (OSError, or
    an undecodable .zst). `label` (default `path`) is what events are tagged with."""
```
- Body: `lines = _read_log_lines(kind, path)`. If `lines is None`, return `None`. Otherwise return `_parse_log_lines(kind, lines, path if label is None else label, source=..., want_stats=..., agent_id_from_name=...)`.
- Call `_read_log_lines` and `_parse_log_lines` by their global names so that T6 can count the calls.

```python
def _log_kind(parser) -> str:
    """Map the parser function that `_remote_parse`/`_parse_remote_files` take to its kind."""
    # parser is parse_lines -> "claude"; parser is parse_codex_lines -> "codex"; else ValueError
```
- Compare identity against the module globals at call time (`parser is parse_lines`).

### Callers after the change

**`parse_events(files, file_stats=None) -> list[dict]`.** The signature, progress labels and dedup stay as they are. The per-file body becomes:
```python
r = _parse_log_file("claude", path, want_stats=file_stats is not None, agent_id_from_name=True)
if r is None:
    if file_stats is not None:
        file_stats[path] = {**new_parse_stats(), "unreadable": True}
    continue
if file_stats is not None:
    file_stats[path] = r["stats"]
event_lists.append(r["events"])
```

**`parse_codex_events(files, source="local", file_stats=None) -> tuple[list[dict], int]`.** Same shape, with `_parse_log_file("codex", path, source=source, want_stats=file_stats is not None)`. When the result is `None`: `if path.endswith(".zst"): skipped += 1`, then the unreadable stats when `file_stats` is given, then `continue`. A plain-file `OSError` is **not** counted in `skipped`.

**`_parse_remote_files(host, items, parser, file_stats) -> list[dict]`.** The signature is unchanged (finding 6). `kind = _log_kind(parser)` once, then for each `(path, lines)`:
```python
label = f"{host}:{path}" if path else f"(remote {host})"
r = _parse_log_lines(kind, lines, label, source=host, want_stats=file_stats is not None)
if file_stats is not None and path:
    file_stats[label] = r["stats"]
lists.append(r["events"])
```
`agent_id_from_name` stays `False` (F4). The function returns `dedup_events(lists)` as before. `_remote_parse` is unchanged.

**`_remote_collect`.** Only the readers inside `claude_items` and `codex_items` change:
- `claude_items`: `lines = _read_log_lines("claude", path)`, yielding when it is not `None`.
- `codex_items`: `lines = _read_log_lines("codex", path)`. When it is `None`: `if path.endswith(".zst"): skipped[0] += 1`, then `continue`. Otherwise `yield path, lines`.
- The calls to `_parse_remote_files(host, …, parse_lines | parse_codex_lines, stats)` stay as they are. The hook loop keeps using `_read_text_lines`.

No new environment variables or CLI options. `REMOTE_PROTOCOL` stays v1. The reply shape is unchanged.

## 5. Behaviour and edge cases

- **Order within a file.** Tag first, then apply the agent-id fallback, per event, as today. Neither step reads the other's field, but keep the order anyway.
- **List order across files.** Callers append to `event_lists`/`lists` in the order they are given, and skip unreadables, as today (F5). Do not sort and do not collect "unreadable" placeholders into the list passed to `dedup_events`.
- **The seam returns the parser's list object itself**, not a copy. Cross-file dedup mutates events afterwards (F3). That is today's behaviour, and serializing before that point is Step 4's job.
- **`want_stats=False` must pass `stats=None` to the parser**, not a throwaway dict. Finding 5 shows the events are the same either way, but this keeps today's speed and call shape.
- **Remote stats are always collected** (`_remote_collect` passes a dict), and stats are keyed only when the stream carried a path. A path-less raw chunk is still parsed and tagged `(remote host)`.
- **`.zst` skip counting.** "unreadable and the path ends with `.zst`" is exactly today's `_read_zstd(...) is None` branch, because `_read_zstd` also returns `None` on `OSError`. Locally a skipped `.zst` file also gets `{"unreadable": True}` stats when stats were requested. Remotely it does not, as today.
- **`label` vs `path` for Codex session ids.** `_tag_event_file` derives a missing Codex `session_id` from `Path(label).name`. Remote labels are `host:/abs/path`, so the basename is unchanged. Keep passing the label, not the bare path, on the remote side.
- **Do not "fix" F4.** Remote Claude keeps no filename fallback, and remote unreadables keep no stats.
- **Do not unify line splitting** (finding 3).

## 6. Tests

New file `tests/test_parse_seam.py`. Import `claude_line`, `codex_line`, `codex_usage` and `write` from `test_detail`, using the `sys.path.insert` pattern from `test_cache_equivalence.py`. For T3, import `build_corpus` from `test_cache_equivalence`, or use `_fake_host` from `test_remote_parse` if importing the corpus builder is too slow. `build_corpus(root, settle=False)` takes well under a second.

| # | Test | Proves |
|---|---|---|
| T1 | `test_seam_unreadable_returns_none(tmp_path)` | For both kinds, `_parse_log_file` and `_read_log_lines` return `None` for a missing path and for a directory. For kind `codex`, a 4-byte `rollout-x.jsonl.zst` with `patch.dict(sys.modules, {"compression": None})` and `patch("shutil.which", return_value=None)` also gives `None`. |
| T2 | `test_seam_matches_the_parsers(tmp_path)` | A Claude file (2 usage lines, 1 bad line, 1 compaction) and a Codex rollout (session_meta, turn_context, token_usage_record). `_parse_log_file(kind, p, want_stats=True)` equals `{"events": <parser(lines, source="local", stats=st)> with each event tagged with p, "stats": st}`, compared as canonical `_tag_json`. With `want_stats=False`, `"stats"` is `None` and the events are the same. With `label="h:/x"` and `source="h"`, every event has `_source_file == "h:/x"` and `source == "h"`. |
| T3 | `test_stats_do_not_change_events(tmp_path)` | For every `*.jsonl` and `*.jsonl.zst` log file in both homes of `build_corpus(..., settle=False)`, skipping `cctrack/` hook dirs and files that come back `None`: events with `want_stats=True` equal events with `want_stats=False`. Assert at least 10 files were compared. This is the premise Step 4 relies on. |
| T4 | `test_agent_id_from_name_only_when_asked(tmp_path)` | Sidechain line without `agentId` in `.../subagents/agent-b2.jsonl`: `agent_id_from_name=True` gives `"b2"` and `False` gives `""`. A sidechain line with `agentId="a1"` in `agent-b2.jsonl` stays `"a1"`. A sidechain line without `agentId` in `s1.jsonl` stays `""`. A non-sidechain line in `agent-b2.jsonl` stays `""`. |
| T5 | `test_remote_paths_never_fill_agent_id_from_filename(tmp_path, monkeypatch)` | (a) `_remote_parse("dgx", raw, "___CCTRACK_FILE_SEP___", cctrack.parse_lines, {})`, with `raw` carrying the path `/h/.claude/projects/p/s1/subagents/agent-b2.jsonl` and one sidechain line without `agentId`, gives one event whose `agent_id` is `""`. (b) `env = _fake_host(tmp_path)`, then `monkeypatch.setenv(k, v)` for each of its `HOME`/`SANDY_HOME` keys and `monkeypatch.delenv("CODEX_HOME", raising=False)`. `conftest.py` already stubs `_sandy_print_state`, and the directory scan of `SANDY_HOME` finds the fake sandbox. Then `cctrack._remote_collect("dgx", True, False)["claude"]` contains request `r3` (from `agent-a1.jsonl`) with `agent_id == ""`. Pins F4's remote side (finding 9). |
| T6 | `test_every_call_site_uses_the_seam(tmp_path, monkeypatch)` | Wrap `cctrack._parse_log_lines` and `cctrack._read_log_lines` with `monkeypatch.setattr` counting wrappers that record `(kind, label)` / `(kind, path)` and delegate to the originals. Then: `parse_events([a, b, missing])` records 2 parse calls of kind claude, labels `[a, b]` in that order, and 3 read calls. `parse_codex_events([r1])` records 1 codex parse call. `_remote_parse("dgx", raw_two_files, sep, parse_lines, {})` records 2 claude parse calls with `dgx:`-prefixed labels and 0 read calls. `_remote_collect("dgx", True, True)` on the `_fake_host` home (set up as in T5(b)) records exactly `len(discover_files(default_dirs())) + len(discover_codex_files())` read calls, computed under the same environment, in discovery order. It also records one parse call per readable file, all with `dgx:` labels. |
| T7 | `test_plain_files_keep_readlines_splitting(tmp_path)` | A Claude usage line written with `json.dumps(obj, ensure_ascii=False)` whose `cwd` contains a raw `"\u2028"`. `_parse_log_file("claude", p, want_stats=True)` gives 1 event, and stats `lines == 1`, `bad_lines == 0`. The same check also runs through `parse_events([p], file_stats=fs)`. |
| T8 | `test_codex_plain_unreadable_is_not_a_zst_skip(tmp_path)` | `fs = {}`. `parse_codex_events([str(tmp_path / "rollout-missing.jsonl")], file_stats=fs)` returns `([], 0)` and `fs[path]["unreadable"] is True`. Also `parse_events([missing], file_stats=fs2)` gives `fs2[missing] == {**new_parse_stats(), "unreadable": True}`. |
| T9 | `test_seam_rejects_unknown_kind()` | `_read_log_lines("hook", "/x")`, `_parse_log_lines("hook", [], "x")` and `_log_kind(len)` each raise `ValueError`. |

### Mapping the brief's criteria

| Criterion | Proof |
|---|---|
| Accept: suite passes untouched | `PYTHONPATH=src python3 -m pytest tests -q`: 394 + 9 new passed, 1 skipped. `git diff --stat a5eed3e -- tests` shows only the new `tests/test_parse_seam.py`. |
| Accept: equivalence harness unchanged | `tests/test_cache_equivalence.py` passes. **And** `compare.py OLD NEW` (section 7) reports 57 entries, 0 differences. That covers all 56 stdout outputs of the section 5 matrix plus the in-process event/stats dump. |
| Accept: `tests/test_remote_parse.py` still proves remote parse equals raw stream | That file passes unchanged. Both of its paths now go through `_parse_log_lines` (T6). |
| Mutation: drop the local-only fallback | M1 below. |
| Added: one seam, used everywhere | T6 |
| Added: F4 remote difference preserved | T5 |
| Added: line splitting preserved | T7 |
| Added: `.zst` skip and unreadable stats preserved | T1, T8, plus the existing `tests/test_codex.py::test_zst_skipped_and_counted_without_decompressor` |
| Added: stats are event-neutral (needed for Step 4) | T3 |

### Mutation checks

Run each on a copy of `src/` (`cp -r src $S/mut`, then `PYTHONPATH=$S/mut python3 -m pytest tests -q`), one at a time.

| # | Change | Must fail |
|---|---|---|
| M1 (brief) | In `parse_events`, pass `agent_id_from_name=False` (or delete the fallback block in `_parse_log_lines`) | `tests/test_detail.py::test_claude_subagent_gets_own_row_linked_to_parent` (and `test_cache_equivalence.py::test_corpus_covers_section5_cases`; with the block deleted, T4 too) |
| M2 | In `_parse_remote_files`, pass `agent_id_from_name=True` | `test_parse_seam.py::test_remote_paths_never_fill_agent_id_from_filename` |
| M3 | In `parse_events`, replace the `_parse_log_file` call with an inline `open`/`parse_lines`/`_tag_event_file` (behaviour otherwise identical) | `test_parse_seam.py::test_every_call_site_uses_the_seam` |
| M4 | In `_read_log_lines`, read plain files with `Path(path).read_text(errors="replace").splitlines()` | `test_parse_seam.py::test_plain_files_keep_readlines_splitting` |
| M5 | In `parse_codex_events`, count `skipped += 1` for every unreadable file (drop the `.zst` condition) | `test_parse_seam.py::test_codex_plain_unreadable_is_not_a_zst_skip` |
| M6 | In `_parse_log_lines`, always pass `stats=new_parse_stats()` (ignore `want_stats`) | `test_parse_seam.py::test_seam_matches_the_parsers` (`stats` must be `None`) |
| M7 | In `_parse_log_lines`, call `_tag_event_file(e, label)` only when `agent_id_from_name` is false | `test_detail.py` (source paths), `test_parse_seam.py::test_seam_matches_the_parsers` |

## 7. Verification

Let `S=/tmp/claude-501/-home-sandy-dev-cctrack/b90020de-1841-4523-9564-a7f61b1fabce/scratchpad`, or any empty scratch dir.

```bash
cd /home/sandy/dev/cctrack
PYTHONPATH=src python3 -m pytest tests -q                      # 403 passed, 1 skipped (394 + 9)
PYTHONPATH=src python3 -m pytest tests/test_parse_seam.py tests/test_remote_parse.py tests/test_cache_equivalence.py -q -v
git diff --stat a5eed3e -- src tests CLAUDE.md README.md        # src/cctrack/__init__.py, tests/test_parse_seam.py, CLAUDE.md only
grep -n 'open(path' src/cctrack/__init__.py                      # no hits left in parse_events / parse_codex_events
```

**Old-vs-new on the fixture corpus.** This is the "equivalence harness unchanged" proof. Get the old tree with `mkdir -p $S/old && git -C /home/sandy/dev/cctrack archive a5eed3e src | tar -x -C $S/old`. Save the two scripts below into `$S`. Then run `python3 $S/compare.py $S/old/src /home/sandy/dev/cctrack/src`. Expected last line: `matrix entries: 57 differences: []`, exit 0.

`$S/dump_events.py`:
```python
"""Print a sha256 of the per-site parse output of whichever cctrack PYTHONPATH selects,
run against a corpus home. Usage: dump_events.py HOME"""
import hashlib, json, os, sys
home = sys.argv[1]
os.environ["HOME"] = home; os.environ["SANDY_HOME"] = home + "/.sandy"
os.environ.pop("CODEX_HOME", None); os.environ["CCTRACK_NO_PROGRESS"] = "1"
import cctrack
def canon(x): return json.dumps(cctrack._tag_json(x), sort_keys=True, default=str)
out = {}
files = cctrack.discover_files(cctrack.default_dirs())
for want in (False, True):
    fs = {} if want else None
    ev = cctrack.parse_events(files, file_stats=fs)
    out[f"claude stats={want}"] = canon([ev, fs])
    cfs = {} if want else None
    cev, sk = cctrack.parse_codex_events(cctrack.discover_codex_files(), file_stats=cfs)
    out[f"codex stats={want}"] = canon([cev, sk, cfs])
out["remote_collect"] = canon(cctrack._remote_collect("fakehost", True, True))
for k, v in out.items():
    print(k, hashlib.sha256(v.encode()).hexdigest()[:16], len(v))
```

`$S/compare.py`:
```python
"""Build the step-0 corpus once; compare stdout of the full section 5 matrix and the
per-site parse dumps between two source trees. Usage: compare.py OLD_SRC NEW_SRC"""
import os, subprocess, sys, tempfile
from pathlib import Path
repo = Path("/home/sandy/dev/cctrack")
old, new = sys.argv[1], sys.argv[2]
sys.path[:0] = [new, str(repo / "tests")]
import test_cache_equivalence as h
root = Path(tempfile.mkdtemp(prefix="seam-cmp-"))
corpus = h.build_corpus(root)
res = {}
for src in (old, new):
    res[src] = h.run_matrix(corpus, env={"PYTHONPATH": src})
    d = subprocess.run([sys.executable, str(Path(__file__).with_name("dump_events.py")), str(corpus.home)],
                       env={**os.environ, "PYTHONPATH": src}, capture_output=True, text=True, check=True)
    res[src][("dump", "")] = d.stdout
    print(src, "\n" + d.stdout)
bad = h.diff_keys(res[old], res[new])
print("matrix entries:", len(res[old]), "differences:", bad)
sys.exit(1 if bad or h._utc_today().date().isoformat() != corpus.anchor else 0)
```

Optionally, sanity-check that the comparison has teeth: apply M1 to a copy of the new tree and rerun. It should report about 15 differences.

**Live smoke against this sandbox's real logs.** The script snapshots `~/.claude/projects` (and `~/.codex` if present) into a scratch home first, because the running session's transcript grows between runs. Run `bash $S/live_compare.sh $S/old/src /home/sandy/dev/cctrack/src`. Every line should read `same`, with exit 0.

`$S/live_compare.sh`:
```bash
#!/bin/bash
# Usage: live_compare.sh OLD_SRC NEW_SRC — snapshot this sandbox's real Claude logs into a
# scratch home, run every report mode with both source trees, diff stdout.
set -u
OLD=$1; NEW=$2; S=$(dirname "$(readlink -f "$0")")
H=$(mktemp -d "$S/live-home.XXXX"); mkdir -p "$H/.claude"
cp -a ~/.claude/projects "$H/.claude/projects"
[ -d ~/.codex ] && cp -a ~/.codex "$H/.codex"
D=$(date -u +%F); rc=0
for args in "" "--explore $D" "--sessions --bucket hour --coverage" "--json" "--csv sessions" "--csv buckets" "--csv coverage" "--csv rates" "--agent claude --json"; do
  for t in OLD NEW; do
    HOME=$H SANDY_HOME=$H/.sandy CCTRACK_NO_PROGRESS=1 PYTHONPATH=${!t} python3 -m cctrack $args </dev/null 2>/dev/null \
      | sed 's/"generated_at": "[^"]*"/"generated_at": "X"/' > "$H/out.$t"
  done
  if cmp -s "$H/out.OLD" "$H/out.NEW"; then echo "same   [$args] $(wc -c <"$H/out.NEW") bytes"; else echo "DIFFER [$args]"; rc=1; fi
done
exit $rc
```

Also run the plain smoke: `PYTHONPATH=src python3 -m cctrack >/dev/null && PYTHONPATH=src python3 -m cctrack --sessions --coverage >/dev/null`. Both should exit 0.

Run M1–M7 (section 6) and confirm that each named test fails.

## 8. Deviations

- **The seam is a pair plus a reader, not a single function.** `_parse_log_file` (path → result) is the seam the brief describes. `_parse_log_lines` is its parse half. It exists because the raw-stream fallback already holds the lines in memory (it has no file to open), and the brief requires `_parse_remote_files` to share the per-file code. `_read_log_lines` is shared so that local and `_remote_collect` read files identically. Step 4 can also wrap the read with its stat-before and stat-after racy check.
- **`_parse_remote_files`/`_remote_parse` keep the parser-function argument** instead of taking a kind string, because `tests/test_detail.py` calls `_remote_parse` with `cctrack.parse_lines` and tests must not be edited. `_log_kind` maps the function to a kind.
- **Added criteria and tests beyond the brief:** T5 and M2 (remote no-fallback was untested, finding 9), T7 and M4 (line splitting, finding 3), T3 (stats event-neutral, the premise of plan 4.2), and T6 and M3 (all call sites use the seam).
- **`CLAUDE.md` gets one sentence.** Rule 7 asks for docs only on behaviour changes, and there is none here. The architecture list names functions, though, and later steps build on these names.
- No change to `docs/plans/incremental-cache.md` sections 3–5.

## 9. Out of scope

- Any cache, store, stat-based watermark or racy rule (Steps 3 and 4). Step 4 puts the cache lookup around `_parse_log_file` in `parse_events`/`parse_codex_events`. It collects stats always (finding 5) and serializes `{events, stats}` before `dedup_events` (F3).
- Hook files (Step 5) and the hook loop in `_remote_collect`, which keeps `_read_text_lines`.
- Remote protocol v2 and per-file remote replies (Step 6). `_remote_collect` still returns per-host deduped events.
- Fixing F4's differences, the `splitlines` vs `readlines` difference between the raw stream and the bundle, or the missing unreadable-file stats on remote hosts. All are pre-existing and are preserved deliberately.
