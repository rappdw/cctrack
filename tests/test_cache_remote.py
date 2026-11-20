"""Step 6: --remote hosts (protocol v2) and local hook files go through the parse cache.

Subprocess tests use the harness's launcher (`run_launched(..., remote_zstd="raw")`), which
logs every .jsonl/.jsonl.zst the local run AND the program on the fake remote opens.
"Remote note" is the detail of the top-level `remote fakehost` timing line."""

import json
import os
import random
import re
import shutil
import sqlite3
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import cctrack

sys.path.insert(0, str(Path(__file__).parent))
from test_cache_equivalence import (  # noqa: E402
    SRC, TIMING_RE, VARIANTS, _utc_today, backdate, build_corpus, cache_db, modes,
    python3_calls, remote_listing, run_cctrack, run_launched, ssh_counter, token_count)
from test_cache_local import (  # noqa: E402
    _agent, _append, _attempts, _cache_lines, _canon, _fresh, _rollout)
from test_cache_store import _raw_frame_text  # noqa: E402
from test_detail import claude_line, codex_line, codex_usage  # noqa: E402
from test_remote_parse import _fake_ssh  # noqa: E402

RP = "remote-home/.claude/projects/-repo"
RUN = dict(remote_zstd="raw")


def _remote_note(stderr):
    for ln in stderr.splitlines():
        m = TIMING_RE.match(ln) if ln.startswith("[timing]") else None
        if m and not m["indent"] and m["name"] == "remote fakehost":
            return m["detail"] or ""
    return None


def _timing_names(stderr):
    return [m["name"] for ln in stderr.splitlines() if ln.startswith("[timing]")
            for m in [TIMING_RE.match(ln)] if m]


def remote_log_files(c, agent="all"):
    """Sorted paths (relative to the corpus root) of every log file the remote reads."""
    out = []
    for f in c.remote_home.rglob("*"):
        if not f.is_file() or not (f.name.endswith(".jsonl") or f.name.endswith(".jsonl.zst")):
            continue
        rel = f.relative_to(c.root)
        if agent == "claude" and "projects" not in rel.parts and "cctrack" not in rel.parts:
            continue
        if agent == "codex" and ".codex" not in rel.parts:
            continue
        out.append(str(rel))
    return sorted(out)


def _n(agent):
    return {"all": 13, "claude": 9, "codex": 4}[agent]


def _rows_of(home, where):
    con = sqlite3.connect(cache_db(home))
    try:
        return con.execute(f"SELECT host, kind, path, code, payload FROM entries WHERE {where}"
                           ).fetchall()
    finally:
        con.close()


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    c = build_corpus(tmp_path_factory.mktemp("c6"), settle=False)
    backdate(c)
    return c


# ── R1 ────────────────────────────────────────────────────────────────────

def test_remote_cache_modes_match_uncached(corpus):
    def go(attempt):
        for v in ("remote", "remote-claude", "remote-codex", "remote-raw"):
            vargs, venv = VARIANTS[v]
            n = _n(_agent(v))
            for m, margs in modes(corpus).items():
                home = corpus.root / f"r1-{attempt}" / f"{v}-{m}"
                key = (v, m)

                def run(*flag):
                    return run_launched(corpus, [*vargs, *margs, *flag],
                                        {**venv, "CCTRACK_TIMING": "1"}, cache_home=home, **RUN)
                off, cold, warm, rebuilt, warm2 = (
                    run("--no-cache"), run(), run(), run("--rebuild-cache"), run())
                runs = (off, cold, warm, rebuilt, warm2)
                assert len({r.stdout for r in runs}) == 1, key
                local_every = _local_files(corpus, _agent(v))
                assert off.opens == cold.opens == rebuilt.opens == local_every, key
                assert warm.opens == [] and warm2.opens == [], key
                if v != "remote-raw":
                    every = remote_log_files(corpus, _agent(v))
                    assert len(every) == n, key
                    for r in (off, cold, rebuilt):
                        assert r.remote_opens == every, key
                    assert warm.remote_opens == [] and warm2.remote_opens == [], key
                    assert _remote_note(off.stderr).endswith("received") and \
                        "cache:" not in _remote_note(off.stderr), key
                    for r in (cold, rebuilt):
                        assert _remote_note(r.stderr).endswith(f"; cache: 0 reused, {n} read"), key
                    for r in (warm, warm2):
                        assert _remote_note(r.stderr).endswith(f"; cache: {n} reused, 0 read"), key
                else:
                    for r in runs:
                        assert _remote_note(r.stderr) == "raw stream", key
                for r in runs:
                    assert _cache_lines(r.stderr) == [], key
    _attempts(go)


