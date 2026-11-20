"""Step 3: the parse cache store, standalone (no report path calls it yet).

Round trips, the hit/miss table, robustness (corrupt blob/database, read-only
directory, missing sqlite3, locked database) and the F6 identity proof."""

import copy
import json
import marshal
import os
import sqlite3
import subprocess
import sys
import time
import zlib
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

import cctrack

sys.path.insert(0, str(Path(__file__).parent))
from test_accounting import FIXTURES, codex, counts, tc  # noqa: E402
from test_cache_equivalence import build_corpus, zstd_raw_frame  # noqa: E402
from test_detail import claude_line, codex_line, codex_usage, compact_line  # noqa: E402

SRC = Path(cctrack.__file__).resolve().parents[1]
DAY = "2026-10-10"


# ── helpers ──────────────────────────────────────────────────────────────

def _canon(x):
    """Strict comparator: key order, tuple vs list, bool vs int, int vs float all show."""
    return json.dumps(cctrack._tag_json(x))


def _old(path, secs=3600):
    t = time.time_ns() - secs * 10**9
    os.utime(path, ns=(t, t))


def _raw_frame_text(path):
    data = Path(path).read_bytes()[9:]
    out, i = [], 0
    while i < len(data):
        h = int.from_bytes(data[i:i + 3], "little")
        size, last = h >> 3, h & 1
        out.append(data[i + 3:i + 3 + size])
        i += 3 + size
        if last:
            break
    return b"".join(out).decode("utf-8")


def _rows(store):
    con = sqlite3.connect(store.path)
    try:
        return con.execute("SELECT count(*) FROM entries").fetchone()[0]
    finally:
        con.close()


def _count_reads(monkeypatch):
    reads = []
    real = cctrack._read_log_lines

    def wrapper(kind, path):
        reads.append(path)
        return real(kind, path)
    monkeypatch.setattr(cctrack, "_read_log_lines", wrapper)
    return reads


def _claude_file(tmp_path, name="a.jsonl", n=1, inp=10, old=True):
    p = tmp_path / name
    p.write_text("\n".join(claude_line(f"{DAY}T01:00:0{i}Z", f"r{i}", inp=inp) for i in range(n)) + "\n")
    if old:
        _old(p)
    return str(p)


def _cached(store, kind, path, **kw):
    kw.setdefault("want_stats", True)
    return cctrack._parse_log_file_cached(store, kind, path, **kw)


def _plain(kind, path, **kw):
    kw.setdefault("want_stats", True)
    return cctrack._parse_log_file(kind, path, **kw)


def _stderr_notes(capsys):
    return [ln for ln in capsys.readouterr().err.splitlines() if ln.startswith("cctrack: cache")]


@pytest.fixture
def store():
    s = cctrack.open_cache()
    assert s is not None
    yield s
    s.close()


# ── round trips ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("case", FIXTURES["cases"], ids=lambda c: c["name"])
def test_roundtrip_parity_cases(case, tmp_path):
    agent = case["agent"]
    results = []
    for i, rows in enumerate(case["files"]):
        path = tmp_path / f"rollout-{i}.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        r = _plain(agent, str(path), agent_id_from_name=agent == "claude")
        d = cctrack._decode_cache_payload(cctrack._encode_cache_payload(r))
        assert d is not None
        assert _canon(d) == _canon(r) and d == r
        results.append((r, d))
    originals = copy.deepcopy([r["events"] for r, _ in results])
    decoded = copy.deepcopy([d["events"] for _, d in results])
    assert _canon(cctrack.dedup_events(decoded)) == _canon(cctrack.dedup_events(originals))


