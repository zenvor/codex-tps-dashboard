import json
import pathlib
import tempfile
import unittest
import sqlite3

from codex_tps import (analyze_turn, parse_rollout, parse_rollouts, parse_logs,
                       aggregate_models, summarize_turns, Store)

ID = '00000000-0000-4000-8000-000000000001'


def turn(usage=None, end=20, first_visible_delay_s=None, outcome=None):
    return {'turn_id': ID, 'start': 0, 'end': end, 'model': 'test', 'effort': 'medium',
            'duration_s': None, 'first_visible_delay_s': first_visible_delay_s,
            'outcome': outcome, 'usage': usage or [
                {'output_tokens': 110, 'reasoning_output_tokens': 10}]}


def events(tool=False):
    result = [{'time': 2, 'kind': 'request_start'},
              {'time': 8, 'kind': 'item_start', 'item_type': 'message', 'item_id': 'm'},
              {'time': 18, 'kind': 'done', 'item_type': 'message', 'item_id': 'm'}]
    if tool:
        result.append({'time': 19, 'kind': 'done', 'item_type': 'tool', 'item_id': None})
    return result


class MetricsTest(unittest.TestCase):
    def test_store_merges_split_rollouts_for_same_chat(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            sessions = root / 'sessions' / '2026' / '10' / '13'
            sessions.mkdir(parents=True)
            second_id = '00000000-0000-4000-8000-000000000003'
            first = sessions / ('rollout-first-' + ID + '.jsonl')
            second = sessions / ('rollout-second-' + ID + '_' + second_id + '.jsonl')

            def rows(turn_id, start, end, history_base=None):
                return [
                    {'timestamp': start, 'type': 'session_meta', 'ordinal': 0,
                     'payload': {'id': ID, 'history_base': history_base}},
                    {'timestamp': start, 'type': 'event_msg',
                     'ordinal': 1 if history_base is None else 3,
                     'payload': {'type': 'task_started', 'turn_id': turn_id}},
                    {'timestamp': end, 'type': 'event_msg',
                     'ordinal': 2 if history_base is None else 4,
                     'payload': {'type': 'task_complete', 'turn_id': turn_id}}]

            first.write_text('\n'.join(json.dumps(row) for row in rows(
                '00000000-0000-4000-8000-000000000002',
                '2026-10-13T01:00:00Z', '2026-10-13T01:00:10Z')) + '\n')
            second.write_text('\n'.join(json.dumps(row) for row in rows(
                second_id, '2026-10-13T01:01:00Z', '2026-10-13T01:01:10Z',
                {'thread_id': ID, 'end_ordinal_exclusive': 3})) + '\n')
            with sqlite3.connect(root / 'state_5.sqlite') as db:
                db.execute('CREATE TABLE threads (id TEXT, rollout_path TEXT, name TEXT, title TEXT)')
                db.execute('INSERT INTO threads VALUES (?,?,?,?)',
                           (ID, str(second), 'split chat', 'split chat'))
            with sqlite3.connect(root / 'logs_2.sqlite') as db:
                db.execute('CREATE TABLE logs (ts INTEGER, ts_nanos INTEGER, target TEXT, '
                           'feedback_log_body TEXT, thread_id TEXT)')
            stats = Store(root).stats(ID)
            self.assertEqual(len(stats['turns']), 2)
            self.assertEqual(stats['summary']['completed_turns'], 2)

    def test_store_follows_fork_history_into_parent_chat(self):
        parent_id = '00000000-0000-4000-8000-000000000002'
        child_id = '00000000-0000-4000-8000-000000000003'
        parent_turn = '00000000-0000-4000-8000-000000000004'
        child_turn = '00000000-0000-4000-8000-000000000005'
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            sessions = root / 'sessions'
            sessions.mkdir()
            parent = sessions / ('rollout-' + parent_id + '.jsonl')
            child = sessions / ('rollout-' + child_id + '.jsonl')

            def event(turn_id, kind, ordinal, stamp):
                return {'timestamp': stamp, 'type': 'event_msg', 'ordinal': ordinal,
                        'payload': {'type': kind, 'turn_id': turn_id}}

            parent_rows = [
                {'timestamp': '2026-10-13T01:00:00Z', 'type': 'session_meta', 'ordinal': 0,
                 'payload': {'id': parent_id}},
                event(parent_turn, 'task_started', 1, '2026-10-13T01:00:00Z'),
                event(parent_turn, 'task_complete', 2, '2026-10-13T01:00:10Z')]
            child_rows = [
                {'timestamp': '2026-10-13T01:01:00Z', 'type': 'session_meta', 'ordinal': 0,
                 'payload': {'id': child_id, 'history_base': {
                     'thread_id': parent_id, 'end_ordinal_exclusive': 3}}},
                event(child_turn, 'task_started', 3, '2026-10-13T01:01:00Z'),
                event(child_turn, 'task_complete', 4, '2026-10-13T01:01:10Z')]
            parent.write_text('\n'.join(json.dumps(row) for row in parent_rows))
            child.write_text('\n'.join(json.dumps(row) for row in child_rows))
            with sqlite3.connect(root / 'state_5.sqlite') as db:
                db.execute('CREATE TABLE threads (id TEXT, rollout_path TEXT, name TEXT, title TEXT)')
                db.executemany('INSERT INTO threads VALUES (?,?,?,?)', [
                    (parent_id, str(parent), 'parent', 'parent'),
                    (child_id, str(child), 'child', 'child')])
            with sqlite3.connect(root / 'logs_2.sqlite') as db:
                db.execute('CREATE TABLE logs (id INTEGER,ts INTEGER,ts_nanos INTEGER,target TEXT,'
                           'feedback_log_body TEXT,thread_id TEXT)')
            stats = Store(root).stats(child_id)
        self.assertEqual([t['turn_id'] for t in stats['turns']], [parent_turn, child_turn])
        self.assertEqual([t['origin_thread_id'] for t in stats['turns']],
                         [parent_id, child_id])

    def test_latest_chat_uses_recency_and_excludes_subagents(self):
        with tempfile.TemporaryDirectory() as directory:
            with sqlite3.connect(pathlib.Path(directory) / 'state_5.sqlite') as db:
                db.execute('CREATE TABLE threads (id TEXT,title TEXT,model TEXT,updated_at INTEGER,'
                           'archived INTEGER,source TEXT,recency_at_ms INTEGER,updated_at_ms INTEGER,'
                           'created_at INTEGER,name TEXT)')
                db.executemany('INSERT INTO threads (id,title,model,updated_at,archived,source,'
                               'recency_at_ms,updated_at_ms,created_at) VALUES (?,?,?,?,?,?,?,?,?)', [
                    ('older', 'older', 'test', 999, 0, 'cli', 100, 999000, 1),
                    ('newer', 'newer', 'test', 100, 0, 'cli', 200, 100000, 2),
                    ('agent', 'agent', 'test', 2000, 0, '{"subagent":{}}', 9999999, 2000000, 3)])
            self.assertEqual([r['id'] for r in Store(directory).threads()], ['newer', 'older'])

    def test_model_summary_includes_chats_without_runtime_logs(self):
        other_id = '00000000-0000-4000-8000-000000000002'
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            sessions = root / 'sessions'
            sessions.mkdir()
            paths = {}
            for index, thread_id in enumerate((ID, other_id), 1):
                path = sessions / ('rollout-' + thread_id + '.jsonl')
                paths[thread_id] = path
                turn_id = thread_id[:-1] + str(index)
                rows = [
                    {'timestamp': '2026-10-13T01:00:00Z', 'type': 'session_meta',
                     'ordinal': 0, 'payload': {'id': thread_id}},
                    {'timestamp': '2026-10-13T01:00:00Z', 'type': 'event_msg',
                     'ordinal': 1, 'payload': {'type': 'task_started', 'turn_id': turn_id}},
                    {'timestamp': '2026-10-13T01:00:01Z', 'type': 'turn_context',
                     'ordinal': 2, 'payload': {'turn_id': turn_id, 'model': 'test',
                                               'effort': 'medium'}},
                    {'timestamp': '2026-10-13T01:00:02Z', 'type': 'token_usage_record',
                     'ordinal': 3, 'payload': {'turn_id': turn_id, 'response_id': 'r',
                                               'usage': {'output_tokens': 100}}},
                    {'timestamp': '2026-10-13T01:00:10Z', 'type': 'event_msg',
                     'ordinal': 4, 'payload': {'type': 'task_complete', 'turn_id': turn_id,
                                               'duration_ms': 10000,
                                               'time_to_first_token_ms': 1000}}]
                path.write_text('\n'.join(json.dumps(row) for row in rows))
            with sqlite3.connect(root / 'state_5.sqlite') as db:
                db.execute('CREATE TABLE threads (id TEXT,rollout_path TEXT,name TEXT,title TEXT,'
                           'source TEXT,thread_source TEXT,updated_at INTEGER)')
                db.executemany('INSERT INTO threads VALUES (?,?,?,?,?,?,?)', [
                    (tid, str(path), tid, tid, 'vscode', 'user', i)
                    for i, (tid, path) in enumerate(paths.items())])
            with sqlite3.connect(root / 'logs_2.sqlite') as db:
                db.execute('CREATE TABLE logs (id INTEGER,ts INTEGER,ts_nanos INTEGER,target TEXT,'
                           'feedback_log_body TEXT,thread_id TEXT)')
            summary = Store(root).models(force=True)
        self.assertEqual(summary['scanned_chats'], 2)
        self.assertEqual(summary['models'][0]['chat_count'], 2)
        self.assertEqual(summary['models'][0]['completed_turns'], 2)
        self.assertEqual(summary['models'][0]['request_turns'], 0)

    def test_model_summary_merges_chats_and_reports_pooled_rates(self):
        first = analyze_turn(turn(), events())
        second = analyze_turn(turn(usage=[{'output_tokens': 200, 'reasoning_output_tokens': 0}]), events())
        second['effort'] = 'high'
        other = dict(first, model='another')
        result = aggregate_models([('chat1', first), ('chat2', second), ('chat3', other)])
        merged = next(r for r in result if r['model'] == 'test')
        self.assertEqual(merged['chat_count'], 2)
        self.assertEqual(merged['request_pooled_tps'], 310 / 32)
        self.assertEqual(merged['body_pooled_tps'], 300 / 20)
        self.assertEqual({r['effort'] for r in merged['by_effort']}, {'medium', 'high'})
        self.assertEqual(next(r for r in merged['by_effort'] if r['effort'] == 'high')['chat_count'], 1)

    def test_model_summary_deduplicates_inherited_fork_turns(self):
        original = analyze_turn(turn(), events())
        original['origin_thread_id'] = 'parent'
        inherited = dict(original)
        result = aggregate_models([('parent', original), ('child', inherited)])
        self.assertEqual(result[0]['completed_turns'], 1)
        self.assertEqual(result[0]['chat_count'], 1)

    def test_turn_average_is_arithmetic_mean(self):
        first = analyze_turn(turn(), events())
        second_events = [{'time': 2, 'kind': 'request_start'},
                         {'time': 3, 'kind': 'item_start', 'item_type': 'message', 'item_id': 'n'},
                         {'time': 6, 'kind': 'done', 'item_type': 'message', 'item_id': 'n'}]
        second = analyze_turn(
            turn(usage=[{'output_tokens': 200, 'reasoning_output_tokens': 0}], end=10),
            second_events)
        summary = summarize_turns([first, second])
        self.assertEqual(summary['request_turns'], 2)
        self.assertEqual(summary['request_output_tokens'], 310)
        self.assertEqual(summary['request_duration_s'], 20)
        self.assertEqual(summary['request_pooled_tps'], 310 / 20)
        self.assertEqual(summary['request_turn_mean_tps'],
                         (first['request_average_tps'] + second['request_average_tps']) / 2)
        self.assertNotEqual(summary['request_turn_mean_tps'], summary['request_pooled_tps'])
        self.assertEqual(summary['wall_turn_mean_s'], 15)
        self.assertEqual(summary['wall_median_s'], 15)
        self.assertEqual(summary['wall_tps_turns'], 2)
        self.assertEqual(summary['wall_turn_mean_tps'], (110 / 20 + 200 / 10) / 2)
        self.assertEqual(summary['wall_median_tps'], (110 / 20 + 200 / 10) / 2)
        self.assertEqual(summary['first_visible_turn_mean_s'], 5.5)
        self.assertEqual(summary['first_visible_median_s'], 5.5)

    def test_summary_missing_metrics_do_not_dilute_valid_samples(self):
        good = analyze_turn(turn(), events())
        missing = analyze_turn(turn(), [])
        unfinished = analyze_turn(turn(end=None), events())
        unknown = dict(good, model=None)
        result = aggregate_models([('chat', t) for t in [good, missing, unfinished, unknown]])[0]
        self.assertEqual(result['completed_turns'], 2)
        self.assertEqual(result['request_turns'], 1)
        self.assertEqual(result['body_turns'], 1)
        self.assertEqual(result['request_pooled_tps'], good['request_average_tps'])

    def test_separates_wait_reasoning_and_body(self):
        result = analyze_turn(turn(), events())
        self.assertEqual(result['request_average_tps'], 110 / 16)
        self.assertEqual(result['body_tps_estimate'], 100 / 10)
        self.assertEqual(result['wall_tps'], 110 / 20)
        self.assertEqual(result['first_visible_delay_s'], 8)
        self.assertEqual(result['request_duration_s'], 16)
        self.assertEqual(result['outside_request_s'], 4)

    def test_first_visible_delay_does_not_require_request_alignment(self):
        result = analyze_turn(turn(usage=[{'output_tokens': 110}, {'output_tokens': 500}],
                                   first_visible_delay_s=8), events())
        self.assertEqual(result['first_visible_delay_s'], 8)
        self.assertIsNone(result['request_duration_s'])
        self.assertIsNone(result['outside_request_s'])

    def test_tool_output_not_mislabeled_as_body(self):
        result = analyze_turn(turn(), events(tool=True))
        self.assertIsNone(result['body_tps_estimate'])
        self.assertEqual(result['request_average_tps'], 110 / 17)

    def test_missing_log_does_not_shift_usage(self):
        result = analyze_turn(turn(usage=[{'output_tokens': 110}, {'output_tokens': 500}]), events())
        self.assertIsNone(result['request_average_tps'])
        self.assertEqual(result['requests'], [])
        self.assertEqual(result['wall_tps'], 610 / 20)

    def test_missing_done_not_used_as_end(self):
        result = analyze_turn(turn(), events()[:-1])
        self.assertIsNone(result['request_average_tps'])
        self.assertIsNone(result['body_tps_estimate'])

    def test_unfinished_has_no_tps(self):
        result = analyze_turn(turn(end=None), events())
        self.assertIsNone(result['request_average_tps'])
        self.assertIsNone(result['body_tps_estimate'])
        self.assertIsNone(result['wall_tps'])

    def test_missing_reasoning_is_not_assumed_zero(self):
        result = analyze_turn(turn(usage=[{'output_tokens': 110}]), events())
        self.assertIsNone(result['body_tps_estimate'])
        self.assertIsNone(result['reasoning_tokens'])

    def test_weighted_rate_not_mean_of_rates(self):
        t = turn(usage=[{'output_tokens': 110, 'reasoning_output_tokens': 10},
                        {'output_tokens': 200, 'reasoning_output_tokens': 0}], end=40)
        ev = events() + [{'time': 22, 'kind': 'request_start'},
                        {'time': 25, 'kind': 'item_start', 'item_type': 'message', 'item_id': 'n'},
                        {'time': 30, 'kind': 'done', 'item_type': 'message', 'item_id': 'n'}]
        result = analyze_turn(t, ev)
        self.assertAlmostEqual(result['request_average_tps'], 310 / 24)
        self.assertEqual(result['body_tps_estimate'], 300 / 15)

    def test_duplicate_counts_and_partial_write(self):
        def event(payload):
            return {'timestamp': '2026-10-13T01:00:00Z', 'type': 'event_msg', 'payload': payload}
        usage = event({'type': 'token_count', 'info': {
            'last_token_usage': {'output_tokens': 110, 'reasoning_output_tokens': 10},
            'total_token_usage': {'output_tokens': 110}}})
        rows = [event({'type': 'task_started', 'turn_id': ID}), usage, usage,
                event({'type': 'task_complete', 'turn_id': ID})]
        with tempfile.TemporaryDirectory() as temp:
            p = pathlib.Path(temp) / 'rollout.jsonl'
            p.write_text('\n'.join(json.dumps(r) for r in rows) + '\n{"incomplete":')
            result = parse_rollout(p)
        self.assertEqual(len(result[0]['usage']), 1)

    def test_token_usage_records_override_legacy_counts_and_deduplicate_response(self):
        def row(kind, payload, ordinal):
            return {'timestamp': '2026-10-13T01:00:00Z', 'type': kind,
                    'ordinal': ordinal, 'payload': payload}
        legacy = {'type': 'token_count', 'info': {
            'last_token_usage': {'output_tokens': 100},
            'total_token_usage': {'output_tokens': 100}}}
        record = {'turn_id': ID, 'response_id': 'response-1',
                  'usage': {'output_tokens': 125, 'reasoning_output_tokens': 25}}
        rows = [row('event_msg', {'type': 'task_started', 'turn_id': ID}, 1),
                row('event_msg', legacy, 2), row('token_usage_record', record, 3),
                row('token_usage_record', record, 4),
                row('event_msg', {'type': 'task_complete', 'turn_id': ID,
                                  'duration_ms': 5000, 'time_to_first_token_ms': 1200}, 5)]
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / 'rollout.jsonl'
            path.write_text('\n'.join(json.dumps(r) for r in rows))
            parsed = parse_rollout(path)[0]
        self.assertEqual(parsed['usage'], [record['usage']])
        self.assertEqual(parsed['duration_s'], 5)
        self.assertEqual(parsed['first_visible_delay_s'], 1.2)

    def test_precise_event_timestamp_keeps_final_done_in_completion_second(self):
        rows = [
            {'timestamp': '2026-10-13T01:00:00.100Z', 'type': 'event_msg', 'ordinal': 1,
             'payload': {'type': 'task_started', 'turn_id': ID, 'started_at': 0}},
            {'timestamp': '2026-10-13T01:00:01Z', 'type': 'token_usage_record', 'ordinal': 2,
             'payload': {'turn_id': ID, 'response_id': 'response-1',
                         'usage': {'output_tokens': 110, 'reasoning_output_tokens': 10}}},
            {'timestamp': '2026-10-13T01:00:20.900Z', 'type': 'event_msg', 'ordinal': 3,
             'payload': {'type': 'task_complete', 'turn_id': ID, 'completed_at': 0,
                         'duration_ms': 20800, 'time_to_first_token_ms': 7900}}]
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / 'rollout.jsonl'
            path.write_text('\n'.join(json.dumps(row) for row in rows))
            parsed = parse_rollout(path)[0]
        start = parsed['start']
        result = analyze_turn(parsed, [
            {'time': start + 2, 'kind': 'request_start'},
            {'time': start + 8, 'kind': 'item_start', 'item_type': 'message', 'item_id': 'm'},
            {'time': start + 20.5, 'kind': 'done', 'item_type': 'message', 'item_id': 'm'}])
        self.assertAlmostEqual(parsed['end'] - parsed['start'], 20.8, places=6)
        self.assertAlmostEqual(result['request_duration_s'], 18.5, places=6)
        self.assertAlmostEqual(result['body_tps_estimate'], 100 / 12.5)

    def test_rollout_chain_excludes_replaced_parent_tail(self):
        second_id = '00000000-0000-4000-8000-000000000002'
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            first = root / ('rollout-' + ID + '.jsonl')
            second = root / ('rollout-' + ID + '_' + second_id + '.jsonl')
            def event(turn_id, kind, ordinal):
                return {'timestamp': '2026-10-13T01:00:00Z', 'ordinal': ordinal,
                        'type': 'event_msg', 'payload': {'type': kind, 'turn_id': turn_id}}
            first_rows = [
                {'timestamp': '2026-10-13T00:00:00Z', 'ordinal': 0,
                 'type': 'session_meta', 'payload': {'id': ID}},
                event(ID, 'task_started', 1), event(ID, 'task_complete', 2),
                event('00000000-0000-4000-8000-000000000003', 'task_started', 3),
                event('00000000-0000-4000-8000-000000000003', 'task_complete', 4)]
            second_rows = [
                {'timestamp': '2026-10-13T00:00:00Z', 'ordinal': 0,
                 'type': 'session_meta', 'payload': {'id': ID, 'history_base': {
                     'thread_id': ID, 'end_ordinal_exclusive': 3}}},
                event(second_id, 'task_started', 3), event(second_id, 'task_complete', 4)]
            first.write_text('\n'.join(json.dumps(r) for r in first_rows))
            second.write_text('\n'.join(json.dumps(r) for r in second_rows))
            parsed = parse_rollouts([first, second], second)
        self.assertEqual([t['turn_id'] for t in parsed], [ID, second_id])

    def test_aborted_turn_is_separate_from_unfinished(self):
        aborted = turn(end=2, outcome='aborted')
        aborted['duration_s'] = 2
        result = analyze_turn(aborted, [])
        self.assertEqual(result['status'], 'aborted')
        self.assertEqual(result['aborted_duration_s'], 2)
        self.assertIsNone(result['wall_tps'])

    def test_span_log_extraction(self):
        prefix = 'session:turn{turn.id="' + ID + '"}: '
        rows = [(1, 0, 'feedback_tags', prefix +
                 'websocket_connection{auth_header_attached=true}'),
                (2, 500000000, 'feedback_tags', prefix +
                 'stream_request{auth_header_attached=true}'),
                (8, 0, 'codex_core::stream_events_utils', prefix +
                 'Output item item_type="message" item_id="m"'),
                (18, 0, 'codex_core::stream_events_utils', prefix +
                 'handle_responses{from="output_item_done"}: Output item item_type="message" item_id="m"')]
        parsed = parse_logs(rows)[ID]
        self.assertEqual(parsed[0]['time'], 2.5)
        self.assertEqual(sum(e['kind'] == 'request_start' for e in parsed), 1)
        self.assertEqual(parsed[-1]['kind'], 'done')


if __name__ == '__main__':
    unittest.main()
