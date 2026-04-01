"""Tests for cctrack.py"""

import json
import os
import tempfile
from pathlib import Path

import cctrack


# ── Helpers ─────────────────────────────────────────────────────────────

def make_event(
    model="claude-sonnet-4-20250514",
    timestamp="2026-03-15T10:00:00Z",
    request_id="req_001",
    session_id="sess_001",
    input_tokens=1000,
    output_tokens=500,
    cache_read=200,
    cache_write=100,
    event_type="assistant",
):
    """Build a JSONL event dict."""
    return {
        "type": event_type,
        "sessionId": session_id,
        "requestId": request_id,
        "timestamp": timestamp,
        "message": {
            "model": model,
            "role": "assistant",
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_write,
            },
        },
    }


def write_jsonl(dir_path: str, filename: str, events: list[dict]) -> str:
    """Write events as JSONL to dir_path/filename, return full path."""
    path = os.path.join(dir_path, filename)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    return path


# ── Rate card tests ─────────────────────────────────────────────────────

def test_get_rates_opus_4_6():
    r = cctrack.get_rates("claude-opus-4-6")
    assert r["family"] == "claude-opus-4-6"
    assert r["input"] == 5.0
    assert r["output"] == 25.0
    assert r["cache_read"] == 0.50
    assert r["cache_write"] == 6.25


def test_get_rates_opus_4_5():
    r = cctrack.get_rates("claude-opus-4-5-20250501")
    assert r["family"] == "claude-opus-4-5"
    assert r["input"] == 5.0


def test_get_rates_opus_4():
    """Opus 4 / 4.1 use the older, higher rate."""
    r = cctrack.get_rates("claude-opus-4-20250401")
    assert r["family"] == "claude-opus-4"
    assert r["input"] == 15.0
    assert r["output"] == 75.0


def test_get_rates_opus_4_1():
    r = cctrack.get_rates("claude-opus-4-1-20250501")
    assert r["family"] == "claude-opus-4"
    assert r["input"] == 15.0


def test_get_rates_sonnet():
    r = cctrack.get_rates("claude-sonnet-4-20250514")
    assert r["family"] == "claude-sonnet-4"
    assert r["input"] == 3.0
    assert r["output"] == 15.0


def test_get_rates_sonnet_4_6():
    """Sonnet 4.6 still matches the claude-sonnet-4 prefix."""
    r = cctrack.get_rates("claude-sonnet-4-6-20260301")
    assert r["family"] == "claude-sonnet-4"
    assert r["input"] == 3.0


def test_get_rates_haiku_4_5():
    r = cctrack.get_rates("claude-haiku-4-5-20251001")
    assert r["family"] == "claude-haiku-4-5"
    assert r["input"] == 1.0
    assert r["output"] == 5.0


def test_get_rates_haiku_3_5():
    r = cctrack.get_rates("claude-haiku-3-5-20241022")
    assert r["family"] == "claude-haiku-3"
    assert r["input"] == 0.80
    assert r["output"] == 4.0


def test_get_rates_unknown_falls_back_to_sonnet():
    r = cctrack.get_rates("claude-mystery-99")
    assert r["family"] == "claude-sonnet-4"


def test_get_rates_empty_string():
    r = cctrack.get_rates("")
    assert r["family"] == "claude-sonnet-4"


# ── Cost calculation tests ──────────────────────────────────────────────

def test_calculate_cost_sonnet():
    # 1M input tokens at $3/M = $3.00
    cost = cctrack.calculate_cost("claude-sonnet-4", 1_000_000, 0, 0, 0)
    assert abs(cost - 3.0) < 0.001


def test_calculate_cost_opus_4_output():
    # Opus 4: 1M output tokens at $75/M = $75.00
    cost = cctrack.calculate_cost("claude-opus-4-20250401", 0, 1_000_000, 0, 0)
    assert abs(cost - 75.0) < 0.001


def test_calculate_cost_opus_4_6_output():
    # Opus 4.6: 1M output tokens at $25/M = $25.00
    cost = cctrack.calculate_cost("claude-opus-4-6", 0, 1_000_000, 0, 0)
    assert abs(cost - 25.0) < 0.001


