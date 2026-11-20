# Step 07. Documentation and release readiness

Plan for `### Step 7` of `docs/plans/incremental-cache.md`. Written against commit `358ed1b` (step 06; 515 passed, 1 skipped, 88 s, Python 3.12.15, Linux aarch64).

**Decisions for the user.**
- **None is needed to build this step.** D1 is still open, and this step documents its recommended option, which is what Step 6 built: an unreachable host is left out and its cached data is not used. D3 (Step 8) is Dan's call. He makes it from the "after" timings this step asks him to collect (section 10), not here.
- **The Mac and dgx "after" timings can only come from Dan.** Section 10 lists the commands he runs and how to compare the results with F10. Nothing is released: no tag, no push, no `uv publish`, no GitHub release.
- **A latent test bug turned up and this step fixes it** (Findings 4). On an interpreter where cctrack is not installed, `test_cache_local.py::test_mutation_then_warm_matches_uncached[7-code-hash]` fails. It passes in `.venv` and in CI only because those have an editable install. The fix makes the test stricter, not weaker.

## 1. Outcome

- `pyproject.toml` says `version = "0.12.0"`. A wheel built from the tree reports `Version: 0.12.0`, and an installed `cctrack` prints `cctrack 0.12.0` in its `[timing]` header.
- The README's "Parse cache" section is complete. It says what is stored and what is not, where the file lives, the three ways to run (default, `--rebuild-cache`, `--no-cache`/`CCTRACK_CACHE=0`) and `CCTRACK_CACHE_DIR`. It says that the first run, and the first run after any upgrade, reads everything. It also covers when a file is read again, how `--remote` hosts are cached, and how cache problems are handled (they never change a report).
- `cctrack --help` has a `parse cache` group that holds both options and says where the cache lives. The usage line says `cctrack` even under `python -m cctrack`.
- `CLAUDE.md` and the module's "Parse cache" comment block describe the cache as it is after Step 6. They also gain a design-decision bullet.
- The `7-code-hash` test no longer depends on cctrack being installed, and `run_launched` refuses a `src` that has no `cctrack/__init__.py`.
- Two small tests pin the help text and the README section.
- The plan file holds the handoff for Dan (section 10): the commands, what to compare against F10, and the sandbox before and after figures.

## 2. Findings

Everything was checked in this sandbox: Linux aarch64, Python 3.12.15 (`.venv`), uv 0.13.9. `S=/tmp/claude-501/-home-sandy-dev-cctrack/b90020de-1841-4523-9564-a7f61b1fabce/scratchpad`. Scripts are in `$S/s7/`.

1. **Most of the brief is already done.**
   - Steps 4 and 6 followed rule 7: README has a "Parse cache" section, CLAUDE.md has item 4a and the cache commands, and `--help` lists `--no-cache` and `--rebuild-cache`.
   - This step therefore completes and corrects what exists. It does not write the section from scratch. Gaps found by reading against the code:
     - README never mentions the racy rule, pruning, what is sent to a `--remote` host, that the raw-stream fallback is uncached, that cache errors only print a note, or the cache's size.
     - README's "Usage" and "What it does" do not mention the cache.
     - `--help` does not say where the cache lives.
     - CLAUDE.md item 3 still says "Results are cached per host". That means the per-run memo `_remote_bundle_cache` and now reads like the parse cache.
     - The module's `# ── Parse cache` comment block still says main() opens it "for local transcripts and rollouts". Hook files and remote replies use it too.
2. **The whole branch leaves reports unchanged against 0.11.2 on real logs** (`$S/s7/release_compare.sh OLD_SRC NEW_SRC`).
   - The script snapshots this sandbox's `~/.claude/projects` and `~/.claude/cctrack` twice into `/dev/shm`: once as the local home and once as a fake `--remote` host behind a fake `ssh`.
   - For each of 10 modes, local and with `--remote fakehost` (20 cases), it runs 0.11.2 (`git archive 9323fa0 src`) and HEAD (`--no-cache`, cold, warm). The modes are: default, `--explore today`, `--sessions --bucket hour --coverage`, `--json`, `--csv sessions|buckets|coverage|rates`, `--agent claude` and `--agent codex --json`.
   - Result: **all 20 cases byte-identical** across all four runs, with `generated_at` normalised. There were no `cctrack: cache` notes, and `~/.cache/cctrack` was never created.
