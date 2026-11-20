# Step 03. Cache store, standalone

Plan for `### Step 3` of `docs/plans/incremental-cache.md`. Written against commit `64725e3` (step 02 done; 403 passed, 1 skipped, about 16 s, Python 3.12.15).

No product decision is needed, and none of the section 7 decisions is needed. This step settles the brief's one "Planner decides" item, the payload encoding: **marshal + zlib** (finding 3). Sections 4.1, 4.3 and 4.4 of the plan were edited to record that choice and three additions to the design (section 8).

## 1. Outcome

- `src/cctrack/__init__.py` gains a self-contained parse cache that no report path calls yet:
  - `open_cache()` opens or creates one SQLite file under `cache_dir()`. On any problem it returns `None` and prints one note to stderr.
  - `CacheStore.get/put/flush/close` read and write entries.
  - `_encode_cache_payload`/`_decode_cache_payload` turn a per-file `{events, stats}` into a blob and back.
  - `_parse_log_file_cached()` wraps the step 2 seam `_parse_log_file`. It applies the 4.1 rules (hit on an unchanged `(size, mtime_ns)` and the same code key; racy and changed-during-read files are parsed but not stored; failures are never stored).
- `sqlite3` is imported only inside `open_cache`. Importing cctrack never loads it, and a Python without it still works.
- `tests/conftest.py` points `CCTRACK_CACHE_DIR` at a per-test temp directory.
- New tests in `tests/test_cache_store.py` cover the brief's criteria:
  - the round trip over the parity cases and the section 5 corpus;
  - the hit/miss table;
  - robustness;
  - the F6 identity proof.
- Decoding 1M realistic events takes about 2.0 s on this machine with GC on (budget 3 s). Tagged JSON took 5.3 s.
- No report, CLI option or stdout changes. Step 4 wires the cache in.

## 2. Findings

All checks ran in this sandbox: Linux aarch64, 4 cores, 16 GB RAM, Python 3.12.15, SQLite 3.53.1. Some checks also used Python 3.14.10 from `$S/uvpy`, which has `compression.zstd`. There is no `zstd` CLI. `/tmp` is a 1 GB tmpfs that is nearly full (finding 14). The prototype scripts are in `$S/s3/`, where `S=/tmp/claude-501/-home-sandy-dev-cctrack/b90020de-1841-4523-9564-a7f61b1fabce/scratchpad`.

1. **Baseline.** `PYTHONPATH=src python3 -m pytest tests -q` gives 403 passed, 1 skipped (`test_zstd_raw_frame_decodes`, which needs zstd).

2. **What per-file payloads contain** (`$S/types.py`, `$S/sharing.py`). I parsed this sandbox's real logs (11 files, 21.5 MB, 1,034 events, about 48 events/MB), both step 0 corpus homes and every `ccusage-parity.json` case through `_parse_log_file(..., want_stats=True)`.
   - **Types.** Events hold `str`, `int`, `float`, `bool` and `None`, plus lists, dicts and tuples (`_raw_usage`, and the items of `_snapshot_aliases`). Stats hold datetimes (`first_ts`, `last_ts`) and tuple dict keys (`compactions`). There are no sets.
   - **No shared containers.** No mutable container appears twice inside one file's payload (46 files checked). So an encoding that loses object identity loses nothing inside a payload.
   - **No `_fork_history`.** Per-file payloads never contain `_fork_history`. Only cross-file dedup adds it (F3).

   F10 sizes, at 48 events/MB: the Mac's 5.8 GB of Claude logs come to about 280k events. 1M events is about three times the Mac's Claude total, so the budget has headroom.

3. **Payload encoding: marshal + zlib.** Measured with `$S/bench_decode.py` and `$S/bench_gc.py`. The events were real parsed events (80 % Claude, 20 % Codex), deep-copied with unique ids into 2,500 files × 400 = 1M events. Times are best of 3.

   | Encoding | Encode | Decode, GC on | Decode, GC off | SQLite fetch + decode | Size |
   |---|---|---|---|---|---|
   | tagged JSON + zlib (`_tag_json`/`_untag_json`) | 6.5 s | **5.3–6.0 s** | 3.9 s | 5.4 s | 67 MB |
   | tagged JSON + zlib, `json.loads(object_hook=…)` | 6.3 s | 2.9–3.9 s | 2.1 s | 3.4 s | 67 MB |
   | **marshal + zlib level 1** | 1.8 s | **2.0–2.1 s** | 1.0 s | 1.8 s | 66 MB |
   | marshal, uncompressed | 0.8 s | 1.6 s | 0.55 s | 1.5 s | 465 MB |

   - Tagged JSON misses the 3 s budget by about 2×. F9's 0.56 s per 200k events was measured on tiny synthetic events. The `object_hook` variant is borderline and fails once the SQLite fetch is included.
   - zlib level: 1, 3 and 6 decompress equally fast (about 0.1 s per 200k events). Compression costs 1.0, 1.2 and 2.7 s per 1M events, and the output is 13.2, 12.5 and 11.6 MB per 200k. Level 1 wins.
   - Of the marshal + zlib decode time, `zlib.decompress` is 0.49 s. `marshal.loads` is 0.55 s with GC off and 1.59 s with it on, so cyclic GC is most of the difference (finding 13).

4. **Is marshal exact?**
   - **Version.** `marshal.version` is 4 on 3.12 and 5 on 3.14. A 3.12 blob happened to load on 3.14, but the format is not promised across minor versions. The key names the interpreter (finding 9).
   - **Supported types** (checked by experiment). marshal **rejects** subclasses with `ValueError: unmarshallable object`: `defaultdict`, `OrderedDict` and a `str` subclass all fail. It keeps `bool`, `-0.0`, NaN, big ints, tuples, tuple dict keys, `frozenset` and dict order. The one silent coercion is `bytearray` → `bytes`, which does not occur in payloads. A string with lone surrogates survives.
   - **Datetimes.** marshal cannot hold `datetime`, so the encoder turns `stats["first_ts"]`/`stats["last_ts"]` into `isoformat()` strings and the decoder restores them with `datetime.fromisoformat`. `_tag_json` already does the same, and remote v1 relies on it.
   - **Shared lists.** marshal keeps object sharing: two events sharing one `_fork_history` list still share it after `loads`. Tagged JSON does not.

5. **Round trip is exact** (`$S/s3/roundtrip.py`, which uses `$S/s3/proto_store.py`, a prototype encoder that matches section 4). The check covers every file from finding 2, grouped by origin (each home's Claude files, each home's Codex files, each parity case). For each file I compared `parse` with `decode(encode(parse))`, and for each group `dedup_events` over the originals with `dedup_events` over the decoded lists. Comparison was strict, with type identity and dict key order at every level.
   - Python 3.12: 25 groups, 46 files, 1,112 events, all equal. The two `.zst` files are unreadable here and were skipped.
   - Python 3.14 (`PYTHONPATH=<3.12 site-packages>` for pytest): 48 files including both `.zst` rollouts, all equal.

6. **F6 identity proof** (`$S/s3/f6.py`). I used the nested-fork fixture from `test_accounting.py`, with one extra post-fork record in the child so that two surviving events share one `_fork_history` list. `local = dedup_events([parent, child])` gives 2 events with `_fork_history` and **1** distinct list object. I then fed `dedup_events([x, grandchild])` with `x` in three forms:
   - `local` round-tripped through marshal (sharing kept),
   - `local` deep-copied per event (sharing broken, 2 objects),
   - `local` round-tripped through `_untag_json(_tag_json(...))` (sharing broken).

   All three gave canonical output identical to the reference, with an input sum of 1,280 each. Losing identity is harmless by value, as F6 claims.

