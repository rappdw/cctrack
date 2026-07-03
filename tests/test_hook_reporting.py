"""Tests for hook data parsing and integrated reporting."""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch
from datetime import datetime, timezone

import cctrack


# ── Helpers ─────────────────────────────────────────────────────────────

def make_hook_entry(
    session_id="sess_001",
    ts="2026-04-15T12:00:00+00:00",
    cost_usd=1.23,
    model="claude-opus-4-6",
    input_tokens=50000,
    output_tokens=10000,
    cache_read=300,
    cache_write=200,
):
    """Build a statusline hook JSONL entry."""
    return {
        "_ts": ts,
        "session_id": session_id,
        "cost_usd": cost_usd,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read": cache_read,
        "cache_write": cache_write,
    }


def write_hook_jsonl(dir_path: str, entries: list[dict], date: str = "2026-04-15") -> str:
    """Write hook entries as a daily JSONL file. Returns the file path."""
    path = os.path.join(dir_path, f"statusline-{date}.jsonl")
    with open(path, "a") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    return path


def make_bucket(input=0, output=0, cache_read=0, cache_write=0, cost=0.0):
    return {"input": input, "output": output, "cache_read": cache_read,
            "cache_write": cache_write, "cost": cost}


# ── parse_hook_events tests ────────────────────────────────────────────

def test_parse_hook_events_basic():
    """Parse a single hook entry."""
    with tempfile.TemporaryDirectory() as d:
        write_hook_jsonl(d, [
            make_hook_entry(session_id="sess_001", cost_usd=1.50),
        ])
        events = cctrack.parse_hook_events(cctrack_dir=Path(d))
        assert len(events) == 1
        assert events[0]["session_id"] == "sess_001"
        assert events[0]["cost_usd"] == 1.50
        assert events[0]["date_str"] == "2026-04-15"
        assert events[0]["model"] == "claude-opus-4-6"
        assert events[0]["input_tokens"] == 50000
        assert events[0]["output_tokens"] == 10000


def test_parse_hook_events_dedup_by_session_id():
    """Multiple entries per session_id: last by _ts wins."""
    with tempfile.TemporaryDirectory() as d:
        write_hook_jsonl(d, [
            make_hook_entry(session_id="sess_A", ts="2026-04-15T10:00:00+00:00", cost_usd=0.50),
            make_hook_entry(session_id="sess_A", ts="2026-04-15T12:00:00+00:00", cost_usd=1.50),
            make_hook_entry(session_id="sess_A", ts="2026-04-15T11:00:00+00:00", cost_usd=1.00),
            make_hook_entry(session_id="sess_B", ts="2026-04-15T10:00:00+00:00", cost_usd=2.00),
        ])
        events = cctrack.parse_hook_events(cctrack_dir=Path(d))
        assert len(events) == 2
        by_session = {e["session_id"]: e for e in events}
        assert by_session["sess_A"]["cost_usd"] == 1.50
        assert by_session["sess_B"]["cost_usd"] == 2.00


def test_parse_hook_events_empty_file():
    """Empty file returns empty list."""
    with tempfile.TemporaryDirectory() as d:
        Path(d, "statusline-2026-04-15.jsonl").write_text("")
        events = cctrack.parse_hook_events(cctrack_dir=Path(d))
        assert events == []


def test_parse_hook_events_missing_dir():
    """Missing directory returns empty list."""
    events = cctrack.parse_hook_events(cctrack_dir=Path("/nonexistent/dir"))
    assert events == []


def test_parse_hook_events_malformed_lines():
    """Malformed lines are skipped."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "statusline-2026-04-15.jsonl")
        with open(path, "w") as f:
            f.write("not json\n")
            f.write('{"broken\n')
            f.write(json.dumps(make_hook_entry(cost_usd=3.33)) + "\n")
        events = cctrack.parse_hook_events(cctrack_dir=Path(d))
        assert len(events) == 1
        assert events[0]["cost_usd"] == 3.33


def test_parse_hook_events_no_session_id_skipped():
    """Entries without session_id are skipped."""
    with tempfile.TemporaryDirectory() as d:
        entry = make_hook_entry()
        del entry["session_id"]
        write_hook_jsonl(d, [entry])
        events = cctrack.parse_hook_events(cctrack_dir=Path(d))
        assert events == []


def test_parse_hook_events_no_ts_skipped():
    """Entries without _ts are skipped."""
    with tempfile.TemporaryDirectory() as d:
        entry = make_hook_entry()
        del entry["_ts"]
        write_hook_jsonl(d, [entry])
        events = cctrack.parse_hook_events(cctrack_dir=Path(d))
        assert events == []


def test_parse_hook_events_multiple_dates():
    """Events across multiple dates are parsed correctly."""
    with tempfile.TemporaryDirectory() as d:
        write_hook_jsonl(d, [
            make_hook_entry(session_id="s1", ts="2026-04-14T10:00:00+00:00", cost_usd=1.00),
        ], date="2026-04-14")
        write_hook_jsonl(d, [
            make_hook_entry(session_id="s2", ts="2026-04-15T10:00:00+00:00", cost_usd=2.00),
        ], date="2026-04-15")
        write_hook_jsonl(d, [
            make_hook_entry(session_id="s3", ts="2026-04-16T10:00:00+00:00", cost_usd=3.00),
        ], date="2026-04-16")
        events = cctrack.parse_hook_events(cctrack_dir=Path(d))
        assert len(events) == 3
        dates = {e["date_str"] for e in events}
        assert dates == {"2026-04-14", "2026-04-15", "2026-04-16"}


def test_parse_hook_events_reads_legacy_file():
    """Legacy statusline.jsonl is also read."""
    with tempfile.TemporaryDirectory() as d:
        legacy_path = os.path.join(d, "statusline.jsonl")
        with open(legacy_path, "w") as f:
            f.write(json.dumps(make_hook_entry(session_id="legacy_1", cost_usd=5.00)) + "\n")
        events = cctrack.parse_hook_events(cctrack_dir=Path(d))
        assert len(events) == 1
        assert events[0]["session_id"] == "legacy_1"


def test_parse_hook_events_session_spans_multiple_days():
    """A session active on multiple days returns one record per (session, day)
    with that day's maximum cumulative values."""
    with tempfile.TemporaryDirectory() as d:
        write_hook_jsonl(d, [
            make_hook_entry(session_id="sA", ts="2026-04-14T10:00:00+00:00", cost_usd=2.00, input_tokens=100, output_tokens=50),
            make_hook_entry(session_id="sA", ts="2026-04-14T18:00:00+00:00", cost_usd=5.00, input_tokens=300, output_tokens=150),
        ], date="2026-04-14")
        write_hook_jsonl(d, [
            make_hook_entry(session_id="sA", ts="2026-04-15T09:00:00+00:00", cost_usd=8.00, input_tokens=500, output_tokens=250),
            make_hook_entry(session_id="sA", ts="2026-04-15T16:00:00+00:00", cost_usd=12.00, input_tokens=800, output_tokens=400),
        ], date="2026-04-15")
        events = cctrack.parse_hook_events(cctrack_dir=Path(d))
        by_date = {e["date_str"]: e for e in events}
        assert len(events) == 2
        assert by_date["2026-04-14"]["cost_usd"] == 5.00
        assert by_date["2026-04-14"]["input_tokens"] == 300
        assert by_date["2026-04-15"]["cost_usd"] == 12.00
        assert by_date["2026-04-15"]["input_tokens"] == 800


# ── compute_hook_deltas tests ──────────────────────────────────────────

