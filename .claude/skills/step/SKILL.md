---
description: Run the plan → implement → verify loop for the steps of docs/plans/incremental-cache.md, using the step-planner (Opus), step-implementer (Sonnet) and step-verifier (Sonnet) agents. Each verified step becomes one local commit on the incremental-cache branch. Nothing is pushed or released.
argument-hint: "[step number, e.g. 3; omit for the next todo step] [--one]"
---

Run the build loop starting at step $ARGUMENTS of `docs/plans/incremental-cache.md` (the
plan). If no step number is given, start at the first `todo` row of the plan's status
table. Step numbers are 0–8; `3` and `03` mean the same step. A step's brief is the
section headed `### Step N. Title`; its detailed plan is `docs/plans/steps/NN-<slug>.md`,
where NN is the two-digit step number.

You are the orchestrator: you dispatch the three agents and decide what happens between
them. You do not plan, implement or verify a step yourself, because the loop depends on
those three roles having separate contexts.

By default this is an outer loop: each step that passes is committed, then the loop
continues with the next step. With `--one`, run a single step and stop after committing
it. Never push, tag, release or touch `main`.

Before the first step:

- Make sure you are on the `incremental-cache` branch, creating it from `main` if it does
  not exist.
- If the plan, `docs/plans/steps/`, or this loop's own files (`.claude/skills/step/`,
  `.claude/agents/step-*.md`) are untracked or modified, commit them first as one commit
  titled `Add incremental-cache plan and /step loop`, staging those paths only.
- Any other uncommitted change: stop and ask. (`.claude/settings.local.json` is ignored by
  git and never committed.)

Before dispatching each step:

- Read the status table and the step's brief. If a step it depends on is not `done` or
  `skipped`, stop and say so.

For each step:

1. **Plan.** Dispatch `step-planner` with the step number. When it returns, read the
   step's plan. Stop the loop and put the question to the user before implementing if
   the planner reports any of:
   - the step cannot be done as briefed;
   - a decision that changes the product (behaviour, output, a dropped criterion, a
     different storage format than the brief allows);
   - one of the plan's open decisions for the user (section 7: D1, D2, D3) is needed;
   - the step's brief says it may be skipped and the evidence says it should be. On the
     user's go-ahead, set the row to `skipped` with the reason, commit the planner's
     file as `step NN: <title> (skipped)`, and continue.
2. **Implement.** Dispatch `step-implementer` with the step number and the plan's path.
   If it stops because the plan is wrong, send its findings to a new `step-planner` to
   revise the plan, then implement again.
3. **Verify.** Dispatch `step-verifier` with the step number and the plan's path. Do not
   pass it the implementer's reply or your own view of the work.
4. **On `VERDICT: FAIL`,** send the blocking findings to the implementer, by continuing
   the same agent if it is still addressable, otherwise to a new one, then verify again
   with a fresh verifier. After two failed rounds, stop the loop and bring the findings
   to the user.
5. **On `VERDICT: PASS`,** set the step to `done` in the status table and commit it as one
   commit titled `step NN: <title>`. Stage the files the step changed by name
   (`git add <paths>`), never `git add -A` or `git add .`; check `git status` afterwards
   for anything left over and stop and ask if something unexpected remains.
6. **Pause points.** If the step's brief contains "Then pause for the user", stop the
   loop after committing and show the user exactly what the brief asks them to do.
7. **Report and continue.** Give the user a short report for the step: the verdict, the
   commit, the verifier's notes, the mutation checks and their results, and the
   unverified items. Then, unless `--one` was given, move on to the next step: the first
   `todo` row in the status table whose dependencies are all `done` or `skipped`. Stop
   when no such step remains.

Stop the loop early for anything that needs the user: a planner question (step 1), two
failed verify rounds, a pause point, a step that cannot be verified without something the
sandbox lacks and the plan does not cover, or the user asking to stop. When the loop
ends, say why it ended and which steps are now unblocked.
