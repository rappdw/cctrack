"""Report rows whose cost doesn't match their tokens at list price are flagged,
and the 2026-11-24 rate card additions."""

import io
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

import cctrack

sys.path.insert(0, str(Path(__file__).parent))
from test_detail import claude_line  # noqa: E402

TODAY = datetime.now(timezone.utc)
DAY = TODAY.strftime("%Y-%m-%d")
LAST_MONTH = (TODAY.replace(day=1) - timedelta(days=1)).replace(day=15).strftime("%Y-%m-%d")


def transcript(day, sid, n, model="claude-opus-5-5"):
    return cctrack.parse_lines([
        claude_line(f"{day}T10:{i % 60:02d}:10Z", f"req-{sid}-{day}-{i}", sid=sid, inp=100,
                    out=1000, cr=200_000, model=model) for i in range(n)])


def price(events):
    return sum(cctrack.calculate_cost(e["model"], e["input"], e["output"], e["cache_read"],
                                      e["cache_write"], e.get("cache_write_1h", 0),
                                      date_str=cctrack._event_date(e),
                                      request_input=e.get("_request_input")) for e in events)


def hook(day, sid, total):
    return [{"session_id": sid, "ts": f"{day}T09:00:00+00:00", "date_str": day,
             "cost_usd": 0.0, "model": "claude-opus-5-5"},
            {"session_id": sid, "ts": f"{day}T11:00:00+00:00", "date_str": day,
             "cost_usd": total, "model": "claude-opus-5-5"}]


def report(events, hook_rows, install="2020-01-01"):
    sources = [{"name": "local", "agent": "claude", "daily": {}, "hook_daily": {},
                "install_date": install, "deltas": [], "_hook_rows": hook_rows}]
    events = cctrack.reconcile_sources(events, sources)
    out = io.StringIO()
    with patch("sys.stdout", out):
        daily, monthly = cctrack.aggregate(events)
        cctrack.print_report(daily, monthly, sources=sources)
    return out.getvalue()


def row_for(text, label):
    return next(l for l in text.splitlines() if l.startswith(f"  {label}"))


# Rows are priced from their transcripts, so a row's cost only departs from its
# tokens through hook cost for sessions with no transcript ("lost" below).

def test_hook_only_cost_far_above_tokens_is_flagged_with_both_figures():
    ev = transcript(DAY, "a", 200)                 # about $12 of tokens
    t = price(ev)
    text = report(ev, hook(DAY, "a", 1.1 * t) + hook(DAY, "lost", 2 * t))
    assert row_for(text, DAY).rstrip().endswith("h!")
    assert "differs from the row's tokens at list price by more than 25%" in text
    assert f"${3 * t:,.2f} shown, ${t:,.2f} from tokens (3.00x); see --explore {DAY}" in text


def test_hook_figure_for_a_transcribed_session_never_moves_the_row():
    ev = transcript(DAY, "a", 200)
    t = price(ev)
    for factor in (0.2, 0.4, 3.0, 20.0):
        text = report(ev, hook(DAY, "a", factor * t))
        assert "! =" not in text and f"${t:,.2f}".replace("$", "") in row_for(text, DAY)


def test_agreeing_rows_and_small_gaps_are_not_flagged():
    ev = transcript(DAY, "a", 200)
    t = price(ev)
    assert "!" not in row_for(report(ev, hook(DAY, "lost", 0.2 * t)), DAY)  # within 25%
    small = transcript(DAY, "b", 10)                                         # well under $5
    assert "! =" not in report(small, hook(DAY, "lost", 3 * price(small)))


def test_hook_spend_without_transcripts_is_flagged():
    other = transcript(DAY, "a", 200)
    lost_day = (TODAY - timedelta(days=1)).strftime("%Y-%m-%d")
    if not lost_day.startswith(DAY[:7]):
        pytest.skip("needs two days in the current month")
    text = report(other, hook(DAY, "a", price(other)) + hook(lost_day, "lost", 50.0))
    assert row_for(text, lost_day).rstrip().endswith("!")
    assert "$50.00 shown, $0.00 from tokens (no transcripts)" in text


