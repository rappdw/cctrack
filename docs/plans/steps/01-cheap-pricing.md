# Step 01. Make pricing cheap

Plan for `### Step 1` of `docs/plans/incremental-cache.md`. Written against commit `6b28d79` (Step 0 done; 379 passed, 1 skipped in about 18 s).

No product decision is needed. Nothing in section 7 is touched. The step does not pause for the user.

## 1. Outcome

- `_find_rate_row(model)` is memoised per model string. The memo resets itself when `cctrack.RATES` or `cctrack.MODEL_ALIASES` is replaced by another object or changes length, so tests that `monkeypatch.setattr(cctrack, "RATES", ...)` see their own rows.
- `aggregate` reads `CCTRACK_ENERGY_SCALE` once per call instead of once per event, and builds its day and month keys without two `strftime` calls per event. Its results are bit-for-bit the same.
- `main` no longer runs `aggregate` per source before `reconcile_sources`. Those results were always overwritten. A default report now prices each event twice (once per source inside `reconcile_sources`, once for the final totals) instead of three times. A detail report or `--explore` prices each event once instead of twice.
- `CCTRACK_TIMING=1` still shows an `aggregate` phase in every mode: the per-source pricing inside `reconcile_sources` is now timed as `aggregate`.
- Measured on the bench corpus, `aggregate` on 200,000 events takes about 0.30 s instead of 3.0–3.3 s (about 10×, against the 5× required). `aggregate` plus `reconcile` in a `scripts/bench_cache.py` run falls from 1.11 s to 0.14 s. Stdout is byte-identical in every mode checked.

## 2. Findings

Checked in this sandbox: `.venv` Python 3.12.15, Linux aarch64, 10 cores. All prototypes were in the scratchpad, on a copy of `src/` and `tests/`. Nothing in the repo was changed except this file.

1. **Baseline reproduces F9.** I built the bench corpus with `scripts/bench_cache.py --dir <scratch>/bench-default` (defaults: 200 Claude files × 100 events, 50 Codex files × 100 events). I parsed it in-process, repeated the 25,000 events to 200,000, and timed one `aggregate` call, best of 3. Three runs gave **3.02, 3.19 and 3.11 s** (15–16 µs per event). F9 recorded 2.96 s. Under cProfile, `_find_rate_row` accounted for 7.6 of 9.2 profiled seconds. That time goes into `sorted(RATES, key=lambda …)` on every call (800,000 sorts), `_canonical_model`'s `re.escape` and `re.fullmatch` per alias, and `_family_matches`.
2. **Memoising alone is not enough.** I memoised `_find_rate_row` and changed nothing else. The same measurement gave **0.567–0.671 s** (0.609, 0.611 and 0.594 s in the final three runs). That is 4.4× to 5.2× against F9's 2.96 s, so the 5× criterion would sometimes fail. With the memo in place, the profile showed what remains:
   - `strftime`, called twice per event: about 25% of the time.
   - `energy_scale()`, which reads `os.environ` once per event: about 15%.
