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
# cache_write is the 5-minute TTL rate (1.25× input); cache_write_1h is the
# 1-hour TTL rate (2× input). Claude Code hits the 1h tier heavily in practice.
# "claude-opus-4" (legacy Opus 4.0/4.1 at 15/75) MUST come after every
# claude-opus-4-x entry — a new opus-4-N missing here would silently price 3× high.
# "tier" selects the energy model (see ENERGY_WH_PER_MTOK), not the price.
# "intro" is a time-limited launch price applying to usage on/before "until".
# Source: https://platform.claude.com/docs/en/about-claude/pricing (2026-07-28).
RATES = [
    {"family": "claude-fable-5",    "tier": "frontier", "input": 10.0,  "output": 50.0,  "cache_read": 1.00,  "cache_write": 12.50,  "cache_write_1h": 20.0},
    {"family": "claude-mythos",     "tier": "frontier", "input": 10.0,  "output": 50.0,  "cache_read": 1.00,  "cache_write": 12.50,  "cache_write_1h": 20.0},
    {"family": "claude-opus-5",     "tier": "opus",     "input": 5.0,   "output": 25.0,  "cache_read": 0.50,  "cache_write": 6.25,   "cache_write_1h": 10.0},
    {"family": "claude-opus-4-8",   "tier": "opus",     "input": 5.0,   "output": 25.0,  "cache_read": 0.50,  "cache_write": 6.25,   "cache_write_1h": 10.0},
    {"family": "claude-opus-4-7",   "tier": "opus",     "input": 5.0,   "output": 25.0,  "cache_read": 0.50,  "cache_write": 6.25,   "cache_write_1h": 10.0},
    {"family": "claude-opus-4-6",   "tier": "opus",     "input": 5.0,   "output": 25.0,  "cache_read": 0.50,  "cache_write": 6.25,   "cache_write_1h": 10.0},
    {"family": "claude-opus-4-5",   "tier": "opus",     "input": 5.0,   "output": 25.0,  "cache_read": 0.50,  "cache_write": 6.25,   "cache_write_1h": 10.0},
    {"family": "claude-opus-4",     "tier": "opus",     "input": 15.0,  "output": 75.0,  "cache_read": 1.50,  "cache_write": 18.75,  "cache_write_1h": 30.0},
    {"family": "claude-sonnet-5",   "tier": "sonnet",   "input": 3.0,   "output": 15.0,  "cache_read": 0.30,  "cache_write": 3.75,   "cache_write_1h": 6.0,
     "intro": {"until": "2026-08-31", "input": 2.0, "output": 10.0, "cache_read": 0.20, "cache_write": 2.50, "cache_write_1h": 4.0}},
    {"family": "claude-sonnet-4",   "tier": "sonnet",   "input": 3.0,   "output": 15.0,  "cache_read": 0.30,  "cache_write": 3.75,   "cache_write_1h": 6.0},
    {"family": "claude-sonnet-3",   "tier": "sonnet",   "input": 3.0,   "output": 15.0,  "cache_read": 0.30,  "cache_write": 3.75,   "cache_write_1h": 6.0},
    {"family": "claude-haiku-4",    "tier": "haiku",    "input": 1.0,   "output": 5.0,   "cache_read": 0.10,  "cache_write": 1.25,   "cache_write_1h": 2.0},
    {"family": "claude-haiku-3-5",  "tier": "haiku",    "input": 0.80,  "output": 4.0,   "cache_read": 0.08,  "cache_write": 1.00,   "cache_write_1h": 1.60},
    {"family": "claude-haiku-3",    "tier": "haiku",    "input": 0.25,  "output": 1.25,  "cache_read": 0.03,  "cache_write": 0.30,   "cache_write_1h": 0.50},
]

SONNET_RATES = next(r for r in RATES if r["family"] == "claude-sonnet-4")  # fallback


def get_rates(model: str, date_str: str | None = None) -> dict:
    """Match model name by prefix. Unknown models fall back to Sonnet.
    When `date_str` (YYYY-MM-DD) falls within a family's introductory-pricing
    window, the introductory rates are returned instead of the standard ones."""
    for r in RATES:
        if model.startswith(r["family"]):
            intro = r.get("intro")
            if intro and date_str and date_str <= intro["until"]:
                merged = dict(r)
                merged.update({k: v for k, v in intro.items() if k != "until"})
                return merged
            return r
    return SONNET_RATES


def is_known_model(model: str) -> bool:
    """True if the model matches a rate-card family (i.e. wasn't priced by fallback)."""
    return any(model.startswith(r["family"]) for r in RATES)


def calculate_cost(model: str, input_t: int, output_t: int, cache_read: int,
                   cache_write: int, cache_write_1h: int = 0,
                   date_str: str | None = None) -> float:
    r = get_rates(model, date_str)
    return (
        input_t / 1_000_000 * r["input"]
        + output_t / 1_000_000 * r["output"]
        + cache_read / 1_000_000 * r["cache_read"]
        + cache_write / 1_000_000 * r["cache_write"]
        + cache_write_1h / 1_000_000 * r["cache_write_1h"]
    )


