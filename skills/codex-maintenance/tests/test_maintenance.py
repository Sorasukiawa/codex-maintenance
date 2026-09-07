import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'maintenance.py'
spec = importlib.util.spec_from_file_location('maintenance', SCRIPT)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / 'codex'
        self.target = self.skill(self.home / 'skills' / 'demo', 'old')
        self.candidate = self.skill(self.root / 'candidate', 'new')

    def skill(self, path, body, name='demo'):
        path.mkdir(parents=True, exist_ok=True)
        (path / 'SKILL.md').write_text(f'---\nname: {name}\ndescription: Example skill\n---\n{body}\n')
        return path

    def plan(self):
        return m.make_plan(self.home, self.target, self.candidate)

    def test_companion_and_mode_changes_are_visible(self):
        (self.target / 'helper.py').write_text('old')
        (self.candidate / 'helper.py').write_text('new')
        (self.target / 'removed').write_text('x')
        (self.candidate / 'added').write_text('x')
        (self.candidate / 'SKILL.md').write_bytes((self.target / 'SKILL.md').read_bytes())
        (self.candidate / 'SKILL.md').chmod(0o755)
        d = m.compare(self.target, self.candidate)
        self.assertEqual(d['added'], ['added'])
        self.assertEqual(d['removed'], ['removed'])
        self.assertEqual(d['changed'], ['SKILL.md', 'helper.py'])

    def test_plan_is_readonly_update_and_rollback_restore_bytes(self):
        before = m.snapshot(self.target)
        p = self.plan()
        self.assertEqual(m.snapshot(self.target), before)
        self.assertFalse((self.home / 'maintenance').exists())
        receipt = m.apply_plan(self.home, p)
        self.assertEqual((self.target / 'SKILL.md').read_bytes(), (self.candidate / 'SKILL.md').read_bytes())
        self.assertTrue(Path(receipt['backup']).is_dir())
        m.rollback(self.home, receipt['id'])
        self.assertEqual(m.snapshot(self.target)['files'], before['files'])

    def test_target_and_candidate_drift_rejected(self):
        for item in (self.target, self.candidate):
            with self.subTest(item=item):
                p = self.plan()
                (item / 'extra').write_text('later change')
                with self.assertRaises(m.MaintenanceError):
                    m.apply_plan(self.home, p)
                (item / 'extra').unlink()
        self.assertIn('old', (self.target / 'SKILL.md').read_text())

    def test_rollback_does_not_overwrite_later_edits(self):
        r = m.apply_plan(self.home, self.plan())
        (self.target / 'extra').write_text('important')
        with self.assertRaises(m.MaintenanceError):
            m.rollback(self.home, r['id'])
        self.assertTrue((self.target / 'extra').exists())

    def test_managed_and_symlink_targets_are_rejected(self):
        for path in (self.home / 'skills' / '.system' / 'demo', self.home / 'plugins' / 'cache' / 'demo'):
            self.skill(path, 'managed')
            with self.assertRaises(m.MaintenanceError):
                m.make_plan(self.home, path, self.candidate)
        linked = self.home / 'skills' / 'linked'
        linked.symlink_to(self.target, target_is_directory=True)
        with self.assertRaises(m.MaintenanceError):
            m.make_plan(self.home, linked, self.candidate)
        (self.candidate / 'escape').symlink_to(self.root)
        with self.assertRaises(m.MaintenanceError):
            self.plan()

    def test_failed_replacement_preserves_original(self):
        p = self.plan()
        original = os.replace
        def fail_stage(src, dst):
            if Path(src).name == 'staged' and Path(dst) == self.target:
                raise OSError('simulated replacement failure')
            return original(src, dst)
        with patch.object(m.os, 'replace', side_effect=fail_stage):
            with self.assertRaises(OSError):
                m.apply_plan(self.home, p)
        self.assertIn('old', (self.target / 'SKILL.md').read_text())

    def test_inventory_plugin_alias_disabled_mcp_and_redaction(self):
        plugin = self.home / 'plugins' / 'cache' / 'official' / 'example' / '1.0'
        self.skill(plugin / 'skills' / 'demo', 'plugin')
        (plugin / '.codex-plugin').mkdir()
        (plugin / '.codex-plugin' / 'plugin.json').write_text(json.dumps({'name': 'example', 'version': '1.0'}))
        (plugin / '.mcp.json').write_text(json.dumps({'mcpServers': {'same': {'command': '/bin/echo', 'env': {'TOKEN': 'SECRET'}}}}))
        (plugin.parent / 'latest').symlink_to(plugin, target_is_directory=True)
        (self.home / 'config.toml').write_text('[mcp_servers.same]\nenabled=false\ncommand="/bin/echo"\nargs=["SECRET"]\nurl="https://name:SECRET@example.org/x?token=SECRET"\n[mcp_servers.same.env]\nTOKEN="SECRET"\n')
        data = m.inventory(self.home)
        self.assertEqual(len(data['plugins']), 1)
        self.assertEqual(len(data['skills']), 2)
        self.assertEqual(len({s['id'] for s in data['skills']}), 2)
        self.assertEqual(len(data['mcp']), 2)
        self.assertFalse(next(s for s in data['mcp'] if s['source'] == 'user-config')['enabled'])
        self.assertTrue(data['aliases'])
        self.assertNotIn('SECRET', json.dumps(data))
        self.assertFalse((self.home / 'maintenance').exists())

    def test_register_retains_fixed_source_and_detects_customization(self):
        upstream = {'SKILL.md': {'git_sha': '0' * 40, 'executable': False}}
        with patch.object(m, 'github_tree', return_value=upstream):
            entry = m.register(self.home, self.target, 'owner/repo', 'skills/demo', 'a' * 40, 'main', 'Preserve local behavior')
        self.assertEqual(entry['source']['commit'], 'a' * 40)
        self.assertEqual(entry['customization']['changed'], ['SKILL.md'])
        with patch.object(m, 'resolve_ref', return_value='b' * 40), patch.object(m, 'github_tree', return_value=upstream):
            check = m.check_updates(self.home)
        self.assertFalse(check['entries'][0]['upstream_changed'])
        self.assertEqual(check['entries'][0]['local_since_registration']['changed'], [])

    def test_existing_official_uppercase_name_is_valid_yaml(self):
        self.skill(self.target, 'official content', name='Presentations')
        data = m.inventory(self.home)
        self.assertEqual(data['skills'][0]['name'], 'Presentations')
        self.assertTrue(data['skills'][0]['yaml_valid'])
        self.assertFalse(data['errors'])

    def test_same_content_replaced_directory_identity_rejected(self):
        import shutil
        p = self.plan()
        displaced = self.root / 'displaced'
        self.target.rename(displaced)
        shutil.copytree(displaced, self.target)
        with self.assertRaises(m.MaintenanceError):
            m.apply_plan(self.home, p)

    def test_plan_tampering_and_competing_lock_rejected(self):
        p = self.plan()
        p['candidate'] = str(self.root / 'different')
        with self.assertRaises(m.MaintenanceError):
            m.apply_plan(self.home, p)
        with m.state_lock(self.home):
            with self.assertRaises(m.MaintenanceError):
                m.apply_plan(self.home, self.plan())

    def test_incomplete_upstream_tree_is_unknown(self):
        with patch.object(m, 'github_json', return_value={'truncated': True, 'tree': []}):
            with self.assertRaises(m.MaintenanceError):
                m.github_tree('owner/repo', 'skills/demo', 'a' * 40)

    def test_malformed_mcp_fields_are_isolated_and_never_leak_values(self):
        for invalid in ({'env': 42}, {'args': None}, {'env': ['SECRET_VALUE_NOT_A_KEY']},
                        {'headers': ['SECRET_HEADER']}, {'enabled': 'yes'}):
            with self.subTest(invalid=invalid):
                plugin = self.home / 'plugins' / 'cache' / 'market' / 'plugin' / '1'
                (plugin / '.codex-plugin').mkdir(parents=True, exist_ok=True)
                (plugin / '.codex-plugin' / 'plugin.json').write_text('{}')
                (plugin / '.mcp.json').write_text(json.dumps({'mcpServers': {'bad': invalid, 'good': {'enabled': True}}}))
                data = m.inventory(self.home)
                self.assertEqual(len(data['skills']), 1)
                self.assertEqual([r['name'] for r in data['mcp']], ['good'])
                self.assertTrue(data['errors'])
                self.assertNotIn('SECRET', json.dumps(data))
        (self.home / 'config.toml').write_text('[mcp_servers.bad]\nenv=42\n[mcp_servers.good]\nenabled=false\n')
        self.assertTrue(m.inventory(self.home)['errors'])


if __name__ == '__main__':
    unittest.main()