7. **Corrupt blobs** (`$S/s3/fuzz.py`).
   - **Through zlib.** I made 20,000 random 1–3 bit flips in a real 57 KB marshal+zlib blob. Every one failed to decode (zlib's adler32 and deflate structure). A truncated blob also failed.
   - **Raw marshal.** 20,000 random byte mutations or truncations of the raw marshal bytes gave `ValueError` 10,828 times, `EOFError` 3,904, `TypeError` 350 and `UnicodeDecodeError` 17. **4,901 loaded successfully as different values.**
   - **Result.** No mutation crashed the interpreter. zlib is what turns corruption into a miss. Uncompressed marshal would silently serve wrong numbers. Dead end: do not drop zlib for speed.
   - **Rule.** Decode catches every `Exception` and checks the shape: a dict with list `events` of dicts and `stats` a dict or `None`.

8. **SQLite behaviour** (`$S/s3/sqlite_exp.py`, `$S/s3/sqlite_exp2.py`).
   - **Garbage file.** `connect` succeeds lazily. The first statement raises `sqlite3.DatabaseError: file is not a database`. The exception's class is exactly `DatabaseError`, not a subclass.
   - **Damaged pages behind a valid header.** I overwrote the pages behind the header with `0xff`. `PRAGMA user_version` still worked, and so did some point lookups. A table scan raised `DatabaseError: database disk image is malformed`. `PRAGMA quick_check` reported the damage (`invalid page number …`) at all three offsets tried, in 1–2 ms on a 14 MB file. On the planned rowid table (200 rows of 3,000 bytes, bytes `[8192, 8192*20)` overwritten), `user_version` still read 1, and `quick_check` itself **raised** `DatabaseError: database disk image is malformed`. So `quick_check` can either return error rows or raise, and both must count as corrupt. I ran the planned open sequence without `quick_check` (read `user_version`, `BEGIN IMMEDIATE`, `CREATE TABLE IF NOT EXISTS`, write `user_version`, prune `DELETE`, `COMMIT`) against 204 copies of that file, each with one page overwritten with `0xff`. The prune scan caught damage on 201 pages. The other 3 were the primary-key index pages, including its root page (`rootpage` of `sqlite_autoindex_entries_1` in `sqlite_master`), and only `quick_check` caught them. A damaged index can mislead point lookups, so `quick_check` stays. On a 73 MB healthy file it took 6 ms (page cache).
   - **Modes.** Created by `sqlite3.connect` under umask 022, the database is **0644**. Pre-created with `os.open(path, O_RDWR|O_CREAT, 0o600)`, it stays 0600, and SQLite gives its `-journal` (and `-wal`/`-shm`) the same 0600.
   - **Read-only directory, existing database.** Reads work. Any write raises `OperationalError: attempt to write a readonly database` (`SQLITE_READONLY_DIRECTORY`), even `PRAGMA user_version = <same value>`. `BEGIN IMMEDIATE` alone succeeds, so it is not a write probe. Creating a new database there raises `OperationalError: unable to open database file`.
   - **Locked database.** Another connection in the same process holds `BEGIN EXCLUSIVE`. A read then raises `OperationalError: database is locked` after the busy timeout (0.33 s with `timeout=0.3`). While the other connection holds only `BEGIN IMMEDIATE`, reads work, and a second writer gets `locked` after the timeout.
   - **Exception classes.** `OperationalError` is a subclass of `DatabaseError`. Corruption (`NOTADB`, `CORRUPT`) raises exactly `DatabaseError`. Lock, read-only and I/O problems raise `OperationalError`.
   - **Write costs.** 2,500 rows of 26 KB (73 MB) written in 9 batched commits took 0.036 s on tmpfs. 2,500 point lookups took 0.059 s. Updating `last_used` on 2,500 rows in one transaction took 0.11 s, and a prune scan 0.007 s.

9. **The code key must name more than the source** (section 8).
   - **Text encoding.** The `open()` default encoding changes the parse. A Claude line whose `cwd` is `/w/café`, parsed with `LC_ALL=C.UTF-8`, gives `'/w/caf\xe9'`. Parsed with `LC_ALL=C PYTHONCOERCECLOCALE=0 PYTHONUTF8=0`, it gives `'/w/caf\ufffd\ufffd'`. `codecs.lookup(io.TextIOWrapper(io.BytesIO()).encoding).name` gives `utf-8` and `ascii` in those two environments, which is exactly what `open()` uses.
   - **Interpreter.** marshal's format varies by interpreter (finding 4).
   - **Parse parameters.** The parse also depends on `source`, `label` and `agent_id_from_name`. Step 2 made these parameters, so they go into the key too.

10. **Concurrency** (`$S/s3/concurrency.py`). Three processes simulated cold runs at the same time on one database: 2,500 lookups each, 26 KB puts, 8 MB batches, each batch in one `BEGIN IMMEDIATE` transaction. They got **0 errors** with a 1.0 s busy timeout and also with 0.1 s. This works because no transaction is held while parsing. Dead end: Python's implicit transactions open a write transaction at the first `INSERT` and hold it until `commit()`. With per-file `INSERT`s, one run would hold the write lock for its whole cold read. So use `isolation_level=None` with an explicit `BEGIN IMMEDIATE … COMMIT` per flush.

11. **Non-UTF-8 file names.** `os.fsdecode(b"/x/caf\xe9.jsonl")` gives `'/x/caf\udce9.jsonl'`. Binding that to SQLite raises `UnicodeEncodeError`, which is not a `sqlite3.Error`. A plain `except sqlite3.Error` would let it crash a report. marshal keeps such strings, so only the key columns are affected. Rule: bypass the store for a path, label or host that does not encode as UTF-8.

12. **`.zst` hits and decoders.** Uncached, a `.zst` rollout that cannot be decoded (no `compression.zstd`, no `zstd` CLI) yields nothing and is counted as skipped. If an entry stored while a decoder existed were served after the decoder went away, the cached report would differ from the uncached one. Rule: a `.zst` lookup happens only when `_zstd_available()` says a decoder exists. It mirrors `_read_zstd`'s checks, so the existing `patch.dict(sys.modules, {"compression": None})` + `patch("shutil.which", …)` test pattern drives it.

13. **For Step 4, not this step: GC.** Bulk decoding allocates millions of containers, and cyclic GC roughly doubles `marshal.loads` time (finding 3). Step 4's planner may want to measure `gc.disable()`/`gc.freeze()` around the warm load. This step's budget is met with GC on.

14. **Sandbox disk.** `/tmp` (which holds `$S`) is a 1 GB tmpfs with about 350 MB free. A first 1M-event run that wrote four SQLite files under `/tmp` filled it. Put any large temporary database in `/dev/shm` (7.9 GB). The scripts in section 7 do.

## 3. Files

| File | Change |
|---|---|
| `src/cctrack/__init__.py` | New section "Parse cache" placed directly after `_log_kind` (before `parse_events`): constants, `cache_dir`, `cache_code_key`, `_cache_params`, `_encode_cache_payload`, `_decode_cache_payload`, `_file_sig`, `_is_racy`, `_zstd_available`, `_key_text_ok`, `_cache_note`, `CacheStore`, `open_cache`, `_parse_log_file_cached`. `hashlib`, `marshal`, `zlib`, `io`, `codecs` and `sqlite3` are imported inside the functions that use them, as the module already does for `zlib`. No existing function changes. |
| `tests/conftest.py` | In the autouse fixture, `monkeypatch.setenv("CCTRACK_CACHE_DIR", str(tmp_path / "cctrack-cache"))`, plus a docstring sentence. |
| `tests/test_cache_store.py` (new) | Tests in section 6. |
| `CLAUDE.md` | One architecture entry (suggested text in section 4, end). No README change: nothing is user-visible yet. |

## 4. Interfaces

### Constants

```python
CACHE_SCHEMA = 1                     # PRAGMA user_version
CACHE_FILENAME = "cache.sqlite3"
CACHE_BUSY_TIMEOUT = 1.0             # seconds; tests patch it smaller
CACHE_RACY_NS = 2_000_000_000        # 2 s, plan 4.1
CACHE_PRUNE_SECONDS = 30 * 86400
CACHE_FLUSH_BYTES = 8 << 20          # flush pending rows at 8 MiB of blobs ...
CACHE_FLUSH_SECONDS = 2.0            # ... or 2 s after the last flush
CACHE_ZLIB_LEVEL = 1
```
Code reads them as module globals at call time, so `monkeypatch.setattr(cctrack, "CACHE_BUSY_TIMEOUT", 0.05)` works.

### Location and key

```python
def cache_dir() -> Path:
    """$CCTRACK_CACHE_DIR (expanduser) if non-empty; else $XDG_CACHE_HOME/cctrack if
    XDG_CACHE_HOME is an absolute path (XDG spec: ignore relative); else ~/.cache/cctrack."""

def cache_code_key() -> str:
    """Everything besides a file's bytes that decides its parse and its payload format:
    f"{sha256(Path(__file__).read_bytes()).hexdigest()} {sys.implementation.name}-"
    f"{'.'.join(map(str, sys.version_info[:3]))} marshal{marshal.version} "
    f"{codecs.lookup(io.TextIOWrapper(io.BytesIO()).encoding).name}"
    Raises OSError if the source can't be read (open_cache turns that into a note)."""

def _cache_params(source: str, label: str, path: str, agent_id_from_name: bool) -> str:
    """json.dumps([source, None if label == path else label, bool(agent_id_from_name)])"""
```
The stored `code` column is `f"{store.code_key} {params}"`. Compute `cache_code_key()` once in `open_cache`, keep it on the store, and look the function up by global name so tests can patch it. Do not memoise it at module level: tests patch it per test.

### Payload encoding

```python
def _encode_cache_payload(payload: dict) -> bytes | None:
    """payload = {"events": list[dict], "stats": dict | None}. Returns
    zlib.compress(marshal.dumps({"events": events, "stats": st}), CACHE_ZLIB_LEVEL),
    where st is a SHALLOW COPY of stats with first_ts/last_ts replaced by
    .isoformat() when they are datetimes (the caller's dict is not modified).
    None if marshal rejects any object (ValueError) — never raises for that."""

def _decode_cache_payload(blob: bytes) -> dict | None:
    """Inverse. None on ANY exception (zlib.error, EOFError, ValueError, TypeError,
    UnicodeDecodeError, KeyError, ...) or wrong shape: the result must be a dict with
    exactly the keys "events" and "stats"; events a list whose items are all dicts;
    stats None or a dict. first_ts/last_ts, when str, go back through
    datetime.fromisoformat."""
```

### File signature and racy rule

```python
def _file_sig(path: str) -> tuple[int, int] | None:
    """(st_size, st_mtime_ns) from os.stat; None on OSError."""

def _is_racy(sig: tuple[int, int], now_ns: int) -> bool:
    """True if the mtime is within CACHE_RACY_NS of now, or in the future:
    sig[1] > now_ns - CACHE_RACY_NS."""

def _zstd_available() -> bool:
    """Mirror of _read_zstd's decoder choice: True if `from compression import zstd`
    succeeds, else shutil.which("zstd") is not None."""

def _key_text_ok(*texts: str) -> bool:
    """Every text encodes as strict UTF-8 (False for lone surrogates, finding 11)."""

def _cache_note(msg: str) -> None:
    """print(f"cctrack: cache {msg}", file=sys.stderr)."""
```

### Store

```python
class CacheStore:
    path: Path                # the database file
    code_key: str
    enabled: bool             # False after the first error; then get -> None, put/flush no-ops
    counts: dict              # {"hits", "misses", "stored", "bad", "unstorable"}: ints

    def get(self, host: str, kind: str, path: str, sig: tuple[int, int],
            params: str = "") -> dict | None: ...
    def put(self, host: str, kind: str, path: str, sig: tuple[int, int],
            payload: dict, params: str = "") -> None: ...
    def flush(self) -> None: ...
    def close(self) -> None: ...
    def __enter__(self) -> "CacheStore": ...
    def __exit__(self, *exc) -> None: ...   # close(); never suppresses exceptions

def open_cache(*, rebuild: bool = False) -> CacheStore | None: ...
```
Private state, named so tests can reach it: `_con` (the `sqlite3.Connection`), `_pending` (row tuples), `_pending_bytes`, `_touched` (keys), `_last_flush` (`time.monotonic()`), `_noted` and `_reset_on_close`. Construct the store as `CacheStore(con, db, code_key, noted=...)`. T18 swaps `_con` for a stub with `execute`, `executemany` and `close`.

Table (an ordinary rowid table, not `WITHOUT ROWID`, because the payloads are large):
```sql
CREATE TABLE IF NOT EXISTS entries (
  host TEXT NOT NULL, kind TEXT NOT NULL, path TEXT NOT NULL,
  size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, code TEXT NOT NULL,
  payload BLOB NOT NULL, stored_at INTEGER NOT NULL, last_used INTEGER NOT NULL,
  PRIMARY KEY (host, kind, path))
```
`stored_at` and `last_used` are whole seconds since the epoch (`int(time.time())`).

**`open_cache(rebuild=False)`**, in order. Every failure returns `None` after exactly one `_cache_note`.

1. `import sqlite3` inside a `try`. On `ImportError`, note `unavailable (sqlite3 module missing); reading all log files`.
2. Compute `code_key = cache_code_key()`. On `OSError`, note.
3. `d = cache_dir(); d.mkdir(mode=0o700, parents=True, exist_ok=True)`. Never `chmod` an existing directory. On `OSError`, note `unavailable (<d>: <error>); …`.
4. `db = d / CACHE_FILENAME`. If `rebuild`, unlink `db` and its `-journal`, `-wal` and `-shm` siblings (`missing_ok=True`). On `OSError`, note.
5. `_connect(db)`:
   - pre-create the file with `os.close(os.open(db, os.O_RDWR | os.O_CREAT, 0o600))`;
   - `con = sqlite3.connect(str(db), timeout=CACHE_BUSY_TIMEOUT, isolation_level=None)`;
   - `PRAGMA synchronous=NORMAL`;
   - `rows = con.execute("PRAGMA quick_check").fetchall()`. Anything but `[("ok",)]` counts as corrupt, and so does `quick_check` raising `DatabaseError` (finding 8);
   - `BEGIN IMMEDIATE`. Read `PRAGMA user_version`. If it is 0, `CREATE TABLE IF NOT EXISTS …`. If it is neither 0 nor `CACHE_SCHEMA`, treat the store as corrupt;
   - `PRAGMA user_version = CACHE_SCHEMA`. This always writes, so it is the write probe (finding 8);
   - `DELETE FROM entries WHERE last_used < now - CACHE_PRUNE_SECONDS`;
   - `COMMIT`.
6. If step 5 raises exactly `sqlite3.DatabaseError` (`type(e) is sqlite3.DatabaseError`, which means corruption) or finds the store corrupt:
   - close the connection and unlink the four files;
   - run step 5 once more;
   - if that succeeds, note `was unreadable and has been reset (<db>)` and return the store;
   - if it fails, note `unavailable (<error>); reading all log files` and return `None`.
7. Any other exception in step 5 (`OperationalError` for locked, read-only or I/O; `OSError` from `os.open`; anything else) → close if open, note `unavailable (<db>: <error>); reading all log files`, return `None`.
8. Return `CacheStore(con, db, code_key, noted=<whether step 6 printed a note>)`. A store prints at most one note in its lifetime, and a note already printed at open counts.

**`get(host, kind, path, sig, params="")`**:
- If disabled, return `None`.
- Run `SELECT size, mtime_ns, code, payload FROM entries WHERE host=? AND kind=? AND path=?`. No row → `None`.
- Compare `size == sig[0]`, `mtime_ns == sig[1]` and `code == f"{self.code_key} {params}"` **in Python**. This guards against a damaged index returning the wrong row. Any mismatch → `None`.
- `p = _decode_cache_payload(payload)`. If it is `None`, `counts["bad"] += 1` and return `None`. There is no note, and no disabling.
- On a hit, `counts["hits"] += 1`, append `(host, kind, path)` to the touched list, and return `p`. `p` is a fresh object graph each call.
- On any exception: `self._fail(e)` and return `None`.

**`put(host, kind, path, sig, payload, params="")`**:
- If disabled, return.
- `blob = _encode_cache_payload(payload)` **now** (F3: cross-file dedup will mutate these events after this call). If it is `None`, `counts["unstorable"] += 1` and return.
- Append the row tuple `(host, kind, path, sig[0], sig[1], f"{code_key} {params}", blob, now, now)` to `self._pending`. Never keep a reference to `payload`.
- If the pending blob bytes reach `CACHE_FLUSH_BYTES`, or `time.monotonic() - self._last_flush >= CACHE_FLUSH_SECONDS`, call `flush()`.
- On any exception: `_fail(e)`.

**`flush()`**:
- If disabled, or nothing is pending or touched, return.
- In one transaction:
  - `BEGIN IMMEDIATE`;
  - `executemany("INSERT OR REPLACE INTO entries VALUES (?,?,?,?,?,?,?,?,?)", pending)`;
  - `executemany("UPDATE entries SET last_used=? WHERE host=? AND kind=? AND path=?", touched)`;
  - `COMMIT`.
- Then `counts["stored"] += len(pending)`, clear both lists, and reset `_last_flush`.
- On any exception: try `ROLLBACK` (ignore errors), then `_fail(e)`.
- No transaction is ever open between calls.

**`close()`**:
- Idempotent, and never raises.
- `flush()`, then `con.close()`.
- If `_reset_on_close` is set, unlink the four files (ignore `OSError`).

**`_fail(e)`**:
- `enabled = False`.
- If `type(e) is sqlite3.DatabaseError`, set `_reset_on_close = True`, so the next run starts fresh.
- Note once: `error (<e>); reading the remaining log files without it`.
- Drop the pending rows.

### Parse through the cache

```python
def _parse_log_file_cached(store: "CacheStore | None", kind: str, path: str, *,
                           host: str = "local", source: str = "local",
                           label: str | None = None, want_stats: bool = False,
                           agent_id_from_name: bool = False) -> dict | None:
    """_parse_log_file with the cache in front. Same return contract: None = unreadable;
    else {"events": [...], "stats": dict if want_stats else None}."""
```
Algorithm:
```python
lbl = path if label is None else label
if store is None or not store.enabled or not _key_text_ok(host, path, lbl, source):
    return _parse_log_file(kind, path, source=source, label=label,
                           want_stats=want_stats, agent_id_from_name=agent_id_from_name)
params = _cache_params(source, lbl, path, agent_id_from_name)
sig = _file_sig(path)
zst_ok = not (kind == "codex" and path.endswith(".zst")) or _zstd_available()
if sig is not None and zst_ok:
    p = store.get(host, kind, path, sig, params)
    if p is not None:
        return {"events": p["events"], "stats": p["stats"] if want_stats else None}
store.counts["misses"] += 1
r = _parse_log_file(kind, path, source=source, label=label, want_stats=True,
                    agent_id_from_name=agent_id_from_name)
if r is None:
    return None                                   # failures are never stored
if sig is not None and _file_sig(path) == sig and not _is_racy(sig, time.time_ns()):
    store.put(host, kind, path, sig, r, params)   # encodes now (F3)
return r if want_stats else {"events": r["events"], "stats": None}
```
- Call `_parse_log_file`, `_file_sig`, `_is_racy` and `_zstd_available` by their global names, so tests can wrap them.
- `time.time_ns()` is read **after** the second stat, which is the conservative end of the read.
- On a miss, the parse's own objects are returned (not a decoded copy). Round-trip equality makes the two the same by value (findings 2 and 5).

### Suggested `CLAUDE.md` entry (after item 4)

> 4a. **Parse cache** (`open_cache`, `CacheStore`, `_parse_log_file_cached`, `_encode_cache_payload`, `cache_code_key`) — not wired into reports yet (step 4). One SQLite file `cache.sqlite3` under `$CCTRACK_CACHE_DIR` / `$XDG_CACHE_HOME/cctrack` / `~/.cache/cctrack` (dir 0700, file 0600), one entry per log file keyed `(host, kind, path)`, valid while `(size, mtime_ns)` and the code key (module sha256 + interpreter + marshal version + default text encoding + parse params) match. Payload is `zlib(marshal({events, stats}))` encoded at `put` time, before any cross-file dedup; zlib's checksum makes a corrupt blob a miss. Racy (mtime within 2 s, or changed during the read) and failed parses are never stored; a `.zst` hit needs a decoder. `sqlite3` is imported lazily; any store error prints one `cctrack: cache …` note and the run continues uncached. `tests/conftest.py` gives every test its own `CCTRACK_CACHE_DIR`.

## 5. Behaviour and edge cases

- **All or nothing per run.** The first SQLite error (or any unexpected exception inside the store) disables the store for the rest of the run, with one note. Hits already served stay valid: each was checked and decoded. The only failure that does not disable the store is a single undecodable blob (`bad`), which is a miss and gets replaced by the following `put`.
- **Read-only directory.** The write probe at open fails, so `open_cache` returns `None` and notes it. Do not try to run "read-only but serving hits". The brief asks for an uncached path, and a single rule is simpler to verify.
- **Corruption found at open** (garbage file, failed `quick_check`, foreign `user_version`): delete, recreate once, one note, then a normal store. **Corruption found mid-run** (`DatabaseError` from `get`/`flush`): disable, one note, and delete the files at `close()`. Do not try to recreate in the middle of a run.
- **Locked database.** At open, `quick_check` or `BEGIN IMMEDIATE` waits `CACHE_BUSY_TIMEOUT`, then `OperationalError` → `None` + note. Mid-run, the same happens through `_fail`. A lock is never treated as corruption: `OperationalError` is a subclass of `DatabaseError`, so test with `type(e) is sqlite3.DatabaseError`, **not** `isinstance`.
- **Batches and interruption.** A put that crosses 8 MiB or 2 s flushes. Anything flushed survives a crash or Ctrl-C. Step 4 will call `close()` in a `finally`. Between flushes, no SQLite lock is held (test T21).
- **`get` reads only flushed rows.** It does not look at pending puts. A run looks up each file once, so this never matters there. Tests must call `store.flush()` between a storing call and a call that should hit. T7–T16 do.
- **F3.** `put` encodes immediately and keeps only bytes. Mutating the events after `put` must not change what is stored.
- **Racy.** A future mtime (clock skew, `touch -d`) counts as racy until the clock passes it. A file whose stat fails before the read is parsed (it may still be readable) but not looked up or stored.
- **Stats.** With a store, the parse always collects stats (plan 4.2; step 2's T3 proved that stats do not change events). When the caller didn't ask, `stats` is returned as `None`, so the contract equals `_parse_log_file`'s.
- **Code key.** It is computed at `open_cache`, from the file on disk. A source file edited during a run does not matter: the key is fixed for that store.
- **Rebuild.** `open_cache(rebuild=True)` deletes the database and starts empty. A rebuilt store behaves like a cold one.
- **Pruning.** Pruning happens at open, inside the probe transaction. An entry pruned just before this run would have used it is only a miss.
- **Unencodable payloads.** If a payload ever contains a datetime outside stats, a set subclass, a `defaultdict` or similar, `put` stores nothing (`unstorable`) and the report stays correct. T6 guards against such a regression on the corpus.
- **Not in this step:** `CCTRACK_CACHE=0`, `--no-cache`, `--rebuild-cache` and progress text (Step 4). Hook files (Step 5). Manifests (Step 6). `open_cache` does not read `CCTRACK_CACHE`.
- **Remote copies never touch the cache.** The module shipped by `_remote_program` sets `__file__ = '<cctrack>'`. Nothing on the remote path calls `open_cache` or `cache_code_key`, and importing the module does not import `sqlite3`.

## 6. Tests

All new tests go in `tests/test_cache_store.py`. Imports:
- `claude_line`, `codex_line`, `codex_usage`, `compact_line` from `test_detail`;
- `codex`, `counts`, `tc` from `test_accounting`;
- `build_corpus`, `zstd_raw_frame` from `test_cache_equivalence`.

Use the `sys.path.insert(0, str(Path(__file__).parent))` pattern from `test_cache_equivalence.py`.

Shared helpers in the test file:
- `_canon(x)`: `json.dumps(cctrack._tag_json(x))` **without** `sort_keys`. It is a strict comparator: key order, tuple vs list, bool vs int and int vs float all show.
- `_old(path, secs=3600)`: `os.utime(path, ns=(t, t))` with `t = time.time_ns() - secs * 10**9`. Use it on every file a test wants stored, because freshly written files are racy.
- `_raw_frame_text(path)`: decodes `zstd_raw_frame` output. Skip the 9-byte header (magic, descriptor, 4-byte size), then repeatedly read a 3-byte little-endian block header `h`: `size = h >> 3`, `last = h & 1`, then take `size` bytes. Join the blocks and decode UTF-8. Tests that need the corpus `.zst` readable use `monkeypatch.setattr(cctrack, "_read_zstd", _raw_frame_text)` and `monkeypatch.setattr(cctrack, "_zstd_available", lambda: True)`.
- `_rows(store)`: the count of entries, read through a separate `sqlite3.connect(store.path)`.
- `_count_reads(monkeypatch)`: wraps `cctrack._read_log_lines` to record paths and delegate to the original.

| # | Test | Proves |
|---|---|---|
| T1 | `test_roundtrip_parity_cases[case]` (parametrized over `FIXTURES["cases"]`) | Write the case files as `test_accounting` does. For each file: `r = _parse_log_file(agent, p, want_stats=True, agent_id_from_name=agent == "claude")`; `_canon(_decode_cache_payload(_encode_cache_payload(r))) == _canon(r)` and `== r`. Per case: `_canon(dedup_events(decoded lists)) == _canon(dedup_events(original lists))`. The originals are deep-copied before the first dedup, because dedup mutates them. |
| T2 | `test_roundtrip_corpus_through_the_store(tmp_path, monkeypatch)` | `build_corpus(tmp_path, settle=False)`, then `_old` on every file. For each home: set `HOME` and `SANDY_HOME` (and `delenv CODEX_HOME`) to that home, and patch the `.zst` reader as in the helpers. Lists are `discover_files(default_dirs())` (Claude, `agent_id_from_name=True`) and `discover_codex_files()` (Codex). With one `open_cache()` store, every file goes through `_parse_log_file_cached(..., want_stats=True)` three ways: cold, warm (after `store.flush()`) and plain `_parse_log_file`. Per file, cold, warm and plain are `_canon`-equal. `dedup_events` over each set of lists (deep copies) is `_canon`-equal. During the warm pass `_count_reads` records **0** reads, and `store.counts["hits"]` equals the number of readable files (both `.zst` included). The corpus has the fork child `C` whose parent `P` is in another file, the sandbox copy, the resumed session, the subagent file without `agentId`, the compaction and the bad line, so this is the 4.4 "section 5 corpus" round trip. |
| T3 | `test_fork_identity_loss_is_harmless()` | F6 (finding 6). Build `parent`, a `child` with 3 `tc` records (the first is a replay) and a `grandchild` with 4. `local = dedup_events([parent, child])`. Assert that `local` has ≥2 events with `_fork_history`, all the **same** list object. Then for `x` in (marshal round trip via `_decode_cache_payload(_encode_cache_payload({"events": local, "stats": None}))["events"]`, `[copy.deepcopy(e) for e in local]`, `_untag_json(_tag_json(local))`): `_canon(dedup_events([x, deepcopy(grandchild)])) == _canon(dedup_events([deepcopy(local), deepcopy(grandchild)]))` and the summed `input` is 1280. |
| T4 | `test_parse_payloads_share_no_containers(tmp_path)` | Over the corpus files and the parity cases: no `dict`/`list` is reachable twice inside one `_parse_log_file` result (walk by `id`). This is why per-file round trips are exact. |
| T5 | `test_stats_datetimes_round_trip()` | Stats with `first_ts` in UTC, `last_ts` at `+05:00` with microseconds, `compactions` with tuple keys, and stats `None`: decode equals input, and each datetime keeps the same `utcoffset()` and `isoformat()`. `_encode_cache_payload` does not modify the caller's stats. |
| T6 | `test_unencodable_payload_is_not_stored(tmp_path)` | A payload whose event holds a `defaultdict` or a `datetime`: `_encode_cache_payload` returns `None`. `store.put` leaves `_rows == 0` and `counts["unstorable"] == 1`, the store stays enabled, and no note is printed. Also: every readable corpus file's payload encodes (T2 asserts `counts["unstorable"] == 0`). |
| T7 | `test_unchanged_file_hits(tmp_path, monkeypatch)` | One Claude file, `_old`. The first call is a miss (1 read, one row after flush). After `store.flush()`, the second call makes **0** reads, `counts["hits"] == 1`, and the result is `_canon`-equal to the first. Also with `want_stats=False`, `stats is None`. |
| T8 | `test_changed_size_is_a_miss` | After storing, append a line, then restore the **original** mtime with `os.utime(ns=…)`. The next call reads the file, returns 2 events, and replaces the row. |
| T9 | `test_changed_mtime_is_a_miss` | After storing, rewrite the file with the **same size** but a different token count (one digit changed), and set its mtime to old + 5 s. Miss, and the new number is returned. |
| T10 | `test_code_change_is_a_miss(monkeypatch)` | Store with one store and close it. `monkeypatch.setattr(cctrack, "cache_code_key", lambda: "other")`, then open a new store: miss (1 read). |
| T11 | `test_parse_params_are_part_of_the_key` | One sidechain file `agent-b2.jsonl` without `agentId`. Store it with `agent_id_from_name=True` (agent_id `"b2"`). A call with `False` is a miss and gives `""`. A call with `source="h"` is a miss. A call with `label="h:/x"` is a miss. |
| T12 | `test_racy_mtime_is_not_stored` | A file whose mtime is `time.time_ns()` (`os.utime`). It is parsed and the result returned, but `_rows == 0` after flush, and the next call reads again. The same holds for an mtime 60 s in the future. After `_old`, it is stored. Also unit-check `_is_racy`: `(0, now - 2s + 1)` is racy, `(0, now - 2s - 1)` is not. |
| T13 | `test_changed_during_read_is_not_stored(monkeypatch)` | Wrap `cctrack._read_log_lines`. After delegating, (a) append a line to the file, or (b) `os.utime` it to a different old mtime (same size). In both cases the result is returned, nothing is stored, and the next call reads again. |
| T14 | `test_failures_are_not_stored(tmp_path)` | Missing path → `None`. Directory → `None`. A Codex `rollout-x.jsonl.zst` (4 magic bytes, `_old`) with `patch.dict(sys.modules, {"compression": None})` and `patch("shutil.which", return_value=None)` → `None`. `_rows == 0` in every case. The same calls with `store=None` give the same results. |
| T15 | `test_zst_hit_needs_a_decoder(tmp_path, monkeypatch)` | `p` = a `.jsonl.zst` file, `_old`. Patch `_read_zstd` to `_raw_frame_text` (written with `zstd_raw_frame`) and `_zstd_available` to `True`, then store. A second call hits (0 reads). Then patch `_zstd_available` to `False`, and `_read_zstd` to `lambda p: None`: the result is `None` (an uncached run would skip it). The row is still there (`_rows == 1`). |
| T16 | `test_corrupt_blob_is_a_miss(tmp_path)` | Store one entry. Then, through a separate connection, set its `payload` in turn to (a) `b"garbage"`, (b) the blob truncated by half, (c) `zlib.compress(marshal.dumps([1, 2]))` (wrong shape), (d) `zlib.compress(marshal.dumps({"events": [1], "stats": None}))`. Each time the next cached call re-reads the file (1 read), returns the correct events, increments `counts["bad"]`, keeps the store enabled and prints no note. After flush the row decodes again. |
| T17 | `test_corrupt_database_is_recreated(tmp_path, capsys)` | (a) Write `b"garbage" * 1000` to `cache_dir()/cache.sqlite3`. `open_cache()` returns an enabled store, stderr has exactly one line starting `cctrack: cache`, and a put/flush/get round trip works. (b) Build a healthy store with 200 rows of 3,000 random bytes and close it. Read `PRAGMA page_size` and the `rootpage` of `sqlite_autoindex_entries_1` from `sqlite_master` through a separate connection. Overwrite that page (`[(root-1)*size, root*size)`) with `0xff`. Same expectations, and the old rows are gone. Only `quick_check` detects this damage (finding 8). (c) Set `PRAGMA user_version = 99` on a healthy store: it is recreated with one note and old rows are gone. |
| T18 | `test_midrun_corruption_disables_and_resets(tmp_path, capsys)` | Open a store, then replace `store._con` with a stub whose `execute`/`executemany` raise `sqlite3.DatabaseError("database disk image is malformed")`. `get` returns `None`, `enabled` is False, and there is one note. Further gets and puts are silent no-ops. Cached parses still equal `_parse_log_file`. After `close()`, `cache.sqlite3` no longer exists. With `sqlite3.OperationalError("database is locked")` instead, the file **still exists** after close. |
| T19 | `test_locked_database(tmp_path, monkeypatch, capsys)` | `CACHE_BUSY_TIMEOUT = 0.05`. Create a store with one row and close it. A second `sqlite3` connection runs `BEGIN EXCLUSIVE`. (a) `open_cache()` returns `None` with one note, and the file is not deleted. (b) Open the store before taking the lock, then `get` → `None`, disabled, one note. The cached parse still returns correct events. Release the lock with `ROLLBACK` in a `finally`. |
| T20 | `test_read_only_directory(tmp_path, capsys)` | Skip if `os.geteuid() == 0`. Create a store with a row and close it. `chmod 0o500` the cache dir: `open_cache()` returns `None` with one note, and `_parse_log_file_cached(None, …)` equals `_parse_log_file`. A cache dir that cannot be created (`CCTRACK_CACHE_DIR` under a regular file) gives the same. Restore `0o700` in a `finally`. |
| T21 | `test_no_write_lock_between_flushes(tmp_path, monkeypatch)` | Set `CACHE_FLUSH_SECONDS = 3600`. Open a store and `put` one small row, which does not flush. A second connection with `timeout=0` can `BEGIN IMMEDIATE` and `COMMIT` at once. |
| T22 | `test_batches_are_committed_during_a_run(tmp_path, monkeypatch)` | `CACHE_FLUSH_BYTES = 1`. After 3 puts, and **without** `close()`, a second connection sees 3 rows. A second test case: `CACHE_FLUSH_SECONDS = 0` gives the same. |
| T23 | `test_payload_is_encoded_at_put(tmp_path)` | Deep-copy a parsed payload as `ref`, then `put` the original. Mutate the original's events (`e["input"] = 999`, `e["_fork_history"] = [(0, (1,))]`, append an event). Flush, then `get` equals `ref` by `_canon`. |
| T24 | `test_two_stores_at_once(tmp_path)` | Two `open_cache()` stores on the same directory, used alternately for puts, flushes and gets of different paths. Both stay enabled with no notes, and each sees the other's flushed rows. |
| T25 | `test_prune_and_last_used(tmp_path, monkeypatch)` | Rows with `last_used` at now − 31 days and now − 29 days (set through a separate connection). Reopen: the first row is gone, the second kept. A hit followed by `close()` updates the row's `last_used` to within 5 s of now. |
| T26 | `test_rebuild_starts_empty(tmp_path)` | Store a row and close. `open_cache(rebuild=True)`: `_rows == 0`, and the next cached call reads the file. |
| T27 | `test_store_file_modes(tmp_path)` | Under `os.umask(0o022)` (restored in a `finally`), with `CCTRACK_CACHE_DIR` set to a not-yet-existing dir: after `open_cache()` the dir is `0o700` and `cache.sqlite3` is `0o600`. |
| T28 | `test_cache_dir_resolution(monkeypatch, tmp_path)` | `CCTRACK_CACHE_DIR` wins, including `~` expansion. Without it, an absolute `XDG_CACHE_HOME` gives `XDG/cctrack`. A relative or empty `XDG_CACHE_HOME` gives `HOME/.cache/cctrack`. |
| T29 | `test_code_key_names_interpreter_and_encoding()` | `cache_code_key()` contains the module source's sha256, the interpreter version and `marshal{marshal.version}`. Two `subprocess.run([sys.executable, "-c", ...])` calls with `PYTHONPATH` set to the source dir under test, `PYTHONUTF8=0`, `PYTHONCOERCECLOCALE=0`, and `LC_ALL=C.UTF-8` vs `LC_ALL=C`, print different keys. Skip if the two calls report the same `locale` encoding. |
| T30 | `test_import_does_not_load_sqlite3()` | A subprocess runs `import sys, cctrack; print("sqlite3" in sys.modules)` and prints `False`. A second subprocess runs `sys.modules["sqlite3"] = None; import cctrack; print(cctrack.open_cache())`: it prints `None`, and stderr has one `cctrack: cache` line naming sqlite3. |
| T31 | `test_missing_sqlite3_in_process(monkeypatch, capsys, tmp_path)` | `monkeypatch.setitem(sys.modules, "sqlite3", None)`: `open_cache()` is `None` with one note, and `_parse_log_file_cached(None, …)` equals `_parse_log_file`. |
| T32 | `test_non_utf8_path_bypasses_the_store(tmp_path)` | Skip if the file system rejects the name. Create `os.fsdecode(tmp_path_bytes + b"/caf\xe9.jsonl")` with one usage line, `_old`. The cached call returns the same as `_parse_log_file`, raises nothing, stores nothing, keeps the store enabled, and prints no note. |
| T33 | `test_tests_use_a_private_cache_dir(tmp_path)` | `cctrack.cache_dir() == tmp_path / "cctrack-cache"`, and `os.environ["CCTRACK_CACHE_DIR"]` is set (guards the conftest change). |

### Brief criteria → tests

| Criterion | Proof |
|---|---|
| Round trip over every `ccusage-parity.json` case and the section 5 corpus | T1, T2 (both include `dedup_events`), T4, T5 |
| F6: identity loss harmless, proven with the fork fixtures | T3 (and T2's cross-file fork `C`/`P`) |
| Hit/miss: unchanged hits | T7, T2 warm pass |
| changed size / mtime / code hash / racy mtime / changed-during-read miss | T8 / T9 / T10 / T12 / T13 |
| failures not stored | T14 (plus T15 for decoder loss) |
| Corrupt blob is a miss | T16 |
| Corrupt database recreated | T17 (and T18 for mid-run) |
| Read-only dir / missing `sqlite3` / locked database → working uncached path and one note | T20 / T30, T31 / T19 |
| Budget: 1M events decode < 3 s; figure and encoding recorded | Verification 3 (a measurement, not a pytest test: timing tests are flaky). Planner's figures: finding 3. |
| `conftest.py` isolates the cache per test | T33 |
| Added: parse params in the key | T11 |
| Added: F3 encode-at-put | T23 |
| Added: no long write transaction; batches survive interruption; two runs at once | T21, T22, T24 |
| Added: pruning, rebuild, modes, location, key contents | T25, T26, T27, T28, T29 |
| Added: rule 4 (lazy `sqlite3`) | T30 |
| Added: non-UTF-8 paths and unencodable payloads can't crash a run | T32, T6 |

### Mutation checks

Run each on a copy of `src/`, one at a time: `cp -r src $S/m3/src`, edit, then `PYTHONPATH=$S/m3/src python3 -m pytest tests/test_cache_store.py -q`. The tests import `cctrack` from `PYTHONPATH`; T29 and T30 must also pass `PYTHONPATH` on to their subprocesses (derive it from `Path(cctrack.__file__).parents[1]`, as `test_cache_equivalence.SRC` does).

| # | Change | Must fail |
|---|---|---|
| M1 (brief) | Skip the racy rule: drop `and not _is_racy(sig, time.time_ns())` in `_parse_log_file_cached` | T12 |
| M2 (brief) | Skip the code-hash check: drop the `code ==` comparison in `get` | T10, T11 |
| M3 | Drop the `size ==` comparison in `get` | T8 |
| M4 | Drop the `mtime_ns ==` comparison in `get` | T9 |
| M5 | Drop the after-read check `_file_sig(path) == sig` | T13 |
| M6 | `_cache_params` returns `""` | T11 |
| M7 | Remove the `try/except` in `_decode_cache_payload` | T16 |
| M8 | Remove the shape check in `_decode_cache_payload` (keep the `try`) | T16 case (d) |
| M9 | In `open_cache`, return `None` instead of recreating on `DatabaseError` | T17 |
| M10 | Drop `PRAGMA quick_check` from `_connect` | T17 (b) |
| M11 | Move `import sqlite3` to the module top | T30 |
| M12 | Remove the `os.open(..., 0o600)` pre-create | T27 |
| M13 | Encode lazily: keep `payload` in `_pending` and encode in `flush` | T23 |
| M14 | Remove the `.zst` `_zstd_available()` condition | T15 |
| M15 | Write rows with plain `con.execute(INSERT…)` at `put` under the default `isolation_level` (implicit transaction), committing in `flush` | T21 |
| M16 | Remove the text-encoding part of `cache_code_key` | T29 |
| M17 | Remove the `CCTRACK_CACHE_DIR` line from `tests/conftest.py` (edit a copy of `tests/` too) | T33 |
| M18 | Decoder leaves `first_ts`/`last_ts` as strings | T1, T2, T5 |
| M19 | `_fail` doesn't disable the store (keeps trying) | T18, T19 (exactly one note) |
| M20 | Store an empty payload when `_parse_log_file` returns `None` | T14 |
| M21 | `_fail` uses `isinstance(e, sqlite3.DatabaseError)` for the reset decision | T18 (the file must survive an `OperationalError`) |
| M22 | Drop the write probe `PRAGMA user_version = CACHE_SCHEMA` (keep `CREATE TABLE IF NOT EXISTS`) | T20 |
| M23 | Remove `_key_text_ok` from the bypass condition | T32 |

## 7. Verification

`S=/tmp/claude-501/-home-sandy-dev-cctrack/b90020de-1841-4523-9564-a7f61b1fabce/scratchpad`. Keep big temporary files out of `/tmp` (finding 14).

1. **Suite and diff.**
   ```bash
   cd /home/sandy/dev/cctrack
   PYTHONPATH=src python3 -m pytest tests -q                 # 403 + new tests passed, 1 skipped
   PYTHONPATH=src python3 -m pytest tests/test_cache_store.py -q -v
   git diff --stat 64725e3 -- src tests CLAUDE.md README.md   # __init__.py, conftest.py, test_cache_store.py, CLAUDE.md
   git diff 64725e3 -- tests | grep '^-[^-]'                  # only the conftest docstring/line edits, no removed tests
   grep -n '^import sqlite3\|^from sqlite3' src/cctrack/__init__.py   # no hits
   ```

2. **Reports unchanged.** No caller exists yet, so all report output must match step 02. Get the old tree with `mkdir -p $S/old03 && git -C /home/sandy/dev/cctrack archive 64725e3 src | tar -x -C $S/old03`. Then:
   - `python3 $S/compare.py $S/old03/src /home/sandy/dev/cctrack/src` (the script from step 02's plan, section 7). Expect `matrix entries: 57 differences: []`.
   - `bash $S/live_compare.sh $S/old03/src /home/sandy/dev/cctrack/src`. Expect every line `same`.

   Afterwards, `rm -rf $S/live-home.*` to free `/tmp`.

3. **Decode budget.** Save the script below as `$S/s3/budget.py`, then run `python3 $S/s3/budget.py 1000000 2500`. Pass if both `decode` and `store get` are **under 3.0 s**. The planner's prototype gave decode 2.0–2.1 s and SQLite fetch + decode 1.8 s. The script needs about 5 GB of RAM and writes its database to `/dev/shm`.
   ```python
   """Step 3 decode budget: N realistic events in F per-file payloads, through the
   product encoder and the store. Usage: budget.py N F"""
   import copy, os, shutil, sys, tempfile, time
   from pathlib import Path
   sys.path[:0] = ["/home/sandy/dev/cctrack/src", "/home/sandy/dev/cctrack/tests"]
   import cctrack
   import test_cache_equivalence as h
   N, F = int(sys.argv[1]), int(sys.argv[2])
   root = Path(tempfile.mkdtemp(dir="/dev/shm"))
   try:
       proto = [cctrack._parse_log_file("claude", f, want_stats=True, agent_id_from_name=True)
                for f in cctrack.discover_files([str(Path.home() / ".claude/projects")])]
       c = h.build_corpus(root / "corpus", settle=False)
       proto += [cctrack._parse_log_file("codex", str(f), want_stats=True)
                 for f in sorted(c.home.rglob("rollout-*.jsonl"))]
       proto = [r for r in proto if r]
       cl = [e for r in proto for e in r["events"] if e.get("agent") != "codex"]
       cx = [e for r in proto for e in r["events"] if e.get("agent") == "codex"]
       per, payloads = N // F, []
       for fi in range(F):
           src, evs = (cx if fi % 5 == 0 else cl), []
           for j in range(per):
               e = copy.deepcopy(src[(fi * per + j) % len(src)])
               e["_request_id"] = f"{e.get('_request_id')}-{fi}-{j}"
               e["_source_file"] = f"/Users/u/.claude/projects/-Users-u-dev-p{fi % 40}/{fi:08x}-{j % 7:04x}.jsonl"
               evs.append(e)
           payloads.append({"events": evs, "stats": copy.deepcopy(proto[fi % len(proto)]["stats"])})
       blobs = [cctrack._encode_cache_payload(p) for p in payloads]
       assert all(b is not None for b in blobs)
       for i in (0, 1, F - 1):
           assert cctrack._decode_cache_payload(blobs[i]) == payloads[i]
       best = 1e9
       for _ in range(3):
           t = time.perf_counter(); out = [cctrack._decode_cache_payload(b) for b in blobs]
           best = min(best, time.perf_counter() - t); del out
       print(f"events {F * per:,}  files {F:,}  blobs {sum(map(len, blobs)) / 1e6:.1f} MB  decode {best:.2f} s")
       os.environ["CCTRACK_CACHE_DIR"] = str(root / "cache")
       st = cctrack.open_cache(); assert st is not None
       for i, p in enumerate(payloads):
           st.put("local", "claude", f"/f{i}", (i, 1), p)
       st.close(); del payloads, blobs
       st = cctrack.open_cache(); t = time.perf_counter()
       got = [st.get("local", "claude", f"/f{i}", (i, 1)) for i in range(F)]
       dt = time.perf_counter() - t
       assert all(g is not None for g in got) and st.counts["hits"] == F
       print(f"store get {dt:.2f} s  db {(root / 'cache' / 'cache.sqlite3').stat().st_size / 1e6:.1f} MB")
       st.close()
   finally:
       shutil.rmtree(root, ignore_errors=True)
   ```

4. **Live smoke on this sandbox's real logs.** This runs the store end to end against real transcripts. Save the script below as `$S/s3/live_store.py`, then run `python3 $S/s3/live_store.py`. Expected output: `files N readable N stored N (minus racy) warm reads 0 ... equal True`, and exit 0.
   ```python
   """Copy ~/.claude/projects into a scratch home, backdate mtimes, and check that cold,
   warm and uncached per-file parses and their dedup agree; warm must read nothing."""
   import copy, json, os, shutil, sys, tempfile, time
   from pathlib import Path
   sys.path.insert(0, "/home/sandy/dev/cctrack/src")
   import cctrack
   root = Path(tempfile.mkdtemp(dir="/dev/shm"))
   try:
       src = root / "projects"; shutil.copytree(Path.home() / ".claude/projects", src)
       old = time.time_ns() - 3600 * 10**9
       files = cctrack.discover_files([str(src)])
       for f in files: os.utime(f, ns=(old, old))
       os.environ["CCTRACK_CACHE_DIR"] = str(root / "cache")
       canon = lambda x: json.dumps(cctrack._tag_json(x))
       reads = []; real = cctrack._read_log_lines
       cctrack._read_log_lines = lambda k, p: (reads.append(p), real(k, p))[1]
       st = cctrack.open_cache()
       kw = dict(want_stats=True, agent_id_from_name=True)
       t = time.perf_counter(); cold = [cctrack._parse_log_file_cached(st, "claude", f, **kw) for f in files]; tc = time.perf_counter() - t
       st.flush(); reads.clear()
       t = time.perf_counter(); warm = [cctrack._parse_log_file_cached(st, "claude", f, **kw) for f in files]; tw = time.perf_counter() - t
       warm_reads = len(reads)
       plain = [cctrack._parse_log_file("claude", f, **kw) for f in files]
       same = all(canon(a) == canon(b) == canon(c) for a, b, c in zip(cold, warm, plain))
       d = [canon(cctrack.dedup_events(copy.deepcopy([r["events"] for r in rs if r]))) for rs in (cold, warm, plain)]
       print(f"files {len(files)} readable {sum(1 for r in plain if r)} stored {st.counts['stored']} "
             f"warm reads {warm_reads} hits {st.counts['hits']} cold {tc:.2f}s warm {tw:.2f}s "
             f"equal {same and d[0] == d[1] == d[2]}")
       st.close()
       sys.exit(0 if same and d[0] == d[1] == d[2] and warm_reads == 0 else 1)
   finally:
       shutil.rmtree(root, ignore_errors=True)
   ```
   CLI smoke (no behaviour change expected): `PYTHONPATH=src python3 -m cctrack >/dev/null && PYTHONPATH=src python3 -m cctrack --sessions --coverage >/dev/null`. Both exit 0. `ls ~/.cache/cctrack` must not exist afterwards unless it existed before, because nothing calls `open_cache` yet.

5. **Second interpreter (optional).** `PYTHONPATH=/home/sandy/dev/cctrack/.venv/lib/python3.12/site-packages:/home/sandy/dev/cctrack/src $S/uvpy/cpython-3.14.10-linux-aarch64-gnu/bin/python3 -m pytest /home/sandy/dev/cctrack/tests/test_cache_store.py -q -p no:cacheprovider`. On 3.14, `compression.zstd` exists, so T14's no-decoder case relies on its `sys.modules` patch. Report the result. A failure that appears only on 3.14 is a finding, not a blocker.

6. Run M1–M23 (section 6) and confirm each named test fails.

## 8. Deviations

- **Code key widened** (plan 4.3 edited). Besides the source hash, the key names the interpreter version, `marshal.version` and the default text encoding, and the `code` column carries the parse parameters. Evidence: findings 4 and 9. D2's intent (no stale numbers) is unchanged. The cost: a Python upgrade or a locale change also causes one full read. Two interpreters that alternate on one cache keep replacing each other's entries (one entry per path). That is correct, just slow. Step 7's docs should mention it.
- **`.zst` hits need a decoder** (plan 4.1 edited). Without this rule, a cached run could report rollouts that an uncached run skips (finding 12).
- **Store mechanics recorded in plan 4.4:** the encoding decision, the `DELETE` journal, the per-batch `BEGIN IMMEDIATE`, `quick_check` plus the write probe at open, and all-or-nothing per run.
- **A read-only directory means uncached.** The store does not serve hits from a directory it cannot write. That follows the brief ("a working uncached path") and keeps one failure rule.
- **The budget is a measurement, not a pytest test** (as Step 1 did for its 5× criterion). The figures are in finding 3, and verification 3 re-measures with the real implementation.
- **`_parse_log_file_cached` is part of this step,** although it has no caller. The brief's hit/miss table includes the racy and changed-during-read rules, which only exist at parse time. Step 4 only has to call it from `parse_events`/`parse_codex_events`.
- **Added criteria** beyond the brief: T4, T6, T11, T15, T18, T21–T32 (section 6 table).

## 9. Out of scope

- Calling the cache from `parse_events`, `parse_codex_events` or `main`, and the options `--no-cache`, `--rebuild-cache` and `CCTRACK_CACHE` (Step 4). Also progress text with reuse counts, the section 5 equivalence with the cache on, open counting on warm runs, and `close()` in a `finally` around a run (Step 4).
- GC tuning for bulk loads (finding 13, Step 4).
- Hook-file entries (`kind="hook"`, Step 5).
- Manifests for remote hosts, storing remote payloads, and the `host` column holding remote names (Step 6). A remote host literally named `local` would collide with the local rows. Step 6 should decide how to handle that, for example by prefixing remote host names.
- README and `--help` text (Step 7). Remember the interpreter/locale invalidation note from section 8.
- Byte-offset resume (Step 8).
