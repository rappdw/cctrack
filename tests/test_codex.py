"""Tests for Codex CLI usage tracking.

Fixtures mirror the rollout schema in the Codex source (codex-rs): each line is
{"timestamp", "type", "payload"}; usage comes from `token_usage_record` items
(per-response, with response_id) or legacy `event_msg`/`token_count` events.
input_tokens INCLUDES cached_input_tokens and cache_write_input_tokens, and
output_tokens INCLUDES reasoning_output_tokens (see Codex's own test fixture:
input 100 = cached 40 + cache_write 60, total 110 = input 100 + output 10).
"""

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import cctrack


# ── Fixture builders ────────────────────────────────────────────────────

def usage(inp=0, cached=0, cache_write=0, out=0, reasoning=0):
    return {"input_tokens": inp, "cached_input_tokens": cached,
            "cache_write_input_tokens": cache_write, "output_tokens": out,
            "reasoning_output_tokens": reasoning, "total_tokens": inp + out}


def line(ts, kind, payload):
    return json.dumps({"timestamp": ts, "type": kind, "payload": payload})


def session_meta(ts="2026-10-10T09:00:00.000Z", thread="thr-1", cwd="/home/u/svc",
                 branch="main"):
    return line(ts, "session_meta", {"session_id": thread, "id": thread, "cwd": cwd,
                                     "timestamp": ts, "originator": "codex_cli_rs",
                                     "cli_version": "0.99.0",
                                     "git": {"branch": branch, "commit_hash": "abc"}})


def turn_context(ts, model="gpt-5.3-codex", cwd="/home/u/svc"):
    return line(ts, "turn_context", {"cwd": cwd, "model": model,
                                     "approval_policy": "on-request"})


def record(ts, rid, u, thread="thr-1"):
    return line(ts, "token_usage_record", {
        "thread_id": thread, "turn_id": "t", "session_id": thread,
        "root_turn_id": "t", "response_id": rid, "usage": u,
        "turn_token_usage": u, "thread_token_usage": u})


def token_count(ts, last, total_tokens):
    total = dict(last)
    total["total_tokens"] = total_tokens
    return line(ts, "event_msg", {"type": "token_count",
                                  "info": {"total_token_usage": total,
                                           "last_token_usage": last,
                                           "model_context_window": 400000},
                                  "rate_limits": None})


# ── Token semantics & pricing ───────────────────────────────────────────

def test_cached_and_cache_write_are_inside_input():
    """Uncached input = input − cached − cache_write (Codex's own fixture)."""
    lines = [session_meta(), turn_context("2026-10-10T09:00:01.000Z"),
             record("2026-10-10T09:00:02.000Z", "r1", usage(inp=100, cached=40,
                                                            cache_write=60, out=10,
                                                            reasoning=5))]
    [e] = cctrack.parse_codex_lines(lines)
    assert e["input"] == 0
    assert e["cache_read"] == 40
    assert e["cache_write"] == 60
    # reasoning (5) is already inside output (10) — never added on top
    assert e["output"] == 10
    assert e["_request_input"] == 100


def test_codex_cost_uses_openai_rates():
    # gpt-5.3-codex: $1.75 in, $0.175 cached, $14 out (no cache-write price →
    # cache writes bill as input).
    lines = [session_meta(), turn_context("2026-10-10T09:00:01.000Z"),
             record("2026-10-10T09:00:02.000Z", "r1",
                    usage(inp=1_000_000, cached=400_000, out=100_000))]
    events = cctrack.parse_codex_lines(lines)
    daily, _ = cctrack.aggregate(events)
    expected = 0.6 * 1.75 + 0.4 * 0.175 + 0.1 * 14.0
    assert abs(daily["2026-10-10"]["cost"] - expected) < 1e-9


def test_long_context_pricing_applies_per_request():
    """gpt-5.5 requests with >272K input bill at the long-context rates."""
    small = {"model": "gpt-5.5", "timestamp": "2026-10-10T10:00:00.000Z",
             "input": 200_000, "output": 0, "cache_read": 0, "cache_write": 0,
             "cache_write_1h": 0, "_request_input": 200_000}
    big = dict(small, input=300_000, _request_input=300_000,
               timestamp="2026-10-11T10:00:00.000Z")
    daily, _ = cctrack.aggregate([small, big])
    assert abs(daily["2026-10-10"]["cost"] - 0.2 * 5.00) < 1e-9
    assert abs(daily["2026-10-11"]["cost"] - 0.3 * 10.00) < 1e-9


