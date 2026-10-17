"""Accounting regressions checked against ccusage 20.0.34 and provider rules.

The checked-in reference cases are synthetic, contain no conversation text,
and run offline without Node, npm, or a network connection.
"""
import copy
import io
import itertools
import json
from pathlib import Path
from unittest.mock import patch

import pytest

import cctrack


FIXTURES = json.loads((Path(__file__).parent / 'fixtures/ccusage-parity.json').read_text())


@pytest.mark.parametrize('case', FIXTURES['cases'], ids=lambda c: c['name'])
def test_pinned_ccusage_reference_cases(case, tmp_path):
    files = []
    for i, rows in enumerate(case['files']):
        path = tmp_path / f'rollout-{i}.jsonl'
        path.write_text('\n'.join(json.dumps(r) for r in rows) + '\n')
        files.append(str(path))
    events = (cctrack.parse_codex_events(files)[0] if case['agent'] == 'codex'
              else cctrack.parse_events(files))
    daily, _ = cctrack.aggregate(events)
    assert sum(cctrack.total_tokens(v) for v in daily.values()) == case['expected_tokens']
    assert sum(v['cost'] for v in daily.values()) == pytest.approx(case['expected_cost_usd'])
    # Detailed JSON/CSV and explore must use the same pricing as aggregate.
    assert sum(sum(cctrack._event_cost_breakdown(e, cctrack._event_date(e)).values())
               for e in events) == pytest.approx(case['expected_cost_usd'])
    assert sum(r['cost_usd'] for r in cctrack.build_session_rows(events, roots=[])) == pytest.approx(
        case['expected_cost_usd'], abs=1e-6)


def claude(mid='msg_1', rid='req_1', sid='s', inp=1000, out=100, **kw):
    row = {'type': 'assistant', 'sessionId': sid, 'requestId': rid,
           'timestamp': '2026-10-10T10:00:00Z', 'cwd': '/work/repo',
           'message': {'id': mid, 'model': 'claude-sonnet-4',
                       'usage': {'input_tokens': inp, 'output_tokens': out}}}
    row.update(kw)
    return row


def codex(kind, payload, second=0):
    return json.dumps({'type': kind, 'payload': payload,
                       'timestamp': f'2026-10-10T10:00:{second:02d}Z'})


def counts(inp=1000, out=100):
    return {'input_tokens': inp, 'output_tokens': out, 'total_tokens': inp + out}


def tc(last=None, total=None, second=1):
    return codex('event_msg', {'type': 'token_count', 'info': {
        **({'last_token_usage': last} if last is not None else {}),
        **({'total_token_usage': total} if total is not None else {})}}, second)


def test_fullest_claude_snapshot_wins_in_either_order():
    for rows in itertools.permutations([claude(out=1000), claude(out=1)]):
        [event] = cctrack.parse_lines([json.dumps(r) for r in rows])
        assert event['output'] == 1000


def test_gateway_message_ids_scoped_by_session_and_timestamp():
    rows = [claude('gateway-id', rid=None, sid='a'), claude('gateway-id', rid=None, sid='b'),
            claude('gateway-id', rid=None, sid='a', timestamp='2026-10-10T10:01:00Z')]
    assert len(cctrack.parse_lines([json.dumps(r) for r in rows])) == 3


def test_sidechain_replay_with_new_request_id_keeps_parent():
    rows = [claude(rid='parent'), claude(rid='new', isSidechain=True, agentId='a')]
    for order in itertools.permutations(rows):
        [e] = cctrack.parse_lines([json.dumps(r) for r in order])
        assert not e['is_sidechain']


def test_agent_progress_usage_and_normal_iterations_are_not_double_counted():
    nested = claude()
    nested['message']['usage']['iterations'] = [{'type': 'message', 'model': 'claude-sonnet-4',
                                                'input_tokens': 1000, 'output_tokens': 100}]
    progress = {'type': 'progress', 'sessionId': 's', 'data': {'message': nested}}
    assert len(cctrack.parse_lines([json.dumps(progress), json.dumps(nested)])) == 1


def test_total_only_codex_events_use_counter_differences_and_skip_reemissions():
    rows = [tc(total=counts(), second=1), tc(total=counts(), second=2),
            tc(total=counts(1500, 150), second=3)]
    events = cctrack.parse_codex_lines(rows)
    assert [e['input'] for e in events] == [1000, 500]
    assert [e['output'] for e in events] == [100, 50]