def test_calculate_cost_mixed():
    # Sonnet: 500k input ($1.50) + 100k output ($1.50) + 200k cache_read ($0.06) + 50k cache_write ($0.1875)
    cost = cctrack.calculate_cost("claude-sonnet-4", 500_000, 100_000, 200_000, 50_000)
    expected = 1.50 + 1.50 + 0.06 + 0.1875
    assert abs(cost - expected) < 0.001


def test_calculate_cost_zero_tokens():
    cost = cctrack.calculate_cost("claude-sonnet-4", 0, 0, 0, 0)
    assert cost == 0.0


# ── File discovery tests ───────────────────────────────────────────────

def test_discover_files_finds_jsonl():
    with tempfile.TemporaryDirectory() as d:
        write_jsonl(d, "project1/sess1.jsonl", [make_event()])
        write_jsonl(d, "project1/sess2.jsonl", [make_event()])
        write_jsonl(d, "project2/sess3.jsonl", [make_event()])

        files = cctrack.discover_files([d])
        assert len(files) == 3
        assert all(f.endswith(".jsonl") for f in files)


def test_discover_files_skips_nonexistent():
    files = cctrack.discover_files(["/nonexistent/path/that/doesnt/exist"])
    assert files == []


def test_discover_files_ignores_non_jsonl():
    with tempfile.TemporaryDirectory() as d:
        write_jsonl(d, "project/sess.jsonl", [make_event()])
        Path(os.path.join(d, "project", "readme.txt")).write_text("hello")

        files = cctrack.discover_files([d])
        assert len(files) == 1


def test_discover_files_multiple_dirs():
    with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
        write_jsonl(d1, "a.jsonl", [make_event()])
        write_jsonl(d2, "b.jsonl", [make_event()])

        files = cctrack.discover_files([d1, d2])
        assert len(files) == 2


def test_discover_files_nested_subagents():
    with tempfile.TemporaryDirectory() as d:
        write_jsonl(d, "project/sess-uuid/subagents/agent-abc.jsonl", [make_event()])
        files = cctrack.discover_files([d])
        assert len(files) == 1


# ── Parsing tests ──────────────────────────────────────────────────────

def test_parse_basic_event():
    with tempfile.TemporaryDirectory() as d:
        path = write_jsonl(d, "s.jsonl", [
            make_event(input_tokens=1000, output_tokens=500),
        ])
        events = cctrack.parse_events([path])
        assert len(events) == 1
        assert events[0]["input"] == 1000
        assert events[0]["output"] == 500


def test_parse_skips_non_assistant():
    with tempfile.TemporaryDirectory() as d:
        path = write_jsonl(d, "s.jsonl", [
            make_event(event_type="user"),
            make_event(event_type="assistant"),
        ])
        events = cctrack.parse_events([path])
        assert len(events) == 1


def test_parse_skips_zero_usage():
    with tempfile.TemporaryDirectory() as d:
        path = write_jsonl(d, "s.jsonl", [
            make_event(input_tokens=0, output_tokens=0, cache_read=0, cache_write=0),
            make_event(input_tokens=100, output_tokens=0, cache_read=0, cache_write=0),
        ])
        events = cctrack.parse_events([path])
        assert len(events) == 1


def test_parse_dedup_by_request_id():
    """Last event with same request_id wins."""
    with tempfile.TemporaryDirectory() as d:
        path = write_jsonl(d, "s.jsonl", [
            make_event(request_id="req_1", input_tokens=100),
            make_event(request_id="req_1", input_tokens=200),  # should win
            make_event(request_id="req_2", input_tokens=300),
        ])
        events = cctrack.parse_events([path])
        assert len(events) == 2
        by_input = {e["input"]: e for e in events}
        assert 200 in by_input  # deduped: second req_1 wins
        assert 300 in by_input


def test_parse_no_request_id_not_deduped():
    """Events without request_id are all kept."""
    with tempfile.TemporaryDirectory() as d:
        path = write_jsonl(d, "s.jsonl", [
            make_event(request_id="", input_tokens=100),
            make_event(request_id="", input_tokens=200),
        ])
        events = cctrack.parse_events([path])
        assert len(events) == 2


