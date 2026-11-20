"""Step 4: local transcripts and rollouts go through the parse cache.

Equivalence (cache off / cold / warm / rebuilt), what a run opens, mutations of the
corpus between runs, options, progress and timing. Subprocess tests use the harness's
launcher (`run_launched`), which logs every .jsonl/.jsonl.zst a run opens."""

import hashlib
import io
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

import cctrack

sys.path.insert(0, str(Path(__file__).parent))
from test_cache_equivalence import (  # noqa: E402
    SRC, TIMING_RE, VARIANTS, _stable, _utc_today, backdate, build_corpus, cache_db,
    local_log_files, modes, run_cctrack, run_launched, token_count)
from test_cache_store import _raw_frame_text  # noqa: E402
from test_detail import claude_line, codex_line, codex_usage  # noqa: E402


def _canon(x):
    return json.dumps(cctrack._tag_json(x))


def _agent(v):
    return "claude" if v.endswith("-claude") else "codex" if v.endswith("-codex") else "all"


def _cache_lines(stderr):
    return [ln for ln in stderr.splitlines() if ln.startswith("cctrack: cache")]


def _attempts(fn):
    """Run fn(attempt); redo once if the UTC date changed while it ran."""
    for attempt in range(2):
        start = _utc_today().date()
        fn(attempt)
        if _utc_today().date() == start:
            return
    pytest.fail("the UTC date changed during two consecutive attempts")


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    c = build_corpus(tmp_path_factory.mktemp("c4"), settle=False)
    backdate(c)
    return c


def _fresh(tmp_path):
    c = build_corpus(tmp_path, settle=False)
    t = backdate(c)
    return c, t


# ── T1 ────────────────────────────────────────────────────────────────────

def test_cache_modes_match_uncached(corpus):
    def go(attempt):
        for v, (vargs, venv) in VARIANTS.items():
            for m, margs in modes(corpus).items():
                home = corpus.root / f"c1-{attempt}" / f"{v}-{m}"
                every = local_log_files(corpus, _agent(v))

                def run(*flag):
                    return run_launched(corpus, [*vargs, *margs, *flag], venv, cache_home=home)
                off, cold, warm, rebuilt, warm2 = (
                    run("--no-cache"), run(), run(), run("--rebuild-cache"), run())
                key = (v, m)
                assert off.stdout == cold.stdout == warm.stdout == rebuilt.stdout == warm2.stdout, key
                assert off.opens == cold.opens == rebuilt.opens == every, key
                assert warm.opens == [] and warm2.opens == [], key
                for r in (off, cold, warm, rebuilt, warm2):
                    assert _cache_lines(r.stderr) == [], key
    _attempts(go)


# ── T2 ────────────────────────────────────────────────────────────────────

P1 = "local-home/.claude/projects/-repo"
CASES = ["1-append", "1b-append-new-mtime", "2-add", "3-delete-claude", "3-fork-child",
         "3-fork-parent", "4-same-size", "5-fork-child-added", "7-code-hash", "8-corrupt",
         "9-read-only", "10-truncate", "10s-truncate-same-mtime"]


def _rollout(c, suffix):
    (f,) = [p for p in (c.home / ".codex").rglob(f"*-{suffix}.jsonl")]
    return f


def _append(path, line, mtime_ns):
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    os.utime(path, ns=(mtime_ns, mtime_ns))