def test_model_and_attribution_from_context():
    lines = [session_meta(thread="thr-9", cwd="/home/u/payments", branch="feat/x"),
             turn_context("2026-10-10T09:00:01.000Z", model="gpt-5.4",
                          cwd="/home/u/payments"),
             record("2026-10-10T09:00:02.000Z", "r1", usage(inp=10, out=5), "thr-9")]
    [e] = cctrack.parse_codex_lines(lines, source="air")
    assert e["model"] == "gpt-5.4"
    assert e["agent"] == "codex"
    assert e["project"] == "payments"
    assert e["session_id"] == "thr-9"
    assert e["git_branch"] == "feat/x"
    assert e["source"] == "air"


def test_model_switch_mid_session_is_tracked():
    lines = [session_meta(),
             turn_context("2026-10-10T09:00:01.000Z", model="gpt-5.4"),
             record("2026-10-10T09:00:02.000Z", "r1", usage(inp=10, out=5)),
             turn_context("2026-10-10T09:05:00.000Z", model="gpt-5.5"),
             record("2026-10-10T09:05:02.000Z", "r2", usage(inp=10, out=5))]
    events = cctrack.parse_codex_lines(lines)
    assert [e["model"] for e in events] == ["gpt-5.4", "gpt-5.5"]


# ── Legacy token_count path ─────────────────────────────────────────────

def test_legacy_token_count_uses_last_usage():
    lines = [session_meta(), turn_context("2026-10-10T09:00:01.000Z"),
             token_count("2026-10-10T09:00:02.000Z", usage(inp=100, out=10), 110),
             token_count("2026-10-10T09:01:00.000Z", usage(inp=200, out=20), 330)]
    events = cctrack.parse_codex_lines(lines)
    assert [e["input"] for e in events] == [100, 200]
    assert [e["output"] for e in events] == [10, 20]


def test_legacy_reemission_with_unchanged_total_not_double_counted():
    """token_count is re-emitted (e.g. rate-limit updates) with the same totals."""
    tc = usage(inp=100, out=10)
    lines = [session_meta(), turn_context("2026-10-10T09:00:01.000Z"),
             token_count("2026-10-10T09:00:02.000Z", tc, 110),
             token_count("2026-10-10T09:00:03.000Z", tc, 110),   # re-emission
             token_count("2026-10-10T09:00:04.000Z", tc, 110)]   # re-emission
    events = cctrack.parse_codex_lines(lines)
    assert len(events) == 1


def test_records_preferred_over_token_count_no_double_count():
    """Newer CLIs write both a record and a token_count per response — only the
    record counts. token_count events from before the first record (a session
    begun on an older CLI) still count."""
    lines = [session_meta(), turn_context("2026-10-10T09:00:01.000Z"),
             token_count("2026-10-10T09:00:02.000Z", usage(inp=100, out=10), 110),  # old CLI
             record("2026-10-10T10:00:00.000Z", "r1", usage(inp=50, out=5)),
             token_count("2026-10-10T10:00:00.500Z", usage(inp=50, out=5), 165)]    # dup of r1
    events = cctrack.parse_codex_lines(lines)
    assert sorted(e["input"] for e in events) == [50, 100]


def test_zero_usage_skipped_and_malformed_lines_ignored():
    lines = ["not json", "[]", '{"type": "event_msg", "payload": "x"}',
             session_meta(), turn_context("2026-10-10T09:00:01.000Z"),
             record("2026-10-10T09:00:02.000Z", "r0", usage()),
             token_count("2026-10-10T09:00:03.000Z", usage(), 0)]
    assert cctrack.parse_codex_lines(lines) == []


# ── Files, dedup, discovery, compression ────────────────────────────────

def _write_rollout(root: Path, sub: str, name: str, lines: list[str]) -> Path:
    p = root / sub / "2026" / "10" / "10" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n")
    return p


