"""Read shared Codex runtime evidence without acquiring a thread or using auth.

Observer events describe managed servers. Indexed latest-turn metadata and real
writer locks describe the canonical store, including the original application.
A persisted unfinished turn alone never proves that a task is still running.
"""
from __future__ import annotations

from contextlib import closing
import argparse
import errno
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time

if __name__ == '__main__':
    sys.dont_write_bytecode = True

TERMINAL_TURNS = frozenset(('completed', 'failed', 'interrupted'))
ACTIVE_FLAGS = frozenset(('waitingOnApproval', 'waitingOnUserInput'))
LATEST_TURN_SQL = (
    'SELECT turn_id,status,started_at,completed_at,duration_ms,rollout_ordinal,error_json '
    'FROM thread_turns WHERE thread_id=? ORDER BY rollout_ordinal DESC LIMIT 1'
)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # POSIX EPERM still means that the process exists.
    return True


def lock_busy(path: Path) -> bool:
    """Probe an existing advisory lock without creating or changing its file.

    A free probe lock is released immediately. An occupied lock is never changed.
    Empty .lock files commonly survive completion and process exit.
    """
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in (errno.EAGAIN, errno.EACCES):
                return True
            raise
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)


def parse_lsof(output: str, paths: dict[Path, str]) -> dict[str, set[int]]:
    owners: dict[str, set[int]] = {}
    current_pid = None
    for line in output.splitlines():
        if line.startswith('p'):
            current_pid = int(line[1:]) if line[1:].isdigit() else None
        elif line.startswith('n') and current_pid is not None:
            tid = paths.get(Path(line[1:]))
            if tid is not None:
                owners.setdefault(tid, set()).add(current_pid)
    return owners