def test_compute_hook_deltas_single_day_session():
    """Session visible on one day produces one delta equal to its cumulative."""
    events = [
        {"date_str": "2026-04-15", "session_id": "s1", "cost_usd": 3.0,
         "input_tokens": 100, "output_tokens": 50, "cache_read": 0, "cache_write": 0, "model": "m"},
    ]
    deltas = cctrack.compute_hook_deltas(events)
    assert len(deltas) == 1
    assert deltas[0]["cost_usd"] == 3.0
    assert deltas[0]["input_tokens"] == 100
    assert deltas[0]["output_tokens"] == 50


def test_compute_hook_deltas_session_spans_days():
    """A session spanning days produces one delta per day, each being the
    day-over-day increment rather than the cumulative value."""
    events = [
        {"date_str": "2026-04-14", "session_id": "sA", "cost_usd": 5.00,
         "input_tokens": 300, "output_tokens": 150, "cache_read": 0, "cache_write": 0, "model": "m"},
        {"date_str": "2026-04-15", "session_id": "sA", "cost_usd": 12.00,
         "input_tokens": 800, "output_tokens": 400, "cache_read": 0, "cache_write": 0, "model": "m"},
    ]
    deltas = cctrack.compute_hook_deltas(events)
    by_date = {d["date_str"]: d for d in deltas}
    assert abs(by_date["2026-04-14"]["cost_usd"] - 5.00) < 0.001
    assert abs(by_date["2026-04-15"]["cost_usd"] - 7.00) < 0.001
    assert by_date["2026-04-14"]["input_tokens"] == 300
    assert by_date["2026-04-15"]["input_tokens"] == 500
    assert by_date["2026-04-14"]["output_tokens"] == 150
    assert by_date["2026-04-15"]["output_tokens"] == 250


def test_compute_hook_deltas_drops_zero_records():
    """A record whose cumulative didn't advance from the prior day is dropped."""
    events = [
        {"date_str": "2026-04-14", "session_id": "sA", "cost_usd": 5.0,
         "input_tokens": 100, "output_tokens": 50, "cache_read": 0, "cache_write": 0, "model": "m"},
        {"date_str": "2026-04-15", "session_id": "sA", "cost_usd": 5.0,
         "input_tokens": 100, "output_tokens": 50, "cache_read": 0, "cache_write": 0, "model": "m"},
    ]
    deltas = cctrack.compute_hook_deltas(events)
    assert len(deltas) == 1
    assert deltas[0]["date_str"] == "2026-04-14"


def test_compute_hook_deltas_clamps_negatives():
    """Cumulative values should never decrease, but if they do (bad data),
    deltas clamp to zero rather than produce negative costs."""
    events = [
        {"date_str": "2026-04-14", "session_id": "sA", "cost_usd": 5.0,
         "input_tokens": 100, "output_tokens": 50, "cache_read": 0, "cache_write": 0, "model": "m"},
        {"date_str": "2026-04-15", "session_id": "sA", "cost_usd": 3.0,
         "input_tokens": 80, "output_tokens": 40, "cache_read": 0, "cache_write": 0, "model": "m"},
    ]
    deltas = cctrack.compute_hook_deltas(events)
    # Day 2 delta is negative, so it's dropped entirely (all-zero after clamping).
    assert len(deltas) == 1
    assert deltas[0]["date_str"] == "2026-04-14"


def test_compute_hook_deltas_multiple_sessions_independent():
    """Each session's deltas are computed independently."""
    events = [
        {"date_str": "2026-04-14", "session_id": "sA", "cost_usd": 10.0,
         "input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_write": 0, "model": "m"},
        {"date_str": "2026-04-15", "session_id": "sA", "cost_usd": 15.0,
         "input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_write": 0, "model": "m"},
        {"date_str": "2026-04-15", "session_id": "sB", "cost_usd": 4.0,
         "input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_write": 0, "model": "m"},
    ]
    deltas = cctrack.compute_hook_deltas(events)
    by_key = {(d["date_str"], d["session_id"]): d["cost_usd"] for d in deltas}
    assert by_key[("2026-04-14", "sA")] == 10.0
    assert by_key[("2026-04-15", "sA")] == 5.0
    assert by_key[("2026-04-15", "sB")] == 4.0


# ── Remote hook data tests ─────────────────────────────────────────────

def _remote_raw(hook_chunks: list[list[dict]], monthly_chunks: list[dict],
                baseline_chunks: list[dict] | None = None) -> str:
    """Build a raw SSH output stream the way fetch_remote_hook_data returns it."""
    parts = []
    for entries in hook_chunks:
        parts.append(cctrack.HOOK_FILE_SEP)
        for e in entries:
            parts.append(json.dumps(e))
    for doc in monthly_chunks:
        parts.append(cctrack.MONTHLY_FILE_SEP)
        parts.append(json.dumps(doc))
    for doc in (baseline_chunks or []):
        parts.append(cctrack.BASELINE_FILE_SEP)
        parts.append(json.dumps(doc))
    return "\n".join(parts) + "\n"


def test_parse_remote_hook_data_basic():
    """Statusline entries and monthly summaries are both parsed from the stream."""
    raw = _remote_raw(
        hook_chunks=[
            [make_hook_entry(session_id="r1", ts="2026-04-15T10:00:00+00:00", cost_usd=3.00)],
            [make_hook_entry(session_id="r2", ts="2026-04-16T10:00:00+00:00", cost_usd=7.00)],
        ],
        monthly_chunks=[
            {"month": "2026-01", "days": {"2026-01-10": {"cost": 10.0, "sessions": 2}}},
        ],
    )
    with patch("cctrack.fetch_remote_hook_data", return_value=raw):
        events, monthly, baselines = cctrack.parse_remote_hook_data("fakehost")
    assert len(events) == 2
    by_session = {e["session_id"]: e for e in events}
    assert by_session["r1"]["cost_usd"] == 3.00
    assert by_session["r2"]["date_str"] == "2026-04-16"
    assert monthly == {"2026-01-10": {"cost": 10.0, "sessions": 2}}
    assert baselines == {}


def test_parse_remote_hook_data_dedup_across_files():
    """Same (session, date) in multiple remote files: latest _ts wins."""
    raw = _remote_raw(
        hook_chunks=[
            [make_hook_entry(session_id="rA", ts="2026-04-15T10:00:00+00:00", cost_usd=2.00)],
            [make_hook_entry(session_id="rA", ts="2026-04-15T18:00:00+00:00", cost_usd=6.00)],
        ],
        monthly_chunks=[],
    )
    with patch("cctrack.fetch_remote_hook_data", return_value=raw):
        events, monthly, baselines = cctrack.parse_remote_hook_data("fakehost")
    assert len(events) == 1
    assert events[0]["cost_usd"] == 6.00


def test_parse_remote_hook_data_empty():
    with patch("cctrack.fetch_remote_hook_data", return_value=""):
        events, monthly, baselines = cctrack.parse_remote_hook_data("fakehost")
    assert events == []
    assert monthly == {}
    assert baselines == {}


def test_parse_remote_hook_data_malformed_monthly_skipped():
    raw = (cctrack.HOOK_FILE_SEP + "\n"
           + json.dumps(make_hook_entry(session_id="ok", cost_usd=1.0)) + "\n"
           + cctrack.MONTHLY_FILE_SEP + "\n"
           + "not json\n")
    with patch("cctrack.fetch_remote_hook_data", return_value=raw):
        events, monthly, baselines = cctrack.parse_remote_hook_data("fakehost")
    assert len(events) == 1
    assert monthly == {}


def test_parse_remote_hook_data_baselines():
    """Session baselines from the remote host are parsed and returned."""
    raw = _remote_raw(
        hook_chunks=[[make_hook_entry(session_id="rA", cost_usd=8.0)]],
        monthly_chunks=[],
        baseline_chunks=[
            {"rA": {"date": "2026-04-10", "cost_usd": 5.0, "input_tokens": 100, "output_tokens": 50}},
        ],
    )
    with patch("cctrack.fetch_remote_hook_data", return_value=raw):
        events, monthly, baselines = cctrack.parse_remote_hook_data("fakehost")
    assert baselines["rA"]["cost_usd"] == 5.0