3. **Sandbox before and after** (`scripts/bench_cache.py --claude-files 60 --codex-files 10 --remote --runs 3`, 116 MB, two machines).
   - "Before" is the F10 code (`09ae4dc`, step 00) run with HEAD's bench script copied into that tree. "After" is HEAD. stdout sha256 was `ac2e3eeb…` in every run.

     | Run | total | claude parse | aggregate | reconcile | remote benchhost |
     |---|---|---|---|---|---|
     | before (Step 0 code), 3 runs | 1.25–1.79 s | 0.08–0.12 | 0.47–0.64 | 0.32–0.40 | 0.32–0.53 |
     | after `--no-cache`, 2 runs | 0.75–0.80 s | 0.11–0.13 | 0.06–0.07 | 0.06–0.08 | 0.40–0.47 |
     | after cold | 0.77 s | 0.15 | 0.06 | 0.06 | 0.42 (358.7 KB received) |
     | after warm, 2 runs | 0.25–0.28 s | 0.014–0.015 | 0.05–0.06 | 0.05–0.06 | 0.11–0.12 (1.1 KB received) |

   - In this sandbox, the cold run costs no more than `--no-cache`. The warm parse phases are about 10× faster. Pricing and merging (aggregate plus reconcile) dropped from about 0.8–1.0 s to 0.11–0.13 s (Step 1).
   - The real logs (21 transcripts, 33 MB) took 0.17 s uncached and 0.023 s warm.
4. **Test bug: `7-code-hash` imports whatever cctrack is installed** (`tests/test_cache_local.py::test_mutation_then_warm_matches_uncached`).
   - The test sets `src = tmp_path / "src2"` *before* its first two runs (`before`, `cold`). It creates that directory only later, in the mutation branch (`shutil.copytree(SRC, src)`).
   - So the first two runs have `PYTHONPATH` pointing at a missing directory, and `import cctrack` falls back to the interpreter's installed cctrack. In `.venv` that is the editable install of this repo (`_editable_impl_cctrack.pth`, dist-info 0.10.0), so the test passes there, and it passes in CI (`pip install -e .`).
   - In a bare venv the run fails with `ModuleNotFoundError: No module named 'cctrack'`. I saw this with Python 3.10.22, pytest 9.2.0 and `PYTHONPATH=src`: `1 failed, 514 passed, 1 skipped`, and that test was the only failure.
   - If the installed copy were a different version, the test would also be comparing two different codes without saying so.
   - `tests/test_cache_remote.py` does it right: it switches `src` to `src2` only inside the mutation branch. The fix copies that pattern (section 6.1).
5. **Full suite on other interpreters** (bare venvs in `$S/v310`, `$S/v3.13`, `$S/v3.14` holding only pytest, with `PYTHONPATH=src`):
   - **3.10.22:** 514 passed, 1 skipped, 1 failed, 108 s.
   - **3.13.5:** 514 passed, 1 skipped, 1 failed, 102 s.
   - **3.14.10:** 515 passed, 1 failed, 97 s. Nothing is skipped: the one skip on the other versions does not apply on 3.14.
   - On every version, the one failure is the `7-code-hash` case from Finding 4. Nothing else in the cache work depends on the interpreter.
   - CI's matrix is 3.10–3.13 with an editable install, so it would not see Finding 4.
6. **`uvx --from .` does not rebuild when only `src/` changes** (uv 0.13.9; experiment in `$S/repo7` with a scratch `UV_CACHE_DIR`).
   - After editing `src/cctrack/__init__.py`, `uvx --from DIR cctrack` ran the old build. So did `uvx --refresh`, `uvx --reinstall`, `--refresh-package cctrack` and `--reinstall-package cctrack`.
   - Only a `pyproject.toml` change (the version bump) or **`uvx --no-cache --from DIR`** rebuilt it, and `--no-cache` did so every time.
   - Dan's Step 0 command (`uvx --from . cctrack`) is therefore not reliable for "after" timings if any commit lands after the version bump without changing `pyproject.toml`. Section 10 uses `uvx --no-cache`. That is uv's own flag; cctrack's `--no-cache` comes after `cctrack`.
   - The `[timing] #` header comes from installed metadata (`importlib.metadata`). Under a `uvx` build it shows the built version (I saw `# cctrack 0.12.0, Python 3.14.10` after the bump). Under `PYTHONPATH=src` in `.venv` it shows the stale `0.10.0` dist-info. **Do not "fix" `.venv`**: the verifier checks the version through a built wheel (section 7).
   - `uvx` picked the newest Python it could find (3.14 here), so section 10 pins `--python 3.11`, the interpreter F10 ran on. The cache's code key includes the interpreter version, so every run must use the same one.
7. **Packaging** (`uv build` of a `git archive HEAD` tree with the version bumped, into `$S/s7/b/dist`):
   - The wheel holds `cctrack/__init__.py`, `cctrack/__main__.py` and the dist-info, with `Version: 0.12.0` and `Requires-Python: >=3.10`.
   - The sdist holds `src`, `tests` (all 15 test files, the fixture and conftest), `scripts` (including `bench_cache.py`), README, CLAUDE.md, LICENSE and pyproject.
   - Installed into a bare 3.10.22 venv, `cctrack` ran against a `/dev/shm` snapshot home twice. Run 1 read 21 files and run 2 reused 20 (the 21st was the live transcript, still racy). stdout was identical, `~/.cache/cctrack` was mode 0700 and `cache.sqlite3` 0600, and `--help` showed `usage: cctrack`.
