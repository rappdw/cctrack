"""The equivalence harness every cache step extends: a fixture corpus covering
the plan's section 5 cases, and helpers that run `cctrack` as a subprocess in
every report mode and variant and hand back stdout. Also tests the CCTRACK_TIMING
output and the bench script."""

import json
import os
import re
import stat
import struct
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import cctrack

sys.path.insert(0, str(Path(__file__).parent))
from test_detail import claude_line, codex_line, codex_usage, compact_line  # noqa: E402
from test_remote_parse import _fake_ssh  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
# The source tree under test: follows PYTHONPATH, so mutation copies are exercised too.
SRC = Path(cctrack.__file__).resolve().parents[1]
TIMING_RE = re.compile(
    r"^\[timing\] (?P<indent> *)(?P<name>[^ #].*?) +(?P<secs>\d+\.\d{3}) s(?:  (?P<detail>.*))?$")


def zstd_raw_frame(data: bytes) -> bytes:
    """A valid zstd frame made of stored (raw) blocks: no compressor needed."""
    out = bytearray(b"\x28\xb5\x2f\xfd")      # magic
    out.append(0xA0)                          # 4-byte content size, single segment
    out += struct.pack("<I", len(data))
    blk = 128 * 1024
    chunks = [data[i:i + blk] for i in range(0, len(data), blk)] or [b""]
    for i, c in enumerate(chunks):
        hdr = (len(c) << 3) | (1 if i == len(chunks) - 1 else 0)  # raw block
        out += hdr.to_bytes(3, "little") + c
    return bytes(out)


def token_count(ts, last, total):
    return codex_line(ts, "event_msg", {"type": "token_count", "info": {
        "last_token_usage": last, "total_token_usage": total, "model_context_window": 400000}})


def _utc_today() -> datetime:
    return datetime.now(timezone.utc)


# ── Corpus ──────────────────────────────────────────────────────────────

@dataclass
class Corpus:
    root: Path
    home: Path
    remote_home: Path
    bin_dir: Path
    anchor: str            # UTC date at build
    explore_date: str      # day(1)
    python: str


def _w(path: Path, text: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text)