3. **Memo plus two small changes inside `aggregate` meets the criterion with room to spare.** The two changes: read `energy_scale()` once per call, and build `date_str`/`month_str` with an f-string. With both, the same measurement gave **0.274, 0.281, 0.304, 0.319 and 0.311 s** (about 1.5 µs per event), 9.3× to 10.8× faster than F9 and about 10× faster than this machine's own baseline.
4. **The f-string date key must keep a `strftime` fallback below year 1000.** glibc's `%Y` does not zero-pad: `datetime(999,3,4).strftime("%Y-%m-%d")` gives `'999-03-04'`, and year 1 gives `'1-03-04'`. macOS pads to four digits. So a plain f-string, or slicing `date_str[:7]` for the month, would change keys for years below 1000 on one platform or the other. For years 1000–9999, `f"{y}-{m:02d}-{d:02d}"` equals `strftime("%Y-%m-%d")` on every platform. Using `strftime` only below 1000 keeps today's keys exactly. This is an absurd edge case, but the rule is byte-identical output, and the fallback costs one comparison.
5. **The energy figure is unchanged to the bit** when the scale is computed once and passed in. `estimate_energy_wh` evaluates `scale * (…)` in the same order; only where `scale` comes from changes. Within one `aggregate` call the environment cannot change.
6. **What `reconcile_sources` overwrites (the "Planner confirms" item).** `reconcile_sources(events, sources)`:
   - sets `src["daily"]` for **every** source, from a fresh `aggregate` of that source's share of the deduped events;
   - sets `src["deltas"]` and `src["hook_daily"]` for every source that has `_hook_rows`, which is every Claude source, local and remote;
   - does not touch `install_date`, `name`, `agent`, `exact`, `_hook_rows`, `_hook_baselines` or `_hook_monthly`. Codex sources' `hook_daily` stays the `{}` set when they were created.

   Of the values `main` computes before reconcile, the only one still read afterwards is **`install_date`**. For the local source, it comes from `read_hook_config()["installed_at"]` and from `min(local_hook_daily)`, where `local_hook_daily` is `aggregate_hook_data(local_hook_events, local_hook_monthly)`. For a remote source, it is `min(remote_hook_daily)`. Neither uses `aggregate`, so the hook-side computation before reconcile (`compute_hook_deltas`, `aggregate_hook_data`) must stay. The three `aggregate` calls before reconcile (`local_daily`, `codex_daily` in `_add_codex_source`, `remote_daily`) are read by nothing: I traced every use of `sources` between their creation and `reconcile_sources` in `main`, and there is none. `aggregate` is pure (it does not mutate events), so removing them cannot change anything else.
7. **Equivalence, old source against new.** I added a scratch script that runs Step 0's matrix (7 variants × 8 modes = 56 runs, including `--remote fakehost`, the raw stream, `--agent claude` and `--agent codex`) on one corpus. It ran with the old `src`, then with the prototype `src`, then with the old `src` again. **All 56 outputs were identical** in four configurations:
   - plain;
   - `CCTRACK_ENERGY_SCALE=3.7`;
   - `--energy` added to every mode;
   - `--days 3` added to every mode.

   The same held for `scripts/bench_cache.py` with and without `--remote` (same `stdout sha256`), and for this sandbox's real logs. The real-log comparison covered the default report, `--explore yesterday`, `--json` and `--energy`, all with identical sha256.
8. **No existing test catches a memo that ignores `RATES` changes.** I mutated the prototype so that the memo is never invalidated, and the full suite still passed (393 passed). The two tests that patch `RATES` (`test_intro_pricing_mechanism`, `test_event_cost_breakdown_is_date_aware`) both use the model `claude-test-9`. Nothing looks it up before the patch. Both patches insert rows with the same prices, so a stale memo hit returns equal values. The brief's mutation therefore needs a **new** test (Deviations).
9. **The Step 0 timing test needs the `aggregate` phase kept in every mode.** `test_timing_lines_cover_the_run` requires a top-level `aggregate` line for `--remote fakehost`, `--explore` and `--json` runs. With the passes before reconcile removed, `--explore` and `--json` would have no `aggregate` phase, because they return before the final `aggregate`, and the test would fail. Fix: `reconcile_sources` takes an optional `timing` and marks its per-source pricing loop as `aggregate`. That is also the more honest split, since it is pricing. Leaving out the `timing=` argument in `main` makes that test fail, which I checked as a mutation. Phase order printed becomes `… reconcile, aggregate, report, total`. The test checks only that the last two are `report, total`, and that the phases sum to the total within 0.02 s. Both still hold.
10. **Memo check cost.** Per call: identity plus length checks on `RATES` and `MODEL_ALIASES` cost about 47 ns; a bare dict hit costs 17 ns; also comparing `tuple(MODEL_ALIASES.items())` costs 117 ns. All are small next to about 1.5 µs per event. Identity plus length was chosen because it covers every way the tests patch rates (`setattr`, or `setitem` adding a key) at the lowest cost.
11. **Non-string models behave as before.** A log line can carry `"model": null` or another JSON type. Today, `_canonical_model(None)` raises `TypeError` from `re.sub`. With the memo, a hashable non-string misses, calls the uncached function, and raises the same `TypeError` before anything is stored. An unhashable value (a list) raises `TypeError` from the dict lookup instead. Either way the error type is the same and nothing is cached. The parsers never produce such values in the fixtures. Do not add coercion: it would change behaviour.
12. **`cctrack.datetime` is replaced by a `MagicMock` in `tests/test_codex.py::test_main_agent_filter_codex_only`**, with `fromisoformat` bound to the real one. `dt.year`, `dt.month` and `dt.day` on the result are real ints, so the f-string path works under that mock. It passed in the prototype.