def test_roundtrip_corpus_through_the_store(tmp_path, monkeypatch):
    corpus = build_corpus(tmp_path / "corpus", settle=False)
    for f in corpus.root.rglob("*"):
        if f.is_file():
            _old(f)
    monkeypatch.setattr(cctrack, "_read_zstd", _raw_frame_text)
    monkeypatch.setattr(cctrack, "_zstd_available", lambda: True)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    st = cctrack.open_cache()
    assert st is not None
    n_files = 0
    for home in (corpus.home, corpus.remote_home):
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("SANDY_HOME", str(home / ".sandy"))
        for kind, files in (("claude", cctrack.discover_files(cctrack.default_dirs())),
                            ("codex", cctrack.discover_codex_files())):
            assert files
            kw = dict(want_stats=True, agent_id_from_name=kind == "claude")
            cold = [cctrack._parse_log_file_cached(st, kind, f, **kw) for f in files]
            st.flush()
            with monkeypatch.context() as m:
                reads = _count_reads(m)
                warm = [cctrack._parse_log_file_cached(st, kind, f, **kw) for f in files]
            assert reads == []
            plain = [_plain(kind, f, agent_id_from_name=kind == "claude") for f in files]
            n_files += sum(1 for r in plain if r)
            for a, b, c in zip(cold, warm, plain):
                assert _canon(a) == _canon(b) == _canon(c)
            groups = [copy.deepcopy([r["events"] for r in rs if r]) for rs in (cold, warm, plain)]
            ded = [_canon(cctrack.dedup_events(g)) for g in groups]
            assert ded[0] == ded[1] == ded[2]
    assert st.counts["unstorable"] == 0 and st.counts["bad"] == 0
    assert st.counts["hits"] == n_files
    assert any(f.endswith(".zst") for f in cctrack.discover_codex_files())
    st.close()


def test_roundtrip_corpus_zst_and_fork_are_in_the_corpus(tmp_path):
    """Guards T2's coverage: the corpus holds the .zst rollout and the cross-file fork."""
    corpus = build_corpus(tmp_path / "corpus", settle=False)
    names = [p.name for p in (corpus.home / ".codex").rglob("rollout-*")]
    assert any(n.endswith(".jsonl.zst") for n in names)
    assert any(n.endswith("-C.jsonl") for n in names) and any(n.endswith("-P.jsonl") for n in names)


def test_fork_identity_loss_is_harmless():
    parent = cctrack.parse_codex_lines([codex("session_meta", {"id": "p"}), tc(counts(), counts())])
    child = cctrack.parse_codex_lines([
        codex("session_meta", {"id": "c", "forked_from_id": "p"}),
        tc(counts(), counts(), 10), tc(counts(200, 20), counts(1200, 120), 20),
        tc(counts(100, 10), counts(1300, 130), 30)])
    grandchild = cctrack.parse_codex_lines([
        codex("session_meta", {"id": "gc", "forked_from_id": "c"}),
        tc(counts(), counts(), 30), tc(counts(200, 20), counts(1200, 120), 31),
        tc(counts(100, 10), counts(1300, 130), 32), tc(counts(50, 5), counts(1350, 135), 40)])
    local = cctrack.dedup_events([parent, child])
    hist = [e for e in local if "_fork_history" in e]
    assert len(hist) >= 2 and len({id(e["_fork_history"]) for e in hist}) == 1
    ref = cctrack.dedup_events([copy.deepcopy(local), copy.deepcopy(grandchild)])
    assert sum(e["input"] for e in ref) == 1350
    forms = [
        cctrack._decode_cache_payload(cctrack._encode_cache_payload(
            {"events": local, "stats": None}))["events"],
        [copy.deepcopy(e) for e in local],
        cctrack._untag_json(cctrack._tag_json(local)),
    ]
    for x in forms:
        out = cctrack.dedup_events([x, copy.deepcopy(grandchild)])
        assert _canon(out) == _canon(ref)
        assert sum(e["input"] for e in out) == sum(e["input"] for e in ref)


def _containers(x, seen, dup):
    if isinstance(x, (dict, list)):
        if id(x) in seen:
            dup.append(x)
            return
        seen.add(id(x))
    if isinstance(x, dict):
        for k, v in x.items():
            _containers(k, seen, dup)
            _containers(v, seen, dup)
    elif isinstance(x, (list, tuple)):
        for v in x:
            _containers(v, seen, dup)