def _populate(home: Path, tag: str, day) -> None:
    d1, d2, d3 = day(1), day(2), day(3)
    proj = home / ".claude" / "projects" / "-repo"
    s1 = "\n".join([
        claude_line(f"{d1}T01:00:00Z", "r1", sid="s1", inp=10_000, out=5_000, cr=100_000, cw1h=500),
        "{not json",
        claude_line(f"{d1}T02:00:00Z", "r2", sid="s1"),
        compact_line(f"{d1}T02:30:00Z"),
        json.dumps({**json.loads(claude_line(f"{d1}T02:40:00Z", "r6", sid="s1")), "message": {
            "model": "claude-opus-5-5", "usage": {
                "input_tokens": 10, "output_tokens": 5,
                "iterations": [{"type": "advisor_message", "model": "claude-opus-4-6",
                                "input_tokens": 1000, "output_tokens": 100}]}}}),
    ]) + "\n"
    _w(proj / "s1.jsonl", s1)
    _w(proj / "s1" / "subagents" / "agent-a1.jsonl",
       claude_line(f"{d1}T01:30:00Z", "r3", sid="s1", out=9, sidechain=True) + "\n")
    _w(proj / "s1-resumed.jsonl", "\n".join([
        claude_line(f"{d1}T02:00:00Z", "r2", sid="s1"),
        claude_line(f"{day(0)}T00:00:01Z", "r5", sid="s1")]) + "\n")
    _w(proj / f"{tag}.jsonl", "\n".join([
        claude_line(f"{d2}T03:00:00Z", f"{tag}-a", sid=tag, model="claude-sonnet-5", inp=500, out=50),
        claude_line(f"{d2}T03:05:00Z", f"{tag}-b", sid=tag, model="claude-unknown-9", inp=700, out=70),
    ]) + "\n")
    box = home / ".sandy" / "sandboxes" / "box-1a2b3c4d"
    _w(box / "WORKSPACE.json", "{}")
    _w(box / "claude" / "projects" / "-repo" / "s1.jsonl", s1)   # sandbox copy of a host session

    cs = home / ".codex" / "sessions"

    def rollout(d, hhmmss, tid):
        y, m, dd = d.split("-")
        return cs / y / m / dd / f"rollout-{d}T{hhmmss}-{tid}.jsonl"

    _w(rollout(d1, "05-00-00", "T1"), "\n".join([
        codex_line(f"{d1}T05:00:00Z", "session_meta", {"id": "T1", "session_id": "T1", "cwd": "/w"}),
        codex_line(f"{d1}T05:00:01Z", "turn_context", {"model": "gpt-6.1-sol"}),
        codex_line(f"{d1}T05:00:02Z", "token_usage_record",
                   {"thread_id": "T1", "session_id": "T1", "response_id": "resp1",
                    "usage": codex_usage(1000, cached=800, out=20)}),
    ]) + "\n")
    _w(rollout(d2, "06-00-00", "P"), "\n".join([
        codex_line(f"{d2}T06:00:00Z", "session_meta", {"id": "P", "session_id": "P", "cwd": "/w"}),
        codex_line(f"{d2}T06:00:01Z", "turn_context", {"model": "gpt-5.4"}),
        token_count(f"{d2}T06:00:02Z", codex_usage(1000, out=100), codex_usage(1000, out=100)),
    ]) + "\n")
    _w(rollout(d1, "07-00-00", "C"), "\n".join([
        codex_line(f"{d1}T07:00:00Z", "session_meta",
                   {"id": "C", "session_id": "C", "forked_from_id": "P", "cwd": "/w"}),
        codex_line(f"{d1}T07:00:01Z", "turn_context", {"model": "gpt-5.4"}),
        token_count(f"{d1}T07:00:02Z", codex_usage(1000, out=100), codex_usage(1000, out=100)),
        token_count(f"{d1}T07:00:03Z", codex_usage(500, out=50), codex_usage(1500, out=150)),
    ]) + "\n")
    z = "\n".join([
        codex_line(f"{d3}T08:00:00Z", "session_meta", {"id": "Z", "session_id": "Z", "cwd": "/w"}),
        codex_line(f"{d3}T08:00:01Z", "turn_context", {"model": "gpt-5.4"}),
        token_count(f"{d3}T08:00:02Z", codex_usage(300, out=30), codex_usage(300, out=30)),
    ]) + "\n"
    _w(home / ".codex" / "archived_sessions" / f"rollout-{d3}T08-00-00-Z.jsonl.zst",
       zstd_raw_frame(z.encode()))

    hook = home / ".claude" / "cctrack"
    rows = lambda sid, costs, d: [  # noqa: E731
        {"_ts": f"{d}T0{i}:00:00+00:00", "session_id": sid, "cost_usd": c,
         "model": "claude-opus-5-5"} for i, c in enumerate(costs)]
    _w(hook / f"statusline-{d1}.jsonl",
       "\n".join(map(json.dumps, rows("s1", (0.0, 0.10, 0.25), d1))) + "\n")
    _w(hook / f"statusline-{day(120)}.jsonl",
       "\n".join(map(json.dumps, rows("olds" if tag == "lh" else "rolds", (0.0, 2.0, 3.0),
                                      day(120)))) + "\n")
    _w(hook / "statusline.jsonl",                       # legacy file
       "\n".join(map(json.dumps, rows("leg" + tag, (0.0, 0.40), day(2)))) + "\n")
    _w(box / "claude" / "cctrack" / f"statusline-{d1}.jsonl",    # a sandbox's hook dir
       "\n".join(map(json.dumps, rows("s1", (0.0, 0.05, 0.30), d1)
                      + rows("bs" + tag, (0.0, 0.20), d1))) + "\n")
    if tag == "lh":
        _w(hook / "config.json", json.dumps({"installed_at": f"{day(150)}T00:00:00+00:00"}))