**Dead ends to avoid**
- Do not use `functools.lru_cache` or `functools.cache` on `_find_rate_row`. It cannot see `RATES` being replaced, which is the leak the brief warns about, and clearing it from tests would mean touching every patching test.
- Do not memoise `get_rates` or `calculate_cost`. Their results depend on the date (intro pricing), the prompt size (long context) and the service tier. The row lookup is the only expensive part.
- Do not memoise `energy_scale()` across calls. `test_energy_scale_env_var` changes the variable between calls. Hoist it per `aggregate` call only.
- Do not compute the global `daily`/`monthly` by summing per-source dailies instead of the final `aggregate(all_events)`. Float addition in a different order changes cost totals in the last digits, so output would no longer be byte-identical.
- Do not slice `date_str[:7]` for the month when the year is below 1000 (finding 4). Mutation M6 checks this.
- Do not remove the hook-side `compute_hook_deltas` and `aggregate_hook_data` calls before reconcile. `install_date` depends on them (finding 6).

## 3. Files

| File | Change |
|---|---|
| `src/cctrack/__init__.py` | Rename the current `_find_rate_row` body to `_find_rate_row_uncached`. Add the memo globals and a new memoised `_find_rate_row`. Add a `scale` keyword to `estimate_energy_wh`. In `aggregate`, read the scale once and build the date keys faster. Add a `timing` keyword to `reconcile_sources`. In `main`, remove the three `aggregate` calls before reconcile and their `timing.phase("aggregate")` marks, and pass `timing=timing` to `reconcile_sources`. |
| `tests/test_pricing_speed.py` | New. The tests in section 6. |
| `CLAUDE.md` | Rate card entry (item 1): one sentence on the memo and how it resets. Aggregation entry (item 5): pricing runs once per source inside `reconcile_sources` and once for the final report, and `main` must not add passes whose results reconcile overwrites. Timing entry (item 7): the `aggregate` phase includes the per-source pricing inside `reconcile_sources`. |

`README.md` does not change: no behaviour a user can see changes (rule 7).

## 4. Interfaces

### 4.1 Rate-row memo (next to `_find_rate_row`)

```python
def _find_rate_row_uncached(model: str) -> dict | None:
    # exactly today's _find_rate_row body: _canonical_model, sorted RATES, _family_matches

_rate_row_memo: dict = {}                 # model -> row dict or None
_rate_row_memo_key: tuple | None = None   # (RATES, len(RATES), MODEL_ALIASES, len(MODEL_ALIASES))

def _find_rate_row(model: str) -> dict | None:
    global _rate_row_memo, _rate_row_memo_key
    key = _rate_row_memo_key
    if (key is None or key[0] is not RATES or key[1] != len(RATES)
            or key[2] is not MODEL_ALIASES or key[3] != len(MODEL_ALIASES)):
        _rate_row_memo = {}
        _rate_row_memo_key = (RATES, len(RATES), MODEL_ALIASES, len(MODEL_ALIASES))
    try:
        return _rate_row_memo[model]
    except KeyError:
        row = _rate_row_memo[model] = _find_rate_row_uncached(model)
        return row
```

