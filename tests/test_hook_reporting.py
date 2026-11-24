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


# ── parse_hook_rows tests ──────────────────────────────────────────────

def test_parse_hook_rows_basic():
    """Parse a single hook row. Only cost/session/model/date are carried —
    the hook's token fields are context snapshots and are deliberately dropped."""
    with tempfile.TemporaryDirectory() as d:
        write_hook_jsonl(d, [
            make_hook_entry(session_id="sess_001", cost_usd=1.50),
        ])
        rows = cctrack.parse_hook_rows(cctrack_dir=Path(d))
        assert len(rows) == 1
        assert rows[0]["session_id"] == "sess_001"
        assert rows[0]["cost_usd"] == 1.50
        assert rows[0]["date_str"] == "2026-04-15"
        assert rows[0]["model"] == "claude-opus-4-6"
        assert "input_tokens" not in rows[0]
        assert "output_tokens" not in rows[0]


def test_parse_hook_rows_keeps_every_render():
    """Every statusline render is kept, sorted by timestamp — the delta walk
    needs them all to survive mid-day counter resets."""
    with tempfile.TemporaryDirectory() as d:
        write_hook_jsonl(d, [
            make_hook_entry(session_id="sess_A", ts="2026-04-15T10:00:00+00:00", cost_usd=0.50),
            make_hook_entry(session_id="sess_A", ts="2026-04-15T12:00:00+00:00", cost_usd=1.50),
            make_hook_entry(session_id="sess_A", ts="2026-04-15T11:00:00+00:00", cost_usd=1.00),
            make_hook_entry(session_id="sess_B", ts="2026-04-15T10:00:00+00:00", cost_usd=2.00),
        ])
        rows = cctrack.parse_hook_rows(cctrack_dir=Path(d))
        assert len(rows) == 4
        a = [r["cost_usd"] for r in rows if r["session_id"] == "sess_A"]
        assert a == [0.50, 1.00, 1.50]  # timestamp order, not file order


def test_parse_hook_rows_empty_file():
    with tempfile.TemporaryDirectory() as d:
        Path(d, "statusline-2026-04-15.jsonl").write_text("")
        assert cctrack.parse_hook_rows(cctrack_dir=Path(d)) == []


def test_parse_hook_rows_missing_dir():
    assert cctrack.parse_hook_rows(cctrack_dir=Path("/nonexistent/dir")) == []


