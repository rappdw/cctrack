"""Tests for the detailed reports: --since/--until, --sessions, --bucket,
--coverage, --json/--csv, --redact."""

import io
import json
from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path

import pytest

import cctrack


def claude_line(ts, rid, sid="s1", inp=10, out=5, cr=0, cw5=0, cw1h=0,
                model="claude-opus-5-5", cwd="/home/u/repo", sidechain=False, agent_id=None):
    e = {"type": "assistant", "timestamp": ts, "requestId": rid, "sessionId": sid,
         "cwd": cwd, "gitBranch": "main", "isSidechain": sidechain,
         "message": {"model": model, "usage": {
             "input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cr,
             "cache_creation": {"ephemeral_5m_input_tokens": cw5,
                                "ephemeral_1h_input_tokens": cw1h}}}}
    if agent_id:
        e["agentId"] = agent_id
    return json.dumps(e)


def compact_line(ts, sid="s1", uuid="c1"):
    return json.dumps({"type": "system", "subtype": "compact_boundary", "timestamp": ts,
                       "sessionId": sid, "uuid": uuid})


def codex_line(ts, kind, payload):
    return json.dumps({"timestamp": ts, "type": kind, "payload": payload})


def codex_usage(inp, cached=0, out=0):
    return {"input_tokens": inp, "cached_input_tokens": cached,
            "cache_write_input_tokens": 0, "output_tokens": out, "total_tokens": inp + out}


