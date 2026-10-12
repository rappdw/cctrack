"""Tests for Sandy sandbox discovery (Claude transcripts, hook data, Codex).

Layout per sandy's published host-side path contract: each sandbox root holds
one directory per agent home — claude/ (~/.claude inside the sandbox) and
codex/ (~/.codex). Roots come from `sandy --print-state` (sandboxes[].path,
which follows $SANDY_HOME) or, without sandy, $SANDY_HOME/sandboxes/*. A
dot-entry such as .rsync-from.<name>.<pid>/ is an in-progress `sandy
--rsync-from` copy and must never be counted.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import cctrack


# ── Fixture builders ────────────────────────────────────────────────────

def _w(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def _rollout(thread, rid, inp, model="gpt-5.4"):
    """A one-response Codex rollout. Requests stay under 272K tokens so they
    bill at the standard gpt-5.4 rate ($2.50/M input)."""
    return [
        {"timestamp": "2026-10-12T09:00:00.000Z", "type": "session_meta",
         "payload": {"id": thread, "cwd": f"/w/{thread}"}},
        {"timestamp": "2026-10-12T09:00:01.000Z", "type": "turn_context",
         "payload": {"model": model, "cwd": f"/w/{thread}"}},
        {"timestamp": "2026-10-12T09:00:02.000Z", "type": "token_usage_record",
         "payload": {"thread_id": thread, "response_id": rid,
                     "usage": {"input_tokens": inp, "cached_input_tokens": 0,
                               "cache_write_input_tokens": 0, "output_tokens": 0,
                               "reasoning_output_tokens": 0, "total_tokens": inp}}},
    ]


def _claude(rid, sid):
    return [{"type": "assistant", "requestId": rid, "sessionId": sid,
             "timestamp": "2026-10-12T09:00:00.000Z", "cwd": "/w/x",
             "message": {"model": "claude-opus-5",
                         "usage": {"input_tokens": 200_000, "output_tokens": 0}}}]


def _hook(cost):
    return [{"_ts": "2026-10-12T09:00:00+00:00", "session_id": "s1", "cost_usd": 0},
            {"_ts": "2026-10-12T10:00:00+00:00", "session_id": "s1", "cost_usd": cost}]


@pytest.fixture
def fake_host(tmp_path, monkeypatch):
    """A host with its own ~/.codex, two sandboxes (one named with a space),
    an in-progress rsync copy, and non-transcript files under claude/."""
    home = tmp_path / "home"
    sb = home / ".sandy" / "sandboxes"
    _w(home / ".codex/sessions/2026/10/12/rollout-a.jsonl", _rollout("host", "r-host", 100_000))
    _w(sb / "web/codex/sessions/2026/10/12/rollout-b.jsonl", _rollout("web", "r-web", 200_000))
    _w(sb / "my box/codex/archived_sessions/rollout-c.jsonl", _rollout("mybox", "r-mybox", 40_000))
    _w(sb / "web/claude/projects/p/s.jsonl", _claude("req-web", "cw"))
    _w(sb / "web/claude/cctrack/statusline-2026-10-12.jsonl", _hook(5.0))
    # Not transcripts — shaped like them on purpose, so only path scoping can
    # keep them out (real ones have other shapes).
    _w(sb / "web/claude/history.jsonl", _claude("req-hist", "ch"))
    _w(sb / "web/claude/jobs/j1/timeline.jsonl", _claude("req-job", "cj"))
    # In-progress `sandy --rsync-from` pull: a hidden full copy. Distinct ids,
    # so only dot-entry skipping (not dedup) keeps it out.
    ghost = sb / ".rsync-from.web.4242"
    _w(ghost / "codex/sessions/2026/10/12/rollout-z.jsonl", _rollout("ghost", "r-ghost", 250_000))
    _w(ghost / "claude/projects/p/s.jsonl", _claude("req-ghost", "cg"))
    _w(ghost / "claude/cctrack/statusline-2026-10-12.jsonl", _hook(5.0))
    (sb / ".web.lock").mkdir()
    # A copy also carries WORKSPACE.json, so only its NAME can exclude it.
    (ghost / "WORKSPACE.json").write_text("{}")
    # A REAL sandbox for a workspace named like a dotfile dir (~/.dotfiles).
    # sandy's own enumeration (and --print-state) currently misses these.
    dot = sb / ".dotfiles-1a2b3c4d"
    (dot).mkdir()
    (dot / "WORKSPACE.json").write_text("{}")
    _w(dot / "codex/sessions/2026/10/12/rollout-d.jsonl", _rollout("dotfiles", "r-dot", 80_000))
    lock = sb / "..dotfiles-1a2b3c4d.lock"
    lock.mkdir()
    (lock / "WORKSPACE.json").write_text("{}")   # even so: a lock is never a sandbox
    # A dot-dir that isn't a sandbox (no WORKSPACE.json) — must be ignored.
    _w(sb / ".scratch/codex/sessions/2026/10/12/rollout-s.jsonl", _rollout("stray", "r-stray", 123_000))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("SANDY_HOME", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(cctrack, "CCTRACK_DIR", home / ".claude" / "cctrack")
    return home


def _cost(events):
    return round(sum(v["cost"] for v in cctrack.aggregate(events)[0].values()), 6)


# host 100K + web 200K + "my box" 40K + .dotfiles 80K tokens of gpt-5.4 input at $2.50/M
CODEX_EXPECTED = (100_000 + 200_000 + 40_000 + 80_000) * 2.50 / 1e6
CODEX_THREADS = ["dotfiles", "host", "mybox", "web"]
ROOT_NAMES = [".dotfiles-1a2b3c4d", "my box", "web"]
# 200K tokens of claude-opus-5 input at $5/M
CLAUDE_EXPECTED = 200_000 * 5.00 / 1e6


# ── Sandbox roots ───────────────────────────────────────────────────────

def test_roots_fallback_dot_entry_rules(fake_host):
    """Skips the rsync copy, lock dirs and dot-dirs without WORKSPACE.json;
    keeps a real dot-named sandbox."""
    assert [p.name for p in cctrack.sandbox_roots()] == ROOT_NAMES


def test_dot_named_sandbox_found_even_with_print_state(fake_host, monkeypatch):
    """sandy --print-state currently omits dot-named sandboxes (a sandy bug);
    cctrack adds them by scanning sandboxes/ itself."""
    sb = fake_host / ".sandy" / "sandboxes"
    monkeypatch.setattr(cctrack, "_sandy_print_state",
                        lambda: [str(sb / "web"), str(sb / "my box")])
    assert [p.name for p in cctrack.sandbox_roots()] == ROOT_NAMES


@pytest.mark.parametrize("name,ws,expected", [
    ("web-1a2b3c4d", None, True),
    (".dotfiles-1a2b3c4d", True, True),
    (".dotfiles-1a2b3c4d", False, False),       # dot-dir without WORKSPACE.json
    (".rsync-from.web-1a2b3c4d.4242", True, False),
    ("..dotfiles-1a2b3c4d.lock", True, False),
    (".web-1a2b3c4d.lock", False, False),
    (".", True, False), ("..", True, False),
])
def test_is_sandbox_name(name, ws, expected):
    assert cctrack._is_sandbox_name(name, ws) is expected


def test_roots_follow_sandy_home(tmp_path, monkeypatch):
    sh = tmp_path / "elsewhere"
    (sh / "sandboxes" / "relocated").mkdir(parents=True)
    monkeypatch.setenv("SANDY_HOME", str(sh))
    assert [p.name for p in cctrack.sandbox_roots()] == ["relocated"]


def test_roots_prefer_print_state(tmp_path, monkeypatch):
    a, hidden = tmp_path / "a", tmp_path / ".rsync-from.a.1"
    monkeypatch.setattr(cctrack, "_sandy_print_state", lambda: [str(a), str(hidden)])
    assert cctrack.sandbox_roots() == [a]


def test_no_sandy_no_sandboxes(tmp_path):
    # conftest points SANDY_HOME at an empty temp dir and stubs the probe
    assert cctrack.sandbox_roots() == []


@pytest.mark.real_sandy_probe
def test_print_state_probe_parses_paths():
    out = json.dumps({"sandboxes": [{"name": "a", "path": "/s/a", "agents": ["codex"]},
                                    {"name": "b", "path": "/s/b"}, {"name": "nopath"}]})
    done = subprocess.CompletedProcess(["sandy"], 0, stdout=out, stderr="")
    with patch("shutil.which", return_value="/usr/bin/sandy"), \
         patch("subprocess.run", return_value=done) as run:
        assert cctrack._sandy_print_state() == ["/s/a", "/s/b"]
        assert run.call_args[0][0] == ["sandy", "--print-state"]


@pytest.mark.real_sandy_probe
@pytest.mark.parametrize("which,result", [
    (None, None),                                                              # no sandy
    ("/usr/bin/sandy", subprocess.CompletedProcess([], 1, stdout="", stderr="x")),  # failed
    ("/usr/bin/sandy", subprocess.CompletedProcess([], 0, stdout="not json", stderr="")),
    ("/usr/bin/sandy", subprocess.CompletedProcess([], 0, stdout='{"other": 1}', stderr="")),
])
def test_print_state_probe_unavailable(which, result):
    with patch("shutil.which", return_value=which), \
         patch("subprocess.run", return_value=result):
        assert cctrack._sandy_print_state() is None


# ── Local discovery ─────────────────────────────────────────────────────

def test_codex_found_in_every_sandbox(fake_host):
    events, skipped = cctrack.parse_codex_events(cctrack.discover_codex_files())
    assert skipped == 0
    assert sorted(e["session_id"] for e in events) == CODEX_THREADS
    assert _cost(events) == pytest.approx(CODEX_EXPECTED)


def test_claude_scan_scoped_to_projects(fake_host):
    """history.jsonl / jobs timelines / codex rollouts aren't transcripts."""
    dirs = cctrack.default_dirs()
    assert all(d.endswith("projects") for d in dirs)
    events = cctrack.parse_events(cctrack.discover_files(dirs))
    assert [e["_request_id"] for e in events] == ["req-web"]
    assert _cost(events) == pytest.approx(CLAUDE_EXPECTED)


