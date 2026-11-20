"""Step 1: pricing is cheap. The rate-row memo must follow RATES/MODEL_ALIASES
changes, aggregate must not do per-event work it can hoist, and main must not
run passes whose results reconcile_sources overwrites."""
import json
from datetime import datetime, timezone

import pytest

import cctrack


def _row(price=3.0):
    return {"family": "claude-test-9", "tier": "sonnet", "provider": "anthropic",
            "input": price, "output": 15.0, "cache_read": 0.3,
            "cache_write": 3.75, "cache_write_1h": 6.0}


def _ev(model="claude-opus-5", ts="2026-07-15T10:00:00Z", out=0, inp=1000):
    return {"timestamp": ts, "model": model, "input": inp, "output": out,
            "cache_read": 0, "cache_write": 0, "cache_write_1h": 0}


def test_rate_memo_follows_rates_replacement(monkeypatch):
    m = "claude-test-9"
    assert not cctrack.is_known_model(m)
    assert cctrack.get_rates(m) is cctrack.SONNET_RATES
    row = _row()
    with monkeypatch.context() as mp:
        mp.setattr(cctrack, "RATES", [row] + cctrack.RATES)
        assert cctrack._find_rate_row(m) is row
        assert cctrack.get_rates(m)["input"] == 3.0
    assert not cctrack.is_known_model(m)
    with monkeypatch.context() as mp:
        mp.setattr(cctrack, "RATES", [_row(7.0)] + cctrack.RATES)
        assert cctrack.get_rates(m)["input"] == 7.0
    assert cctrack.get_rates(m) is cctrack.SONNET_RATES


def test_rate_memo_follows_alias_changes(monkeypatch):
    def family(model):
        r = cctrack._find_rate_row(model)
        return r["family"] if r else None
    a = "claude-test-alias"
    assert family(a) is None
    with monkeypatch.context() as mp:
        mp.setattr(cctrack, "MODEL_ALIASES", {**cctrack.MODEL_ALIASES, a: "claude-opus-5-5"})
        assert family(a) == "claude-opus-5-5"
    assert family(a) is None
    with monkeypatch.context() as mp:
        mp.setitem(cctrack.MODEL_ALIASES, a, "claude-haiku-4-5")
        assert family(a) == "claude-haiku-4-5"
    assert family(a) is None


def test_rate_lookup_is_memoised(monkeypatch):
    monkeypatch.setattr(cctrack, "RATES", list(cctrack.RATES))
    calls = []
    real = cctrack._find_rate_row_uncached
    monkeypatch.setattr(cctrack, "_find_rate_row_uncached",
                        lambda model: (calls.append(model), real(model))[1])
    models = ["claude-opus-5-5", "gpt-5.4", "claude-brandnew-9"]
    cctrack.aggregate([_ev(models[i % 3]) for i in range(999)])
    assert sorted(calls) == sorted(models)


def test_aggregate_reads_energy_scale_once(monkeypatch):
    monkeypatch.setenv("CCTRACK_ENERGY_SCALE", "2.5")
    calls = []
    real = cctrack.energy_scale
    monkeypatch.setattr(cctrack, "energy_scale", lambda: (calls.append(1), real())[1])
    daily, _ = cctrack.aggregate([_ev(out=1_000_000, inp=0) for _ in range(10)])
    assert len(calls) == 1
    assert daily["2026-07-15"]["energy_wh"] == pytest.approx(10 * 2000 * 2.5)


@pytest.mark.parametrize("ts", [
    "2026-07-15T10:00:00Z", "2026-07-15T10:00:00.123456Z",
    "2026-07-15T23:30:00-05:00", "2026-07-16T02:00:00+05:00",
    "2026-07-15T10:00:00", "2026-12-31T23:59:59-01:00",
    "0999-06-01T12:00:00Z", "1000-01-01T00:00:00Z", "9999-12-31T12:00:00Z"])
def test_aggregate_date_keys_match_strftime(ts):
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    daily, monthly = cctrack.aggregate([_ev(ts=ts)])
    assert list(daily) == [dt.strftime("%Y-%m-%d")]
    assert list(monthly) == [dt.strftime("%Y-%m")]


@pytest.mark.parametrize("detail", [False, True])
def test_main_prices_each_event_once_per_pass(tmp_path, monkeypatch, capsys, detail):
    ts = datetime.now(timezone.utc).isoformat()
    row = {'type': 'assistant', 'sessionId': 's', 'requestId': 'req_1', 'timestamp': ts,
           'cwd': '/work/repo',
           'message': {'id': 'msg_1', 'model': 'claude-sonnet-4',
                       'usage': {'input_tokens': 1000, 'output_tokens': 100}}}
    directory = tmp_path / 'sandboxes/local/claude/projects/p'
    directory.mkdir(parents=True)
    (directory / 's.jsonl').write_text(json.dumps(row) + '\n')
    monkeypatch.setattr(cctrack, 'default_dirs', lambda: [str(directory)])
    monkeypatch.setattr(cctrack, 'sandbox_roots', lambda: [])
    monkeypatch.setattr(cctrack, 'discover_hook_data_dirs', lambda: [])
    monkeypatch.setattr(cctrack, 'read_hook_config', lambda: None)
    monkeypatch.setattr(cctrack, 'is_hook_installed', lambda: True)
    monkeypatch.setattr(cctrack, 'fetch_remote_jsonl', lambda host:
                        '___CCTRACK_FILE_SEP___/sandboxes/copy/claude/projects/p/s.jsonl\n'
                        + json.dumps(row) + '\n')
    monkeypatch.setattr(cctrack, 'parse_remote_hook_data', lambda host: ([], {}, {}))
    monkeypatch.setattr('sys.argv', ['cctrack', '--agent', 'claude', '--remote', 'air']
                        + (['--json'] if detail else []))
    sizes = []
    real = cctrack.aggregate
    monkeypatch.setattr(cctrack, 'aggregate', lambda ev: (sizes.append(len(ev)), real(ev))[1])
    cctrack.main()
    capsys.readouterr()
    assert sum(sizes) == (1 if detail else 2)
