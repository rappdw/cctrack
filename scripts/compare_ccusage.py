#!/usr/bin/env python3
"""Run synthetic accounting fixtures against the pinned ccusage executable.

Install ccusage@20.0.34 separately, then run:
    PYTHONPATH=src python3 scripts/compare_ccusage.py --ccusage /path/to/ccusage

The normal test suite needs neither ccusage nor network access. This optional
check verifies that its frozen reference values still match the pinned binary.
"""
import argparse
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile

import cctrack


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ccusage', default='ccusage', help='Path to ccusage 20.0.34 executable')
    args = parser.parse_args()
    fixture = json.loads((Path(__file__).resolve().parents[1]
                         / 'tests/fixtures/ccusage-parity.json').read_text())
    expected_version = fixture['reference']['package'].split('@')[-1]
    version = subprocess.run([args.ccusage, '--version'], check=True, text=True,
                             capture_output=True).stdout.strip()
    if version != f'ccusage {expected_version}':
        parser.error(f'reference requires ccusage {expected_version}; found {version!r}')
    failures = []
    with tempfile.TemporaryDirectory(prefix='cctrack-parity-') as tmp:
        for case in fixture['cases']:
            home = Path(tmp) / case['name']
            agent = case['agent']
            directory = home / ('projects/p' if agent == 'claude' else 'sessions')
            directory.mkdir(parents=True)
            files = []
            for i, rows in enumerate(case['files']):
                path = directory / f'rollout-{i}.jsonl'
                path.write_text('\n'.join(json.dumps(r, separators=(',', ':')) for r in rows) + '\n')
                files.append(str(path))
            env = dict(os.environ)
            # Isolate all config/data discovery from the user's real usage.
            env.update(HOME=str(home), CLAUDE_CONFIG_DIR=str(home), CODEX_HOME=str(home))
            env.pop('CCUSAGE_MODEL_ALIASES', None)
            command = [args.ccusage, agent, 'daily', '--offline', '--timezone', 'UTC', '--json']
            if agent == 'claude':
                command += ['--mode', 'calculate']
            result = subprocess.run(command, env=env, cwd=home, check=True, text=True, capture_output=True)
            totals = json.loads(result.stdout)['totals']
            reference_cost = totals['totalCost' if agent == 'claude' else 'costUSD']
            events = cctrack.parse_codex_events(files)[0] if agent == 'codex' else cctrack.parse_events(files)
            daily, _ = cctrack.aggregate(events)
            cost = sum(v['cost'] for v in daily.values())
            tokens = sum(cctrack.total_tokens(v) for v in daily.values())
            expected_reference = {'claude_long': 1.215, 'claude_unknown': 0}.get(
                case['name'], case['expected_cost_usd'])
            passed = (tokens == totals['totalTokens'] == case['expected_tokens']
                      and math.isclose(cost, case['expected_cost_usd'], abs_tol=1e-9)
                      and math.isclose(reference_cost, expected_reference, abs_tol=1e-9))
            print(f"{'PASS' if passed else 'FAIL'} {case['name']}: "
                  f"{tokens} tokens, cctrack=${cost:.6f}, ccusage=${reference_cost:.6f}")
            if not passed:
                failures.append(case['name'])
    if failures:
        raise SystemExit(f"Accounting comparison failed: {', '.join(failures)}")
    print(f"Verified {len(fixture['cases'])} cases against {version}.")


if __name__ == '__main__':
    main()