# ── Energy estimation (opt-in, order-of-magnitude) ──────────────────────
#
# Anthropic does not publish per-token energy figures or model sizes, so this
# is an ESTIMATE, not a measurement. The table below is anchored on the
# published full-stack per-prompt numbers the major labs have disclosed
# (Google: 0.24 Wh median Gemini text prompt, Aug 2025; OpenAI: ~0.34 Wh
# average ChatGPT query, Jun 2025 — both include accelerator, host, cooling
# and idle capacity), then apportioned across token classes by their relative
# compute cost:
#   - output tokens dominate: decode is sequential and memory-bandwidth bound
#   - input / cache writes are prefill: parallel, far cheaper per token
#   - cache reads skip prefill entirely (~0.1× input, mirroring the price)
# Sanity check: a 1,000-in / 300-out Sonnet-tier prompt lands at ~0.28 Wh,
# matching the published anchors. Frontier tiers scale up from there.
#
# Both cache-write TTLs use the same figure — a 1-hour TTL costs more money
# but the same compute; only the storage duration differs.
#
# Treat results as ±1 order of magnitude. Scale the whole model with the
# CCTRACK_ENERGY_SCALE environment variable if you have better numbers.
ENERGY_WH_PER_MTOK = {
    "frontier": {"input": 120.0, "output": 2400.0, "cache_read": 12.0, "cache_write": 150.0},
    "opus":     {"input": 100.0, "output": 2000.0, "cache_read": 10.0, "cache_write": 125.0},
    "sonnet":   {"input":  40.0, "output":  800.0, "cache_read":  4.0, "cache_write":  50.0},
    "haiku":    {"input":  12.0, "output":  240.0, "cache_read":  1.2, "cache_write":  15.0},
}

DEFAULT_ENERGY_TIER = "sonnet"

# A typical US household uses ~29 kWh/day (EIA, ~10,500 kWh/yr).
US_HOUSEHOLD_KWH_PER_DAY = 29.0


def energy_scale() -> float:
    """Global multiplier for the energy model, from CCTRACK_ENERGY_SCALE."""
    try:
        return float(os.environ.get("CCTRACK_ENERGY_SCALE", "1"))
    except ValueError:
        return 1.0


def get_energy_rates(model: str) -> dict:
    """Wh per million tokens for a model's compute tier."""
    for r in RATES:
        if model.startswith(r["family"]):
            return ENERGY_WH_PER_MTOK[r["tier"]]
    return ENERGY_WH_PER_MTOK[DEFAULT_ENERGY_TIER]


def estimate_energy_wh(model: str, input_t: int, output_t: int, cache_read: int,
                       cache_write: int, cache_write_1h: int = 0) -> float:
    """Estimated inference energy in watt-hours. See ENERGY_WH_PER_MTOK —
    this is an order-of-magnitude estimate, not a measurement."""
    e = get_energy_rates(model)
    return energy_scale() * (
        input_t / 1_000_000 * e["input"]
        + output_t / 1_000_000 * e["output"]
        + cache_read / 1_000_000 * e["cache_read"]
        + (cache_write + cache_write_1h) / 1_000_000 * e["cache_write"]
    )


def format_energy(wh: float) -> str:
    """Human-readable energy: Wh below 1 kWh, kWh above."""
    if wh < 1000:
        return f"{wh:,.1f} Wh"
    return f"{wh / 1000:,.2f} kWh"


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
    """Fetch and parse JSONL from a remote host via SSH.
    Dedup by request_id across the host's files (resumed sessions)."""
    print(f"  Fetching from {host}...", file=sys.stderr)
    raw = fetch_remote_jsonl(host)
    if not raw:
        return []

    chunks = raw.split("___CCTRACK_FILE_SEP___")
    return dedup_events(parse_lines(chunk.splitlines()) for chunk in chunks)


HOOK_FILE_SEP = "___CCTRACK_HOOK_FILE___"
MONTHLY_FILE_SEP = "___CCTRACK_MONTHLY_FILE___"
BASELINE_FILE_SEP = "___CCTRACK_BASELINE_FILE___"

# Hook data locations on remote hosts: the host's own cctrack dir plus any
# Sandy sandbox equivalents (mirrors discover_hook_data_dirs).
REMOTE_HOOK_DIRS = "~/.claude/cctrack ~/.sandy/sandboxes"


def fetch_remote_hook_data(host: str) -> str:
    """SSH to host and cat all statusline JSONL + monthly summary files.
    Returns raw output with file-type separators."""
    remote_cmd = (
        f"find {REMOTE_HOOK_DIRS} -name 'statusline*.jsonl' "
        f"-exec echo '{HOOK_FILE_SEP}' \\; -exec cat {{}} \\; 2>/dev/null; "
        f"find {REMOTE_HOOK_DIRS} -name 'monthly-*.json' "
        f"-exec echo '{MONTHLY_FILE_SEP}' \\; -exec cat {{}} \\; 2>/dev/null; "
        f"find {REMOTE_HOOK_DIRS} -name 'session-baselines.json' "
        f"-exec echo '{BASELINE_FILE_SEP}' \\; -exec cat {{}} \\; 2>/dev/null; "
        f"true"
    )
    cmd = ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", host, remote_cmd]
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