# A sitecustomize.py on the fake remote's PYTHONPATH: logs every .jsonl/.jsonl.zst the
# remote program opens, and gives the remote a deterministic (raw-frame) zstd decoder.
REMOTE_SITE = r'''
import os, sys, types, io
if os.environ.get("CCTRACK_TEST_REMOTE_ZSTD") == "raw":
    def _open(path, mode="rt", errors=None, **kw):
        with open(path, "rb") as fh:
            data = fh.read()[9:]
        out, i = [], 0
        while i < len(data):
            h = int.from_bytes(data[i:i + 3], "little")
            size, last = h >> 3, h & 1
            out.append(data[i + 3:i + 3 + size])
            i += 3 + size
            if last:
                break
        return io.StringIO(b"".join(out).decode("utf-8", errors or "strict"))
    pkg = types.ModuleType("compression")
    z = types.ModuleType("compression.zstd")
    z.open = _open
    pkg.zstd = z
    pkg.__path__ = []
    sys.modules["compression"] = pkg
    sys.modules["compression.zstd"] = z
_log = os.environ.get("CCTRACK_TEST_REMOTE_OPEN_LOG")
if _log:
    _fd = os.open(_log, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
    def _hook(event, args):
        if event == "open" and isinstance(args[0], (str, bytes)):
            t = os.fsdecode(args[0])
            if t.endswith(".jsonl") or t.endswith(".jsonl.zst"):
                os.write(_fd, (t + "\n").encode("utf-8", "surrogateescape"))
    sys.addaudithook(_hook)
'''


def build_corpus(root: Path, *, settle: bool = True, python: str = sys.executable) -> Corpus:
    today = _utc_today().date()
    anchor = today.isoformat()

    def day(n):
        return (today - timedelta(days=n)).isoformat()

    home, remote_home = root / "local-home", root / "remote-home"
    _populate(home, "lh", day)
    _populate(remote_home, "rh", day)
    _w(root / "remote-site" / "sitecustomize.py", REMOTE_SITE)
    bin_dir = _fake_ssh(root, {"HOME": str(remote_home), "SANDY_HOME": str(remote_home / ".sandy"),
                               "PYTHONPATH": str(root / "remote-site")}, python)
    corpus = Corpus(root, home, remote_home, bin_dir, anchor, day(1), python)
    if settle:
        run_cctrack(corpus, [])
    return corpus


def run_env(corpus: Corpus, extra: dict | None = None) -> dict:
    env = {"HOME": str(corpus.home), "SANDY_HOME": str(corpus.home / ".sandy"),
           "PATH": f"{corpus.bin_dir}{os.pathsep}{os.environ['PATH']}",
           "PYTHONPATH": str(SRC), "CCTRACK_NO_PROGRESS": "1",
           "PYTHONIOENCODING": "utf-8", "XDG_CACHE_HOME": str(corpus.root / "xdg-cache"),
           # reference runs are uncached; cache tests pass CCTRACK_CACHE=1
           "CCTRACK_CACHE": "0"}
    env.update(extra or {})
    return env