def test_month_rows_are_checked():
    ev = transcript(LAST_MONTH, "m", 200)
    t = price(ev)
    text = report(ev, hook(LAST_MONTH, "lost", 2 * t))
    assert row_for(text, LAST_MONTH[:7]).rstrip().endswith("h!")
    assert f"{LAST_MONTH[:7]:<10}  ${3 * t:,.2f} shown" in text


def test_unrecognized_models_are_called_out():
    ev = transcript(DAY, "u", 200, model="claude-opus-9-9")
    text = report(ev, hook(DAY, "lost", 2 * price(ev)))
    assert "includes unrecognized models, so the token price is a guess" in text


def test_thresholds_are_adjustable(monkeypatch):
    ev = transcript(DAY, "a", 200)
    t = price(ev)
    monkeypatch.setenv("CCTRACK_MISMATCH_RATIO", "4")
    assert "! =" not in report(ev, hook(DAY, "lost", 2 * t))
    monkeypatch.setenv("CCTRACK_MISMATCH_RATIO", "1.1")
    monkeypatch.setenv("CCTRACK_MISMATCH_MIN_USD", "0.01")
    assert "!" in row_for(report(ev, hook(DAY, "lost", 0.2 * t)), DAY)
    monkeypatch.setenv("CCTRACK_MISMATCH_RATIO", "nonsense")
    assert cctrack.mismatch_thresholds()[0] == 1.25


def test_cost_mismatch_values():
    assert cctrack.cost_mismatch(100, 100) is None
    assert cctrack.cost_mismatch(130, 100) == pytest.approx(1.3)
    assert cctrack.cost_mismatch(70, 100) == pytest.approx(0.7)
    assert cctrack.cost_mismatch(12, 10) is None                 # under the $5 gap
    assert cctrack.cost_mismatch(50, 0) == float("inf")
    assert cctrack.cost_mismatch(0, 50) == 0.0


# ── Rate card, checked against both pricing pages on 2026-11-24 ─────────────

@pytest.mark.parametrize("model,inp,cached,out,write,lc", [
    ("gpt-6.2-astra", 8.0, 0.40, 40.0, 10.0, (16.0, 0.80, 60.0, 20.0)),
    ("gpt-6.2-sol", 2.0, 0.10, 10.0, 2.5, (4.0, 0.20, 15.0, 5.0)),
])
def test_gpt_6_2_rows(model, inp, cached, out, write, lc):
    r = cctrack.get_rates(model)
    assert r["family"] == model
    assert (r["input"], r["cache_read"], r["output"], r["cache_write"]) == (inp, cached, out, write)
    l = cctrack.get_rates(model, request_input=300_000)
    assert (l["input"], l["cache_read"], l["output"], l["cache_write"]) == lc
    fast = cctrack.get_rates(model, service_tier="fast")
    assert (fast["input"], fast["cache_read"], fast["output"]) == (2 * inp, 2 * cached, 2 * out)
    assert cctrack.get_rates(model, service_tier="flex")["input"] == inp      # no flex tier


@pytest.mark.parametrize("model,inp,cached,out", [
    ("gpt-4.1", 2.0, 0.50, 8.0), ("gpt-4.1-mini", 0.40, 0.10, 1.60),
    ("gpt-4o", 2.50, 1.25, 10.0), ("gpt-4o-mini", 0.15, 0.075, 0.60),
    ("o3", 2.0, 0.50, 8.0), ("o3-pro", 20.0, 20.0, 80.0),
])
def test_older_openai_rows(model, inp, cached, out):
    r = cctrack.get_rates(model)
    assert r["family"] == model
    assert (r["input"], r["cache_read"], r["output"]) == (inp, cached, out)


def test_rate_card_version_and_unlisted_ids():
    assert cctrack.RATE_CARD_VERSION == "2026-11-24"
    assert not cctrack.is_known_model("codex-auto-review")
    assert not cctrack.is_known_model("gpt-daybreak-blue-latest")
