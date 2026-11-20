"""The one per-file parse seam (_read_log_lines / _parse_log_lines /
_parse_log_file) and the local/remote differences it keeps as parameters."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import cctrack

sys.path.insert(0, str(Path(__file__).parent))
from test_detail import claude_line, codex_line, codex_usage, write  # noqa: E402
from test_cache_equivalence import build_corpus  # noqa: E402
from test_remote_parse import _fake_host  # noqa: E402

SEP = "___CCTRACK_FILE_SEP___"


def canon(x):
    return json.dumps(cctrack._tag_json(x), sort_keys=True, default=str)


def _claude_file(tmp_path, name="s1.jsonl"):
    return write(tmp_path / "c" / name, [
        claude_line("2026-11-10T01:00:00Z", "r1", out=5),
        "{not json",
        claude_line("2026-11-10T02:00:00Z", "r2", out=7),
        json.dumps({"type": "system", "subtype": "compact_boundary", "sessionId": "s1",
                    "uuid": "c1", "timestamp": "2026-11-10T02:30:00Z"}),
    ])


def _codex_file(tmp_path):
    return write(tmp_path / "x" / "rollout-2026-11-11T05-00-00-T1.jsonl", [
        codex_line("2026-11-11T05:00:00Z", "session_meta", {"id": "T1", "session_id": "T1", "cwd": "/w"}),
        codex_line("2026-11-11T05:00:01Z", "turn_context", {"model": "gpt-6.1-sol"}),
        codex_line("2026-11-11T05:00:02Z", "token_usage_record",
                   {"thread_id": "T1", "session_id": "T1", "response_id": "resp1",
                    "usage": codex_usage(1000, cached=800, out=20)}),
    ])


def test_seam_unreadable_returns_none(tmp_path):
    z = tmp_path / "rollout-x.jsonl.zst"
    z.write_bytes(b"abcd")
    for kind in ("claude", "codex"):
        for p in (str(tmp_path / "missing.jsonl"), str(tmp_path)):
            assert cctrack._read_log_lines(kind, p) is None
            assert cctrack._parse_log_file(kind, p) is None
    with patch.dict(sys.modules, {"compression": None}), patch("shutil.which", return_value=None):
        assert cctrack._read_log_lines("codex", str(z)) is None
        assert cctrack._parse_log_file("codex", str(z)) is None


def test_seam_matches_the_parsers(tmp_path):
    for kind, p, parser in (("claude", _claude_file(tmp_path), cctrack.parse_lines),
                            ("codex", _codex_file(tmp_path), cctrack.parse_codex_lines)):
        lines = open(p).readlines()
        st = cctrack.new_parse_stats()
        want = parser(lines, source="local", stats=st)
        for e in want:
            cctrack._tag_event_file(e, p)
        assert want
        got = cctrack._parse_log_file(kind, p, want_stats=True)
        assert canon(got) == canon({"events": want, "stats": st})
        got2 = cctrack._parse_log_file(kind, p)
        assert got2["stats"] is None
        assert canon(got2["events"]) == canon(want)
        got3 = cctrack._parse_log_file(kind, p, source="h", label="h:/x")
        assert got3["events"]
        assert all(e["_source_file"] == "h:/x" and e["source"] == "h" for e in got3["events"])


def test_stats_do_not_change_events(tmp_path):
    corpus = build_corpus(tmp_path, settle=False)
    n = 0
    for home in (corpus.home, corpus.remote_home):
        for p in sorted(home.rglob("*")):
            name = p.name
            if not p.is_file() or "cctrack" in p.parts or not (
                    name.endswith(".jsonl") or name.endswith(".jsonl.zst")):
                continue
            kind = "codex" if name.startswith("rollout-") else "claude"
            a = cctrack._parse_log_file(kind, str(p), want_stats=True)
            b = cctrack._parse_log_file(kind, str(p), want_stats=False)
            if a is None:
                assert b is None
                continue
            assert canon(a["events"]) == canon(b["events"])
            n += 1
    assert n >= 10


def test_agent_id_from_name_only_when_asked(tmp_path):
    def run(rel, line, flag):
        p = write(tmp_path / rel, [line])
        return cctrack._parse_log_file("claude", p, agent_id_from_name=flag)["events"][0]["agent_id"]

    side = claude_line("2026-11-10T01:00:00Z", "r1", sidechain=True)
    assert run("a/subagents/agent-b2.jsonl", side, True) == "b2"
    assert run("b/subagents/agent-b2.jsonl", side, False) == ""
    assert run("c/agent-b2.jsonl", claude_line("2026-11-10T01:00:00Z", "r1", sidechain=True,
                                               agent_id="a1"), True) == "a1"
    assert run("d/s1.jsonl", side, True) == ""
    assert run("e/agent-b2.jsonl", claude_line("2026-11-10T01:00:00Z", "r1"), True) == ""


def test_remote_paths_never_fill_agent_id_from_filename(tmp_path, monkeypatch):
    path = "/h/.claude/projects/p/s1/subagents/agent-b2.jsonl"
    raw = f"{SEP} {path}\n" + claude_line("2026-11-10T01:00:00Z", "r1", sidechain=True) + "\n"
    ev = cctrack._remote_parse("dgx", raw, SEP, cctrack.parse_lines, {})
    assert len(ev) == 1 and ev[0]["agent_id"] == ""

    _env_for_fake_host(tmp_path, monkeypatch)
    claude = cctrack._remote_collect("dgx", True, False)["claude"]
    r3 = [e for e in claude if e["_request_id"] == "r3"]
    assert len(r3) == 1 and r3[0]["agent_id"] == ""


def _env_for_fake_host(tmp_path, monkeypatch):
    for k, v in _fake_host(tmp_path).items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("CODEX_HOME", raising=False)


def test_every_call_site_uses_the_seam(tmp_path, monkeypatch):
    parses, reads = [], []
    orig_parse, orig_read = cctrack._parse_log_lines, cctrack._read_log_lines

    def parse(kind, lines, label, **kw):
        parses.append((kind, label))
        return orig_parse(kind, lines, label, **kw)

    def read(kind, path):
        reads.append((kind, path))
        return orig_read(kind, path)

    monkeypatch.setattr(cctrack, "_parse_log_lines", parse)
    monkeypatch.setattr(cctrack, "_read_log_lines", read)
    a, b = _claude_file(tmp_path, "a.jsonl"), _claude_file(tmp_path, "b.jsonl")
    missing = str(tmp_path / "missing.jsonl")
    cctrack.parse_events([a, b, missing])
    assert parses == [("claude", a), ("claude", b)]
    assert len(reads) == 3
    parses.clear(); reads.clear()

    cctrack.parse_codex_events([_codex_file(tmp_path)])
    assert [k for k, _ in parses] == ["codex"]
    parses.clear(); reads.clear()

    raw = (f"{SEP} /x/a.jsonl\n" + claude_line("2026-11-10T01:00:00Z", "r1") + "\n"
           f"{SEP} /x/b.jsonl\n" + claude_line("2026-11-10T02:00:00Z", "r2") + "\n")
    cctrack._remote_parse("dgx", raw, SEP, cctrack.parse_lines, {})
    assert parses == [("claude", "dgx:/x/a.jsonl"), ("claude", "dgx:/x/b.jsonl")]
    assert reads == []
    parses.clear()

    _env_for_fake_host(tmp_path / "fh", monkeypatch)
    cf = cctrack.discover_files(cctrack.default_dirs())
    xf = cctrack.discover_codex_files()
    cctrack._remote_collect("dgx", True, True)
    assert reads == [("claude", p) for p in cf] + [("codex", p) for p in xf]
    assert len(parses) == len(cf) + len(xf)
    assert all(l.startswith("dgx:") for _, l in parses)


def test_plain_files_keep_readlines_splitting(tmp_path):
    obj = json.loads(claude_line("2026-11-10T01:00:00Z", "r1"))
    obj["cwd"] = "/home/u/re\u2028po"
    p = write(tmp_path / "s1.jsonl", [json.dumps(obj, ensure_ascii=False)])
    r = cctrack._parse_log_file("claude", p, want_stats=True)
    assert len(r["events"]) == 1
    assert r["stats"]["lines"] == 1 and r["stats"]["bad_lines"] == 0
    fs = {}
    assert len(cctrack.parse_events([p], file_stats=fs)) == 1
    assert fs[p]["lines"] == 1 and fs[p]["bad_lines"] == 0


def test_codex_plain_unreadable_is_not_a_zst_skip(tmp_path):
    fs, fs2 = {}, {}
    path = str(tmp_path / "rollout-missing.jsonl")
    assert cctrack.parse_codex_events([path], file_stats=fs) == ([], 0)
    assert fs[path]["unreadable"] is True
    missing = str(tmp_path / "missing.jsonl")
    cctrack.parse_events([missing], file_stats=fs2)
    assert fs2[missing] == {**cctrack.new_parse_stats(), "unreadable": True}


def test_seam_rejects_unknown_kind():
    with pytest.raises(ValueError):
        cctrack._read_log_lines("hook", "/x")
    with pytest.raises(ValueError):
        cctrack._parse_log_lines("hook", [], "x")
    with pytest.raises(ValueError):
        cctrack._log_kind(len)