def test_parse_malformed_jsonl():
    """Malformed lines are skipped without crashing."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.jsonl")
        with open(path, "w") as f:
            f.write("not json at all\n")
            f.write('{"type": "assistant", "broken json\n')
            f.write(json.dumps(make_event(input_tokens=42)) + "\n")

        events = cctrack.parse_events([path])
        assert len(events) == 1
        assert events[0]["input"] == 42


def test_parse_empty_file():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "empty.jsonl")
        Path(path).write_text("")
        events = cctrack.parse_events([path])
        assert events == []


# ── parse_lines tests (shared between local and remote) ────────────────

def test_parse_lines_basic():
    lines = [json.dumps(make_event(input_tokens=500))]
    events = cctrack.parse_lines(lines)
    assert len(events) == 1
    assert events[0]["input"] == 500


def test_parse_lines_dedup():
    lines = [
        json.dumps(make_event(request_id="r1", input_tokens=100)),
        json.dumps(make_event(request_id="r1", input_tokens=200)),
    ]
    events = cctrack.parse_lines(lines)
    assert len(events) == 1
    assert events[0]["input"] == 200


# ── Remote support tests ──────────────────────────────────────────────

def test_parse_remote_events_splits_by_file():
    """Each file chunk gets independent dedup."""
    from unittest.mock import patch

    # Two "files", each with a req_1. They should NOT dedup across files.
    file1 = json.dumps(make_event(request_id="req_1", input_tokens=100))
    file2 = json.dumps(make_event(request_id="req_1", input_tokens=200))
    raw = f"___CCTRACK_FILE_SEP___\n{file1}\n___CCTRACK_FILE_SEP___\n{file2}\n"

    with patch("cctrack.fetch_remote_jsonl", return_value=raw):
        events = cctrack.parse_remote_events("fakehost")
    assert len(events) == 2


def test_parse_remote_events_empty():
    from unittest.mock import patch

    with patch("cctrack.fetch_remote_jsonl", return_value=""):
        events = cctrack.parse_remote_events("fakehost")
    assert events == []


def test_fetch_remote_jsonl_timeout():
    from unittest.mock import patch

    with patch("subprocess.run", side_effect=cctrack.subprocess.TimeoutExpired("ssh", 60)):
        result = cctrack.fetch_remote_jsonl("fakehost")
    assert result == ""


def test_fetch_remote_jsonl_ssh_failure():
    from unittest.mock import patch, MagicMock

    mock_result = MagicMock()
    mock_result.returncode = 255
    mock_result.stdout = ""
    mock_result.stderr = "Connection refused"

    with patch("subprocess.run", return_value=mock_result):
        result = cctrack.fetch_remote_jsonl("fakehost")
    assert result == ""


def test_parse_blank_lines():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.jsonl")
        with open(path, "w") as f:
            f.write("\n\n")
            f.write(json.dumps(make_event()) + "\n")
            f.write("\n")

        events = cctrack.parse_events([path])
        assert len(events) == 1


def test_parse_inaccessible_file():
    """Inaccessible files are skipped without crashing."""
    events = cctrack.parse_events(["/nonexistent/file.jsonl"])
    assert events == []


# ── Aggregation tests ──────────────────────────────────────────────────

def test_aggregate_single_day():
    events = [
        {"model": "claude-sonnet-4", "timestamp": "2026-03-15T10:00:00Z",
         "input": 1000, "output": 500, "cache_read": 0, "cache_write": 0},
        {"model": "claude-sonnet-4", "timestamp": "2026-03-15T14:00:00Z",
         "input": 2000, "output": 1000, "cache_read": 0, "cache_write": 0},
    ]
    daily, monthly = cctrack.aggregate(events)
    assert "2026-03-15" in daily
    d = daily["2026-03-15"]
    assert d["input"] == 3000
    assert d["output"] == 1500
    assert cctrack.total_tokens(d) == 4500
    assert "2026-03" in monthly
    assert cctrack.total_tokens(monthly["2026-03"]) == 4500


def test_aggregate_multiple_days():
    events = [
        {"model": "claude-sonnet-4", "timestamp": "2026-03-15T10:00:00Z",
         "input": 1000, "output": 0, "cache_read": 0, "cache_write": 0},
        {"model": "claude-sonnet-4", "timestamp": "2026-03-16T10:00:00Z",
         "input": 2000, "output": 0, "cache_read": 0, "cache_write": 0},
    ]
    daily, monthly = cctrack.aggregate(events)
    assert len(daily) == 2
    assert daily["2026-03-15"]["input"] == 1000
    assert daily["2026-03-16"]["input"] == 2000
    assert monthly["2026-03"]["input"] == 3000


def test_aggregate_multiple_months():
    events = [
        {"model": "claude-sonnet-4", "timestamp": "2026-02-15T10:00:00Z",
         "input": 1000, "output": 0, "cache_read": 0, "cache_write": 0},
        {"model": "claude-sonnet-4", "timestamp": "2026-03-15T10:00:00Z",
         "input": 2000, "output": 0, "cache_read": 0, "cache_write": 0},
    ]
    daily, monthly = cctrack.aggregate(events)
    assert "2026-02" in monthly
    assert "2026-03" in monthly


def test_aggregate_cost_calculation():
    """Verify cost flows through correctly."""
    events = [
        {"model": "claude-sonnet-4", "timestamp": "2026-03-15T10:00:00Z",
         "input": 1_000_000, "output": 0, "cache_read": 0, "cache_write": 0},
    ]
    daily, monthly = cctrack.aggregate(events)
    # 1M input tokens at $3/M
    assert abs(daily["2026-03-15"]["cost"] - 3.0) < 0.001


def test_aggregate_empty_events():
    daily, monthly = cctrack.aggregate([])
    assert daily == {}
    assert monthly == {}


def test_aggregate_skips_empty_timestamp():
    events = [
        {"model": "claude-sonnet-4", "timestamp": "",
         "input": 1000, "output": 0, "cache_read": 0, "cache_write": 0},
    ]
    daily, monthly = cctrack.aggregate(events)
    assert daily == {}


def test_aggregate_skips_bad_timestamp():
    events = [
        {"model": "claude-sonnet-4", "timestamp": "not-a-date",
         "input": 1000, "output": 0, "cache_read": 0, "cache_write": 0},
    ]
    daily, monthly = cctrack.aggregate(events)
    assert daily == {}


# ── Output tests ───────────────────────────────────────────────────────

def test_format_tokens():
    assert cctrack.format_tokens(0) == "0"
    assert cctrack.format_tokens(1234) == "1,234"
    assert cctrack.format_tokens(1_234_567) == "1,234,567"


def test_print_report_empty(capsys):
    cctrack.print_report({}, {})
    out = capsys.readouterr().out
    assert "no log data found" in out


def make_bucket(input=0, output=0, cache_read=0, cache_write=0, cost=0.0):
    return {"input": input, "output": output, "cache_read": cache_read, "cache_write": cache_write, "cost": cost}


def test_print_report_with_data(capsys):
    """Use a date in the current month so it appears in the daily breakdown."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    month_str = now.strftime("%Y-%m")

    daily = {date_str: make_bucket(input=3000, output=2000, cost=1.50)}
    monthly = {month_str: make_bucket(input=3000, output=2000, cost=1.50)}
    cctrack.print_report(daily, monthly)
    out = capsys.readouterr().out
    assert "Claude Code Cost Report" in out
    assert date_str in out
    assert "1.50" in out
    assert "3,000" in out   # input tokens shown
    assert "2,000" in out   # output tokens shown


