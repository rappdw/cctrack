# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

cctrack-py is a zero-dependency Python CLI that scans Claude Code JSONL log files and reports token usage and estimated cost. It's a Python port of the Go-based [cctrack dashboard](https://github.com/ksred/cctrack), stripped down to a single command that parses logs and prints a cost report.

## Commands

```bash
# Install in development mode
uv pip install -e .

# Run the tool
python -m cctrack
# or after install:
cctrack

# Run all tests
python -m pytest tests/

# Run a single test
python -m pytest tests/test_cctrack.py::test_name

# Build distribution
uv build
```

## Architecture

This is a single-module CLI tool. All logic lives in `src/cctrack/__init__.py`:

1. **Rate card** (`RATES`, `get_rates`, `calculate_cost`, `is_known_model`) - Model-to-price mapping using prefix matching. Order matters: longer/more-specific prefixes must come first (the legacy `claude-opus-4` 15/75 entry must stay below every `claude-opus-4-x`). Unknown models fall back to Sonnet rates and are flagged with `?` in the report.
2. **File discovery** (`default_dirs`, `discover_files`) - Walks `~/.claude/projects/` and `~/.sandy/sandboxes/` for `.jsonl` files.
3. **Remote aggregation** (`fetch_remote_jsonl`, `parse_remote_events`, `fetch_remote_hook_data`, `parse_remote_hook_data`) - SSHs to hosts, cats all JSONL logs plus statusline hook data (statusline files, monthly summaries, session baselines) with file separators, parses locally.
4. **JSONL parsing** (`parse_lines`, `parse_events`, `dedup_events`) - Extracts `assistant` events with token usage. Deduplicates by `requestId` across all of a host's files (last event wins) — resumed sessions (`--resume`/`--continue`) repeat events with the same `requestId` in new files. Events without `requestId` are never deduped.
5. **Aggregation** (`aggregate`) - Buckets events into daily and monthly summaries with token counts and costs.
6. **Output** (`print_report`) - Prints current month-to-date summary with projections, daily breakdown, and previous month summaries. Cost is composed **per source** (one source per machine): hook cost where a source has statusline coverage, accuracy-factor-adjusted JSONL cost otherwise, summed per date. The accuracy factor is measured per source over dates strictly after that source's hook install day.

Entry point: `cctrack:main` (registered in `pyproject.toml` as a console script).

## Key Design Decisions

- Zero external dependencies - stdlib only (json, argparse, subprocess, etc.)
- Deduplication is global per host by `requestId` (last wins) - resumed sessions replay history into new files and must not double count. Cross-host `requestId` collisions can't occur (distinct API calls).
- Hook data is authoritative for cost where present; JSONL-derived cost is an estimate. Never let one machine's hook coverage suppress another machine's JSONL cost - compose per source.
- `session-baselines.json` carries each session's rolled-up cumulative across the 90-day retention boundary so straddling sessions aren't re-counted from zero.
- Build system is Hatch (`hatchling`)
- Requires Python 3.10+