def test_identical_codex_last_usage_with_different_totals_is_real_spend():
    rows = [tc(counts(), counts(), 1), tc(counts(), counts(2000, 200), 2)]
    assert len(cctrack.parse_codex_lines(rows)) == 2


def test_codex_record_does_not_swallow_identical_later_unrelated_turn():
    rows = [codex('session_meta', {'id': 's'}),
            codex('token_usage_record', {'response_id': 'r', 'usage': counts(),
                                        'thread_token_usage': counts()}, 1),
            tc(counts(), counts(), 2), tc(counts(), counts(2000, 200), 3)]
    assert len(cctrack.parse_codex_lines(rows)) == 2


def test_codex_record_after_snapshot_replaces_only_matching_usage():
    rows = [codex('session_meta', {'id': 's'}), tc(counts(), counts(), 1),
            codex('token_usage_record', {'response_id': 'r', 'model': 'gpt-6-astra',
                                        'usage': counts(), 'thread_token_usage': counts()}, 2),
            tc(counts(200, 20), counts(1200, 120), 3)]
    events = cctrack.parse_codex_lines(rows)
    assert len(events) == 2
    assert events[0]['model'] == 'gpt-6-astra'
    assert sum(e['input'] for e in events) == 1200


def test_codex_snapshot_keys_do_not_collide_between_threads():
    groups = [cctrack.parse_codex_lines([codex('session_meta', {'id': sid}), tc(counts(), counts())])
              for sid in ('a', 'b')]
    assert len(cctrack.dedup_events(groups)) == 2


def test_codex_replayed_fork_dedup_is_idempotent_and_order_independent():
    parent = cctrack.parse_codex_lines([codex('session_meta', {'id': 'p'}), tc(counts(), counts())])
    child = cctrack.parse_codex_lines([
        codex('session_meta', {'id': 'c', 'forked_from_id': 'p'}),
        tc(counts(), counts(), 10), tc(counts(), counts(2000, 200), 20)])
    for order in itertools.permutations([parent, child]):
        events = cctrack.dedup_events(copy.deepcopy(order))
        assert len(events) == 2  # the child's NEW identical request must survive
        assert len(cctrack.dedup_events([events])) == 2


def test_fork_with_missing_parent_keeps_uncertain_usage():
    rows = [codex('session_meta', {'id': 'c', 'forked_from_id': 'missing'}), tc(counts(), counts())]
    assert len(cctrack.parse_codex_lines(rows)) == 1


def test_service_tier_changes_and_per_record_model_do_not_change_surrounding_turn():
    rows = [codex('turn_context', {'model': 'gpt-6.1-sol'}),
            codex('event_msg', {'type': 'thread_settings_applied',
                                'thread_settings': {'service_tier': 'priority'}}),
            codex('token_usage_record', {'response_id': 'r1', 'model': 'gpt-6-astra',
                                        'usage': counts()}, 1),
            codex('event_msg', {'type': 'thread_settings_applied',
                                'thread_settings': {'service_tier': 'default'}}, 2),
            codex('token_usage_record', {'response_id': 'r2', 'usage': counts()}, 3)]
    events = cctrack.parse_codex_lines(rows)
    assert [(e['model'], e['service_tier']) for e in events] == [
        ('gpt-6-astra', 'fast'), ('gpt-6.1-sol', 'standard')]
    assert sum(sum(cctrack._event_cost_breakdown(e, '2026-10-10').values())
               for e in events) == pytest.approx(.033)


@pytest.mark.parametrize('model,base', [('claude-3-haiku-20240307', .25),
    ('claude-3-5-haiku-20241022', .8), ('gpt-5.1-codex-mini-2025-11-13', .25),
    ('gpt-5.1-codex-max', 1.25), ('gpt-5.6-2026-10-01', 4)])
def test_explicit_model_aliases(model, base):
    assert cctrack.get_rates(model)['input'] == base
    assert cctrack.is_known_model(model)


@pytest.mark.parametrize('model,threshold', [('claude-sonnet-4', 200000),
    ('claude-sonnet-4-5', 200000), ('claude-haiku-5-5', 100000), ('gpt-6.1-sol', 272000)])
