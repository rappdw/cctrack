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


def test_aggregate_hook_data_recent_overrides_monthly():
    """If daily events exist for a date that's also in monthly, daily wins."""
    events = [
        {"date_str": "2026-01-10", "cost_usd": 12.00, "session_id": "s1",
         "input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_write": 0, "model": "m"},
    ]
    monthly = {"2026-01-10": {"cost": 10.00, "sessions": 3}}
    result = cctrack.aggregate_hook_data(events, monthly)
    assert result["2026-01-10"]["cost"] == 12.00
    assert result["2026-01-10"]["sessions"] == 1


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
    """Only dates on/after hook install are considered."""
    daily = {
        "2026-04-14": make_bucket(cost=10.00),
        "2026-04-15": make_bucket(cost=5.00),
    }
    hook_daily = {
        "2026-04-14": {"cost": 12.00, "sessions": 1},  # before hook install, ignored
        "2026-04-15": {"cost": 6.25, "sessions": 1},
    }
    factor, is_measured = cctrack._compute_accuracy_factor(daily, hook_daily, "2026-04-15")
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
    """When overlap exists, measured accuracy factor is shown in legend."""
    fake_now = datetime(2026, 4, 17, 12, 0, 0, tzinfo=timezone.utc)

    daily = {
        "2026-04-14": make_bucket(input=1000, cost=4.00),   # pre-hook, estimate
        "2026-04-15": make_bucket(input=2000, cost=5.00),   # post-hook, overlap
    }
    monthly = {"2026-04": make_bucket(input=3000, cost=9.00)}
    hook_daily = {
        "2026-04-15": {"cost": 6.25, "sessions": 1},  # 1.25x factor
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