- `_find_rate_row` must call `_find_rate_row_uncached` through the module global, as above, so a test can wrap it with `monkeypatch.setattr`.
- The key holds strong references to the `RATES` list and the `MODEL_ALIASES` dict. That is deliberate: a replaced list cannot be freed and have its `id` reused, which a key built from `id()` would allow.
- The memo caches `None` for unknown models.
- The row returned is the `RATES` dict itself, as today, so later reads of its prices and `intro`/`long_context` are live.
- `get_rates`, `is_known_model`, `get_energy_rates` and `rate_card_rows` keep calling `_find_rate_row`. Their signatures do not change.

### 4.2 Energy

```python
def estimate_energy_wh(model: str, input_t: int, output_t: int, cache_read: int,
                       cache_write: int, cache_write_1h: int = 0,
                       scale: float | None = None) -> float:
    e = get_energy_rates(model)
    return (energy_scale() if scale is None else scale) * (
        ... same four terms, same order ...)
```

`print_explore` keeps calling it without `scale`.

### 4.3 `aggregate(events) -> (daily, monthly)`

The signature and return shape are unchanged. Inside:

- Before the loop: `scale = energy_scale()`. Pass `scale=scale` to `estimate_energy_wh`.
- Date keys. Parsing, the `(ValueError, AttributeError)` catch and the `replace`/`astimezone` line stay as they are. Then:
  ```python
  if dt.year >= 1000:
      date_str = f"{dt.year}-{dt.month:02d}-{dt.day:02d}"
      month_str = date_str[:7]
  else:
      date_str = dt.strftime("%Y-%m-%d")
      month_str = dt.strftime("%Y-%m")
  ```
- Leave the rest of the loop alone: `calculate_cost` call, bucket updates, `models` counting.

### 4.4 `reconcile_sources(events, sources, timing=None) -> list[dict]`

`timing` is an optional `_Timing` object, positional-or-keyword, default `None`. Existing callers in `tests/test_accounting.py` pass two arguments. When `timing` is not `None`:
- `timing.phase("aggregate")` runs immediately before the `for src in sources: src["daily"], _ = aggregate(...)` loop;
- `timing.phase("reconcile")` runs immediately after it.

The hook-merge part that follows stays in `reconcile`. Nothing else changes.

### 4.5 `main`

- Remove `timing.phase("aggregate")` and `local_daily, _ = aggregate(local_events)`. The local source dict gets `"daily": {}`.
- In `_add_codex_source`, remove `timing.phase("aggregate")` and `codex_daily, _ = aggregate(events)`. The source dict gets `"daily": {}`. The early return for empty events stays.
- In the remote loop, remove `timing.phase("aggregate")`, `remote_daily, _ = aggregate(remote_events)` and the `timing.phase(rphase)` right after it. The remote source dict gets `"daily": {}`. The other `timing.phase(rphase)` marks stay: the one after `_add_codex_source` and the one at the top of each host.
- `all_events = reconcile_sources(all_events, sources, timing=timing)`.
- The final `timing.phase("aggregate")`, `daily, monthly = aggregate(all_events)` and `timing.phase("report")` stay.

There are no new environment variables or CLI options.

## 5. Behaviour and edge cases

1. **Stdout is byte-identical** in every mode, variant and environment checked (finding 7). The cost per event is computed by the same `calculate_cost` call with the same arguments. Only the number of times it runs changes.
2. **Memo invalidation covers:**
   - replacing `RATES` or `MODEL_ALIASES` with another object (`monkeypatch.setattr`), including an equal copy, which just rebuilds the memo;
   - adding or removing entries in place, since the length changes (`monkeypatch.setitem` with a new key, `RATES.append`).

   It does **not** cover replacing an element in place at the same length (`RATES[3] = row`, or `setitem` on an existing alias key), or edits to `_TIER_WORDS`. No code or test does that. CLAUDE.md says to replace these objects rather than edit them in place. Changing a price inside a row dict needs no invalidation, because rows are returned live.
