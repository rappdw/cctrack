#!/usr/bin/env python3
"""Diagnose a cctrack --explore day and show what each session really spent.

The hook's `total_cost_usd` is a session-lifetime cumulative that (a) can be
large the moment cctrack first sees a long-running session, and (b) drops on
resume/compaction. cctrack 0.6.0 mis-attributed both: it booked a session's
whole pre-existing cumulative onto the first day it saw the session, and it
re-added post-drop levels as fresh spend — so a session whose counter never
passed $1,086 could be reported at $2,083, and a day's total could balloon far
above real spend.

This script reads the raw statusline data for a date and, per session, prints:

  * the cost_usd trajectory,
  * BUGGY: what cctrack ≤0.6.0 booked (first level + every post-drop level),
  * FIXED: what cctrack now books — only observed increments, never the
    standing level (== the honest spend cctrack can see on that day).

If FIXED is far below BUGGY, the old report was inflated. FIXED is what the
corrected cctrack reports; any spend from earlier days the hook didn't capture
is genuinely unattributable and isn't booked on this day.

Usage:
    python3 scripts/diagnose_session_cost.py <YYYY-MM-DD> [session_prefix ...]

    # every session that has data that day:
    python3 scripts/diagnose_session_cost.py 2026-08-03

    # just specific ones (any unique prefix of the session id works):
    python3 scripts/diagnose_session_cost.py 2026-08-03 34dfdf14 63fd9dae 7c112e0a

Reads ~/.claude/cctrack and any ~/.sandy/sandboxes/*/claude/cctrack. Stdlib only.
"""
import glob
import json
import sys
from pathlib import Path


def buggy_increment(cur: float, prev: float) -> float:
    """What cctrack <= 0.6.0 did: a meaningful drop counted the new value in
    full (and prev started at 0, so the first reading counted in full too)."""
    if cur < prev - max(1e-6, abs(prev) * 0.001):
        return max(0.0, cur)
    return max(0.0, cur - prev)


def hook_dirs() -> list[Path]:
    dirs = [Path.home() / ".claude" / "cctrack"]
    dirs += [Path(p) for p in glob.glob(
        str(Path.home() / ".sandy" / "sandboxes" / "*" / "claude" / "cctrack"))]
    return [d for d in dirs if d.exists()]


def load_rows(date_str: str) -> dict[str, list[tuple[str, float]]]:
    """Return {session_id: [(ts, cost_usd), ...]} for the given date."""
    rows: dict[str, list[tuple[str, float]]] = {}
    for d in hook_dirs():
        for f in glob.glob(str(d / f"statusline-{date_str}.jsonl")):
            try:
                lines = Path(f).read_text(errors="replace").splitlines()
            except OSError:
                continue
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                sid = e.get("session_id", "")
                ts = e.get("_ts", "")
                if not sid or not ts:
                    continue
                rows.setdefault(sid, []).append((ts, float(e.get("cost_usd", 0) or 0)))
    return rows


def analyze(sid: str, seq: list[tuple[str, float]]) -> dict:
    seq.sort()
    costs = [c for _, c in seq]

    # BUGGY (<= 0.6.0): prev starts at 0; drops re-add the full new level.
    buggy = prev = 0.0
    drops = 0
    for c in costs:
        if c < prev - max(1e-6, abs(prev) * 0.001):
            drops += 1
        buggy += buggy_increment(c, prev)
        prev = c

    # FIXED: first reading is a baseline (not counted); a drop rebaselines
    # (not counted); only genuine upward increments are booked.
    fixed = 0.0
    prev = None
    for c in costs:
        if prev is None:
            prev = c
        elif c < prev - max(1e-6, abs(prev) * 0.001):
            prev = c
        else:
            fixed += c - prev
            prev = c

    return {
        "sid": sid,
        "renders": len(costs),
        "min": min(costs),
        "max": max(costs),
        "first": costs[0],
        "last": costs[-1],
        "drops": drops,
        "buggy": buggy,
        "fixed": fixed,
        "costs": costs,
    }


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    date_str = sys.argv[1]
    prefixes = sys.argv[2:]

    rows = load_rows(date_str)
    if not rows:
        print(f"No statusline data found for {date_str} in:")
        for d in hook_dirs() or ["(no hook directories exist)"]:
            print(f"  {d}")
        return 1

    selected = {sid: seq for sid, seq in rows.items()
                if not prefixes or any(sid.startswith(p) for p in prefixes)}
    if not selected:
        print(f"No sessions matching {prefixes} on {date_str}. "
              f"Sessions present: {', '.join(s[:12] for s in rows)}")
        return 1

    results = [analyze(sid, seq) for sid, seq in selected.items()]
    results.sort(key=lambda r: -r["buggy"])

    grand_buggy = grand_fixed = 0.0
    for r in results:
        grand_buggy += r["buggy"]
        grand_fixed += r["fixed"]
        inflation = (r["buggy"] / r["fixed"]) if r["fixed"] > 0.005 else float("inf")
        tag = "" if inflation < 1.15 else f"  ← inflated {inflation:.0f}×" if inflation != float("inf") else "  ← booked from nothing"
        print(f"\n=== {r['sid'][:12]}  ({r['renders']} renders) ===")
        print(f"  cost_usd   first={r['first']:.2f}  min={r['min']:.2f}  "
              f"max={r['max']:.2f}  last={r['last']:.2f}  drops={r['drops']}")
        print(f"  BUGGY (cctrack ≤0.6.0 booked on this day): ${r['buggy']:.2f}{tag}")
        print(f"  FIXED (real observed spend this day):      ${r['fixed']:.2f}")
        shown = " ".join(f"{c:.1f}" for c in r["costs"][:40])
        print(f"  trajectory: {shown}" + (" …" if len(r["costs"]) > 40 else ""))

    print(f"\n{'─' * 60}")
    print(f"selected sessions — buggy total: ${grand_buggy:.2f}   fixed total: ${grand_fixed:.2f}")
    if grand_fixed > 0.005 and grand_buggy / grand_fixed > 1.15:
        print(f"The old report over-stated these sessions by "
              f"{grand_buggy / grand_fixed:.1f}×. The FIXED column is the honest")
        print("spend cctrack can see for this day. Any earlier-day spend the hook")
        print("never captured is unattributable and is not booked here.")
    else:
        print("Buggy and fixed agree — these sessions were attributed correctly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