def test_parse_payloads_share_no_containers(tmp_path, monkeypatch):
    corpus = build_corpus(tmp_path / "corpus", settle=False)
    monkeypatch.setattr(cctrack, "_read_zstd", _raw_frame_text)
    files = [(k, str(f)) for k, pat in (("claude", "*.jsonl"),) for f in corpus.home.rglob(pat)
             if ".codex" not in f.parts and "cctrack" not in f.parts]
    files += [("codex", str(f)) for f in (corpus.home / ".codex").rglob("rollout-*")]
    for i, case in enumerate(FIXTURES["cases"]):
        for j, rows in enumerate(case["files"]):
            p = tmp_path / f"case{i}-{j}.jsonl"
            p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
            files.append((case["agent"], str(p)))
    checked = 0
    for kind, f in files:
        r = _plain(kind, f, agent_id_from_name=kind == "claude")
        if r is None:
            continue
        dup = []
        _containers(r, set(), dup)
        assert dup == [], f
        checked += 1
    assert checked > 10


def test_stats_datetimes_round_trip():
    utc = datetime(2026, 10, 10, 1, 2, 3, tzinfo=timezone.utc)
    off = datetime(2026, 10, 10, 6, 7, 8, 123456, tzinfo=timezone(timedelta(hours=5)))
    st = {"lines": 3, "first_ts": utc, "last_ts": off, "compactions": {("s", "c1"): 1}}
    before = dict(st)
    d = cctrack._decode_cache_payload(cctrack._encode_cache_payload({"events": [], "stats": st}))
    assert st == before and st["first_ts"] is utc            # caller's dict untouched
    assert d["stats"] == st
    for k in ("first_ts", "last_ts"):
        assert d["stats"][k].utcoffset() == st[k].utcoffset()
        assert d["stats"][k].isoformat() == st[k].isoformat()
    assert list(d["stats"]["compactions"]) == [("s", "c1")]
    none = cctrack._decode_cache_payload(cctrack._encode_cache_payload({"events": [], "stats": None}))
    assert none == {"events": [], "stats": None}


def test_unencodable_payload_is_not_stored(store, capsys):
    for bad in (defaultdict(int), datetime.now(timezone.utc)):
        assert cctrack._encode_cache_payload({"events": [{"x": bad}], "stats": None}) is None
    store.put("local", "claude", "/x", (1, 1), {"events": [{"x": defaultdict(int)}], "stats": None})
    store.flush()
    assert _rows(store) == 0 and store.counts["unstorable"] == 1
    assert store.enabled and _stderr_notes(capsys) == []


# ── hit / miss table ─────────────────────────────────────────────────────

def test_unchanged_file_hits(tmp_path, monkeypatch, store):
    p = _claude_file(tmp_path)
    reads = _count_reads(monkeypatch)
    first = _cached(store, "claude", p)
    assert len(reads) == 1
    store.flush()
    assert _rows(store) == 1
    second = _cached(store, "claude", p)
    assert len(reads) == 1 and store.counts["hits"] == 1
    assert _canon(first) == _canon(second)
    third = _cached(store, "claude", p, want_stats=False)
    assert third["stats"] is None and third["events"] == first["events"]
    assert len(reads) == 1


def test_changed_size_is_a_miss(tmp_path, monkeypatch, store):
    p = _claude_file(tmp_path)
    mt = os.stat(p).st_mtime_ns
    _cached(store, "claude", p)
    store.flush()
    with open(p, "a") as f:
        f.write(claude_line(f"{DAY}T02:00:00Z", "r9") + "\n")
    os.utime(p, ns=(mt, mt))
    reads = _count_reads(monkeypatch)
    r = _cached(store, "claude", p)
    assert len(reads) == 1 and len(r["events"]) == 2
    store.flush()
    assert len(_cached(store, "claude", p)["events"]) == 2 and len(reads) == 1