def test_fetch_remote_hook_data_timeout():
    with patch("subprocess.run", side_effect=cctrack.subprocess.TimeoutExpired("ssh", 60)):
        result = cctrack.fetch_remote_hook_data("fakehost")
    assert result == ""


def test_merge_monthly_summaries_sums_overlapping_dates():
    """Same date on two hosts sums cost and sessions."""
    target = {"2026-01-10": {"cost": 10.0, "sessions": 2}}
    cctrack.merge_monthly_summaries(target, {
        "2026-01-10": {"cost": 5.0, "sessions": 1},
        "2026-01-11": {"cost": 3.0, "sessions": 1},
    })
    assert target["2026-01-10"] == {"cost": 15.0, "sessions": 3}
    assert target["2026-01-11"] == {"cost": 3.0, "sessions": 1}


def test_remote_and_local_hook_costs_sum_per_date():
    """Regression: a date covered by hooks on two hosts must sum both hosts'
    costs, not keep just one host's value."""
    local_events = cctrack.compute_hook_deltas([
        {"date_str": "2026-06-11", "session_id": "local_s", "cost_usd": 9.05,
         "input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_write": 0, "model": "m"},
    ])
    remote_events = cctrack.compute_hook_deltas([
        {"date_str": "2026-06-11", "session_id": "air_s", "cost_usd": 340.90,
         "input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_write": 0, "model": "m"},
    ])
    hook_daily = cctrack.aggregate_hook_data(local_events + remote_events)
    assert abs(hook_daily["2026-06-11"]["cost"] - 349.95) < 0.001
    assert hook_daily["2026-06-11"]["sessions"] == 2


def test_compute_hook_deltas_seeds_from_baseline():
    """A session whose earlier days were rolled up resumes from its baseline
    cumulative, not from zero."""
    events = [
        {"date_str": "2026-04-15", "session_id": "sA", "cost_usd": 12.0,
         "input_tokens": 800, "output_tokens": 400, "cache_read": 0, "cache_write": 0, "model": "m"},
    ]
    baselines = {"sA": {"date": "2026-04-14", "cost_usd": 5.0,
                        "input_tokens": 300, "output_tokens": 150}}
    deltas = cctrack.compute_hook_deltas(events, baselines)
    assert len(deltas) == 1
    assert abs(deltas[0]["cost_usd"] - 7.0) < 0.001
    assert deltas[0]["input_tokens"] == 500
    assert deltas[0]["output_tokens"] == 250


def _days_ago(n: int) -> str:
    from datetime import timedelta
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%d")


def _all_monthly_days(d: str) -> dict:
    """Merge the 'days' maps of every monthly-*.json in dir d."""
    days: dict = {}
    for p in Path(d).glob("monthly-*.json"):
        days.update(json.loads(p.read_text()).get("days", {}))
    return days


def test_rollup_boundary_session_not_double_counted():
    """End-to-end: a session straddling the retention cutoff. Rollup writes the
    old day's delta + a baseline; the remaining live day's delta then starts
    from the baseline, so the total equals the session's true cumulative."""
    with tempfile.TemporaryDirectory() as d:
        day1, day2 = _days_ago(6), _days_ago(3)
        # Day 1 (older than the 4-day retention, will be rolled): cumulative $5.
        write_hook_jsonl(d, [
            make_hook_entry(session_id="sA", ts=f"{day1}T10:00:00+00:00", cost_usd=5.00),
        ], date=day1)
        # Day 2 (within retention, stays live): cumulative $12.
        write_hook_jsonl(d, [
            make_hook_entry(session_id="sA", ts=f"{day2}T10:00:00+00:00", cost_usd=12.00),
        ], date=day2)

        cctrack.rollup_old_hook_files(cctrack_dir=Path(d), retention_days=4)

        days = _all_monthly_days(d)
        assert abs(days[day1]["cost"] - 5.00) < 0.001
        assert day2 not in days  # still live
        baselines = cctrack.load_session_baselines(cctrack_dir=Path(d))
        assert abs(baselines["sA"]["cost_usd"] - 5.00) < 0.001

        deltas = cctrack.compute_hook_deltas(
            cctrack.parse_hook_events(cctrack_dir=Path(d)), baselines)
        assert len(deltas) == 1
        # Delta is 12 - 5 = 7, NOT the full 12. Total counted = 5 + 7 = 12.
        assert abs(deltas[0]["cost_usd"] - 7.00) < 0.001


def test_rollup_seeds_from_prior_baseline():
    """A second rollup pass for the same session continues from the first
    pass's baseline instead of re-counting the earlier cumulative."""
    with tempfile.TemporaryDirectory() as d:
        day1, day2 = _days_ago(6), _days_ago(3)
        write_hook_jsonl(d, [
            make_hook_entry(session_id="sA", ts=f"{day1}T10:00:00+00:00", cost_usd=5.00),
        ], date=day1)
        cctrack.rollup_old_hook_files(cctrack_dir=Path(d), retention_days=4)

        write_hook_jsonl(d, [
            make_hook_entry(session_id="sA", ts=f"{day2}T10:00:00+00:00", cost_usd=12.00),
        ], date=day2)
        cctrack.rollup_old_hook_files(cctrack_dir=Path(d), retention_days=1)

        days = _all_monthly_days(d)
        assert abs(days[day1]["cost"] - 5.00) < 0.001
        assert abs(days[day2]["cost"] - 7.00) < 0.001
        total = sum(day["cost"] for day in days.values())
        assert abs(total - 12.00) < 0.001


# ── aggregate_hook_data tests ──────────────────────────────────────────

def test_aggregate_hook_data_single_day():
    """Multiple sessions on same day are summed."""
    events = [
        {"date_str": "2026-04-15", "cost_usd": 1.50, "session_id": "s1",
         "input_tokens": 100, "output_tokens": 50, "cache_read": 0, "cache_write": 0, "model": "claude-opus-4-6"},
        {"date_str": "2026-04-15", "cost_usd": 2.00, "session_id": "s2",
         "input_tokens": 200, "output_tokens": 100, "cache_read": 0, "cache_write": 0, "model": "claude-opus-4-6"},
    ]
    result = cctrack.aggregate_hook_data(events)
    assert "2026-04-15" in result
    assert abs(result["2026-04-15"]["cost"] - 3.50) < 0.001
    assert result["2026-04-15"]["sessions"] == 2


def test_aggregate_hook_data_multiple_days():
    """Events grouped by date correctly."""
    events = [
        {"date_str": "2026-04-14", "cost_usd": 1.00, "session_id": "s1",
         "input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_write": 0, "model": "m"},
        {"date_str": "2026-04-15", "cost_usd": 2.00, "session_id": "s2",
         "input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_write": 0, "model": "m"},
        {"date_str": "2026-04-15", "cost_usd": 3.00, "session_id": "s3",
         "input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_write": 0, "model": "m"},
    ]
    result = cctrack.aggregate_hook_data(events)
    assert len(result) == 2
    assert abs(result["2026-04-14"]["cost"] - 1.00) < 0.001
    assert result["2026-04-14"]["sessions"] == 1
    assert abs(result["2026-04-15"]["cost"] - 5.00) < 0.001
    assert result["2026-04-15"]["sessions"] == 2


def test_aggregate_hook_data_empty():
    """Empty events list returns empty dict."""
    result = cctrack.aggregate_hook_data([])
    assert result == {}


