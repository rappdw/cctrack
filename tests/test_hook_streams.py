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
    # Still wrong merged: a counter near $0 looks like a restart, so each switch
    # from it back to the $100 counter books about $95. Hence the per-machine split.
    assert old_style > 500
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


# ── A runaway hook counter can't reach the totals ───────────────────────────
# 0.12.1 replaced hook session-days above 5x their transcript cost. Since
# 0.12.3 transcripts price every session-day they cover, so the case can't
# arise; these tests pin that instead.

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
    """Two processes under one session id on ONE machine: the streams can't be
    told apart, so the hook figure itself is inflated."""
    rows = []
    for i in range(20):
        rows.append(row(f"10:{i:02d}:00", 1.0 + 0.01 * i, sid))
        rows.append(row(f"10:{i:02d}:30", 60.0 + 0.01 * i, sid))
    return rows


def _report(events, sources):
    events = cctrack.reconcile_sources(events, sources)
    out = io.StringIO()
    with patch("sys.stdout", out):
        daily, monthly = cctrack.aggregate(events)
        cctrack.print_report(daily, monthly, sources=sources)
    return out.getvalue(), events


def test_inflated_hook_session_is_priced_from_its_transcript():
    events = transcript()
    sources = [source("local", _hook_rows=same_machine_sawtooth(), install_date="2026-11-01")]
    out, events = _report(events, sources)
    assert sources[0]["hook_daily"][DAY]["cost"] > 1000        # the counter is still wrong...
    assert sources[0]["hook_only_daily"] == {}                 # ...but never reaches a total
    daily, _ = cctrack.aggregate(events)
    assert daily[DAY]["cost"] == pytest.approx(estimate(events))
    assert "! =" not in out                                    # the row matches its tokens


def test_hook_session_without_transcript_still_counts():
    events = transcript("other")
    rows = [row("10:00:00", 0.0, "lost"), row("11:00:00", 75.0, "lost")]
    sources = [source("local", _hook_rows=rows, install_date="2026-11-01")]
    out, _ = _report(events, sources)
    assert sources[0]["hook_only_daily"] == {DAY: pytest.approx(75.0)}
    assert "h = includes the hook's cost for sessions with no transcript" in out


def test_explore_shows_the_inflated_hook_figure_for_comparison():
    events = transcript()
    sources = [source("local", _hook_rows=same_machine_sawtooth(), install_date="2026-11-01")]
    events = cctrack.reconcile_sources(events, sources)
    hook = sum(d["cost_usd"] for d in sources[0]["deltas"])
    out = io.StringIO()
    with patch("sys.stdout", out):
        cctrack.print_explore(events, DAY, hook_by_source={"local": hook},
                              hook_by_session={("local", "s"): hook})
    text = out.getvalue()
    assert f"Total:  ${estimate(events):,.2f}  (transcripts priced at list price)" in text
    assert "⚠ The hook recorded" in text
