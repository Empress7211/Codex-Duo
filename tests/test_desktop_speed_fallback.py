import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'source'))
import managed_desktop
import desktop_renderer_patches


class SpeedFallbackTest(unittest.TestCase):
    def test_changed_speed_contract_keeps_complete_existing_integration(self):
        scripts = {'base': b'CODEX_ELECTRON_USER_DATA_PATH userData CODEX_HOME'}
        base = {'main': b'main patch', 'renderer': b'renderer patch'}
        with (patch.object(managed_desktop, 'read_patch_scripts', return_value=scripts),
              patch('desktop_main_patches.discover_main_patches', return_value={'main': base['main']}),
              patch('desktop_renderer_patches.discover_renderer_patches', return_value={'renderer': base['renderer']}),
              patch('desktop_speed_request_patches.discover_speed_request_patches', return_value={'speed': b'partial'}),
              patch('desktop_speed_ui_patches.discover_speed_ui_patches', side_effect=desktop_renderer_patches.PatchError('changed')),
              contextlib.redirect_stderr(io.StringIO()) as errors):
            result = managed_desktop.patch_plan(Path('/does-not-matter'))
        self.assertEqual(result, base)
        self.assertIn('速度选择不可用', errors.getvalue())

    def test_speed_enabled_only_when_both_parts_succeed(self):
        scripts = {'base': b'CODEX_ELECTRON_USER_DATA_PATH userData CODEX_HOME'}
        with (patch.object(managed_desktop, 'read_patch_scripts', return_value=scripts),
              patch('desktop_main_patches.discover_main_patches', return_value={'main': b'main'}),
              patch('desktop_renderer_patches.discover_renderer_patches', return_value={'renderer': b'renderer'}),
              patch('desktop_speed_request_patches.discover_speed_request_patches', return_value={'speed': b'request'}),
              patch('desktop_speed_ui_patches.discover_speed_ui_patches', return_value={'speed': b'request+ui'})):
            result = managed_desktop.patch_plan(Path('/does-not-matter'))
        self.assertEqual(result['speed'], b'request+ui')


if __name__ == '__main__':
    unittest.main()
