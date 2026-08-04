#!/usr/bin/env python3
"""Trace where a cctrack --explore project label actually comes from.

`--explore` labels each session by the basename of the `cwd` recorded on its
JSONL events (e.g. a cwd of /workspace/team-inbox shows as "team-inbox"). That
basename can be a container/automation path that doesn't match any sandbox or
directory you recognize. This script shows the FULL cwd, the git branch, the
session id, and the exact log file on disk for a project or session, so you can
see where the events physically are.

Usage:
    python3 scripts/trace_project.py [YYYY-MM-DD] [name_or_prefix ...]

    # everything logged yesterday, grouped by project:
    python3 scripts/trace_project.py 2026-08-03

    # just the mystery project (matches project basename OR session-id prefix):
    python3 scripts/trace_project.py 2026-08-03 team-inbox

    # a whole date range is not supported; omit the date to scan all logs:
    python3 scripts/trace_project.py team-inbox

Scans ~/.claude/projects and ~/.sandy/sandboxes/*/... for *.jsonl. Stdlib only.
"""
import glob
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path


def is_date(tok: str) -> bool:
    try:
        datetime.strptime(tok, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def scan_dirs() -> list[Path]:
    home = Path.home()
    roots = [home / ".claude" / "projects"]
    sandy = home / ".sandy" / "sandboxes"
    if sandy.exists():
        roots.append(sandy)
    files: list[Path] = []
    for root in roots:
        if root.exists():
            files += [Path(p) for p in glob.glob(str(root / "**" / "*.jsonl"), recursive=True)]
    return files


def main() -> int:
    args = sys.argv[1:]
    date_str = None
    if args and is_date(args[0]):
        date_str = args[0]
        args = args[1:]
    filters = args  # project basenames or session-id prefixes

    def matches(project: str, sid: str) -> bool:
        if not filters:
            return True
        return any(project == f or project.endswith(f) or sid.startswith(f) for f in filters)

    # key: (cwd, sessionId, file) -> {count, models, branches, first, last}
    groups: dict[tuple, dict] = defaultdict(
        lambda: {"count": 0, "models": set(), "branches": set(), "first": "", "last": ""})

    files = scan_dirs()
    for f in files:
        try:
            lines = f.read_text(errors="replace").splitlines()
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
            if e.get("type") != "assistant":
                continue
            ts = e.get("timestamp", "")
            if date_str and not ts.startswith(date_str):
                continue
            cwd = e.get("cwd", "")
            project = Path(cwd).name if cwd else "(unknown)"
            sid = e.get("sessionId", "")
            if not matches(project, sid):
                continue
            g = groups[(cwd, sid, str(f))]
            g["count"] += 1
            model = e.get("message", {}).get("model", "")
            if model:
                g["models"].add(model)
            if e.get("gitBranch"):
                g["branches"].add(e["gitBranch"])
            if ts:
                g["first"] = ts if not g["first"] else min(g["first"], ts)
                g["last"] = max(g["last"], ts)

    if not groups:
        where = f" on {date_str}" if date_str else ""
        filt = f" matching {filters}" if filters else ""
        print(f"No assistant events{filt}{where} found in ~/.claude/projects or ~/.sandy/sandboxes.")
        return 1

    # Group the display by project basename.
    by_project: dict[str, list[tuple]] = defaultdict(list)
    for (cwd, sid, f), g in groups.items():
        by_project[Path(cwd).name if cwd else "(unknown)"].append((cwd, sid, f, g))

    for project in sorted(by_project):
        rows = by_project[project]
        total = sum(g["count"] for *_ , g in rows)
        print(f"\n=== project '{project}'  ({total} assistant events, "
              f"{len(set(s for _, s, _, _ in rows))} sessions) ===")
        # distinct cwds under this basename (reveals collisions / container paths)
        for cwd in sorted(set(c for c, _, _, _ in rows)):
            print(f"  cwd: {cwd or '(empty)'}")
        for cwd, sid, f, g in sorted(rows, key=lambda r: -r[3]["count"]):
            branch = ",".join(sorted(g["branches"])) or "-"
            models = ",".join(sorted(m.replace("claude-", "") for m in g["models"])) or "-"
            print(f"    session {sid[:12]}  {g['count']:>4} ev  branch={branch}  model={models}")
            print(f"      {g['first'][:19]} … {g['last'][:19]}")
            print(f"      file: {f}")
    print()
    print("The `cwd` line is the working directory Claude Code recorded — that is")
    print("the real source of the project label. If it's a container/automation")
    print("path (e.g. /workspace/...), the run happened inside that environment,")
    print("not a host directory or sandbox of that name.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