8. **Help layout** (`$S/s7/ap/apg.py`, 3.10.22 and 3.14.10, run with `-W error`):
   - `parser.add_argument_group(title, description).add_mutually_exclusive_group()` prints the group with its description. It keeps the `[--no-cache | --rebuild-cache]` usage, and still exits 2 with `argument --rebuild-cache: not allowed with argument --no-cache`.
   - No deprecation warning on either version. (Only nesting a mutually exclusive group inside *another mutually exclusive group* is deprecated, and this does not do that.)
   - argparse wraps help with `textwrap`, which may break at hyphens, so tests must not assert on hyphenated phrases across a wrap.
   - Argument `help=` strings are `%`-formatted, so none of the new text may contain `%`.
9. **Cache size.** The figures stay measured, not projected.
   - This sandbox's real logs (33 MB, 21 transcripts plus 5 hook files): `cache.sqlite3` is 160 KB. It holds 25 entries, 2,153 records and 122 KB of payload, about 57 bytes per record.
   - The bench corpus (116 MB over two machines): 1.07 MB. Per record that is 43 bytes for Claude, 78 for Codex and 13 for hook rows.
   - The README says "a few tens of bytes per usage record" and gives no total.
10. **Dead ends.**
    - Don't run Python with the scratchpad as cwd: an earlier step left `$S/types.py` there, which shadows the stdlib.
    - `/dev/shm` is `noexec`, so fake `ssh` scripts must live under `/tmp`.
    - `/tmp` is a 1 GB tmpfs that is about 70% full. Delete bench dirs after use.
    - Don't reinstall `.venv` (Finding 6).

## 3. Files

| File | Change |
|---|---|
| `pyproject.toml` | `version = "0.11.2"` → `version = "0.12.0"`. Nothing else. |
| `src/cctrack/__init__.py` | In `main()`: `argparse.ArgumentParser(prog="cctrack", …)`, and the two cache options move into a `parse cache` argument group (section 4.1). The `# ── Parse cache` comment block is updated (section 4.4). **No other change**: no report output, cache or remote behaviour changes. |
| `README.md` | Usage, What it does, Parse cache (section 4.2). |
| `CLAUDE.md` | Item 3 wording, item 4a (one sentence), Key Design Decisions bullet (section 4.3). |
| `tests/test_cache_local.py` | Fix `7-code-hash` (section 6.1). Add `test_help_describes_cache`. |
| `tests/test_cache_equivalence.py` | `run_launched` asserts its `src` holds `cctrack/__init__.py` (section 6.1). |
| `tests/test_docs.py` (new) | `test_readme_documents_the_cache`, `test_claude_md_documents_the_cache` (section 6). |
| `docs/plans/steps/07-docs-release-readiness.md` | This plan. The implementer fills in Findings 5 (3.13 and 3.14 results) only if the verifier's numbers differ. Otherwise leave it as is. |

`scripts/bench_cache.py`, `.github/workflows/*` and `docs/plans/incremental-cache.md` are unchanged. The `/step` loop updates the Status table.

## 4. Interfaces

### 4.1 `--help`

```python
parser = argparse.ArgumentParser(
    prog="cctrack",
    description="Claude Code cost reporter — scans JSONL logs and reports token usage + cost")  # description unchanged
...
cache_group = parser.add_argument_group(
    "parse cache",
    "Repeat runs re-read only the log files that changed and take the rest from the "
    "cache: cache.sqlite3 in $CCTRACK_CACHE_DIR, else $XDG_CACHE_HOME/cctrack, else "
    "~/.cache/cctrack. It holds parsed usage records, never prices. The first run after "
    "any change to cctrack or Python reads everything.")
cache_opts = cache_group.add_mutually_exclusive_group()
cache_opts.add_argument(
    "--no-cache", action="store_true",
    help="Read every log file; don't read or write the parse cache (or set CCTRACK_CACHE=0).")  # unchanged
cache_opts.add_argument(
    "--rebuild-cache", action="store_true",
    help="Read every log file, including on --remote hosts, and replace the parse cache.")
```

- The group is created where `cache_opts` is created today: after `--agent` and before the "detailed / machine-readable reports" group. Help then prints options, parse cache, detailed, in that order.
- `dest` names stay `no_cache` and `rebuild_cache`.
- No `%` in any of this text.
- Expected help excerpt at 80 columns (wrapping may differ):

```
parse cache:
  Repeat runs re-read only the log files that changed and take the rest from
  the cache: cache.sqlite3 in $CCTRACK_CACHE_DIR, else $XDG_CACHE_HOME/cctrack,
  else ~/.cache/cctrack. It holds parsed usage records, never prices. The first
  run after any change to cctrack or Python reads everything.

  --no-cache            Read every log file; don't read or write the parse
                        cache (or set CCTRACK_CACHE=0).
  --rebuild-cache       Read every log file, including on --remote hosts, and
                        replace the parse cache.
```