def test_parse_hook_rows_malformed_lines():
    """Malformed lines are skipped."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "statusline-2026-04-15.jsonl")
        with open(path, "w") as f:
            f.write("not json\n")
            f.write('{"broken\n')
            f.write(json.dumps(make_hook_entry(cost_usd=3.33)) + "\n")
        rows = cctrack.parse_hook_rows(cctrack_dir=Path(d))
        assert len(rows) == 1
        assert rows[0]["cost_usd"] == 3.33


def test_parse_hook_rows_no_session_id_skipped():
    with tempfile.TemporaryDirectory() as d:
        entry = make_hook_entry()
        del entry["session_id"]
        write_hook_jsonl(d, [entry])
        assert cctrack.parse_hook_rows(cctrack_dir=Path(d)) == []


def test_parse_hook_rows_no_ts_skipped():
    with tempfile.TemporaryDirectory() as d:
        entry = make_hook_entry()
        del entry["_ts"]
        write_hook_jsonl(d, [entry])
        assert cctrack.parse_hook_rows(cctrack_dir=Path(d)) == []


def test_parse_hook_rows_multiple_dates():
    with tempfile.TemporaryDirectory() as d:
        for i, day in enumerate(("2026-04-14", "2026-04-15", "2026-04-16"), start=1):
            write_hook_jsonl(d, [
                make_hook_entry(session_id=f"s{i}", ts=f"{day}T10:00:00+00:00", cost_usd=float(i)),
            ], date=day)
        rows = cctrack.parse_hook_rows(cctrack_dir=Path(d))
        assert len(rows) == 3
        assert {r["date_str"] for r in rows} == {"2026-04-14", "2026-04-15", "2026-04-16"}


def test_parse_hook_rows_reads_legacy_file():
    """Legacy statusline.jsonl is also read."""
    with tempfile.TemporaryDirectory() as d:
        legacy_path = os.path.join(d, "statusline.jsonl")
        with open(legacy_path, "w") as f:
            f.write(json.dumps(make_hook_entry(session_id="legacy_1", cost_usd=5.00)) + "\n")
        rows = cctrack.parse_hook_rows(cctrack_dir=Path(d))
        assert len(rows) == 1
        assert rows[0]["session_id"] == "legacy_1"


def test_hook_token_fields_are_not_cumulative():
    """Documents WHY hook tokens are unusable: `input_tokens` is the context
    window size (≈ cache_read + cache_write), not a running total."""
    entry = make_hook_entry(input_tokens=440341, cache_read=428341, cache_write=11999)
    assert entry["input_tokens"] - (entry["cache_read"] + entry["cache_write"]) == 1
    with tempfile.TemporaryDirectory() as d:
        write_hook_jsonl(d, [entry])
        rows = cctrack.parse_hook_rows(cctrack_dir=Path(d))
        # None of those snapshot fields survive into the parsed row.
        assert set(rows[0]) == {"session_id", "ts", "date_str", "cost_usd", "model"}


# ── compute_hook_deltas tests ──────────────────────────────────────────
#
# The hook's cost_usd is a session-lifetime cumulative. A real Claude Code
# session first renders its statusline at ~$0 (verified in live data), so its
# whole cumulative is watched accumulating and is counted. compute_hook_deltas
# books only the INCREMENTS it observes — never the standing level of the
# counter. These fixtures therefore start each session at $0, the way real
# sessions do.

def make_row(session_id="sA", ts="2026-04-15T10:00:00+00:00", cost_usd=1.0, model="m"):
    return {"session_id": session_id, "ts": ts, "date_str": ts[:10],
            "cost_usd": cost_usd, "model": model}


def test_compute_hook_deltas_single_day_session():
    """A fresh session (first render ~$0) books its full cumulative."""
    rows = [
        make_row(session_id="s1", ts="2026-04-15T09:00:00+00:00", cost_usd=0.0),
        make_row(session_id="s1", ts="2026-04-15T10:00:00+00:00", cost_usd=3.0),
    ]
    deltas = cctrack.compute_hook_deltas(rows)
    assert len(deltas) == 1
    assert abs(deltas[0]["cost_usd"] - 3.0) < 0.001
    assert deltas[0]["date_str"] == "2026-04-15"


def test_compute_hook_deltas_session_spans_days():
    """A session spanning days books each day's own increment."""
    rows = [
        make_row(ts="2026-04-14T09:00:00+00:00", cost_usd=0.00),   # session start
        make_row(ts="2026-04-14T10:00:00+00:00", cost_usd=2.00),
        make_row(ts="2026-04-14T18:00:00+00:00", cost_usd=5.00),
        make_row(ts="2026-04-15T09:00:00+00:00", cost_usd=8.00),
        make_row(ts="2026-04-15T16:00:00+00:00", cost_usd=12.00),
    ]
    by_date = {d["date_str"]: d for d in cctrack.compute_hook_deltas(rows)}
    assert abs(by_date["2026-04-14"]["cost_usd"] - 5.00) < 0.001   # 0 → 5
    assert abs(by_date["2026-04-15"]["cost_usd"] - 7.00) < 0.001   # 5 → 12


def test_compute_hook_deltas_first_seen_cumulative_not_booked():
    """THE $3,370 BUG: a long-running session first observed already deep into
    its life ($1,304 on its first render) must NOT have that pre-existing
    cumulative booked onto the first-seen day. Only the observed climb counts."""
    rows = [
        make_row(session_id="runaway", ts="2026-08-03T09:00:00+00:00", cost_usd=1304.74),
        make_row(session_id="runaway", ts="2026-08-03T18:00:00+00:00", cost_usd=1347.81),
    ]
    deltas = cctrack.compute_hook_deltas(rows)
    assert len(deltas) == 1
    # $43.07 observed on 08-03 — not the whole $1,347 cumulative.
    assert abs(deltas[0]["cost_usd"] - 43.07) < 0.001