def test_aggregate_hook_data_with_monthly_summaries():
    """Monthly summaries are merged with recent events."""
    events = [
        {"date_str": "2026-04-15", "cost_usd": 3.00, "session_id": "s1",
         "input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_write": 0, "model": "m"},
    ]
    monthly = {
        "2026-01-10": {"cost": 10.00, "sessions": 3},
        "2026-01-11": {"cost": 8.00, "sessions": 2},
    }
    result = cctrack.aggregate_hook_data(events, monthly)
    assert len(result) == 3
    assert result["2026-01-10"]["cost"] == 10.00
    assert result["2026-04-15"]["cost"] == 3.00


def test_aggregate_hook_data_live_adds_into_monthly():
    """A date in a monthly summary (e.g. rolled up on host A) that also has
    live events (host B) sums both — the rolled cost must not be dropped."""
    events = [
        {"date_str": "2026-01-10", "cost_usd": 2.00, "session_id": "s1",
         "input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_write": 0, "model": "m"},
    ]
    monthly = {"2026-01-10": {"cost": 10.00, "sessions": 3}}
    result = cctrack.aggregate_hook_data(events, monthly)
    assert abs(result["2026-01-10"]["cost"] - 12.00) < 0.001
    assert result["2026-01-10"]["sessions"] == 4


def test_aggregate_hook_data_sessions_distinct_per_day():
    """Session ids are tracked as sets for distinct counting across days."""
    events = [
        {"date_str": "2026-01-10", "cost_usd": 1.0, "session_id": "sA",
         "input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_write": 0, "model": "m"},
        {"date_str": "2026-01-11", "cost_usd": 1.0, "session_id": "sA",
         "input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_write": 0, "model": "m"},
        {"date_str": "2026-01-11", "cost_usd": 1.0, "session_id": "sB",
         "input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_write": 0, "model": "m"},
    ]
    result = cctrack.aggregate_hook_data(events)
    assert result["2026-01-10"]["sessions"] == 1
    assert result["2026-01-11"]["sessions"] == 2
    # Distinct across both days is 2 (sA spans both) — union of the sets.
    assert len(result["2026-01-10"]["session_ids"] | result["2026-01-11"]["session_ids"]) == 2


# ── load_monthly_hook_summaries tests ─────────────────────────────────

def test_load_monthly_hook_summaries_basic():
    """Loads day-level data from monthly JSON files."""
    with tempfile.TemporaryDirectory() as d:
        monthly_data = {
            "month": "2026-01",
            "days": {
                "2026-01-10": {"cost": 10.0, "sessions": 3},
                "2026-01-11": {"cost": 8.0, "sessions": 2},
            }
        }
        Path(d, "monthly-2026-01.json").write_text(json.dumps(monthly_data))
        result = cctrack.load_monthly_hook_summaries(cctrack_dir=Path(d))
        assert len(result) == 2
        assert result["2026-01-10"]["cost"] == 10.0
        assert result["2026-01-11"]["sessions"] == 2


def test_load_monthly_hook_summaries_missing_dir():
    """Missing directory returns empty dict."""
    result = cctrack.load_monthly_hook_summaries(cctrack_dir=Path("/nonexistent"))
    assert result == {}


def test_load_monthly_hook_summaries_bad_json():
    """Malformed JSON files are skipped."""
    with tempfile.TemporaryDirectory() as d:
        Path(d, "monthly-2026-01.json").write_text("not json")
        result = cctrack.load_monthly_hook_summaries(cctrack_dir=Path(d))
        assert result == {}


# ── rollup_old_hook_files tests ───────────────────────────────────────

def test_rollup_old_files_creates_monthly():
    """Old daily files are aggregated into monthly JSON."""
    with tempfile.TemporaryDirectory() as d:
        write_hook_jsonl(d, [
            make_hook_entry(session_id="s1", ts="2025-12-01T10:00:00+00:00", cost_usd=5.00),
            make_hook_entry(session_id="s1", ts="2025-12-01T12:00:00+00:00", cost_usd=8.00),
            make_hook_entry(session_id="s2", ts="2025-12-01T10:00:00+00:00", cost_usd=3.00),
        ], date="2025-12-01")

        cctrack.rollup_old_hook_files(cctrack_dir=Path(d), retention_days=0)

        assert not Path(d, "statusline-2025-12-01.jsonl").exists()
        monthly_path = Path(d, "monthly-2025-12.json")
        assert monthly_path.exists()
        data = json.loads(monthly_path.read_text())
        assert data["month"] == "2025-12"
        # s1 deduped to latest (cost=8.00), s2=3.00, total=11.00
        assert abs(data["days"]["2025-12-01"]["cost"] - 11.00) < 0.001
        assert data["days"]["2025-12-01"]["sessions"] == 2


def test_rollup_skips_recent_files():
    """Files within retention period are not rolled up."""
    with tempfile.TemporaryDirectory() as d:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        write_hook_jsonl(d, [
            make_hook_entry(session_id="s1", cost_usd=5.00),
        ], date=today)

        cctrack.rollup_old_hook_files(cctrack_dir=Path(d), retention_days=90)

        assert Path(d, f"statusline-{today}.jsonl").exists()
        assert not list(Path(d).glob("monthly-*.json"))


def test_rollup_appends_to_existing_monthly():
    """Rollup merges into existing monthly JSON."""
    with tempfile.TemporaryDirectory() as d:
        existing = {"month": "2025-12", "days": {"2025-12-01": {"cost": 5.00, "sessions": 1}}}
        Path(d, "monthly-2025-12.json").write_text(json.dumps(existing))

        write_hook_jsonl(d, [
            make_hook_entry(session_id="s1", ts="2025-12-02T10:00:00+00:00", cost_usd=7.00),
        ], date="2025-12-02")

        cctrack.rollup_old_hook_files(cctrack_dir=Path(d), retention_days=0)

        data = json.loads(Path(d, "monthly-2025-12.json").read_text())
        assert "2025-12-01" in data["days"]
        assert "2025-12-02" in data["days"]
        assert data["days"]["2025-12-01"]["cost"] == 5.00
        assert data["days"]["2025-12-02"]["cost"] == 7.00


def test_rollup_cross_day_session_attributes_deltas():
    """A session active across multiple rolled-up days should have its
    cumulative cost attributed as per-day deltas, not double-counted."""
    with tempfile.TemporaryDirectory() as d:
        # Session sA: cumulative rises 0 → 5 on day 1, 5 → 12 on day 2.
        # Session sB: only active on day 2, cumulative rises 0 → 3.
        write_hook_jsonl(d, [
            make_hook_entry(session_id="sA", ts="2025-12-01T10:00:00+00:00", cost_usd=2.00),
            make_hook_entry(session_id="sA", ts="2025-12-01T18:00:00+00:00", cost_usd=5.00),
        ], date="2025-12-01")
        write_hook_jsonl(d, [
            make_hook_entry(session_id="sA", ts="2025-12-02T10:00:00+00:00", cost_usd=12.00),
            make_hook_entry(session_id="sB", ts="2025-12-02T11:00:00+00:00", cost_usd=3.00),
        ], date="2025-12-02")

        cctrack.rollup_old_hook_files(cctrack_dir=Path(d), retention_days=0)

        data = json.loads(Path(d, "monthly-2025-12.json").read_text())
        # Day 1: sA delta = 5.00. Total = 5.00, 1 session.
        assert abs(data["days"]["2025-12-01"]["cost"] - 5.00) < 0.001
        assert data["days"]["2025-12-01"]["sessions"] == 1
        # Day 2: sA delta = 12 - 5 = 7.00, sB delta = 3.00. Total = 10.00, 2 sessions.
        assert abs(data["days"]["2025-12-02"]["cost"] - 10.00) < 0.001
        assert data["days"]["2025-12-02"]["sessions"] == 2
        # Sum equals actual session totals: sA $12 + sB $3 = $15.
        total = sum(day["cost"] for day in data["days"].values())
        assert abs(total - 15.00) < 0.001