def parse_remote_hook_data(host: str) -> tuple[list[dict], dict, dict]:
    """Fetch and parse hook data from a remote host.
    Returns (hook_rows, monthly_summaries, session_baselines) in the same
    shapes as parse_hook_rows, load_monthly_hook_summaries, and
    load_session_baselines."""
    raw = fetch_remote_hook_data(host)
    if not raw:
        return [], {}, {}

    # Chunk order matches the find order above: statusline, monthly, baselines.
    main_part, *baseline_chunks = raw.split(BASELINE_FILE_SEP)
    head, _, monthly_raw = main_part.partition(MONTHLY_FILE_SEP)

    lines: list[str] = []
    for chunk in head.split(HOOK_FILE_SEP):
        lines.extend(chunk.splitlines())
    events = _hook_rows_from_lines(lines)

    monthly: dict[str, dict] = {}
    for chunk in monthly_raw.split(MONTHLY_FILE_SEP):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            data = json.loads(chunk)
        except (json.JSONDecodeError, ValueError):
            continue
        for date_str, day_data in data.get("days", {}).items():
            entry = {
                "cost": float(day_data.get("cost", 0.0)),
                "sessions": int(day_data.get("sessions", 0)),
            }
            merge_monthly_summaries(monthly, {date_str: entry})

    baselines: dict[str, dict] = {}
    for chunk in baseline_chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            data = json.loads(chunk)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict):
            for sid, b in data.items():
                # Session ids are unique per host; keep the newer date on clash.
                if sid not in baselines or b.get("date", "") > baselines[sid].get("date", ""):
                    baselines[sid] = b
    return events, monthly, baselines


def merge_monthly_summaries(target: dict, source: dict) -> None:
    """Merge per-date {cost, sessions} summaries from source into target,
    summing where dates overlap (e.g. same day on multiple hosts)."""
    for date_str, data in source.items():
        if date_str in target:
            target[date_str]["cost"] += data["cost"]
            target[date_str]["sessions"] += data["sessions"]
        else:
            target[date_str] = dict(data)


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

        # Prefer nested cache_creation.{ephemeral_5m,ephemeral_1h}_input_tokens
        # — 1h cache writes cost 2× input vs 1.25× for 5m, and Claude Code
        # commonly uses the 1h tier. Fall back to flat cache_creation_input_tokens
        # as all-5m for older logs that didn't emit the split.
        cc = usage.get("cache_creation") or {}
        cache_write = cc.get("ephemeral_5m_input_tokens", 0)
        cache_write_1h = cc.get("ephemeral_1h_input_tokens", 0)
        if not cc:
            cache_write = usage.get("cache_creation_input_tokens", 0)

        if (input_t == 0 and output_t == 0 and cache_read == 0
                and cache_write == 0 and cache_write_1h == 0):
            continue

        req_id = event.get("requestId", "")
        entry = {
            "model": event.get("message", {}).get("model", ""),
            "timestamp": event.get("timestamp", ""),
            "input": input_t,
            "output": output_t,
            "cache_read": cache_read,
            "cache_write": cache_write,
            "cache_write_1h": cache_write_1h,
            "_request_id": req_id,
        }

        if req_id:
            by_request_id[req_id] = entry
        else:
            no_request_id.append(entry)

    return list(by_request_id.values()) + no_request_id


def dedup_events(event_lists) -> list[dict]:
    """Dedup parsed events across files by request_id (last wins).
    A session resumed via --resume/--continue writes a new JSONL file that
    repeats earlier events with the same requestId — without cross-file dedup
    those would be counted twice. Events without a requestId are kept as-is."""
    by_request_id: dict[str, dict] = {}
    no_request_id: list[dict] = []
    for events in event_lists:
        for e in events:
            rid = e.get("_request_id", "")
            if rid:
                by_request_id[rid] = e
            else:
                no_request_id.append(e)
    return list(by_request_id.values()) + no_request_id


def parse_events(files: list[str]) -> list[dict]:
    """Parse all local JSONL files. Dedup by request_id across all files
    (last event wins), so resumed sessions aren't double counted."""
    event_lists = []
    for path in files:
        try:
            with open(path, "r", errors="replace") as f:
                event_lists.append(parse_lines(f.readlines()))
        except (OSError, IOError):
            continue
    return dedup_events(event_lists)


# ── Hook data parsing ──────────────────────────────────────────────────

def _hook_rows_from_lines(lines) -> list[dict]:
    """Parse statusline JSONL lines into per-render rows sorted by timestamp.

    Only `cost_usd` is meaningful here: it is the session's running cumulative
    spend. The token fields the hook records (`input_tokens`, `output_tokens`,
    `cache_read`, `cache_write`) are SNAPSHOTS of the current context window and
    the most recent message — `input_tokens` is the context size (≈ cache_read +
    cache_write) and `output_tokens` is just the last message's output. They rise
    and fall between renders, so they are deliberately NOT carried forward here;
    all token accounting comes from the JSONL logs instead.

    Returns list of {session_id, ts, date_str, cost_usd, model}."""
    rows = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue

        session_id = entry.get("session_id", "")
        ts = entry.get("_ts", "")
        if not session_id or not ts:
            continue

        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue

        rows.append({
            "session_id": session_id,
            "ts": ts,
            "date_str": dt.strftime("%Y-%m-%d"),
            "cost_usd": float(entry.get("cost_usd", 0.0) or 0.0),
            "model": entry.get("model", ""),
        })

    rows.sort(key=lambda r: (r["session_id"], r["ts"]))
    return rows


def parse_hook_rows(cctrack_dir: Path | None = None) -> list[dict]:
    """Parse statusline-*.jsonl files from a local hook data directory into
    per-render rows. Also reads legacy statusline.jsonl for migration."""
    if cctrack_dir is None:
        cctrack_dir = CCTRACK_DIR
    if not cctrack_dir.exists():
        return []

    files = sorted(cctrack_dir.glob("statusline-*.jsonl"))
    legacy = cctrack_dir / "statusline.jsonl"
    if legacy.exists():
        files.append(legacy)

    lines: list[str] = []
    for filepath in files:
        try:
            with open(filepath, "r", errors="replace") as f:
                lines.extend(f.readlines())
        except (OSError, IOError):
            continue

    return _hook_rows_from_lines(lines)


