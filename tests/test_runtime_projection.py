"""Exercise history loading and live completion without launching Codex."""
import copy
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'source'))
from orbit_bridge import RuntimeProjection


def turn(identifier='old', status='completed'):
    return {'id': identifier, 'status': status, 'items': [], 'started_at': 10,
            'completed_at': 20 if status == 'completed' else None, 'duration_ms': 10}


class Reader:
    def __init__(self, root):
        self.root = self.sqlite_home = root
        self.record = {'status': 'idle', 'active_flags': [], 'busy': False,
                       'owner_pid': None, 'owner_profile': None, 'turn': turn()}

    def alive(self, _pid):
        return True

    def snapshot(self, ids, **_kwargs):
        return {'threads': {tid: copy.deepcopy(self.record) for tid in ids}, 'errors': []}


class RuntimeProjectionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        with closing(sqlite3.connect(self.root / 'thread_history_1.sqlite')) as db, db:
            db.execute('CREATE TABLE thread_items (thread_id TEXT,turn_id TEXT,item_id TEXT,'
                       'updated_at_ordinal INTEGER,item_json TEXT,completed_at_ms INTEGER,'
                       'started_at_ms INTEGER)')
        self.reader = Reader(self.root)
        self.projection = RuntimeProjection(self.reader, self.root / 'profiles/local', 11)

    def response(self, method, params, *, turns=None, extra=None):
        result = {'thread': {'id': 'chat', 'turns': turns or [], 'status': {'type': 'idle'}}}
        result.update(extra or {})
        return self.projection.rewrite_response({'method': method, 'params': params}, {'result': result})

    def completions(self):
        _, events = self.projection.poll()
        return [event for event in events if event['method'] == 'turn/completed']

    def test_metadata_reads_do_not_subscribe_to_historical_completions(self):
        for params in ({'threadId': 'chat'}, {'threadId': 'chat', 'includeTurns': False}):
            result = self.response('thread/read', params)
            self.assertEqual(result['result']['thread']['status']['type'], 'idle')
            self.assertNotIn('chat', self.projection.hydrated)
            self.assertEqual(self.completions(), [])

    def test_full_history_is_baseline_but_new_completion_is_delivered_once(self):
        self.response('thread/read', {'threadId': 'chat', 'includeTurns': True}, turns=[turn()])
        self.assertEqual(self.completions(), [])
        self.assertEqual(self.completions(), [])
        self.reader.record['turn'] = turn('new')
        events = self.completions()
        self.assertEqual([e['params']['turn']['id'] for e in events], ['new'])
        self.assertEqual(self.completions(), [])

    def test_active_turn_finishing_before_first_poll_is_not_lost(self):
        self.reader.record.update(status='active', owner_pid=22, owner_profile='native',
                                  turn=turn(status='inProgress'))
        self.response('thread/read', {'threadId': 'chat', 'includeTurns': True},
                      turns=[turn(status='inProgress')])
        self.reader.record.update(status='idle', turn=turn())
        self.assertEqual(len(self.completions()), 1)

    def test_latest_turn_page_supports_pagination_without_replaying_old_completions(self):
        self.response('thread/read', {'threadId': 'chat', 'includeTurns': False})
        self.projection.rewrite_response(
            {'method': 'thread/turns/list', 'params': {'threadId': 'chat', 'cursor': None,
             'sortDirection': 'desc', 'itemsView': 'notLoaded'}},
            {'result': {'data': [turn(), turn('older')], 'nextCursor': 'older-page'}})
        self.assertIn('chat', self.projection.hydrated)
        self.assertEqual(self.completions(), [])
        self.projection.rewrite_response(
            {'method': 'thread/turns/list', 'params': {'threadId': 'chat', 'cursor': 'older-page',
             'sortDirection': 'desc'}}, {'result': {'data': [turn('older')], 'nextCursor': None}})
        self.assertEqual(self.projection.turn_states['chat'], ('old', 'completed'))
        self.assertEqual(self.completions(), [])

    def test_resume_full_history_and_initial_page_establish_baseline(self):
        for page in (False, True):
            with self.subTest(initial_page=page):
                self.projection = RuntimeProjection(self.reader, self.root / 'profiles/local', 11)
                params = {'threadId': 'chat', 'excludeTurns': page}
                extra = None
                if page:
                    params['initialTurnsPage'] = {'sortDirection': 'desc'}
                    extra = {'initialTurnsPage': {'data': [turn(), turn('older')], 'nextCursor': 'next'}}
                self.response('thread/resume', params, turns=[] if page else [turn()], extra=extra)
                self.assertIn('chat', self.projection.hydrated)
                self.assertEqual(self.projection.turn_states['chat'], ('old', 'completed'))
                self.assertEqual(self.completions(), [])
        self.projection = RuntimeProjection(self.reader, self.root / 'profiles/local', 11)
        self.response('thread/resume', {'threadId': 'chat', 'excludeTurns': True})
        self.assertNotIn('chat', self.projection.hydrated)

    def test_first_terminal_observation_is_not_a_live_completion(self):
        self.response('thread/read', {'threadId': 'chat', 'includeTurns': True})
        self.assertEqual(self.completions(), [])
        self.reader.record['turn'] = turn('new')
        self.assertEqual(len(self.completions()), 1)

    def test_peer_completion_is_not_repeated_by_sql_fallback(self):
        self.reader.record.update(status='active', owner_pid=22, owner_profile='peer',
                                  turn=turn(status='inProgress'))
        self.response('thread/read', {'threadId': 'chat', 'includeTurns': True},
                      turns=[turn(status='inProgress')])
        journal = self.root / 'profiles/peer/activity/22.events.jsonl'
        journal.parent.mkdir(parents=True)
        event = {'method': 'turn/completed', 'params': {'threadId': 'chat', 'turn': turn()}}
        journal.write_text(json.dumps({'time': time.time() + 1, 'message': event}) + '\n')
        self.reader.record.update(status='idle', turn=turn())
        self.assertEqual(self.completions(), [event])
        self.assertEqual(self.completions(), [])

    def test_local_completion_is_not_replayed_after_writer_release(self):
        self.response('thread/read', {'threadId': 'chat', 'includeTurns': True},
                      turns=[turn(status='inProgress')])
        self.projection.observe_turn_event(
            {'method': 'turn/completed', 'params': {'threadId': 'chat', 'turn': turn()}})
        self.assertEqual(self.completions(), [])


if __name__ == '__main__':
    unittest.main()
