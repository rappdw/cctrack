#!/usr/bin/env python3
"""Which statusline drop rule tracks the transcripts?

When a session's hook counter drops to a level that isn't near zero, cctrack
0.12.3 restarted counting from the lower level ("rebaseline"); 0.12.4 counts
only rises above the session's earlier high ("high-water"). The first is right
for a session resumed with a lower counter, the second for two processes
writing one session id. Sessions that have transcripts settle it: their
transcript cost is the true figure, so this prices every such session-day both
ways and reports which rule comes closer.

Read-only: reads this machine's Claude Code transcripts (host and every Sandy
sandbox) and statusline hook files. It never rolls up or deletes hook files,
and only reads the parse cache (pass --no-cache to bypass it).

    python3 scripts/hook_rule_check.py
    python3 scripts/hook_rule_check.py --since 2026-10-01 --top 15

Prints only dates, shortened session ids, counts and dollars.
"""

import argparse
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import cctrack  # noqa: E402


def rebaseline_deltas(rows: list[dict], baselines: dict) -> dict:
    """0.12.3's walk: any drop rebaselines, then every climb is booked."""
    out: dict[tuple, float] = defaultdict(float)
    by_session: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_session[r["session_id"]].append(r)
    for sid, rs in by_session.items():
        rs.sort(key=lambda r: r.get("ts") or r["date_str"])
        base = baselines.get(sid)
        prev = float(base["cost_usd"]) if base else None
        for r in rs:
            cur = float(r.get("cost_usd", 0.0))
            if prev is None:
                prev = cur
            elif cur < prev - max(1e-6, abs(prev) * 0.001):
                prev = cur
            else:
                out[(sid, r["date_str"])] += cur - prev
                prev = cur
    return out


def high_water_deltas(rows: list[dict], baselines: dict) -> dict:
    """0.12.4's walk (cctrack.compute_hook_deltas)."""
    out: dict[tuple, float] = defaultdict(float)
    for d in cctrack.compute_hook_deltas(rows, baselines):
        out[(d["session_id"], d["date_str"])] += d["cost_usd"]
    return out