def run_cctrack(corpus: Corpus, argv: list, env: dict | None = None) -> subprocess.CompletedProcess:
    r = subprocess.run([corpus.python, "-m", "cctrack", *argv], cwd=corpus.root,
                       env=run_env(corpus, env), stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert r.returncode == 0, f"cctrack {argv} exited {r.returncode}:\n{r.stderr}"
    return r


def backdate(corpus: Corpus, secs: int = 3600) -> int:
    """Set every file under the corpus homes to one old mtime (fresh files are racy and
    never stored). Returns that time in ns."""
    t = time.time_ns() - secs * 10**9
    for home in (corpus.home, corpus.remote_home):
        for f in home.rglob("*"):
            if f.is_file() and not f.is_symlink():
                os.utime(f, ns=(t, t))
    return t


def local_log_files(corpus: Corpus, agent: str = "all") -> list:
    """Sorted paths (relative to corpus.root) of the local transcripts and rollouts."""
    out = []
    for f in corpus.home.rglob("*"):
        if not f.is_file() or not (f.name.endswith(".jsonl") or f.name.endswith(".jsonl.zst")):
            continue
        rel = f.relative_to(corpus.root)
        if "cctrack" in rel.parts:
            continue
        if agent == "claude" and "projects" not in rel.parts:
            continue
        if agent == "codex" and ".codex" not in rel.parts:
            continue
        out.append(str(rel))
    return sorted(out)


LAUNCHER = r'''
import os, sys
log = os.environ.get("CCTRACK_TEST_OPEN_LOG")
if log:
    _fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
    def _hook(event, args):
        if event == "open":
            p = args[0]
            if isinstance(p, (str, bytes)):
                t = os.fsdecode(p)
                if t.endswith(".jsonl") or t.endswith(".jsonl.zst"):
                    os.write(_fd, (t + "\n").encode("utf-8", "surrogateescape"))
    sys.addaudithook(_hook)
import cctrack
mode = os.environ.get("CCTRACK_TEST_ZSTD")
if mode == "raw":
    def _raw(path):
        with open(path, "rb") as fh:
            data = fh.read()[9:]
        out, i = [], 0
        while i < len(data):
            h = int.from_bytes(data[i:i + 3], "little")
            size, last = h >> 3, h & 1
            out.append(data[i + 3:i + 3 + size])
            i += 3 + size
            if last:
                break
        return b"".join(out).decode("utf-8")
    cctrack._read_zstd = _raw
    cctrack._zstd_available = lambda: True
elif mode == "none":
    cctrack._read_zstd = lambda p: None
    cctrack._zstd_available = lambda: False
sys.argv = ["cctrack", *sys.argv[1:]]
cctrack.main()
'''


@dataclass
class Launched:
    stdout: str
    stderr: str
    opens: list
    hook_opens: list = field(default_factory=list)
    remote_opens: list = field(default_factory=list)


def cache_db(cache_home) -> Path:
    return Path(cache_home) / "cctrack" / "cache.sqlite3"


def run_launched(corpus: Corpus, argv: list, env: dict | None = None, *, zstd: str = "raw",
                 src=None, cache_home=None, remote_zstd: str | None = None) -> Launched:
    """Run cctrack through a launcher that logs every .jsonl/.jsonl.zst open (and, via the
    fake remote's sitecustomize, every one the remote program opens)."""
    ldir = corpus.root / "launch"
    ldir.mkdir(exist_ok=True)
    launcher = ldir / "cctrack_launch.py"
    if not launcher.exists() or launcher.read_text() != LAUNCHER:
        launcher.write_text(LAUNCHER)
    fd, log = tempfile.mkstemp(dir=ldir, suffix=".log")
    os.close(fd)
    fd, rlog = tempfile.mkstemp(dir=ldir, suffix=".rlog")
    os.close(fd)
    full = run_env(corpus, {
        "CCTRACK_CACHE": "1", "PYTHONPATH": str(src or SRC), "CCTRACK_TEST_ZSTD": zstd,
        "CCTRACK_TEST_OPEN_LOG": log, "CCTRACK_TEST_REMOTE_OPEN_LOG": rlog,
        **({"CCTRACK_TEST_REMOTE_ZSTD": remote_zstd} if remote_zstd else {}),
        **({"XDG_CACHE_HOME": str(cache_home)} if cache_home else {}), **(env or {})})
    try:
        r = subprocess.run([corpus.python, str(launcher), *argv], cwd=corpus.root, env=full,
                           stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           encoding="utf-8", timeout=120)
        assert r.returncode == 0, f"cctrack {argv} exited {r.returncode}:\n{r.stderr}"
        logged = Path(log).read_text(encoding="utf-8", errors="surrogateescape").splitlines()
        rlogged = Path(rlog).read_text(encoding="utf-8", errors="surrogateescape").splitlines()
    finally:
        os.unlink(log)
        os.unlink(rlog)
    root = str(corpus.root) + os.sep
    opens, hook_opens = [], []
    for t in logged:
        if t.startswith(root):
            rel = t[len(root):]
            if "cctrack" not in Path(rel).parts:
                opens.append(rel)
            elif t.endswith(".jsonl"):
                hook_opens.append(rel)
    remote_opens = sorted(t[len(root):] for t in rlogged if t.startswith(root))
    return Launched(normalise(r.stdout), r.stderr, sorted(opens), sorted(hook_opens), remote_opens)


def remote_listing(corpus: Corpus) -> list:
    """Sorted (relpath, "d"|"f"|"l", size, mtime_ns) for every entry under the fake
    host's home, from os.lstat."""
    out = []
    for p in [corpus.remote_home, *corpus.remote_home.rglob("*")]:
        st = os.lstat(p)
        kind = "l" if stat.S_ISLNK(st.st_mode) else "d" if stat.S_ISDIR(st.st_mode) else "f"
        out.append((str(p.relative_to(corpus.remote_home)), kind, st.st_size, st.st_mtime_ns))
    return sorted(out)


def ssh_counter(tmp_path: Path, corpus: Corpus) -> tuple:
    """(bin_dir, log): a bash `ssh` that appends its arguments to `log` and execs the
    corpus's fake ssh. Must live under tmp_path, never /dev/shm (noexec)."""
    bin_dir = tmp_path / "ssh-counter"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "ssh-calls.log"
    script = bin_dir / "ssh"
    script.write_text(f'#!/bin/bash\necho "$*" >> {log}\nexec {corpus.bin_dir}/ssh "$@"\n')
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return bin_dir, log


def python3_calls(log: Path) -> int:
    """How many logged ssh calls ran `python3 -`."""
    return sum(1 for ln in log.read_text().splitlines() if ln.endswith("python3 -")) if log.exists() else 0


def normalise(stdout: str) -> str:
    return re.sub(r'"generated_at": "[^"]*"', '"generated_at": "<normalised>"', stdout)


def modes(corpus: Corpus) -> dict:
    return {"report": [], "explore": ["--explore", corpus.explore_date],
            "detail": ["--sessions", "--bucket", "hour", "--coverage"],
            "json": ["--json"], "csv-sessions": ["--csv", "sessions"],
            "csv-buckets": ["--csv", "buckets"], "csv-coverage": ["--csv", "coverage"],
            "csv-rates": ["--csv", "rates"]}


VARIANTS: dict = {
    "local": ([], {}), "local-claude": (["--agent", "claude"], {}),
    "local-codex": (["--agent", "codex"], {}),
    "remote": (["--remote", "fakehost"], {}),
    "remote-claude": (["--agent", "claude", "--remote", "fakehost"], {}),
    "remote-codex": (["--agent", "codex", "--remote", "fakehost"], {}),
    "remote-raw": (["--remote", "fakehost"], {"CCTRACK_REMOTE_PARSE": "0"}),
}


def run_matrix(corpus: Corpus, *, variants=None, modes_=None, env=None) -> dict:
    """(variant, mode) -> normalised stdout. Serial, in VARIANTS x modes order."""
    out = {}
    all_modes = modes(corpus)
    for vname in (variants or list(VARIANTS)):
        vargs, venv = VARIANTS[vname]
        for mname in (modes_ or list(all_modes)):
            r = run_cctrack(corpus, [*vargs, *all_modes[mname]], {**venv, **(env or {})})
            out[(vname, mname)] = normalise(r.stdout)
    return out


def diff_keys(a: dict, b: dict) -> list:
    return [k for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)]