def _local_files(c, agent):
    from test_cache_equivalence import local_log_files
    return local_log_files(c, agent)


# ── R2 ────────────────────────────────────────────────────────────────────

# name: (remote opens, local opens, (reused, read), content changes)
R2 = {
    "r1-append": ([f"{RP}/s1-resumed.jsonl"], [], (12, 1), True),
    "r2-add": ([f"{RP}/new.jsonl"], [], (13, 1), True),
    "r3-fork-child": ([], [], (12, 0), True),
    "r4-same-size": ([f"{RP}/rh.jsonl"], [], (12, 1), True),
    "r5-fork-child-added": (None, [], (13, 1), True),
    "r6-rsync-to-remote": ([f"{RP}/lh.jsonl"], [], (13, 1), True),
    "r6b-rsync-to-local": ([], ["local-home/.claude/projects/-repo/rh.jsonl"], (13, 0), True),
    "r-hook-append": (None, [], (12, 1), True),
    "r7-code-hash": ("all", "all", (0, 13), False),
    "r10-truncate": ([f"{RP}/s1.jsonl"], [], (12, 1), True),
}


@pytest.mark.parametrize("name", list(R2))
def test_remote_mutation_then_warm_matches_uncached(tmp_path, name):
    def go(attempt):
        _r2(tmp_path / str(attempt), name)
    _attempts(go)


def _r2(tmp_path, name):
    tmp_path.mkdir()
    c, t = _fresh(tmp_path)
    day1 = c.explore_date
    home = c.root / "xdg-cache"
    src = SRC
    bin_dir, log = ssh_counter(tmp_path, c)
    path_env = os.pathsep.join([str(bin_dir), str(c.bin_dir), os.environ["PATH"]])
    exp_remote, exp_local, (reused, read), content = R2[name]
    remote_dir = c.remote_home / ".claude" / "cctrack"

    def run(argv, *flag, env=None, s=None):
        return run_launched(c, [*argv, *flag], env, cache_home=home, src=s or src, **RUN)

    rem = ["--remote", "fakehost"]
    before = run([*rem, "--json"], "--no-cache").stdout
    cold = run(rem)
    assert cold.remote_opens == remote_log_files(c) and _cache_lines(cold.stderr) == []
    if name in ("r1-append",):
        _append(c.remote_home / ".claude/projects/-repo/s1-resumed.jsonl",
                claude_line(f"{day1}T04:00:00Z", "r7", sid="s1", inp=777), t)
    elif name == "r2-add":
        p = c.remote_home / ".claude/projects/-repo/new.jsonl"
        p.write_text(claude_line(f"{day1}T04:00:00Z", "rn", sid="sn", inp=999) + "\n")
        os.utime(p, ns=(t, t))
    elif name == "r3-fork-child":
        _rollout_remote(c, "C").unlink()
    elif name == "r4-same-size":
        p = c.remote_home / ".claude/projects/-repo/rh.jsonl"
        text = p.read_text()
        new = text.replace('"input_tokens": 500', '"input_tokens": 600', 1)
        assert new != text and len(new) == len(text)
        p.write_text(new)
        os.utime(p, ns=(t + 5 * 10**9,) * 2)
    elif name == "r5-fork-child-added":
        p = _rollout_remote(c, "P").parent / f"rollout-{day1}T09-00-00-C2.jsonl"
        p.write_text("\n".join([
            codex_line(f"{day1}T09:00:00Z", "session_meta",
                       {"id": "C2", "session_id": "C2", "forked_from_id": "P", "cwd": "/w"}),
            codex_line(f"{day1}T09:00:01Z", "turn_context", {"model": "gpt-5.4"}),
            token_count(f"{day1}T09:00:02Z", codex_usage(1000, out=100), codex_usage(1000, out=100)),
            token_count(f"{day1}T09:00:03Z", codex_usage(700, out=70), codex_usage(1700, out=170)),
        ]) + "\n")
        os.utime(p, ns=(t, t))
        exp_remote = [str(p.relative_to(c.root))]
    elif name == "r6-rsync-to-remote":
        shutil.copy2(c.home / ".claude/projects/-repo/lh.jsonl",
                     c.remote_home / ".claude/projects/-repo/lh.jsonl")
    elif name == "r6b-rsync-to-local":
        shutil.copy2(c.remote_home / ".claude/projects/-repo/rh.jsonl",
                     c.home / ".claude/projects/-repo/rh.jsonl")
    elif name == "r-hook-append":
        p = remote_dir / f"statusline-{day1}.jsonl"
        _append(p, json.dumps({"_ts": f"{day1}T05:00:00+00:00", "session_id": "s1",
                               "cost_usd": 0.90, "model": "claude-opus-5-5"}), t + 3 * 10**9)
        exp_remote = [str(p.relative_to(c.root))]
    elif name == "r7-code-hash":
        src = tmp_path / "src2"
        shutil.copytree(SRC, src)
        with open(src / "cctrack" / "__init__.py", "a", encoding="utf-8") as fh:
            fh.write("\n# step 6 code-hash mutation\n")
        exp_remote, exp_local = remote_log_files(c), _local_files(c, "all")
    elif name == "r10-truncate":
        p = c.remote_home / ".claude/projects/-repo/s1.jsonl"
        lines = p.read_text().splitlines(keepends=True)
        p.write_text("".join(lines[:-1]))
        os.utime(p, ns=(t, t))
    listing = remote_listing(c)

    first = run(rem, env={"CCTRACK_TIMING": "1", "PATH": path_env})
    assert first.remote_opens == exp_remote, name
    assert first.opens == exp_local, name
    assert _remote_note(first.stderr).endswith(f"; cache: {reused} reused, {read} read"), name
    assert python3_calls(log) == 1 and _cache_lines(first.stderr) == [], name
    for m, margs in modes(c).items():
        if m not in ("report", "explore", "detail", "json"):
            continue
        a = run([*rem, *margs], "--no-cache")
        b = run([*rem, *margs])
        assert a.stdout == b.stdout, (name, m)
    if name in ("r3-fork-child", "r5-fork-child-added"):
        a = run(["--agent", "codex", *rem, "--json"], "--no-cache")
        assert a.stdout == run(["--agent", "codex", *rem, "--json"]).stdout
    if content:
        assert run([*rem, "--json"], "--no-cache").stdout != before, name
    again = run(rem, env={"CCTRACK_TIMING": "1"})
    assert again.remote_opens == [], name
    total = reused + read if name != "r7-code-hash" else 13
    assert _remote_note(again.stderr).endswith(f"; cache: {total} reused, 0 read"), name
    assert remote_listing(c) == listing, name


