"""Hook cost when one session id is live on two processes at once.

Observed 2026-11-22: sandboxes rsynced to a second host and started there too
(sandy runs `claude --continue`) put one session id in two processes, each
writing its own cumulative cost. Merging both machines' readings before
differencing booked every switch from the lower counter to the higher one:
$11,612 of hook cost for one day with $86 of real usage.
"""

import copy
import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import cctrack

sys.path.insert(0, str(Path(__file__).parent))
from test_detail import claude_line  # noqa: E402

DAY = "2026-11-22"


def row(ts, cost, sid="s", source=None, day=DAY):
    r = {"session_id": sid, "ts": f"{day}T{ts}+00:00", "date_str": day,
         "cost_usd": cost, "model": "claude-opus-5-5"}
    if source:
        r["_source"] = source
    return r


def source(name, **kw):
    return {"name": name, "agent": "claude", "daily": {}, "hook_daily": {},
            "install_date": None, "deltas": [], **kw}


def history(sid="s"):
    """The session's statusline history from before the rsync: on both hosts."""
    return [row(f"08:{i:02d}:00", c, sid, day="2026-11-21") for i, c in enumerate((0.0, 40.0, 100.0))]


def interleaved(sid="s"):
    """After the copy: the Mac's process idles at $100 and keeps rendering;
    dgx's process, resumed from the same history, restarts low and climbs $19."""
    mac, dgx = [], []
    for i in range(20):
        mac.append(row(f"10:{i:02d}:00", 100.0, sid))
        dgx.append(row(f"10:{i:02d}:30", 5.0 + i, sid))
    return mac, dgx


def booked(sources):
    return sum(d["cost_usd"] for s in sources for d in s["deltas"])


def test_two_live_machines_each_count_only_their_own_climb():
    mac, dgx = interleaved()
    sources = [source("local", _hook_rows=history() + mac),
               source("dgx", _hook_rows=copy.deepcopy(history()) + dgx)]
    cctrack.reconcile_sources([], sources)
    # History once ($100), the idle Mac nothing, dgx its climb after its first reading ($19).
    assert booked(sources) == pytest.approx(100.0 + 19.0)
    assert sources[0]["hook_daily"]["2026-11-21"]["cost"] == pytest.approx(100.0)
    assert sources[1]["hook_daily"][DAY]["cost"] == pytest.approx(19.0)


def test_merging_interleaved_machines_was_the_bug():
    """The old single walk over both machines' readings books each switch."""
    mac, dgx = interleaved()
    merged = [{**r, "_source": ""} for r in sorted(mac + dgx, key=lambda r: r["ts"])]
    old_style = sum(d["cost_usd"] for d in cctrack.compute_hook_deltas(merged))
    assert old_style > 1500                                  # about $95 per switch
    split = [{**r, "_source": "local"} for r in mac] + [{**r, "_source": "dgx"} for r in dgx]
    assert sum(d["cost_usd"] for d in cctrack.compute_hook_deltas(split)) == pytest.approx(19.0)


def test_copied_history_alone_still_counts_once():
    sources = [source("local", _hook_rows=history()),
               source("dgx", _hook_rows=copy.deepcopy(history()))]
    cctrack.reconcile_sources([], sources)
    assert booked(sources) == pytest.approx(100.0)
    assert sources[1]["deltas"] == []                        # the copy is the local one


def test_handoff_between_hosts_keeps_the_running_total():
    """The Mac stops; dgx continues the same counter. Not overlapping, so one walk."""
    later = [row(f"12:{i:02d}:00", c) for i, c in enumerate((110.0, 130.0))]
    sources = [source("local", _hook_rows=history()),
               source("dgx", _hook_rows=copy.deepcopy(history()) + later)]
    cctrack.reconcile_sources([], sources)
    assert booked(sources) == pytest.approx(130.0)
    assert sources[1]["hook_daily"][DAY]["cost"] == pytest.approx(30.0)


def test_both_machines_genuinely_working_count_both():
    mac = [row(f"10:{i:02d}:00", 50.0 + 2 * i) for i in range(10)]
    dgx = [row(f"10:{i:02d}:30", 5.0 + i) for i in range(10)]
    sources = [source("local", _hook_rows=mac), source("dgx", _hook_rows=dgx)]
    cctrack.reconcile_sources([], sources)
    assert sources[0]["hook_daily"][DAY]["cost"] == pytest.approx(18.0)
    assert sources[1]["hook_daily"][DAY]["cost"] == pytest.approx(9.0)


def test_rows_without_timestamps_are_never_dropped_as_copies():
    rows = [{"session_id": "s", "date_str": DAY, "cost_usd": 3.0},
            {"session_id": "s", "date_str": DAY, "cost_usd": 3.0}]
    assert len(cctrack._drop_copied_hook_rows(rows)) == 2