def test_changed_mtime_is_a_miss(tmp_path, monkeypatch, store):
    p = _claude_file(tmp_path, inp=11)
    _cached(store, "claude", p)
    store.flush()
    text = Path(p).read_text()
    Path(p).write_text(text.replace('"input_tokens": 11', '"input_tokens": 22'))
    assert os.stat(p).st_size == len(text.encode())
    t = time.time_ns() - 3595 * 10**9
    os.utime(p, ns=(t, t))
    reads = _count_reads(monkeypatch)
    r = _cached(store, "claude", p)
    assert len(reads) == 1 and r["events"][0]["input"] == 22


def test_code_change_is_a_miss(tmp_path, monkeypatch):
    p = _claude_file(tmp_path)
    with cctrack.open_cache() as s:
        _cached(s, "claude", p)
    monkeypatch.setattr(cctrack, "cache_code_key", lambda: "other")
    reads = _count_reads(monkeypatch)
    with cctrack.open_cache() as s:
        _cached(s, "claude", p)
        assert len(reads) == 1 and s.counts["hits"] == 0


def test_parse_params_are_part_of_the_key(tmp_path, monkeypatch, store):
    sub = tmp_path / "sub"
    sub.mkdir()
    p = sub / "agent-b2.jsonl"
    p.write_text(claude_line(f"{DAY}T01:00:00Z", "r1", sidechain=True) + "\n")
    _old(p)
    p = str(p)
    reads = _count_reads(monkeypatch)
    base = dict(agent_id_from_name=True)
    assert _cached(store, "claude", p, **base)["events"][0]["agent_id"] == "b2"
    store.flush()
    n = len(reads)
    _cached(store, "claude", p, **base)
    assert len(reads) == n                                  # baseline hits
    for variant in (dict(agent_id_from_name=False), dict(source="h", **base),
                    dict(label="h:/x", **base)):
        _cached(store, "claude", p, **base)                 # (re)store the baseline
        store.flush()
        n = len(reads)
        r = _cached(store, "claude", p, **variant)
        assert len(reads) == n + 1, variant
        if variant.get("agent_id_from_name") is False:
            assert r["events"][0]["agent_id"] == ""
        store.flush()


def test_racy_mtime_is_not_stored(tmp_path, monkeypatch, store):
    now = time.time_ns()
    assert cctrack._is_racy((0, now - 2_000_000_000 + 1000), now)
    assert not cctrack._is_racy((0, now - 2_000_000_000 - 1), now)
    p = _claude_file(tmp_path, old=False)
    reads = _count_reads(monkeypatch)
    for mt in (time.time_ns(), time.time_ns() + 60 * 10**9):
        os.utime(p, ns=(mt, mt))
        assert len(_cached(store, "claude", p)["events"]) == 1
        store.flush()
        assert _rows(store) == 0
        n = len(reads)
        _cached(store, "claude", p)
        assert len(reads) == n + 1
    _old(p)
    _cached(store, "claude", p)
    store.flush()
    assert _rows(store) == 1


def test_changed_during_read_is_not_stored(tmp_path, monkeypatch, store):
    p = _claude_file(tmp_path)
    real = cctrack._read_log_lines
    reads = []

    def appending(kind, path):
        out = real(kind, path)
        reads.append(path)
        with open(path, "a") as f:
            f.write(claude_line(f"{DAY}T03:00:00Z", f"x{len(reads)}") + "\n")
        _old(path)
        return out

    def touching(kind, path):
        out = real(kind, path)
        reads.append(path)
        t = time.time_ns() - 7200 * 10**9
        os.utime(path, ns=(t, t))
        return out

    for wrapper in (appending, touching):
        monkeypatch.setattr(cctrack, "_read_log_lines", wrapper)
        reads.clear()
        r = _cached(store, "claude", p)
        assert r is not None and len(reads) == 1
        store.flush()
        assert _rows(store) == 0
        _cached(store, "claude", p)
        assert len(reads) == 2
        _old(p)


