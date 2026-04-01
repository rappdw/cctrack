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

1. **Rate card** (`RATES`, `get_rates`, `calculate_cost`) - Model-to-price mapping using prefix matching. Order matters: longer/more-specific prefixes must come first. Unknown models fall back to Sonnet rates.
2. **File discovery** (`default_dirs`, `discover_files`) - Walks `~/.claude/projects/` and `~/.sandy/sandboxes/` for `.jsonl` files.
3. **Remote aggregation** (`fetch_remote_jsonl`, `parse_remote_events`) - SSHs to hosts, cats all JSONL with file separators, parses locally. Each file chunk gets independent dedup.
4. **JSONL parsing** (`parse_lines`, `parse_events`) - Extracts `assistant` events with token usage. Deduplicates by `requestId` per file (last event wins), matching Claude Code's semantics. Events without `requestId` are never deduped.
5. **Aggregation** (`aggregate`) - Buckets events into daily and monthly summaries with token counts and costs.
6. **Output** (`print_report`) - Prints current month-to-date summary with projections, daily breakdown, and previous month summaries.

Entry point: `cctrack:main` (registered in `pyproject.toml` as a console script).

## Key Design Decisions

- Zero external dependencies - stdlib only (json, argparse, subprocess, etc.)
- Deduplication is per-file, not global - same `requestId` in different files produces separate entries
- Build system is Hatch (`hatchling`)
- Requires Python 3.10+