@pytest.mark.parametrize("name", CASES)
def test_mutation_then_warm_matches_uncached(tmp_path, name):
    if name == "9-read-only" and os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    c, t = _fresh(tmp_path)
    day1 = c.explore_date
    home = c.root / "xdg-cache"
    src = SRC
    if name == "7-code-hash":
        src = tmp_path / "src2"
    expect, notes, content = [], [], True
    all_opens = local_log_files(c)

    def run(argv, *flag, env=None):
        return run_launched(c, [*argv, *flag], env, cache_home=home, src=src)

    before = run(["--json"], "--no-cache").stdout
    cold = run([])
    assert cold.opens == all_opens and _cache_lines(cold.stderr) == []
    ro_dir = home / "cctrack"
    try:
        if name in ("1-append", "1b-append-new-mtime"):
            _append(c.root / P1 / "s1-resumed.jsonl",
                    claude_line(f"{day1}T04:00:00Z", "r7", sid="s1", inp=777),
                    t if name == "1-append" else t + 3 * 10**9)
            expect = [f"{P1}/s1-resumed.jsonl"]
        elif name == "2-add":
            p = c.root / P1 / "new.jsonl"
            p.write_text(claude_line(f"{day1}T04:00:00Z", "rn", sid="sn", inp=999) + "\n")
            os.utime(p, ns=(t, t))
            expect = [f"{P1}/new.jsonl"]
        elif name == "3-delete-claude":
            (c.root / P1 / "s1-resumed.jsonl").unlink()
        elif name == "3-fork-child":
            _rollout(c, "C").unlink()
        elif name == "3-fork-parent":
            _rollout(c, "P").unlink()
        elif name == "4-same-size":
            p = c.root / P1 / "lh.jsonl"
            text = p.read_text()
            new = text.replace('"input_tokens": 500', '"input_tokens": 600', 1)
            assert new != text and len(new) == len(text)
            p.write_text(new)
            os.utime(p, ns=(t + 5 * 10**9,) * 2)
            expect = [f"{P1}/lh.jsonl"]
        elif name == "5-fork-child-added":
            p = _rollout(c, "P").parent / f"rollout-{day1}T09-00-00-C2.jsonl"
            p.write_text("\n".join([
                codex_line(f"{day1}T09:00:00Z", "session_meta",
                           {"id": "C2", "session_id": "C2", "forked_from_id": "P", "cwd": "/w"}),
                codex_line(f"{day1}T09:00:01Z", "turn_context", {"model": "gpt-5.4"}),
                token_count(f"{day1}T09:00:02Z", codex_usage(1000, out=100),
                            codex_usage(1000, out=100)),
                token_count(f"{day1}T09:00:03Z", codex_usage(700, out=70),
                            codex_usage(1700, out=170)),
            ]) + "\n")
            os.utime(p, ns=(t, t))
            expect = [str(p.relative_to(c.root))]
        elif name == "7-code-hash":
            shutil.copytree(SRC, src)
            with open(src / "cctrack" / "__init__.py", "a", encoding="utf-8") as fh:
                fh.write("\n# step 4 code-hash mutation\n")
            expect, content = all_opens, False
        elif name == "8-corrupt":
            cache_db(home).write_bytes(b"garbage" * 1000)
            expect, content = all_opens, False
            notes = ["cctrack: cache was unreadable and has been reset"]
        elif name == "9-read-only":
            ro_dir.chmod(0o500)
            expect, content = all_opens, False
            notes = ["cctrack: cache unavailable"]
        elif name in ("10-truncate", "10s-truncate-same-mtime"):
            p = c.root / P1 / "s1.jsonl"
            lines = p.read_text().splitlines(keepends=True)
            p.write_text("".join(lines[:-1]))
            m = t + 7 * 10**9 if name == "10-truncate" else t
            os.utime(p, ns=(m, m))
            expect = [f"{P1}/s1.jsonl"]

        first = run([])
        assert first.opens == expect
        if notes:
            assert len(_cache_lines(first.stderr)) == 1
            assert _cache_lines(first.stderr)[0].startswith(notes[0])
        else:
            assert _cache_lines(first.stderr) == []
        for v in ("local", "local-claude", "local-codex"):
            vargs = VARIANTS[v][0]
            for m, margs in modes(c).items():
                if m not in ("report", "explore", "detail", "json"):
                    continue
                a = run([*vargs, *margs], "--no-cache")
                b = run([*vargs, *margs])
                assert a.stdout == b.stdout, (name, v, m)
        if content:
            after = run(["--json"], "--no-cache").stdout
            assert after != before
        again = run([])
        assert again.opens == (all_opens if name == "9-read-only" else [])
    finally:
        if ro_dir.exists():
            ro_dir.chmod(0o700)


# ── T3, T4 ────────────────────────────────────────────────────────────────