3. **Unknown models** are memoised as `None`. `get_rates` still picks `OPENAI_FALLBACK_RATES` or `SONNET_RATES` per call via `_is_openai_model`.
4. **Memo size** is bounded by the number of distinct model strings in the logs (dozens). It is not pruned.
5. **Remote hosts** run `_remote_collect`, which never prices, so the memo is never filled there. `_remote_program` still ships the whole module; `test_remote_program_unchanged_without_timing` passed in the prototype.
6. **Timestamps the parser cannot handle** behave exactly as before. A bad string is skipped (`ValueError`/`AttributeError`). A non-string has no `.replace`, so it raises `AttributeError` and is skipped. An offset that pushes the date out of range raises `OverflowError` from `astimezone`. That error is uncaught today and stays uncaught, because the date-key change comes after it.
7. **Phase order with timing** becomes `startup, discovery, claude parse, hook read, [discovery,] codex parse, remote <h>…, reconcile, aggregate, report, total`. The first `aggregate` now appears inside reconcile, and the final one adds to the same line.
8. **Sources keep their shape.** Every source dict still has a `daily` key, `{}`, from creation until `reconcile_sources` fills it. Nothing reads it in between (finding 6).

## 6. Tests

Fixtures: none on disk except the one test in row "Passes removed", which writes one transcript under `tmp_path`. That test copies the setup of `tests/test_accounting.py::test_cli_reconciles_sandbox_and_remote_costs_before_reporting`. All other tests build event dicts inline. The Step 0 harness (`tests/test_cache_equivalence.py`) is used unchanged.

All new tests go in `tests/test_pricing_speed.py` (`import cctrack`, `pytest`, `json`, `datetime`, `timezone`).

