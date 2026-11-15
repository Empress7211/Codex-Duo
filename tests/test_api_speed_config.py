"""Check that a CC Switch speed default survives instance configuration sync."""
import json
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'source'))
from backend import Manager, toml_dump


class ApiSpeedConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.root, self.source, self.cc = base / 'duo', base / 'source', base / 'cc'
        self.root.mkdir()
        self.source.mkdir()
        self.cc.mkdir()
        (self.source / 'config.toml').write_text('model = "example"\n')
        self.manager = Manager(self.root, self.source, self.cc)
        (self.root / 'manifest.json').write_text(json.dumps({
            'schema': 1, 'owner': 'codex-dual', 'root': str(self.manager.root),
            'profiles': [{'id': 'api', 'name': 'API', 'kind': 'api'}],
        }))
        self.manager.home('api').mkdir(parents=True)

    def import_provider(self, config, provider_id='one'):
        provider = {'config_toml': config, 'auth': {'auth_mode': 'apikey', 'OPENAI_API_KEY': 'test-key'}}
        with patch('backend.list_providers', return_value=[{'id': provider_id, 'name': provider_id, 'kind': 'api_key'}]), \
             patch('backend.load_provider', return_value=provider), \
             patch.object(self.manager, 'require_upgrade_ready'), \
             patch.object(self.manager, 'require_stopped'):
            imported = self.manager.configure_api(provider_id)
            synced = self.manager.sync_settings()
        return tomllib.loads((self.manager.home('api') / 'config.toml').read_text()), imported, synced

    def test_imported_tier_survives_settings_sync(self):
        config, _, _ = self.import_provider('model = "example"\nservice_tier = "fast"\n')
        self.assertEqual(config['service_tier'], 'fast')
        self.assertNotIn('features', config)

    def test_missing_tier_does_not_enable_fast(self):
        config, _, _ = self.import_provider('model = "example"\n')
        self.assertNotIn('service_tier', config)
        self.assertNotIn('features', config)

    def test_explicit_fast_mode_false_is_preserved_and_reported(self):
        (self.source / 'config.toml').write_text('model = "example"\n[features]\nfast_mode = false\n')
        config, imported, synced = self.import_provider('model = "example"\nservice_tier = "priority"\n')
        self.assertFalse(config['features']['fast_mode'])
        self.assertEqual(config['service_tier'], 'priority')
        self.assertIn('不会随请求发送', imported['message'])
        self.assertIn('不会随请求发送', synced['message'])

    def test_native_selections_survive_sync_and_same_provider_reimport(self):
        self.import_provider('model = "example"\nservice_tier = "priority"\n')
        path = self.manager.home('api') / 'config.toml'
        config = tomllib.loads(path.read_text())
        config['service_tier'] = 'default'
        config['profiles'] = {'work': {'service_tier': 'fast'}}
        path.write_text(toml_dump(config))
        with patch.object(self.manager, 'require_upgrade_ready'), patch.object(self.manager, 'require_stopped'):
            self.manager.sync_settings()
        config = tomllib.loads(path.read_text())
        self.assertEqual(config['service_tier'], 'default')
        self.assertEqual(config['profiles']['work']['service_tier'], 'fast')
        config, _, _ = self.import_provider('model = "example"\nservice_tier = "fast"\n')
        self.assertEqual(config['service_tier'], 'default')
        self.assertEqual(config['profiles']['work']['service_tier'], 'fast')
        record = json.loads(self.manager.provider_path('api').read_text())
        self.assertEqual(record['service_tier'], 'fast')

    def test_switching_provider_preserves_existing_window_selection(self):
        self.import_provider('model = "example"\nservice_tier = "fast"\n')
        path = self.manager.home('api') / 'config.toml'
        config = tomllib.loads(path.read_text())
        config['profiles'] = {'work': {'service_tier': 'fast'}}
        path.write_text(toml_dump(config))
        config, _, _ = self.import_provider('model = "example"\nservice_tier = "default"\n', provider_id='two')
        self.assertEqual(config['service_tier'], 'fast')
        self.assertEqual(config['profiles']['work']['service_tier'], 'fast')
        record = json.loads(self.manager.provider_path('api').read_text())
        self.assertEqual(record['service_tier'], 'default')

    def test_new_provider_default_applies_when_no_existing_selection(self):
        self.import_provider('model = "example"\n')
        config, _, _ = self.import_provider('model = "example"\nservice_tier = "fast"\n', provider_id='two')
        self.assertEqual(config['service_tier'], 'fast')


if __name__ == '__main__':
    unittest.main()