def _rollout_remote(c, suffix):
    (f,) = [p for p in (c.remote_home / ".codex").rglob(f"*-{suffix}.jsonl")]
    return f


# ── R3 ────────────────────────────────────────────────────────────────────

def test_remote_host_unmodified(tmp_path):
    c, _ = _fresh(tmp_path)
    home = c.root / "xdg-cache"
    before = remote_listing(c)
    rem = ["--remote", "fakehost"]
    run_launched(c, rem, cache_home=home, **RUN)
    run_launched(c, rem, cache_home=home, **RUN)
    run_launched(c, [*rem, "--rebuild-cache"], cache_home=home, **RUN)
    run_launched(c, [*rem, "--no-cache"], cache_home=home, **RUN)
    run_launched(c, [*rem, "--json"], cache_home=home, **RUN)
    assert remote_listing(c) == before
    assert not (c.remote_home / ".cache").exists()


# ── R4 ────────────────────────────────────────────────────────────────────

def test_unsuppliable_hit_refetches_once(tmp_path):
    c, _ = _fresh(tmp_path)
    home = c.root / "xdg-cache"
    bin_dir, log = ssh_counter(tmp_path, c)
    env = {"CCTRACK_TIMING": "1", "PATH": os.pathsep.join([str(bin_dir), str(c.bin_dir),
                                                           os.environ["PATH"]])}
    rem = ["--remote", "fakehost", "--json"]
    run_launched(c, rem, cache_home=home, **RUN)
    con = sqlite3.connect(cache_db(home))
    try:
        con.execute("UPDATE entries SET payload = X'00' WHERE rowid = (SELECT rowid FROM entries "
                    "WHERE host = 'remote:fakehost' AND kind = 'claude' ORDER BY path LIMIT 1)")
        con.commit()
    finally:
        con.close()
    log.unlink(missing_ok=True)
    r = run_launched(c, rem, env, cache_home=home, **RUN)
    off = run_launched(c, [*rem, "--no-cache"], cache_home=home, **RUN)
    assert r.stdout == off.stdout
    lines = _cache_lines(r.stderr)
    assert len(lines) == 1 and lines[0].startswith(
        "cctrack: cache couldn't supply fakehost's unchanged files")
    assert python3_calls(log) == 2
    assert _remote_note(r.stderr).endswith("; refetched")
    log.unlink()
    nxt = run_launched(c, rem, env, cache_home=home, **RUN)
    assert python3_calls(log) == 1 and _cache_lines(nxt.stderr) == []
    assert _remote_note(nxt.stderr).endswith("; cache: 13 reused, 0 read")