def test_rsync_copy_hook_data_not_double_counted(fake_host):
    """Regression: hook data in a hidden .rsync-from copy was summed again
    ($10 reported for $5 of real spend)."""
    dirs = cctrack.discover_hook_data_dirs()
    assert not any(".rsync-from" in str(d) for d in dirs)
    events = []
    for d in dirs:
        events += cctrack.compute_hook_deltas(cctrack.parse_hook_rows(d),
                                              cctrack.load_session_baselines(d))
    assert cctrack.aggregate_hook_data(events)["2026-10-12"]["cost"] == pytest.approx(5.0)


def test_codex_homes_dedup_by_real_path(fake_host, monkeypatch):
    """If CODEX_HOME points into a sandbox, that home isn't read twice."""
    monkeypatch.setenv("CODEX_HOME", str(fake_host / ".sandy/sandboxes/web/codex"))
    homes = [h.resolve() for h in cctrack.codex_homes()]
    assert len(homes) == len(set(homes))


def test_sandbox_without_codex_is_fine(fake_host):
    shutil.rmtree(fake_host / ".sandy/sandboxes/my box/codex")
    events, _ = cctrack.parse_codex_events(cctrack.discover_codex_files())
    assert sorted(e["session_id"] for e in events) == ["dotfiles", "host", "web"]