def read_writer_locks(lock_dir: Path, thread_ids, *, alive=pid_alive) -> dict:
    lock_dir = Path(lock_dir).resolve()
    records = {}
    busy_paths = {}
    for tid in thread_ids:
        path = lock_dir / (tid + '.lock')
        try:
            busy = lock_busy(path)
            records[tid] = {'busy': busy, 'owner_pid': None}
            if busy:
                busy_paths[path] = tid
        except OSError as error:
            records[tid] = {'busy': None, 'owner_pid': None, 'error': str(error)}
    if not busy_paths:
        return records
    try:
        result = subprocess.run(
            ['/usr/sbin/lsof', '-F', 'pn', '--', *map(str, busy_paths)],
            # Some macOS hosts take longer than five seconds to enumerate open
            # files. Callers in the desktop bridge run this off the RPC loop.
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode not in (0, 1):
            raise OSError(f'lsof exited with code {result.returncode}')
        owners = parse_lsof(result.stdout, busy_paths)
        for tid, candidates in owners.items():
            live = [pid for pid in candidates if alive(pid)]
            if len(live) == 1:
                records[tid]['owner_pid'] = live[0]
            elif len(live) > 1:
                records[tid]['error'] = 'Multiple open-file owners; writer identity is uncertain'
    except (OSError, subprocess.TimeoutExpired) as error:
        for tid in busy_paths.values():
            records[tid]['error'] = str(error)
    return records


class BackgroundLockReader:
    """Probe lock state immediately and resolve owners away from the RPC loop.

    Owner results are short-lived hints for display and handoff discovery. A
    write request must independently use read_writer_locks before admission.
    """
    def __init__(self, refresh_interval=5):
        self.refresh_interval = refresh_interval
        self.mutex = threading.Lock()
        self.cached = {}
        self.last_finished = 0
        self.running = False

    def __call__(self, lock_dir: Path, thread_ids, *, alive=pid_alive) -> dict:
        records = {}
        busy = []
        for tid in thread_ids:
            try:
                held = lock_busy(Path(lock_dir) / (tid + '.lock'))
                records[tid] = {'busy': held, 'owner_pid': None}
                if held:
                    busy.append(tid)
            except OSError as error:
                records[tid] = {'busy': None, 'owner_pid': None, 'error': str(error)}
        with self.mutex:
            now = time.monotonic()
            for tid in thread_ids:
                if records[tid]['busy'] is not True:
                    self.cached.pop(tid, None)
                elif now - self.last_finished < self.refresh_interval:
                    cached = self.cached.get(tid)
                    if cached and cached.get('owner_pid') is not None and alive(cached['owner_pid']):
                        records[tid]['owner_pid'] = cached['owner_pid']
            if busy and not self.running and now - self.last_finished >= self.refresh_interval:
                self.running = True
                threading.Thread(target=self._resolve, args=(lock_dir, tuple(busy)), daemon=True).start()
        return records

    def _resolve(self, lock_dir, thread_ids):
        records = {}
        try:
            records = read_writer_locks(lock_dir, thread_ids)
        finally:
            with self.mutex:
                self.cached = records
                self.last_finished = time.monotonic()
                self.running = False


class RuntimeSnapshot:
    def __init__(self, root: Path, *, sqlite_home: Path | None = None,
                 lock_dir: Path | None = None, alive=pid_alive,
                 lock_reader=read_writer_locks):
        self.root = Path(root).resolve()
        self.sqlite_home = Path(sqlite_home or self.root / 'shared/sqlite').resolve()
        self.lock_dir = Path(lock_dir or self.root / 'shared/thread-writer-locks').resolve()
        self.alive = alive
        self.lock_reader = lock_reader

    @classmethod
    def from_environment(cls, env: dict | None = None):
        env = os.environ if env is None else env
        root = Path(env['ORBIT_ROOT']).expanduser().resolve()
        sync = json.loads((root / 'manifest.json').read_text()).get('sync', {})
        return cls(root,
                   sqlite_home=env.get('ORBIT_SQLITE_HOME') or sync.get('sqlite_home'),
                   lock_dir=env.get('ORBIT_LOCK_DIR') or sync.get('thread-writer-locks'))

    def latest_turns(self, thread_ids) -> dict:
        """One indexed metadata lookup per interested thread; never read items."""
        database = self.sqlite_home / 'thread_history_1.sqlite'
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=.2)) as db:
            db.execute('BEGIN')
            turns = {}
            for tid in thread_ids:
                row = db.execute(LATEST_TURN_SQL, (tid,)).fetchone()
                if row is not None:
                    turns[tid] = dict(zip(
                        ('id', 'status', 'started_at', 'completed_at', 'duration_ms', 'rollout_ordinal'), row[:6]))
                    turns[tid]['error'] = json.loads(row[6]) if row[6] is not None else None
            return turns

    def _observations(self, thread_ids, alive) -> tuple[dict, dict, dict, list]:
        wanted = set(thread_ids)
        observations = {}
        profiles = {}
        handoff_ready = {}
        errors = []
        for path in (self.root / 'profiles').glob('*/activity/[0-9]*.json'):
            if not path.stem.isdigit() or not alive(int(path.stem)):
                continue
            try:
                record = json.loads(path.read_text())
                child_pid = record['child_pid']
                profile_id = path.parent.parent.name
                if (child_pid != int(path.stem) or record.get('profile_id') != profile_id
                        or record.get('monitor_status') != 'ok'
                        or record.get('process_status') != 'running'):
                    continue
                profiles[child_pid] = profile_id
                bridge_pid = record.get('bridge_pid')
                if not isinstance(bridge_pid, int) or not alive(bridge_pid):
                    continue
                handoff_ready[child_pid] = record.get('handoff_ready') is True
                for tid, state in record.get('threads', {}).items():
                    if tid in wanted:
                        observations.setdefault(tid, {})[child_pid] = state
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
                errors.append({'source': 'observer', 'profile': path.parent.parent.name, 'error': str(error)})
        return observations, profiles, handoff_ready, errors

    def snapshot(self, thread_ids, *, local_pid: int | None = None,
                 local_profile: str | None = None, lock_reader=None) -> dict:
        # Identifiers come from thread/list/read. Reject path-like identifiers
        # at the filesystem boundary; retain normal official and test IDs.
        tids = list(dict.fromkeys(thread_ids))
        if any(not isinstance(tid, str) or not tid or '/' in tid or '\\' in tid
               or tid in ('.', '..') for tid in tids):
            raise ValueError('Invalid thread identifier')
        result = {'version': 1, 'updated_at': time.time(), 'threads': {}, 'errors': []}
        if not tids:
            return result
        liveness = {}

        def alive(pid):
            if pid not in liveness:
                liveness[pid] = self.alive(pid)
            return liveness[pid]

        observations, profiles, handoff_ready, errors = self._observations(tids, alive)
        result['errors'].extend(errors)
        try:
            turns = self.latest_turns(tids)
        except (sqlite3.Error, OSError, ValueError) as error:
            turns = {}
            result['errors'].append({'source': 'history', 'error': str(error)})
        locks = (lock_reader or self.lock_reader)(self.lock_dir, tids, alive=alive)
        for tid in tids:
            lock = locks[tid]
            busy, owner_pid = lock['busy'], lock.get('owner_pid')
            if owner_pid is not None and not alive(owner_pid):
                owner_pid = None
            if lock.get('error'):
                result['errors'].append({'source': 'lock', 'thread_id': tid, 'error': lock['error']})
            turn = turns.get(tid)
            candidates = observations.get(tid, {})
            observed = candidates.get(owner_pid)
            # A managed active event can cover the brief race before its lock
            # appears. A peer's notLoaded/idle never overrides a foreign writer.
            if observed is None and busy is False:
                active = [(pid, record) for pid, record in candidates.items() if record.get('status') == 'active']
                if len(active) == 1:
                    owner_pid, observed = active[0]
            status, source = 'unknown', 'unavailable'
            flags = []
            if observed is not None and observed.get('status') == 'active':
                if (turn is not None and turn['status'] in TERMINAL_TURNS
                        and observed.get('turn_id') == turn['id']):
                    status, source = 'idle', 'durable_terminal'
                else:
                    status, source = 'active', 'observer'
                    flags = [flag for flag in observed.get('active_flags', []) if flag in ACTIVE_FLAGS]
                    if observed.get('turn_id') and (turn is None or turn['id'] != observed['turn_id']):
                        turn = {'id': observed['turn_id'], 'status': 'inProgress', 'started_at': None,
                                'completed_at': None, 'duration_ms': None, 'rollout_ordinal': None}
            elif observed is not None and observed.get('status') in ('idle', 'systemError'):
                status, source = 'idle', 'observer'
            elif turn is not None and turn['status'] in TERMINAL_TURNS:
                status, source = 'idle', 'history'
            elif turn is not None and turn['status'] == 'inProgress' and busy is True and owner_pid is not None:
                status, source = 'active', 'history_and_live_writer'
            elif turn is not None:
                source = 'unfinished_history_without_live_writer'
            foreign = owner_pid != local_pid if owner_pid is not None and local_pid is not None else None
            result['threads'][tid] = {
                'status': status, 'source': source, 'busy': busy,
                'owner_pid': owner_pid,
                'owner_profile': profiles.get(owner_pid, 'native') if owner_pid is not None else None,
                'foreign_owner': foreign,
                'writable': busy is False and status != 'active' or owner_pid is not None and owner_pid == local_pid,
                # This is a request-to-handoff hint, never proof of a free lock.
                # The owner serializes the final gate again before any exit.
                'handoff_available': busy is True and status == 'idle' and handoff_ready.get(owner_pid, False),
                'turn': turn, 'active_flags': flags,
                'completed': turn is not None and turn['status'] in TERMINAL_TURNS,
                'local_profile': local_profile,
            }
        return result


def rpc_status(record: dict) -> dict:
    if record['status'] == 'active':
        return {'type': 'active', 'activeFlags': record.get('active_flags', [])}
    if record['status'] == 'idle':
        return {'type': 'idle'}
    return {'type': 'notLoaded'}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path)
    parser.add_argument('--sqlite-home', type=Path)
    parser.add_argument('--lock-dir', type=Path)
    parser.add_argument('--thread', action='append', default=[])
    parser.add_argument('--local-pid', type=int)
    parser.add_argument('--local-profile')
    parser.add_argument('--stdio', action='store_true')
    arguments = parser.parse_args()
    reader = (RuntimeSnapshot(arguments.root, sqlite_home=arguments.sqlite_home, lock_dir=arguments.lock_dir)
              if arguments.root else RuntimeSnapshot.from_environment())
    if arguments.stdio:
        for line in sys.stdin:
            request = json.loads(line)
            print(json.dumps(reader.snapshot(request['thread_ids'], local_pid=request.get('local_pid'),
                                             local_profile=request.get('local_profile'))), flush=True)
    else:
        print(json.dumps(reader.snapshot(arguments.thread, local_pid=arguments.local_pid,
                                         local_profile=arguments.local_profile), indent=2))


if __name__ == '__main__':
    main()