# ── R5 ────────────────────────────────────────────────────────────────────

def test_unreachable_host_same_warning_and_report(tmp_path):
    c, _ = _fresh(tmp_path)
    home = c.root / "xdg-cache"
    rem = ["--remote", "fakehost"]
    run_launched(c, rem, cache_home=home, **RUN)
    ok = run_launched(c, rem, cache_home=home, **RUN).stdout
    bad = tmp_path / "bad-bin"
    bad.mkdir()
    (bad / "ssh").write_text("#!/bin/sh\necho 'ssh: refused' >&2\nexit 255\n")
    (bad / "ssh").chmod(0o755)
    env = {"CCTRACK_TIMING": "1", "PATH": f"{bad}{os.pathsep}{os.environ['PATH']}"}
    n_before = len(_rows_of(home, "host = 'remote:fakehost'"))
    assert n_before == 13
    cached = run_launched(c, rem, env, cache_home=home, **RUN)
    off = run_launched(c, [*rem, "--no-cache"], env, cache_home=home, **RUN)
    assert cached.stdout == off.stdout and cached.stdout != ok
    for r in (cached, off):
        assert "Warning: SSH to fakehost failed" in r.stderr
        assert _remote_note(r.stderr) == "failed"
    assert len(_rows_of(home, "host = 'remote:fakehost'")) == n_before


# ── R6 ────────────────────────────────────────────────────────────────────

def test_raw_fallback_stays_uncached(tmp_path):
    c, _ = _fresh(tmp_path)
    rem = ["--remote", "fakehost", "--json"]
    old = tmp_path / "py39"
    old.write_text("#!/bin/sh\necho 'CCTRACK_REMOTE_UNSUPPORTED 3.9'\n")
    old.chmod(0o755)
    env2 = {"HOME": str(c.remote_home), "SANDY_HOME": str(c.remote_home / ".sandy"),
            "PYTHONPATH": str(c.root / "remote-site")}
    (tmp_path / "old").mkdir()
    old_bin = _fake_ssh(tmp_path / "old", env2, python=str(old))
    cases = [({"CCTRACK_REMOTE_PARSE": "0"}, None),
             ({"PATH": f"{old_bin}{os.pathsep}{os.environ['PATH']}"}, "cctrack needs 3.10+")]
    for i, (env, msg) in enumerate(cases):
        home = c.root / f"xdg-{i}"
        cold = run_launched(c, rem, env, cache_home=home, **RUN)
        warm = run_launched(c, rem, env, cache_home=home, **RUN)
        off = run_launched(c, [*rem, "--no-cache"], env, cache_home=home, **RUN)
        assert cold.stdout == warm.stdout == off.stdout
        assert _rows_of(home, "host LIKE 'remote:%'") == []
        assert _rows_of(home, "host = 'local'")
        if msg:
            assert msg in cold.stderr


# ── R7 ────────────────────────────────────────────────────────────────────

def test_stored_remote_entries_equal_fresh_parse(tmp_path, monkeypatch):
    c, _ = _fresh(tmp_path)
    home = c.root / "xdg-cache"
    run_launched(c, ["--remote", "fakehost"], cache_home=home, **RUN)
    rows = _rows_of(home, "host = 'remote:fakehost'")
    assert len(rows) == 13 and all(r[2].startswith(str(c.remote_home)) for r in rows)
    monkeypatch.setattr(cctrack, "_read_zstd", _raw_frame_text)
    monkeypatch.setattr(cctrack, "_zstd_available", lambda: True)
    import hashlib
    sha = hashlib.sha256((SRC / "cctrack" / "__init__.py").read_bytes()).hexdigest()
    params = cctrack._remote_cache_params("fakehost", cctrack._parse_env_key())
    kinds = set()
    for _, kind, path, code, blob in rows:
        kinds.add(kind)
        assert code.startswith(sha + " ") and code.endswith(" " + params)
        payload = cctrack._decode_cache_payload(blob)
        if kind == "hook":
            fresh = {"events": cctrack._hook_rows_from_blob_text(
                "".join(cctrack._read_text_lines(path))), "stats": None}
        else:
            fresh = cctrack._parse_log_file(kind, path, source="fakehost",
                                            label=f"fakehost:{path}", want_stats=True)
            assert not any("_locations" in e for e in payload["events"]), path
        assert _canon(payload) == _canon(fresh), path
    assert kinds == {"claude", "codex", "hook"}


# ── R8 ────────────────────────────────────────────────────────────────────