def drop_census(rows: list[dict]) -> dict:
    """Classify every counter drop: to near zero (a restart), or to a non-zero
    level; and for the latter, whether the counter later went back above the
    pre-drop level while readings below it kept arriving (alternation)."""
    by_session: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_session[r["session_id"]].append(r)
    restart = nonzero = alternating = 0
    for rs in by_session.values():
        rs.sort(key=lambda r: r.get("ts") or r["date_str"])
        vals = [float(r.get("cost_usd", 0.0)) for r in rs]
        for i in range(1, len(vals)):
            prev, cur = vals[i - 1], vals[i]
            if not cur < prev - max(1e-6, abs(prev) * 0.001):
                continue
            if cur <= cctrack.HOOK_RESTART_FRACTION * prev:
                restart += 1
                continue
            nonzero += 1
            later = vals[i + 1:i + 41]
            if any(v >= prev for v in later) and any(v < prev - 1e-6 for v in later[1:]):
                alternating += 1
    return {"restart": restart, "nonzero": nonzero, "alternating": alternating}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--since", help="first UTC day to compare (YYYY-MM-DD)")
    ap.add_argument("--until", help="last UTC day to compare (YYYY-MM-DD)")
    ap.add_argument("--min-usd", type=float, default=0.50,
                    help="skip session-days whose transcript cost is below this (default 0.50)")
    ap.add_argument("--include-first-day", action="store_true",
                    help="also compare each session's first hook day (both rules skip the "
                         "first reading, so that day reads low either way)")
    ap.add_argument("--top", type=int, default=10, help="session-days to list (default 10)")
    ap.add_argument("--no-cache", action="store_true", help="don't read the parse cache")
    args = ap.parse_args()

    print("Reading transcripts and hook files on this machine...", file=sys.stderr)
    store = None if args.no_cache else cctrack.open_cache()
    try:
        events = cctrack.parse_events(cctrack.discover_files(cctrack.default_dirs()), cache=store)
    finally:
        if store is not None:
            store.close()

    truth: dict[tuple, float] = defaultdict(float)
    for e in events:
        if e.get("agent", "claude") != "claude":
            continue
        day = cctrack._event_date(e)
        truth[(e.get("session_id", ""), day)] += cctrack.calculate_cost(
            e["model"], e["input"], e["output"], e["cache_read"], e["cache_write"],
            e.get("cache_write_1h", 0), date_str=day, request_input=e.get("_request_input"),
            service_tier=e.get("service_tier"))

    rows: list[dict] = []
    baselines: dict = {}
    for d in cctrack.discover_hook_data_dirs():       # read-only: no rollup here
        rows.extend(cctrack.parse_hook_rows(d))
        for sid, b in cctrack.load_session_baselines(d).items():
            if sid not in baselines or b.get("date", "") < baselines[sid].get("date", ""):
                baselines[sid] = b
    rows = cctrack._drop_copied_hook_rows(rows)

    readings: dict[tuple, int] = defaultdict(int)
    first_day: dict[str, str] = {}
    for r in rows:
        readings[(r["session_id"], r["date_str"])] += 1
        if r["date_str"] < first_day.get(r["session_id"], "9999"):
            first_day[r["session_id"]] = r["date_str"]

    a = rebaseline_deltas(rows, baselines)
    b = high_water_deltas(rows, baselines)

    keys = []
    for (sid, day), t in truth.items():
        if t < args.min_usd or readings.get((sid, day), 0) < 2:
            continue
        if args.since and day < args.since or args.until and day > args.until:
            continue
        if not args.include_first_day and first_day.get(sid) == day:
            continue
        keys.append((sid, day))

    print()
    print("Hook drop rules vs transcripts (this machine, Claude Code only)")
    print("=" * 63)
    if not keys:
        print("No session-days with both a transcript and hook readings in range.")
        return 0

    t_total = sum(truth[k] for k in keys)
    print(f"Session-days compared: {len(keys):,} across "
          f"{len({k[0] for k in keys}):,} sessions, "
          f"{min(k[1] for k in keys)} to {max(k[1] for k in keys)}")
    print(f"Transcript cost (truth): ${t_total:,.2f}")
    print()

    def summary(name, deltas):
        vals = [deltas.get(k, 0.0) for k in keys]
        ratios = [deltas.get(k, 0.0) / truth[k] for k in keys]
        within = sum(1 for r in ratios if 0.9 <= r <= 1.1)
        abs_err = sum(abs(deltas.get(k, 0.0) - truth[k]) for k in keys)
        print(f"  {name:<22} total ${sum(vals):>12,.2f} ({sum(vals) / t_total:6.1%} of truth)   "
              f"median ratio {statistics.median(ratios):5.2f}   within ±10%: "
              f"{within / len(keys):5.1%}   abs error ${abs_err:,.2f}")
        return abs_err

    err_a = summary("0.12.3 rebaseline", a)
    err_b = summary("0.12.4 high-water", b)

    closer_a = closer_b = ties = 0
    for k in keys:
        da, db = abs(a.get(k, 0.0) - truth[k]), abs(b.get(k, 0.0) - truth[k])
        if abs(da - db) < 0.005:
            ties += 1
        elif da < db:
            closer_a += 1
        else:
            closer_b += 1
    print()
    print(f"  Session-days where the rules differ: {closer_a + closer_b:,} "
          f"(rebaseline closer: {closer_a:,}, high-water closer: {closer_b:,}); "
          f"identical: {ties:,}")

    census = drop_census(rows)
    print(f"  Counter drops seen: {census['restart']:,} to near zero (restarts), "
          f"{census['nonzero']:,} to a non-zero level, of which "
          f"{census['alternating']:,} alternated back and forth (two processes)")

    differ = sorted((k for k in keys if abs(a.get(k, 0.0) - b.get(k, 0.0)) >= 0.005),
                    key=lambda k: -abs(a.get(k, 0.0) - b.get(k, 0.0)))
    if differ:
        print()
        print(f"  Largest differences (top {min(args.top, len(differ))} of {len(differ)}):")
        print(f"    {'Day':<10}  {'Session':<9} {'Transcript':>11} {'Rebaseline':>11} "
              f"{'High-water':>11}  Closer")
        for k in differ[:args.top]:
            t, va, vb = truth[k], a.get(k, 0.0), b.get(k, 0.0)
            closer = "rebaseline" if abs(va - t) < abs(vb - t) else "high-water"
            print(f"    {k[1]:<10}  {k[0][:8]:<9} ${t:>10,.2f} ${va:>10,.2f} ${vb:>10,.2f}  {closer}")

    print()
    if abs(err_a - err_b) < 0.01 * t_total:
        verdict = "the two rules are about equally close; the drop rule barely matters here"
    elif err_a < err_b:
        verdict = "the 0.12.3 rebaseline rule tracks the transcripts more closely"
    else:
        verdict = "the 0.12.4 high-water rule tracks the transcripts more closely"
    print(f"  Verdict: {verdict}.")
    print("  (Both rules miss usage the hook never saw, e.g. a status line that wasn't "
          "redrawn, so both can read below the transcripts.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
