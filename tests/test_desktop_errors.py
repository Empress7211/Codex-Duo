"""Check that known desktop errors reach the user without raw subprocess output."""
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'source'))
import backend
from managed_desktop import DesktopCopyError, _run


class DesktopErrorTests(unittest.TestCase):
    def test_known_error_is_displayed(self):
        message = '当前 Codex 的集成接口不兼容。'
        stdout = io.StringIO()
        with patch.object(sys, 'argv', ['backend.py', '--root', '/unused']), \
             patch.object(sys, 'stdin', io.StringIO('{"action":"status"}')), \
             patch.object(sys, 'stdout', stdout), \
             patch.object(backend.Manager, 'dispatch', side_effect=DesktopCopyError(message)):
            self.assertEqual(backend.main(), 0)
        self.assertEqual(json.loads(stdout.getvalue()), {'ok': False, 'error': message})

    def test_subprocess_error_does_not_expose_stderr(self):
        result = subprocess.CompletedProcess([], 1, '', 'sensitive diagnostic')
        with patch('managed_desktop.subprocess.run', return_value=result):
            with self.assertRaisesRegex(DesktopCopyError, '桌面副本签名验证失败') as raised:
                _run(['/usr/bin/codesign'])
        self.assertNotIn('sensitive diagnostic', str(raised.exception))


if __name__ == '__main__':
    unittest.main()