def test_remote_named_local_does_not_clash(tmp_path):
    c, _ = _fresh(tmp_path)
    home = c.root / "xdg-cache"
    (tmp_path / "self").mkdir()
    selfbin = _fake_ssh(tmp_path / "self", {"HOME": str(c.home), "SANDY_HOME": str(c.home / ".sandy"),
                                            "PYTHONPATH": str(c.root / "remote-site")})
    env = {"PATH": f"{selfbin}{os.pathsep}{os.environ['PATH']}", "CCTRACK_TIMING": "1"}
    rem = ["--remote", "local"]
    run_launched(c, rem, env, cache_home=home, **RUN)
    warm = run_launched(c, rem, env, cache_home=home, **RUN)
    notes = {m["name"]: m["detail"] or "" for ln in warm.stderr.splitlines()
             if ln.startswith("[timing]") for m in [TIMING_RE.match(ln)] if m and not m["indent"]}
    assert notes["claude parse"].endswith("; cache: 5 reused, 0 read")
    assert notes["hook read"].endswith("; cache: 3 reused, 0 read")
    assert notes["remote local"].endswith("; cache: 12 reused, 0 read")
    assert warm.opens == []
    off = run_launched(c, [*rem, "--no-cache"], env, cache_home=home, **RUN)
    assert warm.stdout == off.stdout
    local = {r[2] for r in _rows_of(home, "host = 'local'")}
    remote = {r[2] for r in _rows_of(home, "host = 'remote:local'")}
    assert local == remote and len(local) == 12


# ── R9 / R10 / R11 / R13 (in-process remote collector) ────────────────────

def _remote_side(tmp_path, monkeypatch):
    c, t = _fresh(tmp_path)
    monkeypatch.setenv("HOME", str(c.remote_home))
    monkeypatch.setenv("SANDY_HOME", str(c.remote_home / ".sandy"))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(cctrack, "CCTRACK_DIR", c.remote_home / ".claude" / "cctrack")
    monkeypatch.setattr(cctrack, "_read_zstd", _raw_frame_text)
    monkeypatch.setattr(cctrack, "_zstd_available", lambda: True)
    return c, t


def _manifest(data):
    return {"rkey": data["rkey"],
            "files": {k: {it[0]: [it[1], it[2]] for it in items}
                      for k, items in data["files"].items()}}


def test_remote_collect_hits_need_rkey_sig_and_decoder(tmp_path, monkeypatch):
    c, t = _remote_side(tmp_path, monkeypatch)
    cold = cctrack._remote_collect_files("fakehost", True, True)
    assert all(len(it) == 5 and it[3] == "s" for items in cold["files"].values() for it in items)
    assert {k: len(v) for k, v in cold["files"].items()} == {"claude": 5, "codex": 4, "hook": 4}
    man = _manifest(cold)
    reads = []
    for name in ("_read_log_lines", "_read_text_lines"):
        real = getattr(cctrack, name)
        monkeypatch.setattr(cctrack, name, lambda *a, _r=real, **k: (reads.append(a), _r(*a, **k))[1])
    warm = cctrack._remote_collect_files("fakehost", True, True, manifest=man)
    assert all(it[3] == "h" and len(it) == 4 for items in warm["files"].values() for it in items)
    assert reads == [] and sum(v[0] for v in warm["counts"].values()) == 13
    assert [it[0] for it in warm["files"]["claude"]] == [it[0] for it in cold["files"]["claude"]]
    other = cctrack._remote_collect_files("fakehost", True, True, manifest={**man, "rkey": "other"})
    assert all(len(it) == 5 for items in other["files"].values() for it in items)
    path = cold["files"]["claude"][2][0]
    for idx in (1, 2):
        m2 = json.loads(json.dumps(man))
        m2["files"]["claude"][path][idx - 1 if idx == 1 else 1] += 1
        got = cctrack._remote_collect_files("fakehost", True, True, manifest=m2)
        assert [it[0] for it in got["files"]["claude"] if it[3] != "h"] == [path]
        assert all(it[3] == "h" for k in ("codex", "hook") for it in got["files"][k])
    monkeypatch.setattr(cctrack, "_zstd_available", lambda: False)
    monkeypatch.setattr(cctrack, "_read_zstd", lambda p: None)
    nz = cctrack._remote_collect_files("fakehost", True, True, manifest=man)
    assert nz["zst_skipped"] == 1 and len(nz["files"]["codex"]) == 3
    assert all(it[3] == "h" for it in nz["files"]["codex"])