| Criterion | Test(s) |
|---|---|
| Brief: all report output unchanged | Existing suite unchanged, in particular `tests/test_cache_equivalence.py` (56-run matrix, timing tests) and `tests/test_accounting.py` (ccusage parity, reconcile). Old-against-new comparison in Verification 2 (not a pytest test, because it needs the pre-step source tree). |
| Brief: full test suite unchanged | `PYTHONPATH=src python3 -m pytest tests -q`: every existing test passes, none is edited. The count rises by the new tests (15 in the prototype). |
| Brief: `aggregate` on the bench corpus ≥5× faster than F9 (2.96 s per 200k events, so ≤0.592 s) | Verification 3. Measured, not a pytest test, because timing tests are flaky. The structural guards are `test_rate_lookup_is_memoised` and `test_aggregate_reads_energy_scale_once`. |
| Brief mutation: memo ignores `RATES` changes | `test_rate_memo_follows_rates_replacement`. (1) `is_known_model("claude-test-9")` is False and `get_rates("claude-test-9") is cctrack.SONNET_RATES`. (2) Inside `with monkeypatch.context() as mp: mp.setattr(cctrack, "RATES", [ROW] + cctrack.RATES)`: `_find_rate_row("claude-test-9") is ROW` and `get_rates(...)["input"] == 3.0`. (3) After the context, unknown again. (4) A second context with a row whose `input` is 7.0 gives 7.0. (5) After it, `get_rates(...) is SONNET_RATES`. `ROW` is a full Anthropic row dict with family `claude-test-9` (`tier "sonnet"`, prices 3.0/15.0/0.3/3.75/6.0). Use `monkeypatch.context()`, not `monkeypatch.undo()`: `undo()` would also undo `conftest.py`'s autouse patches. |
| Added: memo follows `MODEL_ALIASES` | `test_rate_memo_follows_alias_changes`. `claude-test-alias` is unknown. With `mp.setattr(cctrack, "MODEL_ALIASES", {**cctrack.MODEL_ALIASES, "claude-test-alias": "claude-opus-5-5"})`, family is `claude-opus-5-5`. After the context it is unknown again. With `mp.setitem(cctrack.MODEL_ALIASES, "claude-test-alias", "claude-haiku-4-5")` (in place, length grows), family is `claude-haiku-4-5`. After the context it is unknown again. |
| Added: memo is used | `test_rate_lookup_is_memoised`. First `monkeypatch.setattr(cctrack, "RATES", list(cctrack.RATES))`, which forces a fresh memo regardless of test order. Then wrap `cctrack._find_rate_row_uncached` with a recording lambda. Aggregate 999 events cycling over `claude-opus-5-5`, `gpt-5.4` and the unknown `claude-brandnew-9`. Assert the recorded calls are exactly those three models, once each. Without the memo there would be 1,998 calls. |
| Added: energy scale read once, value right | `test_aggregate_reads_energy_scale_once`. Wrap `cctrack.energy_scale` with a counter and `setenv("CCTRACK_ENERGY_SCALE", "2.5")`. Aggregate 10 events, each `claude-opus-5` with 1M output on one day. Assert one call, and `energy_wh == approx(10 × 2000 × 2.5)`. |
| Added: date keys unchanged | `test_aggregate_date_keys_match_strftime`, parametrised over `2026-07-15T10:00:00Z`, `…T10:00:00.123456Z`, `2026-07-15T23:30:00-05:00`, `2026-07-16T02:00:00+05:00`, naive `2026-07-15T10:00:00`, `2026-12-31T23:59:59-01:00` (rolls into the next year), `0999-06-01T12:00:00Z`, `1000-01-01T00:00:00Z` and `9999-12-31T12:00:00Z`. For each, compute `dt` the way `aggregate` does and assert `list(daily) == [dt.strftime("%Y-%m-%d")]` and `list(monthly) == [dt.strftime("%Y-%m")]`. |
| Added: passes removed and must stay removed | `test_main_prices_each_event_once_per_pass[detail]`, parametrised `detail` in `False`/`True`. Set up as in the accounting test named above: one transcript in `tmp_path/sandboxes/local/claude/projects/p/s.jsonl`, `--agent claude --remote air`, and `fetch_remote_jsonl` returning the same row as an rsynced copy, so it dedups to one event. Monkeypatch `default_dirs`, `sandbox_roots`, `discover_hook_data_dirs`, `read_hook_config`, `is_hook_installed` and `parse_remote_hook_data` as in that test. Wrap `cctrack.aggregate` to record `len(events)` per call, run `cctrack.main()` with `--json` when `detail`, and assert `sum(recorded) == (1 if detail else 2)`. Before this step the sums are 3 and 4: the prototype run against the old source recorded `[1, 1, 1, 0]` for `--json`. |
| Added: timing keeps `aggregate` in every mode | Existing `tests/test_cache_equivalence.py::test_timing_lines_cover_the_run` (unchanged). |

### Mutation checks

Run each on a copy of the repo (`src/` and `tests/`), one at a time, with `PYTHONPATH=src python3 -m pytest tests -q`. Every one below was run on the prototype and failed exactly as listed.

| # | Change | Expected failure |
|---|---|---|
| M1 (brief) | In `_find_rate_row`, drop `key[0] is not RATES or key[1] != len(RATES)` from the condition, so it reads `if (key is None or key[2] is not MODEL_ALIASES or key[3] != len(MODEL_ALIASES)):` | `test_rate_memo_follows_rates_replacement` |
| M2 | Drop both `MODEL_ALIASES` checks from that condition | `test_rate_memo_follows_alias_changes` |
| M2b | Drop only `or key[3] != len(MODEL_ALIASES)` | `test_rate_memo_follows_alias_changes` (the `setitem` case) |
| M3 | In `main`, add `aggregate(local_events)` right after `local_events = parse_events(...)` | `test_main_prices_each_event_once_per_pass[False]` and `[True]` |
| M4 | In `aggregate`, drop `scale=scale` from the `estimate_energy_wh` call | `test_aggregate_reads_energy_scale_once` |
| M5 | In `main`, call `reconcile_sources(all_events, sources)` without `timing=timing` | `tests/test_cache_equivalence.py::test_timing_lines_cover_the_run` |
| M6 | In `aggregate`, replace `if dt.year >= 1000:` with `if True:` | `test_aggregate_date_keys_match_strftime[0999-06-01T12:00:00Z]` |
| M7 | Make `_find_rate_row` return `_find_rate_row_uncached(model)` directly (no memo) | `test_rate_lookup_is_memoised` |

