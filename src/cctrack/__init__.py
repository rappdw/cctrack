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
import base64
import json
import os
import stat
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
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

    chunks = raw.split("___CCTRACK_FILE_SEP___")
    all_events = []
    for chunk in chunks:
        events = parse_lines(chunk.splitlines())
        all_events.extend(events)
    return all_events


# ── Hook infrastructure ───────────────────────────────────────────────

HOOK_SCRIPT = """\
#!/usr/bin/env python3
\"\"\"cctrack statusline hook — appends Claude Code session data to JSONL.\"\"\"
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

def main():
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return
        data = json.loads(raw)

        cost_usd = data.get("cost", {}).get("total_cost_usd")
        if cost_usd is None:
            cost_usd = 0.0

        session_id = data.get("session_id", "")
        model = data.get("model", {}).get("id", "")

        ctx = data.get("context_window", {})
        input_tokens = ctx.get("total_input_tokens", 0)
        output_tokens = ctx.get("total_output_tokens", 0)
        usage = ctx.get("current_usage", {})
        cache_read = usage.get("cache_read_input_tokens", 0)
        cache_write = usage.get("cache_creation_input_tokens", 0)

        record = {
            "_ts": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id,
            "cost_usd": cost_usd,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read": cache_read,
            "cache_write": cache_write,
        }

        out_dir = Path.home() / ".claude" / "cctrack"
        out_dir.mkdir(parents=True, exist_ok=True)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        out_path = out_dir / f"statusline-{today}.jsonl"
        with open(out_path, "a") as f:
            f.write(json.dumps(record) + "\\n")
    except Exception:
        pass

if __name__ == "__main__":
    main()
"""

CCTRACK_DIR = Path.home() / ".claude" / "cctrack"
HOOK_SCRIPT_PATH = Path.home() / ".claude" / "hooks" / "cctrack-hook.py"
HOOK_CONFIG_PATH = CCTRACK_DIR / "config.json"
CLAUDE_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
HOOK_RETENTION_DAYS = 90


def cctrack_dir() -> Path:
    """Return the cctrack data directory path (~/.claude/cctrack)."""
    return CCTRACK_DIR


def discover_hook_data_dirs() -> list[Path]:
    """Return all directories that may contain cctrack hook data.
    Includes the host's ~/.claude/cctrack and any Sandy sandbox equivalents
    at ~/.sandy/sandboxes/*/claude/cctrack."""
    dirs = []
    if CCTRACK_DIR.exists():
        dirs.append(CCTRACK_DIR)
    sandy = Path.home() / ".sandy" / "sandboxes"
    if sandy.exists():
        for candidate in sandy.glob("*/claude/cctrack"):
            if candidate.is_dir() and candidate not in dirs:
                dirs.append(candidate)
    return dirs


def _hook_command() -> str:
    """Return the statusLine command string for our hook.
    Uses ~ so the path resolves correctly in both host and sandbox environments."""
    return "python3 ~/.claude/hooks/cctrack-hook.py"