def _cost_increment(cur: float, prev: float) -> float:
    """Increment contributed by a cumulative-cost reading.

    Normally the session's `total_cost_usd` only climbs, so the increment is
    cur - prev. But the counter RESTARTS when a session is resumed or its
    context is compacted (observed: $67.55 -> $4.85 under one session id), and
    a statusline render can momentarily report 0 before cost is populated.
    Clamping those to zero — the previous behaviour — silently discarded every
    dollar spent after the reset. Treat a meaningful drop as a restart, so the
    new reading counts in full.

    The drop must clear both a relative and an absolute floor so float jitter
    on an unchanged value is never mistaken for a restart."""
    if cur < prev - max(1e-6, abs(prev) * 0.001):
        return max(0.0, cur)          # counter restarted: cur is a fresh total
    return max(0.0, cur - prev)


def compute_hook_deltas(rows: list[dict], baselines: dict | None = None) -> list[dict]:
    """Convert per-render cumulative cost readings into per-(session, date)
    spend, walking every row of each session in timestamp order.

    Walking rows (rather than sampling one reading per day) matters: it captures
    spend that happens before a mid-day counter reset, and it is immune to the
    last render of a day reporting a stale or zero total.

    A session's walk starts from its rolled-up baseline (see
    load_session_baselines) so days already folded into a monthly summary are
    not counted twice. Records with zero spend are dropped.

    Only cost is derived from hook data — see _hook_rows_from_lines for why the
    hook's token fields are unusable. Rows lacking `ts` sort by `date_str`,
    so callers may pass one aggregated reading per day."""
    by_session: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_session[r["session_id"]].append(r)

    day_cost: dict[tuple[str, str], float] = defaultdict(float)
    day_model: dict[tuple[str, str], str] = {}

    for sid, session_rows in by_session.items():
        session_rows.sort(key=lambda r: r.get("ts") or r["date_str"])
        prev = float(((baselines or {}).get(sid) or {}).get("cost_usd", 0.0))
        for r in session_rows:
            cur = float(r.get("cost_usd", 0.0))
            key = (sid, r["date_str"])
            day_cost[key] += _cost_increment(cur, prev)
            prev = cur
            if r.get("model"):
                day_model.setdefault(key, r["model"])

    result = []
    for (sid, date_str), cost in day_cost.items():
        if cost <= 0.0:
            continue
        result.append({
            "date_str": date_str,
            "session_id": sid,
            "cost_usd": cost,
            "model": day_model.get((sid, date_str), ""),
        })
    return result


BASELINES_FILENAME = "session-baselines.json"


def load_session_baselines(cctrack_dir: Path | None = None) -> dict:
    """Load per-session cumulative baselines written by rollup_old_hook_files.
    Returns {session_id: {date, cost_usd}}."""
    if cctrack_dir is None:
        cctrack_dir = CCTRACK_DIR
    path = cctrack_dir / BASELINES_FILENAME
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _write_session_baselines(cctrack_dir: Path, baselines: dict) -> None:
    try:
        (cctrack_dir / BASELINES_FILENAME).write_text(json.dumps(baselines, indent=2) + "\n")
    except OSError:
        pass


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
    """Roll up daily JSONL files older than retention_days into monthly summaries,
    then delete them. Uses the same reset-aware per-session walk as
    compute_hook_deltas, so a session spanning multiple days contributes its
    true spend to each day and a restarted cost counter isn't lost."""
    if cctrack_dir is None:
        cctrack_dir = CCTRACK_DIR
    if not cctrack_dir.exists():
        return

    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).strftime("%Y-%m-%d")

    old_paths: list[tuple[str, Path]] = []
    for path in sorted(cctrack_dir.glob("statusline-*.jsonl")):
        date_str = path.stem.replace("statusline-", "")
        if not date_str or date_str >= cutoff:
            continue
        old_paths.append((date_str, path))

    if not old_paths:
        return

    # Parse every row of the expiring files (not one sample per day).
    rows: list[dict] = []
    empty_paths: list[Path] = []
    for date_str, path in old_paths:
        try:
            with open(path, "r", errors="replace") as f:
                file_rows = _hook_rows_from_lines(f.readlines())
        except (OSError, IOError):
            continue
        if not file_rows:
            empty_paths.append(path)
        rows.extend(file_rows)

    # Baselines carry each session's cumulative across rollup passes, so a
    # session with days on both sides of the cutoff isn't re-counted from zero.
    baselines = load_session_baselines(cctrack_dir)

    day_costs: dict[str, float] = defaultdict(float)
    day_session_ids: dict[str, set] = defaultdict(set)
    for d in compute_hook_deltas(rows, baselines):
        day_costs[d["date_str"]] += d["cost_usd"]
        day_session_ids[d["date_str"]].add(d["session_id"])

    # Record each session's final cumulative so the next pass — and any live
    # statusline data for the same session — resumes from the right point.
    by_session: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_session[r["session_id"]].append(r)
    for sid, session_rows in by_session.items():
        session_rows.sort(key=lambda r: r["ts"])
        last = session_rows[-1]
        baselines[sid] = {"date": last["date_str"], "cost_usd": last["cost_usd"]}

    # Prune baselines for sessions long past the cutoff (no live data can
    # reference them anymore), then persist.
    prune_before = (datetime.now(timezone.utc)
                    - timedelta(days=retention_days + 30)).strftime("%Y-%m-%d")
    baselines = {sid: b for sid, b in baselines.items()
                 if b.get("date", "") >= prune_before}
    _write_session_baselines(cctrack_dir, baselines)

    # Write per-day deltas into the appropriate monthly file and delete dailies.
    by_month: dict[str, dict] = {}
    for date_str in sorted(day_costs.keys()):
        month_str = date_str[:7]
        if month_str not in by_month:
            monthly_path = cctrack_dir / f"monthly-{month_str}.json"
            if monthly_path.exists():
                try:
                    by_month[month_str] = json.loads(monthly_path.read_text())
                except (json.JSONDecodeError, OSError):
                    by_month[month_str] = {"month": month_str, "days": {}}
            else:
                by_month[month_str] = {"month": month_str, "days": {}}
            by_month[month_str].setdefault("days", {})
        by_month[month_str]["days"][date_str] = {
            "cost": day_costs[date_str],
            "sessions": len(day_session_ids[date_str]),
        }

    for month_str, data in by_month.items():
        monthly_path = cctrack_dir / f"monthly-{month_str}.json"
        try:
            monthly_path.write_text(json.dumps(data, indent=2) + "\n")
        except OSError:
            pass

    for _, path in old_paths:
        try:
            path.unlink()
        except OSError:
            pass
    for path in empty_paths:
        try:
            path.unlink()
        except OSError:
            pass