# ── Remote discovery ────────────────────────────────────────────────────

def _remote_out(state: str, glob_lines: list[str],
                dot_lines: list[str] = ()) -> subprocess.CompletedProcess:
    out = (f"{state}\n{cctrack._STATE_END}\n" + "\n".join(glob_lines) + "\n"
           f"{cctrack._STATE_END}\n" + "\n".join(dot_lines) + "\n")
    return subprocess.CompletedProcess(["ssh"], 0, stdout=out, stderr="")


def test_remote_roots_prefer_print_state():
    state = json.dumps({"sandboxes": [{"path": "/h/.sandy/sandboxes/a"},
                                      {"path": "/h/.sandy/sandboxes/.rsync-from.a.9"}]})
    with patch("subprocess.run", return_value=_remote_out(state, ["/h/.sandy/sandboxes/zzz/"])):
        assert cctrack.remote_sandbox_roots("h") == ["/h/.sandy/sandboxes/a"]


def test_remote_roots_glob_fallback_and_cache():
    out = _remote_out("", ["/h/.sandy/sandboxes/a/", "/h/.sandy/sandboxes/b c/"])
    with patch("subprocess.run", return_value=out) as run:
        assert cctrack.remote_sandbox_roots("h") == ["/h/.sandy/sandboxes/a",
                                                     "/h/.sandy/sandboxes/b c"]
        cctrack.remote_sandbox_roots("h")
        assert run.call_count == 1          # one SSH round trip per host