def test_print_report_month_to_date(capsys):
    """Current month shows MTD stats including avg/day and projected."""
    from unittest.mock import patch
    from datetime import datetime, timezone

    fake_now = datetime(2026, 3, 20, 12, 0, 0, tzinfo=timezone.utc)
    daily = {
        "2026-03-18": make_bucket(input=500, output=500, cost=3.00),
        "2026-03-19": make_bucket(input=1000, output=1000, cost=6.00),
        "2026-03-20": make_bucket(input=750, output=750, cost=4.50),
    }
    monthly = {"2026-03": make_bucket(input=2250, output=2250, cost=13.50)}

    with patch("cctrack.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.fromisoformat = datetime.fromisoformat
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        cctrack.print_report(daily, monthly)

    out = capsys.readouterr().out
    assert "month to date" in out
    assert "day 20" in out
    assert "3 active" in out
    assert "Input tokens:" in out
    assert "Output tokens:" in out
    assert "Avg/day" in out
    assert "$4.50" in out  # 13.50 / 3 days
    assert "Projected" in out


def test_print_report_previous_months(capsys):
    """Previous months appear in the report with day counts."""
    from unittest.mock import patch
    from datetime import datetime, timezone

    fake_now = datetime(2026, 3, 20, 12, 0, 0, tzinfo=timezone.utc)
    daily = {
        "2026-03-20": make_bucket(input=1000, cost=3.00),
        "2026-02-10": make_bucket(input=2000, cost=6.00),
        "2026-02-11": make_bucket(input=3000, cost=9.00),
    }
    monthly = {
        "2026-03": make_bucket(input=1000, cost=3.00),
        "2026-02": make_bucket(input=5000, cost=15.00),
    }

    with patch("cctrack.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.fromisoformat = datetime.fromisoformat
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        cctrack.print_report(daily, monthly)

    out = capsys.readouterr().out
    assert "Previous months:" in out
    assert "2026-02" in out
    assert "2d" in out
    assert "15.00" in out


def test_print_report_days_limit(capsys):
    from unittest.mock import patch
    from datetime import datetime, timezone

    fake_now = datetime(2026, 3, 20, 12, 0, 0, tzinfo=timezone.utc)
    daily = {
        "2026-03-15": make_bucket(input=1000, cost=0.50),
        "2026-03-14": make_bucket(input=2000, cost=1.00),
        "2026-03-13": make_bucket(input=3000, cost=1.50),
    }
    monthly = {"2026-03": make_bucket(input=6000, cost=3.00)}

    with patch("cctrack.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.fromisoformat = datetime.fromisoformat
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        cctrack.print_report(daily, monthly, days=2)

    out = capsys.readouterr().out
    assert "2026-03-15" in out
    assert "2026-03-14" in out
    assert "2026-03-13" not in out


# ── End-to-end test ────────────────────────────────────────────────────

def test_end_to_end():
    """Full pipeline: write JSONL → parse → aggregate → verify numbers."""
    with tempfile.TemporaryDirectory() as d:
        events = [
            make_event(
                model="claude-opus-4-6",
                timestamp="2026-03-15T10:00:00Z",
                request_id="req_1",
                input_tokens=100_000,
                output_tokens=50_000,
                cache_read=10_000,
                cache_write=5_000,
            ),
            make_event(
                model="claude-sonnet-4-20250514",
                timestamp="2026-03-15T12:00:00Z",
                request_id="req_2",
                input_tokens=200_000,
                output_tokens=100_000,
                cache_read=20_000,
                cache_write=10_000,
            ),
            # This is a dupe of req_1 with higher tokens — should replace
            make_event(
                model="claude-opus-4-6",
                timestamp="2026-03-15T10:05:00Z",
                request_id="req_1",
                input_tokens=150_000,
                output_tokens=60_000,
                cache_read=15_000,
                cache_write=8_000,
            ),
        ]
        write_jsonl(d, "project/sess.jsonl", events)

        files = cctrack.discover_files([d])
        parsed = cctrack.parse_events(files)

        # 2 events after dedup (req_1 deduped, req_2 kept)
        assert len(parsed) == 2

        daily, monthly = cctrack.aggregate(parsed)
        assert "2026-03-15" in daily

        d = daily["2026-03-15"]
        # req_1 (deduped, second version): input=150k, output=60k, cr=15k, cw=8k
        # req_2: input=200k, output=100k, cr=20k, cw=10k
        assert d["input"] == 150_000 + 200_000
        assert d["output"] == 60_000 + 100_000
        assert d["cache_read"] == 15_000 + 20_000
        assert d["cache_write"] == 8_000 + 10_000
        assert cctrack.total_tokens(d) == 233_000 + 330_000

        # req_1 cost (opus 4.6): 150k/1M*5 + 60k/1M*25 + 15k/1M*0.5 + 8k/1M*6.25
        opus_cost = 0.15 * 5 + 0.06 * 25 + 0.015 * 0.5 + 0.008 * 6.25
        # req_2 cost (sonnet): 200k/1M*3 + 100k/1M*15 + 20k/1M*0.3 + 10k/1M*3.75
        sonnet_cost = 0.2 * 3 + 0.1 * 15 + 0.02 * 0.3 + 0.01 * 3.75

        expected_cost = opus_cost + sonnet_cost
        assert abs(daily["2026-03-15"]["cost"] - expected_cost) < 0.001