(A `{dt.year:04d}` mutation does not fail on Linux for years ≥1000 and is not a useful check. M6 covers the fallback.)

## 7. Verification

`S` is the verifier's scratch directory. `BASE` is the pre-step commit: `HEAD` if the step is not yet committed, otherwise `HEAD~1`.

1. **Suite:**
   ```bash
   cd /home/sandy/dev/cctrack && PYTHONPATH=src python3 -m pytest tests -q
   ```
   All pass, none skipped beyond the existing one, and the count is 379 plus the new tests.
2. **Old against new stdout across the Step 0 matrix.** Extract the old tree and run the script below four times. Each run must print `56 runs compared; differing: []` and exit 0:
   ```bash
   mkdir -p $S/base/src/cctrack && git -C /home/sandy/dev/cctrack show $BASE:src/cctrack/__init__.py > $S/base/src/cctrack/__init__.py
   python3 $S/cross_src.py $S/base/src /home/sandy/dev/cctrack/src
   python3 $S/cross_src.py $S/base/src /home/sandy/dev/cctrack/src CCTRACK_ENERGY_SCALE=3.7
   python3 $S/cross_src.py $S/base/src /home/sandy/dev/cctrack/src --energy
   python3 $S/cross_src.py $S/base/src /home/sandy/dev/cctrack/src --days 3
   ```
   `$S/cross_src.py`:
   ```python
   """usage: python3 cross_src.py OLD_SRC NEW_SRC [ENV=VAL ...] [extra cctrack args]"""
   import sys, tempfile, os, difflib
   from pathlib import Path
   old, new = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
   extra = dict(a.split("=", 1) for a in sys.argv[3:] if "=" in a)
   EXTRA_ARGS = [a for a in sys.argv[3:] if "=" not in a]
   sys.path.insert(0, "/home/sandy/dev/cctrack/tests"); sys.path.insert(0, str(old))
   import test_cache_equivalence as h
   _m = h.modes
   h.modes = lambda c: {k: v + EXTRA_ARGS for k, v in _m(c).items()}
   root = Path(tempfile.mkdtemp(prefix="xsrc-", dir=os.path.dirname(os.path.abspath(__file__))))
   h.SRC = old; corpus = h.build_corpus(root); a = h.run_matrix(corpus, env=extra)
   h.SRC = new; b = h.run_matrix(corpus, env=extra)
   h.SRC = old; a2 = h.run_matrix(corpus, env=extra)
   assert a == a2, h.diff_keys(a, a2)
   d = h.diff_keys(a, b)
   print(f"{len(a)} runs compared; differing: {d}")
   for k in d[:3]:
       print("\n".join(list(difflib.unified_diff(a[k].splitlines(), b[k].splitlines(), lineterm=""))[:30]))
   sys.exit(1 if d else 0)
   ```
