"""--remote parses logs ON the host and ships only usage events. Must produce
exactly what streaming the raw logs produces, and fall back to that stream
when the host can't run it."""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import cctrack

sys.path.insert(0, str(Path(__file__).parent))
from test_detail import claude_line, codex_line, codex_usage  # noqa: E402

pytestmark = pytest.mark.real_remote_bundle


def _fake_host(tmp_path: Path) -> dict:
    """A host's home: own Claude transcripts + hook data, and a Sandy sandbox
    with Claude and Codex logs. Returns env for the fake ssh."""
    home = tmp_path / "remote-home"
    sandy = home / ".sandy"
    proj = home / ".claude" / "projects" / "-repo"
    proj.mkdir(parents=True)
    (proj / "s1.jsonl").write_text("\n".join([
        claude_line("2026-11-10T01:00:00Z", "r1", sid="s1", inp=10, out=5, cr=1000, cw1h=50),
        "{not json",
        claude_line("2026-11-10T02:00:00Z", "r2", sid="s1", out=7),
        json.dumps({"type": "system", "subtype": "compact_boundary", "sessionId": "s1",
                    "uuid": "c1", "timestamp": "2026-11-10T02:30:00Z"}),
    ]) + "\n")
    (proj / "s1" / "subagents").mkdir(parents=True)
    (proj / "s1" / "subagents" / "agent-a1.jsonl").write_text(
        claude_line("2026-11-10T01:30:00Z", "r3", sid="s1", out=9, sidechain=True) + "\n")
    # Same API request copied into a resumed file: dedup must agree.
    (proj / "s1-resumed.jsonl").write_text(
        claude_line("2026-11-10T02:00:00Z", "r2", sid="s1", out=7) + "\n")

    box = sandy / "sandboxes" / "box-1a2b3c4d"
    bproj = box / "claude" / "projects" / "-w"
    bproj.mkdir(parents=True)
    (bproj / "s2.jsonl").write_text(
        claude_line("2026-11-11T05:00:00Z", "r4", sid="s2", model="claude-sonnet-5",
                    cwd="/w") + "\n")
    rollouts = box / "codex" / "sessions" / "2026" / "11" / "11"
    rollouts.mkdir(parents=True)
    (rollouts / "rollout-2026-11-11T05-00-00-T1.jsonl").write_text("\n".join([
        codex_line("2026-11-11T05:00:00Z", "session_meta", {"id": "T1", "session_id": "T1",
                                                             "cwd": "/w"}),
        codex_line("2026-11-11T05:00:01Z", "turn_context", {"model": "gpt-6.1-sol"}),
        codex_line("2026-11-11T05:00:02Z", "token_usage_record",
                   {"thread_id": "T1", "session_id": "T1", "response_id": "resp1",
                    "usage": codex_usage(1000, cached=800, out=20)}),
    ]) + "\n")

    hook = home / ".claude" / "cctrack"
    hook.mkdir(parents=True)
    rows = [{"_ts": f"2026-11-10T0{i}:00:00+00:00", "session_id": "s1",
             "cost_usd": c, "model": "claude-opus-5-5"} for i, c in enumerate((0.0, 0.5, 1.25))]
    (hook / "statusline-2026-11-10.jsonl").write_text("\n".join(map(json.dumps, rows)) + "\n")
    (hook / "monthly-2026-08.json").write_text(json.dumps(
        {"days": {"2026-08-03": {"cost": 4.5, "sessions": 2}}}))
    (hook / "session-baselines.json").write_text(json.dumps(
        {"old": {"date": "2026-08-31", "cost_usd": 3.0}}))
    return {"HOME": str(home), "SANDY_HOME": str(sandy)}