def test_remote_roots_add_dot_named_sandboxes():
    """Dot-dirs the remote confirmed hold WORKSPACE.json are added, except
    rsync copies and locks."""
    state = json.dumps({"sandboxes": [{"path": "/h/.sandy/sandboxes/a"}]})
    dots = ["/h/.sandy/sandboxes/.dotfiles-1a2b3c4d/",
            "/h/.sandy/sandboxes/.rsync-from.a.9/",
            "/h/.sandy/sandboxes/..dotfiles-1a2b3c4d.lock/",
            "/h/.sandy/sandboxes/./"]
    with patch("subprocess.run", return_value=_remote_out(state, [], dots)):
        assert cctrack.remote_sandbox_roots("h") == ["/h/.sandy/sandboxes/.dotfiles-1a2b3c4d",
                                                     "/h/.sandy/sandboxes/a"]


def test_remote_roots_unreachable_host():
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("ssh", 60)):
        assert cctrack.remote_sandbox_roots("h") == []


@pytest.mark.skipif(not shutil.which("bash") or not shutil.which("find"),
                    reason="needs bash and find")
def test_remote_commands_run_in_a_real_shell(fake_host):
    """End-to-end: the exact remote commands, executed by a real shell against
    the fake host (ssh swapped for local bash), find every sandbox — including
    one whose name has a space — and skip the rsync copy and non-transcripts."""
    real_run = subprocess.run

    def via_bash(cmd, **kw):
        if cmd and cmd[0] == "ssh":
            env = {**os.environ, "HOME": str(fake_host)}
            env.pop("SANDY_HOME", None)
            env.pop("CODEX_HOME", None)
            # no `sandy` on this PATH, so the glob fallback is exercised
            env["PATH"] = "/usr/bin:/bin"
            return real_run(["bash", "-c", cmd[-1]], **{**kw, "env": env})
        return real_run(cmd, **kw)

    with patch("subprocess.run", side_effect=via_bash), patch("sys.stderr"):
        roots = cctrack.remote_sandbox_roots("fake")
        codex = cctrack.parse_remote_codex_events("fake")
        claude = cctrack.parse_remote_events("fake")
        rows, _, _ = cctrack.parse_remote_hook_data("fake")
    assert [r.rsplit("/", 1)[-1] for r in roots] == ROOT_NAMES
    assert sorted(e["session_id"] for e in codex) == CODEX_THREADS
    assert _cost(codex) == pytest.approx(CODEX_EXPECTED)
    assert [e["_request_id"] for e in claude] == ["req-web"]
    hook = cctrack.aggregate_hook_data(cctrack.compute_hook_deltas(rows))
    assert hook["2026-10-12"]["cost"] == pytest.approx(5.0)