def test_context_threshold_is_per_request_includes_caches_and_is_strict(model, threshold):
    normal = cctrack.get_rates(model, request_input=threshold)
    above = cctrack.get_rates(model, request_input=threshold + 1)
    assert above['input'] > normal['input']
    if model.startswith('claude'):
        r = claude(inp=1, out=1)
        r['message']['model'] = model
        r['message']['usage']['cache_read_input_tokens'] = threshold
        [e] = cctrack.parse_lines([json.dumps(r)])
        assert cctrack._event_cost_breakdown(e, '2026-10-10')['output'] == pytest.approx(above['output'] / 1e6)


def test_utc_buckets_and_explore_agree_on_offset_timestamps():
    [e] = cctrack.parse_lines([json.dumps(claude(timestamp='2026-10-11T00:30:00+02:00'))])
    daily, _ = cctrack.aggregate([e])
    assert list(daily) == ['2026-10-10']
    assert cctrack._event_date(e) == '2026-10-10'


def source(name, agent='claude', **kw):
    return {'name': name, 'agent': agent, 'daily': {}, 'hook_daily': {},
            'install_date': None, 'deltas': [], **kw}


def test_copied_usage_across_sandboxes_and_hosts_counted_once_with_locations(tmp_path):
    local = tmp_path / 'sandboxes/a/claude/projects/p/s.jsonl'
    remote = '/home/u/.sandy/sandboxes/b/claude/projects/p/s.jsonl'
    local.parent.mkdir(parents=True)
    local.write_text(json.dumps(claude()) + '\n')
    events = cctrack.parse_events([str(local)])
    with patch('cctrack.fetch_remote_jsonl', return_value=(
            f'___CCTRACK_FILE_SEP___{remote}\n{json.dumps(claude())}\n')):
        events += cctrack.parse_remote_events('air')
    sources = [source('local'), source('air')]
    events = cctrack.reconcile_sources(events, sources)
    assert len(events) == 1
    assert sum(sum(d['cost'] for d in s['daily'].values()) for s in sources) == pytest.approx(.0045)
    [row] = cctrack.build_session_rows(events, roots=[])
    assert len(row['source_locations']) == 2
    assert set(row['source_files']) == {str(local), f'air:{remote}'}
    assert {loc['host'] for loc in row['source_locations']} == {'local', 'air'}
    # The same rules drive the main table, not only detailed JSON totals.
    out = io.StringIO()
    with patch('sys.stdout', out):
        daily, monthly = cctrack.aggregate(events)
        cctrack.print_report(daily, monthly, sources=sources)
    assert '$' in out.getvalue()


def test_global_codex_snapshot_and_response_copies_merge_and_keep_tier():
    header = codex('session_meta', {'id': 's'})
    local = cctrack.parse_codex_lines([header,
        codex('turn_context', {'model': 'gpt-6.1-sol'}),
        codex('token_usage_record', {'response_id': 'r', 'usage': counts()}, 1),
        tc(counts(), counts(), 2)])
    remote = cctrack.parse_codex_lines([header,
        codex('turn_context', {'model': 'gpt-6.1-sol', 'service_tier': 'priority'}),
        tc(counts(), counts(), 2)], source='air')
    sources = [source('local', 'codex', exact=True), source('air', 'codex', exact=True)]
    events = cctrack.reconcile_sources(local + remote, sources)
    assert len(events) == 1
    assert events[0]['service_tier'] == 'fast'
    assert sum(sum(d['cost'] for d in s['daily'].values()) for s in sources) == pytest.approx(.006)


def test_copied_hook_histories_are_differenced_globally():
    rows = [{'session_id': 's', 'date_str': '2026-10-10', 'model': 'claude-sonnet-4',
             'ts': f'2026-10-10T10:00:0{i}Z', 'cost_usd': v} for i, v in enumerate((0, 2, 5))]
    sources = [source('local', _hook_rows=rows), source('air', _hook_rows=copy.deepcopy(rows))]
    cctrack.reconcile_sources([], sources)
    assert sum(sum(d['cost'] for d in s['hook_daily'].values()) for s in sources) == 5


def test_disjoint_hook_history_after_moving_host_keeps_real_increments():
    rows = [{'session_id': 's', 'date_str': '2026-10-10', 'model': 'claude-sonnet-4',
             'ts': f'2026-10-10T10:00:0{i}Z', 'cost_usd': v} for i, v in enumerate((0, 2, 5, 9))]
    sources = [source('local', _hook_rows=rows[:2]), source('air', _hook_rows=rows[2:])]
    cctrack.reconcile_sources([], sources)
    assert sources[0]['hook_daily']['2026-10-10']['cost'] == 2
    assert sources[1]['hook_daily']['2026-10-10']['cost'] == 7