def test_failures_are_not_stored(tmp_path, store):
    z = tmp_path / "rollout-x.jsonl.zst"
    z.write_bytes(b"\x28\xb5\x2f\xfd")
    _old(z)
    cases = [("claude", str(tmp_path / "missing.jsonl")), ("claude", str(tmp_path)),
             ("codex", str(z))]
    with patch.dict(sys.modules, {"compression": None}), patch("shutil.which", return_value=None):
        for kind, path in cases:
            assert _cached(store, kind, path) is None
            assert _plain(kind, path) is None
            assert cctrack._parse_log_file_cached(None, kind, path) is None
            store.flush()
            assert _rows(store) == 0


def test_zst_hit_needs_a_decoder(tmp_path, monkeypatch, store):
    z = tmp_path / "rollout-2026-10-10T08-00-00-Z.jsonl.zst"
    text = "\n".join([
        codex_line(f"{DAY}T08:00:00Z", "session_meta", {"id": "Z", "session_id": "Z", "cwd": "/w"}),
        codex_line(f"{DAY}T08:00:01Z", "turn_context", {"model": "gpt-5.4"}),
        tc(counts(300, 30), counts(300, 30), 2)]) + "\n"
    z.write_bytes(zstd_raw_frame(text.encode()))
    _old(z)
    p = str(z)
    monkeypatch.setattr(cctrack, "_read_zstd", _raw_frame_text)
    monkeypatch.setattr(cctrack, "_zstd_available", lambda: True)
    first = _cached(store, "codex", p)
    assert first and first["events"]
    store.flush()
    reads = _count_reads(monkeypatch)
    assert _cached(store, "codex", p)["events"] == first["events"] and reads == []
    monkeypatch.setattr(cctrack, "_zstd_available", lambda: False)
    monkeypatch.setattr(cctrack, "_read_zstd", lambda path: None)
    assert _cached(store, "codex", p) is None
    assert _rows(store) == 1


# ── robustness ───────────────────────────────────────────────────────────

