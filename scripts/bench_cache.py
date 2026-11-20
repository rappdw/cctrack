#!/usr/bin/env python3
"""Synthetic benchmark for cctrack: builds a reproducible fake home (Claude
transcripts, Codex rollouts, statusline hook files and optionally a fake
`--remote` host reached through a fake ssh), runs cctrack N times with
CCTRACK_TIMING=1 and reports wall time, the phase timings and a hash of stdout.

Stdlib only; does not import cctrack. Usage: see --help."""

import argparse
import hashlib
import json
import os
import random
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TIMING_RE = re.compile(
    r"^\[timing\] (?P<indent> *)(?P<name>[^ #].*?) +(?P<secs>\d+\.\d{3}) s(?:  (?P<detail>.*))?$")
CORPUS_FILE = "bench-corpus.json"


def _pad(rng_text: str, target: int, used: int) -> str:
    return rng_text * max(0, (target - used) // max(1, len(rng_text)) + 1)


def _ts(rng: random.Random, today, days: int, n: int, i: int) -> str:
    d = today - timedelta(days=rng.randrange(days))
    sec = (i * 7 + n) % 86000
    return f"{d.isoformat()}T{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}Z"


def build_home(home: Path, o, seed: int, tag: str, today) -> dict:
    rng = random.Random(seed)
    (home / ".sandy" / "sandboxes").mkdir(parents=True, exist_ok=True)
    text = "lorem ipsum dolor sit amet " * 4
    stats = {"claude_files": 0, "claude_events": 0, "codex_files": 0, "codex_events": 0,
             "hook_files": 0, "bytes": 0}
    for k in range(o.claude_files):
        d = home / ".claude" / "projects" / f"-bench-{k}"
        d.mkdir(parents=True, exist_ok=True)
        lines = []
        for i in range(o.claude_events):
            ts = _ts(rng, today, o.days, k, i)
            ev = {"type": "assistant", "timestamp": ts, "requestId": f"req_{tag}_{k}_{i}",
                  "sessionId": f"sess-{tag}-{k}", "cwd": f"/bench/{k}", "gitBranch": "main",
                  "message": {"id": f"msg_{tag}_{k}_{i}", "model": "claude-opus-5-5",
                              "usage": {"input_tokens": rng.randrange(1, 5000),
                                        "output_tokens": rng.randrange(1, 4000),
                                        "cache_read_input_tokens": rng.randrange(0, 150000),
                                        "cache_creation": {"ephemeral_5m_input_tokens": 0,
                                                           "ephemeral_1h_input_tokens":
                                                               rng.randrange(0, 2000)}},
                              "content": [{"type": "text", "text": ""}]}}
            base = len(json.dumps(ev))
            ev["message"]["content"][0]["text"] = _pad(text, o.line_bytes, base)[:max(0, o.line_bytes - base)]
            lines.append(json.dumps(ev))
            for j in range(o.filler_lines):
                fl = {"type": "user", "timestamp": ts, "sessionId": f"sess-{tag}-{k}",
                      "message": {"role": "user", "content": ""}}
                fb = len(json.dumps(fl))
                fl["message"]["content"] = _pad(text, o.line_bytes, fb)[:max(0, o.line_bytes - fb)]
                lines.append(json.dumps(fl))
        body = "\n".join(lines) + "\n"
        (d / f"s{k}.jsonl").write_text(body)
        stats["claude_files"] += 1
        stats["claude_events"] += o.claude_events
        stats["bytes"] += len(body)
    for k in range(o.codex_files):
        day = today - timedelta(days=rng.randrange(o.days))
        d = home / ".codex" / "sessions" / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}"
        d.mkdir(parents=True, exist_ok=True)
        tid = f"bench-{tag}-{k}"
        t0 = f"{day.isoformat()}T01:00:00Z"
        lines = [json.dumps({"timestamp": t0, "type": "session_meta",
                             "payload": {"id": tid, "session_id": tid, "cwd": f"/bench/{k}"}}),
                 json.dumps({"timestamp": t0, "type": "turn_context", "payload": {"model": "gpt-5.4"}})]
        tot = {"input_tokens": 0, "cached_input_tokens": 0, "cache_write_input_tokens": 0,
               "output_tokens": 0, "total_tokens": 0}
        for i in range(o.codex_events):
            inp, out = rng.randrange(100, 9000), rng.randrange(10, 900)
            u = {"input_tokens": inp, "cached_input_tokens": inp // 2, "cache_write_input_tokens": 0,
                 "output_tokens": out, "total_tokens": inp + out}
            for key in tot:
                tot[key] += u[key]
            ts = f"{day.isoformat()}T02:{i // 60 % 60:02d}:{i % 60:02d}Z"
            lines.append(json.dumps({"timestamp": ts, "type": "token_usage_record", "payload": {
                "thread_id": tid, "session_id": tid, "response_id": f"resp-{tag}-{k}-{i}", "usage": u}}))
            lines.append(json.dumps({"timestamp": ts, "type": "event_msg", "payload": {
                "type": "token_count", "info": {"last_token_usage": u, "total_token_usage": dict(tot),
                                                "model_context_window": 400000}}}))
            for j in range(o.filler_lines):
                fl = {"timestamp": ts, "type": "response_item", "payload": {"text": ""}}
                fb = len(json.dumps(fl))
                fl["payload"]["text"] = _pad(text, o.line_bytes, fb)[:max(0, o.line_bytes - fb)]
                lines.append(json.dumps(fl))
        body = "\n".join(lines) + "\n"
        (d / f"rollout-{day.isoformat()}T01-00-00-{tid}.jsonl").write_text(body)
        stats["codex_files"] += 1
        stats["codex_events"] += o.codex_events
        stats["bytes"] += len(body)
    hook = home / ".claude" / "cctrack"
    for n in range(min(o.hook_files, 89)):
        day = today - timedelta(days=n)
        hook.mkdir(parents=True, exist_ok=True)
        cost: dict = {}
        rows = []
        for i in range(o.hook_rows):
            sid = f"hs-{tag}-{n}-{i % 5}"
            cost[sid] = cost.get(sid, 0.0) + (0.0 if sid not in cost else rng.random() * 0.05)
            sec = (i * 13) % 86000
            rows.append(json.dumps({"_ts": f"{day.isoformat()}T{sec // 3600:02d}:{sec % 3600 // 60:02d}:"
                                           f"{sec % 60:02d}+00:00",
                                    "session_id": sid, "cost_usd": round(cost[sid], 4),
                                    "model": "claude-opus-5-5"}))
        body = "\n".join(rows) + "\n"
        (hook / f"statusline-{day.isoformat()}.jsonl").write_text(body)
        stats["hook_files"] += 1
        stats["bytes"] += len(body)
    return stats