def test_compute_hook_deltas_never_exceeds_counter_peak():
    """THE $2,083-vs-$1,086 BUG: a mid-session drop must not be re-added as
    fresh spend: the drop rebaselines and only the climb after it counts."""
    rows = [
        make_row(session_id="s", ts="2026-08-03T09:00:00+00:00", cost_usd=0.0),
        make_row(session_id="s", ts="2026-08-03T10:00:00+00:00", cost_usd=1076.4),
        make_row(session_id="s", ts="2026-08-03T11:00:00+00:00", cost_usd=1086.97),  # peak
        make_row(session_id="s", ts="2026-08-03T12:00:00+00:00", cost_usd=971.12),   # drop
        make_row(session_id="s", ts="2026-08-03T13:00:00+00:00", cost_usd=996.15),   # climb
    ]
    deltas = cctrack.compute_hook_deltas(rows)
    booked = deltas[0]["cost_usd"]
    # Increments: 0→1086.97 (=1086.97) + 971.12→996.15 (=25.03) = 1112.00. The
    # climb after the drop counts: resumed sessions restart lower and keep working
    # (0.12.4 dropped it and fell to 85% of transcript cost on real data).
    assert abs(booked - 1112.00) < 0.01


def test_compute_hook_deltas_drops_zero_records():
    """A day whose cumulative didn't advance contributes nothing."""
    rows = [
        make_row(ts="2026-04-14T09:00:00+00:00", cost_usd=0.0),
        make_row(ts="2026-04-14T10:00:00+00:00", cost_usd=5.0),
        make_row(ts="2026-04-15T10:00:00+00:00", cost_usd=5.0),   # no advance
    ]
    deltas = cctrack.compute_hook_deltas(rows)
    assert len(deltas) == 1
    assert deltas[0]["date_str"] == "2026-04-14"


def test_compute_hook_deltas_resume_increments_are_counted():
    """A counter that resets to ~0 on resume and climbs again books the
    post-resume increments (they are genuinely new spend)."""
    rows = [
        make_row(ts="2026-04-14T09:00:00+00:00", cost_usd=0.0),
        make_row(ts="2026-04-14T10:00:00+00:00", cost_usd=67.55),   # day 1: $67.55
        make_row(ts="2026-04-15T10:00:00+00:00", cost_usd=4.85),    # resume, counter reset
        make_row(ts="2026-04-15T12:00:00+00:00", cost_usd=6.00),
    ]
    by_date = {d["date_str"]: d["cost_usd"] for d in cctrack.compute_hook_deltas(rows)}
    assert abs(by_date["2026-04-14"] - 67.55) < 0.001
    # Day 2 books the resumed climb 4.85 → 6.00 = $1.15, NOT the $4.85 reset level.
    assert abs(by_date["2026-04-15"] - 1.15) < 0.001


def test_compute_hook_deltas_trailing_zero_render_ignored():
    """A trailing 0 render (stale/reset) rebaselines but doesn't erase the day."""
    rows = [
        make_row(ts="2026-04-15T08:00:00+00:00", cost_usd=0.0),
        make_row(ts="2026-04-15T09:00:00+00:00", cost_usd=8.09),
        make_row(ts="2026-04-15T20:00:00+00:00", cost_usd=67.55),
        make_row(ts="2026-04-15T22:59:00+00:00", cost_usd=0.0),   # stale/reset render
    ]
    deltas = cctrack.compute_hook_deltas(rows)
    assert len(deltas) == 1
    assert abs(deltas[0]["cost_usd"] - 67.55) < 0.001


def test_compute_hook_deltas_float_jitter_is_not_a_reset():
    """A negligible decrease is treated as no-change, not a rebaseline."""
    rows = [
        make_row(ts="2026-04-15T09:00:00+00:00", cost_usd=0.0),
        make_row(ts="2026-04-15T10:00:00+00:00", cost_usd=10.0),
        make_row(ts="2026-04-15T11:00:00+00:00", cost_usd=10.0 - 1e-12),
    ]
    deltas = cctrack.compute_hook_deltas(rows)
    assert len(deltas) == 1
    assert abs(deltas[0]["cost_usd"] - 10.0) < 0.001


def test_compute_hook_deltas_rows_without_ts_sort_by_date():
    """Callers may pass one pre-aggregated reading per day (no `ts`). The first
    day seen sets the baseline (its level is unattributable prior history)."""
    rows = [
        {"session_id": "sA", "date_str": "2026-04-15", "cost_usd": 12.0, "model": "m"},
        {"session_id": "sA", "date_str": "2026-04-14", "cost_usd": 5.0, "model": "m"},
    ]
    by_date = {d["date_str"]: d["cost_usd"] for d in cctrack.compute_hook_deltas(rows)}
    # 04-14 is the first observation → baseline, not booked; 04-15 books 5 → 12.
    assert "2026-04-14" not in by_date
    assert abs(by_date["2026-04-15"] - 7.0) < 0.001


