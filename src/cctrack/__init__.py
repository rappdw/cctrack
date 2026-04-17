#!/usr/bin/env python3
"""cctrack — Claude Code cost reporter.

Scans JSONL logs from Claude Code and Sandy sandboxes,
tallies token usage, and prints daily + monthly cost summaries.

Usage:
    python cctrack.py                          # local only
    python cctrack.py --days 7                 # last 7 days
    python cctrack.py --remote dgx macbook-air # aggregate with remote hosts
    python cctrack.py --dirs ~/custom          # custom log directory
"""

import argparse
import json
import os
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# ── Rate card (per million tokens) ──────────────────────────────────────

# Longer/more-specific prefixes MUST come first — "claude-opus-4-6" would
# otherwise match "claude-opus-4" and get the wrong rate.
RATES = [
    {"family": "claude-opus-4-7",   "input": 5.0,   "output": 25.0,  "cache_read": 0.50,  "cache_write": 6.25},
    {"family": "claude-opus-4-6",   "input": 5.0,   "output": 25.0,  "cache_read": 0.50,  "cache_write": 6.25},
    {"family": "claude-opus-4-5",   "input": 5.0,   "output": 25.0,  "cache_read": 0.50,  "cache_write": 6.25},
    {"family": "claude-opus-4",     "input": 15.0,  "output": 75.0,  "cache_read": 1.50,  "cache_write": 18.75},
    {"family": "claude-sonnet-4",   "input": 3.0,   "output": 15.0,  "cache_read": 0.30,  "cache_write": 3.75},
    {"family": "claude-sonnet-3",   "input": 3.0,   "output": 15.0,  "cache_read": 0.30,  "cache_write": 3.75},
    {"family": "claude-haiku-4",    "input": 1.0,   "output": 5.0,   "cache_read": 0.10,  "cache_write": 1.25},
    {"family": "claude-haiku-3-5",  "input": 0.80,  "output": 4.0,   "cache_read": 0.08,  "cache_write": 1.00},
    {"family": "claude-haiku-3",    "input": 0.25,  "output": 1.25,  "cache_read": 0.03,  "cache_write": 0.30},
]

SONNET_RATES = RATES[4]  # fallback


def get_rates(model: str) -> dict:
    """Match model name by prefix. Unknown models fall back to Sonnet."""
    for r in RATES:
        if model.startswith(r["family"]):
            return r
    return SONNET_RATES


def calculate_cost(model: str, input_t: int, output_t: int, cache_read: int, cache_write: int) -> float:
    r = get_rates(model)
    return (
        input_t / 1_000_000 * r["input"]
        + output_t / 1_000_000 * r["output"]
        + cache_read / 1_000_000 * r["cache_read"]
        + cache_write / 1_000_000 * r["cache_write"]
    )


# ── File discovery ──────────────────────────────────────────────────────

def default_dirs() -> list[str]:
    home = Path.home()
    dirs = [str(home / ".claude" / "projects")]
    sandy = home / ".sandy" / "sandboxes"
    if sandy.exists():
        dirs.append(str(sandy))
    return dirs


def discover_files(dirs: list[str]) -> list[str]:
    """Walk directories, return all .jsonl file paths."""
    files = []
    for d in dirs:
        p = Path(d)
        if not p.exists():
            continue
        for f in p.rglob("*.jsonl"):
            files.append(str(f))
    return files


# ── Remote host support ────────────────────────────────────────────────

REMOTE_DIRS = "~/.claude/projects ~/.sandy/sandboxes"


