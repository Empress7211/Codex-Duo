import json
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'source'))
from thread_reader import ThreadReader, dispatch

TID = '01a1ca9d-5136-7363-b49f-c9d3dbe9a372'


class ThreadReaderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.reader = ThreadReader(self.home)
        with closing(sqlite3.connect(self.home / 'state_5.sqlite')) as db, db:
            db.execute('CREATE TABLE threads (id TEXT,title TEXT,cwd TEXT,archived INT,updated_at INT,history_mode TEXT,rollout_path TEXT)')
            db.execute('INSERT INTO threads VALUES (?,?,?,?,?,?,?)',
                       (TID, '本轮标注', '/project', 1, 5, 'paginated', str(self.home / 'legacy.jsonl')))
        with closing(sqlite3.connect(self.home / 'thread_history_1.sqlite')) as db, db:
            db.execute('CREATE TABLE thread_items (thread_id TEXT,turn_id TEXT,item_id TEXT,rollout_ordinal INT,item_type TEXT,item_json TEXT)')
            for index, kind in enumerate(('userMessage', 'reasoning', 'commandExecution', 'agentMessage')):
                db.execute('INSERT INTO thread_items VALUES (?,?,?,?,?,?)',
                           (TID, 'turn', str(index), index, kind, json.dumps({'type': kind, 'text': str(index)})))

    def test_search_includes_archived_and_read_accepts_deep_link(self):
        found = self.reader.call('list_threads', {'query': '标注'})
        self.assertEqual(found['threads'][0]['id'], TID)
        self.assertEqual(found['threads'][0]['archived'], 1)
        page = self.reader.call('read_thread', {'thread_id': 'codex://threads/' + TID, 'limit': 1})
        self.assertEqual(page['items'][0]['item']['type'], 'userMessage')
        self.assertEqual(page['next_offset'], 1)
        page = self.reader.call('read_thread', {'thread_id': TID, 'offset': page['next_offset'], 'limit': 1})
        self.assertEqual(page['items'][0]['item']['type'], 'agentMessage')
        self.assertIsNone(page['next_offset'])
        tools = self.reader.call('read_thread', {'thread_id': TID, 'include_tools': True})
        self.assertEqual([r['item']['type'] for r in tools['items']],
                         ['userMessage', 'commandExecution', 'agentMessage'])

    def test_legacy_messages_and_incomplete_append(self):
        with closing(sqlite3.connect(self.home / 'state_5.sqlite')) as db, db:
            db.execute("UPDATE threads SET history_mode='legacy'")
        event = {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': []}}
        (self.home / 'legacy.jsonl').write_bytes((json.dumps(event) + '\n{"partial"').encode())
        page = self.reader.call('read_thread', {'thread_id': TID})
        self.assertEqual(len(page['items']), 1)
        self.assertTrue(page['persisted_only'])

    def test_protocol_errors_and_missing_history_are_visible(self):
        result = dispatch(self.reader, {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
        self.assertEqual([t['name'] for t in result['result']['tools']], ['list_threads', 'read_thread'])
        self.assertIsNone(dispatch(self.reader, {'method': 'notifications/initialized'}))
        for arguments in ({'thread_id': '../bad'}, {'thread_id': TID, 'limit': 0}):
            result = dispatch(self.reader, {'id': 2, 'method': 'tools/call',
                                           'params': {'name': 'read_thread', 'arguments': arguments}})
            self.assertTrue(result['result']['isError'])
        (self.home / 'thread_history_1.sqlite').unlink()
        result = dispatch(self.reader, {'id': 3, 'method': 'tools/call',
                                       'params': {'name': 'read_thread', 'arguments': {'thread_id': TID}}})
        self.assertTrue(result['result']['isError'])
        self.assertFalse((self.home / 'thread_history_1.sqlite').exists())


if __name__ == '__main__':
    unittest.main()
