# Step 05: Local hook data (recommendation: skip)

**Recommendation: skip this step.** The brief's gate is "skip this step if Step 0 timings show hook reading is under 5% of a run". F10 shows **3.3%** (7.2 s of 220.7 s, both machines together). There is nothing to implement, test or mutate. The `/step` loop should set row 5 of the Status table to `skipped` with the reason "F10: hook read 3.3% of a run, under the 5% gate", and go on to Step 6.

**One thing the user should know (not blocking).** The gate measures a share of today's **uncached** run. Once the cache is in, a warm run is much shorter, so the hook read becomes a larger share of it. For a local-only warm run on the Mac I estimate about 15–20% (Findings 3). Caching hook files would save about 1.2 s per run (Findings 2). The options:

- **A. Skip as gated** (what this plan does). The local hook read stays uncached at about 1.5–1.9 s per run. Step 7 re-measures, and if the number matters it can be revisited then.
- **B. Skip now, and fold local hook caching into Step 6.** Step 6 already has to build per-file hook entries (kind `hook`) for remote hosts, and the local half is a small addition on top. This widens Step 6's scope.
- **C. Do not skip.** Plan Step 5 in full now. The design is sound: the sort-equivalence proof in Findings 4 passes.

A is the default because it is what the brief says. B is the cheapest way to get the 1.2 s back if Dan wants it.

## 1. Outcome

Nothing in the product changes. This file records the evidence for the skip, plus the facts about hook data that Step 6 needs, because Step 6's brief covers "hook files on the remote host".

## 2. Findings

All scripts are in `/tmp/claude-501/-home-sandy-dev-cctrack/b90020de-1841-4523-9564-a7f61b1fabce/scratchpad/s5/`. The synthetic corpus was built in `/dev/shm/s5hook` and deleted afterwards. These were run in this container on Python 3.12.15, against HEAD `daa0fef`.

1. **Gate (F10).** Hook read took 1.9 s on the Mac and 5.3 s on dgx, 7.2 s in total out of a 220.7 s run: **3.3%**, under 5%. Taken per machine it is also under 5%: Mac 1.9 / 220.7 = 0.9%, and dgx 5.3 s of the host's 127.3 s = 4.2%.

2. **What caching would actually save** (`hookbench.py`). I built a synthetic hook corpus sized like the Mac in F10: 646 `statusline-*.jsonl` files, 96.8 MB, 412,794 rows, about 240-byte lines copied from this sandbox's real hook file. The files are spread over 8 dirs and all are newer than 90 days, so rollup deletes nothing. I timed the hook phase from `main` piece by piece, 3 runs:

   | Piece | Time |
   |---|---|
   | `rollup_old_hook_files` (nothing old) | 0.002 s |
   | `load_session_baselines` + `load_monthly_hook_summaries` | 0.001 s |
   | `parse_hook_rows` (read + `_hook_rows_from_lines` + sort) | **1.29–1.38 s** |
   | `compute_hook_deltas` (not cacheable: it is cross-file) | 0.22–0.25 s |
   | total | 1.52–1.62 s (F10 on the Mac: 1.9 s) |

   For the cached path I decoded per-file `zlib(marshal(rows))` blobs (5.5 MB in total), concatenated them and did the final sort: **0.15–0.21 s**, before any SQLite lookups. So the most Step 5 could save is about **1.1–1.2 s per local run**. `compute_hook_deltas` stays on every run either way.