def is_hook_installed() -> bool:
    """Check if the hook is fully installed (config exists + settings.json has our statusLine)."""
    if not HOOK_CONFIG_PATH.exists():
        return False
    if not CLAUDE_SETTINGS_PATH.exists():
        return False
    try:
        settings = json.loads(CLAUDE_SETTINGS_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return False
    status_line = settings.get("statusLine")
    if not isinstance(status_line, dict):
        return False
    return status_line.get("command") == _hook_command()


def read_hook_config() -> dict | None:
    """Read and return the hook config, or None if not installed."""
    if not HOOK_CONFIG_PATH.exists():
        return None
    try:
        return json.loads(HOOK_CONFIG_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def install_hook() -> None:
    """Install the cctrack statusline hook."""
    CCTRACK_DIR.mkdir(parents=True, exist_ok=True)
    HOOK_SCRIPT_PATH.parent.mkdir(parents=True, exist_ok=True)

    HOOK_SCRIPT_PATH.write_text(HOOK_SCRIPT)
    HOOK_SCRIPT_PATH.chmod(HOOK_SCRIPT_PATH.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    CLAUDE_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    settings: dict = {}
    if CLAUDE_SETTINGS_PATH.exists():
        try:
            settings = json.loads(CLAUDE_SETTINGS_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            settings = {}
    settings["statusLine"] = {"type": "command", "command": _hook_command()}
    CLAUDE_SETTINGS_PATH.write_text(json.dumps(settings, indent=2) + "\n")

    config = {
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "version": "0.3.0",
    }
    HOOK_CONFIG_PATH.write_text(json.dumps(config, indent=2) + "\n")

    print(f"cctrack hook installed successfully.")
    print(f"  Hook script: {HOOK_SCRIPT_PATH}")
    print(f"  Data dir:    {CCTRACK_DIR}")
    print(f"  Settings:    {CLAUDE_SETTINGS_PATH}")


def uninstall_hook() -> None:
    """Uninstall the cctrack statusline hook, keeping data."""
    if CLAUDE_SETTINGS_PATH.exists():
        try:
            settings = json.loads(CLAUDE_SETTINGS_PATH.read_text())
            if "statusLine" in settings:
                del settings["statusLine"]
                CLAUDE_SETTINGS_PATH.write_text(json.dumps(settings, indent=2) + "\n")
        except (json.JSONDecodeError, OSError):
            pass

    if HOOK_SCRIPT_PATH.exists():
        HOOK_SCRIPT_PATH.unlink()
    if HOOK_CONFIG_PATH.exists():
        HOOK_CONFIG_PATH.unlink()

    print(f"cctrack hook uninstalled.")
    if CCTRACK_DIR.exists():
        print(f"  Data preserved: {CCTRACK_DIR}")


def remote_install_hook(host: str) -> None:
    """Install the cctrack statusline hook on a remote host via SSH."""
    encoded_script = base64.b64encode(HOOK_SCRIPT.encode()).decode()

    installer = f"""\
import base64, json, os, stat, sys
from datetime import datetime, timezone
from pathlib import Path

hook_script = base64.b64decode("{encoded_script}").decode()
home = Path.home()
cctrack_dir = home / ".claude" / "cctrack"
hooks_dir = home / ".claude" / "hooks"
hook_path = hooks_dir / "cctrack-hook.py"
config_path = cctrack_dir / "config.json"
settings_path = home / ".claude" / "settings.json"
hook_cmd = "python3 ~/.claude/hooks/cctrack-hook.py"

cctrack_dir.mkdir(parents=True, exist_ok=True)
hooks_dir.mkdir(parents=True, exist_ok=True)
hook_path.write_text(hook_script)
hook_path.chmod(hook_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

settings_path.parent.mkdir(parents=True, exist_ok=True)
settings = {{}}
if settings_path.exists():
    try:
        settings = json.loads(settings_path.read_text())
    except Exception:
        settings = {{}}
settings["statusLine"] = {{"type": "command", "command": hook_cmd}}
settings_path.write_text(json.dumps(settings, indent=2) + "\\n")

config = {{"installed_at": datetime.now(timezone.utc).isoformat(), "version": "0.3.0"}}
config_path.write_text(json.dumps(config, indent=2) + "\\n")

print(f"Hook script: {{hook_path}}")
print(f"Data dir:    {{cctrack_dir}}")
print(f"Settings:    {{settings_path}}")
"""

    cmd = ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", host, "python3", "-"]
    try:
        result = subprocess.run(cmd, input=installer, capture_output=True, text=True, timeout=30)
        if result.returncode == 0 and "Hook script:" in result.stdout:
            print(f"cctrack hook installed on {host}.")
            for line in result.stdout.strip().splitlines():
                print(f"  {line}")
        else:
            print(f"Hook install on {host} failed: {result.stderr.strip()}", file=sys.stderr)
    except subprocess.TimeoutExpired:
        print(f"SSH to {host} timed out", file=sys.stderr)
    except FileNotFoundError:
        print("ssh command not found", file=sys.stderr)


def remote_uninstall_hook(host: str) -> None:
    """Uninstall the cctrack statusline hook on a remote host via SSH."""
    uninstaller = """\
import json
from pathlib import Path

home = Path.home()
hook_path = home / ".claude" / "hooks" / "cctrack-hook.py"
cctrack_dir = home / ".claude" / "cctrack"
config_path = cctrack_dir / "config.json"
settings_path = home / ".claude" / "settings.json"

if settings_path.exists():
    try:
        settings = json.loads(settings_path.read_text())
        if "statusLine" in settings:
            del settings["statusLine"]
            settings_path.write_text(json.dumps(settings, indent=2) + "\\n")
    except Exception:
        pass

if hook_path.exists():
    hook_path.unlink()
if config_path.exists():
    config_path.unlink()

print("uninstalled")
if cctrack_dir.exists():
    print(f"Data preserved: {cctrack_dir}")
"""

    cmd = ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", host, "python3", "-"]
    try:
        result = subprocess.run(cmd, input=uninstaller, capture_output=True, text=True, timeout=30)
        if "uninstalled" in result.stdout:
            print(f"cctrack hook uninstalled on {host}.")
            for line in result.stdout.strip().splitlines():
                if line != "uninstalled":
                    print(f"  {line}")
        else:
            print(f"Hook uninstall on {host} failed: {result.stderr.strip()}", file=sys.stderr)
    except subprocess.TimeoutExpired:
        print(f"SSH to {host} timed out", file=sys.stderr)
    except FileNotFoundError:
        print("ssh command not found", file=sys.stderr)


DEFAULT_ACCURACY_FACTOR = 1.25


def prompt_hook_install() -> None:
    """Interactively offer to install the statusline hook."""
    try:
        if not sys.stdin.isatty():
            return
        print(
            "\nThe statusline hook provides more accurate cost tracking.",
            file=sys.stderr,
        )
        answer = input("Install it now? [y/N] ").strip().lower()
        if answer in ("y", "yes"):
            install_hook()
    except (EOFError, KeyboardInterrupt):
        pass


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


# ── Hook data parsing ──────────────────────────────────────────────────

def parse_hook_events(cctrack_dir: Path | None = None) -> list[dict]:
    """Parse statusline-*.jsonl files. Dedup by session_id (last entry by _ts wins).
    Also reads legacy statusline.jsonl for migration.
    Returns list of {date_str, cost_usd, input_tokens, output_tokens, cache_read, cache_write, model, session_id}."""
    if cctrack_dir is None:
        cctrack_dir = CCTRACK_DIR
    if not cctrack_dir.exists():
        return []

    by_session: dict[str, dict] = {}

    files = sorted(cctrack_dir.glob("statusline-*.jsonl"))
    legacy = cctrack_dir / "statusline.jsonl"
    if legacy.exists():
        files.append(legacy)

    for filepath in files:
        try:
            with open(filepath, "r", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue

                    session_id = entry.get("session_id", "")
                    if not session_id:
                        continue

                    ts = entry.get("_ts", "")
                    if not ts:
                        continue

                    if session_id in by_session:
                        existing_ts = by_session[session_id].get("_ts", "")
                        if ts <= existing_ts:
                            continue

                    by_session[session_id] = entry
        except (OSError, IOError):
            continue

    results = []
    for session_id, entry in by_session.items():
        ts = entry.get("_ts", "")
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue

        results.append({
            "date_str": dt.strftime("%Y-%m-%d"),
            "cost_usd": float(entry.get("cost_usd", 0.0)),
            "input_tokens": int(entry.get("input_tokens", 0)),
            "output_tokens": int(entry.get("output_tokens", 0)),
            "cache_read": int(entry.get("cache_read", 0)),
            "cache_write": int(entry.get("cache_write", 0)),
            "model": entry.get("model", ""),
            "session_id": session_id,
        })

    return results


def load_monthly_hook_summaries(cctrack_dir: Path | None = None) -> dict:
    """Load pre-aggregated monthly-YYYY-MM.json summary files.
    Returns {date_str: {cost: float, sessions: int}}."""
    if cctrack_dir is None:
        cctrack_dir = CCTRACK_DIR
    if not cctrack_dir.exists():
        return {}

    result: dict[str, dict] = {}
    for path in sorted(cctrack_dir.glob("monthly-*.json")):
        try:
            data = json.loads(path.read_text())
            for date_str, day_data in data.get("days", {}).items():
                result[date_str] = {
                    "cost": float(day_data.get("cost", 0.0)),
                    "sessions": int(day_data.get("sessions", 0)),
                }
        except (json.JSONDecodeError, OSError):
            continue
    return result


def rollup_old_hook_files(cctrack_dir: Path | None = None,
                          retention_days: int = HOOK_RETENTION_DAYS) -> None:
    """Roll up daily JSONL files older than retention_days into monthly summaries, then delete them."""
    if cctrack_dir is None:
        cctrack_dir = CCTRACK_DIR
    if not cctrack_dir.exists():
        return

    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).strftime("%Y-%m-%d")

    for path in sorted(cctrack_dir.glob("statusline-*.jsonl")):
        date_str = path.stem.replace("statusline-", "")
        if not date_str or date_str >= cutoff:
            continue

        by_session: dict[str, dict] = {}
        try:
            with open(path, "r", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    sid = entry.get("session_id", "")
                    ts = entry.get("_ts", "")
                    if not sid or not ts:
                        continue
                    if sid in by_session and ts <= by_session[sid].get("_ts", ""):
                        continue
                    by_session[sid] = entry
        except (OSError, IOError):
            continue

        if not by_session:
            try:
                path.unlink()
            except OSError:
                pass
            continue

        day_cost = sum(float(e.get("cost_usd", 0.0)) for e in by_session.values())
        day_sessions = len(by_session)

        month_str = date_str[:7]
        monthly_path = cctrack_dir / f"monthly-{month_str}.json"
        monthly_data: dict = {"month": month_str, "days": {}}
        if monthly_path.exists():
            try:
                monthly_data = json.loads(monthly_path.read_text())
            except (json.JSONDecodeError, OSError):
                monthly_data = {"month": month_str, "days": {}}

        monthly_data["days"][date_str] = {"cost": day_cost, "sessions": day_sessions}

        try:
            monthly_path.write_text(json.dumps(monthly_data, indent=2) + "\n")
            path.unlink()
        except OSError:
            pass


def aggregate_hook_data(events: list[dict], monthly_summaries: dict | None = None) -> dict:
    """Aggregate hook events by date, merging with pre-aggregated monthly data.
    Returns {date_str: {cost: float, sessions: int}}."""
    by_date: dict[str, dict] = {}

    if monthly_summaries:
        for date_str, data in monthly_summaries.items():
            by_date[date_str] = {"cost": data["cost"], "sessions": data["sessions"]}

    recent_dates: set[str] = set()
    for e in events:
        date_str = e["date_str"]
        if date_str not in recent_dates:
            by_date[date_str] = {"cost": 0.0, "sessions": 0}
            recent_dates.add(date_str)
        by_date[date_str]["cost"] += e["cost_usd"]
        by_date[date_str]["sessions"] += 1

    return by_date


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


def _compute_accuracy_factor(daily: dict, hook_daily: dict, hook_install_date: str | None) -> tuple[float, bool]:
    """Compute ratio of hook cost to JSONL cost for overlapping dates.
    Returns (factor, is_measured). Falls back to DEFAULT_ACCURACY_FACTOR when
    no overlapping data exists."""
    if not hook_install_date:
        return DEFAULT_ACCURACY_FACTOR, False

    jsonl_total = 0.0
    hook_total = 0.0
    for date_str in daily:
        if date_str >= hook_install_date and date_str in hook_daily:
            jsonl_cost = daily[date_str]["cost"]
            hook_cost = hook_daily[date_str]["cost"]
            if jsonl_cost > 0 and hook_cost > 0:
                jsonl_total += jsonl_cost
                hook_total += hook_cost

    if jsonl_total > 0 and hook_total > 0:
        return hook_total / jsonl_total, True
    return DEFAULT_ACCURACY_FACTOR, False


def print_report(daily: dict, monthly: dict, days: int | None = None,
                 hook_daily: dict | None = None,
                 hook_install_date: str | None = None):
    if not daily and not monthly and not hook_daily:
        print("cctrack — no log data found")
        print()
        print("Looked in:")
        for d in default_dirs():
            print(f"  {d}")
        return

    if hook_daily is None:
        hook_daily = {}

    now = datetime.now(timezone.utc)
    current_month = now.strftime("%Y-%m")
    current_month_name = now.strftime("%B %Y")

    all_dates = set(daily.keys()) | set(hook_daily.keys())
    all_months_set: set[str] = set()
    for d in all_dates:
        all_months_set.add(d[:7])
    for m in monthly:
        all_months_set.add(m)

    sorted_days = sorted(all_dates, reverse=True)
    sorted_months = sorted(all_months_set, reverse=True)

    accuracy_factor, factor_is_measured = _compute_accuracy_factor(daily, hook_daily, hook_install_date)

    def _date_cost(date_str: str) -> tuple[float, bool]:
        """Returns (cost, is_authoritative).
        Uses hook data for dates after hook install.
        On the install day itself, takes max(hook, adjusted JSONL) since
        the hook only captured post-install sessions.
        For pre-hook days, applies accuracy_factor to JSONL cost."""
        hook_cost = hook_daily.get(date_str, {}).get("cost", 0.0)
        jsonl_cost = daily.get(date_str, {}).get("cost", 0.0)

        if hook_install_date and date_str == hook_install_date:
            adjusted = jsonl_cost * accuracy_factor if jsonl_cost > 0 else 0.0
            return max(hook_cost, adjusted), False
        if hook_install_date and date_str > hook_install_date and hook_cost > 0:
            return hook_cost, True
        if jsonl_cost > 0:
            cost = jsonl_cost
            if hook_install_date:
                cost *= accuracy_factor
            return cost, not bool(hook_install_date)
        if hook_cost > 0:
            return hook_cost, True
        return 0.0, False

    # Header
    print("cctrack — Claude Code Cost Report")
    print("══════════════════════════════════")
    print()

    # Current month-to-date
    current_days_all = [d for d in sorted_days if d.startswith(current_month)]
    if current_days_all:
        days_active = len(current_days_all)
        day_of_month = now.day

        mtd_cost = 0.0
        auth_days = 0
        est_days = 0
        for d in current_days_all:
            cost, is_auth = _date_cost(d)
            mtd_cost += cost
            if is_auth:
                auth_days += 1
            else:
                est_days += 1

        m = monthly.get(current_month)

        print(f"{current_month_name} — month to date (day {day_of_month}, {days_active} active)")
        print("───────────────────────────────────────")
        if m:
            print(f"  Input tokens:   {format_tokens(m['input']):>15}")
            print(f"  Output tokens:  {format_tokens(m['output']):>15}")
            if m['cache_read'] or m['cache_write']:
                print(f"  Cache read:     {format_tokens(m['cache_read']):>15}")
                print(f"  Cache write:    {format_tokens(m['cache_write']):>15}")
            print(f"  Total tokens:   {format_tokens(total_tokens(m)):>15}")

        if hook_install_date and est_days > 0:
            factor_source = "measured" if factor_is_measured else "estimated ~20-25%"
            cost_note = f" ({accuracy_factor:.2f}x adjustment, {factor_source})"
            if auth_days > 0:
                cost_note = f" ({auth_days}d authoritative, {est_days}d adjusted {accuracy_factor:.2f}x)"
            print(f"  Total cost:     ${mtd_cost:.2f}{cost_note}")
        elif hook_install_date and auth_days > 0:
            print(f"  Total cost:     ${mtd_cost:.2f} (authoritative)")
        else:
            print(f"  Total cost:     ${mtd_cost:.2f}")

        if days_active > 0:
            avg_daily = mtd_cost / days_active
            projected = avg_daily * 30
            print(f"  Avg/day:        ${avg_daily:.2f}")
            print(f"  Projected/mo:   ${projected:.2f} (based on {days_active}-day avg)")

        print()

    # Daily breakdown (current month first, then previous)
    current_month_days = [d for d in sorted_days if d.startswith(current_month)]
    prev_month_days = [d for d in sorted_days if not d.startswith(current_month)]

    if current_month_days:
        print("Daily breakdown:")
        if hook_install_date:
            print(f"  {'Date':<12} {'Input':>12} {'Output':>12} {'Cache R':>12} {'Cache W':>12} {'Cost':>10} {'':>3}")
            print(f"  {'─'*12} {'─'*12} {'─'*12} {'─'*12} {'─'*12} {'─'*10} {'─'*3}")
        else:
            print(f"  {'Date':<12} {'Input':>12} {'Output':>12} {'Cache R':>12} {'Cache W':>12} {'Cost':>10}")
            print(f"  {'─'*12} {'─'*12} {'─'*12} {'─'*12} {'─'*12} {'─'*10}")
        display_days = current_month_days
        if days is not None:
            display_days = display_days[:days]
        for date_str in display_days:
            cost, is_auth = _date_cost(date_str)
            d = daily.get(date_str)
            input_t = d["input"] if d else 0
            output_t = d["output"] if d else 0
            cache_r = d["cache_read"] if d else 0
            cache_w = d["cache_write"] if d else 0
            if hook_install_date:
                marker = "" if is_auth else "~"
                print(f"  {date_str:<12} {format_tokens(input_t):>12} {format_tokens(output_t):>12} {format_tokens(cache_r):>12} {format_tokens(cache_w):>12} ${cost:>8.2f} {marker:>3}")
            else:
                print(f"  {date_str:<12} {format_tokens(input_t):>12} {format_tokens(output_t):>12} {format_tokens(cache_r):>12} {format_tokens(cache_w):>12} ${cost:>8.2f}")

    if hook_install_date and current_month_days:
        has_estimated = any(not _date_cost(d)[1] for d in current_month_days)
        if has_estimated:
            if factor_is_measured:
                print(f"  ~ = estimated (JSONL-derived, {accuracy_factor:.1f}x undercount measured from hook data)")
            else:
                print("  ~ = estimated (JSONL-derived, may undercount by ~20-25%)")

    # Previous months summary
    prev_months = [m for m in sorted_months if m != current_month]
    if prev_months:
        print()
        print("Previous months:")
        print(f"  {'Month':<12} {'Input':>12} {'Output':>12} {'Cache R':>12} {'Cache W':>12} {'Cost':>10}")
        print(f"  {'─'*12} {'─'*12} {'─'*12} {'─'*12} {'─'*12} {'─'*10}")
        for month_str in prev_months:
            m = monthly.get(month_str)
            month_days_list = [d for d in prev_month_days if d.startswith(month_str)]
            days_str = f" ({len(month_days_list)}d)" if month_days_list else ""

            month_cost = 0.0
            month_has_hook = False
            month_has_est = False
            for d in month_days_list:
                cost, is_auth = _date_cost(d)
                month_cost += cost
                if is_auth:
                    month_has_hook = True
                else:
                    month_has_est = True

            if not month_days_list and m:
                month_cost = m["cost"]
                if hook_install_date:
                    month_cost *= accuracy_factor
                month_has_est = True

            input_t = m["input"] if m else 0
            output_t = m["output"] if m else 0
            cache_r = m["cache_read"] if m else 0
            cache_w = m["cache_write"] if m else 0

            cost_str = f"${month_cost:>8.2f}"
            if hook_install_date and month_has_est and not month_has_hook:
                cost_str += "  ~"
            print(f"  {month_str + days_str:<12} {format_tokens(input_t):>12} {format_tokens(output_t):>12} {format_tokens(cache_r):>12} {format_tokens(cache_w):>12} {cost_str}")

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
    parser.add_argument(
        "--install-hook", action="store_true",
        help="Install the cctrack statusline hook for Claude Code"
    )
    parser.add_argument(
        "--uninstall-hook", action="store_true",
        help="Uninstall the cctrack statusline hook"
    )
    args = parser.parse_args()

    # Hook management (run and exit)
    if args.install_hook:
        if args.remote:
            for host in args.remote:
                remote_install_hook(host)
        else:
            install_hook()
        return
    if args.uninstall_hook:
        if args.remote:
            for host in args.remote:
                remote_uninstall_hook(host)
        else:
            uninstall_hook()
        return

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

    # Hook data (authoritative cost from statusline hook)
    hook_daily = None
    hook_install_date = None

    config = read_hook_config()
    if config and "installed_at" in config:
        try:
            dt = datetime.fromisoformat(config["installed_at"].replace("Z", "+00:00"))
            hook_install_date = dt.strftime("%Y-%m-%d")
        except (ValueError, AttributeError):
            pass

    hook_dirs = discover_hook_data_dirs()
    if hook_dirs:
        all_hook_events: list[dict] = []
        all_monthly: dict[str, dict] = {}
        for hdir in hook_dirs:
            rollup_old_hook_files(hdir)
            all_hook_events.extend(parse_hook_events(hdir))
            for date_str, data in load_monthly_hook_summaries(hdir).items():
                if date_str in all_monthly:
                    all_monthly[date_str]["cost"] += data["cost"]
                    all_monthly[date_str]["sessions"] += data["sessions"]
                else:
                    all_monthly[date_str] = dict(data)
        if all_hook_events or all_monthly:
            hook_daily = aggregate_hook_data(all_hook_events, all_monthly)

    # Suggest hook if not installed
    if not is_hook_installed():
        prompt_hook_install()

    print_report(daily, monthly, days=args.days,
                 hook_daily=hook_daily, hook_install_date=hook_install_date)


if __name__ == "__main__":
    main()