def _fake_ssh(tmp_path: Path, env: dict, python: str = sys.executable) -> Path:
    """`ssh [opts] host CMD…`: runs CMD locally in the fake host's home —
    `python3 -` gets the program on stdin, anything else goes to bash."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "ssh"
    exports = "".join(f"export {k}={v!r}\n".replace("'", '"') for k, v in env.items())
    script.write_text(
        "#!/bin/bash\n" + exports +
        "unset CODEX_HOME\n"
        "while [ $# -gt 0 ]; do case \"$1\" in -o) shift 2;; -*) shift;; *) break;; esac; done\n"
        "shift  # host\n"
        f"if [ \"$*\" = 'python3 -' ]; then exec {python} -; fi\n"
        "exec bash -c \"$*\"\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return bin_dir


def _collect(monkeypatch):
    cctrack._remote_bundle_cache.clear()
    cctrack._remote_roots_cache.clear()
    fs: dict = {}
    cctrack.fetch_remote_bundle("dgx", True, True)
    codex = cctrack.parse_remote_codex_events("dgx", fs)
    claude = cctrack.parse_remote_events("dgx", fs)
    hook = cctrack.parse_remote_hook_data("dgx")
    return claude, codex, hook, fs


def _canon(x):
    return json.dumps(cctrack._tag_json(x), sort_keys=True, default=str)


def _sorted_events(evs):
    return sorted(_canon(e) for e in evs)


@pytest.fixture
def host(tmp_path, monkeypatch):
    env = _fake_host(tmp_path)
    monkeypatch.setenv("PATH", f"{_fake_ssh(tmp_path, env)}:{os.environ['PATH']}")
    return env


def test_remote_parse_matches_raw_stream_exactly(host, monkeypatch, capsys):
    claude, codex, hook, fs = _collect(monkeypatch)
    assert cctrack._remote_bundle_cache["dgx"].get("ok"), capsys.readouterr().err
    monkeypatch.setenv("CCTRACK_REMOTE_PARSE", "0")
    r_claude, r_codex, r_hook, r_fs = _collect(monkeypatch)
    assert cctrack._remote_bundle_cache["dgx"] == {"fallback": True}

    assert len(claude) == 4 and len(codex) == 1          # r2 copy deduped
    assert _sorted_events(claude) == _sorted_events(r_claude)
    assert _sorted_events(codex) == _sorted_events(r_codex)
    assert _canon(fs) == _canon(r_fs)                       # stats incl. bad line, compaction
    assert any(st["bad_lines"] == 1 for st in fs.values())
    rows, monthly, baselines = hook
    assert [r["cost_usd"] for r in rows] == [0.0, 0.5, 1.25]
    assert monthly == {"2026-08-03": {"cost": 4.5, "sessions": 2}} and "old" in baselines
    assert _canon(hook) == _canon(r_hook)
    sandboxed = [e for e in claude + codex if "box-1a2b3c4d" in e["_source_file"]]
    assert len(sandboxed) == 2 and all(e["source"] == "dgx" for e in claude + codex)
    assert all(e["_source_file"].startswith("dgx:/") for e in claude + codex)


def test_remote_parse_is_one_ssh_call_and_much_smaller(host, monkeypatch):
    calls = []
    real_run = subprocess.run

    def counting_run(cmd, **kw):
        calls.append(cmd[-1])
        r = real_run(cmd, **kw)
        calls[-1] = (cmd[-1], len(r.stdout or b""))
        return r
    monkeypatch.setattr(subprocess, "run", counting_run)
    _collect(monkeypatch)
    assert [c for c, _ in calls] == ["-"]                  # just `ssh dgx python3 -`
    remote_bytes = calls[0][1]
    calls.clear()
    monkeypatch.setenv("CCTRACK_REMOTE_PARSE", "0")
    _collect(monkeypatch)
    assert len(calls) == 4                                  # roots + 3 raw streams
    assert remote_bytes < 3000


def test_old_python_on_host_falls_back_to_raw_stream(tmp_path, monkeypatch, capsys):
    env = _fake_host(tmp_path)
    old = tmp_path / "py39"
    old.write_text("#!/bin/sh\necho 'CCTRACK_REMOTE_UNSUPPORTED 3.9'\n")
    old.chmod(0o755)
    monkeypatch.setenv("PATH", f"{_fake_ssh(tmp_path, env, python=str(old))}:{os.environ['PATH']}")
    claude, codex, hook, _ = _collect(monkeypatch)
    err = capsys.readouterr().err
    assert "its python3 is 3.9; cctrack needs 3.10+" in err and "streaming raw logs" in err
    assert len(claude) == 4 and len(codex) == 1 and len(hook[0]) == 3


def test_unreachable_host_fails_once_without_raw_retries(monkeypatch, tmp_path, capsys):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "ssh").write_text("#!/bin/sh\necho 'ssh: connect to host dgx: refused' >&2\nexit 255\n")
    (bin_dir / "ssh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    claude, codex, hook, _ = _collect(monkeypatch)
    assert (claude, codex, hook) == ([], [], ([], {}, {}))
    assert cctrack._remote_bundle_cache["dgx"] == {"failed": True}
    assert "connect to host dgx: refused" in capsys.readouterr().err


def test_tagged_json_round_trips_python_types():
    from datetime import datetime, timezone
    obj = {"a": (1, "x", (2,)), ("k", ""): {"__cct": "user"}, "s": {3},
           "t": datetime(2026, 11, 10, 1, 2, 3, tzinfo=timezone.utc), "l": [None, 1.5]}
    assert cctrack._untag_json(json.loads(json.dumps(cctrack._tag_json(obj)))) == obj


def test_raw_stream_is_gzipped_and_decoded():
    import gzip
    assert cctrack._decode_stream(gzip.compress("héllo\n".encode())) == "héllo\n"
    assert cctrack._decode_stream(b"plain") == "plain"
    assert cctrack._decode_stream("text") == "text"
    truncated = gzip.compress(b"x" * 100000)[:-20]
    assert cctrack._decode_stream(truncated).startswith("x")
    assert "gzip -c" in cctrack._gzip_wrapped("find .")


def test_remote_parse_with_warm_cache_matches_raw_stream(host, monkeypatch):
    import time
    old = time.time_ns() - 3600 * 10**9
    for f in Path(host["HOME"]).rglob("*"):
        if f.is_file():
            os.utime(f, ns=(old, old))

    def collect(store):
        cctrack._remote_bundle_cache.clear()
        cctrack._remote_roots_cache.clear()
        fs: dict = {}
        cctrack.fetch_remote_bundle("dgx", True, True, cache=store)
        return (cctrack.parse_remote_events("dgx", fs), cctrack.parse_remote_codex_events("dgx", fs),
                cctrack.parse_remote_hook_data("dgx"), fs)

    monkeypatch.setenv("CCTRACK_REMOTE_PARSE", "0")
    raw = collect(None)
    monkeypatch.setenv("CCTRACK_REMOTE_PARSE", "1")
    store = cctrack.open_cache()
    assert store is not None
    cold = collect(store)
    cold_bundle = cctrack._remote_bundle_cache["dgx"]
    store.flush()
    warm = collect(store)
    warm_bundle = cctrack._remote_bundle_cache["dgx"]
    for got in (cold, warm):
        assert _sorted_events(got[0]) == _sorted_events(raw[0])
        assert _sorted_events(got[1]) == _sorted_events(raw[1])
        assert _canon(got[2]) == _canon(raw[2]) and _canon(got[3]) == _canon(raw[3])
    assert cold_bundle["cache_counts"] == {"reused": 0, "read": 6}
    assert warm_bundle["cache_counts"] == {"reused": 6, "read": 0}
    assert warm_bundle["_wire_bytes"] < cold_bundle["_wire_bytes"]
    store.close()