def aggregate_hook_data(events: list[dict], monthly_summaries: dict | None = None) -> dict:
    """Aggregate hook cost deltas by date, merging with pre-aggregated monthly
    data. Live events ADD INTO monthly-seeded buckets — a date can legitimately
    have both (e.g. one host rolled it up while another host still has live
    data). Same-host overlap can't double count because rollup deletes the daily
    files it rolls. Returns {date_str: {cost, sessions, rolled_sessions,
    session_ids, models}}.

    Cost is the only quantity taken from hook data; token counts come from the
    JSONL logs (see _hook_rows_from_lines)."""
    def _empty() -> dict:
        return {"cost": 0.0, "sessions": 0, "rolled_sessions": 0,
                "session_ids": set(), "models": {}}

    by_date: dict[str, dict] = {}

    if monthly_summaries:
        for date_str, data in monthly_summaries.items():
            bucket = _empty()
            bucket["cost"] = data["cost"]
            bucket["rolled_sessions"] = data["sessions"]
            by_date[date_str] = bucket

    for e in events:
        bucket = by_date.setdefault(e["date_str"], _empty())
        bucket["cost"] += e["cost_usd"]
        sid = e.get("session_id", "")
        if sid:
            bucket["session_ids"].add(sid)
        model = e.get("model", "")
        if model:
            bucket["models"][model] = bucket["models"].get(model, 0) + 1

    # Per-day session count: distinct live sessions + rolled-up count.
    for bucket in by_date.values():
        bucket["sessions"] = len(bucket["session_ids"]) + bucket["rolled_sessions"]

    return by_date


# ── Aggregation ─────────────────────────────────────────────────────────

def new_bucket() -> dict:
    return {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
            "cache_write_1h": 0, "cost": 0.0, "energy_wh": 0.0, "models": {}}