def _stable(build_and_run):
    """Midnight guard: output depends on the UTC date, so redo once if it changed."""
    for attempt in range(2):
        result, anchor = build_and_run()
        if _utc_today().date().isoformat() == anchor:
            return result
    pytest.fail("the UTC date changed during two consecutive attempts")


@pytest.fixture(scope="module")
def baseline(tmp_path_factory):
    def go():
        corpus = build_corpus(tmp_path_factory.mktemp("corpus"))
        return (corpus, run_matrix(corpus)), corpus.anchor
    return _stable(go)


# ── Tests ───────────────────────────────────────────────────────────────

def test_corpus_covers_section5_cases(baseline):
    corpus, _ = baseline
    doc = json.loads(run_cctrack(corpus, ["--json", "--remote", "fakehost"]).stdout)
    rows = doc["sessions"]
    s1 = [r for r in rows if r.get("session_id") == "s1" and r.get("agent") == "claude"
          and not r.get("is_subagent")]
    assert s1 and any({"local", "fakehost"} <= {loc.get("host") for loc in r["source_locations"]}
                      and len(r["source_locations"]) >= 3 for r in s1)
    assert any(r.get("is_subagent") and r.get("subagent_id") == "a1" for r in rows)
    assert any((r.get("compactions") or 0) >= 1 for r in rows)
    sources = doc["coverage"]["data_sources"]
    assert any((s.get("unparseable_lines") or 0) >= 1 for s in sources)
    assert any(r.get("session_id") == "C" and r.get("parent_session_id") == "P" for r in rows)
    assert any("claude-unknown-9" in (r.get("priced_by_fallback") or []) for r in rows)
    archived = [s for s in sources if s["host"] == "local" and "archived_sessions" in s["path"]]
    assert archived and any(s.get("usage_records", 0) >= 1 or s.get("unreadable_files") == 1
                            for s in archived)
    cc = corpus.home / ".claude" / "cctrack"
    day120 = (_utc_today().date() - timedelta(days=120)).isoformat()
    assert list(cc.glob("monthly-*.json")) and not (cc / f"statusline-{day120}.jsonl").exists()
    assert (corpus.remote_home / ".claude" / "cctrack" / f"statusline-{day120}.jsonl").exists()
    for h in (corpus.home, corpus.remote_home):
        assert (h / ".claude" / "cctrack" / "statusline.jsonl").exists()      # legacy file
        assert list((h / ".sandy" / "sandboxes").glob("*/claude/cctrack/statusline-*.jsonl"))