@pytest.mark.parametrize('record_first', [True, False])
def test_first_total_only_snapshot_preserves_usage_before_known_record(record_first):
    record = codex('token_usage_record', {'response_id': 'r', 'usage': counts(300, 30),
                                        'thread_token_usage': counts(400, 50)}, 2)
    snapshot = tc(total=counts(400, 50), second=3)
    rows = [codex('session_meta', {'id': 's'}),
            *([record, snapshot] if record_first else [snapshot, record])]
    events = cctrack.parse_codex_lines(rows)
    assert sum(e['input'] for e in events) == 400
    assert sum(e['output'] for e in events) == 50


def test_repeated_completed_record_does_not_hide_next_pending_response():
    old = codex('token_usage_record', {'response_id': 'old', 'usage': counts(100, 20)}, 1)
    new = codex('token_usage_record', {'response_id': 'new', 'usage': counts(300, 30)}, 3)
    rows = [codex('session_meta', {'id': 's'}), old, tc(total=counts(100, 20), second=2),
            new, old, tc(total=counts(400, 50), second=5)]
    events = cctrack.parse_codex_lines(rows)
    assert sum(e['input'] for e in events) == 400
    assert sum(e['output'] for e in events) == 50


def test_invalid_record_does_not_consume_a_valid_snapshot():
    record = json.dumps({'type': 'token_usage_record', 'payload': {
        'response_id': 'bad', 'usage': counts()}})
    events = cctrack.parse_codex_lines([record, tc(counts(), counts())])
    assert len(events) == 1
    assert events[0]['input'] == 1000


def test_split_cumulative_snapshot_and_plain_remote_copy_do_not_double_count():
    header = codex('session_meta', {'id': 's'})
    local = cctrack.parse_codex_lines([header,
        codex('token_usage_record', {'response_id': 'r', 'usage': counts(300, 30),
                                    'thread_token_usage': counts(400, 50)}, 1),
        tc(total=counts(400, 50), second=2)])
    remote = cctrack.parse_codex_lines([header, tc(total=counts(400, 50), second=2)], source='air')
    for groups in itertools.permutations([local, remote]):
        events = cctrack.dedup_events(copy.deepcopy(groups))
        assert sum(e['input'] for e in events) == 400
        assert sum(e['output'] for e in events) == 50
        assert sum(e['input'] for e in cctrack.dedup_events([events])) == 400


def test_previously_deduped_fork_and_raw_remote_copy_merge_idempotently():
    parent = cctrack.parse_codex_lines([codex('session_meta', {'id': 'p'}), tc(counts(), counts())])
    child_rows = [codex('session_meta', {'id': 'c', 'forked_from_id': 'p'}),
                  tc(counts(), counts(), 10), tc(counts(), counts(2000, 200), 20)]
    local = cctrack.dedup_events([parent, cctrack.parse_codex_lines(child_rows)])
    remote = cctrack.parse_codex_lines(child_rows, source='air')
    for groups in itertools.permutations([local, remote]):
        events = cctrack.dedup_events(copy.deepcopy(groups))
        assert sum(e['input'] for e in events) == 2000


def test_nested_fork_can_match_a_parent_whose_replay_was_already_removed():
    parent = cctrack.parse_codex_lines([codex('session_meta', {'id': 'p'}), tc(counts(), counts())])
    child = cctrack.parse_codex_lines([codex('session_meta', {'id': 'c', 'forked_from_id': 'p'}),
            tc(counts(), counts(), 10), tc(counts(200, 20), counts(1200, 120), 20)])
    local = cctrack.dedup_events([parent, child])
    grandchild = cctrack.parse_codex_lines([
        codex('session_meta', {'id': 'gc', 'forked_from_id': 'c'}),
        tc(counts(), counts(), 30), tc(counts(200, 20), counts(1200, 120), 31),
        tc(counts(50, 5), counts(1250, 125), 40)])
    events = cctrack.dedup_events([local, grandchild])
    assert sum(e['input'] for e in events) == 1250


def test_advisor_usage_survives_a_zero_usage_parent():
    row = claude(inp=0, out=0)
    row['message']['usage']['iterations'] = [{'type': 'advisor_message',
        'model': 'claude-opus-4-6', 'input_tokens': 1000, 'output_tokens': 100}]
    [event] = cctrack.parse_lines([json.dumps(row)])
    assert event['model'] == 'claude-opus-4-6'