def aggregate(events: list[dict]) -> tuple[dict, dict]:
    """
    Returns:
        daily: {date_str: {input, output, cache_read, cache_write, cache_write_1h,
                           cost, energy_wh, models}}
        monthly: same shape, keyed by month_str
    Cost is priced with the rates in effect on the event's own date, so
    time-limited introductory pricing is applied correctly for past usage.
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

        cache_1h = e.get("cache_write_1h", 0)
        cost = calculate_cost(e["model"], e["input"], e["output"],
                              e["cache_read"], e["cache_write"], cache_1h,
                              date_str=date_str)
        energy = estimate_energy_wh(e["model"], e["input"], e["output"],
                                    e["cache_read"], e["cache_write"], cache_1h)
        model = e.get("model", "")

        for bucket in (daily[date_str], monthly[month_str]):
            bucket["input"] += e["input"]
            bucket["output"] += e["output"]
            bucket["cache_read"] += e["cache_read"]
            bucket["cache_write"] += e["cache_write"]
            bucket["cache_write_1h"] += cache_1h
            bucket["cost"] += cost
            bucket["energy_wh"] += energy
            if model:
                bucket["models"][model] = bucket["models"].get(model, 0) + 1

    return dict(daily), dict(monthly)


# ── Output ──────────────────────────────────────────────────────────────

def format_tokens(n: int) -> str:
    return f"{n:,}"


def format_model(models: dict) -> str:
    """Format a model counter dict as 'short-name' or 'short-name+N' when
    multiple models appear. Returns empty string for no models.
    Strips the 'claude-' prefix and any trailing YYYYMMDD date suffix."""
    if not models:
        return ""
    ranked = sorted(models.items(), key=lambda kv: (-kv[1], kv[0]))
    primary = ranked[0][0]
    short = primary[len("claude-"):] if primary.startswith("claude-") else primary
    parts = short.split("-")
    if len(parts) > 1 and parts[-1].isdigit() and len(parts[-1]) == 8:
        short = "-".join(parts[:-1])
    if len(ranked) > 1:
        short += f"+{len(ranked) - 1}"
    return short


def total_tokens(b: dict) -> int:
    return (b["input"] + b["output"] + b["cache_read"]
            + b["cache_write"] + b.get("cache_write_1h", 0))


def _compute_accuracy_factor_sources(sources: list[dict]) -> tuple[float, bool]:
    """Compute ratio of hook cost to JSONL cost over (source, date) pairs where
    both exist. Dates strictly AFTER each source's install date only — the
    install day itself has partial hook coverage and would bias the factor low.
    Comparing per source keeps a hook-less machine's JSONL out of the
    denominator. Returns (factor, is_measured); falls back to
    DEFAULT_ACCURACY_FACTOR when no overlapping data exists."""
    jsonl_total = 0.0
    hook_total = 0.0
    for src in sources:
        install = src.get("install_date")
        if not install:
            continue
        for date_str, d in src["daily"].items():
            if date_str <= install:
                continue
            hook_cost = src["hook_daily"].get(date_str, {}).get("cost", 0.0)
            if d["cost"] > 0 and hook_cost > 0:
                jsonl_total += d["cost"]
                hook_total += hook_cost

    if jsonl_total > 0 and hook_total > 0:
        return hook_total / jsonl_total, True
    return DEFAULT_ACCURACY_FACTOR, False


def _compute_accuracy_factor(daily: dict, hook_daily: dict, hook_install_date: str | None) -> tuple[float, bool]:
    """Single-source convenience wrapper around _compute_accuracy_factor_sources."""
    return _compute_accuracy_factor_sources([
        {"daily": daily, "hook_daily": hook_daily, "install_date": hook_install_date}
    ])


def print_report(daily: dict, monthly: dict, days: int | None = None,
                 hook_daily: dict | None = None,
                 hook_install_date: str | None = None,
                 sources: list[dict] | None = None,
                 show_energy: bool = False):
    """Print the report. `sources` is a list of {name, daily, hook_daily,
    install_date} — one per machine — enabling per-source cost composition
    (hook cost where covered, adjusted JSONL where not) so a hook-less machine
    isn't dropped on days another machine has hook data. When omitted, the
    legacy (daily, hook_daily, hook_install_date) args form a single source."""
    if hook_daily is None:
        hook_daily = {}
    if sources is None:
        sources = [{"name": "all", "daily": daily, "hook_daily": hook_daily,
                    "install_date": hook_install_date}]

    if not daily and not monthly and not any(src["hook_daily"] for src in sources):
        print("cctrack — no log data found")
        print()
        print("Looked in:")
        for d in default_dirs():
            print(f"  {d}")
        return

    hooks_exist = any(src.get("install_date") or src["hook_daily"] for src in sources)

    now = datetime.now(timezone.utc)
    current_month = now.strftime("%Y-%m")
    current_month_name = now.strftime("%B %Y")

    all_dates: set[str] = set()
    for src in sources:
        all_dates |= set(src["daily"].keys()) | set(src["hook_daily"].keys())
    all_dates |= set(daily.keys())
    all_months_set: set[str] = set()
    for d in all_dates:
        all_months_set.add(d[:7])
    for m in monthly:
        all_months_set.add(m)

    sorted_days = sorted(all_dates, reverse=True)
    sorted_months = sorted(all_months_set, reverse=True)

    accuracy_factor, factor_is_measured = _compute_accuracy_factor_sources(sources)

    def _source_date_cost(src: dict, date_str: str) -> tuple[float, str | None]:
        """Returns (cost, kind) for one source; kind is 'auth', 'est', or None.
        Hook cost is authoritative for dates after that source's hook install;
        the install day itself takes max(hook, adjusted JSONL) since the hook
        only captured post-install sessions; JSONL-only days are adjusted by
        the accuracy factor whenever any hook data exists anywhere."""
        hook_cost = src["hook_daily"].get(date_str, {}).get("cost", 0.0)
        jsonl_cost = src["daily"].get(date_str, {}).get("cost", 0.0)
        install = src.get("install_date")

        if install and date_str == install:
            adjusted = jsonl_cost * accuracy_factor if jsonl_cost > 0 else 0.0
            cost = max(hook_cost, adjusted)
            return (cost, "est") if cost > 0 else (0.0, None)
        if install and date_str > install and hook_cost > 0:
            return hook_cost, "auth"
        if jsonl_cost > 0:
            if hooks_exist:
                return jsonl_cost * accuracy_factor, "est"
            return jsonl_cost, "auth"
        if hook_cost > 0:
            return hook_cost, "auth"
        return 0.0, None

    def _date_cost(date_str: str) -> tuple[float, bool]:
        """Sum of per-source costs; authoritative only if every contributing
        source component is hook-authoritative."""
        total = 0.0
        kinds = []
        for src in sources:
            cost, kind = _source_date_cost(src, date_str)
            total += cost
            if kind:
                kinds.append(kind)
        is_auth = bool(kinds) and all(k == "auth" for k in kinds)
        return total, is_auth

    def _date_tokens(date_str: str) -> tuple[int, int]:
        """Input/output per date, summed across sources. Always JSONL-derived:
        the hook's token fields are context-window snapshots, not cumulative
        usage (see _hook_rows_from_lines)."""
        ti = to = 0
        for src in sources:
            d = src["daily"].get(date_str)
            if d:
                ti += d["input"]
                to += d["output"]
        return ti, to

    def _date_sessions(date_str: str) -> int:
        return sum(src["hook_daily"].get(date_str, {}).get("sessions", 0) for src in sources)

    def _date_hook_models(date_str: str) -> dict:
        merged: dict = {}
        for src in sources:
            for mname, cnt in src["hook_daily"].get(date_str, {}).get("models", {}).items():
                merged[mname] = merged.get(mname, 0) + cnt
        return merged

    def _date_energy_wh(date_str: str) -> float:
        """Estimated energy per date, summed across sources. JSONL-derived —
        it needs the per-model token split, which only the logs carry."""
        return sum(src["daily"].get(date_str, {}).get("energy_wh", 0.0)
                   for src in sources)

    saw_unknown_model = False

    def _model_str(models: dict) -> str:
        nonlocal saw_unknown_model
        s = format_model(models)
        if any(not is_known_model(mn) for mn in models):
            saw_unknown_model = True
            s += "?"
        return s

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
            input_t = output_t = 0
            for d in current_days_all:
                ti, to = _date_tokens(d)
                input_t += ti
                output_t += to
            print(f"  Input tokens:   {format_tokens(input_t):>15}")
            print(f"  Output tokens:  {format_tokens(output_t):>15}")
            cache_w_total = m['cache_write'] + m.get('cache_write_1h', 0)
            if m['cache_read'] or cache_w_total:
                print(f"  Cache read:     {format_tokens(m['cache_read']):>15}")
                print(f"  Cache write:    {format_tokens(cache_w_total):>15}")
            total = input_t + output_t + m['cache_read'] + cache_w_total
            print(f"  Total tokens:   {format_tokens(total):>15}")
            mtd_model = _model_str(m.get("models", {}))
            if mtd_model:
                print(f"  Model:          {mtd_model:>15}")

        # Distinct sessions for the month: union of live session ids (a session
        # spanning several days counts once) + rolled-up per-day counts.
        mtd_ids: set = set()
        mtd_counted = 0
        for d in current_days_all:
            for src in sources:
                h = src["hook_daily"].get(d)
                if not h:
                    continue
                ids = h.get("session_ids")
                if ids:
                    mtd_ids |= ids
                    mtd_counted += h.get("rolled_sessions", 0)
                else:
                    mtd_counted += h.get("sessions", 0)
        mtd_sessions = len(mtd_ids) + mtd_counted
        if mtd_sessions:
            print(f"  Sessions:       {mtd_sessions:>15}")

        if hooks_exist and est_days > 0:
            factor_source = "measured" if factor_is_measured else "estimated ~20-25%"
            cost_note = f" ({accuracy_factor:.2f}x adjustment, {factor_source})"
            if auth_days > 0:
                cost_note = f" ({auth_days}d authoritative, {est_days}d adjusted {accuracy_factor:.2f}x)"
            print(f"  Total cost:     ${mtd_cost:.2f}{cost_note}")
        elif hooks_exist and auth_days > 0:
            print(f"  Total cost:     ${mtd_cost:.2f} (authoritative)")
        else:
            print(f"  Total cost:     ${mtd_cost:.2f}")

        if days_active > 0:
            avg_daily = mtd_cost / days_active
            projected = avg_daily * 30
            print(f"  Avg/day:        ${avg_daily:.2f}")
            print(f"  Projected/mo:   ${projected:.2f} (based on {days_active}-day avg)")

        if show_energy:
            mtd_wh = sum(_date_energy_wh(d) for d in current_days_all)
            print(f"  Energy (est):   {format_energy(mtd_wh):>15}")
            household_hours = mtd_wh / 1000 / (US_HOUSEHOLD_KWH_PER_DAY / 24)
            if household_hours >= 24:
                equiv = f"{household_hours / 24:,.1f} days"
            else:
                equiv = f"{household_hours:,.1f} hours"
            print(f"  {'':<15} ≈ {equiv} of a typical US household's use")

        print()

    # Daily breakdown (current month first, then previous)
    current_month_days = [d for d in sorted_days if d.startswith(current_month)]
    prev_month_days = [d for d in sorted_days if not d.startswith(current_month)]

    if current_month_days:
        energy_head = f" {'Energy':>10}" if show_energy else ""
        energy_rule = f" {'─'*10}" if show_energy else ""
        print("Daily breakdown:")
        if hooks_exist:
            print(f"  {'Date':<12} {'Input':>11} {'Output':>11} {'Cache R':>11} {'Cache W':>11} {'Sess':>5} {'Model':<12} {'Cost':>9}{energy_head} {'':>2}")
            print(f"  {'─'*12} {'─'*11} {'─'*11} {'─'*11} {'─'*11} {'─'*5} {'─'*12} {'─'*9}{energy_rule} {'─'*2}")
        else:
            print(f"  {'Date':<12} {'Input':>11} {'Output':>11} {'Cache R':>11} {'Cache W':>11} {'Model':<12} {'Cost':>9}{energy_head}")
            print(f"  {'─'*12} {'─'*11} {'─'*11} {'─'*11} {'─'*11} {'─'*12} {'─'*9}{energy_rule}")
        display_days = current_month_days
        if days is not None:
            display_days = display_days[:days]
        for date_str in display_days:
            cost, is_auth = _date_cost(date_str)
            d = daily.get(date_str)
            input_t, output_t = _date_tokens(date_str)
            cache_r = d["cache_read"] if d else 0
            cache_w = (d["cache_write"] + d.get("cache_write_1h", 0)) if d else 0
            models = (d.get("models", {}) if d else {}) or _date_hook_models(date_str)
            model_str = _model_str(models)
            energy_col = f" {format_energy(_date_energy_wh(date_str)):>10}" if show_energy else ""
            if hooks_exist:
                marker = "" if is_auth else "~"
                sessions = _date_sessions(date_str)
                sess_str = str(sessions) if sessions else "-"
                print(f"  {date_str:<12} {format_tokens(input_t):>11} {format_tokens(output_t):>11} {format_tokens(cache_r):>11} {format_tokens(cache_w):>11} {sess_str:>5} {model_str:<12} ${cost:>7.2f}{energy_col} {marker:>2}")
            else:
                print(f"  {date_str:<12} {format_tokens(input_t):>11} {format_tokens(output_t):>11} {format_tokens(cache_r):>11} {format_tokens(cache_w):>11} {model_str:<12} ${cost:>7.2f}{energy_col}")

    if hooks_exist and current_month_days:
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
        prev_energy_head = f" {'Energy':>10}" if show_energy else ""
        prev_energy_rule = f" {'─'*10}" if show_energy else ""
        print("Previous months:")
        print(f"  {'Month':<12} {'Input':>11} {'Output':>11} {'Cache R':>11} {'Cache W':>11} {'Model':<12} {'Cost':>9}{prev_energy_head}")
        print(f"  {'─'*12} {'─'*11} {'─'*11} {'─'*11} {'─'*11} {'─'*12} {'─'*9}{prev_energy_rule}")
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
                if hooks_exist:
                    month_cost *= accuracy_factor
                month_has_est = True

            input_t = m["input"] if m else 0
            output_t = m["output"] if m else 0
            cache_r = m["cache_read"] if m else 0
            cache_w = (m["cache_write"] + m.get("cache_write_1h", 0)) if m else 0
            model_str = _model_str(m.get("models", {})) if m else ""

            cost_str = f"${month_cost:>7.2f}"
            month_energy = f" {format_energy(m.get('energy_wh', 0.0) if m else 0.0):>10}" if show_energy else ""
            if hooks_exist and month_has_est and not month_has_hook:
                cost_str += "  ~"
            print(f"  {month_str + days_str:<12} {format_tokens(input_t):>11} {format_tokens(output_t):>11} {format_tokens(cache_r):>11} {format_tokens(cache_w):>11} {model_str:<12} {cost_str}{month_energy}")

    if saw_unknown_model:
        print()
        print("  ? = unrecognized model — priced at default Sonnet rates; update cctrack's rate card")

    if show_energy:
        print()
        print("  Energy is a rough ESTIMATE, not a measurement — Anthropic does not publish")
        print("  per-token energy figures. Modelled from logged tokens using published")
        print("  per-prompt figures (~0.24-0.34 Wh/query) apportioned across token classes,")
        print("  scaled by model tier. Treat as ±1 order of magnitude. Includes datacenter")
        print("  overhead (cooling, host); excludes training and your own device.")
        scale = energy_scale()
        if scale != 1.0:
            print(f"  CCTRACK_ENERGY_SCALE={scale:g} applied.")

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
        "--energy", action="store_true",
        help="Show estimated inference energy (kWh). Rough estimate — see the "
             "note printed with the report. Scale with CCTRACK_ENERGY_SCALE."
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

    # ── Local source: JSONL events + hook data ──
    dirs = args.dirs if args.dirs else default_dirs()
    files = discover_files(dirs)
    local_events = parse_events(files)
    local_daily, _ = aggregate(local_events)

    local_install = None
    config = read_hook_config()
    if config and "installed_at" in config:
        try:
            dt = datetime.fromisoformat(config["installed_at"].replace("Z", "+00:00"))
            local_install = dt.strftime("%Y-%m-%d")
        except (ValueError, AttributeError):
            pass

    local_hook_events: list[dict] = []
    local_hook_monthly: dict[str, dict] = {}
    for hdir in discover_hook_data_dirs():
        rollup_old_hook_files(hdir)
        baselines = load_session_baselines(hdir)
        local_hook_events.extend(compute_hook_deltas(parse_hook_rows(hdir), baselines))
        merge_monthly_summaries(local_hook_monthly, load_monthly_hook_summaries(hdir))

    local_hook_daily: dict = {}
    if local_hook_events or local_hook_monthly:
        local_hook_daily = aggregate_hook_data(local_hook_events, local_hook_monthly)

    # Hook coverage may start earlier than the configured install date
    # (e.g. data restored from another machine); the earliest observed hook
    # date is the effective install date for this source.
    if local_hook_daily:
        earliest = min(local_hook_daily.keys())
        if local_install is None or earliest < local_install:
            local_install = earliest

    all_events = list(local_events)
    sources = [{"name": "local", "daily": local_daily,
                "hook_daily": local_hook_daily, "install_date": local_install}]

    # ── One source per remote host. Deltas are computed per host so a
    # session's cumulative history never mixes across hosts. ──
    if args.remote:
        for host in args.remote:
            remote_events = parse_remote_events(host)
            all_events.extend(remote_events)
            remote_daily, _ = aggregate(remote_events)

            remote_hook_events, remote_monthly, remote_baselines = parse_remote_hook_data(host)
            remote_deltas = compute_hook_deltas(remote_hook_events, remote_baselines)
            remote_hook_daily: dict = {}
            if remote_deltas or remote_monthly:
                remote_hook_daily = aggregate_hook_data(remote_deltas, remote_monthly)
            remote_install = min(remote_hook_daily.keys()) if remote_hook_daily else None

            sources.append({"name": host, "daily": remote_daily,
                            "hook_daily": remote_hook_daily, "install_date": remote_install})

    # Global aggregates feed the token columns; cost is composed per source.
    daily, monthly = aggregate(all_events)

    # Suggest hook if not installed
    if not is_hook_installed():
        prompt_hook_install()

    print_report(daily, monthly, days=args.days, sources=sources,
                 show_energy=args.energy)


if __name__ == "__main__":
    main()