3. **The 5× criterion.** Build the bench corpus, then time `aggregate` on 200,000 events with the old and new trees:
   ```bash
   python3 /home/sandy/dev/cctrack/scripts/bench_cache.py --dir $S/bench-default --runs 1 >/dev/null
   SRC=$S/base/src python3 $S/agg_bench.py
   SRC=/home/sandy/dev/cctrack/src python3 $S/agg_bench.py
   ```
   `$S/agg_bench.py`:
   ```python
   import sys, time, glob, os
   sys.path.insert(0, os.environ["SRC"])
   import cctrack
   S = os.path.dirname(os.path.abspath(__file__))
   home = S + "/bench-default/home"
   ev = cctrack.parse_events(glob.glob(home + "/.claude/projects/*/*.jsonl"))
   cev, _ = cctrack.parse_codex_events(sorted(glob.glob(home + "/.codex/sessions/**/*.jsonl", recursive=True)))
   allev = ev + cev; N = 200_000
   big = (allev * (N // len(allev) + 1))[:N]
   def run():
       t = time.perf_counter(); cctrack.aggregate(big); return time.perf_counter() - t
   best = min(run() for _ in range(3))
   print(f"aggregate 200k: {best:.3f} s  ({best / N * 1e6:.2f} us/event)")
   ```
   Pass if the new figure is ≤0.592 s (F9's 2.96 s / 5) **and** at least 5× below the old figure on the same machine. Planner's figures: old 3.02–3.19 s, new 0.27–0.32 s.
4. **Bench run, end to end.** Both trees must print the same `stdout sha256`. `aggregate` + `reconcile` should fall by roughly 8× (planner: 1.11 s to 0.14 s locally, 0.61 s to 0.07 s with `--remote --claude-files 50 --codex-files 10`):
   ```bash
   python3 /home/sandy/dev/cctrack/scripts/bench_cache.py --dir $S/bench-default --runs 2
   python3 /home/sandy/dev/cctrack/scripts/bench_cache.py --dir $S/bench-remote --remote --claude-files 50 --codex-files 10 --runs 1
   ```
   For the old tree's figures, copy `scripts/` next to `$S/base/src`, so `$S/base/scripts/bench_cache.py` uses `REPO=$S/base`, and run the same commands from there.
5. **Live smoke on this sandbox's real logs.** Stdout hashes must match between the old and new trees for each mode. `[timing]` must show `aggregate` after `reconcile`:
   ```bash
   cd /home/sandy/dev/cctrack
   for src in $S/base/src src; do for m in "" "--explore yesterday" "--json" "--energy"; do
     CCTRACK_NO_PROGRESS=1 PYTHONPATH=$src python3 -m cctrack $m </dev/null 2>/dev/null \
       | sed 's/"generated_at": "[^"]*"/GA/' | sha256sum | cut -c1-12; done; echo; done
   CCTRACK_TIMING=1 CCTRACK_NO_PROGRESS=1 PYTHONPATH=src python3 -m cctrack </dev/null 2>&1 >/dev/null | grep timing
   ```
6. **Mutations M1–M7** from section 6, each on a copy of the repo.
7. **Diff hygiene.** `git diff --stat` shows only `src/cctrack/__init__.py`, `tests/test_pricing_speed.py` and `CLAUDE.md`, plus the plan files under `docs/plans/`. No existing test file is changed.

## 8. Deviations

- **The brief says an *existing* rate test must fail when the memo ignores `RATES` changes. None does** (finding 8: the suite passed with that mutation). The plan adds `test_rate_memo_follows_rates_replacement`, which fails under it. No existing test is changed.
- **Two changes beyond memoising, both inside `aggregate`:** reading the energy scale once per call, and an f-string date key with a `strftime` fallback below year 1000. With the memo alone, the 5× criterion fails on some runs (finding 2). Both changes leave results bit-identical (findings 4, 5 and 7).
- **`reconcile_sources` gets an optional `timing` argument**, so that `--explore` and detail runs still show an `aggregate` phase, which the Step 0 timing test requires (finding 9). Because of this, the `aggregate` and `reconcile` lines are split differently from F10: F10's `reconcile` included its own pricing. To compare before and after, compare `aggregate + reconcile`.
- The design in sections 3–5 of `docs/plans/incremental-cache.md` is unchanged. F9's figures still describe the code before this step, and I have not edited them.

## 9. Out of scope

- Any cache code (Steps 3–6).
- Removing the hook-side `compute_hook_deltas`/`aggregate_hook_data` calls before reconcile. They feed `install_date` and are cheap (7.2 s of hook reading in F10 is file reading, not this).
- Pricing each event once and sharing the result between `reconcile_sources` and the final `aggregate`. That would need a per-event cost field and a check that float sums keep their order. With this step, both passes together cost about 3 µs per event.
- Speeding up `print_explore`, `build_session_rows` or `rate_card_rows`. They gain from the memo automatically and are not changed.
- The `cctrack 0.10.0` version string in the `[timing]` header. It comes from stale installed metadata in this sandbox and is not a code issue.