def test_remote_collect_marks_racy_and_changing_files(tmp_path, monkeypatch):
    c, t = _remote_side(tmp_path, monkeypatch)
    proj = c.remote_home / ".claude" / "projects" / "-repo"
    racy, changing = proj / "rh.jsonl", proj / "s1.jsonl"
    now = time.time_ns()
    os.utime(racy, ns=(now, now))
    real = cctrack._read_log_lines

    def wrapper(kind, path):
        lines = real(kind, path)
        if path == str(changing):
            _append(changing, "{}", t + 99 * 10**9)
        return lines
    monkeypatch.setattr(cctrack, "_read_log_lines", wrapper)
    data = cctrack._remote_collect_files("fakehost", True, True)
    flags = {it[0]: it[3] for items in data["files"].values() for it in items}
    assert flags[str(racy)] == "n" and flags[str(changing)] == "n"
    assert sorted(set(flags.values())) == ["n", "s"] and list(flags.values()).count("n") == 2


import time  # noqa: E402


def test_remote_items_are_per_file_and_assemble_to_v1(tmp_path, monkeypatch):
    c, _ = _remote_side(tmp_path, monkeypatch)
    data = cctrack._remote_collect_files("fakehost", True, True)
    items = [it for k in ("claude", "codex") for it in data["files"][k]]
    events = [e for it in items for e in it[4]["events"]]
    assert not any("_locations" in e or "_fork_history" in e for e in events)
    assert sum(1 for it in data["files"]["claude"] for e in it[4]["events"]
               if "r2" in json.dumps(e, default=str)) >= 2
    got = cctrack._assemble_remote_bundle("fakehost", data, None)
    for kind, parser, files in (("claude", cctrack.parse_lines,
                                 cctrack.discover_files(cctrack.default_dirs())),
                                ("codex", cctrack.parse_codex_lines, cctrack.discover_codex_files())):
        stats = {}
        ref = cctrack._parse_remote_files(
            "fakehost", [(p, l) for p in files if (l := cctrack._read_log_lines(kind, p)) is not None],
            parser, stats)
        assert _canon(got[kind]) == _canon(ref) and _canon(got[f"{kind}_stats"]) == _canon(stats)
    ref_hook = cctrack.parse_hook_blob(
        cctrack._remote_hook_blob(cctrack.discover_hook_data_dirs()))
    assert _canon(got["hook"]) == _canon(ref_hook)
    v1 = cctrack._remote_collect("fakehost", True, True)
    for k in ("claude", "codex", "claude_stats", "codex_stats", "hook"):
        assert _canon(v1[k]) == _canon(got[k]), k
    assert {"cache_counts", "_cached", "_local_merge"}.isdisjoint(v1)


# ── R12 ───────────────────────────────────────────────────────────────────

def test_remote_hook_per_file_equals_blob():
    rnd = random.Random(7)
    seps = (cctrack.MONTHLY_FILE_SEP, cctrack.BASELINE_FILE_SEP)
    weird = ["", "", "", " ", "\x1c", "\x0b", "\x85", cctrack.HOOK_FILE_SEP, "\r"]

    def v1_blob(stat_texts, monthly, base):
        parts = [sep + "\n" + t for texts, sep in ((stat_texts, cctrack.HOOK_FILE_SEP),
                                                   (monthly, cctrack.MONTHLY_FILE_SEP),
                                                   (base, cctrack.BASELINE_FILE_SEP)) for t in texts]
        return "\n".join(parts)

    def line(sid, h, cost, w):
        rec = {"_ts": f"2026-11-1{h % 3}T0{h % 10}:00:00{rnd.choice(['Z', '+00:00', '+02:00'])}",
               "session_id": sid + w, "cost_usd": cost, "model": "m"}
        return json.dumps(rec, ensure_ascii=rnd.random() < 0.5)

    def file_text():
        ls = []
        for _ in range(rnd.randint(0, 10)):
            r = rnd.random()
            if r < 0.1:
                ls.append("{not json")
            elif r < 0.15:
                ls.append("")
            else:
                ls.append(line(rnd.choice("abc"), rnd.randint(0, 5),
                               rnd.choice([0.0, 0.5, None, 1.25]), rnd.choice(weird)))
        text = "\n".join(ls)
        if rnd.random() < 0.7:
            text += "\n"
        if rnd.random() < 0.03:
            text += rnd.choice(seps)
        return text

    def per_file(stat_texts, monthly, base, naive_rows):
        rows = [r for t in stat_texts for r in cctrack._hook_rows_from_blob_text(t)]
        rows.sort(key=lambda r: (r["session_id"], r["ts"]))
        b = v1_blob([], monthly, base)
        _, m, bl = cctrack.parse_hook_blob(b) if b else ([], {}, {})
        return [rows, m, bl]

    skipped = naive_differs = 0
    for _ in range(2000):
        st = [file_text() for _ in range(rnd.randint(0, 5))]
        mo = [json.dumps({"days": {f"2026-08-0{j}": {"cost": j, "sessions": 1}}})
              for j in range(rnd.randint(0, 2))]
        ba = [json.dumps({"s": {"date": "2026-08-31", "cost_usd": 3.0}})] if rnd.random() < 0.5 else []
        b = v1_blob(st, mo, ba)
        ref = list(cctrack.parse_hook_blob(b)) if b else [[], {}, {}]
        got = per_file(st, mo, ba, None)
        if any(cctrack._hook_text_breaks_blob(t) for t in st):
            skipped += 1
            naive_differs += json.dumps(got, sort_keys=True) != json.dumps(ref, sort_keys=True)
            continue
        assert json.dumps(got, sort_keys=True) == json.dumps(ref, sort_keys=True)
    assert skipped >= 100 and naive_differs >= 1