### 4.2 README

**Usage** block: after the `--agent codex` example, add:

```bash

# Repeat runs re-read only changed log files (see Parse cache)
cctrack --rebuild-cache   # read everything and replace the cache
cctrack --no-cache        # read everything; leave the cache alone
```

**What it does:** after item 1, insert this item and renumber the rest:

`2. Re-reads only the log files that changed since the last run, taking the rest from its [parse cache](#parse-cache)`

**Parse cache** section: replace the whole section (heading to just before `## Rate card`) with this text. It is a template; keep the facts:

~~~~markdown
## Parse cache

cctrack remembers what it read from each log file, so a repeat run re-reads only the files that changed. The first run reads everything. So does the first run after any change to cctrack, the Python version or the text encoding: it takes as long as an uncached run. Runs after that are much faster.

- **What's stored:** for each Claude Code transcript, Codex rollout and statusline-hook file, the usage records parsed from it: ids, file paths, working directories, git branches, timestamps, models and token counts. It never stores prompt or response text. It never stores prices or totals either: costs are worked out fresh on every run, so a rate-card fix applies to all history. That comes to a few tens of bytes per usage record.
- **Where:** one file, `~/.cache/cctrack/cache.sqlite3` (or under `$XDG_CACHE_HOME/cctrack`, or in `$CCTRACK_CACHE_DIR`), readable only by you. You can always delete it safely: the next run reads everything again.
- **When a file is read again:** when its size or modification time changes. A file modified in the last two seconds is read but not remembered, because it may still be being written. Files that haven't been seen for 30 days are dropped from the cache.
- **`--remote` hosts** are cached on this machine, not on the host. cctrack sends the host the size and modification time it holds for each of the host's files. The host reads and sends back only the files that changed, and nothing on the host is written. The raw-log fallback (`CCTRACK_REMOTE_PARSE=0`, or a host without Python 3.10+) isn't cached. A host that can't be reached is left out of the report, as before: its cached data isn't used.
- **Problems never change a report.** If the cache can't be opened or written, for example because the directory is read-only, the file is damaged, or another cctrack run is holding it, cctrack prints one `cctrack: cache …` note and reads the logs directly. A damaged file is reset.

```bash
cctrack                   # use the cache (the default)
cctrack --rebuild-cache   # read everything, including on --remote hosts, and replace the cache
cctrack --no-cache        # read everything; leave the cache alone (or CCTRACK_CACHE=0)
```

With `CCTRACK_TIMING=1`, the timing line of each step that reads logs ends with `cache: N reused, M read`.
~~~~

Each claim above is backed by code that exists:
- `_is_racy` and `CACHE_RACY_NS` (2 s);
- `CACHE_PRUNE_SECONDS` (30 days, by `last_used`);
- `cache_dir`;
- `open_cache` (corrupt files are reset, any other error gives one note);
- `_remote_manifest` (size and mtime only);
- D1 as built in Step 6;
- `_note_cache_use`.

Do not claim a figure for Dan's machines.

### 4.3 CLAUDE.md

- **Item 3:** replace "Results are cached per host; the parse_remote_* functions read the bundle." with "Bundles are memoised per host for the run (`_remote_bundle_cache`; not the parse cache); the parse_remote_* functions read the bundle."
- **Item 4a:** append one sentence: "User-facing docs: README "Parse cache" and the `parse cache` group in `--help`; `tests/test_docs.py` pins both."
- **Key Design Decisions:** add this bullet after the "Each coding agent is its own source…" bullet:

  "- The parse cache stores only per-file parse results (`{events, stats}`, encoded before any cross-file dedup), never prices, totals or cross-file results. An entry is valid while the file's `(size, mtime_ns)` and the code key (whole-module sha256 + interpreter + text encoding) match, so any cctrack upgrade re-reads everything. stdout must be byte-identical with the cache off, cold, warm or rebuilt; `tests/test_cache_equivalence.py` is where that is proved."

### 4.4 Module comment block

Replace the line `# main() opens it once per run for local transcripts and rollouts. \`sqlite3\`, …` and the line after it with:

```python
# main() opens it once per run. Local transcripts, rollouts and statusline files go
# through it, and so do --remote hosts' per-file replies (protocol v2); the raw stream
# never does. `sqlite3`, `marshal`, `zlib`, `hashlib`, `io` and `codecs` are imported
# inside the functions that need them (cache-only imports).
```

## 5. Behaviour and edge cases