def test_two_uncached_runs_match(baseline):
    corpus, first = baseline
    again = run_matrix(corpus)
    assert len(first) == 56 and all(first.values())
    assert diff_keys(first, again) == []
    if _utc_today().date().isoformat() != corpus.anchor:
        pytest.skip("UTC date changed during the run")


@pytest.mark.parametrize("change", ["local-claude", "remote-codex"])
def test_harness_detects_an_altered_fixture(tmp_path, change):
    def go():
        corpus = build_corpus(tmp_path / f"c-{len(list(tmp_path.iterdir()))}")
        before = run_matrix(corpus, variants=["remote"])
        if change == "local-claude":
            day1 = corpus.explore_date
            with open(corpus.home / ".claude/projects/-repo/s1-resumed.jsonl", "a") as f:
                f.write(claude_line(f"{day1}T03:00:00Z", "r99", sid="s1", inp=1_000_000) + "\n")
        else:
            t1 = next((corpus.remote_home / ".codex" / "sessions").rglob("*-T1.jsonl"))
            with open(t1, "a") as f:
                f.write(codex_line(f"{corpus.explore_date}T05:00:03Z", "token_usage_record",
                                   {"thread_id": "T1", "session_id": "T1", "response_id": "resp99",
                                    "usage": codex_usage(1_000_000, out=10)}) + "\n")
        after = run_matrix(corpus, variants=["remote"])
        return (before, after), corpus.anchor
    before, after = _stable(go)
    changed = diff_keys(before, after)
    assert ("remote", "report") in changed and ("remote", "json") in changed


def _timing_lines(stderr: str) -> list:
    return [ln for ln in stderr.splitlines() if ln.startswith("[timing]")]


def test_timing_off_by_default(baseline):
    corpus, _ = baseline
    for env in (None, {"CCTRACK_TIMING": "0"}):
        r = run_cctrack(corpus, ["--remote", "fakehost"], env)
        assert _timing_lines(r.stderr) == []


def test_timing_does_not_change_stdout(baseline):
    corpus, base = baseline
    variants = ["remote", "remote-raw", "local-codex"]
    for vname in variants:
        vargs, venv = VARIANTS[vname]
        for mname, margs in modes(corpus).items():
            r = run_cctrack(corpus, [*vargs, *margs], {**venv, "CCTRACK_TIMING": "1"})
            assert normalise(r.stdout) == base[(vname, mname)], (vname, mname)
            assert any(ln.startswith("[timing] total") for ln in r.stderr.splitlines()), (vname, mname)
    if _utc_today().date().isoformat() != corpus.anchor:
        pytest.skip("UTC date changed during the run")