3. **Projected warm-run share.** These are estimates from F10, scaled by the speedups measured in Steps 1, 3 and 4. They are not measurements.
   - **Mac, local only:**
     - Discovery takes 0.8 s.
     - Changed Claude files are re-read: 30.6 s × 744 MB / 5.8 GB ≈ 3.9 s.
     - Decoding the cached files takes about 1 s (Step 3: 2.0 s per 1M events, at real logs' ~48 events/MB).
     - Aggregate and reconcile take about 2–4 s (Step 1: about 8× faster).
     - Hook read takes 1.9 s.
     - Total: about 10–12 s, of which hook is about 15–20%. Step 5 would bring that to about 6–8%.
   - **With `--remote dgx`, after Step 6:** dgx still re-reads about 6 GB of changed files every run (F10: 3.5 GB of Claude transcripts and one 2.6 GB Codex rollout), roughly 70 s, so the run stays near 90–100 s. Local Step 5 would save about 1% of it.

4. **Planner proves: per-file sort equals the single sort.** This holds, and Step 6 needs the same fact for remote hook files.
   - **Argument.** `_hook_rows_from_lines` handles each line on its own (no state carries from one line to the next), then does one stable sort on `(session_id, ts)`. Sorting each file's rows, joining them in file order and sorting again with the same key gives the same result:
     - Rows with different keys come out in key order both ways.
     - Rows with equal keys keep their input order both ways, because the sort is stable. That order is (file position, line position), the same as the line order in today's single list.
   - **Line boundaries match too.** `parse_hook_rows` extends a list with each file's `readlines()`, so a file without a final newline never merges with the next file's first line.
   - **Experiment** (`sortprop.py`). 3,000 random cases of 1–5 files with 0–12 lines each. They include unparseable lines, blank lines, a missing final newline, non-UTC offsets, `cost_usd: null`, and equal `(session_id, ts)` with different costs in different files. **0 mismatches**: `_hook_rows_from_lines(all lines) == sorted(concat(per-file rows))`.

5. **For Step 6: remote hook reading is not the local code.** `_remote_collect` builds one blob and calls `parse_hook_blob`. That differs from the local path in four ways:
   - No rollup runs (the remote is read-only).
   - Files come from `d.rglob("statusline*.jsonl")`: recursive, and including the legacy `statusline.jsonl` in sorted position. Locally it is `glob("statusline-*.jsonl")`, with the legacy file appended last.
   - Rows from **all** hook dirs on the host go into one list and one sort. Locally each dir is parsed and differenced separately.
   - Lines are split with `str.splitlines()`, not `readlines()`.

   `splitlines` also splits on ` `, `\x0b`, `\x1c` and similar characters. A hook line holding a raw ` ` gives 1 row via `_hook_rows_from_lines([line])` and **0 rows** via `parse_hook_blob` (checked). `HOOK_SCRIPT` writes `json.dumps(rec)` with ASCII escaping, so real files should not contain these characters. Even so, a per-file remote hook entry must be parsed with the blob's `splitlines` semantics to be exact, not with `_hook_rows_from_lines(readlines())`. Findings 4 then carries over unchanged: the order is the host's file order across all dirs, with one final stable sort.

6. **For Step 6: hook rows are a large part of the remote reply.** I encoded the 412,794 synthetic rows the way `_remote_collect` does (tagged JSON + zlib level 6). That gave **6.7 MB** (7.5 MB at level 1). Encoding took 0.97 s on the remote side and decoding (`_untag_json(json.loads(...))`) 0.49 s locally. dgx's 581 files and 101 MB are about the same size, so hook rows could be roughly a quarter of F10's 28.7 MB reply and 1–1.5 s of its 30 s "other". Synthetic costs and session ids compress worse than real ones, so this is an upper bound. Step 6's manifest of hits would remove most of it: only today's hook file changes, and F10 counts 19 files changed in 24 h on dgx, mostly in sandboxes.

7. **For Step 6: the store already fits hook entries.**
   - `CacheStore.get/put` take any `kind`, and the payload shape `{"events": [...], "stats": None}` holds hook rows. They are plain dicts of str and float, so `marshal` encodes them.
   - Hook files have no `_parse_log_file` kind in `LOG_KINDS`. Step 6 needs either a hook branch next to `_parse_log_file_cached`, or a separate small function.
   - The racy rule matters for hook files. Today's file is appended to on every status-line render, so it is often racy and gets re-read. That is correct.

8. **Dead ends.** Do not try to cache `compute_hook_deltas` output or monthly or baseline files. Deltas are cross-file (sessions span days) and depend on `session-baselines.json`, which rollup rewrites. Monthly and baseline files total about 1 ms (Findings 2).

## 3. Files

- `docs/plans/steps/05-local-hook-data.md`: this file.

No product, test or other plan files change. Setting the Status table row to `skipped` is the `/step` loop's job after this step is accepted.

## 4. Interfaces

None.

## 5. Behaviour and edge cases

None. Hook reading stays exactly as it is: `rollup_old_hook_files`, `load_session_baselines`, `parse_hook_rows`, `compute_hook_deltas` and `load_monthly_hook_summaries` run per dir in `main` on every run, uncached.

## 6. Tests

No new tests. The brief's Accept items are about behaviour that is not built:
- hook totals, `hook_cost_usd` and `--coverage` identical cached and uncached;
- a rolled-up file leaves no effect;
- only today's file is opened on a warm run.

They are trivially met: the hook path does not touch the cache. Step 4's harness already compares stdout across cache modes with hook files on both machines, including one older than 90 days (section 5 corpus).

The brief gives no mutation checks, and none are added.

## 7. Verification

The verifier should confirm the skip is justified and nothing changed:

1. `git status --porcelain` shows only `docs/plans/steps/05-local-hook-data.md` added (plus the Status table edit, if the loop has made it).
2. `PYTHONPATH=src python3 -m pytest tests -q` passes, with the same count as at `daa0fef`.
3. The gate arithmetic from F10 in `docs/plans/incremental-cache.md`: (1.9 + 5.3) / 220.7 = 3.26% < 5%.
4. Optional, to reproduce Findings 2 and 4 (with `S=/tmp/claude-501/-home-sandy-dev-cctrack/b90020de-1841-4523-9564-a7f61b1fabce/scratchpad/s5`). Both scripts import `cctrack` from `/home/sandy/dev/cctrack/src`. They need about 100 MB in `/dev/shm`, and neither opens a cache.

   ```
   python3 $S/hookbench.py --rebuild    # parse ≈1.3 s, cached path ≈0.15–0.2 s
   python3 $S/sortprop.py               # "mismatches 0 of 3000"
   rm -rf /dev/shm/s5hook
   ls ~/.cache/cctrack                  # must not exist
   ```

There is no live smoke, because no behaviour changes.

## 8. Deviations

- The step is skipped, as its brief allows. The 5% gate is applied as written, to F10's uncached run. The warm-run projection (Findings 3) is shown as information and options for the user, not used to override the gate.
- Sections 3–5 of `docs/plans/incremental-cache.md` are not edited. Step 6's planner should read Findings 4–7 here: remote hook semantics, reply size, store fit.

## 9. Out of scope

- Remote hook files: Step 6, as briefed. Findings 4–7 are its starting point.
- Re-measuring hook share after the cache: Step 7 (timings before and after) and Step 8's gate.
- Any change to rollup, baselines, monthly summaries or `compute_hook_deltas`.