def write(path: Path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    return str(path)


def args(**kw):
    base = dict(sessions=False, bucket=None, coverage=False, json=True, csv=None,
                redact=False, billing_mode=None)
    base.update(kw)
    return Namespace(**base)


def run_json(events, sources=None, file_stats=None, since=None, until=None, **kw):
    out = io.StringIO()
    cctrack.run_detail_report(args(**kw), events, sources or [], file_stats or {},
                              since, until, out=out)
    return json.loads(out.getvalue())


# ── Time windows ────────────────────────────────────────────────────────

def test_parse_utc_formats():
    z = cctrack.parse_utc("2026-10-11T02:30:04Z")
    assert z == datetime(2026, 10, 11, 2, 30, 4, tzinfo=timezone.utc)
    assert cctrack.parse_utc("2026-10-11T04:30:04+02:00") == z
    assert cctrack.parse_utc("2026-10-11T02:30:04") == z  # naive = UTC
    assert cctrack.parse_utc("2026-10-11") == datetime(2026, 10, 11, tzinfo=timezone.utc)
    # A bare --until date includes that whole day.
    assert cctrack.parse_utc("2026-10-11", end=True) == datetime(2026, 10, 12, tzinfo=timezone.utc)


def test_filter_window_half_open():
    evs = [{"timestamp": t} for t in ("2026-10-11T00:59:59Z", "2026-10-11T01:00:00Z",
                                      "2026-10-11T01:59:59.999Z", "2026-10-11T02:00:00Z")]
    got = cctrack.filter_window(evs, cctrack.parse_utc("2026-10-11T01:00:00Z"),
                                cctrack.parse_utc("2026-10-11T02:00:00Z"))
    assert [e["timestamp"] for e in got] == ["2026-10-11T01:00:00Z", "2026-10-11T01:59:59.999Z"]


# ── Sessions ────────────────────────────────────────────────────────────

def test_session_row_tokens_cost_and_context(tmp_path):
    f = write(tmp_path / "p" / "s1.jsonl", [
        claude_line("2026-10-11T01:00:00Z", "r1", inp=100, out=10, cr=1000, cw1h=50),
        claude_line("2026-10-11T03:00:00Z", "r2", inp=0, out=20, cr=3000, cw5=10),
    ])
    stats = {}
    events = cctrack.parse_events([f], file_stats=stats)
    doc = run_json(events, file_stats=stats)
    [row] = doc["sessions"]
    assert row["requests"] == 2
    assert row["tokens"] == {"input": 100, "output": 30, "cache_read": 4000,
                             "cache_write_5m": 10, "cache_write_1h": 50}
    # Opus 5.5: $4 in, $20 out, $0.20 read, $5 5m write, $8 1h write per Mtok.
    expected = (100 * 4 + 30 * 20 + 4000 * 0.2 + 10 * 5 + 50 * 8) / 1e6
    assert row["cost_usd"] == pytest.approx(expected)
    assert sum(row["cost_by_class_usd"].values()) == pytest.approx(expected)
    assert row["cache_read_per_request_mean"] == 2000
    assert row["cache_read_per_request_max"] == 3000
    assert row["context_tokens_per_request_max"] == 3010
    assert row["first_ts"] == "2026-10-11T01:00:00Z"
    assert row["last_ts"] == "2026-10-11T03:00:00Z"
    assert row["models"] == {"claude-opus-5-5": 2}
    assert row["cost_basis"] == "list_price"
    assert doc["timezone"] == "UTC"
    assert doc["schema_version"] == cctrack.SCHEMA_VERSION
    assert doc["rate_card"][0]["usd_per_mtok"]["cache_write_1h"] == 8.0


def test_claude_subagent_gets_own_row_linked_to_parent(tmp_path):
    main = write(tmp_path / "p" / "s1.jsonl", [claude_line("2026-10-11T01:00:00Z", "r1")])
    # Newer layout: <session>/subagents/agent-<id>.jsonl, agentId in the line.
    sub = write(tmp_path / "p" / "s1" / "subagents" / "agent-a1.jsonl",
                [claude_line("2026-10-11T01:01:00Z", "r2", out=50, sidechain=True,
                             agent_id="a1")])
    # Older: no agentId in the line; the file name carries it.
    old = write(tmp_path / "p" / "s1" / "subagents" / "agent-b2.jsonl",
                [claude_line("2026-10-11T01:02:00Z", "r3", out=7, sidechain=True)])
    events = cctrack.parse_events([main, sub, old])
    rows = {r["row_id"]: r for r in run_json(events)["sessions"]}
    assert set(rows) == {"s1", "s1/a1", "s1/b2"}
    assert rows["s1"]["is_subagent"] is False
    assert rows["s1/a1"]["parent_session_id"] == "s1"
    assert rows["s1/a1"]["is_subagent"] is True
    assert rows["s1/a1"]["tokens"]["output"] == 50
    assert rows["s1/b2"]["tokens"]["output"] == 7


def test_compactions_counted_once_and_windowed(tmp_path):
    lines = [claude_line("2026-10-11T01:00:00Z", "r1"),
             compact_line("2026-10-11T02:00:00Z", uuid="c1"),
             compact_line("2026-10-12T02:00:00Z", uuid="c2")]
    a = write(tmp_path / "p" / "s1.jsonl", lines)
    b = write(tmp_path / "p" / "s1-resumed.jsonl", lines)  # resume replays history
    stats = {}
    events = cctrack.parse_events([a, b], file_stats=stats)
    assert cctrack.compaction_counts(stats) == {("s1", ""): 2}
    since = cctrack.parse_utc("2026-10-11")
    until = cctrack.parse_utc("2026-10-11", end=True)
    [row] = run_json(events, file_stats=stats, since=since, until=until)["sessions"]
    assert row["compactions"] == 1


def test_codex_subagent_thread_links_parent_and_fork_replay_stays_with_parent():
    parent = [
        codex_line("2026-10-11T01:00:00Z", "session_meta", {"id": "P", "session_id": "P",
                                                             "cwd": "/w"}),
        codex_line("2026-10-11T01:00:01Z", "turn_context", {"model": "gpt-6.1-sol"}),
        codex_line("2026-10-11T01:00:02Z", "token_usage_record",
                   {"thread_id": "P", "session_id": "P", "response_id": "r1",
                    "usage": codex_usage(100, out=1)}),
    ]
    child = [
        codex_line("2026-10-11T01:05:00Z", "session_meta", {
            "id": "C", "session_id": "C", "cwd": "/w",
            "source": {"subagent": {"thread_spawn": {"parent_thread_id": "P", "depth": 1}}}}),
        codex_line("2026-10-11T01:05:01Z", "turn_context", {"model": "gpt-6.1-sol"}),
        # Replayed parent record: still the parent's spend.
        parent[2],
        codex_line("2026-10-11T01:05:02Z", "token_usage_record",
                   {"thread_id": "C", "session_id": "C", "response_id": "r2",
                    "usage": codex_usage(40, out=2)}),
        codex_line("2026-10-11T01:06:00Z", "compacted", {"message": ""}),
        codex_line("2026-10-11T01:06:00.5Z", "event_msg", {"type": "context_compacted"}),
    ]
    stats_p, stats_c = cctrack.new_parse_stats(), cctrack.new_parse_stats()
    evs = cctrack.dedup_events([cctrack.parse_codex_lines(parent, stats=stats_p),
                                cctrack.parse_codex_lines(child, stats=stats_c)])
    rows = {r["session_id"]: r for r in run_json(
        evs, file_stats={"p": stats_p, "c": stats_c})["sessions"]}
    assert rows["P"]["tokens"]["input"] == 100 and rows["P"]["is_subagent"] is False
    assert rows["C"]["tokens"]["input"] == 40
    assert rows["C"]["parent_session_id"] == "P" and rows["C"]["is_subagent"] is True
    assert rows["C"]["compactions"] == 1


def test_hook_cost_only_for_day_aligned_windows():
    events = cctrack.parse_lines([claude_line("2026-10-11T01:00:00Z", "r1")])
    sources = [{"name": "local", "agent": "claude", "install_date": "2026-10-01",
                "deltas": [{"session_id": "s1", "date_str": "2026-10-11", "cost_usd": 2.5}]}]
    day = run_json(events, sources, since=cctrack.parse_utc("2026-10-11"),
                   until=cctrack.parse_utc("2026-10-11", end=True))
    assert day["sessions"][0]["hook_cost_usd"] == 2.5
    assert day["buckets"][0]["hook_cost_usd_host_day"] == 2.5
    part = run_json(events, sources, since=cctrack.parse_utc("2026-10-11T00:30:00Z"))
    assert part["sessions"][0]["hook_cost_usd"] is None


# ── Buckets ─────────────────────────────────────────────────────────────

def test_hour_buckets_are_utc():
    events = cctrack.parse_lines([
        claude_line("2026-10-11T01:10:00Z", "r1"),
        claude_line("2026-10-11T03:10:00+02:00", "r2"),  # = 01:10Z
        claude_line("2026-10-11T02:59:59Z", "r3"),
    ])
    rows = run_json(events, bucket="hour")["buckets"]
    assert [(r["bucket_start"], r["requests"]) for r in rows] == [
        ("2026-10-11T01:00:00Z", 2), ("2026-10-11T02:00:00Z", 1)]


def test_sandbox_attribution_from_path(tmp_path):
    f = write(tmp_path / "sandboxes" / "factory-a1b2" / "claude" / "projects" / "x" / "s.jsonl",
              [claude_line("2026-10-11T01:00:00Z", "r1")])
    [row] = run_json(cctrack.parse_events([f]))["sessions"]
    assert row["sandbox"] == "factory-a1b2"
    assert cctrack.sandbox_of("dgx:/home/u/.sandy/sandboxes/box/codex/sessions/r.jsonl") == "box"
    assert cctrack.sandbox_of("/home/u/.claude/projects/x.jsonl") == ""


# ── Coverage ────────────────────────────────────────────────────────────

def test_coverage_flags_bad_lines_gaps_and_hook_only_sessions(tmp_path):
    f = write(tmp_path / ".claude" / "projects" / "x" / "s1.jsonl", [
        claude_line("2026-10-11T01:00:00Z", "r1"), "{not json",
        claude_line("2026-10-13T01:00:00Z", "r2")])
    stats = {}
    events = cctrack.parse_events([f], file_stats=stats)
    sources = [{"name": "local", "agent": "claude", "install_date": "2026-10-01", "deltas": [
        {"session_id": "s1", "date_str": "2026-10-11", "cost_usd": 1.0},
        {"session_id": "lost", "date_str": "2026-10-12", "cost_usd": 7.0}]}]
    cov = run_json(events, sources, stats, since=cctrack.parse_utc("2026-10-11"),
                   until=cctrack.parse_utc("2026-10-13", end=True))["coverage"]
    [src] = cov["data_sources"]
    assert src["status"] == "UNKNOWN" and src["unparseable_lines"] == 1
    assert src["first_ts"] == "2026-10-11T01:00:00Z" and src["last_ts"] == "2026-10-13T01:00:00Z"
    [gap] = cov["gaps"]
    assert gap["days_without_records"] == [["2026-10-12", "2026-10-12"]]
    assert gap["hook_spend_days_without_records"] == ["2026-10-12"]
    [lost] = gap["sessions_with_hook_spend_but_no_logs"]
    assert lost["session_id"] == "lost" and lost["tokens"] == "UNKNOWN"


# ── Output formats, redaction, billing ─────────────────────────────────

def test_redact_hashes_paths_not_ids(tmp_path, monkeypatch):
    monkeypatch.setenv("CCTRACK_REDACT_SALT", "pepper")
    f = write(tmp_path / "sandboxes" / "secret-repo" / "claude" / "projects" / "x" / "s1.jsonl",
              [claude_line("2026-10-11T01:00:00Z", "r1", cwd="/home/u/secret-repo")])
    doc = run_json(cctrack.parse_events([f], file_stats=(st := {})), file_stats=st, redact=True)
    text = json.dumps(doc)
    assert "secret-repo" not in text
    row = doc["sessions"][0]
    assert row["session_id"] == "s1"
    assert row["cwd"].startswith("h:") and row["sandbox"].startswith("h:")
    assert doc["redacted"] is True


def test_csv_sessions_header_and_values():
    events = cctrack.parse_lines([claude_line("2026-10-11T01:00:00Z", "r1")])
    out = io.StringIO()
    cctrack.run_detail_report(args(json=False, csv="sessions"), events, [], {}, None, None,
                              out=out)
    header, row = out.getvalue().splitlines()
    assert header.split(",") == ["schema_version", *cctrack.SESSION_CSV_FIELDS]
    vals = dict(zip(header.split(","), row.split(",")))
    assert vals["schema_version"] == "1" and vals["is_subagent"] == "false"
    assert vals["rate_card_version"] == cctrack.RATE_CARD_VERSION


def test_billing_mode_spec(monkeypatch):
    assert cctrack.parse_billing_mode("subscription") == {"claude": "subscription",
                                                          "codex": "subscription"}
    assert cctrack.parse_billing_mode("claude=subscription,codex=api") == {
        "claude": "subscription", "codex": "api"}
    monkeypatch.setenv("CCTRACK_BILLING_MODE", "api")
    assert cctrack.parse_billing_mode(None) == {"claude": "api", "codex": "api"}


def test_remote_stream_carries_file_paths():
    sep = "___CCTRACK_FILE_SEP___"
    raw = (f"{sep} /home/u/.sandy/sandboxes/box/claude/projects/x/s1.jsonl\n"
           + claude_line("2026-10-11T01:00:00Z", "r1") + "\n"
           + f"{sep}\n" + claude_line("2026-10-11T01:00:01Z", "r2") + "\n")  # old format
    stats = {}
    evs = cctrack._remote_parse("dgx", raw, sep, cctrack.parse_lines, stats)
    by = {e["_request_id"]: e for e in evs}
    assert by["r1"]["_source_file"] == "dgx:/home/u/.sandy/sandboxes/box/claude/projects/x/s1.jsonl"
    assert by["r2"]["_source_file"] == "(remote dgx)"
    assert list(stats) == [by["r1"]["_source_file"]]
    [row] = [r for r in run_json(evs)["sessions"] if r["sandbox"]]
    assert row["host"] == "dgx" and row["sandbox"] == "box"


def test_since_requires_a_detail_view(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["cctrack", "--since", "2026-10-11"])
    with pytest.raises(SystemExit):
        cctrack.main()
    assert "--since/--until apply to" in capsys.readouterr().err


def test_remote_separator_inside_content_does_not_split_a_file():
    sep = "___CCTRACK_FILE_SEP___"
    tricky = claude_line("2026-10-11T01:00:00Z", "r1", cwd=f"/x/{sep}/y")
    raw = f"{sep} /a.jsonl\n{tricky}\n{sep} /b.jsonl\n" + claude_line(
        "2026-10-11T01:00:01Z", "r2") + "\n"
    stats = {}
    evs = cctrack._remote_parse("dgx", raw, sep, cctrack.parse_lines, stats)
    assert sorted(e["_request_id"] for e in evs) == ["r1", "r2"]
    assert sorted(stats) == ["dgx:/a.jsonl", "dgx:/b.jsonl"]
    assert all(st["bad_lines"] == 0 for st in stats.values())