# ── R13 ───────────────────────────────────────────────────────────────────

def test_remote_hook_separator_falls_back(tmp_path, monkeypatch):
    c, t = _remote_side(tmp_path, monkeypatch)
    d5 = (_utc_today().date() - timedelta(days=5)).isoformat()
    f = c.remote_home / ".claude" / "cctrack" / f"statusline-{d5}.jsonl"
    f.write_text(json.dumps({"_ts": f"{d5}T01:00:00+00:00", "session_id": "x" + cctrack.MONTHLY_FILE_SEP,
                             "cost_usd": 1.0, "model": "m"}) + "\n")
    os.utime(f, ns=(t, t))
    dirs = cctrack.discover_hook_data_dirs()
    data = cctrack._remote_collect_files("fakehost", True, True)
    assert "hook" not in data["files"] and "hook_extra" not in data
    assert _canon(data["hook"]) == _canon(cctrack.parse_hook_blob(cctrack._remote_hook_blob(dirs)))
    assert data["counts"]["hook"] == [0, 5]
    home = c.root / "xdg-cache"
    rem = ["--remote", "fakehost", "--json"]
    cold = run_launched(c, rem, cache_home=home, **RUN)
    warm = run_launched(c, rem, cache_home=home, **RUN)
    off = run_launched(c, [*rem, "--no-cache"], cache_home=home, **RUN)
    assert cold.stdout == warm.stdout == off.stdout
    assert _rows_of(home, "host = 'remote:fakehost' AND kind = 'hook'") == []


# ── R14 ───────────────────────────────────────────────────────────────────

def test_hook_line_splitting_kept_per_side(tmp_path, monkeypatch):
    d = tmp_path / "cctrack"
    d.mkdir()
    f = d / "statusline-2026-11-10.jsonl"
    text = json.dumps({"_ts": "2026-11-10T01:00:00Z", "session_id": "a b", "cost_usd": 1.0},
                      ensure_ascii=False) + "\n"
    f.write_text(text, encoding="utf-8")
    t = time.time_ns() - 3600 * 10**9
    os.utime(f, ns=(t, t))
    store = cctrack.open_cache()
    assert len(cctrack.parse_hook_rows(d)) == 1
    assert len(cctrack.parse_hook_rows(d, cache=store)) == 1            # cold
    store.flush()
    assert len(cctrack.parse_hook_rows(d, cache=store)) == 1            # warm
    text = "".join(cctrack._read_text_lines(str(f)))
    assert cctrack._hook_rows_from_blob_text(text) == []
    assert cctrack.parse_hook_blob(cctrack.HOOK_FILE_SEP + "\n" + text)[0] == []
    (item,) = cctrack._remote_hook_items("fakehost", [d], {})      # the remote's per-file path
    assert item[3] == "s" and item[4]["events"] == []
    store.close()


# ── R15 ───────────────────────────────────────────────────────────────────

