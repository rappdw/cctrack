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

With `--remote`, cctrack SSHs to each host (one call per host) and streams back all JSONL content for local parsing. Requires SSH key auth.

```bash
# Aggregate this machine + DGX server + laptop
cctrack --remote dgx macbook-air
```

## Rate card

Prices per million tokens, from [Anthropic's pricing page](https://www.anthropic.com/pricing):

| Model | Input | Output | Cache Read | Cache Write |
|---|---|---|---|---|
| Opus 4.6 / 4.5 | $5.00 | $25.00 | $0.50 | $6.25 |
| Opus 4 / 4.1 | $15.00 | $75.00 | $1.50 | $18.75 |
| Sonnet 4.x | $3.00 | $15.00 | $0.30 | $3.75 |
| Haiku 4.5 | $1.00 | $5.00 | $0.10 | $1.25 |
| Haiku 3.x | $0.80 | $4.00 | $0.08 | $1.00 |

Unknown models fall back to Sonnet rates.

## Origins

This is a Python rewrite of the Go-based [cctrack dashboard](https://github.com/ksred/cctrack), written by Dan Rapp using Claude. The Go version provides a full web dashboard with real-time updates, session explorer, and project breakdown. This Python version strips it down to the essentials: a single command that parses logs and prints a cost report. The JSONL parsing logic, deduplication strategy, and rate card are ported directly from the Go implementation.

## License

[MIT](LICENSE)
