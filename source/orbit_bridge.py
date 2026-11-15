"""Codex stdio observer and managed-only runtime projection.

Ordinary CLI invocations stay transparent. Canonical managed homes opt into
read-only foreign runtime events; writes always use their own authenticated CLI.
"""
from __future__ import annotations

import json
from collections import OrderedDict, deque
import os
from pathlib import Path
import queue
import re
import select
import signal
import subprocess
import sys
import threading
import time
import uuid

if __name__ == '__main__':
    sys.dont_write_bytecode = True

REAL_CLI = Path('/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex')
METHODS = {'thread/status/changed', 'turn/started', 'turn/completed', 'thread/closed', 'thread/started'}
STATUSES = {'notLoaded', 'idle', 'systemError', 'active'}
RUNTIME_EVENTS = {'thread/status/changed', 'thread/started', 'turn/started',
                  'turn/completed', 'item/started', 'item/completed', 'error',
                  'turn/plan/updated', 'turn/diff/updated'}


class NativeCLIProcess:
    """Keep the real writer PID while a signed Node owns the CLI process."""

    def __init__(self, node: Path, real_cli: Path, args: list[str]):
        host = Path(__file__).with_name('native_cli_host.cjs')
        self.process = subprocess.Popen([str(node), str(host), str(real_cli), *args],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        try:
            self.pid = json.loads(self.process.stdout.readline())['cli_pid']
        except (ValueError, KeyError):
            self.process.stdin.close()
            self.process.stdout.close()
            self.process.wait()
            raise RuntimeError('Native CLI host did not return its writer PID') from None
        self.stdin = self.process.stdin
        self.stdout = self.process.stdout

    @property
    def returncode(self):
        return self.process.returncode

    def poll(self):
        return self.process.poll()

    def wait(self, timeout=None):
        return self.process.wait(timeout=timeout)

    def send_signal(self, signum):
        self.process.send_signal(signum)

    def terminate(self):
        self.send_signal(signal.SIGTERM)


def start_cli(real_cli: Path, args: list[str]):
    node = os.environ.get('ORBIT_NATIVE_NODE')
    if node:
        return NativeCLIProcess(Path(node), real_cli, args)
    return subprocess.Popen([str(real_cli), *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE)


def profile_directory(env: dict) -> Path:
    """Resolve only a home owned by this tool; never write into ordinary Codex."""
    raw = env.get('CODEX_HOME', '')
    if not raw:
        raise ValueError('CODEX_HOME is missing')
    home = Path(raw).expanduser().absolute()
    profile = home.parent
    root = profile.parent.parent
    if home.name != 'codex-home' or profile.parent.name != 'profiles':
        raise ValueError('Not a managed Codex profile home')
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,63}', profile.name):
        raise ValueError('Invalid managed profile identifier')
    if home.resolve() != home or profile.resolve() != profile or root.resolve() != root:
        raise ValueError('Managed profile path must not redirect outside its owned root')
    marker = root / 'manifest.json'
    if marker.resolve() != marker:
        raise ValueError('Manifest must not redirect outside its owned root')
    data = json.loads(marker.read_text(encoding='utf-8'))
    if data.get('owner') != 'codex-dual' or data.get('schema') != 1 or data.get('root') != str(root):
        raise ValueError('Managed workspace identity does not match')
    profiles = data.get('profiles')
    allowed = {p['id'] for p in profiles} if isinstance(profiles, list) else {'subscription', 'api'}
    if profile.name not in allowed:
        raise ValueError('Profile is absent from the managed manifest')
    return profile


def is_stdio_server(args: list[str]) -> bool:
    if 'app-server' not in args or any(a in args for a in ('--help', '-h', '--version', '-V')):
        return False
    tail = args[args.index('app-server') + 1:]
    if any(a in tail for a in ('daemon', 'proxy', 'generate-ts', 'generate-json-schema', 'help')):
        return False
    for i, arg in enumerate(tail):
        if arg.startswith('--listen=') and arg != '--listen=stdio://':
            return False
        if arg == '--listen' and (i + 1 == len(tail) or tail[i + 1] != 'stdio://'):
            return False
    return True


class Activity:
    def __init__(self, profile: Path | None, child_pid: int):
        self.path = None
        if profile:
            directory = profile / 'activity'
            try:
                if directory.resolve().parent != profile or directory.is_symlink():
                    raise ValueError('Activity directory must remain inside its owned profile')
                directory.mkdir(mode=0o700, exist_ok=True)
                self.path = directory / f'{child_pid}.json'
            except (OSError, ValueError):
                pass
        now = time.time()
        self.data = {'version': 1, 'profile_id': profile.name if profile else None,
                     'bridge_pid': os.getpid(), 'child_pid': child_pid,
                     'session_id': str(uuid.uuid4()), 'started_at': now, 'updated_at': now,
                     'monitor_status': 'ok' if self.path else 'unavailable',
                     'process_status': 'running', 'threads': {}}
        self.warned = False
        self.persist()

    def warn(self) -> None:
        if not self.warned:
            try:
                sys.stderr.write('codex-dual: activity monitor unavailable; CLI forwarding continues.\n')
                sys.stderr.flush()
            except OSError:
                pass
            self.warned = True

    def persist(self) -> None:
        self.data['updated_at'] = time.time()
        if self.path is None:
            self.data['monitor_status'] = 'unavailable'
            self.warn()
            return
        temporary = self.path.with_name(f'.{self.data["child_pid"]}-{os.getpid()}.tmp')
        try:
            # A disk failure must never turn into a CLI/RPC failure. The reader
            # validates child_pid/session identity rather than an idle timeout.
            self.data['monitor_status'] = 'ok'
            with temporary.open('w', encoding='utf-8') as handle:
                json.dump(self.data, handle, separators=(',', ':'))
            os.replace(temporary, self.path)
            self.warned = False
        except (OSError, ValueError):
            self.data['monitor_status'] = 'unavailable'
            self.warn()
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def observe(self, line: bytes) -> None:
        try:
            event = json.loads(line)
            if not isinstance(event, dict) or event.get('method') not in METHODS:
                return
            params = event.get('params')
            if not isinstance(params, dict):
                return
            method = event['method']
            if method == 'thread/started':
                thread = params.get('thread', {})
                tid = thread.get('id')
                status = thread.get('status', {}).get('type')
            else:
                tid = params.get('threadId')
                status = params.get('status', {}).get('type') if method == 'thread/status/changed' else None
            if not isinstance(tid, str) or not tid:
                return
            if method == 'thread/closed':
                self.data['threads'].pop(tid, None)
                self.persist()
                return
            if method == 'turn/started':
                status = 'active'
            elif method == 'turn/completed':
                status = 'idle'
            if status not in STATUSES:
                return
            record = {'status': status, 'updated_at': time.time()}
            previous = self.data['threads'].get(tid, {})
            if previous.get('turn_id'):
                record['turn_id'] = previous['turn_id']
            turn = params.get('turn', {})
            if isinstance(turn, dict) and isinstance(turn.get('id'), str):
                record['turn_id'] = turn['id']
                record['turn_status'] = turn.get('status', 'inProgress' if status == 'active' else None)
            if status == 'active':
                flags = params.get('status', {}).get('activeFlags', [])
                record['active_flags'] = [f for f in flags if f in ('waitingOnApproval', 'waitingOnUserInput')]
            self.data['threads'][tid] = record
            self.persist()
        except (ValueError, TypeError, AttributeError):
            # Unknown or malformed output is still forwarded byte-for-byte.
            return

    def finish(self, returncode: int, *, reason: str | None = None) -> None:
        self.data['process_status'] = 'stopped' if returncode == 0 or reason == 'idle_handoff' else 'error'
        self.data['exit_code'] = returncode
        if reason:
            self.data['stop_reason'] = reason
        self.persist()


class EventJournal:
    """A bounded session event tail for other managed windows' live transcript.

    These are the same thread/turn/item events already in shared chat history.
    Authentication, RPC responses and arbitrary server requests are never saved.
    """
    def __init__(self, profile: Path, pid: int):
        self.path = profile / 'activity' / f'{pid}.events.jsonl'
        self.warned = False

    def observe(self, message: dict) -> None:
        method = message.get('method', '')
        if method not in RUNTIME_EVENTS and not method.startswith('item/'):
            return
        params = message.get('params', {})
        if not isinstance(params, dict) or not isinstance(params.get('threadId'), str):
            return
        if 'id' in message:
            return
        try:
            if self.path.exists() and self.path.stat().st_size > 4 * 1024 * 1024:
                os.replace(self.path, self.path.with_suffix('.previous'))
            with self.path.open('ab') as stream:
                stream.write(json.dumps({'time': time.time(), 'message': message},
                                        separators=(',', ':')).encode() + b'\n')
        except OSError:
            if not self.warned:
                sys.stderr.write('codex-orbit: live event journal unavailable.\n')
                self.warned = True


class RuntimeProjection:
    def __init__(self, reader, profile: Path, pid: int, *, now=time.monotonic):
        self.reader, self.profile, self.pid, self.now = reader, profile, pid, now
        self.interested = OrderedDict()
        self.snapshots = {}
        self.journal_offsets = {}
        self.item_versions = {}
        self.turn_states = {}
        self.hydrated = set()
        self.hydrated_at = {}
        self.hydration_replays = {}
        self.native_threads = set()
        self.last_published = None
        self.output_path = reader.root / 'runtime-state' / f'{os.getpid()}.json'

    def retain(self, ids) -> None:
        for tid in ids:
            if isinstance(tid, str) and tid:
                self.interested[tid] = self.now()
                self.interested.move_to_end(tid)
        while len(self.interested) > 512:
            tid, _ = self.interested.popitem(last=False)
            self.snapshots.pop(tid, None)
            self.hydrated.discard(tid)
            self.hydrated_at.pop(tid, None)
            self.hydration_replays.pop(tid, None)
            self.native_threads.discard(tid)
            self.turn_states.pop(tid, None)
            for key in list(self.item_versions):
                if key[0] == tid:
                    self.item_versions.pop(key)

    def snapshot(self, ids=None) -> dict:
        if ids is None:
            threshold = self.now() - 600
            ids = [tid for tid, seen in self.interested.items()
                   if seen >= threshold or tid in self.hydrated
                   or self.snapshots.get(tid, (None,))[0] == 'active']
        return self.reader.snapshot(ids, local_pid=self.pid, local_profile=self.profile.name)

    def fresh_writer_snapshot(self, tid) -> dict:
        from runtime_sync import read_writer_locks
        return self.reader.snapshot([tid], local_pid=self.pid, local_profile=self.profile.name,
                                    lock_reader=read_writer_locks)['threads'][tid]

    def _hydrate(self, tid: str, record: dict, turns: list, latest: dict | None) -> None:
        self.hydrated.add(tid)
        self.hydrated_at[tid] = time.time()
        # History already delivered in a response is a baseline, not a new
        # completion. Native clients persist unread state on turn/completed.
        if latest is not None:
            self.turn_states[tid] = (latest['id'], latest['status'])
        if (record['status'] == 'active' and record['owner_pid'] != self.pid
                and record['owner_profile'] not in (None, 'native') and record.get('turn')):
            turn_id = record['turn']['id']
            baseline = {item['id'] for turn in turns if turn.get('id') == turn_id
                        for item in turn.get('items', []) if item.get('id')}
            self.hydration_replays[tid] = {'turn_id': turn_id, 'items': baseline}
        if record['owner_profile'] == 'native':
            self.native_threads.add(tid)

    def observe_turn_event(self, message: dict) -> None:
        if message.get('method') in ('turn/started', 'turn/completed'):
            params = message['params']
            turn = params['turn']
            self.turn_states[params['threadId']] = (turn['id'], turn['status'])

    def rewrite_response(self, request: dict, message: dict) -> dict:
        from runtime_sync import rpc_status
        if 'result' not in message:
            return message
        method = request.get('method')
        params = request.get('params') or {}
        value = message['result']
        if method in ('thread/list', 'thread/read', 'thread/resume', 'thread/start'):
            threads = value.get('data', []) if method == 'thread/list' else [value.get('thread')]
            threads = [thread for thread in threads if isinstance(thread, dict) and thread.get('id')]
            self.retain([thread['id'] for thread in threads])
            records = self.snapshot([thread['id'] for thread in threads])['threads']
            for thread in threads:
                record = records[thread['id']]
                if record['status'] != 'unknown':
                    thread['status'] = rpc_status(record)
                full_read = method == 'thread/read' and params.get('includeTurns') is True
                full_resume = method == 'thread/resume' and not params.get('excludeTurns', False)
                page = value.get('initialTurnsPage') if method == 'thread/resume' else None
                if full_read or full_resume or page is not None:
                    turns = page['data'] if page is not None else thread.get('turns', [])
                    descending = page is not None and (params.get('initialTurnsPage') or {}).get('sortDirection', 'desc') == 'desc'
                    latest = (turns[0] if descending else turns[-1]) if turns else None
                    self._hydrate(thread['id'], record, turns, latest)
            return message
        if (method == 'thread/turns/list' and params.get('cursor') is None
                and params.get('sortDirection') == 'desc'):
            # Modern clients load the open chat through pages after a metadata
            # read. Older pages must not replace the latest-turn baseline.
            tid = params['threadId']
            self.retain([tid])
            record = self.snapshot([tid])['threads'][tid]
            turns = value['data']
            self._hydrate(tid, record, turns, turns[0] if turns else None)
        return message

    def _journal_events(self, snapshot: dict) -> list:
        events = []
        paths = []
        for current in (self.reader.root / 'profiles').glob('*/activity/[0-9]*.events.jsonl'):
            # The previous segment keeps a poll delayed during a tool call from
            # losing the tail when the bounded journal rolls over.
            previous = current.with_suffix('.previous')
            if previous.exists():
                paths.append(previous)
            paths.append(current)
        live_cursors = set()
        replayed_pids = set()
        for path in paths:
            raw_pid = path.name.split('.', 1)[0]
            if not raw_pid.isdigit() or int(raw_pid) == self.pid or not self.reader.alive(int(raw_pid)):
                continue
            try:
                stat = path.stat()
                cursor = (int(raw_pid), stat.st_ino)
                live_cursors.add(cursor)
                offset = self.journal_offsets.get(cursor, 0)
                if offset > stat.st_size:
                    offset = 0
                with path.open('rb') as stream:
                    replay_owner = any(snapshot['threads'].get(tid, {}).get('owner_pid') == int(raw_pid)
                                       for tid in self.hydration_replays)
                    stream.seek(0 if replay_owner else offset)
                    if replay_owner:
                        replayed_pids.add(int(raw_pid))
                    while True:
                        start = stream.tell()
                        line = stream.readline()
                        if not line or not line.endswith(b'\n'):
                            stream.seek(start)
                            break
                        row = json.loads(line)
                        message = row['message']
                        tid = message.get('params', {}).get('threadId')
                        record = snapshot['threads'].get(tid)
                        if record is None or record['owner_pid'] != int(raw_pid) or tid not in self.hydrated:
                            continue
                        replay = self.hydration_replays.get(tid)
                        if start < offset and replay is None:
                            continue
                        if row.get('time', 0) > self.hydrated_at.get(tid, 0):
                            events.append(message)
                        elif replay:
                            # Active assistant content is not in SQL yet. Replay
                            # just missing current-turn items; completed items
                            # in the read baseline must not receive old deltas.
                            params, method = message.get('params', {}), message.get('method', '')
                            item_id = params.get('itemId') or (params.get('item') or {}).get('id')
                            if (method.startswith('item/') and params.get('turnId') == replay['turn_id']
                                    and item_id not in replay['items']):
                                events.append(message)
                            elif method == 'turn/started' and (params.get('turn') or {}).get('id') == replay['turn_id']:
                                events.append(message)
                    self.journal_offsets[cursor] = stream.tell()
            except (OSError, ValueError, KeyError, TypeError):
                continue
        self.journal_offsets = {key: offset for key, offset in self.journal_offsets.items() if key in live_cursors}
        for tid in list(self.hydration_replays):
            if snapshot['threads'].get(tid, {}).get('owner_pid') in replayed_pids:
                self.hydration_replays.pop(tid)
        return events

    def _native_turn_events(self, snapshot: dict) -> list:
        from runtime_sync import TERMINAL_TURNS
        events = []
        native = [(tid, record) for tid, record in snapshot['threads'].items()
                  if record['owner_pid'] != self.pid
                  and (record['owner_profile'] == 'native' or tid in self.native_threads
                       or (record.get('turn') or {}).get('status') in TERMINAL_TURNS)
                  and tid in self.hydrated and record.get('turn')]
        if not native:
            return events
        import sqlite3
        from contextlib import closing
        try:
            with closing(sqlite3.connect((self.reader.sqlite_home / 'thread_history_1.sqlite').as_uri()
                                         + '?mode=ro', uri=True, timeout=.2)) as db:
                for tid, record in native:
                    turn = record['turn']
                    key = (tid, turn['id'])
                    for old_key in list(self.item_versions):
                        if old_key[0] == tid and old_key != key:
                            self.item_versions.pop(old_key)
                    previous = self.turn_states.get(tid)
                    completion = None
                    if previous != (turn['id'], turn['status']):
                        method = 'turn/completed' if turn['status'] in TERMINAL_TURNS else 'turn/started'
                        event = {'method': method, 'params': {'threadId': tid,
                                       'turn': {'id': turn['id'], 'status': turn['status'], 'items': [],
                                                'error': turn.get('error'), 'durationMs': turn['duration_ms'],
                                                'startedAt': turn['started_at'], 'completedAt': turn['completed_at']}}}
                        if method == 'turn/completed':
                            # A terminal first observation is only history.
                            # A known active/older turn proves a new completion.
                            if previous is not None:
                                completion = event
                        else:
                            events.append(event)
                        self.turn_states[tid] = (turn['id'], turn['status'])
                    # The index constrains these reads to the viewed latest turn.
                    versions = self.item_versions.setdefault(key, {})
                    metadata = db.execute('SELECT item_id,updated_at_ordinal FROM thread_items '
                                          'WHERE thread_id=? AND turn_id=?', key).fetchall()
                    for item_id, revision in metadata:
                        if versions.get(item_id) == revision:
                            continue
                        raw = db.execute('SELECT item_json,completed_at_ms,started_at_ms FROM thread_items '
                                         'WHERE thread_id=? AND turn_id=? AND item_id=?',
                                         (*key, item_id)).fetchone()
                        item = json.loads(raw[0])
                        completed = raw[1] is not None or turn['status'] in TERMINAL_TURNS
                        method = 'item/completed' if completed else 'item/started'
                        params = {'threadId': tid, 'turnId': turn['id'], 'item': item}
                        params['completedAtMs' if completed else 'startedAtMs'] = raw[1] if completed else raw[2]
                        events.append({'method': method, 'params': params})
                        versions[item_id] = revision
                    if completion:
                        events.append(completion)
        except (OSError, sqlite3.Error, ValueError) as error:
            snapshot['errors'].append({'source': 'live_items', 'error': str(error)})
            return events
        return events

    def poll(self) -> tuple[dict, list]:
        from runtime_sync import rpc_status
        snapshot = self.snapshot()
        events = []
        for tid, record in snapshot['threads'].items():
            old = self.snapshots.get(tid)
            if record['owner_profile'] not in (None, 'native'):
                self.native_threads.discard(tid)
            elif record['owner_profile'] == 'native':
                self.native_threads.add(tid)
            if record['owner_pid'] == self.pid:
                continue
            if record['busy'] is True and record['owner_pid'] is None:
                # Owner discovery is pending or unavailable; don't replace a
                # real backend status with a speculative notLoaded event.
                continue
            signature = (record['status'], tuple(record['active_flags']),
                         record['busy'], record['owner_pid'])
            if old != signature:
                events.append({'method': 'thread/status/changed', 'params': {
                    'threadId': tid, 'status': rpc_status(record)}})
            self.snapshots[tid] = signature
        journal_events = self._journal_events(snapshot)
        for event in journal_events:
            self.observe_turn_event(event)
        events.extend(journal_events)
        events.extend(self._native_turn_events(snapshot))
        stable = json.dumps(snapshot['threads'], sort_keys=True, separators=(',', ':'))
        if stable != self.last_published:
            try:
                self.output_path.parent.mkdir(exist_ok=True)
                temporary = self.output_path.with_suffix('.tmp')
                temporary.write_text(json.dumps(snapshot, separators=(',', ':')))
                os.replace(temporary, self.output_path)
                self.last_published = stable
            except OSError as error:
                snapshot['errors'].append({'source': 'runtime_publication', 'error': str(error)})
        return snapshot, events


class RuntimeProxy:
    """Serialize requests, child output and safe managed backend handoffs.

    Only a server whose observed threads are all idle can be replaced. Client
    input stays queued behind this gate, so no newly admitted turn is terminated.
    Foreign writers are read-only and never receive this profile's turn input.
    """
    def __init__(self, args: list[str], real_cli: Path, profile: Path, reader):
        self.args, self.real_cli, self.profile, self.reader = args, real_cli, profile, reader
        self.messages = queue.Queue()
        self.pending = {}
        self.server_requests = set()
        self.saved_initialize = None
        self.initialized = False
        self.generation = 0
        self.child = None
        self.activity = None
        self.projection = None
        self.journal = None
        self.waiting_resumes = {}
        self.release_leases = {}
        self.stdin_closed = False
        self.deferred_inputs = deque()
        self.gate_pending = False
        self.next_poll = 0
        self._start_child()

    def _start_child(self, replay=False) -> None:
        self.generation += 1
        generation = self.generation
        child = start_cli(self.real_cli, self.args)
        self.child = child
        self.activity = Activity(self.profile, child.pid)
        self.journal = EventJournal(self.profile, child.pid)
        if self.projection is None:
            self.projection = RuntimeProjection(self.reader, self.profile, child.pid)
        else:
            # Only the authenticated child changes. Foreign stream cursors and
            # rendered-item revisions remain valid and must not replay twice.
            self.projection.pid = child.pid

        def read_child():
            for line in iter(child.stdout.readline, b''):
                self.messages.put(('output', generation, line))
            self.messages.put(('exit', generation, child.wait()))

        self.child_reader = threading.Thread(target=read_child, name='orbit-child-output')
        self.child_reader.start()
        if replay:
            initialize = dict(self.saved_initialize, id='orbit-initialize-' + str(generation))
            self.pending[initialize['id']] = {'internal': 'initialize', 'request': initialize}
            self.send(initialize)

    def send(self, message: dict | bytes) -> None:
        line = message if isinstance(message, bytes) else json.dumps(message, separators=(',', ':')).encode() + b'\n'
        self.child.stdin.write(line)
        self.child.stdin.flush()

    def emit(self, message: dict | bytes) -> None:
        line = message if isinstance(message, bytes) else json.dumps(message, separators=(',', ':')).encode() + b'\n'
        sys.stdout.buffer.write(line)
        sys.stdout.buffer.flush()

    def _writer_error(self, request: dict, record: dict) -> None:
        owner = record.get('owner_profile') or 'unknown'
        tid = request.get('params', {}).get('threadId', '')
        self.emit({'id': request['id'], 'error': {'code': -32600,
                  'message': f'thread {tid} already has an active writer (Codex Duo: {owner}; '
                             f'execution {record["status"]}, pid {record.get("owner_pid")})'}})

    def _request_handoff(self, message: dict, record: dict) -> None:
        tid = message['params']['threadId']
        owner_pid = record['owner_pid']
        directory = self.reader.root / 'handoff' / str(owner_pid)
        directory.mkdir(parents=True, exist_ok=True)
        request_id = uuid.uuid4().hex
        path = directory / (request_id + '.json')
        path.write_text(json.dumps({'version': 1, 'thread_id': tid, 'owner_pid': owner_pid,
                                   'requester_pid': self.child.pid, 'requester_bridge_pid': os.getpid(),
                                   'created_at': time.time(), 'expires_at': time.time() + 8,
                                   'status': 'requested'}))
        self.waiting_resumes[message['id']] = {'request': message, 'record': record,
                                              'path': path, 'deadline': time.monotonic() + 8}

    def _input(self, line: bytes, checked_record=None) -> None:
        try:
            message = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            self.send(line)
            return
        if not isinstance(message, dict):
            self.send(line)
            return
        if 'method' not in message:  # Response to a local server approval/tool request.
            self.server_requests.discard(message.get('id'))
            self.send(line)
            return
        method, params = message['method'], message.get('params') or {}
        if method == 'initialize':
            self.saved_initialize = dict(message)
        if method == 'initialized':
            self.initialized = True
        if method == 'thread/list' and params.get('modelProviders') is None:
            message['params'] = dict(params, modelProviders=[])
        tid = params.get('threadId')
        if isinstance(tid, str):
            self.projection.retain([tid])
            if 'id' in message and (method == 'thread/resume' or method.startswith('turn/')
                                    or method in ('thread/compact/start', 'thread/shellCommand', 'thread/inject_items')):
                if checked_record is None:
                    self.gate_pending = True
                    generation = self.generation
                    def check_writer():
                        try:
                            result = self.projection.fresh_writer_snapshot(tid)
                        except Exception as error:
                            result = error
                        self.messages.put(('writer_checked', generation, (line, result)))
                    threading.Thread(target=check_writer, daemon=True).start()
                    return
                record = checked_record
                lease = self.release_leases.get(tid)
                if lease and lease['expires_at'] > time.time() and self.reader.alive(lease['requester_pid']):
                    self._writer_error(message, dict(record, owner_profile='handoff', status='idle'))
                    return
                if not record['writable']:
                    if (method == 'thread/resume' and record['status'] == 'idle'
                            and record['owner_profile'] not in (None, 'native') and record['owner_pid']):
                        self._request_handoff(message, record)
                    else:
                        self._writer_error(message, record)
                    return
        if 'id' in message:
            self.pending[message['id']] = {'request': message}
        self.send(message)

    def _output(self, line: bytes) -> None:
        try:
            message = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            self.emit(line)
            return
        if not isinstance(message, dict):
            self.emit(line)
            return
        if 'id' in message and 'method' not in message:
            pending = self.pending.pop(message['id'], None)
            if pending and pending.get('internal') == 'initialize':
                if 'error' in message:
                    raise RuntimeError('Replacement managed backend initialization failed')
                if self.initialized:
                    self.send({'method': 'initialized'})
                return
            if pending:
                # Own state comes from this backend before any foreign status
                # projection. A turn/start result already proves active work,
                # even if its following notification is still queued.
                result = message.get('result', {})
                method = pending['request'].get('method')
                if isinstance(result.get('thread'), dict) and method in ('thread/start', 'thread/resume', 'thread/fork'):
                    self.activity.observe(json.dumps({'method': 'thread/started',
                                                     'params': {'thread': result['thread']}}).encode())
                if method == 'turn/start' and isinstance(result.get('turn'), dict):
                    self.activity.observe(json.dumps({'method': 'turn/started', 'params': {
                        'threadId': pending['request'].get('params', {}).get('threadId'),
                        'turn': result['turn']}}).encode())
                if method == 'thread/unsubscribe' and 'result' in message:
                    tid = pending['request'].get('params', {}).get('threadId')
                    self.projection.hydrated.discard(tid)
                message = self.projection.rewrite_response(pending['request'], message)
            self.emit(message)
            return
        if 'id' in message:
            self.server_requests.add(message['id'])
        self.activity.observe(line)
        self.journal.observe(message)
        self.projection.observe_turn_event(message)
        tid = (message.get('params') or {}).get('threadId')
        if isinstance(tid, str):
            self.projection.retain([tid])
        self.emit(line)

    def _all_idle(self) -> bool:
        if (not self.initialized or self.saved_initialize is None or self.pending or self.gate_pending
                or self.server_requests or self.waiting_resumes or self.deferred_inputs):
            return False
        with self.messages.mutex:
            if any(message[0] == 'input' or message[0] == 'output' and message[1] == self.generation
                   for message in self.messages.queue):
                return False
        threads = self.activity.data['threads']
        return bool(threads) and all(record.get('status') in ('idle', 'notLoaded') for record in threads.values())

    def _publish_handoff_availability(self) -> None:
        ready = self._all_idle() and not self.stdin_closed
        if self.activity.data.get('handoff_ready') != ready:
            self.activity.data['handoff_ready'] = ready
            self.activity.persist()

    def _handle_handoffs(self) -> None:
        directory = self.reader.root / 'handoff' / str(self.child.pid)
        if self.stdin_closed or not directory.exists() or not self._all_idle():
            return
        accepted = []
        for path in directory.glob('*.json'):
            try:
                request = json.loads(path.read_text())
                if (request['status'] == 'requested' and request['owner_pid'] == self.child.pid
                        and request['expires_at'] > time.time() and self.reader.alive(request['requester_pid'])
                        and self.activity.data['threads'].get(request['thread_id'], {}).get('status') == 'idle'):
                    accepted.append((path, request))
            except (OSError, ValueError, KeyError):
                continue
        if not accepted:
            return
        old = self.child
        old_reader = self.child_reader
        released = list(self.activity.data['threads'])
        # Input is forwarded only by this serialized loop. New queued input is
        # replayed after the replacement initializes; no admitted turn races kill.
        old.terminate()
        old.wait(timeout=5)
        old_reader.join(timeout=1)
        self.activity.finish(old.returncode, reason='idle_handoff')
        old.stdin.close()
        old.stdout.close()
        self._start_child(replay=True)
        for path, request in accepted:
            request.update(status='released', released_at=time.time())
            self.release_leases[request['thread_id']] = request
            path.write_text(json.dumps(request))
        for tid in released:
            self.emit({'method': 'thread/status/changed', 'params': {
                'threadId': tid, 'status': {'type': 'notLoaded'}, 'orbitReleased': True}})
            self.emit({'method': 'thread/closed', 'params': {'threadId': tid}})

    def _resume_handoffs(self) -> None:
        for request_id, waiting in list(self.waiting_resumes.items()):
            request = waiting['request']
            tid = request['params']['threadId']
            record = self.projection.snapshot([tid])['threads'][tid]
            if record['busy'] is False:
                self.waiting_resumes.pop(request_id)
                waiting['path'].unlink(missing_ok=True)
                self.pending[request_id] = {'request': request}
                self.send(request)
            elif time.monotonic() >= waiting['deadline']:
                self.waiting_resumes.pop(request_id)
                waiting['path'].unlink(missing_ok=True)
                self._writer_error(request, record)

    def run(self) -> int:
        stop_input = threading.Event()

        def read_input():
            # Buffered readline holds Python's stdin lock while blocked. If a
            # signal ends the child first, interpreter finalization aborts on
            # that lock. Raw reads plus a short select make shutdown stoppable.
            descriptor = sys.stdin.fileno()
            buffered = bytearray()
            while not stop_input.is_set():
                try:
                    if not select.select([descriptor], [], [], .1)[0]:
                        continue
                    chunk = os.read(descriptor, 65536)
                except OSError:
                    chunk = b''
                if not chunk:
                    if buffered:
                        self.messages.put(('input', None, bytes(buffered)))
                    self.messages.put(('stdin_closed', None, None))
                    return
                buffered.extend(chunk)
                while (end := buffered.find(b'\n')) >= 0:
                    self.messages.put(('input', None, bytes(buffered[:end + 1])))
                    del buffered[:end + 1]

        input_reader = threading.Thread(target=read_input, name='orbit-client-input')
        input_reader.start()
        previous = {sig: signal.signal(sig, lambda signum, _frame: self.child.send_signal(signum))
                    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
        try:
            while True:
                try:
                    kind, generation, value = self.messages.get(timeout=.1)
                except queue.Empty:
                    kind = None
                if kind == 'input':
                    # During replacement initialization, defer normal requests.
                    if self.gate_pending or any(p.get('internal') == 'initialize' for p in self.pending.values()):
                        self.deferred_inputs.append(value)
                    else:
                        self._input(value)
                elif kind == 'writer_checked' and generation == self.generation:
                    self.gate_pending = False
                    line, record = value
                    if isinstance(record, Exception):
                        request = json.loads(line)
                        self.emit({'id': request['id'], 'error': {'code': -32603,
                                  'message': 'Could not verify the conversation writer: ' + str(record)}})
                    else:
                        self._input(line, record)
                elif kind == 'output' and generation == self.generation:
                    self._output(value)
                elif kind == 'stdin_closed':
                    self.stdin_closed = True
                    if not self.deferred_inputs and not self.gate_pending:
                        self.child.stdin.close()
                elif kind == 'exit' and generation == self.generation:
                    self.activity.finish(value)
                    return value if value >= 0 else 128 - value
                if not any(p.get('internal') == 'initialize' for p in self.pending.values()):
                    while self.deferred_inputs and not self.gate_pending:
                        self._input(self.deferred_inputs.popleft())
                    if self.stdin_closed and not self.deferred_inputs and not self.gate_pending and not self.child.stdin.closed:
                        self.child.stdin.close()
                self._publish_handoff_availability()
                if time.monotonic() >= self.next_poll and not self.stdin_closed:
                    self.next_poll = time.monotonic() + .5
                    snapshot, events = self.projection.poll()
                    for event in events:
                        self.emit(event)
                    self._resume_handoffs()
                    self._handle_handoffs()
        finally:
            stop_input.set()
            input_reader.join(timeout=1)
            for sig, handler in previous.items():
                signal.signal(sig, handler)
            if self.child.poll() is None:
                self.child.terminate()
                self.child.wait(timeout=5)
                self.activity.finish(self.child.returncode)
            self.child_reader.join(timeout=1)
            if not self.child.stdin.closed:
                self.child.stdin.close()
            self.child.stdout.close()


def run_bridge(args: list[str], real_cli: Path = REAL_CLI) -> int:
    try:
        profile = profile_directory(os.environ)
    except (OSError, ValueError, KeyError, TypeError):
        profile = None
    if profile is not None and os.environ.get('ORBIT_ROOT'):
        from runtime_sync import RuntimeSnapshot
        from runtime_sync import BackgroundLockReader
        reader = RuntimeSnapshot.from_environment()
        reader.lock_reader = BackgroundLockReader()
        if reader.root != profile.parent.parent:
            raise ValueError('Runtime projection root must match the managed profile')
        return RuntimeProxy(args, real_cli, profile, reader).run()
    child = start_cli(real_cli, args)
    activity = Activity(profile, child.pid)

    def relay_input() -> None:
        try:
            while True:
                chunk = os.read(sys.stdin.fileno(), 65536)
                if not chunk:
                    break
                child.stdin.write(chunk)
                child.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                child.stdin.close()
            except OSError:
                pass

    def forward_signal(signum, _frame) -> None:
        try:
            child.send_signal(signum)
        except ProcessLookupError:
            pass

    previous = {s: signal.signal(s, forward_signal) for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    threading.Thread(target=relay_input, name='codex-dual-stdin', daemon=True).start()
    try:
        for line in iter(child.stdout.readline, b''):
            sys.stdout.buffer.write(line)
            sys.stdout.buffer.flush()
            activity.observe(line)
        returncode = child.wait()
        activity.finish(returncode)
        return returncode if returncode >= 0 else 128 - returncode
    except BrokenPipeError:
        child.terminate()
        returncode = child.wait()
        activity.finish(returncode)
        return 1
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        child.stdout.close()


def main(args: list[str] | None = None, *, real_cli: Path | None = None) -> int:
    args = sys.argv[1:] if args is None else args
    real_cli = Path(real_cli or os.environ.get('ORBIT_REAL_CLI', REAL_CLI))
    if real_cli.resolve() == Path(__file__).resolve():
        raise RuntimeError('Real Codex CLI cannot point to the monitoring bridge')
    if not is_stdio_server(args):
        os.execv(str(real_cli), [str(real_cli), *args])
    return run_bridge(args, real_cli)


if __name__ == '__main__':
    raise SystemExit(main())