- **Report output must not change.** The only `src` changes are argparse construction and a comment. Changing the module source changes the cache code key, so the first run after this step is a full read. That is expected (D2).
- **`prog="cctrack"`** changes `usage:` and the error prefix (`cctrack: error: …`) under `python -m cctrack`. The installed console script already showed `cctrack`. No test pins `__main__.py`: I grepped `tests/` for `__main__.py` and `usage:`, and the only `--help` test checks substrings. `test_cache_flags_are_exclusive` checks `not allowed with argument --no-cache`, which is unchanged.
- **Moving the options into a group must keep them mutually exclusive.** `test_cache_flags_are_exclusive` must pass unchanged (Findings 8).
- **Do not use `%`** in help or description text (Findings 8). Do not write `$CCTRACK_CACHE_DIR` inside `help=` with a `%`.
- **The version lives in exactly one place**, `pyproject.toml`. `cctrack_version()` reads the installed metadata. Do not add `__version__`, and do not hard-code the version in `src`.
- **The test fix must keep the code-hash mutation meaningful.** `before` and `cold` run with `SRC`. Only the runs after the mutation use `src2`. The closure `run` must see the new value: Python closures look the variable up when called, so assigning `src = src2` in the enclosing function is enough (that is how `test_cache_remote.py` does it).
- **Don't touch** `.github/workflows`. Adding 3.14 to CI is a release-time choice for Dan (section 9).

## 6. Tests

Fixtures: none new. The doc tests read `README.md` and `CLAUDE.md` through `Path(__file__).resolve().parents[1]`. Both are in the sdist (Findings 7). If a file is missing, `pytest.skip`.

### 6.1 Test fix (planner-added criterion: "the suite passes on a Python without cctrack installed")

`tests/test_cache_local.py::test_mutation_then_warm_matches_uncached`:
- Keep `src = SRC` for every case at the start. Remove the early `src = tmp_path / "src2"` line.
- In the `7-code-hash` branch:

  ```python
  src2 = tmp_path / "src2"
  shutil.copytree(SRC, src2)
  with open(src2 / "cctrack" / "__init__.py", "a", encoding="utf-8") as fh:
      fh.write("\n# step 4 code-hash mutation\n")
  src = src2
  ```

  `src` is assigned in the function, so `run`'s closure picks it up.
- Everything else stays as it is: `expect = all_opens` and `content = False`.

`tests/test_cache_equivalence.py::run_launched`: at the top, add

```python
pp = Path(src or SRC)
assert (pp / "cctrack" / "__init__.py").is_file(), f"no cctrack under {pp}"
```

so that no launched run can silently import an installed cctrack.

### 6.2 New tests

| Name | File | Proves |
|---|---|---|
| `test_help_describes_cache` | `tests/test_cache_local.py` | Set `COLUMNS=100` (`monkeypatch.setenv`) and `sys.argv = ["__main__.py", "--help"]`. Argv[0] must be `__main__.py`, not `cctrack`, so that a missing `prog=` shows. `cctrack.main()` raises `SystemExit` 0. Take `out` from capsys and collapse its whitespace with `" ".join(out.split())`. Then check: (a) `out` starts with `usage: cctrack `; (b) it contains `parse cache:`, `$CCTRACK_CACHE_DIR`, `$XDG_CACHE_HOME/cctrack`, `~/.cache/cctrack`, `cache.sqlite3`, `never prices`, `CCTRACK_CACHE=0`; (c) the text between `parse cache:` and `detailed / machine-readable` contains both `--no-cache` and `--rebuild-cache`; (d) `cctrack.cache_dir()` does not exist afterwards. |
| `test_readme_documents_the_cache` | `tests/test_docs.py` | Take the README text from `## Parse cache` to the next `\n## `. It contains each of `~/.cache/cctrack/cache.sqlite3`, `$XDG_CACHE_HOME/cctrack`, `$CCTRACK_CACHE_DIR`, `--rebuild-cache`, `--no-cache`, `CCTRACK_CACHE=0`, `never`, `prompt or response text`, `first run after any change to cctrack`, `two seconds`, `30 days`, `CCTRACK_REMOTE_PARSE=0`, `cctrack: cache` and `cache: N reused, M read`. The whole README also contains `(#parse-cache)`. |
| `test_claude_md_documents_the_cache` | `tests/test_docs.py` | CLAUDE.md contains `4a. **Parse cache**`, `cctrack --rebuild-cache`, `CCTRACK_CACHE=0`, `tests/test_docs.py` and `_remote_bundle_cache`, and no longer contains `Results are cached per host`. |

The `first run after any change to cctrack` phrase in the README template above is written "So does the first run after any change to cctrack". The test looks for the substring `first run after any change to cctrack`, so keep those words contiguous.

### 6.3 Brief criteria → proof