def test_timing_lines_cover_the_run(baseline):
    corpus, _ = baseline
    for argv in (["--remote", "fakehost"], ["--remote", "fakehost", "--explore", corpus.explore_date],
                 ["--remote", "fakehost", "--json"]):
        lines = _timing_lines(run_cctrack(corpus, argv, {"CCTRACK_TIMING": "1"}).stderr)
        assert lines[0].startswith("[timing] # cctrack ")
        parsed = []
        for ln in lines[1:]:
            m = TIMING_RE.match(ln)
            assert m, ln
            parsed.append(m)
        top = {m["name"]: m for m in parsed if not m["indent"]}
        assert {"startup", "discovery", "claude parse", "aggregate", "hook read", "codex parse",
                "remote fakehost", "reconcile", "report", "total"} <= set(top)
        assert [m["name"] for m in parsed if not m["indent"]][-2:] == ["report", "total"]
        parts = sum(float(m["secs"]) for n, m in top.items() if n != "total")
        assert abs(parts - float(top["total"]["secs"])) < 0.02
        subs = {m["name"] for m in parsed if m["indent"]}
        assert {f"on fakehost: {n}" for n in
                ("discovery", "claude parse", "hook read", "codex parse", "other")} <= subs
        assert top["remote fakehost"]["detail"].endswith("received")
        assert re.match(r"^\d[\d,]* files, ", top["claude parse"]["detail"])


def test_timing_remote_raw_and_failed(baseline, tmp_path):
    corpus, _ = baseline
    r = run_cctrack(corpus, ["--remote", "fakehost"],
                    {"CCTRACK_TIMING": "1", "CCTRACK_REMOTE_PARSE": "0"})
    ms = [TIMING_RE.match(ln) for ln in _timing_lines(r.stderr)[1:]]
    assert [m["detail"] for m in ms if m["name"] == "remote fakehost"] == ["raw stream"]
    assert not any("on fakehost:" in ln for ln in r.stderr.splitlines())
    bad = tmp_path / "bin"
    bad.mkdir()
    (bad / "ssh").write_text("#!/bin/sh\necho 'ssh: refused' >&2\nexit 255\n")
    (bad / "ssh").chmod(0o755)
    r = run_cctrack(corpus, ["--remote", "fakehost"],
                    {"CCTRACK_TIMING": "1", "PATH": f"{bad}{os.pathsep}{os.environ['PATH']}"})
    ms = [TIMING_RE.match(ln) for ln in _timing_lines(r.stderr)[1:]]
    assert [m["detail"] for m in ms if m["name"] == "remote fakehost"] == ["failed"]


def test_timing_printed_when_interrupted(monkeypatch, capsys):
    monkeypatch.setattr(cctrack, "default_dirs", lambda: [])
    monkeypatch.setattr(cctrack, "discover_hook_data_dirs", lambda: [])
    monkeypatch.setattr(cctrack, "read_hook_config", lambda: None)
    monkeypatch.setattr(cctrack, "discover_codex_files", lambda: [])

    def boom(*a, **k):
        raise KeyboardInterrupt
    monkeypatch.setattr(cctrack, "parse_codex_events", boom)
    monkeypatch.setenv("CCTRACK_TIMING", "1")
    monkeypatch.setattr(sys, "argv", ["cctrack"])
    with pytest.raises(KeyboardInterrupt):
        cctrack.main()
    err = capsys.readouterr().err
    assert "[timing] total" in err
    assert any(ln.startswith("[timing] codex parse") and "(interrupted)" in ln
               for ln in err.splitlines())


def test_remote_program_unchanged_without_timing():
    assert cctrack._remote_program("h", True, False).endswith(b"mod._remote_main('h', True, False)\n")
    assert cctrack._remote_program("h", True, False, timing=True).endswith(
        b"mod._remote_main('h', True, False, timing=True)\n")


def test_bench_script_smoke(tmp_path):
    r = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "bench_cache.py"), "--claude-files", "2",
         "--claude-events", "3", "--codex-files", "1", "--codex-events", "2", "--hook-files", "1",
         "--hook-rows", "3", "--remote", "--runs", "2", "--json"],
        capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
    assert r.returncode == 0, r.stderr
    runs = json.loads(r.stdout)["runs"]
    assert len(runs) == 2 and runs[0]["stdout_sha256"] == runs[1]["stdout_sha256"]
    for run in runs:
        assert {"total", "claude parse", "codex parse", "remote benchhost"} <= set(run["phases"])


def test_zstd_raw_frame_decodes(tmp_path):
    p = tmp_path / "x.jsonl.zst"
    p.write_bytes(zstd_raw_frame(b"a\nb\n"))
    got = cctrack._read_zstd(str(p))
    if got is None:
        pytest.skip("no zstd decoder available")
    assert got == "a\nb\n"
