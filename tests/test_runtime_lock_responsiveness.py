"""Slow lock ownership discovery must not block the desktop RPC stream."""
import fcntl
import json
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'source'))
import runtime_sync
from orbit_bridge import RuntimeProxy


class BackgroundLockReaderTest(unittest.TestCase):
    def test_slow_owner_lookup_does_not_block_and_free_lock_invalidates_hint(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'chat.lock'
            with path.open('w') as held:
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                started, release = threading.Event(), threading.Event()
                def slow_lookup(_directory, _ids):
                    started.set()
                    self.assertTrue(release.wait(3))
                    return {'chat': {'busy': True, 'owner_pid': 42}}
                reader = runtime_sync.BackgroundLockReader(refresh_interval=10)
                with mock.patch.object(runtime_sync, 'read_writer_locks', side_effect=slow_lookup) as lookup:
                    before = time.monotonic()
                    first = reader(Path(directory), ['chat'], alive=lambda _: True)
                    self.assertLess(time.monotonic() - before, 1)
                    self.assertEqual(first['chat'], {'busy': True, 'owner_pid': None})
                    self.assertTrue(started.wait(1))
                    self.assertEqual(reader(Path(directory), ['chat'])['chat']['owner_pid'], None)
                    self.assertEqual(lookup.call_count, 1)
                    release.set()
                    deadline = time.monotonic() + 2
                    while reader.running and time.monotonic() < deadline:
                        time.sleep(.01)
                    self.assertEqual(reader(Path(directory), ['chat'], alive=lambda _: True)['chat']['owner_pid'], 42)
                    fcntl.flock(held, fcntl.LOCK_UN)
                    self.assertEqual(reader(Path(directory), ['chat'])['chat'],
                                     {'busy': False, 'owner_pid': None})


class ProxyWriterGateTest(unittest.TestCase):
    def test_slow_verification_does_not_block_output_and_gate_stays_authoritative(self):
        for writable in (True, False):
            with self.subTest(writable=writable):
                started, release = threading.Event(), threading.Event()
                class Projection:
                    def retain(self, _ids): pass
                    def fresh_writer_snapshot(self, _tid):
                        started.set()
                        if not release.wait(3):
                            raise TimeoutError('test did not release verification')
                        return {'writable': writable, 'status': 'active', 'owner_profile': 'peer',
                                'owner_pid': 99}
                    def observe_turn_event(self, _message): pass
                proxy = RuntimeProxy.__new__(RuntimeProxy)
                proxy.projection = Projection()
                proxy.generation = 1
                proxy.gate_pending = False
                proxy.messages = queue.Queue()
                proxy.release_leases = {}
                proxy.pending = {}
                proxy.server_requests = set()
                proxy.saved_initialize = None
                proxy.sent, proxy.emitted = [], []
                proxy.send = proxy.sent.append
                proxy.emit = proxy.emitted.append
                proxy.activity = mock.Mock()
                proxy.journal = mock.Mock()
                request = {'id': 7, 'method': 'turn/start', 'params': {'threadId': 'chat'}}
                line = json.dumps(request).encode() + b'\n'
                proxy._input(line)
                self.assertTrue(started.wait(1))
                self.assertTrue(proxy.gate_pending)
                self.assertEqual(proxy.sent, [])
                event = b'{"method":"item/started","params":{}}\n'
                proxy._output(event)
                self.assertIn(event, proxy.emitted)
                release.set()
                kind, generation, (checked_line, record) = proxy.messages.get(timeout=2)
                self.assertEqual((kind, generation), ('writer_checked', 1))
                proxy.gate_pending = False
                proxy._input(checked_line, record)
                if writable:
                    self.assertEqual(proxy.sent, [request])
                else:
                    self.assertEqual(proxy.sent, [])
                    self.assertEqual(proxy.emitted[-1]['id'], 7)
                    self.assertIn('active writer', proxy.emitted[-1]['error']['message'])

    def test_running_proxy_forwards_child_events_during_slow_writer_check(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child = root / 'child.py'
            child.write_text('''#!/usr/bin/env python3
import json, sys, threading, time
def notify():
    time.sleep(.4)
    print(json.dumps({"method": "item/started", "params": {}}), flush=True)
threading.Thread(target=notify, daemon=True).start()
for line in sys.stdin:
    message = json.loads(line)
    if "id" in message:
        print(json.dumps({"id": message["id"], "result": {}}), flush=True)
''')
            child.chmod(0o755)
            harness = root / 'proxy.py'
            source = str(Path(__file__).resolve().parents[1] / 'source')
            harness.write_text('''import sys, time
from pathlib import Path
sys.path.insert(0, %r)
from orbit_bridge import RuntimeProxy
class Reader:
    def __init__(self, root): self.root = self.sqlite_home = root
    def alive(self, pid): return True
    def snapshot(self, ids, **kwargs):
        if kwargs.get("lock_reader") is not None: time.sleep(1.5)
        record = {"writable": False, "busy": True, "status": "active", "owner_profile": "peer", "owner_pid": 99, "active_flags": [], "turn": None}
        return {"threads": {tid: record.copy() for tid in ids}, "errors": []}
root = Path(sys.argv[1])
profile = root / "profiles" / "test"
profile.mkdir(parents=True)
sys.exit(RuntimeProxy([], root / "child.py", profile, Reader(root)).run())
''' % source)
            process = subprocess.Popen([sys.executable, str(harness), str(root)], stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
            try:
                lines = queue.Queue()
                threading.Thread(target=lambda: [lines.put(line) for line in process.stdout], daemon=True).start()
                process.stdin.write(json.dumps({'id': 7, 'method': 'turn/start',
                                                'params': {'threadId': 'chat'}}) + '\n')
                process.stdin.flush()
                deadline = time.monotonic() + 1.2
                first = {}
                while first.get('method') != 'item/started':
                    try:
                        first = json.loads(lines.get(timeout=max(.001, deadline - time.monotonic())))
                    except queue.Empty:
                        self.fail('child event was blocked by the writer check')
                    self.assertNotIn('error', first)
                second = json.loads(lines.get(timeout=3))
                self.assertEqual(second['id'], 7)
                self.assertIn('active writer', second['error']['message'])
            finally:
                process.terminate()
                process.communicate(timeout=5)


if __name__ == '__main__':
    unittest.main()