def test_remote_manifest_selects_current_rows(tmp_path):
    store = cctrack.open_cache()
    pl = {"events": [], "stats": None}
    pa, pb = (cctrack._remote_cache_params("h", k) for k in ("A", "B"))
    hk = cctrack._remote_host_key("h")
    for kind, path, p in (("claude", "/a1", pa), ("claude", "/a2", pa), ("hook", "/a3", pa),
                          ("claude", "/b1", pb), ("codex", "/a4", pa)):
        store.put(hk, kind, path, (1, 2), pl, p)
    store.put(hk, "claude", "/other-code", (1, 2), pl, "x")
    store.put("local", "claude", "/local", (1, 2), pl, pa)
    store.flush()
    store.code_key, real = store.code_key, store.code_key
    store.code_key = "other-key"
    store.put(hk, "claude", "/old", (1, 2), pl, pa)
    store.code_key = real
    store.flush()
    m = cctrack._remote_manifest(store, "h", ("claude", "hook"))
    assert m == {"rkey": "A", "files": {"claude": {"/a1": [1, 2], "/a2": [1, 2]},
                                        "hook": {"/a3": [1, 2]}}}
    assert cctrack._remote_manifest(store, "h", ("codex",)) == {
        "rkey": "A", "files": {"codex": {"/a4": [1, 2]}}}
    assert cctrack._remote_manifest(store, "nobody", ("claude",)) is None
    assert cctrack._remote_manifest(None, "h", ("claude",)) is None
    store._fail(RuntimeError("boom"))
    assert cctrack._remote_manifest(store, "h", ("claude",)) is None


# ── R16 ───────────────────────────────────────────────────────────────────

def test_remote_timing_lines(tmp_path):
    c, _ = _fresh(tmp_path)
    home = c.root / "xdg-cache"
    env = {"CCTRACK_TIMING": "1"}
    rem = ["--remote", "fakehost"]
    for flag in ((), (), ("--no-cache",)):
        r = run_launched(c, [*rem, *flag], env, cache_home=home, **RUN)
        lines = [ln for ln in r.stderr.splitlines() if ln.startswith("[timing]")]
        parsed = [TIMING_RE.match(ln) for ln in lines[1:]]
        assert all(parsed)
        subs = {m["name"] for m in parsed if m["indent"]}
        assert {"on fakehost: encode", "local: decode", "local: merge", "on fakehost: other"} <= subs
        top = {m["name"]: m for m in parsed if not m["indent"]}
        assert [m["name"] for m in parsed if not m["indent"]][-2:] == ["report", "total"]
        parts = sum(float(m["secs"]) for n, m in top.items() if n != "total")
        assert abs(parts - float(top["total"]["secs"])) < 0.02
        if flag:
            assert top["remote fakehost"]["detail"].endswith("received")


# ── H1 ────────────────────────────────────────────────────────────────────

def test_local_hook_rows_cached_equal_uncached(tmp_path, monkeypatch):
    d = tmp_path / "cctrack"
    d.mkdir()

    def row(sid, h, cost, day="2026-11-10"):
        return json.dumps({"_ts": f"{day}T0{h}:00:00+00:00", "session_id": sid,
                           "cost_usd": cost, "model": "claude-opus-5-5"})
    f1, f2, leg = (d / "statusline-2026-11-10.jsonl", d / "statusline-2026-11-11.jsonl",
                   d / "statusline.jsonl")
    f1.write_text("\n".join([row("a", 1, 1.0), row("b", 1, 0.5), "{bad", "", row("a", 2, 2.0),
                             row("b", 3, 0.7)]))                 # no final newline
    f2.write_text("\n".join([row("a", 1, 9.0), row("b", 5, 1.5, "2026-11-11")]) + "\n")
    leg.write_text(row("b", 0, 0.1) + "\n")
    t = time.time_ns() - 3600 * 10**9
    for f in (f1, f2, leg):
        os.utime(f, ns=(t, t))
    reads = []
    real = cctrack._read_text_lines
    monkeypatch.setattr(cctrack, "_read_text_lines", lambda p: (reads.append(p), real(p))[1])
    store = cctrack.open_cache()
    uncached = cctrack.parse_hook_rows(d)
    reads.clear()
    assert _canon(cctrack.parse_hook_rows(d, cache=store)) == _canon(uncached)
    assert len(reads) == 3
    store.flush()
    reads.clear()
    assert _canon(cctrack.parse_hook_rows(d, cache=store)) == _canon(uncached)
    assert reads == []
    with open(f1, "a") as fh:
        fh.write("\n" + row("a", 4, 3.0) + "\n")
    os.utime(f1, ns=(t + 5 * 10**9,) * 2)
    fresh = cctrack.parse_hook_rows(d)
    reads.clear()
    assert _canon(cctrack.parse_hook_rows(d, cache=store)) == _canon(fresh)
    assert len(reads) == 1 and len(fresh) == 8
    store.flush()
    rows = [r for r in _store_rows(store) if r[0] == "local" and r[1] == "hook"]
    assert len(rows) == 3 and all(r[2].endswith(" " + cctrack._HOOK_LOCAL_PARAMS) for r in rows)
    store.close()


def _store_rows(store):
    con = sqlite3.connect(store.path)
    try:
        return con.execute("SELECT host, kind, code FROM entries").fetchall()
    finally:
        con.close()
