"""Keep desktop-owned plugin sources scoped to the managed instance."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'source'))
from backend import Manager, read_toml, toml_dump


class BundledMarketplaceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.manager = Manager(base / 'duo', base / 'native', base / 'cc')
        self.manager.root.mkdir(parents=True)
        (self.manager.root / 'manifest.json').write_text(json.dumps({
            'owner': 'codex-dual', 'schema': 1, 'root': str(self.manager.root),
            'profiles': [{'id': 'api', 'kind': 'api', 'name': 'API'}],
        }))
        self.manager.home('api').mkdir(parents=True)
        self.config = {
            'marketplaces': {
                'openai-bundled': {'source_type': 'local', 'source': '/native/.tmp/openai-bundled'},
                'custom': {'source_type': 'local', 'source': '/custom/plugins'},
            },
            'model': 'example',
            'mcp_servers': {'custom': {'command': '/custom/server'}},
        }

    def test_profile_config_localizes_bundled_source_only(self):
        original = copy.deepcopy(self.config)
        config = self.manager._profile_config('api', self.config, 'example')
        self.assertEqual(config['marketplaces']['openai-bundled']['source'],
                         str(self.manager.home('api') / '.tmp/bundled-marketplaces/openai-bundled'))
        self.assertEqual(config['marketplaces']['custom'], original['marketplaces']['custom'])
        self.assertEqual(config['mcp_servers']['duo_history']['args'][-1], str(self.manager.history_sqlite()))
        self.assertEqual(self.config, original)

    def test_existing_config_repair_is_targeted_and_idempotent(self):
        path = self.manager.home('api') / 'config.toml'
        path.write_text(toml_dump(self.config))
        self.assertTrue(self.manager.repair_bundled_marketplace('api'))
        fixed = read_toml(path)
        expected = copy.deepcopy(self.config)
        expected['marketplaces']['openai-bundled']['source'] = str(
            self.manager.home('api') / '.tmp/bundled-marketplaces/openai-bundled')
        self.assertEqual(fixed, expected)
        modified = path.stat().st_mtime_ns
        self.assertFalse(self.manager.repair_bundled_marketplace('api'))
        self.assertEqual(path.stat().st_mtime_ns, modified)

    def test_absent_or_remote_marketplace_is_preserved(self):
        for config in ({}, {'marketplaces': {'openai-bundled': {'source_type': 'git', 'source': 'example'}}}):
            original = copy.deepcopy(config)
            self.manager._localize_bundled_marketplace('api', config)
            self.assertEqual(config, original)

    def test_existing_history_tool_registration_preserves_other_settings(self):
        path = self.manager.home('api') / 'config.toml'
        path.write_text(toml_dump(self.config))
        self.manager.repair_history_tools('api')
        fixed = read_toml(path)
        expected = copy.deepcopy(self.config)
        expected['mcp_servers']['duo_history'] = self.manager._history_tools_config()
        self.assertEqual(fixed, expected)
        modified = path.stat().st_mtime_ns
        self.manager.repair_history_tools('api')
        self.assertEqual(path.stat().st_mtime_ns, modified)


if __name__ == '__main__':
    unittest.main()