# ── Safety net: a hook day far above its transcript estimate ────────────────

def transcript(sid="s", n=4):
    """Real usage for the session that day: n small Opus 5.5 requests."""
    return cctrack.parse_lines([
        claude_line(f"{DAY}T10:{i:02d}:10Z", f"req-{sid}-{i}", sid=sid, inp=100, out=1000,
                    cr=200_000, model="claude-opus-5-5") for i in range(n)])


def estimate(events):
    return sum(cctrack.calculate_cost(e["model"], e["input"], e["output"], e["cache_read"],
                                      e["cache_write"], e.get("cache_write_1h", 0),
                                      date_str=DAY, request_input=e.get("_request_input"))
               for e in events)


def same_machine_sawtooth(sid="s"):
    """Two processes under one session id on ONE machine: streams can't be told
    apart, so the walk still books every switch."""
    rows = []
    for i in range(20):
        rows.append(row(f"10:{i:02d}:00", 1.0 + 0.01 * i, sid))
        rows.append(row(f"10:{i:02d}:30", 60.0 + 0.01 * i, sid))
    return rows


def test_implausible_hook_day_is_priced_from_transcripts():
    events = transcript()
    j = estimate(events)
    sources = [source("local", _hook_rows=same_machine_sawtooth())]
    cctrack.reconcile_sources(events, sources)
    assert sources[0]["hook_daily"][DAY]["cost"] == pytest.approx(j)
    [f] = cctrack.capped_hook_days(sources)
    assert f["session_id"] == "s" and f["hook"] > 1000 and f["estimate"] == pytest.approx(j)


def test_plausible_hook_days_are_left_alone():
    events = transcript(n=40)
    j = estimate(events)
    assert 4.0 * j - j > cctrack.HOOK_CAP_MIN_USD            # only the 5x rule keeps it
    rows = [row("10:00:00", 0.0), row("11:00:00", 4.0 * j)]    # hook 4x: within the 5x rule
    sources = [source("local", _hook_rows=rows)]
    cctrack.reconcile_sources(events, sources)
    assert sources[0]["hook_daily"][DAY]["cost"] == pytest.approx(4.0 * j)
    assert cctrack.capped_hook_days(sources) == []


def test_small_gaps_and_missing_transcripts_keep_the_hook_cost():
    tiny = cctrack.parse_lines([claude_line(f"{DAY}T10:00:10Z", "req-t", sid="t", inp=1, out=1)])
    sources = [source("local", _hook_rows=[row("10:00:00", 0.0, "t"), row("11:00:00", 0.5, "t"),
                                           row("10:00:00", 0.0, "lost"), row("11:00:00", 75.0, "lost")])]
    cctrack.reconcile_sources(tiny, sources)
    costs = {d["session_id"]: d["cost_usd"] for d in sources[0]["deltas"]}
    assert costs == {"t": pytest.approx(0.5), "lost": pytest.approx(75.0)}   # < $1 gap; no transcript
    assert cctrack.capped_hook_days(sources) == []


def test_cap_keeps_the_accuracy_factor_and_history_sane():
    """An inflated hook day used to raise the factor that scales estimated days."""
    events = transcript()
    sources = [source("local", _hook_rows=same_machine_sawtooth(), install_date="2026-11-01")]
    cctrack.reconcile_sources(events, sources)
    factor, measured = cctrack._compute_accuracy_factor_sources(sources)
    assert measured and factor == pytest.approx(1.0)


def test_report_and_explore_say_what_was_replaced():
    events = transcript()
    sources = [source("local", _hook_rows=same_machine_sawtooth(), install_date="2026-11-01")]
    events = cctrack.reconcile_sources(events, sources)
    out = io.StringIO()
    with patch("sys.stdout", out):
        daily, monthly = cctrack.aggregate(events)
        cctrack.print_report(daily, monthly, sources=sources)
    text = out.getvalue()
    assert "1 session-day(s) had a hook cost more than 5x their transcript estimate" in text
    assert f"{DAY}  s…  hook $" in text

    capped = cctrack.capped_hook_days(sources)
    hook_by_session = {("local", "s"): sum(d["cost_usd"] for d in sources[0]["deltas"])}
    out = io.StringIO()
    with patch("sys.stdout", out):
        cctrack.print_explore(events, DAY, hook_by_source={"local": hook_by_session[("local", "s")]},
                              hook_by_session=hook_by_session, capped=capped)
    text = out.getvalue()
    assert "priced from the transcripts instead" in text
    assert "! in the Hook $ column" in text
    assert "!" in [l for l in text.splitlines() if l.lstrip().startswith("s ")][0]
