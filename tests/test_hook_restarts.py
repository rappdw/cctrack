"""compute_hook_deltas: restarts vs a second process on the same session id.

The September 2026 series below is every 40th statusline reading of one real
session (a sandbox whose transcripts were lost in a host docker crash). Claude
reported its "most recent conversation moved to a background session ...
registered from another machine or container": two processes, one session id.
None of its drops goes near $0, so none is a restart. Until 0.12.4 each climb
from a lower level was booked as new spend: $984 for a counter that rose $731.
"""

import pytest

import cctrack

SEPTEMBER = [
    ("2026-09-19T14:30", 14.3064355), ("2026-09-19T18:27", 30.246942),
    ("2026-09-19T19:18", 41.272944), ("2026-09-19T19:26", 21.433828),
    ("2026-09-19T19:32", 28.39017), ("2026-09-19T20:14", 38.186166),
    ("2026-09-20T01:26", 58.275832), ("2026-09-20T12:06", 78.967518),
    ("2026-09-20T18:08", 242.3018975), ("2026-09-21T02:26", 267.434582),
    ("2026-09-21T15:11", 355.671606), ("2026-09-24T20:32", 305.3896045),
    ("2026-09-24T21:18", 311.6347253), ("2026-09-25T13:02", 335.9605541),
    ("2026-09-26T00:23", 348.3447037), ("2026-09-27T03:10", 363.9034341),
    ("2026-09-27T11:53", 340.7020637), ("2026-09-27T13:02", 376.4837494),
    ("2026-09-27T14:25", 418.4432958), ("2026-09-27T22:16", 343.4581495),
    ("2026-09-27T22:51", 360.589353), ("2026-09-28T02:48", 434.9204225),
    ("2026-09-28T18:11", 512.2701114), ("2026-09-28T22:08", 644.7153679),
    ("2026-09-29T03:01", 733.2487956), ("2026-09-29T15:54", 742.195783),
    ("2026-09-30T23:41", 742.0742262), ("2026-09-30T23:45", 745.4281188),
]


def rows(series, sid="s"):
    return [{"session_id": sid, "ts": f"{t}:00+00:00", "date_str": t[:10],
             "cost_usd": c, "model": "claude-opus-5-5"} for t, c in series]


def booked(deltas):
    return sum(d["cost_usd"] for d in deltas)


def test_second_process_drops_book_only_new_highs():
    deltas = cctrack.compute_hook_deltas(rows(SEPTEMBER))
    assert booked(deltas) == pytest.approx(745.4281188 - 14.3064355)       # $731.12
    by_day = {d["date_str"]: d["cost_usd"] for d in deltas}
    assert "2026-09-24" not in by_day             # 305 → 336: below the $355.67 high
    # Sep 27 books only what rose above the earlier high ($355.67, Sep 21).
    assert by_day["2026-09-27"] == pytest.approx(418.4432958 - 355.671606)


def test_genuine_restart_counts_the_new_process():
    series = [("2026-09-01T09:00", 0.0), ("2026-09-01T12:00", 80.0),
              ("2026-09-02T09:00", 0.4), ("2026-09-02T12:00", 25.4)]      # restart near $0
    assert booked(cctrack.compute_hook_deltas(rows(series))) == pytest.approx(80.0 + 25.0)


def test_restart_threshold_is_a_fraction_of_the_previous_reading():
    f = cctrack.HOOK_RESTART_FRACTION
    just_restart = [("2026-09-01T09:00", 0.0), ("2026-09-01T10:00", 100.0),
                    ("2026-09-01T11:00", 100.0 * f), ("2026-09-01T12:00", 100.0 * f + 5)]
    not_restart = [("2026-09-01T09:00", 0.0), ("2026-09-01T10:00", 100.0),
                   ("2026-09-01T11:00", 100.0 * f + 1), ("2026-09-01T12:00", 100.0 * f + 6)]
    assert booked(cctrack.compute_hook_deltas(rows(just_restart))) == pytest.approx(105.0)
    assert booked(cctrack.compute_hook_deltas(rows(not_restart))) == pytest.approx(100.0)


def test_interleaved_processes_on_one_machine_book_one_climb():
    """Two live processes, $100 apart, both climbing $10: the higher one's
    climb is what's booked, never the switches between them."""
    series = []
    for i in range(10):
        series += [(f"2026-09-01T10:{i:02d}", 200.0 + i), (f"2026-09-01T10:{i:02d}:30"[:16], 300.0 + i)]
    series = sorted(set(series))
    assert booked(cctrack.compute_hook_deltas(rows(series))) == pytest.approx(309.0 - 200.0, abs=0.01)


def test_marks_report_the_highest_counted_level():
    marks = {}
    cctrack.compute_hook_deltas(rows(SEPTEMBER), marks=marks)
    assert marks == {"s": pytest.approx(745.4281188)}
    # A last reading below the high: the mark is still the high.
    marks = {}
    cctrack.compute_hook_deltas(rows(SEPTEMBER[:20]), marks=marks)      # ends at 343.46
    assert marks["s"] == pytest.approx(418.4432958)


def test_rollup_seeds_later_data_from_the_high_not_the_last_reading(tmp_path, monkeypatch):
    """A rolled-up day ending on the lower process's reading must not let the
    higher process's next reading be counted again."""
    import json
    from datetime import datetime, timedelta, timezone
    day = (datetime.now(timezone.utc) - timedelta(days=40)).strftime("%Y-%m-%d")
    old = [(f"{day}T10:00", 0.0), (f"{day}T11:00", 418.44), (f"{day}T12:00", 343.46)]
    (tmp_path / f"statusline-{day}.jsonl").write_text("\n".join(
        json.dumps({"_ts": f"{t}:00+00:00", "session_id": "s", "cost_usd": c}) for t, c in old) + "\n")
    cctrack.rollup_old_hook_files(tmp_path, retention_days=30)
    assert not (tmp_path / f"statusline-{day}.jsonl").exists()           # it was rolled up
    base = cctrack.load_session_baselines(tmp_path)
    assert base["s"]["cost_usd"] == pytest.approx(418.44)
    later = rows([((datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%dT10:00"), 420.0)])
    assert booked(cctrack.compute_hook_deltas(later, base)) == pytest.approx(1.56)
