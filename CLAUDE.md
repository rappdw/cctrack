# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

cctrack-py is a zero-dependency Python CLI that scans Claude Code and Codex CLI log files and reports token usage and estimated cost. It's a Python port of the Go-based [cctrack dashboard](https://github.com/ksred/cctrack), stripped down to a single command that parses logs and prints a cost report.

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

1. **Rate card** (`RATES`, `_an`/`_oa` row builders, `_family_matches`, `get_rates`, `calculate_cost`, `is_known_model`) - Anthropic and OpenAI prices. **Every model version needs its own row**: `_family_matches` lets a row absorb only the exact id, dated snapshots (`-20250514`, `-2025-08-07`) and non-version suffixes (`-codex`) — never a short-number minor version (`claude-opus-5` does NOT match `claude-opus-5-5`) or a size-tier word (`mini`, `nano`, `pro`, `max`, `luna`, `terra`, `sol`, `astra`). This is deliberate: prefix inheritance silently mispriced opus-4-8 (3×) and opus-5-5 (84%) before. Unknown models are flagged `?` and fall back to Sonnet (Claude) or GPT-5 (OpenAI) rates. `get_rates` takes the usage date (time-limited `intro` pricing) and the request's prompt size (`long_context` tiers: Haiku 5.5 >100K, several OpenAI models >272K). Re-verify against the official pricing pages when adding rows; don't infer a price from a neighbouring model.
1a. **Energy model** (`ENERGY_WH_PER_MTOK`, `estimate_energy_wh`) - Opt-in (`--energy`) order-of-magnitude estimate keyed off each rate entry's `tier`. Deliberately NOT a function of price: both cache-write TTLs use one figure because a 1h write costs more money but the same compute.
2. **File discovery** (`sandy_home`, `_sandy_print_state`, `sandbox_roots`, `default_dirs`, `discover_files`) - Claude transcripts from `~/.claude/projects/` plus each Sandy sandbox's `claude/projects/` (ONLY `projects/` — `claude/` also holds `history.jsonl`, job timelines, tool-audit logs). Sandbox roots come from `sandy --print-state` → `sandboxes[].path` (follows `$SANDY_HOME`), else `$SANDY_HOME/sandboxes/*` (default `~/.sandy`). Per sandy's host-side path contract each root has agent homes `claude/` (→ `~/.claude` in the sandbox) and `codex/` (→ `~/.codex`); everything else under a root is private. **Dot-entries** (`_is_sandbox_name`): skip `.rsync-from.*` (an in-progress `sandy --rsync-from` copy — its hook data was being summed twice) and `*.lock`; any other dot-dir is a REAL sandbox iff it holds `WORKSPACE.json` (a workspace like `~/.dotfiles` → sandbox `.dotfiles-<sha8>`). Sandy's own enumeration, hence `--print-state`, currently omits dot-named sandboxes, so they're always added by scanning `sandboxes/` directly. Tests are isolated from real sandboxes by `tests/conftest.py`.
2a. **Codex** (`codex_homes`, `discover_codex_files`, `parse_codex_lines`, `parse_codex_events`, `fetch_remote_codex`) - Reads `$CODEX_HOME` (default `~/.codex`) and every sandbox's `codex/` (sandy never sets CODEX_HOME), `sessions/` + `archived_sessions/` rollouts, incl. `.jsonl.zst` (stdlib `compression.zstd` on 3.14+, else the `zstd` CLI, else skipped and counted). Lines are `{timestamp, type, payload}`. Prefer `token_usage_record` items (per-response, dedup by `response_id` — forks replay parent history); fall back to legacy `event_msg`/`token_count` only before a file's first record, counting `last_token_usage` only when `total_token_usage.total_tokens` moved (it's re-emitted unchanged). **`input_tokens` includes `cached_input_tokens` and `cache_write_input_tokens`; `output_tokens` includes reasoning** — uncached input = input − cached − cache_write. Model/cwd from the latest `turn_context`; thread id + git branch from `session_meta`. Codex events carry `agent="codex"`.
3. **Remote aggregation** (`remote_sandbox_roots`, `fetch_remote_jsonl`, `parse_remote_events`, `fetch_remote_hook_data`, `parse_remote_hook_data`, `fetch_remote_codex`) - One SSH call per host finds its sandbox roots (same rules as locally, cached), then each fetch cats the host's own dirs plus the matching per-sandbox dirs with file separators and parses locally.
3a. **Hook parsing** (`parse_hook_rows`, `compute_hook_deltas`) - The hook gives **cost only**. Its `input_tokens`/`output_tokens`/`cache_*` fields are context-window SNAPSHOTS (`input_tokens` ≈ `cache_read + cache_write`; `output_tokens` is just the last message's), not cumulative — never use them for token counts. `cost_usd` is a session-lifetime cumulative that is unreliable in two ways handled by `compute_hook_deltas`: (1) it can already be large the first time a session is observed (a long-running/unattended session that only rendered its statusline partway through), so the **first observation is a baseline that is NOT counted** — booking a session's pre-existing cumulative onto its first-seen day is what produced a bogus $3,370 day; (2) it **drops** on resume/compaction (or when parallel processes share one session id), so a drop **rebaselines without counting the new level** — otherwise a day's booked spend can exceed the counter's own peak, which is impossible for real money. Net rule: **book only observed upward increments**; a rolled-up baseline seeds `prev` so already-counted days aren't recounted.
4. **JSONL parsing** (`parse_lines`, `parse_events`, `dedup_events`) - Extracts `assistant` events with token usage. Deduplicates by `requestId` across all of a host's files (last event wins) — resumed sessions (`--resume`/`--continue`) repeat events with the same `requestId` in new files. Events without `requestId` are never deduped.
5. **Aggregation** (`aggregate`) - Buckets events into daily and monthly summaries with token counts and costs.
6. **Output** (`print_report`) - Prints current month-to-date summary with projections, daily breakdown, and previous month summaries. Cost is composed **per source** (one source per machine): hook cost where a source has statusline coverage, accuracy-factor-adjusted JSONL cost otherwise, summed per date. The accuracy factor is measured per source over dates strictly after that source's hook install day; when unmeasurable, `DEFAULT_ACCURACY_FACTOR` is **1.0** — the old 1.25 "logs undercount ~20-25%" was entirely rate-card error and inflated unmeasured days 25%.
6a. **Explore** (`print_explore`, `--explore DATE`) - Drills into one day: dollar decomposition by token class, then by model / project / session / machine. Each JSONL event carries its `project` (from `cwd`), `session_id`, `source` (host) and `git_branch` for attribution. Per-session JSONL estimate is shown against the hook's authoritative per-session cost; a >15% day-total divergence is flagged so an inflated estimate (incomplete logs, unknown model) is distinguishable from real spend.
6b. **Detailed reports** (`run_detail_report`, `build_session_rows`, `build_bucket_rows`, `build_coverage`, `build_document`, `write_csv`; flags `--sessions --bucket --coverage --json --csv --since --until --redact --billing-mode`) - Everything UTC; `--since/--until` are a half-open window and are rejected without one of these views. Session key = (agent, host, sandbox, session id, subagent id): Claude sidechain turns (`isSidechain`, `agentId`, else the `agent-<id>.jsonl` filename) are separate rows linked to the parent sessionId; Codex threads use the record's `thread_id` (a fork's replayed records stay with the parent) and get a parent from `session_meta` (`source…parent_thread_id`, `forked_from_id`, or `session_id` ≠ `id`). Parsers fill per-file stats (`new_parse_stats`: lines, unparseable lines, records, timestamp range, compactions keyed by marker so resume replays don't double count) only when asked. Remote streams carry each file's path after the separator. Hook cost is day-granular, so it's attached only for day-aligned windows. Output is ids/paths/counts/money only; `SCHEMA_VERSION` must be bumped on any breaking field change.

Entry point: `cctrack:main` (registered in `pyproject.toml` as a console script).

## Key Design Decisions

- Zero external dependencies - stdlib only (json, argparse, subprocess, etc.)
- Deduplication is global per host by `requestId` (last wins) - resumed sessions replay history into new files and must not double count. Cross-host `requestId` collisions can't occur (distinct API calls).
- Hook data is authoritative for **cost only**; every token count comes from the JSONL logs. Never mix the two in one figure - the hook's token fields are snapshots and would double count against JSONL cache columns.
- Never let one machine's hook coverage suppress another machine's JSONL cost - compose per source.
- Each coding agent is its own source. Codex sources are `exact`: priced straight from their logs, never replaced by the Claude hook and never scaled by the (Claude-derived) accuracy factor. In `--explore`, hook comparisons use only the Claude slice of the JSONL total.
- Costs are API list prices. For subscription users (ChatGPT plans, Claude subscriptions) they're API-equivalent values, not invoices.
- Cumulative counters from the hook are not monotonic and don't start at zero when first observed. `compute_hook_deltas` books only observed upward increments (first observation and post-drop levels are baselines, never counted), so a day's hook cost can never exceed the counter's peak and a session's pre-observation history is never mis-dated onto its first-seen day. Test fixtures must start sessions at ~$0 to mirror real statusline behavior.
- `session-baselines.json` carries each session's rolled-up cumulative across the 90-day retention boundary so straddling sessions aren't re-counted from zero.
- Build system is Hatch (`hatchling`)
- Requires Python 3.10+
