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

1. **Rate card** (`RATES`, `_an`/`_oa` row builders, `_family_matches`, `get_rates`, `calculate_cost`, `is_known_model`) - Anthropic and OpenAI prices. **Every model version needs its own row**: `_family_matches` lets a row absorb only the exact id, dated snapshots (`-20250514`, `-2025-08-07`) and non-version suffixes (`-codex`) — never a short-number minor version (`claude-opus-5` does NOT match `claude-opus-5-5`) or a size-tier word (`mini`, `nano`, `pro`, `max`, `luna`, `terra`, `sol`, `astra`). This is deliberate: prefix inheritance silently mispriced opus-4-8 (3×) and opus-5-5 (84%) before. Unknown models are flagged `?` and fall back to Sonnet (Claude) or GPT-5 (OpenAI) rates. `get_rates` takes the usage date (time-limited `intro` pricing) and the request's prompt size (`long_context` tiers: historical Sonnet 4/4.5 >200K, Haiku 5.5 >100K, several OpenAI models >272K). Re-verify against the official pricing pages when adding rows; don't infer a price from a neighbouring model.
1a. **Energy model** (`ENERGY_WH_PER_MTOK`, `estimate_energy_wh`) - Opt-in (`--energy`) order-of-magnitude estimate keyed off each rate entry's `tier`. Deliberately NOT a function of price: both cache-write TTLs use one figure because a 1h write costs more money but the same compute.
2. **File discovery** (`sandy_home`, `_sandy_print_state`, `sandbox_roots`, `default_dirs`, `discover_files`) - Claude transcripts from `~/.claude/projects/` plus each Sandy sandbox's `claude/projects/` (ONLY `projects/` — `claude/` also holds `history.jsonl`, job timelines, tool-audit logs). Sandbox roots come from `sandy --print-state` → `sandboxes[].path` (follows `$SANDY_HOME`), else `$SANDY_HOME/sandboxes/*` (default `~/.sandy`). Per sandy's host-side path contract each root has agent homes `claude/` (→ `~/.claude` in the sandbox) and `codex/` (→ `~/.codex`); everything else under a root is private. **Dot-entries** (`_is_sandbox_name`): skip `.rsync-from.*` (an in-progress `sandy --rsync-from` copy — its hook data was being summed twice) and `*.lock`; any other dot-dir is a REAL sandbox iff it holds `WORKSPACE.json` (a workspace like `~/.dotfiles` → sandbox `.dotfiles-<sha8>`). Sandy's own enumeration, hence `--print-state`, currently omits dot-named sandboxes, so they're always added by scanning `sandboxes/` directly. Tests are isolated from real sandboxes by `tests/conftest.py`.
2a. **Codex** (`codex_homes`, `discover_codex_files`, `parse_codex_lines`, `parse_codex_events`, `fetch_remote_codex`) - Reads `$CODEX_HOME` (default `~/.codex`) and every sandbox's `codex/` (sandy never sets CODEX_HOME), `sessions/` + `archived_sessions/` rollouts, incl. `.jsonl.zst` (stdlib `compression.zstd` on 3.14+, else the `zstd` CLI, else skipped and counted). Lines are `{timestamp, type, payload}`. Reconcile `token_usage_record` items with their matching `event_msg`/`token_count` snapshots; never discard the rest of a file after its first record. Keep standalone compaction usage, last-only and cumulative-only snapshots; dedup unchanged cumulative re-emissions. Prefer the record's explicit model and recorded service tier. Forked legacy usage is matched against the parent's sequence, preserving history through repeated merges and nested forks; missing parents retain uncertain usage flagged UNKNOWN. **`input_tokens` includes `cached_input_tokens` and `cache_write_input_tokens`; `output_tokens` includes reasoning** — uncached input = input − cached − cache_write. Model/cwd from the latest `turn_context`; thread id + git branch from `session_meta`. Codex events carry `agent="codex"`.
3. **Remote aggregation** (`remote_sandbox_roots`, `fetch_remote_jsonl`, `parse_remote_events`, `fetch_remote_hook_data`, `parse_remote_hook_data`, `fetch_remote_codex`) - One SSH call per host finds its sandbox roots (same rules as locally, cached), then each fetch cats the host's own dirs plus the matching per-sandbox dirs with file separators and parses locally.
3a. **Hook parsing** (`parse_hook_rows`, `compute_hook_deltas`) - The hook gives **cost only**. Its `input_tokens`/`output_tokens`/`cache_*` fields are context-window SNAPSHOTS (`input_tokens` ≈ `cache_read + cache_write`; `output_tokens` is just the last message's), not cumulative — never use them for token counts. `cost_usd` is a session-lifetime cumulative that is unreliable in two ways handled by `compute_hook_deltas`: (1) it can already be large the first time a session is observed (a long-running/unattended session that only rendered its statusline partway through), so the **first observation is a baseline that is NOT counted** — booking a session's pre-existing cumulative onto its first-seen day is what produced a bogus $3,370 day; (2) it **drops** on resume/compaction (or when parallel processes share one session id), so a drop **rebaselines without counting the new level** — otherwise a day's booked spend can exceed the counter's own peak, which is impossible for real money. Net rule: **book only observed upward increments**; a rolled-up baseline seeds `prev` so already-counted days aren't recounted.
3b. **Hook install** (`install_hook`, `uninstall_hook`, `_apply_hook_install`, `_apply_hook_uninstall`, `remote_*_hook`, `HOOK_SCRIPT`) - The hook is Claude Code's `statusLine` command. Install **wraps** any existing status line: it's saved as `wrapped_status_line` in `~/.claude/cctrack/config.json` (other statusLine keys like `padding` are kept), and `HOOK_SCRIPT` records the cost and then runs it via the shell with the same stdin, passing stdout and exit code through. Re-install keeps the saved command and `installed_at` (the start of hook coverage). Uninstall restores the wrapped line only if `statusLine` still points at cctrack. `_apply_hook_*` are self-contained (imports inside) because `remote_*_hook` ship their source with `inspect.getsource` to `ssh host python3 -`. Local and remote share one implementation, and tests run that shipped program for real.
4. **JSONL parsing** (`parse_lines`, `parse_events`, `dedup_events`) - Extracts `assistant` events with token usage. Deduplicates by message + request identity, falling back to Anthropic message id scoped to the session when request id is absent, and then request id when older logs omit message id. Gateway message ids without request ids also include timestamps. Keep the fullest snapshot, prefer parent messages over sidechain replays, and count advisor iterations separately. `reconcile_sources` performs the final cross-host merge and rebuilds source costs so all report modes agree. Preserve copy locations in JSON/CSV.
5. **Aggregation** (`aggregate`) - Buckets events into daily and monthly summaries with token counts and costs.
6. **Output** (`print_report`) - Prints current month-to-date summary with projections, daily breakdown, and previous month summaries. Cost is composed **per source** (one source per machine): hook cost where a source has statusline coverage, accuracy-factor-adjusted JSONL cost otherwise, summed per date. The accuracy factor is measured per source over dates strictly after that source's hook install day; when unmeasurable, `DEFAULT_ACCURACY_FACTOR` is **1.0** — the old 1.25 "logs undercount ~20-25%" was entirely rate-card error and inflated unmeasured days 25%.
6a. **Explore** (`print_explore`, `--explore DATE`) - Drills into one day: dollar decomposition by token class, then by model / project / session / machine. Each JSONL event carries its `project` (from `cwd`), `session_id`, `source` (host) and `git_branch` for attribution. Per-session JSONL estimate is shown against the hook's authoritative per-session cost; a >15% day-total divergence is flagged so an inflated estimate (incomplete logs, unknown model) is distinguishable from real spend.
6b. **Detailed reports** (`run_detail_report`, `build_session_rows`, `build_bucket_rows`, `build_coverage`, `build_document`, `write_csv`; flags `--sessions --bucket --coverage --json --csv --since --until --redact --billing-mode`) - Everything UTC; `--since/--until` are a half-open window and are rejected without one of these views. Session key = (agent, host, sandbox, session id, subagent id): Claude sidechain turns (`isSidechain`, `agentId`, else the `agent-<id>.jsonl` filename) are separate rows linked to the parent sessionId; Codex threads use the record's `thread_id` (a fork's replayed records stay with the parent) and get a parent from `session_meta` (`source…parent_thread_id`, `forked_from_id`, or `session_id` ≠ `id`). Parsers fill per-file stats (`new_parse_stats`: lines, unparseable lines, records, timestamp range, compactions keyed by marker so resume replays don't double count) only when asked. Remote streams carry each file's path after the separator. Hook cost is day-granular, so it's attached only for day-aligned windows. Output is ids/paths/counts/money only; `SCHEMA_VERSION` must be bumped on any breaking field change.

Entry point: `cctrack:main` (registered in `pyproject.toml` as a console script).

## Key Design Decisions

- Zero external dependencies - stdlib only (json, argparse, subprocess, etc.)
- Deduplication is global across files, Sandy sandboxes and hosts by API identity, with fullest-snapshot selection. Rsync copies can share request/response ids across hosts. Preserve all copy locations and rebuild per-source totals after dedup. Raw hook histories are also merged before differencing; legacy monthly rollups lack the identities needed to dedup copied summaries reliably.
- Hook data is authoritative for **cost only**; every token count comes from the JSONL logs. Never mix the two in one figure - the hook's token fields are snapshots and would double count against JSONL cache columns.
- Never let one machine's hook coverage suppress another machine's JSONL cost - compose per source.
- Each coding agent is its own source. Codex sources are `exact`: priced straight from their logs, never replaced by the Claude hook and never scaled by the (Claude-derived) accuracy factor. In `--explore`, hook comparisons use only the Claude slice of the JSONL total.
- Costs are API list prices. For subscription users (ChatGPT plans, Claude subscriptions) they're API-equivalent values, not invoices.
- Cumulative counters from the hook are not monotonic and don't start at zero when first observed. `compute_hook_deltas` books only observed upward increments (first observation and post-drop levels are baselines, never counted), so a day's hook cost can never exceed the counter's peak and a session's pre-observation history is never mis-dated onto its first-seen day. Test fixtures must start sessions at ~$0 to mirror real statusline behavior.
- `session-baselines.json` carries each session's rolled-up cumulative across the 90-day retention boundary so straddling sessions aren't re-counted from zero.
- Build system is Hatch (`hatchling`)
- Requires Python 3.10+

## Accounting compatibility

- `tests/test_accounting.py` and `tests/fixtures/ccusage-parity.json` pin synthetic reference cases to ccusage 20.0.34. `scripts/compare_ccusage.py` optionally checks an installed binary in isolated temporary homes; no Node/network requirement for ordinary tests or runtime.
- Provider rules take precedence over matching ccusage. Historical Sonnet 4/4.5 context premiums apply to the whole request, including cache input in the threshold; ccusage's marginal calculation is an intentional documented difference.
- Service tiers come from log metadata, never retrospectively from the user's current config. Explicit model aliases must not absorb arbitrary suffix variants. JSON rate metadata includes service-tier multipliers; session rows include tier counts and copy locations. Redaction must cover all added paths/cwds.