def write_fake_ssh(bin_dir: Path, remote_home: Path, python: str) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "ssh"
    env = {"HOME": str(remote_home), "SANDY_HOME": str(remote_home / ".sandy")}
    exports = "".join(f'export {k}="{v}"\n' for k, v in env.items())
    script.write_text(
        "#!/bin/bash\n" + exports + "unset CODEX_HOME\n"
        "while [ $# -gt 0 ]; do case \"$1\" in -o) shift 2;; -*) shift;; *) break;; esac; done\n"
        "shift  # host\n"
        f"if [ \"$*\" = 'python3 -' ]; then exec {python} -; fi\n"
        "exec bash -c \"$*\"\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)


def parse_timing(stderr: str) -> dict:
    phases, sub, details = {}, {}, {}
    parent = None
    for ln in stderr.splitlines():
        m = TIMING_RE.match(ln)
        if not m:
            continue
        secs = float(m["secs"])
        if m["indent"]:
            sub.setdefault(parent, {})[m["name"]] = secs
        else:
            parent = m["name"]
            phases[parent] = secs
            if m["detail"]:
                details[parent] = m["detail"]
    return {"phases": phases, "sub": sub, "details": details}


def main() -> int:
    ap = argparse.ArgumentParser(description="Benchmark cctrack on a synthetic corpus.")
    ap.add_argument("--claude-files", type=int, default=200)
    ap.add_argument("--claude-events", type=int, default=100)
    ap.add_argument("--line-bytes", type=int, default=2800)
    ap.add_argument("--filler-lines", type=int, default=2)
    ap.add_argument("--codex-files", type=int, default=50)
    ap.add_argument("--codex-events", type=int, default=100)
    ap.add_argument("--hook-files", type=int, default=30)
    ap.add_argument("--hook-rows", type=int, default=300)
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--remote", action="store_true", help="also build a fake host 'benchhost'")
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--dir", help="build in DIR and keep it")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--json", action="store_true")
    o, extra = ap.parse_known_args()
    if extra and extra[0] == "--":
        extra = extra[1:]

    today = datetime.now(timezone.utc).date()
    opts = {k: v for k, v in vars(o).items() if k not in ("dir", "runs", "json")}
    opts["anchor"] = today.isoformat()
    cleanup = None
    if o.dir:
        root = Path(o.dir).resolve()
        if root.exists() and any(root.iterdir()) and not (root / CORPUS_FILE).exists():
            print(f"error: {root} is not empty and has no {CORPUS_FILE}; refusing to use it",
                  file=sys.stderr)
            return 2
    else:
        root = Path(tempfile.mkdtemp(prefix="cctrack-bench-"))
        cleanup = root
    try:
        marker = root / CORPUS_FILE
        corpus = None
        if marker.exists():
            try:
                saved = json.loads(marker.read_text())
                if saved.get("opts") == opts:
                    corpus = saved["stats"]
            except (OSError, ValueError):
                pass
            if corpus is None:
                for sub in ("home", "remote-home", "bin", "xdg-cache"):
                    shutil.rmtree(root / sub, ignore_errors=True)
        if corpus is None:
            root.mkdir(parents=True, exist_ok=True)
            corpus = {"local": build_home(root / "home", o, o.seed, "l", today)}
            if o.remote:
                corpus["remote"] = build_home(root / "remote-home", o, o.seed + 1, "r", today)
                write_fake_ssh(root / "bin", root / "remote-home", o.python)
            marker.write_text(json.dumps({"opts": opts, "stats": corpus}))
        env = {"HOME": str(root / "home"), "SANDY_HOME": str(root / "home" / ".sandy"),
               "PATH": f"{root / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}",
               "PYTHONPATH": str(REPO / "src"), "CCTRACK_TIMING": "1", "CCTRACK_NO_PROGRESS": "1",
               "PYTHONIOENCODING": "utf-8", "XDG_CACHE_HOME": str(root / "xdg-cache")}
        argv = [o.python, "-m", "cctrack"] + (["--remote", "benchhost"] if o.remote else []) + extra
        runs = []
        for _ in range(o.runs):
            t = time.perf_counter()
            r = subprocess.run(argv, cwd=root, env=env, stdin=subprocess.DEVNULL,
                               capture_output=True)
            wall = time.perf_counter() - t
            err = r.stderr.decode("utf-8", "replace")
            if r.returncode != 0:
                print(f"error: cctrack exited {r.returncode}:\n{err}", file=sys.stderr)
                return 1
            runs.append({"wall": wall, "stdout_sha256": hashlib.sha256(r.stdout).hexdigest(),
                         "rc": r.returncode, "stderr": err, **parse_timing(err)})
        if o.json:
            print(json.dumps({"corpus": corpus,
                              "runs": [{k: v for k, v in r.items() if k not in ("stderr", "rc")}
                                       for r in runs]}, indent=2))
        else:
            for kind, st in corpus.items():
                print(f"corpus {kind}: {st['claude_files']} Claude files "
                      f"({st['claude_events']:,} events), {st['codex_files']} Codex files "
                      f"({st['codex_events']:,} events), {st['hook_files']} hook files, "
                      f"{st['bytes'] / 1e6:.1f} MB")
            for n, r in enumerate(runs, 1):
                print(f"run {n}: wall {r['wall']:.2f} s  stdout sha256 "
                      f"{r['stdout_sha256'][:8]}...  (rc {r['rc']})")
                for ln in r["stderr"].splitlines():
                    if ln.startswith("[timing]"):
                        print(ln)
        return 0
    finally:
        if cleanup:
            shutil.rmtree(cleanup, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
