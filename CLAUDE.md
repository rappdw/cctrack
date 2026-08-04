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

1. **Rate card** (`RATES`, `get_rates`, `calculate_cost`, `is_known_model`) - Model-to-price mapping using prefix matching. Order matters: longer/more-specific prefixes must come first (the legacy `claude-opus-4` 15/75 entry must stay below every `claude-opus-4-x`). Unknown models fall back to Sonnet rates and are flagged with `?` in the report. `get_rates`/`calculate_cost` take the usage date so time-limited `intro` pricing (e.g. Sonnet 5 through 2026-08-31) applies to past usage correctly.
1a. **Energy model** (`ENERGY_WH_PER_MTOK`, `estimate_energy_wh`) - Opt-in (`--energy`) order-of-magnitude estimate keyed off each rate entry's `tier`. Deliberately NOT a function of price: both cache-write TTLs use one figure because a 1h write costs more money but the same compute.
2. **File discovery** (`default_dirs`, `discover_files`) - Walks `~/.claude/projects/` and `~/.sandy/sandboxes/` for `.jsonl` files.
3. **Remote aggregation** (`fetch_remote_jsonl`, `parse_remote_events`, `fetch_remote_hook_data`, `parse_remote_hook_data`) - SSHs to hosts, cats all JSONL logs plus statusline hook data (statusline files, monthly summaries, session baselines) with file separators, parses locally.
3a. **Hook parsing** (`parse_hook_rows`, `compute_hook_deltas`) - The hook gives **cost only**. Its `input_tokens`/`output_tokens`/`cache_*` fields are context-window SNAPSHOTS (`input_tokens` ≈ `cache_read + cache_write`; `output_tokens` is just the last message's), not cumulative — never use them for token counts. `cost_usd` is a session-lifetime cumulative that is unreliable in two ways handled by `compute_hook_deltas`: (1) it can already be large the first time a session is observed (a long-running/unattended session that only rendered its statusline partway through), so the **first observation is a baseline that is NOT counted** — booking a session's pre-existing cumulative onto its first-seen day is what produced a bogus $3,370 day; (2) it **drops** on resume/compaction (or when parallel processes share one session id), so a drop **rebaselines without counting the new level** — otherwise a day's booked spend can exceed the counter's own peak, which is impossible for real money. Net rule: **book only observed upward increments**; a rolled-up baseline seeds `prev` so already-counted days aren't recounted.
4. **JSONL parsing** (`parse_lines`, `parse_events`, `dedup_events`) - Extracts `assistant` events with token usage. Deduplicates by `requestId` across all of a host's files (last event wins) — resumed sessions (`--resume`/`--continue`) repeat events with the same `requestId` in new files. Events without `requestId` are never deduped.
5. **Aggregation** (`aggregate`) - Buckets events into daily and monthly summaries with token counts and costs.
6. **Output** (`print_report`) - Prints current month-to-date summary with projections, daily breakdown, and previous month summaries. Cost is composed **per source** (one source per machine): hook cost where a source has statusline coverage, accuracy-factor-adjusted JSONL cost otherwise, summed per date. The accuracy factor is measured per source over dates strictly after that source's hook install day.
6a. **Explore** (`print_explore`, `--explore DATE`) - Drills into one day: dollar decomposition by token class, then by model / project / session / machine. Each JSONL event carries its `project` (from `cwd`), `session_id`, `source` (host) and `git_branch` for attribution. Per-session JSONL estimate is shown against the hook's authoritative per-session cost; a >15% day-total divergence is flagged so an inflated estimate (incomplete logs, unknown model) is distinguishable from real spend.

Entry point: `cctrack:main` (registered in `pyproject.toml` as a console script).

## Key Design Decisions

- Zero external dependencies - stdlib only (json, argparse, subprocess, etc.)
- Deduplication is global per host by `requestId` (last wins) - resumed sessions replay history into new files and must not double count. Cross-host `requestId` collisions can't occur (distinct API calls).
- Hook data is authoritative for **cost only**; every token count comes from the JSONL logs. Never mix the two in one figure - the hook's token fields are snapshots and would double count against JSONL cache columns.
- Never let one machine's hook coverage suppress another machine's JSONL cost - compose per source.
- Cumulative counters from the hook are not monotonic and don't start at zero when first observed. `compute_hook_deltas` books only observed upward increments (first observation and post-drop levels are baselines, never counted), so a day's hook cost can never exceed the counter's peak and a session's pre-observation history is never mis-dated onto its first-seen day. Test fixtures must start sessions at ~$0 to mirror real statusline behavior.
- `session-baselines.json` carries each session's rolled-up cumulative across the 90-day retention boundary so straddling sessions aren't re-counted from zero.
- Build system is Hatch (`hatchling`)
- Requires Python 3.10+