| Brief item | Proof |
|---|---|
| README section on the cache: what is stored | `test_readme_documents_the_cache` (`prompt or response text`, `never`) plus review |
| … where | same (`~/.cache/cctrack/cache.sqlite3`, `$XDG_CACHE_HOME/cctrack`, `$CCTRACK_CACHE_DIR`) |
| … the three options | same (`--rebuild-cache`, `--no-cache`, `CCTRACK_CACHE=0`); default shown in the code block |
| … first run after an upgrade is a full read | same (`first run after any change to cctrack`) |
| `CLAUDE.md` architecture entry | `test_claude_md_documents_the_cache` |
| `--help` text | `test_help_describes_cache`; existing `test_help_lists_cache_options` and `test_cache_flags_are_exclusive` unchanged |
| Version 0.12.0 | Verification 3 (built wheel metadata), Verification 4 (installed header) |
| Do not release | Verification 9 (no tag, no push, branch only) |
| Hand back with Step 0 timings before and after | Section 10 (sandbox figures in Findings 3; Dan's commands) |
| (added) reports unchanged vs 0.11.2 on real logs | Verification 6 (`release_compare.sh`) |
| (added) suite passes without an installed cctrack, on 3.10 | Verification 5 |

### 6.4 Mutation checks (all planner-added; the brief has no Mutation line)

Run each on a copy (e.g. `cp -a src $S/m7/src`, or `git stash`-free: edit, test, `git checkout -- <file>`), one at a time.

| # | Change | Test expected to fail |
|---|---|---|
| M1 | In `main()`, drop `prog="cctrack"` | `test_help_describes_cache` (a): usage shows `__main__.py` |
| M2 | Remove `$CCTRACK_CACHE_DIR, else ` from the group description | `test_help_describes_cache` |
| M3 | Create the cache options on `parser` instead of `cache_group` (back to today's layout) | `test_help_describes_cache` (the section check) |
| M4 | Delete the `**When a file is read again:**` bullet from README | `test_readme_documents_the_cache` |
| M5 | Put back `Results are cached per host` in CLAUDE.md item 3 | `test_claude_md_documents_the_cache` |
| M6 | Revert the `7-code-hash` fix (put the early `src = tmp_path / "src2"` back) | run with the bare 3.10 venv: `run_launched`'s new assert fails in `test_mutation_then_warm_matches_uncached[7-code-hash]` (also under `.venv`, because the assert fires before any import) |
| M7 | Remove the `run_launched` assert but keep the reverted test from M6 | under `.venv` the test passes (that is the bug the assert catches); under the bare 3.10 venv it fails with `ModuleNotFoundError`. Record both. |


## 7. Verification

Use `S=/tmp/claude-501/-home-sandy-dev-cctrack/b90020de-1841-4523-9564-a7f61b1fabce/scratchpad` and run from `/home/sandy/dev/cctrack`, never with cwd in `$S` (Findings 10). Never create `~/.cache/cctrack` or touch `~/.claude/cctrack`. Check before and after with `ls -d ~/.cache/cctrack` (it must not exist).

1. **Suite:** `PYTHONPATH=src python3 -m pytest tests -q`. Expect at least 518 passed (515 plus 3 new), 1 skipped, no failures.
2. **Mutation checks** M1–M7 (section 6.4).
3. **Version via a built wheel:**
   ```bash
   rm -rf $S/s7v && mkdir -p $S/s7v/tree && git archive HEAD | tar -x -C $S/s7v/tree   # after the step is committed; else copy the working tree
   (cd $S/s7v/tree && UV_CACHE_DIR=$S/s7v/uvc uv build -q --out-dir $S/s7v/dist)
   python3 -c "import zipfile;w=zipfile.ZipFile('$S/s7v/dist/cctrack-0.12.0-py3-none-any.whl');print(w.namelist());print([l for l in w.read('cctrack-0.12.0.dist-info/METADATA').decode().splitlines() if l.startswith(('Version','Requires-Python'))])"
   ```
   Expect `Version: 0.12.0` and `Requires-Python: >=3.10`, and that the wheel holds only `cctrack/__init__.py`, `cctrack/__main__.py` and dist-info. Also `grep -n '^version' pyproject.toml` must show `0.12.0`, and `grep -rn '0\.11\.2' pyproject.toml src` must find nothing. If no git commit exists yet, use `rsync -a --exclude .venv --exclude .git ./ $S/s7v/tree/`.
4. **Installed smoke on 3.10** (the `$S/uvpy` 3.10.22 interpreter; `uv python install 3.10` with `UV_PYTHON_INSTALL_DIR=$S/uvpy` if missing):
   ```bash
   uv venv -q -p $S/uvpy/cpython-3.10.22-linux-aarch64-gnu/bin/python3.10 $S/s7v/w310
   uv pip install -q -p $S/s7v/w310/bin/python $S/s7v/dist/cctrack-0.12.0-py3-none-any.whl
   H=$(mktemp -d /dev/shm/s7v.XXXX); mkdir -p $H/.claude $H/.sandy/sandboxes
   cp -a ~/.claude/projects ~/.claude/cctrack $H/.claude/
   for i in 1 2; do HOME=$H SANDY_HOME=$H/.sandy CCTRACK_TIMING=1 CCTRACK_NO_PROGRESS=1 $S/s7v/w310/bin/cctrack 2>$H/err$i >$H/out$i; grep -E '# cctrack|cache' $H/err$i; done
   cmp $H/out1 $H/out2 && echo same; stat -c '%a %n' $H/.cache/cctrack $H/.cache/cctrack/cache.sqlite3
   $S/s7v/w310/bin/cctrack --help | sed -n '/^parse cache:/,/^detailed/p'
   rm -rf $H
   ```
   Expect: the header says `cctrack 0.12.0, Python 3.10.22`; run 2's `claude parse` shows `cache: N reused` with N ≥ 1; the outputs are `same`; modes are `700` and `600`; the help shows the group.
5. **Suite on a bare 3.10 interpreter** (no cctrack installed; proves the Findings 4 fix):
   ```bash
   [ -x $S/v310/bin/python ] || { uv venv -q -p $S/uvpy/cpython-3.10.22-linux-aarch64-gnu/bin/python3.10 $S/v310 && uv pip install -q -p $S/v310/bin/python pytest; }
   PYTHONPATH=src $S/v310/bin/python -m pytest tests -q -p no:cacheprovider
   ```
   Expect 0 failures. Optionally repeat with `$S/v3.13` and `$S/v3.14`.
6. **Reports unchanged against 0.11.2 on real logs:**
   ```bash
   rm -rf $S/old11 && mkdir $S/old11 && git archive 9323fa0 src | tar -x -C $S/old11
   $S/s7/release_compare.sh $S/old11/src /home/sandy/dev/cctrack/src
   ```
   Expect 20 `same` lines, exit 0, and no `WARNING`. The script is in the scratchpad. If it is gone, recreate it from Findings 2: snapshot two homes into `/dev/shm`; put a fake ssh under `$S` that exports the remote HOME and execs `python3 -` or `bash -c`; run each mode with OLD and with NEW (`--no-cache`, cold, warm) using `CCTRACK_CACHE_DIR` under the snapshot; `sed` out `generated_at`; `cmp`.
7. **Live smoke, the step's options against this sandbox's real logs** (snapshot home, scratch cache):
   ```bash
   H=$(mktemp -d /dev/shm/s7l.XXXX); mkdir -p $H/.claude $H/.sandy/sandboxes; cp -a ~/.claude/projects ~/.claude/cctrack $H/.claude/
   E="HOME=$H SANDY_HOME=$H/.sandy CCTRACK_CACHE_DIR=$H/cache CCTRACK_NO_PROGRESS=1 PYTHONPATH=src"
   env $E python3 -m cctrack --help | head -2                       # usage: cctrack ...
   env $E python3 -m cctrack --no-cache > $H/a; ls $H/cache 2>&1    # no such dir
   env $E CCTRACK_TIMING=1 python3 -m cctrack > $H/b 2>$H/eb        # cold
   env $E CCTRACK_TIMING=1 python3 -m cctrack > $H/c 2>$H/ec        # warm
   env $E python3 -m cctrack --rebuild-cache > $H/d
   cmp $H/a $H/b && cmp $H/a $H/c && cmp $H/a $H/d && echo same; grep 'cache:' $H/ec
   env $E python3 -m cctrack --no-cache --rebuild-cache; echo "rc=$?"  # 2, "cctrack: error: argument --rebuild-cache: not allowed with argument --no-cache"
   rm -rf $H
   ```
8. **Docs read-through.** Read the README "Parse cache" section against the code it cites (section 4.2 list). Every sentence must be true of the code. Confirm that no Dan-machine figure is quoted.
9. **Not released:** `git tag --points-at HEAD` is empty; `git status` is clean after the commit; `git log origin/incremental-cache..HEAD 2>/dev/null` (if the remote branch exists) shows unpushed commits, or the branch has no upstream. No `dist/` files are added to git (`dist/` is ignored).

## 8. Deviations

- **`prog="cctrack"`** is not in the brief. It is release polish: `python -m cctrack --help` printed `usage: __main__.py`. Help and usage text only.
- **The test fix and the `run_launched` guard** are not in the brief. Findings 4 shows the suite fails on a Python without an installed cctrack, which is release readiness. The test gets stricter. Nothing is weakened or deleted (rule 1).
- **`tests/test_docs.py`** is new. It pins documentation the brief asks for, so later edits can't silently drop it.
- The plan's sections 3–5 are not changed.

## 9. Out of scope

- Step 8 and decision D3. Dan decides from section 10's numbers.
- D1's alternative (serving a down host from the cache).
- Releasing: tagging, pushing, `uv publish`, GitHub release notes. A draft note is in section 10 for Dan's use.
- CI changes: adding Python 3.14 to `.github/workflows/*.yml`, or running the suite without `pip install -e .`. Both would be good. They are Dan's call at release time.
- The argparse `description` and the `pyproject.toml` `description` still say "Claude Code" only. Rewording them is a product-copy choice, left for the release.
- `cctrack_version()` returning stale metadata under `PYTHONPATH=src` (Findings 6). That is pre-existing and only affects development runs.

## 10. Handoff to Dan: timings before and after

"Before" is **F10** in `docs/plans/incremental-cache.md` (0.11.2 + Step 0, Python 3.11, Mac with `--remote dgx`, 220.7 s). Steps 1–6 should change three things: pricing and merging (Step 1), local parsing on a warm run (Step 4), and the dgx reply on a warm run (Step 6). The commands below measure each one.

**On the Mac, in the cctrack checkout, at the step 07 commit:**

```bash
git switch incremental-cache && git log -1 --oneline        # "step 07: Documentation and release readiness"
R=$PWD; O=$(mktemp -d); cd "$O"
c() { CCTRACK_TIMING=1 uvx --no-cache --python 3.11 --from "$R" cctrack --remote dgx "$@"; }
c --no-cache      > nocache.out 2> nocache.err   # 1. uncached, the like-for-like comparison with F10
c --rebuild-cache > cold.out    2> cold.err      # 2. cold: reads everything and fills ~/.cache/cctrack
c                 > warm.out    2> warm.err      # 3. warm, straight after
grep -h '^\[timing\]' nocache.err cold.err warm.err
diff nocache.out warm.out && echo "same report"
echo "$O"
```

- The first `--no-cache` is uv's: it forces a fresh build from the checkout (Findings 6). The second is cctrack's.
- `--python 3.11` matches F10, and all three runs must use the same Python, because the cache key includes it.
- Each header line must read `# cctrack 0.12.0, Python 3.11.x`. `cold.err` and `warm.err` must have a `cache` line. If either is missing, an old build ran.
- If Claude Code or Codex was busy on either machine between runs, `diff` may show differences in today's figures. Otherwise the report must be the same.
- A day later, with ordinary use in between, one more plain `c > warm2.out 2> warm2.err` gives the realistic warm figure.

**What to compare** (paste the three `[timing]` blocks; the numbers per line come from the census details, so data growth since F10 is visible):

| Line | F10 | `--no-cache` | cold | warm | Expected |
|---|---|---|---|---|---|
| claude parse (Mac) | 30.6 s | | | | warm: only changed files (`cache: N reused, M read`) |
| aggregate | 21.1 s | | | | all three: much lower (Step 1; ~8× on the bench). Now printed after `reconcile`. |
| reconcile | 16.5 s | | | | all three: much lower (Step 1) |
| hook read (Mac) | 1.9 s | | | | warm: only today's file read |
| codex parse (Mac) | 22.5 s | | | | warm: ~0 if no rollout changed |
| remote dgx | 127.3 s, 28.7 MB | | | | warm: smaller reply (`… received; cache: …`) |
| on dgx: claude parse | 59.8 s | | | | warm: still re-reads files changed since the last run (F10: 3.5 GB changed in 24 h) |
| on dgx: codex parse | 32.3 s | | | | warm: still re-reads a changed rollout in full (F10: one 2.6 GB file) |
| on dgx: encode / local: decode / local: merge | (in "other" 29.9 s) | | | | new sub-lines; F10's "other" split up |
| on dgx: other | 29.9 s | | | | what is left: ssh and transfer |
| cache | — | — | | | opening and closing the store |
| total | 220.7 s | | | | |

**For D3 (Step 8)**, take the warm run (better: `warm2`) and work out
`(claude parse (Mac) + on dgx: claude parse) / total`. That is the share of a warm run spent re-reading Claude transcripts that grew, which is the only thing Step 8 can remove. Codex is excluded from Step 8 as briefed. Step 8 is worth doing only if that share is large enough for Dan to care. The plan sets no threshold.

**Sandbox reference** (Findings 3, synthetic 116 MB over two machines): Step 0 code 1.25–1.79 s; 0.12.0 `--no-cache` 0.75–0.80 s; cold 0.77 s; warm 0.25–0.28 s. Same stdout in every run.

**Draft release note** (for Dan to edit when he releases; not committed anywhere):

> 0.12.0: repeat runs re-read only changed logs. cctrack now keeps a parse cache (`~/.cache/cctrack/cache.sqlite3`): for each log file, the usage records parsed from it, never prices. A repeat run re-reads only files whose size or modification time changed, locally and on `--remote` hosts, which now send back only their changed files. Reports are byte-identical with the cache on or off. `--rebuild-cache` and `--no-cache` (`CCTRACK_CACHE=0`) control it, and `CCTRACK_CACHE_DIR` moves it. Pricing is much faster too. The first run after upgrading reads everything once.
