"""compute_hook_deltas on drops, checked against real data.

When a session's statusline counter drops, cctrack rebaselines and counts the
climb after it. Resumed sessions restart their counter at a lower, often
non-zero, level and keep working. 0.12.4 counted only rises above an earlier
high instead; scripts/hook_rule_check.py run on 577 real session-days with
transcripts (2026-11) showed that rule at 85.2% of transcript cost (median
session-day 0.52) against 105.8% (median 1.04) for rebaselining, so 0.12.5
went back to it. Two processes truly alternating on one session id on one
machine (23 of 158 non-zero drops in that data) are still over-counted; since
0.12.3 that can only affect sessions with no transcript.
"""

import pytest

import cctrack

# Every 40th statusline reading of one real session (Sep 2026, transcripts lost
# in a host docker crash).
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


def test_resume_at_a_lower_non_zero_level_keeps_counting():
    """The common real case: the counter drops to a lower level and climbs on."""
    series = [("2026-09-01T09:00", 0.0), ("2026-09-01T12:00", 50.0),
              ("2026-09-02T09:00", 30.0), ("2026-09-02T12:00", 80.0)]
    by_day = {d["date_str"]: d["cost_usd"] for d in cctrack.compute_hook_deltas(rows(series))}
    assert by_day == {"2026-09-01": pytest.approx(50.0), "2026-09-02": pytest.approx(50.0)}


def test_restart_near_zero_counts_the_new_climb():
    series = [("2026-09-01T09:00", 0.0), ("2026-09-01T12:00", 80.0),
              ("2026-09-02T09:00", 0.4), ("2026-09-02T12:00", 25.4)]
    assert booked(cctrack.compute_hook_deltas(rows(series))) == pytest.approx(105.0)


def test_september_series_books_every_climb_after_each_drop():
    # Every step counts except real drops; a sub-0.1% dip is jitter and nets out.
    climbs = sum(b - a for (_, a), (_, b) in zip(SEPTEMBER, SEPTEMBER[1:])
                 if not b < a - max(1e-6, a * 0.001))
    assert booked(cctrack.compute_hook_deltas(rows(SEPTEMBER))) == pytest.approx(climbs)


def test_marks_report_the_final_reading():
    marks = {}
    cctrack.compute_hook_deltas(rows(SEPTEMBER[:20]), marks=marks)      # ends at 343.46
    assert marks == {"s": pytest.approx(343.4581495)}


def test_rollup_baseline_is_the_last_reading(tmp_path):
    import json
    from datetime import datetime, timedelta, timezone
    day = (datetime.now(timezone.utc) - timedelta(days=40)).strftime("%Y-%m-%d")
    old = [(f"{day}T10:00", 0.0), (f"{day}T11:00", 418.44), (f"{day}T12:00", 343.46)]
    (tmp_path / f"statusline-{day}.jsonl").write_text("\n".join(
        json.dumps({"_ts": f"{t}:00+00:00", "session_id": "s", "cost_usd": c}) for t, c in old) + "\n")
    cctrack.rollup_old_hook_files(tmp_path, retention_days=30)
    assert not (tmp_path / f"statusline-{day}.jsonl").exists()           # it was rolled up
    base = cctrack.load_session_baselines(tmp_path)
    assert base["s"]["cost_usd"] == pytest.approx(343.46)
    later = rows([((datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%dT10:00"), 350.0)])
    assert booked(cctrack.compute_hook_deltas(later, base)) == pytest.approx(6.54)
