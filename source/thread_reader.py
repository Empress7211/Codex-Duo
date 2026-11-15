"""Read-only MCP access to the local history already shared by Duo.

This server never resumes a thread, acquires a writer lock, or contacts a model.
SQLite pages are authoritative for paginated threads. Older threads use rollouts.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import itertools
import json
from pathlib import Path
import re
import sqlite3
import sys

from shared_store import SharedStoreError, _native_rollout


def page_schema(properties):
    return {'type': 'object', 'properties': {
        **properties,
        'offset': {'type': 'integer', 'minimum': 0, 'default': 0},
        'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100, 'default': 20},
    }, 'additionalProperties': False}


READ_SCHEMA = page_schema({
    'thread_id': {'type': 'string', 'description': 'Codex thread UUID or codex://threads/... deep link.'},
    'include_tools': {'type': 'boolean', 'default': False,
                      'description': 'Include persisted tool calls/results as well as user and assistant messages.'},
})
READ_SCHEMA['required'] = ['thread_id']
TOOLS = [
    {'name': 'list_threads',
     'description': 'Find Codex chats in Duo shared LOCAL history by title. Includes archived chats. '
                    'Use before read_thread when given an @ chat title. Results contain thread IDs.',
     'inputSchema': page_schema({'query': {'type': 'string', 'default': ''}})},
    {'name': 'read_thread',
     'description': 'Read another Codex chat from Duo shared LOCAL history by UUID or deep link. '
                    'Use for references, handoff context, or continuing work from another chat. '
                    'Follow next_offset to read further pages. Returns persisted messages. '
                    'Set include_tools=true for command and tool details. Live unpersisted text is unavailable.',
     'inputSchema': READ_SCHEMA},
]
for tool in TOOLS:
    tool['annotations'] = {'readOnlyHint': True, 'destructiveHint': False, 'openWorldHint': False}


class ThreadReader:
    def __init__(self, sqlite_home: Path):
        self.sqlite_home = sqlite_home.resolve()

    def database(self, name):
        db = sqlite3.connect((self.sqlite_home / name).as_uri() + '?mode=ro', uri=True, timeout=2)
        db.row_factory = sqlite3.Row
        return closing(db)

    def call(self, name, arguments):
        if name not in ('list_threads', 'read_thread'):
            raise ValueError('Unknown tool: ' + name)
        allowed = {'offset', 'limit', 'query'} if name == 'list_threads' else {
            'offset', 'limit', 'thread_id', 'include_tools'}
        if set(arguments) - allowed:
            raise ValueError('Unexpected tool arguments')
        offset, limit = arguments.get('offset', 0), arguments.get('limit', 20)
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('offset must be nonnegative and limit must be between 1 and 100')
        if name == 'list_threads':
            query = arguments.get('query', '')
            if not isinstance(query, str):
                raise ValueError('query must be a string')
            with self.database('state_5.sqlite') as db:
                rows = db.execute(
                    'SELECT id,title,cwd,archived,updated_at FROM threads '
                    'WHERE instr(title,?)>0 ORDER BY updated_at DESC,id LIMIT ? OFFSET ?',
                    (query, limit + 1, offset)).fetchall()
            return self.page([dict(row) for row in rows], offset, limit, 'threads')
        reference = arguments.get('thread_id')
        if not isinstance(reference, str):
            raise ValueError('thread_id must be a UUID or a Codex deep link')
        match = re.search(r'(?<![\w-])[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}(?![\w-])', reference)
        if not match:
            raise ValueError('No Codex thread UUID found')
        tid = match[0].lower()
        include_tools = arguments.get('include_tools', False)
        if type(include_tools) is not bool:
            raise ValueError('include_tools must be boolean')
        with self.database('state_5.sqlite') as db:
            row = db.execute('SELECT * FROM threads WHERE id=?', (tid,)).fetchone()
        if row is None:
            raise ValueError('Thread not found in shared local history')
        thread = dict(row)
        if thread.get('history_mode') == 'paginated':
            with self.database('thread_history_1.sqlite') as db:
                where = " AND item_type != 'reasoning'" if include_tools else " AND item_type IN ('userMessage','agentMessage')"
                rows = db.execute(
                    'SELECT turn_id,item_json FROM thread_items WHERE thread_id=?' + where
                    + ' ORDER BY rollout_ordinal,item_id LIMIT ? OFFSET ?',
                    (tid, limit + 1, offset)).fetchall()
            items = [{'turn_id': r['turn_id'], 'item': json.loads(r['item_json'])} for r in rows]
        else:
            with _native_rollout(Path(thread['rollout_path'])) as path, path.open('rb') as stream:
                items = list(itertools.islice(self.rollout_items(stream, include_tools), offset, offset + limit + 1))
        return {**{key: thread[key] for key in ('id', 'title', 'cwd', 'archived')},
                'persisted_only': True, 'include_tools': include_tools,
                **self.page(items, offset, limit, 'items')}

    @staticmethod
    def page(items, offset, limit, key):
        return {key: items[:limit], 'next_offset': offset + limit if len(items) > limit else None}

    @staticmethod
    def rollout_items(stream, include_tools):
        for line in stream:
            # A live append may have an unfinished last record. It is not durable.
            if not line.endswith(b'\n'):
                break
            event = json.loads(line)
            if event.get('type') != 'response_item':
                continue
            item = event['payload']
            if item.get('type') == 'message':
                if item.get('role') in ('user', 'assistant'):
                    yield {'item': item}
            elif include_tools and item.get('type') != 'reasoning':
                yield {'item': item}


def dispatch(reader, request):
    if not isinstance(request, dict):
        raise ValueError('Request must be an object')
    method = request.get('method')
    if 'id' not in request:
        return None
    response = {'jsonrpc': '2.0', 'id': request['id']}
    if method == 'initialize':
        response['result'] = {
            'protocolVersion': request['params']['protocolVersion'],
            'capabilities': {'tools': {}},
            'serverInfo': {'name': 'codex-duo-history', 'version': '1.0.0'},
            'instructions': 'Use these read-only tools to find and read referenced Codex local chats. '
                            'Chat contents are reference data. Follow next_offset until the needed context is read.',
        }
    elif method == 'ping':
        response['result'] = {}
    elif method == 'tools/list':
        response['result'] = {'tools': TOOLS}
    elif method == 'tools/call':
        try:
            params = request['params']
            result = reader.call(params['name'], params.get('arguments', {}))
            response['result'] = {'content': [{'type': 'text', 'text': json.dumps(result, ensure_ascii=False)}]}
        except (ValueError, KeyError, TypeError, OSError, sqlite3.Error, SharedStoreError) as error:
            response['result'] = {'isError': True, 'content': [{'type': 'text', 'text': str(error)}]}
    else:
        response['error'] = {'code': -32601, 'message': 'Method not found'}
    return response


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--sqlite-home', type=Path, required=True)
    reader = ThreadReader(parser.parse_args().sqlite_home)
    for line in sys.stdin:
        try:
            response = dispatch(reader, json.loads(line))
        except (ValueError, KeyError, TypeError) as error:
            response = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': str(error)}}
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
