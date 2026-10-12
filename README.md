# cctrack

A lightweight CLI tool that scans [Claude Code](https://docs.anthropic.com/en/docs/claude-code) and [Codex CLI](https://github.com/openai/codex) logs and reports token usage and estimated cost. Zero dependencies, runs anywhere Python 3.10+ is available.

## Install

```bash
# Run directly (no install needed)
uvx cctrack

# Or install globally
uv tool install cctrack
```

## Usage

```bash
# Scan local logs and print report
cctrack

# Last 7 days only
cctrack --days 7

# Aggregate with remote machines via SSH
cctrack --remote dgx macbook-air

# Custom log directories
cctrack --dirs ~/.claude/projects ~/.sandy/sandboxes

# Only one coding agent (default: all found)
cctrack --agent claude
cctrack --agent codex
```

## Example output

```
cctrack — Claude Code Cost Report
══════════════════════════════════

April 2026 — month to date (day 1, 1 active)
───────────────────────────────────────
  Input tokens:            23,381
  Output tokens:          199,069
  Cache read:          30,862,977
  Cache write:          1,051,023
  Total tokens:        32,136,450
  Total cost:     $25.85
  Avg/day:        $25.85
  Projected/mo:   $775.50 (based on 1-day avg)

Daily breakdown:
  Date                Input       Output      Cache R      Cache W       Cost
  ──────────── ──────────── ──────────── ──────────── ──────────── ──────────
  2026-04-01         23,381      199,069   30,862,977    1,051,023 $   25.85
```

## What it does

1. Walks `~/.claude/projects/` and `~/.sandy/sandboxes/` for JSONL log files
2. Parses `assistant` events with token usage
3. Deduplicates by `requestId` (last event wins, matching Claude Code's semantics)
4. Calculates cost using Anthropic's published rates per model
5. Prints daily breakdown with input/output/cache token splits and monthly summaries

## Codex CLI

cctrack also reads [Codex CLI](https://github.com/openai/codex) usage from `$CODEX_HOME` (default `~/.codex`) — `sessions/` and `archived_sessions/` — and with `--remote`, from each host. When both agents have spend, the month-to-date block and `--explore` show a per-agent split.

How it's counted (verified against the Codex source, not inferred):

- **Per-response usage.** Newer Codex versions log a `token_usage_record` for every API response with a unique `response_id`; those are counted and deduplicated across files, so forked and resumed threads (which replay their parent's history) aren't double counted. Older versions only log `token_count` events, which are re-emitted with unchanged totals — only events whose running total moved are counted.
- **Cached tokens are inside `input_tokens`.** Codex reports cached reads and cache writes as parts of `input_tokens`, and reasoning tokens as part of `output_tokens`. Billable uncached input is `input − cached − cache_write`; reasoning is never added on top.
- **Its own cost source.** Codex spend is priced directly from its logs and is never replaced by, or scaled with, Claude Code's statusline-hook figures.
- **Compressed rollouts.** Codex compresses older rollouts to `.jsonl.zst`. Python's standard library only reads zstd from 3.14, so on older Pythons cctrack uses the `zstd` command if it's installed, and otherwise skips those files and says how many it skipped.

Costs are at OpenAI's **API list prices**. If you use Codex through a ChatGPT plan, that's an API-equivalent value, not what you're billed — the same is true of Claude Code on a Claude subscription.

## Explore a day

When a day's cost looks wrong or surprisingly high, `--explore` breaks it down:

```bash
cctrack --explore 2026-08-03          # a specific day
cctrack --explore yesterday           # or 'today'
cctrack --explore yesterday --remote air dgx   # across machines
```

It reports, for that day:

- **Where it went** — the day's cost decomposed by token class (output, 1h/5m cache writes, input, cache reads), so a cache-write blowup or a runaway output loop is obvious at a glance.
- **By model / by project / by machine** — cost and share for each, so you can see which model, which repo, and which host drove it.
- **Top sessions** — ranked by cost, each with its project and model, and — where the statusline hook covered that session — its **authoritative** cost next to the JSONL estimate.

The day's JSONL-estimated total is shown next to the hook-authoritative total. If they diverge by more than ~15% the report says so: a big gap means the estimate is off (incomplete logs, or a model the rate card doesn't know), not that you actually spent that much. This is the fastest way to tell a real expensive day from an accounting artifact.

The project label is the basename of each session's working directory (`cwd`). When that's an unfamiliar name — a container path like `/workspace/team-inbox`, or a directory you don't recognize — trace it to its real location:

```bash
cctrack --explore 2026-08-03 --project team-inbox
```

This restricts the view to that project and prints each session's full `cwd`, git branch, and the log file on disk (which reveals the host directory or the specific Sandy sandbox it came from). The unfiltered view flags this automatically when a `cwd` looks like a container path. `scripts/trace_project.py` and `scripts/diagnose_session_cost.py` are standalone versions of the trace and per-session cost diagnostics.

## Remote aggregation

With `--remote`, cctrack SSHs to each host and streams back both the JSONL logs and the host's statusline hook data (`~/.claude/cctrack/`) for local parsing, so authoritative hook costs from every machine are summed per day. Requires SSH key auth.

```bash
# Aggregate this machine + DGX server + laptop
cctrack --remote dgx macbook-air

# For accurate aggregate costs, install the hook on each host too
cctrack --install-hook --remote dgx macbook-air
```

## Rate card

Prices per million tokens. Claude prices are from [Anthropic's pricing page](https://platform.claude.com/docs/en/about-claude/pricing); cache writes are priced per TTL (5-minute and 1-hour).

| Claude model | Input | Output | Cache Read | Cache Write 5m | Cache Write 1h |
|---|---|---|---|---|---|
| Fable 5.1 / Mythos 5.1 | $10.00 | $50.00 | $0.25 | $12.50 | $20.00 |
| Fable 5 / Mythos 5 | $10.00 | $50.00 | $1.00 | $12.50 | $20.00 |
| Opus 5.5 | $4.00 | $20.00 | $0.20 | $5.00 | $8.00 |
| Opus 5 / 4.8 / 4.7 / 4.6 / 4.5 | $5.00 | $25.00 | $0.50 | $6.25 | $10.00 |
| Opus 4.1 / 4 | $15.00 | $75.00 | $1.50 | $18.75 | $30.00 |
| Sonnet 5.5 | $2.00 | $10.00 | $0.10 | $2.50 | $4.00 |
| Sonnet 5 | $2.00 | $10.00 | $0.20 | $2.50 | $4.00 |
| Sonnet 4.6 / 4.5 / 4 / 3.7 | $3.00 | $15.00 | $0.30 | $3.75 | $6.00 |
| Haiku 5.5 (prompt ≤ 100K) | $0.10 | $0.50 | $0.01 | $0.125 | $0.20 |
| Haiku 5.5 (prompt > 100K) | $0.50 | $2.50 | $0.05 | $0.625 | $1.00 |
| Haiku 4.5 | $1.00 | $5.00 | $0.10 | $1.25 | $2.00 |
| Haiku 3.5 | $0.80 | $4.00 | $0.08 | $1.00 | $1.60 |
| Haiku 3 | $0.25 | $1.25 | $0.03 | $0.30 | $0.50 |

OpenAI prices (for Codex) are from [OpenAI's pricing page](https://developers.openai.com/api/docs/pricing), Standard tier. OpenAI has a single cache-write price, listed only for some models; elsewhere cache writes bill as ordinary input.

| OpenAI model | Input | Cached input | Output | Cache write | Over 272K input |
|---|---|---|---|---|---|
| gpt-6-astra | $10.00 | $1.00 | $50.00 | $12.50 | $20 / $2 / $75 / $25 |
| gpt-6.1-sol | $2.00 | $0.10 | $10.00 | $2.50 | $4 / $0.20 / $15 / $5 |
| gpt-6-sol | $2.00 | $0.20 | $10.00 | $2.50 | $4 / $0.40 / $15 / $5 |
| gpt-6-luna | $0.10 | $0.01 | $0.50 | $0.125 | $0.20 / $0.02 / $0.75 / $0.25 |
| gpt-5.6-sol | $4.00 | $0.40 | $20.00 | $5.00 | $8 / $0.80 / $30 / $10 |
| gpt-5.6-terra | $2.00 | $0.20 | $12.00 | $2.50 | $4 / $0.40 / $18 / $5 |
| gpt-5.6-luna | $0.20 | $0.02 | $1.20 | $0.25 | $0.40 / $0.04 / $1.80 / $0.50 |
| gpt-5.5 | $5.00 | $0.50 | $30.00 | — | $10 / $1 / $45 |
| gpt-5.5-pro / gpt-5.4-pro | $30.00 | — | $180.00 | — | $60 / — / $270 |
| gpt-5.4 | $2.50 | $0.25 | $15.00 | — | $5 / $0.50 / $22.50 |
| gpt-5.4-mini | $0.75 | $0.075 | $4.50 | — | |
| gpt-5.4-nano | $0.20 | $0.02 | $1.25 | — | |
| gpt-5.3-codex / gpt-5.2 | $1.75 | $0.175 | $14.00 | — | |
| gpt-5.2-pro | $21.00 | — | $168.00 | — | |
| gpt-5.1 / gpt-5 | $1.25 | $0.125 | $10.00 | — | |
| gpt-5-pro | $15.00 | — | $120.00 | — | |
| gpt-5-mini | $0.25 | $0.025 | $2.00 | — | |
| gpt-5-nano | $0.05 | $0.005 | $0.40 | — | |

Codex variants without their own price (e.g. `gpt-5.1-codex`) are priced as their base model, consistent with the one variant OpenAI does list (`gpt-5.3-codex` = `gpt-5.2`). Long-context and Haiku 5.5 prompt-length tiers are applied per request.

**Every model version has its own row.** A model only matches a row if it is that version or a dated snapshot of it — never a newer minor version. So a newly released model (say `claude-opus-5-7`, or `gpt-5.1-codex-mini`) shows as unknown and is flagged with `?` in the Model column, rather than silently inheriting its predecessor's price. Unknown Claude models are estimated at Sonnet rates, unknown OpenAI models at GPT-5 rates. Pricing is date-aware, so any time-limited introductory price applies only to usage on the days it was in effect.

## Energy estimate

`cctrack --energy` adds an estimated inference energy column (Wh/kWh):

```bash
cctrack --energy
```

This is a **rough estimate, not a measurement** — Anthropic does not publish per-token energy figures or model sizes. It is modelled from logged tokens using the published full-stack per-prompt figures the major labs have disclosed (Google: 0.24 Wh median Gemini text prompt; OpenAI: ~0.34 Wh average ChatGPT query — both include accelerator, host, cooling and idle capacity), apportioned across token classes by relative compute cost and scaled by model tier:

- **output tokens dominate** — decode is sequential and memory-bandwidth bound
- **input and cache writes** are prefill: parallel, far cheaper per token
- **cache reads** skip prefill entirely (~0.1× input)
- both cache-write TTLs use the same figure — a 1-hour write costs more money but the same compute

Treat results as ±1 order of magnitude. It covers datacenter inference only: not model training, and not your own machine. Scale the whole model with `CCTRACK_ENERGY_SCALE=1.5` if you have better numbers for your workload.

## Origins

This is a Python rewrite of the Go-based [cctrack dashboard](https://github.com/ksred/cctrack). The Go version provides a full web dashboard with real-time updates, session explorer, and project breakdown. This Python version strips it down to the essentials: a single command that parses logs and prints a cost report. The JSONL parsing logic, deduplication strategy, and rate card are ported directly from the Go implementation.

## License

[MIT](LICENSE)