def test_unrecognized_alias_suffix_does_not_inherit_a_price():
    assert not cctrack.is_known_model('gpt-5.6-cyber')
    assert not cctrack.is_known_model('gpt-5.1-codex-mini-super')


def test_coverage_distinguishes_copied_logs_and_unresolved_forks():
    local = cctrack.parse_lines([json.dumps(claude())])
    remote = cctrack.parse_lines([json.dumps(claude())], source='air')
    sources = [source('local'), source('air')]
    events = cctrack.reconcile_sources(local + remote, sources)
    cov = cctrack.build_coverage({}, events, sources, cctrack.parse_utc('2026-10-10'),
                                  cctrack.parse_utc('2026-10-11'), roots=[])
    assert all(g['days_without_records'] == [] for g in cov['gaps'])
    orphan = cctrack.parse_codex_lines([codex('session_meta', {'id': 'c', 'forked_from_id': 'missing'}),
                                       tc(counts(), counts())])
    cov = cctrack.build_coverage({}, orphan, [source('local', 'codex')], None, None, roots=[])
    assert cov['unresolved_codex_forks'][0]['session_id'] == 'c'


def test_report_exports_recorded_service_tiers_and_redacts_all_copy_paths():
    e = cctrack.parse_lines([json.dumps(claude())])[0]
    e['service_tier'] = 'fast'
    e['_locations'] = [{'host': 'air', 'path': '/secret/copied.jsonl',
                        'session_id': 's', 'cwd': '/secret/project'}]
    [row] = cctrack.build_session_rows([e], roots=[], redact=cctrack.make_redactor(True))
    assert '/secret' not in json.dumps(row)
    assert row['service_tiers'] == {'fast': 1}
    out = io.StringIO()
    cctrack.write_csv([row], cctrack.SESSION_CSV_FIELDS, out)
    assert 'source_locations' in out.getvalue() and 'service_tiers' in out.getvalue()


@pytest.mark.parametrize('detail', [True, False])
def test_cli_reconciles_sandbox_and_remote_costs_before_reporting(tmp_path, monkeypatch, capsys, detail):
    from datetime import datetime, timezone
    timestamp = datetime.now(timezone.utc).isoformat()
    row = claude(inp=100000, out=10000, timestamp=timestamp)
    directory = tmp_path / 'sandboxes/local/claude/projects/p'
    directory.mkdir(parents=True)
    (directory / 's.jsonl').write_text(json.dumps(row) + '\n')
    monkeypatch.setattr(cctrack, 'default_dirs', lambda: [str(directory)])
    monkeypatch.setattr(cctrack, 'sandbox_roots', lambda: [])
    monkeypatch.setattr(cctrack, 'discover_hook_data_dirs', lambda: [])
    monkeypatch.setattr(cctrack, 'read_hook_config', lambda: None)
    monkeypatch.setattr(cctrack, 'is_hook_installed', lambda: True)
    monkeypatch.setattr(cctrack, 'fetch_remote_jsonl', lambda host:
                        '___CCTRACK_FILE_SEP___/sandboxes/copy/claude/projects/p/s.jsonl\n'
                        + json.dumps(row) + '\n')
    monkeypatch.setattr(cctrack, 'parse_remote_hook_data', lambda host: ([], {}, {}))
    monkeypatch.setattr('sys.argv', ['cctrack', '--agent', 'claude', '--remote', 'air']
                        + (['--json'] if detail else []))
    cctrack.main()
    out = capsys.readouterr().out
    if detail:
        doc = json.loads(out)
        assert sum(s['cost_usd'] for s in doc['sessions']) == pytest.approx(.45)
        assert len(doc['sessions'][0]['source_locations']) == 2
        assert sum(b['cost_usd'] for b in doc['buckets']) == pytest.approx(.45)
    else:
        import re
        assert re.search(r'\$\s*0\.45\b', out)
        assert not re.search(r'\$\s*0\.90\b', out)


def test_missing_codex_model_never_borrows_claude_rates():
    [event] = cctrack.parse_codex_lines([tc(counts(), counts())])
    assert event['model'] == 'codex-unknown'
    assert not cctrack.is_known_model(event['model'])
    daily, _ = cctrack.aggregate([event])
    assert daily['2026-10-10']['cost'] == pytest.approx(.00225)
    [session] = cctrack.build_session_rows([event], roots=[])
    assert session['service_tiers'] == {'unknown': 1}