def test_corrupt_blob_is_a_miss(tmp_path, monkeypatch, capsys, store):
    p = _claude_file(tmp_path)
    good = _cached(store, "claude", p)
    store.flush()
    blob = sqlite3.connect(store.path).execute("SELECT payload FROM entries").fetchone()[0]
    bads = [b"garbage", blob[:len(blob) // 2], zlib.compress(marshal.dumps([1, 2])),
            zlib.compress(marshal.dumps({"events": [1], "stats": None}))]
    reads = _count_reads(monkeypatch)
    for i, bad in enumerate(bads, 1):
        con = sqlite3.connect(store.path)
        con.execute("UPDATE entries SET payload=?", (bad,))
        con.commit()
        con.close()
        n = len(reads)
        r = _cached(store, "claude", p)
        assert len(reads) == n + 1 and _canon(r) == _canon(good)
        assert store.counts["bad"] == i and store.enabled
        store.flush()
        assert cctrack._decode_cache_payload(
            sqlite3.connect(store.path).execute("SELECT payload FROM entries").fetchone()[0]) is not None
    assert _stderr_notes(capsys) == []


def _row_store(n=200):
    s = cctrack.open_cache()
    s._con.execute("BEGIN")
    for i in range(n):
        s._con.execute("INSERT INTO entries VALUES ('local','claude',?,1,1,'c',?,?,?)",
                       (f"/p{i}", os.urandom(3000), int(time.time()), int(time.time())))
    s._con.execute("COMMIT")
    return s


def _assert_recreated(capsys, expect_rows=0):
    s = cctrack.open_cache()
    assert s is not None and s.enabled
    notes = _stderr_notes(capsys)
    assert len(notes) == 1, notes
    assert _rows(s) == expect_rows
    s.put("local", "claude", "/a", (1, 2), {"events": [{"a": 1}], "stats": None})
    s.flush()
    assert s.get("local", "claude", "/a", (1, 2)) == {"events": [{"a": 1}], "stats": None}
    assert s.counts["hits"] == 1
    s.close()


def test_corrupt_database_is_recreated(capsys):
    db = cctrack.cache_dir() / cctrack.CACHE_FILENAME
    db.parent.mkdir(parents=True)
    db.write_bytes(b"garbage" * 1000)
    _assert_recreated(capsys)


def test_corrupt_index_page_is_recreated(capsys):
    s = _row_store()
    s.close()
    con = sqlite3.connect(s.path)
    size = con.execute("PRAGMA page_size").fetchone()[0]
    root = con.execute("SELECT rootpage FROM sqlite_master "
                       "WHERE name='sqlite_autoindex_entries_1'").fetchone()[0]
    con.close()
    with open(s.path, "r+b") as f:
        f.seek((root - 1) * size)
        f.write(b"\xff" * size)
    _assert_recreated(capsys)


def test_foreign_schema_version_is_recreated(capsys):
    s = _row_store(5)
    s.close()
    con = sqlite3.connect(s.path)
    con.execute("PRAGMA user_version = 99")
    con.commit()
    con.close()
    _assert_recreated(capsys)


class _Stub:
    def __init__(self, exc):
        self.exc = exc

    def execute(self, *a, **k):
        raise self.exc

    executemany = execute

    def close(self):
        pass


def test_midrun_corruption_disables_and_resets(tmp_path, capsys):
    p = _claude_file(tmp_path)
    for exc, survives in ((sqlite3.DatabaseError("database disk image is malformed"), False),
                          (sqlite3.OperationalError("database is locked"), True)):
        s = cctrack.open_cache()
        real = s._con
        s._con = _Stub(exc)
        assert s.get("local", "claude", p, (1, 1)) is None
        assert not s.enabled and len(_stderr_notes(capsys)) == 1
        assert s.get("local", "claude", p, (1, 1)) is None
        s.put("local", "claude", p, (1, 1), {"events": [], "stats": None})
        s.flush()
        assert _stderr_notes(capsys) == []
        assert _canon(_cached(s, "claude", p)) == _canon(_plain("claude", p))
        s._con = real
        s.close()
        assert (cctrack.cache_dir() / cctrack.CACHE_FILENAME).exists() is survives


def test_locked_database(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cctrack, "CACHE_BUSY_TIMEOUT", 0.05)
    p = _claude_file(tmp_path)
    with cctrack.open_cache() as s:
        _cached(s, "claude", p)
    db = cctrack.cache_dir() / cctrack.CACHE_FILENAME
    other = sqlite3.connect(db, isolation_level=None)
    try:
        other.execute("BEGIN EXCLUSIVE")
        assert cctrack.open_cache() is None
        assert len(_stderr_notes(capsys)) == 1 and db.exists()
        other.execute("ROLLBACK")
        s = cctrack.open_cache()
        assert s is not None and _stderr_notes(capsys) == []
        other.execute("BEGIN EXCLUSIVE")
        assert s.get("local", "claude", p, (1, 1)) is None
        assert not s.enabled and len(_stderr_notes(capsys)) == 1
        assert _canon(_cached(s, "claude", p)) == _canon(_plain("claude", p))
        assert _stderr_notes(capsys) == []
        s.close()
    finally:
        other.execute("ROLLBACK") if other.in_transaction else None
        other.close()
    assert db.exists()


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores modes")
def test_read_only_directory(tmp_path, capsys, monkeypatch):
    p = _claude_file(tmp_path)
    with cctrack.open_cache() as s:
        _cached(s, "claude", p)
    d = cctrack.cache_dir()
    d.chmod(0o500)
    try:
        assert cctrack.open_cache() is None
        assert len(_stderr_notes(capsys)) == 1
        assert _canon(cctrack._parse_log_file_cached(None, "claude", p, want_stats=True)) == \
            _canon(_plain("claude", p))
    finally:
        d.chmod(0o700)
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv("CCTRACK_CACHE_DIR", str(blocker / "sub"))
    assert cctrack.open_cache() is None
    assert len(_stderr_notes(capsys)) == 1


def test_no_write_lock_between_flushes(monkeypatch, store):
    monkeypatch.setattr(cctrack, "CACHE_FLUSH_SECONDS", 3600)
    store.put("local", "claude", "/a", (1, 1), {"events": [{"a": 1}], "stats": None})
    assert store._pending and _rows(store) == 0
    con = sqlite3.connect(store.path, timeout=0, isolation_level=None)
    con.execute("BEGIN IMMEDIATE")
    con.execute("COMMIT")
    con.close()


@pytest.mark.parametrize("knob", ["CACHE_FLUSH_BYTES", "CACHE_FLUSH_SECONDS"])
def test_batches_are_committed_during_a_run(monkeypatch, store, knob):
    monkeypatch.setattr(cctrack, knob, 1 if knob == "CACHE_FLUSH_BYTES" else 0)
    for i in range(3):
        store.put("local", "claude", f"/p{i}", (1, 1), {"events": [{"a": i}], "stats": None})
    assert _rows(store) == 3                      # no close()


def test_payload_is_encoded_at_put(store):
    payload = {"events": [{"input": 1, "_fork_history": [(1, (2,))]}], "stats": None}
    ref = copy.deepcopy(payload)
    store.put("local", "claude", "/a", (1, 1), payload)
    payload["events"][0]["input"] = 999
    payload["events"][0]["_fork_history"] = [(0, (1,))]
    payload["events"].append({"x": 1})
    store.flush()
    assert _canon(store.get("local", "claude", "/a", (1, 1))) == _canon(ref)


def test_two_stores_at_once(tmp_path, capsys):
    a, b = cctrack.open_cache(), cctrack.open_cache()
    assert a and b
    a.put("local", "claude", "/a", (1, 1), {"events": [{"a": 1}], "stats": None})
    a.flush()
    b.put("local", "claude", "/b", (1, 1), {"events": [{"b": 1}], "stats": None})
    b.flush()
    assert b.get("local", "claude", "/a", (1, 1)) == {"events": [{"a": 1}], "stats": None}
    assert a.get("local", "claude", "/b", (1, 1)) == {"events": [{"b": 1}], "stats": None}
    assert a.enabled and b.enabled and _stderr_notes(capsys) == []
    a.close()
    b.close()


def test_prune_and_last_used(store):
    now = int(time.time())
    for p in ("/old", "/new"):
        store.put("local", "claude", p, (1, 1), {"events": [], "stats": None})
    store.flush()
    path = store.path
    store.close()
    con = sqlite3.connect(path)
    con.execute("UPDATE entries SET last_used=? WHERE path='/old'", (now - 31 * 86400,))
    con.execute("UPDATE entries SET last_used=? WHERE path='/new'", (now - 29 * 86400,))
    con.commit()
    con.close()
    s = cctrack.open_cache()
    assert s.get("local", "claude", "/old", (1, 1)) is None
    assert s.get("local", "claude", "/new", (1, 1)) is not None
    s.close()
    con = sqlite3.connect(path)
    rows = dict(con.execute("SELECT path, last_used FROM entries").fetchall())
    con.close()
    assert list(rows) == ["/new"] and abs(rows["/new"] - time.time()) < 5


def test_rebuild_starts_empty(tmp_path, monkeypatch):
    p = _claude_file(tmp_path)
    with cctrack.open_cache() as s:
        _cached(s, "claude", p)
    s = cctrack.open_cache(rebuild=True)
    assert _rows(s) == 0
    reads = _count_reads(monkeypatch)
    _cached(s, "claude", p)
    assert len(reads) == 1
    s.close()


def test_store_file_modes(tmp_path, monkeypatch):
    d = tmp_path / "fresh" / "nested"
    monkeypatch.setenv("CCTRACK_CACHE_DIR", str(d))
    old = os.umask(0o022)
    try:
        s = cctrack.open_cache()
    finally:
        os.umask(old)
    assert s
    assert (d.stat().st_mode & 0o777) == 0o700
    assert ((d / cctrack.CACHE_FILENAME).stat().st_mode & 0o777) == 0o600
    s.close()


def test_cache_dir_resolution(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CCTRACK_CACHE_DIR", "~/custom")
    assert cctrack.cache_dir() == tmp_path / "home" / "custom"
    monkeypatch.delenv("CCTRACK_CACHE_DIR")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert cctrack.cache_dir() == tmp_path / "xdg" / "cctrack"
    for bad in ("relative/dir", ""):
        monkeypatch.setenv("XDG_CACHE_HOME", bad)
        assert cctrack.cache_dir() == tmp_path / "home" / ".cache" / "cctrack"


def _run_py(code, **env):
    e = {k: v for k, v in os.environ.items() if k not in ("LC_ALL", "LANG", "PYTHONUTF8")}
    e.update(PYTHONPATH=str(SRC), **env)
    return subprocess.run([sys.executable, "-c", code], env=e, capture_output=True, text=True)


def test_code_key_names_interpreter_and_encoding():
    import hashlib
    key = cctrack.cache_code_key()
    assert hashlib.sha256(Path(cctrack.__file__).read_bytes()).hexdigest() in key
    assert ".".join(map(str, sys.version_info[:3])) in key and f"marshal{marshal.version}" in key
    code = "import cctrack, io, codecs; print(cctrack.cache_code_key())"
    a = _run_py(code, PYTHONUTF8="0", PYTHONCOERCECLOCALE="0", LC_ALL="C.UTF-8")
    b = _run_py(code, PYTHONUTF8="0", PYTHONCOERCECLOCALE="0", LC_ALL="C")
    assert a.returncode == 0 and b.returncode == 0, (a.stderr, b.stderr)
    enc = "import io, codecs; print(codecs.lookup(io.TextIOWrapper(io.BytesIO()).encoding).name)"
    ea = _run_py(enc, PYTHONUTF8="0", PYTHONCOERCECLOCALE="0", LC_ALL="C.UTF-8").stdout
    eb = _run_py(enc, PYTHONUTF8="0", PYTHONCOERCECLOCALE="0", LC_ALL="C").stdout
    if ea == eb:
        pytest.skip("this system's C and C.UTF-8 locales use the same encoding")
    assert a.stdout != b.stdout


def test_import_does_not_load_sqlite3(tmp_path):
    r = _run_py("import sys, cctrack; print('sqlite3' in sys.modules)")
    assert r.stdout.strip() == "False", r.stderr
    r = _run_py("import sys; sys.modules['sqlite3'] = None; import cctrack; print(cctrack.open_cache())",
                CCTRACK_CACHE_DIR=str(tmp_path / "c"))
    assert r.stdout.strip() == "None"
    lines = [ln for ln in r.stderr.splitlines() if ln.startswith("cctrack: cache")]
    assert len(lines) == 1 and "sqlite3" in lines[0]


def test_missing_sqlite3_in_process(monkeypatch, capsys, tmp_path):
    p = _claude_file(tmp_path)
    monkeypatch.setitem(sys.modules, "sqlite3", None)
    assert cctrack.open_cache() is None
    assert len(_stderr_notes(capsys)) == 1
    assert _canon(cctrack._parse_log_file_cached(None, "claude", p, want_stats=True)) == \
        _canon(_plain("claude", p))


def test_non_utf8_path_bypasses_the_store(tmp_path, capsys, store):
    raw = os.fsencode(tmp_path) + b"/caf\xe9.jsonl"
    try:
        with open(raw, "w") as f:
            f.write(claude_line(f"{DAY}T01:00:00Z", "r1") + "\n")
    except OSError:
        pytest.skip("file system rejects non-UTF-8 names")
    path = os.fsdecode(raw)
    _old(path)
    r = _cached(store, "claude", path)
    assert _canon(r) == _canon(_plain("claude", path)) and r["events"]
    store.flush()
    assert _rows(store) == 0 and store.enabled and _stderr_notes(capsys) == []


def test_tests_use_a_private_cache_dir(tmp_path):
    assert cctrack.cache_dir() == tmp_path / "cctrack-cache"
    assert os.environ["CCTRACK_CACHE_DIR"] == str(tmp_path / "cctrack-cache")