def fetch_remote_jsonl(host: str) -> str:
    """SSH to host and cat all JSONL files from default dirs. Returns raw output."""
    # Single SSH call: find all .jsonl files and cat them with a separator
    # so we can split per-file for dedup. We use a marker line between files.
    cmd = [
        "ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", host,
        f"find {REMOTE_DIRS} -name '*.jsonl' -exec echo '___CCTRACK_FILE_SEP___' \\; -exec cat {{}} \\; 2>/dev/null"
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0 and not result.stdout:
            print(f"  Warning: SSH to {host} failed: {result.stderr.strip()}", file=sys.stderr)
            return ""
        return result.stdout
    except subprocess.TimeoutExpired:
        print(f"  Warning: SSH to {host} timed out", file=sys.stderr)
        return ""
    except FileNotFoundError:
        print(f"  Warning: ssh command not found", file=sys.stderr)
        return ""


def parse_remote_events(host: str) -> list[dict]:
    """Fetch and parse JSONL from a remote host via SSH."""
    print(f"  Fetching from {host}...", file=sys.stderr)
    raw = fetch_remote_jsonl(host)
    if not raw:
        return []

    # Split into per-file chunks for correct dedup (per-file, not global)
    chunks = raw.split("___CCTRACK_FILE_SEP___")
    all_events = []
    for chunk in chunks:
        events = parse_lines(chunk.splitlines())
        all_events.extend(events)
    return all_events


# ── JSONL parsing + dedup ───────────────────────────────────────────────

def parse_lines(lines: list[str]) -> list[dict]:
    """Parse JSONL lines from a single file. Dedup by request_id (last wins)."""
    by_request_id: dict[str, dict] = {}
    no_request_id: list[dict] = []

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue

        if event.get("type") != "assistant":
            continue

        usage = event.get("message", {}).get("usage", {})
        input_t = usage.get("input_tokens", 0)
        output_t = usage.get("output_tokens", 0)
        cache_read = usage.get("cache_read_input_tokens", 0)
        cache_write = usage.get("cache_creation_input_tokens", 0)

        if input_t == 0 and output_t == 0 and cache_read == 0 and cache_write == 0:
            continue

        entry = {
            "model": event.get("message", {}).get("model", ""),
            "timestamp": event.get("timestamp", ""),
            "input": input_t,
            "output": output_t,
            "cache_read": cache_read,
            "cache_write": cache_write,
        }

        req_id = event.get("requestId", "")
        if req_id:
            by_request_id[req_id] = entry
        else:
            no_request_id.append(entry)

    return list(by_request_id.values()) + no_request_id


def parse_events(files: list[str]) -> list[dict]:
    """Parse all local JSONL files. Dedup by request_id (last event wins per file)."""
    all_events = []
    for path in files:
        try:
            with open(path, "r", errors="replace") as f:
                events = parse_lines(f.readlines())
                all_events.extend(events)
        except (OSError, IOError):
            continue
    return all_events


# ── Aggregation ─────────────────────────────────────────────────────────

def new_bucket() -> dict:
    return {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "cost": 0.0}


def aggregate(events: list[dict]) -> tuple[dict, dict]:
    """
    Returns:
        daily: {date_str: {input, output, cache_read, cache_write, cost}}
        monthly: {month_str: {input, output, cache_read, cache_write, cost}}
    """
    daily = defaultdict(new_bucket)
    monthly = defaultdict(new_bucket)

    for e in events:
        ts = e["timestamp"]
        if not ts:
            continue

        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue

        date_str = dt.strftime("%Y-%m-%d")
        month_str = dt.strftime("%Y-%m")

        cost = calculate_cost(e["model"], e["input"], e["output"], e["cache_read"], e["cache_write"])

        for bucket in (daily[date_str], monthly[month_str]):
            bucket["input"] += e["input"]
            bucket["output"] += e["output"]
            bucket["cache_read"] += e["cache_read"]
            bucket["cache_write"] += e["cache_write"]
            bucket["cost"] += cost

    return dict(daily), dict(monthly)


# ── Output ──────────────────────────────────────────────────────────────

def format_tokens(n: int) -> str:
    return f"{n:,}"


def total_tokens(b: dict) -> int:
    return b["input"] + b["output"] + b["cache_read"] + b["cache_write"]


def print_report(daily: dict, monthly: dict, days: int | None = None):
    if not daily and not monthly:
        print("cctrack — no log data found")
        print()
        print("Looked in:")
        for d in default_dirs():
            print(f"  {d}")
        return

    now = datetime.now(timezone.utc)
    current_month = now.strftime("%Y-%m")
    current_month_name = now.strftime("%B %Y")

    sorted_days = sorted(daily.keys(), reverse=True)
    sorted_months = sorted(monthly.keys(), reverse=True)

    # Header
    print("cctrack — Claude Code Cost Report")
    print("══════════════════════════════════")
    print()

    # Current month-to-date
    if current_month in monthly:
        m = monthly[current_month]
        current_days = [d for d in sorted_days if d.startswith(current_month)]
        days_active = len(current_days)
        day_of_month = now.day
        print(f"{current_month_name} — month to date (day {day_of_month}, {days_active} active)")
        print("───────────────────────────────────────")
        print(f"  Input tokens:   {format_tokens(m['input']):>15}")
        print(f"  Output tokens:  {format_tokens(m['output']):>15}")
        if m['cache_read'] or m['cache_write']:
            print(f"  Cache read:     {format_tokens(m['cache_read']):>15}")
            print(f"  Cache write:    {format_tokens(m['cache_write']):>15}")
        print(f"  Total tokens:   {format_tokens(total_tokens(m)):>15}")
        print(f"  Total cost:     ${m['cost']:.2f}")
        if days_active > 0:
            avg_daily = m['cost'] / days_active
            projected = avg_daily * 30
            print(f"  Avg/day:        ${avg_daily:.2f}")
            print(f"  Projected/mo:   ${projected:.2f} (based on {days_active}-day avg)")
        print()

    # Daily breakdown (current month first, then previous)
    current_month_days = [d for d in sorted_days if d.startswith(current_month)]
    prev_month_days = [d for d in sorted_days if not d.startswith(current_month)]

    if current_month_days:
        print("Daily breakdown:")
        print(f"  {'Date':<12} {'Input':>12} {'Output':>12} {'Cache R':>12} {'Cache W':>12} {'Cost':>10}")
        print(f"  {'─'*12} {'─'*12} {'─'*12} {'─'*12} {'─'*12} {'─'*10}")
        display_days = current_month_days
        if days is not None:
            display_days = display_days[:days]
        for date_str in display_days:
            d = daily[date_str]
            print(f"  {date_str:<12} {format_tokens(d['input']):>12} {format_tokens(d['output']):>12} {format_tokens(d['cache_read']):>12} {format_tokens(d['cache_write']):>12} ${d['cost']:>8.2f}")

    # Previous months summary
    prev_months = [m for m in sorted_months if m != current_month]
    if prev_months:
        print()
        print("Previous months:")
        print(f"  {'Month':<12} {'Input':>12} {'Output':>12} {'Cache R':>12} {'Cache W':>12} {'Cost':>10}")
        print(f"  {'─'*12} {'─'*12} {'─'*12} {'─'*12} {'─'*12} {'─'*10}")
        for month_str in prev_months:
            m = monthly[month_str]
            month_days = [d for d in prev_month_days if d.startswith(month_str)]
            days_str = f" ({len(month_days)}d)" if month_days else ""
            print(f"  {month_str + days_str:<12} {format_tokens(m['input']):>12} {format_tokens(m['output']):>12} {format_tokens(m['cache_read']):>12} {format_tokens(m['cache_write']):>12} ${m['cost']:>8.2f}")

    print()


# ── CLI ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Claude Code cost reporter — scans JSONL logs and reports token usage + cost"
    )
    parser.add_argument(
        "--days", type=int, default=None,
        help="Limit daily breakdown to last N days"
    )
    parser.add_argument(
        "--dirs", nargs="+", default=None,
        help="Custom log directories to scan (default: ~/.claude/projects and ~/.sandy/sandboxes)"
    )
    parser.add_argument(
        "--remote", nargs="+", default=None, metavar="HOST",
        help="SSH hosts to fetch logs from (e.g. dgx macbook-air user@server)"
    )
    args = parser.parse_args()

    # Local events
    dirs = args.dirs if args.dirs else default_dirs()
    files = discover_files(dirs)
    events = parse_events(files)

    # Remote events
    if args.remote:
        for host in args.remote:
            remote_events = parse_remote_events(host)
            events.extend(remote_events)

    daily, monthly = aggregate(events)
    print_report(daily, monthly, days=args.days)


if __name__ == "__main__":
    main()
