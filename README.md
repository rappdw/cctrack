# cctrack

A lightweight CLI tool that scans [Claude Code](https://docs.anthropic.com/en/docs/claude-code) JSONL logs and reports token usage and estimated cost. Zero dependencies, runs anywhere Python 3.10+ is available.

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

## Remote aggregation

With `--remote`, cctrack SSHs to each host and streams back both the JSONL logs and the host's statusline hook data (`~/.claude/cctrack/`) for local parsing, so authoritative hook costs from every machine are summed per day. Requires SSH key auth.

```bash
# Aggregate this machine + DGX server + laptop
cctrack --remote dgx macbook-air

# For accurate aggregate costs, install the hook on each host too
cctrack --install-hook --remote dgx macbook-air
```

## Rate card

Prices per million tokens, from [Anthropic's pricing page](https://platform.claude.com/docs/en/about-claude/pricing). Cache writes are priced per TTL: 5-minute at 1.25× input, 1-hour at 2× input.

| Model | Input | Output | Cache Read | Cache Write 5m | Cache Write 1h |
|---|---|---|---|---|---|
| Fable 5 / Mythos 5 | $10.00 | $50.00 | $1.00 | $12.50 | $20.00 |
| Opus 5 / 4.8 / 4.7 / 4.6 / 4.5 | $5.00 | $25.00 | $0.50 | $6.25 | $10.00 |
| Opus 4 / 4.1 | $15.00 | $75.00 | $1.50 | $18.75 | $30.00 |
| Sonnet 5 (through 2026-08-31) | $2.00 | $10.00 | $0.20 | $2.50 | $4.00 |
| Sonnet 5 (from 2026-09-01) / 4.x / 3.7 | $3.00 | $15.00 | $0.30 | $3.75 | $6.00 |
| Haiku 4.x | $1.00 | $5.00 | $0.10 | $1.25 | $2.00 |
| Haiku 3.5 | $0.80 | $4.00 | $0.08 | $1.00 | $1.60 |
| Haiku 3 | $0.25 | $1.25 | $0.03 | $0.30 | $0.50 |

Usage is priced with the rates in effect on the day it happened, so time-limited introductory pricing is applied correctly to past usage.

Unknown models fall back to Sonnet rates and are flagged with `?` in the report's Model column — a prompt to update the rate card rather than a silent guess.

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
