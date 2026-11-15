import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'source'))
from orbit_bridge import NativeCLIProcess
import backend


NODE = Path('/Applications/ChatGPT.app/Contents/Resources/cua_node/bin/node')


class NativeRuntimeLaunchTests(unittest.TestCase):
    def test_normal_launch_pins_native_runtimes(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            desktop = base / 'Native.app'
            resources = desktop / 'Contents/Resources'
            native_node = resources / 'cua_node/bin/node'
            native_node.parent.mkdir(parents=True)
            native_node.touch()
            manager = backend.Manager(base / 'duo', base / 'native', base / 'cc')
            item = {'id': 'subscription', 'kind': 'subscription', 'name': 'Subscription'}
            current = {'version': 'test', 'build': 'test'}
            manifest = {'initialized': True, 'app': current}
            with patch.object(manager, 'owned', return_value=manifest), \
                 patch.object(manager, 'profile', return_value=item), \
                 patch.object(manager, '_check_shell_redirects'), \
                 patch.object(manager, 'repair_bundled_marketplace'), \
                 patch.object(manager, 'repair_history_tools'), \
                 patch.object(manager, 'desktop_app', return_value=desktop), \
                 patch.object(manager, 'pids', side_effect=[[], [123], [123]]), \
                 patch.object(backend, 'app_info', return_value=current), \
                 patch.object(backend.subprocess, 'run') as run:
                run.return_value.returncode = 0
                manager.launch('subscription')
            command = run.call_args.args[0]
            for key in ('ORBIT_NATIVE_NODE', 'CODEX_MCP_NODE_PATH', 'CODEX_BROWSER_USE_NODE_PATH'):
                self.assertIn(key + '=' + str(native_node), command)
            self.assertIn('ORBIT_REAL_CLI=' + str(
                resources / 'codex-cli/CodexCLI.app/Contents/MacOS/codex'), command)


@unittest.skipUnless(NODE.is_file(), 'Requires the installed native Codex Node runtime')
class NativeCLIHostTests(unittest.TestCase):
    def test_real_pid_streaming_eof_and_exit_code(self):
        script = (
            'console.log(JSON.stringify({pid:process.pid,parent:process.ppid}));'
            'process.stdin.pipe(process.stdout);'
            'process.stdin.on("end",()=>{process.exitCode=7});'
        )
        child = NativeCLIProcess(NODE, NODE, ['-e', script])
        try:
            identity = json.loads(child.stdout.readline())
            self.assertEqual(identity, {'pid': child.pid, 'parent': child.process.pid})
            child.stdin.write(b'first request\n')
            child.stdin.flush()
            self.assertEqual(child.stdout.readline(), b'first request\n')
            child.stdin.close()
            self.assertEqual(child.stdout.read(), b'')
            self.assertEqual(child.wait(timeout=5), 7)
            self.assertEqual(child.returncode, 7)
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)
            child.stdout.close()
            child.stdin.close()

    def test_forwards_signal_and_leaves_no_cli(self):
        child = NativeCLIProcess(NODE, NODE, ['-e',
            'console.log("ready");setInterval(()=>{},1000)'])
        try:
            self.assertEqual(child.stdout.readline(), b'ready\n')
            child.send_signal(signal.SIGTERM)
            self.assertEqual(child.wait(timeout=5), -signal.SIGTERM)
            with self.assertRaises(ProcessLookupError):
                os.kill(child.pid, 0)
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)
            child.stdout.close()
            child.stdin.close()

    def test_spawn_failure_is_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, 'writer PID'):
                NativeCLIProcess(NODE, Path(directory) / 'missing-cli', [])


if __name__ == '__main__':
    unittest.main()