def test_compute_hook_deltas_multiple_sessions_independent():
    """Each session's deltas are computed independently."""
    rows = [
        make_row(session_id="sA", ts="2026-04-14T08:00:00+00:00", cost_usd=0.0),
        make_row(session_id="sA", ts="2026-04-14T10:00:00+00:00", cost_usd=10.0),
        make_row(session_id="sA", ts="2026-04-15T10:00:00+00:00", cost_usd=15.0),
        make_row(session_id="sB", ts="2026-04-15T08:00:00+00:00", cost_usd=0.0),
        make_row(session_id="sB", ts="2026-04-15T10:00:00+00:00", cost_usd=4.0),
    ]
    deltas = cctrack.compute_hook_deltas(rows)
    by_key = {(d["date_str"], d["session_id"]): d["cost_usd"] for d in deltas}
    assert abs(by_key[("2026-04-14", "sA")] - 10.0) < 0.001
    assert abs(by_key[("2026-04-15", "sA")] - 5.0) < 0.001
    assert abs(by_key[("2026-04-15", "sB")] - 4.0) < 0.001


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


def test_parse_remote_hook_data_rows_across_files_resolve_to_one_day():
    """A session's renders may be split across remote files; all rows are kept
    and the delta walk resolves them to that day's true spend."""
    raw = _remote_raw(
        hook_chunks=[
            [make_hook_entry(session_id="rA", ts="2026-04-15T08:00:00+00:00", cost_usd=0.0),
             make_hook_entry(session_id="rA", ts="2026-04-15T10:00:00+00:00", cost_usd=2.00)],
            [make_hook_entry(session_id="rA", ts="2026-04-15T18:00:00+00:00", cost_usd=6.00)],
        ],
        monthly_chunks=[],
    )
    with patch("cctrack.fetch_remote_hook_data", return_value=raw):
        rows, monthly, baselines = cctrack.parse_remote_hook_data("fakehost")
    assert len(rows) == 3
    deltas = cctrack.compute_hook_deltas(rows)
    assert len(deltas) == 1
    assert abs(deltas[0]["cost_usd"] - 6.00) < 0.001


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
        {"date_str": "2026-06-11", "session_id": "local_s", "cost_usd": 0.0, "model": "m"},
        {"date_str": "2026-06-11", "session_id": "local_s", "cost_usd": 9.05, "model": "m"},
    ])
    remote_events = cctrack.compute_hook_deltas([
        {"date_str": "2026-06-11", "session_id": "air_s", "cost_usd": 0.0, "model": "m"},
        {"date_str": "2026-06-11", "session_id": "air_s", "cost_usd": 340.90, "model": "m"},
    ])
    hook_daily = cctrack.aggregate_hook_data(local_events + remote_events)
    assert abs(hook_daily["2026-06-11"]["cost"] - 349.95) < 0.001
    assert hook_daily["2026-06-11"]["sessions"] == 2


def test_compute_hook_deltas_seeds_from_baseline():
    """A session whose earlier days were rolled up resumes from its baseline
    cumulative, not from zero."""
    rows = [make_row(session_id="sA", ts="2026-04-15T10:00:00+00:00", cost_usd=12.0)]
    baselines = {"sA": {"date": "2026-04-14", "cost_usd": 5.0}}
    deltas = cctrack.compute_hook_deltas(rows, baselines)
    assert len(deltas) == 1
    assert abs(deltas[0]["cost_usd"] - 7.0) < 0.001