def test_rollup_deletes_empty_daily():
    """Daily file with no valid sessions is just deleted."""
    with tempfile.TemporaryDirectory() as d:
        Path(d, "statusline-2025-01-01.jsonl").write_text("not valid json\n")

        cctrack.rollup_old_hook_files(cctrack_dir=Path(d), retention_days=0)

        assert not Path(d, "statusline-2025-01-01.jsonl").exists()
        assert not list(Path(d).glob("monthly-*.json"))


# ── read_hook_config tests ─────────────────────────────────────────────

def test_read_hook_config_valid():
    """Valid config is parsed."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "config.json")
        with open(path, "w") as f:
            json.dump({"installed_at": "2026-04-10T00:00:00+00:00", "version": "0.3.0"}, f)
        with patch.object(cctrack, "HOOK_CONFIG_PATH", Path(path)):
            config = cctrack.read_hook_config()
        assert config is not None
        assert config["installed_at"] == "2026-04-10T00:00:00+00:00"
        assert config["version"] == "0.3.0"


def test_read_hook_config_missing():
    """Missing config returns None."""
    with patch.object(cctrack, "HOOK_CONFIG_PATH", Path("/nonexistent/config.json")):
        config = cctrack.read_hook_config()
    assert config is None


def test_read_hook_config_invalid_json():
    """Invalid JSON returns None."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "config.json")
        Path(path).write_text("not json")
        with patch.object(cctrack, "HOOK_CONFIG_PATH", Path(path)):
            config = cctrack.read_hook_config()
    assert config is None


# ── Accuracy factor tests ──────────────────────────────────────────────

def test_accuracy_factor_computed():
    """Accuracy factor is ratio of hook cost to JSONL cost for overlapping dates."""
    daily = {
        "2026-04-14": make_bucket(cost=4.00),  # pre-hook
        "2026-04-15": make_bucket(cost=5.00),  # overlapping
        "2026-04-16": make_bucket(cost=6.00),  # overlapping
    }
    hook_daily = {
        "2026-04-15": {"cost": 6.25, "sessions": 2},   # 1.25x
        "2026-04-16": {"cost": 7.50, "sessions": 1},    # 1.25x
    }
    factor, is_measured = cctrack._compute_accuracy_factor(daily, hook_daily, "2026-04-15")
    # hook total = 6.25 + 7.50 = 13.75; jsonl total = 5.00 + 6.00 = 11.00
    assert is_measured is True
    assert abs(factor - (13.75 / 11.00)) < 0.001


def test_accuracy_factor_default_when_no_overlap():
    """No overlapping dates falls back to default factor."""
    daily = {"2026-04-14": make_bucket(cost=4.00)}
    hook_daily = {"2026-04-15": {"cost": 6.00, "sessions": 1}}
    factor, is_measured = cctrack._compute_accuracy_factor(daily, hook_daily, "2026-04-15")
    assert is_measured is False
    assert factor == cctrack.DEFAULT_ACCURACY_FACTOR


def test_accuracy_factor_default_when_no_hook_install_date():
    """No hook install date falls back to default factor."""
    daily = {"2026-04-15": make_bucket(cost=5.00)}
    hook_daily = {"2026-04-15": {"cost": 6.00, "sessions": 1}}
    factor, is_measured = cctrack._compute_accuracy_factor(daily, hook_daily, None)
    assert is_measured is False
    assert factor == cctrack.DEFAULT_ACCURACY_FACTOR


def test_accuracy_factor_ignores_pre_hook_dates():
    """Only dates strictly after hook install are considered."""
    daily = {
        "2026-04-14": make_bucket(cost=10.00),
        "2026-04-16": make_bucket(cost=5.00),
    }
    hook_daily = {
        "2026-04-14": {"cost": 12.00, "sessions": 1},  # before hook install, ignored
        "2026-04-16": {"cost": 6.25, "sessions": 1},
    }
    factor, is_measured = cctrack._compute_accuracy_factor(daily, hook_daily, "2026-04-15")
    assert is_measured is True
    assert abs(factor - 1.25) < 0.001


def test_accuracy_factor_excludes_install_day():
    """The install day has partial hook coverage (only post-install sessions),
    so including it would bias the factor low. It must be excluded."""
    daily = {
        "2026-04-15": make_bucket(cost=10.00),  # install day: full-day JSONL
        "2026-04-16": make_bucket(cost=5.00),
    }
    hook_daily = {
        "2026-04-15": {"cost": 2.00, "sessions": 1},   # partial-day hook — would drag factor down
        "2026-04-16": {"cost": 6.25, "sessions": 1},
    }
    factor, is_measured = cctrack._compute_accuracy_factor(daily, hook_daily, "2026-04-15")
    assert is_measured is True
    assert abs(factor - 1.25) < 0.001  # 6.25/5.00 only — install day excluded


def test_accuracy_factor_per_source_ignores_hookless_host():
    """A hook-less machine's JSONL must not inflate the factor denominator."""
    hooked = {
        "daily": {"2026-04-16": make_bucket(cost=4.00)},
        "hook_daily": {"2026-04-16": {"cost": 5.00, "sessions": 1}},
        "install_date": "2026-04-15",
    }
    hookless = {
        "daily": {"2026-04-16": make_bucket(cost=100.00)},  # would swamp the ratio
        "hook_daily": {},
        "install_date": None,
    }
    factor, is_measured = cctrack._compute_accuracy_factor_sources([hooked, hookless])
    assert is_measured is True
    assert abs(factor - 1.25) < 0.001


def test_accuracy_factor_default_when_zero_jsonl_cost():
    """Zero JSONL cost on overlapping date falls back to default."""
    daily = {"2026-04-15": make_bucket(cost=0.0)}
    hook_daily = {"2026-04-15": {"cost": 5.00, "sessions": 1}}
    factor, is_measured = cctrack._compute_accuracy_factor(daily, hook_daily, "2026-04-15")
    assert is_measured is False
    assert factor == cctrack.DEFAULT_ACCURACY_FACTOR


# ── Report output with hook data only (all authoritative) ──────────────