def test_forked_thread_replay_deduped_by_response_id():
    """A fork replays its parent's history into a new rollout file."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        parent = [session_meta(thread="p"), turn_context("2026-10-10T09:00:01.000Z"),
                  record("2026-10-10T09:00:02.000Z", "r1", usage(inp=100, out=10), "p")]
        fork = parent + [record("2026-10-10T09:30:00.000Z", "r2", usage(inp=7, out=3), "f")]
        _write_rollout(root, "sessions", "rollout-a.jsonl", parent)
        _write_rollout(root, "sessions", "rollout-b.jsonl", fork)
        events, skipped = cctrack.parse_codex_events(cctrack.discover_codex_files(root))
        assert skipped == 0
        assert sorted(e["input"] for e in events) == [7, 100]


def test_discover_includes_archived_and_prefers_plain_over_zst():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        live = _write_rollout(root, "sessions", "rollout-1.jsonl", [session_meta()])
        (live.parent / "rollout-1.jsonl.zst").write_bytes(b"x")          # stale sibling
        zst_only = live.parent / "rollout-2.jsonl.zst"
        zst_only.write_bytes(b"x")
        arch = _write_rollout(root, "archived_sessions", "rollout-3.jsonl", [session_meta()])
        files = cctrack.discover_codex_files(root)
        assert str(live) in files
        assert str(live) + ".zst" not in files
        assert str(zst_only) in files
        assert str(arch) in files


def test_codex_home_env_override(monkeypatch):
    monkeypatch.setenv("CODEX_HOME", "/srv/codexhome")
    assert cctrack.codex_home() == Path("/srv/codexhome")
    monkeypatch.delenv("CODEX_HOME")
    assert cctrack.codex_home() == Path.home() / ".codex"


def test_zst_skipped_and_counted_without_decompressor():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "rollout-x.jsonl.zst"
        p.write_bytes(b"\x28\xb5\x2f\xfd")
        with patch.dict(sys.modules, {"compression": None}), \
             patch("shutil.which", return_value=None):
            events, skipped = cctrack.parse_codex_events([str(p)])
        assert events == [] and skipped == 1


def test_zst_decompressed_via_cli_when_available():
    body = "\n".join([session_meta(), turn_context("2026-10-10T09:00:01.000Z"),
                      record("2026-10-10T09:00:02.000Z", "r1", usage(inp=10, out=5))])
    done = subprocess.CompletedProcess(["zstd"], 0, stdout=body.encode(), stderr=b"")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "rollout-x.jsonl.zst"
        p.write_bytes(b"\x28\xb5\x2f\xfd")
        with patch.dict(sys.modules, {"compression": None}), \
             patch("shutil.which", return_value="/usr/bin/zstd"), \
             patch("subprocess.run", return_value=done):
            events, skipped = cctrack.parse_codex_events([str(p)])
    assert skipped == 0 and len(events) == 1


def test_remote_codex_stream_parsed_and_tagged():
    raw = (f"{cctrack.REMOTE_CODEX_SEP}\n" + "\n".join([
        session_meta(), turn_context("2026-10-10T09:00:01.000Z"),
        record("2026-10-10T09:00:02.000Z", "r1", usage(inp=10, out=5))]) + "\n")
    with patch("cctrack.fetch_remote_codex", return_value=raw):
        events = cctrack.parse_remote_codex_events("dgx")
    assert len(events) == 1
    assert events[0]["source"] == "dgx"
    assert events[0]["_source_file"] == "(remote dgx)"


# ── Rate-card matching ──────────────────────────────────────────────────

def test_codex_variants_inherit_base_price():
    assert cctrack.get_rates("gpt-5-codex")["family"] == "gpt-5"
    assert cctrack.get_rates("gpt-5.1-codex")["family"] == "gpt-5.1"
    assert cctrack.get_rates("gpt-5-2025-08-07")["family"] == "gpt-5"


def test_family_boundary_and_tier_words():
    assert not cctrack._family_matches("gpt-5.5", "gpt-5")
    assert not cctrack._family_matches("gpt-5-mini", "gpt-5")
    # an unverified size variant must not silently borrow its base model's price
    assert not cctrack.is_known_model("gpt-5.1-codex-mini")
    assert cctrack.get_rates("gpt-5.4-mini")["input"] == 0.75


def test_unknown_openai_model_falls_back_to_gpt5_not_sonnet():
    r = cctrack.get_rates("gpt-9-hyperion")
    assert r["family"] == "gpt-5"
    assert not cctrack.is_known_model("gpt-9-hyperion")
    assert cctrack.get_rates("claude-mystery-9")["family"] == "claude-sonnet-4"


def test_pro_models_bill_cached_input_at_full_rate():
    r = cctrack.get_rates("gpt-5.5-pro")
    assert r["cache_read"] == r["input"] == 30.0


# ── Report integration ──────────────────────────────────────────────────

def _bucket(cost, **kw):
    b = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
         "cache_write_1h": 0, "cost": cost, "energy_wh": 0.0, "models": {}}
    b.update(kw)
    return b


def _run_report(daily, monthly, sources, capsys):
    fake_now = datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc)
    with patch("cctrack.datetime") as m:
        m.now.return_value = fake_now
        m.fromisoformat = datetime.fromisoformat
        m.side_effect = lambda *a, **kw: datetime(*a, **kw)
        cctrack.print_report(daily, monthly, sources=sources)
    return capsys.readouterr().out


def test_codex_spend_not_displaced_by_claude_hook(capsys):
    """Regression guard: on a day the Claude hook covers, the same machine's
    Codex spend must still be added (not replaced by the hook figure), and must
    not be scaled by the Claude-derived accuracy factor."""
    claude = {"name": "local", "agent": "claude",
              "daily": {"2026-10-10": _bucket(8.0)},
              "hook_daily": {"2026-10-10": {"cost": 10.0, "sessions": 1}},
              "install_date": "2026-10-01"}
    codex = {"name": "local", "agent": "codex", "exact": True,
             "daily": {"2026-10-10": _bucket(4.0)}, "hook_daily": {},
             "install_date": None}
    daily = {"2026-10-10": _bucket(12.0)}
    monthly = {"2026-10": _bucket(12.0)}
    out = _run_report(daily, monthly, [claude, codex], capsys)
    row = next(l for l in out.splitlines() if "2026-10-10" in l)
    assert "14.00" in row          # 10.00 hook + 4.00 Codex, unscaled
    assert "~" not in row
    assert "By agent:" in out
    assert "Claude Code $10.00" in out and "Codex $4.00" in out


def test_no_agent_line_for_claude_only(capsys):
    claude = {"name": "local", "agent": "claude",
              "daily": {"2026-10-10": _bucket(8.0)}, "hook_daily": {},
              "install_date": None}
    out = _run_report({"2026-10-10": _bucket(8.0)}, {"2026-10": _bucket(8.0)},
                      [claude], capsys)
    assert "By agent:" not in out


def test_explore_hook_divergence_ignores_codex(capsys):
    """The hook only sees Claude, so Codex spend must not trip the divergence
    warning, and the agent split is shown."""
    base = {"timestamp": "2026-10-10T10:00:00Z", "input": 0, "cache_read": 0,
            "cache_write": 0, "cache_write_1h": 0, "source": "local",
            "project": "p", "cwd": "/home/u/p"}
    events = [dict(base, model="claude-opus-5", output=400_000, session_id="c1",
                   agent="claude"),                                   # $10.00
              dict(base, model="gpt-5.3-codex", output=1_000_000, session_id="x1",
                   agent="codex")]                                    # $14.00
    cctrack.print_explore(events, "2026-10-10", hook_by_source={"local": 10.0},
                          hook_by_session={("local", "c1"): 10.0})
    out = capsys.readouterr().out
    assert "⚠" not in out
    assert "Claude Code (hook-authoritative): $10.00" in out
    assert "By agent:" in out and "Codex" in out


def test_main_agent_filter_codex_only(tmp_path, monkeypatch, capsys):
    """End-to-end: --agent codex reads only $CODEX_HOME and reports it."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    _write_rollout(tmp_path / "codex", "sessions", "rollout-1.jsonl", [
        session_meta(), turn_context("2026-10-11T09:00:01.000Z"),
        record("2026-10-11T09:00:02.000Z", "r1", usage(inp=1_000_000, out=0))])
    fake_now = datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc)
    with patch.object(sys, "argv", ["cctrack", "--agent", "codex",
                                    "--dirs", str(tmp_path / "none")]), \
         patch.object(cctrack, "discover_hook_data_dirs",
                      side_effect=AssertionError("Claude hook dirs read")), \
         patch.object(cctrack, "is_hook_installed", return_value=True), \
         patch("cctrack.datetime") as m:
        m.now.return_value = fake_now
        m.fromisoformat = datetime.fromisoformat
        m.strptime = datetime.strptime
        m.side_effect = lambda *a, **kw: datetime(*a, **kw)
        cctrack.main()
    out = capsys.readouterr().out
    assert "2026-10-11" in out
    assert "1.75" in out          # 1M uncached input × $1.75
    assert "5.3-codex" in out