def _snapshot(d):
    return {p.name: (p.stat().st_size, p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
            for p in sorted(Path(d).iterdir())}


def test_no_cache_leaves_cache_untouched(corpus):
    home = corpus.root / "c3"
    run_launched(corpus, [], cache_home=home)
    snap = _snapshot(home / "cctrack")
    assert snap
    every = local_log_files(corpus)
    for argv, env in ((["--no-cache"], {}), ([], {"CCTRACK_CACHE": "0"}),
                      ([], {"CCTRACK_CACHE": " Off "})):
        r = run_launched(corpus, argv, env, cache_home=home)
        assert r.opens == every
        assert _snapshot(home / "cctrack") == snap
    fresh = corpus.root / "c3-fresh"
    run_launched(corpus, ["--no-cache"], cache_home=fresh)
    assert not fresh.exists()


def _entry_paths(home):
    con = sqlite3.connect(cache_db(home))
    try:
        return sorted(r[0] for r in con.execute("SELECT path FROM entries"))
    finally:
        con.close()


def test_rebuild_replaces_the_cache(tmp_path):
    c, _ = _fresh(tmp_path)
    home = c.root / "xdg-cache"
    run_launched(c, [], cache_home=home)
    gone = c.root / P1 / "s1-resumed.jsonl"
    gone.unlink()
    remaining = local_log_files(c)
    assert len(remaining) == 8
    r = run_launched(c, ["--rebuild-cache"], cache_home=home)
    assert r.opens == remaining and _cache_lines(r.stderr) == []
    assert _entry_paths(home) == sorted(str(c.root / p) for p in remaining)
    assert run_launched(c, [], cache_home=home).opens == []
    cache_db(home).write_bytes(b"garbage" * 1000)
    r = run_launched(c, ["--rebuild-cache"], cache_home=home)
    assert _cache_lines(r.stderr) == [] and r.opens == remaining
    assert run_launched(c, [], cache_home=home).opens == []


# ── in-process helpers ────────────────────────────────────────────────────

def _inproc(tmp_path, monkeypatch):
    c, t = _fresh(tmp_path)
    monkeypatch.setenv("HOME", str(c.home))
    monkeypatch.setenv("SANDY_HOME", str(c.home / ".sandy"))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(cctrack, "_read_zstd", _raw_frame_text)
    monkeypatch.setattr(cctrack, "_zstd_available", lambda: True)
    files = cctrack.discover_files(cctrack.default_dirs())
    cx = cctrack.discover_codex_files()
    return c, t, files, cx


def _count_reads(monkeypatch):
    reads = []
    real = cctrack._read_log_lines

    def wrapper(kind, path):
        reads.append(path)
        return real(kind, path)
    monkeypatch.setattr(cctrack, "_read_log_lines", wrapper)
    return reads


# ── T5 ────────────────────────────────────────────────────────────────────

def test_parse_through_cache_keeps_order_and_stats(tmp_path, monkeypatch):
    c, t, files, cx = _inproc(tmp_path, monkeypatch)
    assert files and cx
    store = cctrack.open_cache()
    assert store is not None

    def reference():
        fs, fsc = {}, {}
        ev = cctrack.parse_events(files, file_stats=fs)
        cev, sk = cctrack.parse_codex_events(cx, file_stats=fsc)
        return _canon([ev, fs]), _canon([cev, sk, fsc])

    def cached():
        fs, fsc = {}, {}
        ev = cctrack.parse_events(files, file_stats=fs, cache=store)
        cev, sk = cctrack.parse_codex_events(cx, file_stats=fsc, cache=store)
        return _canon([ev, fs]), _canon([cev, sk, fsc])

    ref = reference()
    reads = _count_reads(monkeypatch)
    assert cached() == ref
    assert len(reads) == len(files) + len(cx)
    store.flush()
    reads.clear()
    assert cached() == ref
    assert reads == []
    day = c.explore_date
    _append(files[-1], claude_line(f"{day}T05:00:00Z", "r-new", sid="snew", inp=321), t + 5 * 10**9)
    plain = [f for f in cx if not f.endswith(".zst")][-1]
    _append(plain, token_count(f"{day}T10:00:00Z", codex_usage(50, out=5), codex_usage(9999, out=999)),
            t + 5 * 10**9)
    ref = reference()
    reads.clear()
    assert cached() == ref
    assert len(reads) == 2
    assert _canon(cctrack.parse_events(files, cache=store)) == _canon(cctrack.parse_events(files))
    store.close()


# ── T6 ────────────────────────────────────────────────────────────────────

def test_stored_entries_equal_fresh_parse(tmp_path, monkeypatch):
    c, t, files, cx = _inproc(tmp_path, monkeypatch)
    store = cctrack.open_cache()
    cctrack.parse_events(files, cache=store)
    cctrack.parse_codex_events(cx, cache=store)
    store.close()
    con = sqlite3.connect(store.path)
    try:
        rows = con.execute("SELECT host, kind, path, code, payload FROM entries").fetchall()
    finally:
        con.close()
    assert len(rows) == len(files) + len(cx)
    for host, kind, path, code, payload in rows:
        assert host == "local"
        assert code == f"{store.code_key} {cctrack._cache_params('local', path, path, kind == 'claude')}"
        got = cctrack._decode_cache_payload(payload)
        fresh = cctrack._parse_log_file(kind, path, want_stats=True,
                                        agent_id_from_name=kind == "claude")
        assert _canon(got) == _canon(fresh), path
        for ev in got["events"]:
            # A per-file parse sets _unresolved_fork itself (the parent is in another file);
            # what dedup adds across files is _locations, and that must never be stored.
            assert "_locations" not in ev, path


# ── T8 ────────────────────────────────────────────────────────────────────

def test_zst_entry_needs_a_decoder(tmp_path):
    c, _ = _fresh(tmp_path)
    home = c.root / "xdg-cache"

    def go(attempt):
        h = home / str(attempt)
        cold = run_launched(c, ["--json"], cache_home=h, zstd="raw")
        warm = run_launched(c, ["--json"], cache_home=h, zstd="raw")
        assert warm.opens == [] and warm.stdout == cold.stdout
        for argv in (["--json"], []):
            off = run_launched(c, [*argv, "--no-cache"], cache_home=h, zstd="none")
            on = run_launched(c, argv, cache_home=h, zstd="none")
            assert on.stdout == off.stdout
            assert "skipped 1 compressed Codex rollout" in on.stderr
            assert "skipped 1 compressed Codex rollout" in off.stderr
        assert on.stdout != run_launched(c, [], cache_home=h, zstd="raw").stdout
        assert off.stdout != cold.stdout
    _attempts(go)


# ── T9 ────────────────────────────────────────────────────────────────────

def test_remote_runs_cache_only_local_files(tmp_path):
    c, _ = _fresh(tmp_path)
    home = c.root / "xdg-cache"

    def go(attempt):
        h = home / str(attempt)
        run_launched(c, ["--remote", "fakehost"], cache_home=h)
        con = sqlite3.connect(cache_db(h))
        try:
            assert con.execute("SELECT DISTINCT host FROM entries").fetchall() == [("local",)]
            paths = [r[0] for r in con.execute("SELECT path FROM entries")]
        finally:
            con.close()
        assert len(paths) == 9 and all(p.startswith(str(c.home)) for p in paths)
        assert not (c.remote_home / ".cache").exists()
        warm = run_launched(c, ["--remote", "fakehost", "--json"], cache_home=h)
        off = run_launched(c, ["--remote", "fakehost", "--json", "--no-cache"], cache_home=h)
        assert warm.stdout == off.stdout and warm.opens == []
    _attempts(go)


# ── options ───────────────────────────────────────────────────────────────

def test_cache_mode(monkeypatch):
    for v in (None, "", "1", "yes"):
        if v is None:
            monkeypatch.delenv("CCTRACK_CACHE", raising=False)
        else:
            monkeypatch.setenv("CCTRACK_CACHE", v)
        assert cctrack.cache_mode() == "on"
    for v in ("0", "no", "FALSE", " off "):
        monkeypatch.setenv("CCTRACK_CACHE", v)
        assert cctrack.cache_mode() == "off"
        assert cctrack.cache_mode(False, True) == "rebuild"
    for v in ("1", "0", ""):
        monkeypatch.setenv("CCTRACK_CACHE", v)
        assert cctrack.cache_mode(True, False) == "off"
        assert cctrack.cache_mode(True, True) == "off"


def test_cache_flags_are_exclusive(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["cctrack", "--no-cache", "--rebuild-cache"])
    with pytest.raises(SystemExit) as e:
        cctrack.main()
    assert e.value.code == 2
    assert "not allowed with argument --no-cache" in capsys.readouterr().err
    assert not cctrack.cache_dir().exists()


def test_help_lists_cache_options(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["cctrack", "--help"])
    with pytest.raises(SystemExit) as e:
        cctrack.main()
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert "--no-cache" in out and "--rebuild-cache" in out and "CCTRACK_CACHE=0" in out


def test_hook_commands_do_not_open_the_cache(monkeypatch):
    monkeypatch.setattr(cctrack, "install_hook", lambda *a, **k: None)
    monkeypatch.setattr(cctrack, "uninstall_hook", lambda *a, **k: None)
    for flag in ("--install-hook", "--uninstall-hook"):
        monkeypatch.setattr(sys, "argv", ["cctrack", flag])
        cctrack.main()
        assert not cctrack.cache_dir().exists()


def test_interrupted_run_keeps_parsed_files(tmp_path, monkeypatch, capsys):
    c, t, files, cx = _inproc(tmp_path, monkeypatch)
    monkeypatch.setattr(cctrack, "CACHE_FLUSH_SECONDS", 3600)
    monkeypatch.setattr(cctrack, "CACHE_FLUSH_BYTES", 1 << 40)

    def boom(*a, **k):
        raise KeyboardInterrupt
    monkeypatch.setattr(cctrack, "parse_codex_events", boom)
    monkeypatch.setenv("CCTRACK_TIMING", "1")
    monkeypatch.setattr(sys, "argv", ["cctrack"])
    with pytest.raises(KeyboardInterrupt):
        cctrack.main()
    err = capsys.readouterr().err
    con = sqlite3.connect(cctrack.cache_dir() / cctrack.CACHE_FILENAME)
    try:
        assert con.execute("SELECT count(*) FROM entries").fetchone()[0] == 5
    finally:
        con.close()
    lines = err.splitlines()
    assert any(ln.startswith("[timing] codex parse") and "(interrupted)" in ln for ln in lines)
    assert not any(ln.startswith("[timing] cache") and "(interrupted)" in ln for ln in lines)


# ── progress and timing ───────────────────────────────────────────────────

def test_progress_reports_changed_files(tmp_path, monkeypatch):
    c, t, files, cx = _inproc(tmp_path, monkeypatch)
    cl = [f for f in files if "/.sandy/" not in f][:3]
    rl = [f for f in cx if not f.endswith(".zst")][:2]
    assert len(cl) == 3 and len(rl) == 2
    monkeypatch.setattr(cctrack, "progress_enabled", lambda: True)
    store = cctrack.open_cache()

    def run(fn, *a, **k):
        buf = io.StringIO()
        with monkeypatch.context() as m:
            m.setattr(sys, "stderr", buf)
            fn(*a, **k)
        return buf.getvalue()

    assert "3 changed of 3 files" in run(cctrack.parse_events, cl, cache=store)
    assert "2 changed of 2 files" in run(cctrack.parse_codex_events, rl, cache=store)
    store.flush()
    assert "0 changed of 3 files" in run(cctrack.parse_events, cl, cache=store)
    assert "0 changed of 2 files" in run(cctrack.parse_codex_events, rl, cache=store)
    assert "changed" not in run(cctrack.parse_events, cl)
    assert "changed" not in run(cctrack.parse_codex_events, rl)
    store.close()


def _timing(stderr):
    ms = [TIMING_RE.match(ln) for ln in stderr.splitlines() if ln.startswith("[timing]")]
    return [m for m in ms if m]


def test_timing_reports_cache_use(tmp_path):
    c, _ = _fresh(tmp_path)
    home = c.root / "xdg-cache"
    env = {"CCTRACK_TIMING": "1"}

    def details(r):
        ms = _timing(r.stderr)
        return ms, {m["name"]: m["detail"] or "" for m in ms if not m["indent"]}

    ms, d = details(run_launched(c, [], env, cache_home=home))
    assert "cache" in d
    assert d["claude parse"].endswith("; cache: 0 reused, 5 read")
    assert d["codex parse"].endswith("; cache: 0 reused, 4 read")
    ms, d = details(run_launched(c, [], env, cache_home=home))
    assert d["claude parse"].endswith("; cache: 5 reused, 0 read")
    assert d["codex parse"].endswith("; cache: 4 reused, 0 read")
    ms, d = details(run_launched(c, ["--no-cache"], env, cache_home=home))
    assert "cache" not in d
    assert not any("cache:" in (m["detail"] or "") for m in ms)
    assert [m["name"] for m in ms[-2:]] == ["report", "total"]


def test_reference_runs_are_uncached(tmp_path):
    c = build_corpus(tmp_path, settle=False)
    backdate(c)
    run_cctrack(c, [])
    run_cctrack(c, [])
    assert not (c.root / "xdg-cache").exists()