def test_compute_hook_deltas_reset_below_baseline_is_counted():
    """A resumed session can restart its counter BELOW the rolled-up baseline
    (real case: baseline $12.84, session reappears months later starting at $0
    and climbing to $7.94). The drop rebaselines, then the fresh climb is
    counted — so the reappearance books its own spend, not zero, and not the
    stale $12.84 baseline."""
    rows = [
        make_row(session_id="sA", ts="2026-06-12T08:00:00+00:00", cost_usd=0.0),
        make_row(session_id="sA", ts="2026-06-12T10:00:00+00:00", cost_usd=7.94),
    ]
    baselines = {"sA": {"date": "2026-04-20", "cost_usd": 12.84}}
    deltas = cctrack.compute_hook_deltas(rows, baselines)
    assert len(deltas) == 1
    assert abs(deltas[0]["cost_usd"] - 7.94) < 0.001


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
        # Day 1 (older than the 4-day retention, will be rolled): 0 → $5.
        write_hook_jsonl(d, [
            make_hook_entry(session_id="sA", ts=f"{day1}T08:00:00+00:00", cost_usd=0.0),
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
            cctrack.parse_hook_rows(cctrack_dir=Path(d)), baselines)
        assert len(deltas) == 1
        # Delta is 12 - 5 = 7, NOT the full 12. Total counted = 5 + 7 = 12.
        assert abs(deltas[0]["cost_usd"] - 7.00) < 0.001


def test_rollup_seeds_from_prior_baseline():
    """A second rollup pass for the same session continues from the first
    pass's baseline instead of re-counting the earlier cumulative."""
    with tempfile.TemporaryDirectory() as d:
        day1, day2 = _days_ago(6), _days_ago(3)
        write_hook_jsonl(d, [
            make_hook_entry(session_id="sA", ts=f"{day1}T08:00:00+00:00", cost_usd=0.0),
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
            make_hook_entry(session_id="s1", ts="2025-12-01T09:00:00+00:00", cost_usd=0.00),
            make_hook_entry(session_id="s1", ts="2025-12-01T10:00:00+00:00", cost_usd=5.00),
            make_hook_entry(session_id="s1", ts="2025-12-01T12:00:00+00:00", cost_usd=8.00),
            make_hook_entry(session_id="s2", ts="2025-12-01T09:00:00+00:00", cost_usd=0.00),
            make_hook_entry(session_id="s2", ts="2025-12-01T10:00:00+00:00", cost_usd=3.00),
        ], date="2025-12-01")

        cctrack.rollup_old_hook_files(cctrack_dir=Path(d), retention_days=0)

        assert not Path(d, "statusline-2025-12-01.jsonl").exists()
        monthly_path = Path(d, "monthly-2025-12.json")
        assert monthly_path.exists()
        data = json.loads(monthly_path.read_text())
        assert data["month"] == "2025-12"
        # s1 climbs to 8.00, s2 to 3.00, total = 11.00
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
            make_hook_entry(session_id="s1", ts="2025-12-02T09:00:00+00:00", cost_usd=0.00),
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
            make_hook_entry(session_id="sA", ts="2025-12-01T09:00:00+00:00", cost_usd=0.00),
            make_hook_entry(session_id="sA", ts="2025-12-01T10:00:00+00:00", cost_usd=2.00),
            make_hook_entry(session_id="sA", ts="2025-12-01T18:00:00+00:00", cost_usd=5.00),
        ], date="2025-12-01")
        write_hook_jsonl(d, [
            make_hook_entry(session_id="sA", ts="2025-12-02T10:00:00+00:00", cost_usd=12.00),
            make_hook_entry(session_id="sB", ts="2025-12-02T09:00:00+00:00", cost_usd=0.00),
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


# ── Report cost composition: transcripts first ─────────────────────────
#
# Since 0.12.3 every session-day with a transcript is priced from its tokens at
# list price; the statusline hook adds only session-days that have no
# transcript. (Before, the hook's figure replaced the transcripts for a whole
# machine-day, which dropped sessions the hook never saw: 2026-11-05 showed
# $116.50 for $576 of usage.)

def _report(daily, monthly, fake_now, **kw):
    with patch("cctrack.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.fromisoformat = datetime.fromisoformat
        mock_dt.side_effect = lambda *a, **kw2: datetime(*a, **kw2)
        cctrack.print_report(daily, monthly, **kw)


def _row(out, label):
    return next(l for l in out.splitlines() if l.startswith(f"  {label}"))


def _cost(row):
    """(cost, markers) from a report row."""
    tail = row.split("$")[-1].split()
    return float(tail[0].replace(",", "")), "".join(tail[1:])


def test_transcript_days_are_priced_from_transcripts(capsys):
    """The hook's figure never replaces a day's transcript cost, whether it is
    higher (a runaway counter) or lower (missed redraws)."""
    fake_now = datetime(2026, 4, 17, 12, 0, 0, tzinfo=timezone.utc)
    daily = {"2026-04-15": make_bucket(input=1000, output=500, cost=3.00),
             "2026-04-16": make_bucket(input=2000, output=1000, cost=5.00)}
    monthly = {"2026-04": make_bucket(input=3000, output=1500, cost=8.00)}
    hook_daily = {"2026-04-15": {"cost": 30.00, "sessions": 1},
                  "2026-04-16": {"cost": 1.00, "sessions": 2}}
    _report(daily, monthly, fake_now, hook_daily=hook_daily, hook_install_date="2026-04-01")
    out = capsys.readouterr().out
    assert _cost(_row(out, "2026-04-15")) == (3.00, "")
    assert _cost(_row(out, "2026-04-16")) == (5.00, "")
    assert "Total cost:     $8.00\n" in out
    assert "authoritative" not in out and "adjust" not in out and "~" not in out


def test_hook_only_days_are_added_and_marked(capsys):
    """A day with hook data and no transcript keeps the hook's cost, marked h."""
    fake_now = datetime(2026, 4, 17, 12, 0, 0, tzinfo=timezone.utc)
    daily = {"2026-04-15": make_bucket(input=1000, cost=3.00)}
    monthly = {"2026-04": make_bucket(input=1000, cost=3.00)}
    hook_daily = {"2026-04-15": {"cost": 3.75, "sessions": 1},
                  "2026-04-16": {"cost": 5.00, "sessions": 2}}
    _report(daily, monthly, fake_now, hook_daily=hook_daily, hook_install_date="2026-04-15")
    out = capsys.readouterr().out
    cost, marks = _cost(_row(out, "2026-04-16"))
    assert cost == 5.00 and "h" in marks          # also "!": $5 shown, $0 of tokens
    assert _cost(_row(out, "2026-04-15")) == (3.00, "")
    assert "Total cost:     $8.00 (incl. $5.00 from the hook for sessions with no transcript)" in out
    assert "h = includes the hook's cost for sessions with no transcript" in out


def test_session_days_without_transcripts_add_to_transcribed_days(capsys):
    """reconcile_sources separates, per session-day, hook cost the transcripts
    don't cover; it adds to that day's transcript cost."""
    fake_now = datetime(2026, 11, 6, 12, 0, 0, tzinfo=timezone.utc)
    day = "2026-11-05"
    seen = {"session_id": "seen", "ts": f"{day}T10:00:00+00:00", "date_str": day,
            "cost_usd": 0.0, "model": "m"}
    rows = [seen, {**seen, "ts": f"{day}T11:00:00+00:00", "cost_usd": 82.0},
            {**seen, "session_id": "lost", "cost_usd": 0.0},
            {**seen, "session_id": "lost", "ts": f"{day}T11:00:00+00:00", "cost_usd": 7.0}]
    events = [{"model": "claude-opus-5-5", "timestamp": f"{day}T10:30:00Z", "input": 0,
               "output": 1_000_000, "cache_read": 0, "cache_write": 0, "cache_write_1h": 0,
               "_request_id": f"r{sid}", "source": "local", "session_id": sid,
               "agent": "claude"} for sid in ("seen", "unseen")]              # $20 each
    src = {"name": "local", "agent": "claude", "daily": {}, "hook_daily": {},
           "install_date": "2026-11-01", "deltas": [], "_hook_rows": rows}
    events = cctrack.reconcile_sources(events, [src])
    assert src["hook_only_daily"] == {day: 7.0}
    daily, monthly = cctrack.aggregate(events)
    _report(daily, monthly, fake_now, sources=[src])
    out = capsys.readouterr().out
    # $40 of transcripts (the hook's $82 for "seen" is not used) + $7 hook-only.
    assert _cost(_row(out, day)) == (47.00, "h")


def test_rolled_up_hook_days_count_only_without_transcripts(capsys):
    """Rolled-up hook days (no session detail) fill a day only when the source
    has no transcript that day."""
    fake_now = datetime(2026, 4, 17, 12, 0, 0, tzinfo=timezone.utc)
    src = {"name": "local", "agent": "claude", "install_date": "2026-01-01",
           "daily": {"2026-03-02": make_bucket(input=10, cost=4.00)},
           "hook_daily": {"2026-03-01": {"cost": 9.00}, "2026-03-02": {"cost": 50.00}},
           "hook_only_daily": {},
           "hook_rolled_daily": {"2026-03-01": 9.00, "2026-03-02": 50.00}}
    daily = {"2026-03-02": make_bucket(input=10, cost=4.00)}
    monthly = {"2026-03": make_bucket(input=10, cost=4.00)}
    _report(daily, monthly, fake_now, sources=[src])
    out = capsys.readouterr().out
    assert "$  13.00" in _row(out, "2026-03")                   # 4 transcript + 9 rolled


def test_previous_months_are_not_scaled(capsys):
    fake_now = datetime(2026, 4, 17, 12, 0, 0, tzinfo=timezone.utc)
    daily = {"2026-04-16": make_bucket(input=1000, cost=4.00),
             "2026-03-20": make_bucket(input=2000, cost=8.00),
             "2026-03-21": make_bucket(input=1500, cost=6.00)}
    monthly = {"2026-04": make_bucket(input=1000, cost=4.00),
               "2026-03": make_bucket(input=3500, cost=14.00)}
    hook_daily = {"2026-04-16": {"cost": 5.00, "sessions": 1}}
    _report(daily, monthly, fake_now, hook_daily=hook_daily, hook_install_date="2026-04-15")
    out = capsys.readouterr().out
    assert "$  14.00" in _row(out, "2026-03")
    assert "~" not in out


def _merged_daily(*dailies):
    merged: dict = {}
    for d in dailies:
        for k, b in d.items():
            t = merged.setdefault(k, dict(make_bucket()))
            for f in ("input", "output", "cache_read", "cache_write", "cost"):
                t[f] = t.get(f, 0) + b.get(f, 0)
    return merged


def test_per_source_hosts_add_up(capsys):
    """Every machine's transcript cost counts, whether or not it has a hook."""
    fake_now = datetime(2026, 6, 13, 12, 0, 0, tzinfo=timezone.utc)
    hooked = {"name": "pro", "daily": {"2026-06-12": make_bucket(input=1000, cost=4.00)},
              "hook_daily": {"2026-06-12": {"cost": 5.00, "sessions": 1}},
              "install_date": "2026-06-10"}
    hookless = {"name": "dgx", "daily": {"2026-06-12": make_bucket(input=500, cost=100.00)},
                "hook_daily": {}, "install_date": None}
    two = {"name": "air", "daily": {"2026-06-12": make_bucket(input=200, cost=280.00)},
           "hook_daily": {"2026-06-12": {"cost": 340.90, "sessions": 7}},
           "install_date": "2026-06-01"}
    daily = _merged_daily(hooked["daily"], hookless["daily"], two["daily"])
    monthly = {"2026-06": make_bucket(input=1700, cost=384.00)}
    _report(daily, monthly, fake_now, sources=[hooked, hookless, two])
    out = capsys.readouterr().out
    assert _cost(_row(out, "2026-06-12")) == (384.00, "")
    assert _row(out, "2026-06-12").split()[5] == "8"           # sessions summed across hosts


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


def test_report_energy_column_opt_in(capsys):
    """--energy adds the column, MTD line and the estimate caveat; without it
    the report is unchanged."""
    fake_now = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)
    bucket = make_bucket(input=1000, output=1_000_000, cost=25.0)
    bucket["energy_wh"] = 2000.0
    daily = {"2026-07-15": bucket}
    monthly = {"2026-07": dict(bucket)}

    def run(show_energy):
        with patch("cctrack.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            cctrack.print_report(daily, monthly, show_energy=show_energy)
        return capsys.readouterr().out

    off = run(False)
    assert "Energy" not in off
    assert "ESTIMATE" not in off

    on = run(True)
    assert "Energy" in on
    assert "2.00 kWh" in on          # 2000 Wh
    assert "ESTIMATE" in on          # caveat is shown, not buried
    assert "US household" in on


def test_report_tokens_never_come_from_hook(capsys):
    """Regression: hook token fields are context snapshots. Even when hook data
    is present, the Input/Output columns must reflect the JSONL logs."""
    fake_now = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)
    daily = {"2026-07-15": make_bucket(input=6404, output=112882, cost=61.41)}
    monthly = {"2026-07": make_bucket(input=6404, output=112882, cost=61.41)}
    # A hook bucket carrying the snapshot-shaped values the old code preferred.
    hook_daily = {"2026-07-15": {"cost": 67.55, "sessions": 1,
                                 "input_tokens": 411811, "output_tokens": 80}}

    with patch("cctrack.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.fromisoformat = datetime.fromisoformat
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        cctrack.print_report(daily, monthly, hook_daily=hook_daily,
                             hook_install_date="2026-07-01")

    out = capsys.readouterr().out
    assert "112,882" in out      # JSONL output tokens
    assert "6,404" in out        # JSONL input tokens
    assert "411,811" not in out  # hook context-size snapshot must not appear
    assert "61.41" in out and "67.55" not in out   # cost from the transcripts


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


def test_discover_hook_data_dirs_with_sandy(monkeypatch):
    """Finds both host and Sandy sandbox cctrack dirs.
    With SANDY_HOME unset, sandboxes default to ~/.sandy/sandboxes."""
    monkeypatch.delenv("SANDY_HOME", raising=False)
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


def test_discover_hook_data_dirs_sandy_only(monkeypatch):
    """Finds Sandy sandbox dirs even when host dir doesn't exist.
    With SANDY_HOME unset, sandboxes default to ~/.sandy/sandboxes."""
    monkeypatch.delenv("SANDY_HOME", raising=False)
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
        # Host hook data: session climbs 0 → $5.00
        write_hook_jsonl(str(cctrack_dir), [
            make_hook_entry(session_id="host_sess", cost_usd=0.0,
                            ts="2026-04-17T09:00:00+00:00"),
            make_hook_entry(session_id="host_sess", cost_usd=5.0,
                            ts="2026-04-17T10:00:00+00:00"),
        ], date="2026-04-17")
        # Sandy sandbox hook data: session climbs 0 → $95.30
        sb_dir = Path(home) / ".sandy" / "sandboxes" / "sb1" / "claude" / "cctrack"
        sb_dir.mkdir(parents=True)
        write_hook_jsonl(str(sb_dir), [
            make_hook_entry(session_id="sandy_sess", cost_usd=0.0,
                            ts="2026-04-17T11:00:00+00:00"),
            make_hook_entry(session_id="sandy_sess", cost_usd=95.30,
                            ts="2026-04-17T12:00:00+00:00"),
        ], date="2026-04-17")
        # Config with install date before today
        config = {"installed_at": "2026-04-16T00:00:00+00:00", "version": "0.3.0"}
        (cctrack_dir / "config.json").write_text(json.dumps(config))

        hook_dirs = [cctrack_dir, sb_dir]
        all_hook_events: list[dict] = []
        for hdir in hook_dirs:
            all_hook_events.extend(cctrack.compute_hook_deltas(cctrack.parse_hook_rows(hdir)))
        hook_daily = cctrack.aggregate_hook_data(all_hook_events)
        # Both sessions counted: $5.00 + $95.30 = $100.30
        assert abs(hook_daily["2026-04-17"]["cost"] - 100.30) < 0.01
        assert hook_daily["2026-04-17"]["sessions"] == 2


def test_sessions_are_counted_from_transcripts_plus_hook_only(capsys):
    """Sess counts every session with a transcript that day (Claude Code and
    Codex), plus sessions only the hook saw; the hook booking $0 for a session
    (its counter below an earlier high) doesn't hide it."""
    fake_now = datetime(2026, 11, 14, 12, 0, 0, tzinfo=timezone.utc)
    day = "2026-11-13"

    def ev(sid, agent="claude", model="claude-opus-5-5"):
        return {"model": model, "timestamp": f"{day}T10:00:00Z", "input": 0, "output": 1000,
                "cache_read": 0, "cache_write": 0, "cache_write_1h": 0, "_request_id": f"r-{sid}",
                "source": "local", "session_id": sid, "agent": agent}
    events = [ev("a"), ev("b"), ev("c"), ev("x", "codex", "gpt-6.1-sol")]
    flat = {"session_id": "a", "ts": f"{day}T09:00:00+00:00", "date_str": day,
            "cost_usd": 50.0, "model": "m"}
    rows = [flat, {**flat, "ts": f"{day}T11:00:00+00:00", "cost_usd": 40.0},   # books $0
            {**flat, "session_id": "lost", "cost_usd": 0.0},
            {**flat, "session_id": "lost", "ts": f"{day}T11:00:00+00:00", "cost_usd": 3.0}]
    src = {"name": "local", "agent": "claude", "daily": {}, "hook_daily": {},
           "install_date": "2026-11-01", "deltas": [], "_hook_rows": rows}
    codex = {"name": "local", "agent": "codex", "exact": True, "daily": {}, "hook_daily": {},
             "install_date": None, "deltas": []}
    events = cctrack.reconcile_sources(events, [src, codex])
    daily, monthly = cctrack.aggregate(events)
    _report(daily, monthly, fake_now, sources=[src, codex])
    out = capsys.readouterr().out
    assert _row(out, day).split()[5] == "5"                     # a, b, c, x + lost
    assert [l for l in out.splitlines() if "Sessions:" in l][0].split()[-1] == "5"
