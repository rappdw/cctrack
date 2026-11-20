# Step 06. Remote protocol v2 (plus local hook files)

Plan for `### Step 6` of `docs/plans/incremental-cache.md`. Written against commit `84f3835` (step 05 skipped; 485 passed, 1 skipped, 104 s, Python 3.12.15, Linux aarch64).

**Decisions for the user.** None is needed to build this step.
- **D1 (unreachable host) is still open, and this plan does not need it.** The brief's Accept item ("Unreachable host: same warning and same report as today") is D1's recommended option, and this plan builds exactly that: a host that fails or times out contributes nothing, and its cached rows are left alone but never used. If Dan later picks the alternative ("use the last cached data and say how old it is"), it can be added on top of this step without changing the protocol. Test R5 pins today's behaviour.
- **The remote host named `local` (step 3's open question) is settled here, not a user decision.** Remote rows are stored under the host key `remote:<name>`. Local rows keep `local`. The two can never collide, even when a remote host is literally called `local` and has the same paths (test R8).
- **F10's "on dgx: other" (30 s): local decode is about 2–4 s of it** (Findings 1). Remote encoding is another 3–5 s. The rest (about 20 s) is ssh and transfer, which this sandbox cannot measure. This step adds timing sub-lines so Dan's next `CCTRACK_TIMING=1` run splits it up.

## 1. Outcome

- `REMOTE_PROTOCOL` is `CCTRACK_REMOTE_V2`. It is still one `ssh host python3 -` call, and the remote is still read-only.
- Local sends a manifest inside the program: `{rkey, files: {kind: {path: [size, mtime_ns]}}}` for the rows it holds for that host with the current code key.
- The remote runs its normal discovery. For each Claude transcript, Codex rollout and `statusline*.jsonl` file, in discovery order, it emits either a hit marker (stat matches the manifest) or that file's own per-file parse. It never runs a cross-file step.
- Local walks the reply in order. It loads hits from the cache and stores misses (encoding each one at `put`, before any dedup). Then it runs `dedup_events` per kind and the final hook sort itself. A hit the cache cannot supply triggers one full refetch with an empty manifest.
- **Local hook files go through the cache too** (kind `hook`, host `local`). `rollup_old_hook_files` still runs first, and monthly and baseline files are always read. A warm local run opens only hook files that changed.
- Remote hook entries use the remote's semantics (blob splitting, recursive glob, one sort across folders) and local hook entries use the local semantics, exactly. A statusline file that would break the remote blob's separators makes the remote fall back to today's whole-blob hook parse for that run.
- `--no-cache` sends an empty manifest and stores nothing. `--rebuild-cache` starts from an empty store, so it re-reads every remote file. The raw stream fallback (`CCTRACK_REMOTE_PARSE=0`, a host without Python 3.10+) is unchanged and uncached. A failed or timed-out host behaves as today.
- With `CCTRACK_TIMING=1`, the `remote <host>` line says `…received; cache: N reused, M read`. It gains the sub-lines `on <host>: encode`, `local: decode` and `local: merge`, and `hook read` gets the `; cache: …` note.
- stdout is byte-identical with the cache off, cold, warm or rebuilt, with `--remote`.

## 2. Findings

All checks ran in this sandbox: Linux aarch64, 10 cores, Python 3.12.15, uid 501, no zstd decoder. `S=/tmp/claude-501/-home-sandy-dev-cctrack/b90020de-1841-4523-9564-a7f61b1fabce/scratchpad`. The scripts are in `$S/s6/`.

1. **F10's "on dgx: other" and local decode** (`$S/s6/f10_decode.py`, `real_reply.py`).
   - I encoded this sandbox's real logs the way `_remote_main` does: 1,283 events and 733 hook rows came to 90 KB, about 70 compressed bytes per event.
   - Then I built synthetic replies from real events, with random request ids, message ids, timestamps and token counts, 1,500 file labels and per-file stats. 200,000 events plus 300,000 hook rows gave **27.7 MB** compressed (196 MB of JSON), about F10's 28.7 MB.

     | Side | Piece | Time (2 runs) |
     |---|---|---|
     | remote | `_tag_json` | 1.24 s |
     | remote | `json.dumps` | 0.45 s |
     | remote | `zlib` level 6 | 1.19 s |
     | **local decode** | `zlib.decompress` | 0.22–0.31 s |
     | | `json.loads` | 0.54–0.62 s |
     | | `_untag_json` | 1.16–1.46 s |
     | | **total** | **1.9–2.4 s** |

   - Random ids compress worse than real ones, so a real 28.7 MB reply holds more events, perhaps 1.5×. A fair estimate on the Mac is **local decode 2–4 s (7–13% of the 30 s) and remote encode 3–5 s**.
   - Remote program start-up (decompressing and compiling the module) took 0.05–0.07 s.
   - Local `compute_hook_deltas` for dgx's rows also runs inside the `remote dgx` phase. Step 5 measured 0.22–0.25 s for 412,794 rows.
   - That leaves roughly 20 s for ssh connection and transfer. I can't measure that here. 28.7 MB in 20 s would be about 1.4 MB/s, which points to a slow link.
   - So v2 helps mostly by shipping less: on a warm run only changed files' events cross the wire.
   - **Dead end:** a lower zlib level would cut encode time but enlarge the reply, which is the wrong trade on a slow link. Keep level 6.
   - The new timing sub-lines (section 4.9) measure encode, decode and merge on the real machines.

2. **Remote hook per-file semantics** (`$S/s6/hookprop.py`).
   - In each of 5,000 random cases, a set of statusline file texts was joined into a v1 blob and compared with the v2 per-file path. The texts included raw U+2028, `\x1c`, `\x0b`, U+0085, `\r`, the `HOOK_FILE_SEP` text inside a line, unparseable and blank lines, missing final newlines, `null` costs, non-UTC offsets, and equal `(session_id, ts)` in different files.
   - v1 is `parse_hook_blob(blob)`. v2 parses each file with `_hook_rows_from_lines([l for ch in text.split(HOOK_FILE_SEP) for l in ch.splitlines()])`, joins the rows in file order and stable-sorts them by `(session_id, ts)`. The monthly and baseline parts come from a blob that holds only those parts.
   - Result: **0 mismatches**. 364 cases were excluded by the rule below.
   - **The separator rule is needed.** If a statusline file contains `MONTHLY_FILE_SEP` or `BASELINE_FILE_SEP`, v1's `partition`/`split` treats every later file in the blob as monthly or baseline data. That cannot be reproduced per file.
     - Among 1,770 such cases, the naive per-file result differed from v1 in 1,527.
     - So when a missed statusline file contains either separator, the remote returns today's whole-blob result for hooks in that run (section 5).
     - A file never reaches the store with such content, so a cached hit cannot hide one.
   - **Local differs, as step 5 found.** A raw U+2028 inside a JSON string gives 1 row via `readlines` (local) and 0 via the blob (remote). Each side keeps its own rule.

3. **Reply sizes** (`$S/s6/v2size.py`, `manifest_size.py`).
   - For `test_remote_parse`'s fake host, v1 is 901 bytes, v2 cold 1,002 and v2 warm 386. That is under the existing test's 3,000-byte bound.
   - At dgx scale (1,472 Claude, 94 Codex and 581 hook paths, uuid names): the manifest is 279 KB of JSON, 71 KB after zlib-9 and 95 KB as base64 inside the program. The remote decodes it in 1 ms. The hit markers in a warm reply are 73 KB compressed.
   - Both are small next to a cold reply. On dgx, a warm reply still carries the ~6 GB of changed files' events (F10 readings), so it shrinks but does not vanish.

4. **The existing call sites that need care.**
   - `tests/test_parse_seam.py` calls `cctrack._remote_collect("dgx", True, False)["claude"]` and expects deduped events. It also checks that `_remote_collect` reads every Claude and Codex file through `_read_log_lines` in discovery order.
   - So `_remote_collect` keeps its v1 return shape: it becomes `_assemble_remote_bundle(host, _remote_collect_files(...), None)`. The v2 wire producer is the new `_remote_collect_files`.
   - `tests/test_cache_equivalence.py::test_remote_program_unchanged_without_timing` requires the program to end `mod._remote_main('h', True, False)\n`. The manifest argument is therefore appended only when it is non-empty.
   - conftest stubs `fetch_remote_bundle` as `lambda host, *a, **k`, so a new `cache=` keyword is safe.

5. **Existing tests whose premise this step changes** (each was checked against the code):
   - `test_cache_local.py::test_remote_runs_cache_only_local_files` (T9) asserts that the only host is `local` and that there are 9 rows. Both stop being true.
   - `test_rebuild_replaces_the_cache` (T4) compares every `entries.path` with the 8 log files. Local hook rows are now also present.
   - `test_interrupted_run_keeps_parsed_files` (T14) counts 5 rows. The hook read runs before the interrupted Codex parse, so hook rows are stored too.

   Section 6 makes each of these more specific; none is weakened (section 8).

6. **Making the fake remote observable** (prototype: `$S/s6/remote_site_proto.py`).
   - A `sitecustomize.py` on the fake remote's `PYTHONPATH` is imported by `python3 -`. An `open` audit hook installed there logs every `.jsonl`/`.jsonl.zst` the remote opens.
   - The same file can install a fake `compression.zstd` module (a raw-frame reader). With it, `_zstd_available()` is True on the remote and `_read_zstd` decodes the corpus's `.zst` through a logged `open`, so remote `.zst` handling is the same on every machine.
   - This sandbox has no system `sitecustomize`, so nothing is shadowed.
   - The local launcher process never imports it, because only the fake ssh exports that `PYTHONPATH`.

7. **Dead end: `/dev/shm` is mounted `noexec`.** A fake `ssh` written there is skipped by `PATH` lookup, and cctrack silently reaches the real `/usr/bin/ssh` ("Connection refused"). Fake ssh scripts must live under `/tmp` (pytest's `tmp_path`, or the scratchpad). Large data may still go in `/dev/shm`. `$S/s6/live_remote.sh` already does this.

8. **Live smoke at HEAD** (`$S/s6/live_remote.sh`). It takes two `cp -a` snapshots of this sandbox's real `~/.claude/projects` and `~/.claude/cctrack`: one is the local home, the other a fake host. It ran 9 modes × 4 cache modes: all `same`. The host listing (names, types, sizes and mtimes, directories included) was unchanged, and `~/.cache/cctrack` was never created. At HEAD the `remote fakehost` line shows `90.5 KB received` warm and cold. After this step the warm line must show reuse.

9. **Bench at HEAD** (`scripts/bench_cache.py --dir $S/bench6 --claude-files 60 --codex-files 10 --remote --runs 3`, 112 MB):
   - `remote benchhost` took 0.566 s cold and 0.461 / 0.440 s warm; `hook read` took 0.060, 0.038 and 0.032 s.
   - This is the before figure for verification 6.
   - The bench's fake ssh lives under `--dir`, so `--dir` must not be in `/dev/shm` (finding 7).

## 3. Files

| File | Change |
|---|---|
| `src/cctrack/__init__.py` | Protocol v2: `_parse_env_key`, `_remote_cache_params`, `_remote_host_key`, `CacheStore.host_rows`, `_remote_manifest`, `_remote_collect_files`, `_remote_file_item`, `_remote_hook_items`, `_remote_hook_blob`, `_remote_hook_extra`, `_hook_rows_from_blob_text`, `_hook_text_breaks_blob`, `_assemble_remote_bundle`, `_remote_exchange`; `_remote_collect`, `_remote_main`, `_remote_program`, `fetch_remote_bundle` and `_note_remote_timing` changed; `REMOTE_PROTOCOL` V2, `REMOTE_TIMING`. Local hooks: `_HOOK_LOCAL_PARAMS`, `_hook_files`, `_hook_file_rows_cached`, `parse_hook_rows(…, cache=None)`, and the hook loop in `main`. Update the "Remote parsing" and "Parse cache" comment blocks. |
| `tests/test_cache_equivalence.py` | Corpus: a legacy `statusline.jsonl` and a sandbox hook dir in both homes. `REMOTE_SITE` (`sitecustomize.py`) written by `build_corpus` and put on the fake remote's `PYTHONPATH`. `Launched` gains `hook_opens` and `remote_opens`. `run_launched` gains `remote_zstd`. Add `remote_listing`. |
| `tests/test_cache_local.py` | T4, T9 and T14 updated (section 6.4). Add H2–H4. |
| `tests/test_cache_remote.py` (new) | R1–R10, H1 (section 6). |
| `tests/test_remote_parse.py` | Add `test_remote_parse_with_warm_cache_matches_raw_stream`. |
| `CLAUDE.md` | Items 3 and 4a (section 4.10). |
| `README.md` | "Parse cache" section's last line (section 4.10). |
| `docs/plans/incremental-cache.md` | Already edited by the planner: 4.4 host key, 4.6 rewrite, F10 reading (section 8). |

`scripts/bench_cache.py` is unchanged.

## 4. Interfaces

### 4.1 Keys

```python
def _parse_env_key() -> str:
    """What decides a parse on the machine running it, besides the source and the file:
    f"{sys.implementation.name}-{'.'.join(map(str, sys.version_info[:3]))} {enc}", with
    enc = codecs.lookup(io.TextIOWrapper(io.BytesIO()).encoding).name. Runs on the remote
    (the 'rkey'). Imports codecs/io inside."""

def _remote_host_key(host: str) -> str:
    """The `host` column for a --remote host's rows: "remote:" + host. Local rows are
    "local", so a host named "local" can never collide with them."""

def _remote_cache_params(host: str, rkey: str) -> str:
    """json.dumps(["remote", host, rkey]); the params part of every remote row's code.
    host fixes source (= host) and label (= f"{host}:{path}"); remote parses never use
    agent_id_from_name; the kind is in the key."""

_HOOK_LOCAL_PARAMS = json.dumps(["hook", "local"])
```

`cache_code_key()` is unchanged; the implementer may not alter its output.

A remote row's `code` column is `f"{store.code_key} {_remote_cache_params(host, rkey)}"`. The local code key (module sha256, local interpreter, local marshal version, local encoding) covers the source the remote ran and the local payload format. `rkey` covers the remote interpreter and the remote's text encoding.

### 4.2 Store

```python
class CacheStore:
    def host_rows(self, host_key: str) -> list[tuple[str, str, int, int, str]]:
        """[(kind, path, size, mtime_ns, code)] for one host key, without payloads.
        Any error: self._fail(e) and []. Not counted as hits; does not touch last_used."""
```

### 4.3 Manifest

```python
def _remote_manifest(store, host: str, kinds: tuple[str, ...]) -> dict | None:
    """The manifest to send to `host`, or None for an empty one. Only rows whose code is
    f"{store.code_key} " + json.dumps(["remote", host, rkey]) for some rkey and whose kind
    is in `kinds` qualify (parse the JSON tail; skip anything else). If several rkeys
    qualify, use the one with the most rows (ties: the greatest string). Returns
    {"rkey": rkey, "files": {kind: {path: [size, mtime_ns]}}}, or None if nothing qualifies
    or the store is None/disabled."""
```

`kinds` is `("claude", "hook")` if `want_claude`, plus `("codex",)` if `want_codex`.

**Transport.** `_remote_program(host, want_claude, want_codex, timing=False, manifest=None)`:
- if `manifest` is truthy, it encodes `base64.b64encode(zlib.compress(json.dumps(manifest, separators=(",", ":")).encode(), 9)).decode()`;
- it appends `, manifest='<that>'` to the `mod._remote_main(...)` call, after the optional `timing=True`;
- with no manifest, the program text is unchanged from today.

`_remote_main(host, want_claude, want_codex, timing=False, manifest="")` decodes it. Any error in decoding, or a result without a `"files"` dict, means an empty manifest. It never raises.

### 4.4 Remote collector (runs on the host)

```python
REMOTE_PROTOCOL = b"CCTRACK_REMOTE_V2\n"
REMOTE_TIMING = "CCTRACK_REMOTE_TIMING "

def _remote_collect_files(host: str, want_claude: bool, want_codex: bool,
                          timing: bool = False, manifest: dict | None = None) -> dict:
    """Runs ON the remote host; read-only; never opens a cache. Same discovery as v1.
    Returns the v2 wire data (shape below). Never calls dedup_events across files."""
```

The wire data, before `_tag_json`:

```python
{
  "python": "3.12.3",
  "rkey": _parse_env_key(),
  "files": {                      # a kind is present only if it was collected
      "claude": [item, ...],      # discovery order: discover_files(default_dirs())
      "codex":  [item, ...],      # discover_codex_files()
      "hook":   [item, ...],      # absent when the hook fallback fired
  },
  "hook_extra": [monthly, baselines],      # with files["hook"]
  "hook": (rows, monthly, baselines),       # instead, only when the hook fallback fired
  "counts": {kind: [reused, read]},         # read = discovered - reused (unreadable included)
  "zst_skipped": int,                       # only when want_codex
  "timing": {...},                          # only when timing, same shape as v1
}
item = [path, size, mtime_ns, "h"]                     # hit: the file was stat'ed, not read
     | [path, size, mtime_ns, "s" | "n", payload]      # parsed; "s" = the local side may store it
payload = {"events": [...], "stats": {...} | None}     # claude/codex: stats always present
                                                       # hook: {"events": rows, "stats": None}
```

The manifest is used only if `manifest.get("rkey") == _parse_env_key()`. Otherwise every file is a miss.

```python
def _remote_file_item(host: str, kind: str, path: str, known: dict) -> list | None:
```
For `claude` and `codex`:
1. `sig = _file_sig(path)`.
2. **Hit** when all of these hold:
   - `sig is not None`;
   - `known.get(path) == [sig[0], sig[1]]`;
   - the file is not a `.zst`, or `_zstd_available()`.

   The marker carries the file's **current** `sig`, which by this check equals the manifest's. The file is not opened.
3. Otherwise:
   - `lines = _read_log_lines(kind, path)`; `None` means return `None` (unreadable: no item).
   - `r = _parse_log_lines(kind, lines, f"{host}:{path}", source=host, want_stats=True)`.
   - `ok = sig is not None and _file_sig(path) == sig and not _is_racy(sig, time.time_ns())` (the remote's clock).
   - Return `[path, *(sig or (0, 0)), "s" if ok else "n", r]`.

The caller keeps v1's progress lines (`f"{i:,}/{n:,} files"`) and `zst_skipped` counting (`None` for a `.zst` path adds 1).

Hook files: `_remote_hook_items(host, hook_dirs, known) -> list | None`.
- Files are `[f for d in hook_dirs for f in sorted(d.rglob("statusline*.jsonl"))]`, v1's order.
- Each file follows the same hit/miss rule, without the `.zst` clause.
- On a miss: `text = "".join(lines)` with `lines = _read_text_lines(str(f))`; `None` means no item.
  - If `_hook_text_breaks_blob(text)`, return `None` for the whole call: the fallback.
  - Otherwise the item's payload is `{"events": _hook_rows_from_blob_text(text), "stats": None}`, flagged `"s"`/`"n"` by the same racy and changed-while-read rule.
- `hook_dirs = discover_hook_data_dirs()`, as in v1. The hook part runs only if `want_claude`, after the Claude part, as in v1.

```python
def _hook_rows_from_blob_text(text: str) -> list[dict]:
    """One statusline file's rows exactly as parse_hook_blob would split it:
    _hook_rows_from_lines([l for ch in text.split(HOOK_FILE_SEP) for l in ch.splitlines()])."""

def _hook_text_breaks_blob(text: str) -> bool:
    """MONTHLY_FILE_SEP in text or BASELINE_FILE_SEP in text."""

def _remote_hook_blob(hook_dirs) -> str:
    """v1's blob, verbatim: for (pattern, sep) in (statusline*, HOOK), (monthly-*, MONTHLY),
    (session-baselines.json, BASELINE): for d in hook_dirs: for f in sorted(d.rglob(pattern)):
    lines = _read_text_lines(str(f)); if lines is not None: parts.append(sep + "\n" + "".join(lines)).
    Returns "\n".join(parts) ("" if none)."""

def _remote_hook_extra(hook_dirs) -> list:
    """[monthly, baselines] from parse_hook_blob of the same blob built from the monthly and
    baseline patterns only ([{}, {}] if there are none)."""
```

Fallback: when `_remote_hook_items` returns `None`, use `out["hook"] = parse_hook_blob(b) if (b := _remote_hook_blob(hook_dirs)) else ([], {}, {})`. Leave out `files["hook"]` and `hook_extra`. `counts["hook"]` is `[0, n_files]`.

```python
def _remote_collect(host, want_claude, want_codex, timing=False) -> dict:
    """v1-shaped result (claude, claude_stats, codex, codex_stats, zst_skipped, hook, timing,
    python), as the local side assembles it: _assemble_remote_bundle(host,
    _remote_collect_files(host, want_claude, want_codex, timing), None) minus the
    bookkeeping keys. Kept for tests and as the definition of "the per-host result"."""
```

`_remote_main` writes `REMOTE_PROTOCOL + zlib.compress(json.dumps(_tag_json(data), separators=(",", ":")).encode(), 6)`, as today, using `_remote_collect_files`. With `timing`, after flushing stdout, it writes one stderr line: `f"{REMOTE_TIMING}encode={secs:.6f}\n"`, where `secs` covers `_tag_json`, `json.dumps` and `zlib`.

### 4.5 Local assembly

```python
def _assemble_remote_bundle(host: str, data: dict, store) -> dict | None:
    """v2 wire data -> the bundle fetch_remote_bundle caches (v1 keys plus bookkeeping).
    None if a hit marker can't be supplied (no store, store disabled, row gone, bad blob,
    wrong sig) — the caller then refetches. Misses flagged "s" are put (encoded at put,
    before dedup) under _remote_host_key(host) with _remote_cache_params(host, data["rkey"]),
    when _key_text_ok(host key, path, label)."""
```

Algorithm. Let `hk = _remote_host_key(host)`, `params = _remote_cache_params(host, data["rkey"])` and `st = store if store is not None and store.enabled else None`.
- For `kind` in `("claude", "codex")`, if `kind in data["files"]`:
  1. Set `lists = []` and `stats = {}`.
  2. For each item, in order:
     - a hit gives `p = st.get(hk, kind, path, (size, mtime), params) if st else None`; if `p is None`, return `None`;
     - a miss gives `p = item[4]`, and if its flag is `"s"` and `st` is set, `st.put(hk, kind, path, (size, mtime), p, params)`.
  3. In both cases: `lists.append(p["events"])` and `stats[f"{host}:{path}"] = p["stats"]`.
  4. Then `res[kind] = dedup_events(lists)` and `res[f"{kind}_stats"] = stats`.
- Hook, if `"hook" in data["files"]`: concatenate each item's rows in order, the same way, then `rows.sort(key=lambda r: (r["session_id"], r["ts"]))`, and set `res["hook"] = (rows, *data["hook_extra"])`.
- Else, if `"hook" in data`: `res["hook"] = tuple(data["hook"])`.
- Copy `python`, `zst_skipped` (if present) and `timing` (if present).
- Set `res["cache_counts"] = {"reused": Σ counts[k][0], "read": Σ counts[k][1]}` and `res["_cached"] = st is not None`.
- Record `res["_local_merge"]` (seconds).

`dedup_events` is the same function `_parse_remote_files` applies. It gets the same per-file lists in the same order, so the per-host result is unchanged (plan 4.6 step 3).

### 4.6 `fetch_remote_bundle`

```python
def fetch_remote_bundle(host: str, want_claude: bool = True, want_codex: bool = True,
                        cache: "CacheStore | None" = None) -> dict:
```

1. Use `_remote_bundle_cache` and the `CCTRACK_REMOTE_PARSE=0` → `{"fallback": True}` check, as today.
2. Set `st = cache if cache is not None and cache.enabled else None`.
   - **Source guard.** If `st` is set and `st.code_key` does not start with `hashlib.sha256(Path(__file__).read_bytes()).hexdigest() + " "`, set `st = None` for this host. The module changed on disk mid-run, so the shipped source would not match the key.
3. `manifest = _remote_manifest(st, host, kinds) if st else None`.
4. Call `ex = _remote_exchange(host, want_claude, want_codex, manifest)`. This function is today's ssh call plus reply handling, moved out unchanged:
   - same messages and the same `{"failed"}`/`{"fallback"}` results;
   - on success it returns `{"data": <untagged wire data>, "wire": len(out), "decode": secs, "encode": secs | None}`;
   - `decode` times `zlib.decompress` + `json.loads` + `_untag_json`;
   - `encode` is parsed from the last stderr line starting with `REMOTE_TIMING` (`encode=` value); `None` if there is no such line.

   A wire-data error in assembly (`KeyError`, `IndexError`, `TypeError`, `ValueError`) is treated like an unreadable reply today: the note `couldn't read {host}'s parsed results (...)`, then `{"fallback": True}`.
5. Set `res = _assemble_remote_bundle(host, ex["data"], st)`.
6. If `res is None`:
   - print `f"cctrack: cache couldn't supply {host}'s unchanged files; fetching everything from {host} again"` to stderr;
   - make a second exchange with `manifest=None`;
   - if it fails, it gives `{"failed"}`/`{"fallback"}` as usual;
   - assemble it with `st`; if that is `None` again (a hit in a reply to an empty manifest, which is a protocol error), give the unreadable-reply note and `{"fallback": True}`;
   - set `res["_refetched"] = True`.
7. `res = {"ok": True, **res, "_wire_bytes": <sum over exchanges>, "_local_decode": <sum>, "_remote_encode": <last, or absent>}`. Then the `zst_skipped` note as today. Cache the result in `_remote_bundle_cache`.

`main` passes `cache=store`. The `parse_remote_*` functions are unchanged.

### 4.7 Local hook files

```python
def _hook_files(cctrack_dir: Path) -> list[Path]:
    """sorted(cctrack_dir.glob("statusline-*.jsonl")) plus cctrack_dir / "statusline.jsonl"
    appended if it exists — exactly parse_hook_rows' list today."""

def _hook_file_rows_cached(store, path: Path) -> list[dict] | None:
    """One local hook file's rows, `_hook_rows_from_lines(_read_text_lines(p))`, through
    the cache (host "local", kind "hook", params _HOOK_LOCAL_PARAMS). None = unreadable
    (never stored). Same hit/racy/changed-while-read/_key_text_ok rules as
    _parse_log_file_cached; counts a miss in store.counts["misses"]."""

def parse_hook_rows(cctrack_dir: Path | None = None, cache=None) -> list[dict]:
    """cache None: today's code, unchanged. With a cache: rows of each _hook_files() entry
    via _hook_file_rows_cached, concatenated in that order, then
    rows.sort(key=lambda r: (r["session_id"], r["ts"])) (stable; step 5 finding 4)."""
```

In `main`'s hook loop, order per dir stays `rollup_old_hook_files(hdir)` → `load_session_baselines(hdir)` → `parse_hook_rows(hdir, cache=store)` → deltas → monthly. Count the hook files: `n_hook += len(_hook_files(hdir))` after the rollup. Take `h0 = _cache_hits(store)` before the loop and call `_note_cache_use(timing, "hook read", store, h0, n_hook)` after it.

### 4.8 Test helpers (`tests/test_cache_equivalence.py`)

- `_populate(home, tag, day)` also writes:
  - `hook / "statusline.jsonl"` (legacy): `rows("leg" + tag, (0.0, 0.40), day(2))`.
  - `box / "claude" / "cctrack" / f"statusline-{d1}.jsonl"`: `rows("s1", (0.0, 0.05, 0.30), d1) + rows("bs" + tag, (0.0, 0.20), d1)`. The first three rows have the same `(session_id, ts)` as the main dir's `s1` rows.
- `REMOTE_SITE`: the text of a `sitecustomize.py`:
  - With `CCTRACK_TEST_REMOTE_OPEN_LOG` set, it adds the same `open` audit hook as `LAUNCHER`, writing to that file with `os.write`.
  - With `CCTRACK_TEST_REMOTE_ZSTD=raw`, it installs a fake package: `sys.modules["compression"]` is a module with `__path__ = []` and a `zstd` attribute, and `sys.modules["compression.zstd"]` is that same `zstd` module. Its `open(path, mode="rt", errors=None, **kw)` reads the file with the builtin `open(path, "rb")` and decodes the raw-frame blocks as `LAUNCHER` does. It returns `io.StringIO(text)`.

  The prototype is `$S/s6/remote_site_proto.py` (tested as in finding 6).
- `build_corpus` writes `root / "remote-site" / "sitecustomize.py"` and adds `"PYTHONPATH": str(root / "remote-site")` to the env dict it passes to `_fake_ssh`.
- `Launched` gains `hook_opens: list = field(default_factory=list)` and `remote_opens: list = field(default_factory=list)`.
  - `hook_opens`: logged local paths under `corpus.root` that have a `cctrack` component and end `.jsonl`.
  - `remote_opens`: sorted paths, relative to `corpus.root`, from the remote log.
- `run_launched(…, remote_zstd: str | None = None)`:
  - always creates a second temp log, passed as `CCTRACK_TEST_REMOTE_OPEN_LOG`;
  - sets `CCTRACK_TEST_REMOTE_ZSTD` when `remote_zstd` is given;
  - reads and deletes both logs.
- `remote_listing(corpus) -> list`: sorted `(relpath, "d"|"f"|"l", size, st_mtime_ns)` for every entry under `corpus.remote_home`, with `os.lstat`.
- Helper `ssh_counter(tmp_path, corpus) -> (bin_dir, log)`: a bash `ssh` that appends `"$*"` to `log` and `exec`s `corpus.bin_dir/ssh "$@"`. It must sit under `tmp_path`, not `/dev/shm` (finding 7). `python3_calls(log)` counts lines ending in `python3 -`.

### 4.9 Timing

`_note_remote_timing(timing, host)`, for an `ok` bundle:
- The detail is `f"{format_bytes(wire)} received"`, plus `f"; cache: {reused:,} reused, {read:,} read"` when `b["_cached"]`, plus `"; refetched"` when `b.get("_refetched")`.
- After the `on {host}: <phase>` sub-lines, add:
  - `on {host}: encode` (`_remote_encode`, if present);
  - `local: decode` (`_local_decode`);
  - `local: merge` (`_local_merge`).
- Then `other`, with the text `"ssh, transfer and the rest"`.
- `fallback` gives `"raw stream"` and `failed` gives `"failed"`, unchanged.

### 4.10 Documentation text

`CLAUDE.md` item 3: replace "`_remote_collect` runs there … returns events + parse stats + hook rows" with this:

> `_remote_collect_files` runs there (read-only; no hook rollup; never opens a cache) and returns protocol v2: per file, in discovery order, either a hit marker (its `(size, mtime_ns)` matches the manifest the local side embedded in the program) or that file's own `{events, stats}` / hook rows — never deduped across files. The local side (`_assemble_remote_bundle`) loads hits from the cache, stores misses (host key `remote:<name>`, code = local code key + `["remote", host, rkey]` where rkey is the remote interpreter and encoding), and runs `dedup_events` and the hook sort itself; a hit it can't supply triggers one full refetch. Remote hook files keep the blob semantics (`_hook_rows_from_blob_text`; a file containing the monthly/baseline separators makes the remote return v1's whole-blob hook result). `_remote_collect` is the v1-shaped equivalent.

Keep the rest of item 3: the raw stream, `_run_ssh`, timeouts and progress.

`CLAUDE.md` item 4a:
- "Hook files, `--remote` hosts and the raw stream are not cached yet" becomes: "Local hook files (`parse_hook_rows(dir, cache=)`, kind `hook`, after rollup) and `--remote` hosts (item 3) go through it too; the raw stream never does."
- Add the harness's `remote_opens`/`remote_listing` and the fake remote `sitecustomize` to the harness sentence.

`README.md`, "Parse cache" section: replace the last line with:

> `--remote` hosts are cached the same way, on this machine: the host still reads only its changed files and sends only those. A host that can't be reached is left out of the report, as before.

## 5. Behaviour and edge cases

- **Per file, before dedup, on both sides.**
  - The remote never calls `dedup_events`, `_remove_codex_fork_replays` or any cross-file step on items.
  - The local side `put`s each miss while walking, before the kind's `dedup_events`. Dedup mutates fork parents' `_locations`, so a later encode would store them (plan section 5, step 4 finding).
- **Order.** Items are in remote discovery order, and hits and misses are interleaved where they fall. Local appends them in that order. Never group hits separately.
- **Hit marker sig.** The marker carries the current stat, and local `get` checks it against the stored row. A remote that wrongly claims a hit, for example by ignoring mtime, is then caught locally and costs a refetch, never a wrong report.
- **Racy and changed files, by the remote's clock.** These are flagged `"n"`: used in this run, not stored. Today's hook file and the active transcript are usually `"n"`.
- **`.zst` on the remote.** A hit needs `_zstd_available()` on the remote. A `.zst` the remote can't decode gives no item and counts in `zst_skipped`, exactly as in v1.
- **Unreadable files** give no item (v1 skipped them too), are never stored, and count as `read`.
- **rkey mismatch** (the remote's Python or locale changed): the remote ignores the manifest, every file is a miss, and the new rows replace the old ones (same primary key).
- **Code change, `--rebuild-cache`, `--no-cache`.**
  - A new local code key means no qualifying rows, so the manifest is empty.
  - `--rebuild-cache` deletes the store first, so the manifest is empty.
  - `--no-cache` gives `store is None`: an empty manifest, nothing stored and no `cache:` timing note. The program text is then byte-identical to today's apart from the protocol constant inside the source.
- **Host key.** It is always `_remote_host_key(host)`, for get, put and `host_rows`. `--remote local` and the local machine keep separate rows even for identical paths.
- **Hook fallback.**
  - Only a **missed** statusline file is checked. A stored file never contains the separators (it was checked when stored), and a hit means it is unchanged.
  - In a fallback run nothing is stored for hooks, so every later run falls back again until that file changes. That is correct, and it does not happen with real `HOOK_SCRIPT` output (`json.dumps`, ASCII-escaped; session ids and model ids would need to contain the separator text).
- **Hook semantics stay apart.**
  - Remote rows use `_hook_rows_from_blob_text`, the recursive `statusline*.jsonl` glob (legacy file in sorted position), one list across all hook dirs and one final sort, with no rollup.
  - Local rows use `readlines` per dir, `glob("statusline-*.jsonl")` with the legacy file last, and are differenced per dir after `rollup_old_hook_files`.
  - Monthly and baseline files are always read on both sides.
- **Rolled-up local files.** Rollup deletes them before `parse_hook_rows` globs, so their stale rows are never looked up (and are pruned after 30 days).
- **Want flags.**
  - `--agent codex`: the manifest holds only `codex`, and the remote collects only Codex. The bundle has no `claude` or `hook` keys, as in v1.
  - `--agent claude`: `claude` and `hook` only.
- **Failure paths.**
  - Unreachable host or ssh error 255: `{"failed"}`, the same warning, no refetch, cached rows untouched and unused (D1 as recommended).
  - Timeout: the same warning as today.
  - No python3 or old Python: `{"fallback"}`, raw stream, uncached.
- **Store disabled mid-assembly.** `get` returns `None`, which leads to a refetch with an empty manifest; `put` is a no-op. The run continues correctly with one note.
- **Source guard.** If `__init__.py` changed on disk after the store was opened, the host is fetched without a manifest and nothing is stored for it.
- **Two runs at once.** This is as in step 4. Puts are batched, and a lost race only costs a refetch or a miss.
- **Security.** The manifest holds paths and sizes the remote already has. The reply holds the same ids, paths and counts as v1. Never pickle; tagged JSON on the wire, as before.

## 6. Tests

New file `tests/test_cache_remote.py` (same import pattern as `test_cache_local.py`; reuse its `_canon`, `_agent`, `_cache_lines`, `_attempts`, `_fresh`, `_rollout`, `_append` by importing from `test_cache_local`). Subprocess tests use `run_launched(..., remote_zstd="raw")` and `_attempts` for the midnight guard. "Remote note" means the detail of the top-level `remote fakehost` timing line (run with `CCTRACK_TIMING=1`, parsed with `TIMING_RE`).

Corpus counts after section 4.8's additions:

| Where | Claude | Codex | Hook |
|---|---|---|---|
| Remote host | 5 | 4 | 4: `statusline-d1`, `statusline-day120`, `statusline.jsonl`, `box/claude/cctrack/statusline-d1` |
| Local, after rollup | 5 | 4 | 3 |

So a full remote read is `13` (`--agent claude`: 9, `--agent codex`: 4).

### 6.1 Remote tests

| # | Test | Proves |
|---|---|---|
| R1 | `test_remote_cache_modes_match_uncached(corpus)` (module corpus, backdated) | For v in `remote`, `remote-claude`, `remote-codex`, `remote-raw` × all 8 modes, run 5 times with `CCTRACK_TIMING=1`: `--no-cache`, cold, warm, `--rebuild-cache`, warm. Each run has its own `cache_home`. Checks: (1) all stdouts are equal; (2) local `opens` are `local_log_files(agent)` for off, cold and rebuild, and `[]` for both warm runs; (3) for non-raw variants, `remote_opens` is the full remote log-file list (13/9/4 paths, `.zst` included) for off, cold and rebuild, and `[]` for both warm runs; (4) the remote note is `…received` for off, `…; cache: 0 reused, N read` for cold and rebuild, and `…; cache: N reused, 0 read` for warm (N = 13/9/4); `remote-raw` gives `raw stream` with no `cache:`; (5) no `cctrack: cache` stderr line. |
| R2 | `test_remote_mutation_then_warm_matches_uncached(tmp_path, name)`, parametrized (table below) | Own corpus, `t = backdate(c)`. Take `before = --json --no-cache`, do one cold run, apply the mutation, then take `listing = remote_listing(c)`. (a) The first cached default run's `remote_opens`, `opens` and remote note match the table; the ssh wrapper counts exactly 1 `python3 -` call; no `cctrack: cache` line. (b) For modes `report`, `explore`, `detail`, `json` with `--remote fakehost`, `--no-cache` and cached stdout are equal; for Codex cases also `remote-codex` `json`. (c) For content cases, `--json --no-cache` now differs from `before`. (d) A further cached run has `remote_opens == []` and the note `M reused, 0 read`. (e) `remote_listing(c) == listing`. |
| R3 | `test_remote_host_unmodified(tmp_path)` | `remote_listing` before; then cold, warm, `--rebuild-cache`, `--no-cache` and `--json` warm, all with `--remote fakehost`; `remote_listing` after is equal; `(c.remote_home / ".cache")` does not exist. |
| R4 | `test_unsuppliable_hit_refetches_once(tmp_path)` | Cold `--remote fakehost`. Then in the db: `UPDATE entries SET payload = X'00' WHERE rowid = (SELECT rowid FROM entries WHERE host = 'remote:fakehost' AND kind = 'claude' ORDER BY path LIMIT 1)`. A cached `--json` run with the ssh wrapper and timing: stdout equals `--no-cache --json`; exactly one `cctrack: cache` line, starting `cctrack: cache couldn't supply fakehost's unchanged files`; 2 `python3 -` calls; the remote note ends `; refetched`. The next cached run: 1 call, `13 reused, 0 read`, no note. |
| R5 | `test_unreachable_host_same_warning_and_report(tmp_path)` | Cold `--remote fakehost` (fills remote rows); `ok = cached --remote fakehost` stdout. Then `PATH` gets a failing ssh first (`exit 255`, as in `test_timing_remote_raw_and_failed`). Cached and `--no-cache` runs: equal stdout; both stderrs contain `Warning: SSH to fakehost failed`; stdout `!= ok` (cached remote data not used); the remote note is `failed`; the db still has its `remote:fakehost` rows (count unchanged). |
| R6 | `test_raw_fallback_stays_uncached(tmp_path)` | (1) `CCTRACK_REMOTE_PARSE=0`: cold and warm cached runs with `--remote fakehost --json` equal `--no-cache`; no row has `host LIKE 'remote:%'`. (2) A py39 shim: a second fake ssh from `_fake_ssh(tmp_path / "old", env, python=<shim>)`, with the shim as in `test_old_python_on_host_falls_back_to_raw_stream`. Same assertions, and stderr has `cctrack needs 3.10+`. |
| R7 | `test_stored_remote_entries_equal_fresh_parse(tmp_path, monkeypatch)` | Cold `--remote fakehost` (subprocess), then read the `host = 'remote:fakehost'` rows: there are 13. In-process, with `_read_zstd` set to `_raw_frame_text` and `_zstd_available` to True: every `code` starts with `sha256(SRC/cctrack/__init__.py)` and ends `" " + _remote_cache_params("fakehost", cctrack._parse_env_key())`. A claude/codex payload `_canon`-equals `_parse_log_file(kind, path, source="fakehost", label=f"fakehost:{path}", want_stats=True)`, and none of its events has `_locations`. A hook payload equals `{"events": _hook_rows_from_blob_text("".join(_read_text_lines(path))), "stats": None}`. |
| R8 | `test_remote_named_local_does_not_clash(tmp_path)` | A second fake ssh, `_fake_ssh(tmp_path / "self", {"HOME": str(c.home), "SANDY_HOME": str(c.home / ".sandy"), "PYTHONPATH": str(c.root / "remote-site")})` (the `PYTHONPATH` entry is needed so `remote_zstd="raw"` reaches it), first on `PATH`. `--remote local` cold, then warm with timing. Warm: `claude parse` note `5 reused, 0 read`, `hook read` note `3 reused, 0 read`, remote note `12 reused, 0 read` (5 + 4 + 3: the "remote" is the local home after rollup); `opens == []`; stdout equals `--no-cache`. The db has hosts `local` and `remote:local`, with the same paths under both. |
| R9 | `test_remote_collect_hits_need_rkey_sig_and_decoder(tmp_path, monkeypatch)` | In-process `_remote_collect_files` with HOME and SANDY_HOME set to a backdated corpus's `remote_home`, `CODEX_HOME` unset, `CCTRACK_DIR` patched to `remote_home/.claude/cctrack`, and raw zstd patched in. A cold call gives every item a payload and flag `"s"`. Build the manifest from it. (1) Same rkey: every item is `"h"`, a `_read_log_lines`/`_read_text_lines` wrapper records no reads, and `counts` give 13 reused. (2) `rkey = "other"`: no hits. (3) One Claude path with mtime + 1 in the manifest: exactly that item is a miss. Likewise with size + 1. (4) `_zstd_available` → False: the `.zst` is not a hit, and (with `_read_zstd` → None) `zst_skipped == 1` with no item. |
| R10 | `test_remote_collect_marks_racy_and_changing_files(tmp_path, monkeypatch)` | Same setup. Give one Claude file `mtime = now`: its item is `"n"`. Give another a wrapper on `_read_log_lines` that appends a line and bumps its mtime during the read: `"n"`. Others: `"s"`. |
| R11 | `test_remote_items_are_per_file_and_assemble_to_v1(tmp_path, monkeypatch)` | Same setup (corpus `remote_home`: forks `P`/`C`, a resumed copy, 4 hook files in 2 dirs). The items' events contain `r2` twice (two files); no item event has `_locations` or `_fork_history`. `_assemble_remote_bundle("fakehost", data, None)` equals, by `_canon`: (claude, codex) `_parse_remote_files("fakehost", [(p, _read_log_lines(k, p)) …], parser, stats)` and its stats; (hook) `parse_hook_blob(_remote_hook_blob(discover_hook_data_dirs()))`. `_remote_collect` returns the same. |
| R12 | `test_remote_hook_per_file_equals_blob()` | Port of `$S/s6/hookprop.py`: `random.Random(7)`, 2,000 cases. Same generators and a test-side v1 blob builder. For cases where `_hook_text_breaks_blob` is False for all texts: the per-file v2 path (product `_hook_rows_from_blob_text` + concat + sort + `parse_hook_blob` of a monthly/baseline-only blob) equals `parse_hook_blob(blob)`. At least 100 cases must be skipped by the rule, and at least one skipped case must differ under the naive per-file path (so the rule has teeth). |
| R13 | `test_remote_hook_separator_falls_back(tmp_path, monkeypatch)` | In the corpus `remote_home/.claude/cctrack`, add `statusline-<day 5>.jsonl` with one row whose `session_id` is `"x" + MONTHLY_FILE_SEP`. In-process `_remote_collect_files`: `"hook" not in data["files"]`, and `data["hook"] == parse_hook_blob(_remote_hook_blob(dirs))`. Then a subprocess cold run and a warm run with `--remote fakehost --json`: equal to `--no-cache`; no `remote:fakehost` row of kind `hook`. |
| R14 | `test_hook_line_splitting_kept_per_side(tmp_path, monkeypatch)` | Write a hook file holding one JSON line with a raw U+2028 inside `session_id` (`ensure_ascii=False`). Local: `parse_hook_rows(d)` and `parse_hook_rows(d, cache=store)` (cold and warm) both give 1 row. Remote: `_hook_rows_from_blob_text(text)` gives 0 rows, equal to `parse_hook_blob(HOOK_FILE_SEP + "\n" + text)[0]`. |
| R15 | `test_remote_manifest_selects_current_rows(tmp_path)` | Unit, with an `open_cache()` store. Put rows for `remote:h` with rkey A (3 rows), rkey B (1 row), one row with another code prefix, a `codex` row and a `local` row. `_remote_manifest(store, "h", ("claude", "hook"))` → rkey A, only its claude/hook paths. With `("codex",)` → only the codex row. `_remote_manifest(None, …)` → None. After `store._fail(...)` → None. |
| R16 | `test_remote_timing_lines(tmp_path)` | `CCTRACK_TIMING=1`, `--remote fakehost`. Cold and warm: the remote sub-lines include `on fakehost: encode`, `local: decode`, `local: merge` and `on fakehost: other`; the top-level parts still sum to `total` within 0.02 s; the last two lines are `report`, `total`. `--no-cache`: the remote detail ends `received`. |
| R17 | `test_remote_parse_with_warm_cache_matches_raw_stream(host, monkeypatch, tmp_path)` (in `tests/test_remote_parse.py`) | Backdate the fake host's files. With `store = cctrack.open_cache()`, call `fetch_remote_bundle("dgx", True, True, cache=store)` cold, then `store.flush()`, then clear `_remote_bundle_cache` and call it warm. Each time the `parse_remote_*` results equal the raw stream (`CCTRACK_REMOTE_PARSE=0`) by the existing `_sorted_events`/`_canon` comparisons. Warm: `cache_counts == {"reused": 6, "read": 0}` (4 Claude + 1 Codex + 1 hook), and `_wire_bytes` is smaller than cold. |

**R2 cases.** `RP = remote-home/.claude/projects/-repo`; `t` is the backdate time; `day1 = c.explore_date`. "Remote opens" and "local opens" are the first warm run's.

| Case | Mutation | Remote opens | Local opens | Remote note | Content |
|---|---|---|---|---|---|
| `r1-append` | Append `claude_line(f"{day1}T04:00:00Z", "r7", sid="s1", inp=777)` to `RP/s1-resumed.jsonl`, restore mtime `t` | `[RP/s1-resumed.jsonl]` | `[]` | `12 reused, 1 read` | yes |
| `r2-add` | New `RP/new.jsonl` (`claude_line(…, "rn", sid="sn", inp=999)`), mtime `t` | `[RP/new.jsonl]` | `[]` | `13 reused, 1 read` | yes |
| `r3-fork-child` | Delete the remote `*-C.jsonl` | `[]` | `[]` | `12 reused, 0 read` | yes |
| `r4-same-size` | In `RP/rh.jsonl` replace the first `"input_tokens": 500` with `600` (size unchanged), mtime `t + 5 s` | `[RP/rh.jsonl]` | `[]` | `12 reused, 1 read` | yes |
| `r5-fork-child-added` | Remote `rollout-{day1}T09-00-00-C2.jsonl` next to the remote `P`, contents as step 4's `5-fork-child-added`, mtime `t` | `[that path]` | `[]` | `13 reused, 1 read` | yes |
| `r6-rsync-to-remote` | `shutil.copy2` local `-repo/lh.jsonl` to `RP/lh.jsonl` | `[RP/lh.jsonl]` | `[]` | `13 reused, 1 read` | yes (`--json` locations) |
| `r6b-rsync-to-local` | `shutil.copy2` `RP/rh.jsonl` to local `-repo/rh.jsonl` | `[]` | `[local …/rh.jsonl]` | `13 reused, 0 read` | yes |
| `r-hook-append` | Append a `s1` row (`_ts` `{day1}T05:00:00+00:00`, cost 0.90) to remote `.claude/cctrack/statusline-{day1}.jsonl`, mtime `t + 3 s` | `[that path]` | `[]` | `12 reused, 1 read` | yes |
| `r7-code-hash` | Cached runs use `src2` (a copy of SRC with a comment appended, as in step 4) | all 13 | all 9 | `0 reused, 13 read` | no |
| `r10-truncate` | Drop the last line of `RP/s1.jsonl`, restore mtime `t` | `[RP/s1.jsonl]` | `[]` | `12 reused, 1 read` | yes (`--json`) |

For `r7-code-hash`, (d) expects `13 reused, 0 read` with `src2`.

### 6.2 Local hook tests

| # | Test | Proves |
|---|---|---|
| H1 | `test_local_hook_rows_cached_equal_uncached(tmp_path, monkeypatch)` (in `test_cache_remote.py`) | The dir holds `statusline-2026-11-10.jsonl` (sessions a and b interleaved, one bad line, one blank line, no final newline), `statusline-2026-11-11.jsonl` (a row with the same `(session_id, ts)` as one in the first file but a different cost, plus a later b row) and legacy `statusline.jsonl` (b), all backdated. Wrap `cctrack._read_text_lines` to count calls. Compare `parse_hook_rows(d)` (uncached) with `parse_hook_rows(d, cache=store)`, by `_canon`: cold (3 reads); after `flush`, warm (0 reads); after appending to the 11-10 file with mtime `t + 5 s`, equal to a fresh uncached result (1 read). The db has 3 rows with host `local` and kind `hook`, each code ending `" " + _HOOK_LOCAL_PARAMS`. |
| H2 | `test_warm_run_opens_only_changed_hook_files(tmp_path)` (in `test_cache_local.py`) | Own corpus, backdated. Cold: `hook_opens` equals the 3 surviving local hook files plus the day-120 file (opened by rollup, then deleted). Warm: `hook_opens == []`. Then write `statusline-<today>.jsonl` in the local hook dir (one `s1` row), mtime `t + 10 s`: the first warm run's `hook_opens` is only that file; the next run's is `[]`. Each cached stdout equals `--no-cache` (default report and `--json`). |
| H3 | `test_rolled_up_hook_file_leaves_no_effect(tmp_path, monkeypatch, capsys)` (in `test_cache_local.py`) | In-process corpus home (`_inproc`). Add `statusline-<day 30>.jsonl` (session `r30`, costs 0.0, 1.0, 2.5), backdated. Run `main()` twice (cold, warm; `sys.argv = ["cctrack"]`). Then `monkeypatch.setattr(cctrack, "rollup_old_hook_files", lambda d, retention_days=10, _r=cctrack.rollup_old_hook_files: _r(d, retention_days))`. A cached `main()` captures `a`, and the file is now gone while its row is still in the db. Then `main()` with `--no-cache` captures `b`. `a == b`. Also, with `--explore <day 30>`, cached equals `--no-cache`. |
| H4 | `test_timing_reports_hook_cache_use(tmp_path)` (in `test_cache_local.py`) | Own corpus, backdated, `CCTRACK_TIMING=1`. The `hook read` detail ends `; cache: 0 reused, 3 read` cold and `; cache: 3 reused, 0 read` warm. With `--no-cache` there is no `cache:` in it. |

### 6.3 Brief criteria → tests

| Criterion | Proof |
|---|---|
| Local hooks: hook totals, `hook_cost_usd` fields and `--coverage` hook findings identical cached and uncached | R1 and T1 (`report`, `explore`, `json` (`hook_cost_usd`), `detail` and `csv-coverage` across 4 cache modes, local and remote), H1 (rows) |
| Local hooks: a file removed by rollup leaves no effect | H3, plus the corpus's day-120 file in every T1/R1 run |
| Local hooks: only today's hook file is opened on a warm local run | H2 |
| `tests/test_remote_parse.py`: remote parse still equals the raw stream; with a warm cache still equal | existing `test_remote_parse_matches_raw_stream_exactly` (now v2), R17 |
| Section 5 equivalence with `--remote` | R1 (4 variants × 8 modes × 5 runs), T1 (unchanged; its remote variants are now cached) |
| … including mutation 6 (rsynced copy) | R2 `r6-rsync-to-remote`, `r6b-rsync-to-local` |
| … and a fork child added on the remote | R2 `r5-fork-child-added` |
| Warm run: remote opens only changed files; the response reports how many it parsed | R1 (`remote_opens == []`, note `N reused, 0 read`), R2 (a), R9 (1) |
| The response is smaller than the cold one | R17 (`_wire_bytes`), finding 3 |
| Remote host unmodified (listing with mtimes) | R3, R2 (e) |
| Hit the cache cannot supply → one automatic full refetch, correct result | R4 |
| Unreachable host: same warning and same report as today | R5 |
| Fallback (`CCTRACK_REMOTE_PARSE=0`, old Python) unchanged and uncached | R6, existing `test_old_python_on_host_falls_back_to_raw_stream`, `test_timing_remote_raw_and_failed` |
| Added: per-file, never deduped remotely; stored entries equal a fresh parse | R7, R11 |
| Added: rkey, sig, `.zst` and racy rules on the remote clock | R9, R10 |
| Added: remote hook semantics exact; separator fallback | R11, R12, R13, R14 |
| Added: host named `local` | R8 |
| Added: manifest selection | R15 |
| Added: timing split for F10's "other" | R16 |

### 6.4 Changes to existing tests (none weakened)

- `test_cache_local.py::_entry_paths` selects `path FROM entries WHERE host = 'local' AND kind IN ('claude', 'codex')`. In T4, also assert that after `--rebuild-cache` the local hook rows are exactly the 3 current local hook files.
- T9 becomes `test_remote_runs_store_remote_rows_apart(tmp_path)`. Every run in it uses `remote_zstd="raw"`; without that, whether the remote `.zst` is stored would depend on the machine. After a cold `--remote fakehost`:
  - `SELECT DISTINCT host` is `{"local", "remote:fakehost"}`;
  - `local` has 9 claude/codex rows, all under `c.home`, plus 3 hook rows;
  - `remote:fakehost` has 13 rows, all under `c.remote_home`;
  - `c.remote_home / ".cache"` does not exist;
  - warm `--json` equals `--no-cache --json`, with `opens == []` and `remote_opens == []`.
- T14 counts `WHERE kind = 'claude'` (still 5), and asserts that the `hook` rows are 3.
- `test_cache_equivalence.py`: corpus additions, `REMOTE_SITE`, `Launched` and `run_launched` extensions (4.8). `test_corpus_covers_section5_cases` gains: the remote has a legacy `statusline.jsonl` and a sandbox hook dir.

### 6.5 Mutation checks

Run each on a copy: `rm -rf $S/m6 && cp -r src $S/m6 && <edit> && PYTHONPATH=$S/m6 python3 -m pytest tests -q -x -k '<tests>'`. The subprocess helpers follow `PYTHONPATH` through `SRC`, and the program shipped to the fake remote is the mutated file, so remote mutations take effect.

| # | Change | Must fail |
|---|---|---|
| MB1 (brief) | Remote dedups across files: in `_remote_collect_files`, just before `return out`, add `for k in ("claude", "codex"): dedup_events([it[4]["events"] for it in out["files"].get(k, []) if len(it) > 4])` | R7 (`_locations` in a stored payload), R11, R2 `r3-fork-child` |
| MB2 (brief) | Remote ignores the manifest's mtime: in `_remote_file_item` compare only `known.get(path, [None])[0] == sig[0]` | R2 `r4-same-size` (refetch note, 2 calls), R9 (3, mtime) |
| M3 | Local stores remote misses after dedup: in `_assemble_remote_bundle`, collect the `put` arguments and run them after that kind's `dedup_events` | R7, R2 `r3-fork-child` |
| M4 | No refetch: in `_assemble_remote_bundle`, skip a hit that `get` can't supply (`continue`) instead of returning None | R4 |
| M5 | No host prefix: `_remote_host_key` returns `host` | R8 |
| M6 | Remote ignores rkey: use the manifest whatever its `rkey` | R9 (2) |
| M7 | Remote `.zst` hit without a decoder: drop the `_zstd_available()` clause | R9 (4) |
| M8 | Remote racy rule dropped: always flag `"s"` | R10 |
| M9 | No hook fallback: `_hook_text_breaks_blob` returns False | R13 |
| M10 | Local cached hooks skip the final sort in `parse_hook_rows` | H1 |
| M11 | Remote hook assembly skips the final sort | R11 (hook), R1 or T1 (remote reports) |
| M12 | Local cached hooks use `_hook_rows_from_blob_text` | R14 (local), H1 |
| M13 | Remote hook items use `_hook_rows_from_lines(lines)` (readlines) | R14 (remote), R12 |
| M14 | In `main`, call `parse_hook_rows` before `rollup_old_hook_files` | H3 |
| M15 | `main` passes no cache to `parse_hook_rows` | H2, H4 |
| M16 | `main` passes no cache to `fetch_remote_bundle` | R1 (warm `remote_opens`), T9 |
| M17 | `_remote_manifest` ignores `kinds` | R15 |

The step 2–4 mutation checks still apply.

## 7. Verification

Never leave a real `~/.cache/cctrack`: every manual run sets `CCTRACK_CACHE_DIR` or `HOME` to a scratch place. Fake ssh scripts must not live in `/dev/shm` (noexec, finding 7).

1. **Suite and diff.**
   ```bash
   cd /home/sandy/dev/cctrack
   ls -d ~/.cache/cctrack 2>/dev/null                       # must print nothing (before)
   PYTHONPATH=src python3 -m pytest tests -q                 # 485 + new passed, 1 skipped
   PYTHONPATH=src python3 -m pytest tests/test_cache_remote.py tests/test_remote_parse.py tests/test_cache_local.py -q --durations=8
   ls -d ~/.cache/cctrack 2>/dev/null                       # must print nothing (after)
   git diff --stat 84f3835 -- src tests scripts CLAUDE.md README.md docs
   git diff 84f3835 -- tests | grep '^-[^-]'                 # only the T4/T9/T14 lines of 6.4; no test removed
   ```

2. **Old vs new over the whole matrix.**
   ```bash
   S=/tmp/claude-501/-home-sandy-dev-cctrack/b90020de-1841-4523-9564-a7f61b1fabce/scratchpad
   rm -rf $S/old06 && mkdir -p $S/old06 && git -C /home/sandy/dev/cctrack archive 84f3835 src | tar -x -C $S/old06
   python3 $S/s4/compare4.py $S/old06/src /home/sandy/dev/cctrack/src
   ```
   Expected: `entries: 56 cache file: True differences: {'new cache off': [], 'new cold': [], 'new warm': []}`, exit 0. The new tree's "warm" pass now serves the remote variants from the cache. The corpus comes from the new `tests/`, so the extra hook files are included.

3. **Live smoke with a fake remote on this sandbox's real logs.** Run `bash $S/s6/live_remote.sh /home/sandy/dev/cctrack/src`. Expected:
   - every line `same`;
   - `remote host unchanged`;
   - on the two timing runs, the `remote fakehost` line shows `; cache: N reused, M read`, with `M` 0 or 1 (1 if the copied active transcript or today's hook file is racy), and a `received` figure well below the `90.5 KB` that HEAD shipped on every run (finding 8);
   - `hook read` shows `; cache: …`;
   - sub-lines `on fakehost: encode`, `local: decode`, `local: merge`;
   - no `WARNING`;
   - exit 0.

4. **CLI smoke** (scratch cache and fake host from the test corpus):
   ```bash
   export CCTRACK_CACHE_DIR=/dev/shm/smoke6-cache
   PYTHONPATH=src python3 -m cctrack --help | grep -E -- '--no-cache|--rebuild-cache'
   CCTRACK_TIMING=1 PYTHONPATH=src python3 -m cctrack >/dev/null 2>$S/s6/t1.txt; grep -E 'hook read|cache' $S/s6/t1.txt
   CCTRACK_TIMING=1 PYTHONPATH=src python3 -m cctrack 2>&1 >/dev/null | grep 'hook read'   # "N reused"
   rm -rf /dev/shm/smoke6-cache; unset CCTRACK_CACHE_DIR
   ```

5. **F10 decode figures** (informational): `python3 $S/s6/real_reply.py && python3 $S/s6/f10_decode.py 200000 300000; rm -f /dev/shm/s6_real.pkl`. Expect about 28 MB and a local decode of about 2 s on this sandbox (finding 1). The script uses pickle only to pass scratch data between the two scratch scripts, never in product code.

6. **Bench, remote** (informational, no gate in the brief):
   ```bash
   python3 scripts/bench_cache.py --dir $S/bench6 --claude-files 60 --codex-files 10 --remote --runs 3 --json > $S/s6/bench_new.json
   python3 -c "import json; r=json.load(open('$S/s6/bench_new.json'))['runs']; print([(round(x['phases']['remote benchhost'],3), round(x['phases']['hook read'],3)) for x in r]); assert len({x['stdout_sha256'] for x in r})==1"
   rm -rf $S/bench6
   ```
   HEAD gave `remote benchhost` 0.566 / 0.461 / 0.440 s (finding 9). After this step, report the warm figures. The planner expects warm well below cold, since the remote reads no files. Report the numbers rather than tuning code.

7. **Mutations.** Run MB1, MB2 and M3–M17 (6.5) and confirm that each named test fails.

## 8. Deviations

- **MB2 is mapped to mutation 4 on the remote, not mutation 1.**
  - The brief says "ignore the manifest's mtime → mutation 1 on the remote must fail". An append changes the size, so a size-only check still catches it.
  - The mutation that ignoring mtime misses is a same-size rewrite with a new mtime (mutation 4).
  - And because the hit marker carries the current stat and local `get` re-checks it, the mutation cannot produce a wrong report. It produces a refetch, which R2 `r4-same-size` and R9 detect (one ssh call, no note).
- **Plan section 4.6 edited:**
  - host key `remote:<name>`;
  - rkey;
  - hit marker with the current stat;
  - the remote's storable flag;
  - the hook items and their separator fallback;
  - local dedup and hook sort;
  - the source guard.
- **Plan section 4.4 edited:** the `host` column is `local` or `remote:<name>`.
- **F10 readings line edited:** finding 1's answer.
- **Existing tests T4, T9 and T14 changed** (6.4). Their premises ("remote isn't cached", "no hook rows") were step 4 facts that this step retires. Each assertion is kept for what it meant (local log rows; local rows under the local home; Claude rows kept on interrupt) and more is asserted. Nothing is deleted.
- **Corpus extended:** a legacy `statusline.jsonl` and a sandbox hook dir, so the remote's single sort across folders and the local per-dir differencing are both exercised. Existing harness outputs change with the corpus, but every harness comparison is between runs on the same corpus.
- **Added beyond the brief:**
  - timing sub-lines (`on <host>: encode`, `local: decode`, `local: merge`), the `remote <host>` cache note and the `hook read` cache note, so F10's "other" can be split on the real machines;
  - the source guard;
  - R7–R17, H1–H4;
  - the fake remote's `sitecustomize` (opens log, deterministic `.zst`).
- **`_remote_collect` keeps its v1 shape** as a wrapper over the new `_remote_collect_files` plus local assembly (finding 4). This keeps step 2's seam tests meaningful.

## 9. Out of scope

- D1's alternative (use stale cached data for an unreachable host): left for Dan.
- Remote progress text (it still says `i/n files`, without "changed"), README polish, `--help` text and version 0.12.0: Step 7.
- Byte-offset resume of growing transcripts, including dgx's 3.5 GB of transcripts that change daily: Step 8. Its 2.6 GB Codex rollout stays a full re-read on the host for good.
- Caching the raw-stream fallback: never (plan section 2).
- `--remote local` sharing the source name `local` with this machine in reports: this is already so today and is not changed here. Only the cache keys are kept apart.
- Fixing F4's local/remote differences, and step 5 finding 5's hook differences: never, in this work.
