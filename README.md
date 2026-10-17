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
cctrack --dirs ~/.claude/projects ~/old-machine/projects

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

1. Walks `~/.claude/projects/` — and each [Sandy](#sandy-sandboxes) sandbox's `claude/projects/` — for Claude Code transcripts, and each Codex home for Codex rollouts
2. Parses `assistant` events with token usage
3. Deduplicates API message/response identities across files, Sandy sandboxes and remote hosts, keeping the fullest usage snapshot
4. Calculates cost using Anthropic's published rates per model
5. Prints daily breakdown with input/output/cache token splits and monthly summaries

## Codex CLI

cctrack also reads [Codex CLI](https://github.com/openai/codex) usage from `$CODEX_HOME` (default `~/.codex`) and from every [Sandy sandbox](#sandy-sandboxes)'s `codex/` — `sessions/` and `archived_sessions/` — and with `--remote`, from each host. When both agents have spend, the month-to-date block and `--explore` show a per-agent split.

How it's counted (verified against the Codex source, not inferred):

- **Request records and cumulative snapshots.** `token_usage_record` and `token_count` can coexist, including around compactions. A request record replaces only its matching snapshot; later ordinary usage still counts. Last-usage-only and cumulative-only rollouts are supported, and unchanged cumulative re-emissions are ignored. Explicit models on request records take precedence over the surrounding turn's model.
- **Forks and copies.** Response IDs deduplicate exact records. Older fork histories with rewritten timestamps are compared against the parent's usage sequence. If the parent is unavailable, possible replay is retained and flagged UNKNOWN by `--coverage`; a fast burst alone is not proof of duplication.
- **Cached tokens are inside `input_tokens`.** Codex reports cached reads and cache writes as parts of `input_tokens`, and reasoning tokens as part of `output_tokens`. Billable uncached input is `input − cached − cache_write`; reasoning is never added on top.
- **Its own cost source.** Codex spend is priced directly from its logs and is never replaced by, or scaled with, Claude Code's statusline-hook figures.
- **Compressed rollouts.** Codex compresses older rollouts to `.jsonl.zst`. Python's standard library only reads zstd from 3.14, so on older Pythons cctrack uses the `zstd` command if it's installed, and otherwise skips those files and says how many it skipped.

Recorded `service_tier` settings (`priority`/`fast`, `flex`, or `default`/`standard`) select the model's corresponding price. Claude's recorded `usage.speed` is also respected. Missing tier metadata uses standard pricing; today's config is not applied retrospectively to old requests.

Costs are at OpenAI's **API list prices**. If you use Codex through a ChatGPT plan, that's an API-equivalent value, not what you're billed — the same is true of Claude Code on a Claude subscription.

## Sandy sandboxes

cctrack finds every [Sandy](https://github.com/rappdw/sandy) sandbox on the machine (and on `--remote` hosts) and includes its Claude Code and Codex usage. It follows sandy's published host-side path contract:

- **Finding sandboxes:** `sandy --print-state` (`sandboxes[].path`) when sandy is installed, which follows `$SANDY_HOME`; otherwise `$SANDY_HOME/sandboxes/*` (default `~/.sandy/sandboxes`).
- **Per sandbox:** Claude Code transcripts from `claude/projects/`, statusline-hook data from `claude/cctrack/`, Codex rollouts from `codex/sessions/` and `codex/archived_sessions/`. Other files under `claude/` (`history.jsonl`, job timelines, tool-audit logs) aren't transcripts and are ignored.
- **Copies:** matching API identities are charged once across all collected locations. The fullest usage snapshot wins; equivalent copies have deterministic attribution. Session JSON/CSV includes `source_locations` and `source_files` so every copy remains traceable. Raw statusline histories are merged before calculating cost increments. Old monthly hook summaries contain no session identities, so copied summaries cannot yet be deduplicated reliably.
- **Temporary copies:** the hidden `.rsync-from.<name>.<pid>/` copy that exists while `sandy --rsync-from` is pulling a sandbox in is skipped, as are `.lock` directories. A sandbox's Codex home is never shared with the host's `~/.codex`, and transcript history copied between hosts with `sandy --rsync` is deduplicated by API message/response identity.

- **Dot-named sandboxes count:** a workspace named like a dotfile directory (e.g. `~/.dotfiles`) gets a sandbox whose name starts with a dot. Sandy's own listing currently omits these, so cctrack scans for them itself — any dot-directory holding `WORKSPACE.json` is a real sandbox.

Orphaned sandboxes (workspace deleted, sandbox not yet removed) still count: their usage is real history.

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

## Sessions, time buckets and coverage (JSON / CSV)

For attributing cost to sessions, roles or time windows. All times and day boundaries are **UTC**; output holds ids, paths, counts and money only, never prompt or response content.

```bash
cctrack --sessions --since 2026-10-11T02:30:04Z --until 2026-10-12   # per-session table
cctrack --bucket hour --since 2026-10-11 --until 2026-10-12          # cost per UTC hour
cctrack --coverage                                                   # what data exists, and gaps
cctrack --json --since 2026-10-11 > report.json                      # everything, one document
cctrack --csv sessions                                               # or buckets | coverage | rates
cctrack --json --redact                                              # hash paths and names
```

- `--since` / `--until` take ISO-8601 (zones converted to UTC; naive times are UTC) or `YYYY-MM-DD`. The window is `since <= t < until`, and a bare `--until` date includes that whole day.
- **Sessions**: one row per session, with Claude subagent (sidechain) turns as their own rows linked by `parent_session_id`. Codex subagent threads are separate sessions that carry their parent's id. Each row has agent, host, Sandy sandbox, project/cwd, git branch, recorded service-tier counts and all observed copy locations, first and last record time, models, request count, tokens and cost in five classes (input, output, cache read, 5-minute and 1-hour cache write), mean and max cache-read and context tokens per request, and compactions.
- **Cost basis**: `cost_usd` is always API list price from the built-in rate card. The rates applied (per model, $/Mtok per class), the card's version and its sources are in the JSON (`rate_card`, `cost_basis`) and in `--csv rates`. For Claude, `hook_cost_usd` is Claude Code's own cost counter. It's booked per UTC day, so it's only filled in for day-aligned windows. Logs don't say whether you're on API or subscription billing, so record that with `--billing-mode subscription` (or `claude=subscription,codex=api`, or `CCTRACK_BILLING_MODE`). It's shown on each row and doesn't change cost.
- **Coverage**: for each log directory (per host and sandbox), the file count, usage records, earliest and latest timestamp, and any unparseable lines or unreadable files (status `UNKNOWN`). Days in the window with no records are listed as UNKNOWN, since idle days and lost logs look the same. Where the statusline hook saw Claude spend but no transcript exists, the day and session are flagged with tokens UNKNOWN. That's the signature of lost logs.
- `--redact` hashes cwd, project, sandbox, branch and file paths, but not session ids. Set `CCTRACK_REDACT_SALT` so the hashes of guessable repo names can't be reversed.
- JSON and CSV carry `schema_version` (currently 1). Field names are stable within a schema version.

## Statusline hook (optional)

```bash
cctrack --install-hook      # this machine (add --remote HOST to install on another)
cctrack --uninstall-hook    # keeps the data it collected
```

The hook is optional. Without it, Claude Code cost is priced from the transcripts at list price. With current rates that tracks Claude Code's own counter closely, typically within a few percent. With it, cctrack also records Claude Code's own running cost counter. That gives you:

- the "authoritative" Claude cost in reports and `hook_cost_usd` in JSON;
- a measured correction for days priced from transcripts;
- detection of lost transcripts in `--coverage`.

It's installed as Claude Code's `statusLine` command (`~/.claude/hooks/cctrack-hook.py`), not as an entry under `hooks`. Each status-line redraw appends one record to `~/.claude/cctrack/statusline-<UTC date>.jsonl`.

**An existing status line is kept.** If one is configured, install saves it in `~/.claude/cctrack/config.json`. On every redraw the hook records the cost, then runs your command with the same input, and passes its output and exit code through, so your status line looks the same as before. Uninstall puts your status line back. If you've changed the status line to something else since installing, uninstall leaves it alone. Re-installing keeps the saved command and the original install date.

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
| Sonnet 4.6 / 4.5 / 4 / 3.7 (base) | $3.00 | $15.00 | $0.30 | $3.75 | $6.00 |
| Sonnet 4.5 / 4 (historical prompts > 200K) | $6.00 | $22.50 | $0.60 | $7.50 | $12.00 |
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

Codex variants without their own price (e.g. `gpt-5.1-codex`) are priced as their base model, consistent with the one variant OpenAI does list (`gpt-5.3-codex` = `gpt-5.2`). Long-context and Haiku 5.5 prompt-length tiers are applied to the whole request, including cache tokens when selecting the tier. The historical Sonnet 4/4.5 premium follows [Anthropic's published whole-request pricing](https://claude.com/resources/articles/1m-context), rather than ccusage 20.0.34's marginal calculation. Explicit aliases include `gpt-5.1-codex-mini` (GPT-5 mini), `gpt-5.1-codex-max` (GPT-5.1), and `gpt-5.6` (GPT-5.6 Sol); dated snapshots of those aliases are supported.

**Every model version has its own row.** A model only matches a row if it is that version or a dated snapshot of it — never a newer minor version. So a newly released model (say `claude-opus-5-7`, or `gpt-5.9-codex-mini`) shows as unknown and is flagged with `?` in the Model column, rather than silently inheriting its predecessor's price. Unknown Claude models are estimated at Sonnet rates, unknown OpenAI models at GPT-5 rates. Pricing is date-aware, so any time-limited introductory price applies only to usage on the days it was in effect.

## Accounting regression checks

Run the offline test suite with `PYTHONPATH=src python3 -m pytest`. Synthetic fixtures in `tests/fixtures/ccusage-parity.json` are pinned to ccusage 20.0.34, commit `e4f63baa4e2943d4588f1b644281d6c8325faa74`. They cover compactions, legacy snapshots, forks, speed tiers, message deduplication, advisor calls and model aliases. Additional tests cover sandbox/host copies, cumulative hook histories, context boundaries and attribution.

To repeat the comparison against an independently installed executable:

```bash
PYTHONPATH=src python3 scripts/compare_ccusage.py --ccusage /path/to/ccusage
```

The command requires exactly ccusage 20.0.34 and uses isolated temporary homes with offline pricing. Runtime cctrack and its normal tests do not depend on Node or ccusage. Provider documentation resolves pricing disagreements. Intentional differences are recorded in the fixture: Sonnet 4 uses whole-request context pricing, unknown models retain a flagged estimate, and cctrack's token-derived costs correspond to ccusage's `calculate` mode rather than its stored-`costUSD` `auto` mode.

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