def test_print_report_hook_only(capsys):
    """When all days have hook data, post-install days are authoritative,
    install day uses max(hook, adjusted JSONL)."""
    fake_now = datetime(2026, 4, 17, 12, 0, 0, tzinfo=timezone.utc)

    daily = {
        "2026-04-15": make_bucket(input=1000, output=500, cost=3.00),
        "2026-04-16": make_bucket(input=2000, output=1000, cost=5.00),
    }
    monthly = {"2026-04": make_bucket(input=3000, output=1500, cost=8.00)}
    hook_daily = {
        "2026-04-15": {"cost": 3.75, "sessions": 1},
        "2026-04-16": {"cost": 6.25, "sessions": 2},
    }

    with patch("cctrack.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.fromisoformat = datetime.fromisoformat
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        cctrack.print_report(daily, monthly, hook_daily=hook_daily,
                             hook_install_date="2026-04-15")

    out = capsys.readouterr().out
    assert "Claude Code Cost Report" in out
    # Install day (04-15): max(3.75, 3.00*1.25) = 3.75, estimated
    # Post-install (04-16): 6.25, authoritative
    # Total: 3.75 + 6.25 = 10.00
    assert "10.00" in out
    # Install day is estimated, so tilde appears
    assert "~" in out
    assert "1d authoritative" in out


def test_print_report_post_install_day_no_tilde(capsys):
    """Post-install days are authoritative with no ~ marker."""
    fake_now = datetime(2026, 4, 17, 12, 0, 0, tzinfo=timezone.utc)

    daily = {
        "2026-04-15": make_bucket(input=500, cost=2.00),
        "2026-04-16": make_bucket(input=1000, cost=4.00),
    }
    monthly = {"2026-04": make_bucket(input=1500, cost=6.00)}
    hook_daily = {
        "2026-04-15": {"cost": 2.50, "sessions": 1},
        "2026-04-16": {"cost": 5.00, "sessions": 1},
    }

    with patch("cctrack.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.fromisoformat = datetime.fromisoformat
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        cctrack.print_report(daily, monthly, hook_daily=hook_daily,
                             hook_install_date="2026-04-15")

    out = capsys.readouterr().out
    lines = out.split("\n")
    # Post-install day (04-16) should use hook cost, no tilde
    date_16_lines = [l for l in lines if "2026-04-16" in l]
    assert len(date_16_lines) >= 1
    assert "5.00" in date_16_lines[0]
    assert "~" not in date_16_lines[0]
    # Install day (04-15) gets tilde since it's partial
    date_15_lines = [l for l in lines if "2026-04-15" in l]
    assert len(date_15_lines) >= 1
    assert "~" in date_15_lines[0]


# ── Report output with mixed hook + JSONL data ────────────────────────

def test_print_report_mixed_data(capsys):
    """Some days authoritative, some adjusted. Install day is estimated."""
    fake_now = datetime(2026, 4, 17, 12, 0, 0, tzinfo=timezone.utc)

    daily = {
        "2026-04-14": make_bucket(input=1000, cost=3.00),   # pre-hook
        "2026-04-15": make_bucket(input=2000, cost=5.00),   # install day
        "2026-04-16": make_bucket(input=1500, cost=4.00),   # post-hook
    }
    monthly = {"2026-04": make_bucket(input=4500, cost=12.00)}
    hook_daily = {
        "2026-04-15": {"cost": 6.25, "sessions": 1},
        "2026-04-16": {"cost": 5.00, "sessions": 2},
    }

    with patch("cctrack.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.fromisoformat = datetime.fromisoformat
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        cctrack.print_report(daily, monthly, hook_daily=hook_daily,
                             hook_install_date="2026-04-15")

    out = capsys.readouterr().out
    # 04-14: pre-hook adjusted 3.00*1.25=3.75
    # 04-15: install day max(6.25, 5.00*1.25=6.25)=6.25, estimated
    # 04-16: post-hook 5.00, authoritative
    # Total: 3.75 + 6.25 + 5.00 = 15.00
    assert "1d authoritative" in out
    assert "2d adjusted" in out
    assert "15.00" in out
    assert "~" in out


def test_print_report_mixed_shows_accuracy_factor(capsys):
    """When post-install overlap exists, measured factor is shown in legend."""
    fake_now = datetime(2026, 4, 17, 12, 0, 0, tzinfo=timezone.utc)

    daily = {
        "2026-04-14": make_bucket(input=1000, cost=4.00),   # pre-hook, estimate
        "2026-04-16": make_bucket(input=2000, cost=5.00),   # post-install, overlap
    }
    monthly = {"2026-04": make_bucket(input=3000, cost=9.00)}
    hook_daily = {
        "2026-04-16": {"cost": 6.25, "sessions": 1},  # 1.25x factor
    }

    with patch("cctrack.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.fromisoformat = datetime.fromisoformat
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        cctrack.print_report(daily, monthly, hook_daily=hook_daily,
                             hook_install_date="2026-04-15")

    out = capsys.readouterr().out
    # Should mention measured undercount in legend
    assert "measured" in out
    assert "1.2x" in out
    # Total: hook(6.25) + adjusted(4.00*1.25=5.00) = 11.25
    assert "11.25" in out


# ── Report output with JSONL only (no hook data) ──────────────────────

def test_print_report_no_hook_unchanged(capsys):
    """Without hook data, report output is same as before."""
    fake_now = datetime(2026, 4, 17, 12, 0, 0, tzinfo=timezone.utc)

    daily = {
        "2026-04-15": make_bucket(input=1000, output=500, cost=3.00),
        "2026-04-16": make_bucket(input=2000, output=1000, cost=5.00),
    }
    monthly = {"2026-04": make_bucket(input=3000, output=1500, cost=8.00)}

    with patch("cctrack.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.fromisoformat = datetime.fromisoformat
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        cctrack.print_report(daily, monthly)

    out = capsys.readouterr().out
    assert "Claude Code Cost Report" in out
    assert "$8.00" in out
    # No hook-specific annotations
    assert "authoritative" not in out
    assert "~" not in out
    assert "undercount" not in out


def test_print_report_no_hook_empty_still_works(capsys):
    """Empty data with no hook data still shows 'no log data found'."""
    cctrack.print_report({}, {})
    out = capsys.readouterr().out
    assert "no log data found" in out


def test_print_report_no_hook_with_days_limit(capsys):
    """Days limit still works without hook data."""
    fake_now = datetime(2026, 4, 17, 12, 0, 0, tzinfo=timezone.utc)

    daily = {
        "2026-04-15": make_bucket(input=1000, cost=1.00),
        "2026-04-14": make_bucket(input=2000, cost=2.00),
        "2026-04-13": make_bucket(input=3000, cost=3.00),
    }
    monthly = {"2026-04": make_bucket(input=6000, cost=6.00)}

    with patch("cctrack.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.fromisoformat = datetime.fromisoformat
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        cctrack.print_report(daily, monthly, days=2)

    out = capsys.readouterr().out
    assert "2026-04-15" in out
    assert "2026-04-14" in out
    assert "2026-04-13" not in out


# ── Edge case: hook installed today ────────────────────────────────────

def test_print_report_hook_installed_today(capsys):
    """Hook installed today: install day is estimated (partial hook coverage)."""
    fake_now = datetime(2026, 4, 17, 12, 0, 0, tzinfo=timezone.utc)

    daily = {"2026-04-17": make_bucket(input=500, cost=2.00)}
    monthly = {"2026-04": make_bucket(input=500, cost=2.00)}
    hook_daily = {"2026-04-17": {"cost": 2.50, "sessions": 1}}

    with patch("cctrack.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.fromisoformat = datetime.fromisoformat
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        cctrack.print_report(daily, monthly, hook_daily=hook_daily,
                             hook_install_date="2026-04-17")

    out = capsys.readouterr().out
    # Install day: max(2.50, 2.00*1.25=2.50) = 2.50, marked estimated
    assert "2.50" in out
    assert "~" in out


# ── Hook data in previous months ───────────────────────────────────────

def test_print_report_previous_months_with_hook(capsys):
    """Previous month entries use hook data when available."""
    fake_now = datetime(2026, 4, 17, 12, 0, 0, tzinfo=timezone.utc)

    daily = {
        "2026-04-15": make_bucket(input=1000, cost=3.00),
        "2026-03-20": make_bucket(input=2000, cost=6.00),
        "2026-03-21": make_bucket(input=1500, cost=4.50),
    }
    monthly = {
        "2026-04": make_bucket(input=1000, cost=3.00),
        "2026-03": make_bucket(input=3500, cost=10.50),
    }
    hook_daily = {
        "2026-04-15": {"cost": 3.75, "sessions": 1},
    }

    with patch("cctrack.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.fromisoformat = datetime.fromisoformat
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        cctrack.print_report(daily, monthly, hook_daily=hook_daily,
                             hook_install_date="2026-04-10")

    out = capsys.readouterr().out
    assert "Previous months:" in out
    assert "2026-03" in out


# ── Hook-only dates (no JSONL for that date) ──────────────────────────

def test_print_report_hook_date_without_jsonl(capsys):
    """A date with hook data but no JSONL data still appears."""
    fake_now = datetime(2026, 4, 17, 12, 0, 0, tzinfo=timezone.utc)

    daily = {"2026-04-15": make_bucket(input=1000, cost=3.00)}
    monthly = {"2026-04": make_bucket(input=1000, cost=3.00)}
    hook_daily = {
        "2026-04-15": {"cost": 3.75, "sessions": 1},
        "2026-04-16": {"cost": 5.00, "sessions": 2},  # no JSONL for this date
    }

    with patch("cctrack.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.fromisoformat = datetime.fromisoformat
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        cctrack.print_report(daily, monthly, hook_daily=hook_daily,
                             hook_install_date="2026-04-15")

    out = capsys.readouterr().out
    assert "2026-04-16" in out
    assert "5.00" in out
    # Total should include both: 3.75 + 5.00 = 8.75
    assert "8.75" in out


# ── Unaccounted cost estimation ────────────────────────────────────────

def test_adjusted_cost_with_measured_factor(capsys):
    """Pre-hook costs are adjusted by measured accuracy factor in totals and daily rows."""
    fake_now = datetime(2026, 4, 17, 12, 0, 0, tzinfo=timezone.utc)

    daily = {
        "2026-04-13": make_bucket(input=1000, cost=4.00),   # pre-hook
        "2026-04-14": make_bucket(input=1000, cost=4.00),   # pre-hook
        "2026-04-15": make_bucket(input=2000, cost=8.00),   # post-hook
        "2026-04-16": make_bucket(input=1500, cost=6.00),   # post-hook
    }
    monthly = {"2026-04": make_bucket(input=5500, cost=22.00)}
    hook_daily = {
        "2026-04-15": {"cost": 10.00, "sessions": 1},  # 1.25x
        "2026-04-16": {"cost": 7.50, "sessions": 2},   # 1.25x
    }

    with patch("cctrack.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.fromisoformat = datetime.fromisoformat
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        cctrack.print_report(daily, monthly, hook_daily=hook_daily,
                             hook_install_date="2026-04-15")

    out = capsys.readouterr().out
    # Factor = (10+7.5)/(8+6) = 17.5/14 = 1.25x
    assert "1.25x" in out
    # MTD: hook(10+7.5) + adjusted(4*1.25 + 4*1.25) = 17.5 + 10.0 = 27.50
    assert "27.50" in out
    # Tilde on pre-hook dates
    assert "~" in out


def test_adjusted_cost_without_overlap_uses_default(capsys):
    """Without overlap, uses default 1.25x factor for adjustment."""
    fake_now = datetime(2026, 4, 17, 12, 0, 0, tzinfo=timezone.utc)

    daily = {
        "2026-04-13": make_bucket(input=1000, cost=4.00),   # pre-hook only
        "2026-04-14": make_bucket(input=1000, cost=4.00),   # pre-hook only
    }
    monthly = {"2026-04": make_bucket(input=2000, cost=8.00)}
    hook_daily = {
        "2026-04-16": {"cost": 5.00, "sessions": 1},
    }

    with patch("cctrack.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.fromisoformat = datetime.fromisoformat
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        cctrack.print_report(daily, monthly, hook_daily=hook_daily,
                             hook_install_date="2026-04-15")

    out = capsys.readouterr().out
    # MTD: adjusted(4*1.25 + 4*1.25) + hook(5) = 10 + 5 = 15.00
    assert "15.00" in out
    # Tilde on estimated days
    assert "~" in out
    # Legend mentions 20-25%
    assert "20-25%" in out


# ── Previous month accuracy estimation ─────────────────────────────────

def test_previous_month_estimated_actual(capsys):
    """Previous months with only JSONL data show estimated actual when factor available."""
    fake_now = datetime(2026, 4, 17, 12, 0, 0, tzinfo=timezone.utc)

    daily = {
        "2026-04-15": make_bucket(input=1000, cost=4.00),
        "2026-03-20": make_bucket(input=2000, cost=8.00),
        "2026-03-21": make_bucket(input=1500, cost=6.00),
    }
    monthly = {
        "2026-04": make_bucket(input=1000, cost=4.00),
        "2026-03": make_bucket(input=3500, cost=14.00),
    }
    hook_daily = {
        "2026-04-15": {"cost": 5.00, "sessions": 1},  # 1.25x factor
    }

    with patch("cctrack.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.fromisoformat = datetime.fromisoformat
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        cctrack.print_report(daily, monthly, hook_daily=hook_daily,
                             hook_install_date="2026-04-15")

    out = capsys.readouterr().out
    # Previous month cost is JSONL-only: (8+6)*1.25 = 17.50 (adjusted per day)
    assert "Previous months:" in out
    assert "17.50" in out
    assert "~" in out


# ── Per-source cost composition ────────────────────────────────────────

def _merged_daily(*dailies):
    merged: dict = {}
    for d in dailies:
        for k, b in d.items():
            t = merged.setdefault(k, dict(make_bucket()))
            for f in ("input", "output", "cache_read", "cache_write", "cost"):
                t[f] = t.get(f, 0) + b.get(f, 0)
    return merged


def test_per_source_hookless_host_not_dropped(capsys):
    """Regression: on a date where host A has hook data, host B's JSONL-only
    cost must still be counted (adjusted), not silently discarded."""
    fake_now = datetime(2026, 6, 13, 12, 0, 0, tzinfo=timezone.utc)

    hooked = {
        "name": "pro",
        "daily": {"2026-06-11": make_bucket(input=1000, cost=8.00),
                  "2026-06-12": make_bucket(input=1000, cost=4.00)},
        "hook_daily": {"2026-06-11": {"cost": 10.00, "sessions": 1},   # 1.25x overlap
                       "2026-06-12": {"cost": 5.00, "sessions": 1}},
        "install_date": "2026-06-10",
    }
    hookless = {
        "name": "dgx",
        "daily": {"2026-06-12": make_bucket(input=500, cost=100.00)},
        "hook_daily": {},
        "install_date": None,
    }
    daily = _merged_daily(hooked["daily"], hookless["daily"])
    monthly = {"2026-06": make_bucket(input=2500, cost=112.00)}

    with patch("cctrack.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.fromisoformat = datetime.fromisoformat
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        cctrack.print_report(daily, monthly, sources=[hooked, hookless])

    out = capsys.readouterr().out
    # 06-12: pro hook $5.00 (auth) + dgx JSONL $100 * 1.25 = $125 (est) = $130
    assert "130.00" in out
    # The day has an estimated component, so it carries the ~ marker.
    lines = [l for l in out.splitlines() if "2026-06-12" in l]
    assert lines and "~" in lines[0]
    # 06-11 is fully hook-covered: authoritative, no marker.
    lines = [l for l in out.splitlines() if "2026-06-11" in l]
    assert lines and "~" not in lines[0] and "10.00" in lines[0]


def test_per_source_two_hooked_hosts_sum(capsys):
    """Costs for the same date on two hook-covered machines add together."""
    fake_now = datetime(2026, 6, 13, 12, 0, 0, tzinfo=timezone.utc)

    src_a = {
        "name": "pro",
        "daily": {"2026-06-11": make_bucket(input=100, cost=7.00)},
        "hook_daily": {"2026-06-11": {"cost": 9.05, "sessions": 1}},
        "install_date": "2026-06-01",
    }
    src_b = {
        "name": "air",
        "daily": {"2026-06-11": make_bucket(input=200, cost=280.00)},
        "hook_daily": {"2026-06-11": {"cost": 340.90, "sessions": 7}},
        "install_date": "2026-06-01",
    }
    daily = _merged_daily(src_a["daily"], src_b["daily"])
    monthly = {"2026-06": make_bucket(input=300, cost=287.00)}

    with patch("cctrack.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.fromisoformat = datetime.fromisoformat
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        cctrack.print_report(daily, monthly, sources=[src_a, src_b])

    out = capsys.readouterr().out
    # 9.05 + 340.90 = 349.95, authoritative
    assert "349.95" in out
    lines = [l for l in out.splitlines() if "2026-06-11" in l]
    assert lines and "~" not in lines[0]
    # Sessions column sums across hosts: 8
    assert lines[0].split()[3] == "8" or " 8 " in lines[0]


def test_unknown_model_marked(capsys):
    """A model priced by fallback gets a '?' marker and a legend line."""
    fake_now = datetime(2026, 6, 13, 12, 0, 0, tzinfo=timezone.utc)

    bucket = make_bucket(input=1000, cost=3.00)
    bucket["models"] = {"claude-novel-7": 5}
    daily = {"2026-06-12": bucket}
    mbucket = make_bucket(input=1000, cost=3.00)
    mbucket["models"] = {"claude-novel-7": 5}
    monthly = {"2026-06": mbucket}

    with patch("cctrack.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.fromisoformat = datetime.fromisoformat
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        cctrack.print_report(daily, monthly)

    out = capsys.readouterr().out
    assert "novel-7?" in out
    assert "unrecognized model" in out


def test_known_model_not_marked(capsys):
    fake_now = datetime(2026, 6, 13, 12, 0, 0, tzinfo=timezone.utc)
    bucket = make_bucket(input=1000, cost=3.00)
    bucket["models"] = {"claude-opus-4-8": 5}
    daily = {"2026-06-12": bucket}
    monthly = {"2026-06": dict(bucket)}

    with patch("cctrack.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.fromisoformat = datetime.fromisoformat
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        cctrack.print_report(daily, monthly)

    out = capsys.readouterr().out
    assert "opus-4-8" in out
    assert "opus-4-8?" not in out
    assert "unrecognized" not in out


def test_mtd_sessions_distinct_across_days(capsys):
    """A session spanning multiple days counts once in the MTD Sessions line."""
    fake_now = datetime(2026, 6, 13, 12, 0, 0, tzinfo=timezone.utc)

    deltas = [
        {"date_str": "2026-06-11", "session_id": "sA", "cost_usd": 5.0,
         "input_tokens": 10, "output_tokens": 5, "cache_read": 0, "cache_write": 0, "model": "m"},
        {"date_str": "2026-06-12", "session_id": "sA", "cost_usd": 3.0,
         "input_tokens": 10, "output_tokens": 5, "cache_read": 0, "cache_write": 0, "model": "m"},
        {"date_str": "2026-06-12", "session_id": "sB", "cost_usd": 2.0,
         "input_tokens": 10, "output_tokens": 5, "cache_read": 0, "cache_write": 0, "model": "m"},
    ]
    hook_daily = cctrack.aggregate_hook_data(deltas)
    daily = {
        "2026-06-11": make_bucket(input=100, cost=4.00),
        "2026-06-12": make_bucket(input=100, cost=4.00),
    }
    monthly = {"2026-06": make_bucket(input=200, cost=8.00)}

    with patch("cctrack.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.fromisoformat = datetime.fromisoformat
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        cctrack.print_report(daily, monthly, hook_daily=hook_daily,
                             hook_install_date="2026-06-10")

    out = capsys.readouterr().out
    # sA spans two days but is one session; sB is a second → 2, not 3.
    session_lines = [l for l in out.splitlines() if "Sessions:" in l]
    assert session_lines and session_lines[0].split()[-1] == "2"


# ── discover_hook_data_dirs tests ─────────────────────────────────────

def test_discover_hook_data_dirs_host_only():
    """Finds host cctrack dir when no Sandy sandboxes exist."""
    with tempfile.TemporaryDirectory() as home:
        cctrack_dir = Path(home) / ".claude" / "cctrack"
        cctrack_dir.mkdir(parents=True)
        with patch.object(cctrack, "CCTRACK_DIR", cctrack_dir), \
             patch("cctrack.Path") as mock_path:
            mock_path.home.return_value = Path(home)
            sandy = Path(home) / ".sandy" / "sandboxes"
            # Sandy doesn't exist
            dirs = cctrack.discover_hook_data_dirs()
            assert cctrack_dir in dirs
            assert len(dirs) == 1


def test_discover_hook_data_dirs_with_sandy():
    """Finds both host and Sandy sandbox cctrack dirs."""
    with tempfile.TemporaryDirectory() as home:
        cctrack_dir = Path(home) / ".claude" / "cctrack"
        cctrack_dir.mkdir(parents=True)
        sandy_base = Path(home) / ".sandy" / "sandboxes"
        sb1 = sandy_base / "sandbox-abc" / "claude" / "cctrack"
        sb2 = sandy_base / "sandbox-def" / "claude" / "cctrack"
        sb1.mkdir(parents=True)
        sb2.mkdir(parents=True)
        with patch.object(cctrack, "CCTRACK_DIR", cctrack_dir), \
             patch("pathlib.Path.home", return_value=Path(home)):
            dirs = cctrack.discover_hook_data_dirs()
            assert cctrack_dir in dirs
            assert sb1 in dirs
            assert sb2 in dirs
            assert len(dirs) == 3


def test_discover_hook_data_dirs_sandy_only():
    """Finds Sandy sandbox dirs even when host dir doesn't exist."""
    with tempfile.TemporaryDirectory() as home:
        cctrack_dir = Path(home) / ".claude" / "cctrack"
        # Don't create host dir
        sandy_base = Path(home) / ".sandy" / "sandboxes"
        sb1 = sandy_base / "sandbox-abc" / "claude" / "cctrack"
        sb1.mkdir(parents=True)
        with patch.object(cctrack, "CCTRACK_DIR", cctrack_dir), \
             patch("pathlib.Path.home", return_value=Path(home)):
            dirs = cctrack.discover_hook_data_dirs()
            assert cctrack_dir not in dirs
            assert sb1 in dirs
            assert len(dirs) == 1


def test_main_aggregates_sandy_hook_data():
    """End-to-end: hook data from Sandy sandbox is included in report."""
    with tempfile.TemporaryDirectory() as home:
        cctrack_dir = Path(home) / ".claude" / "cctrack"
        cctrack_dir.mkdir(parents=True)
        # Host hook data: $5.00
        write_hook_jsonl(str(cctrack_dir), [
            make_hook_entry(session_id="host_sess", cost_usd=5.0,
                            ts="2026-04-17T10:00:00+00:00"),
        ], date="2026-04-17")
        # Sandy sandbox hook data: $95.30
        sb_dir = Path(home) / ".sandy" / "sandboxes" / "sb1" / "claude" / "cctrack"
        sb_dir.mkdir(parents=True)
        write_hook_jsonl(str(sb_dir), [
            make_hook_entry(session_id="sandy_sess", cost_usd=95.30,
                            ts="2026-04-17T12:00:00+00:00"),
        ], date="2026-04-17")
        # Config with install date before today
        config = {"installed_at": "2026-04-16T00:00:00+00:00", "version": "0.3.0"}
        (cctrack_dir / "config.json").write_text(json.dumps(config))

        hook_dirs = [cctrack_dir, sb_dir]
        all_hook_events: list[dict] = []
        for hdir in hook_dirs:
            all_hook_events.extend(cctrack.parse_hook_events(hdir))
        hook_daily = cctrack.aggregate_hook_data(all_hook_events)
        # Both sessions counted: $5.00 + $95.30 = $100.30
        assert abs(hook_daily["2026-04-17"]["cost"] - 100.30) < 0.01
        assert hook_daily["2026-04-17"]["sessions"] == 2
