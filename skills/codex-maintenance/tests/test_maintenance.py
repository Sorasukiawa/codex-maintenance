import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
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
        (path / 'SKILL.md').write_text(f'---\nname: {name}\ndescription: Example skill\n---\n{body}\n', encoding='utf-8')
        return path

    def plan(self):
        return m.make_plan(self.home, self.target, self.candidate)

    def directory_link(self, link, destination):
        if os.name == 'nt':
            subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(destination)], check=True, capture_output=True)
            self.addCleanup(lambda: os.rmdir(link) if os.path.lexists(link) else None)
        else:
            link.symlink_to(destination, target_is_directory=True)

    def test_companion_and_mode_changes_are_visible(self):
        (self.target / 'helper.py').write_text('old')
        (self.candidate / 'helper.py').write_text('new')
        (self.target / 'removed').write_text('x')
        (self.candidate / 'added').write_text('x')
        (self.candidate / 'SKILL.md').write_bytes((self.target / 'SKILL.md').read_bytes())
        (self.candidate / 'SKILL.md').chmod(0o444 if os.name == 'nt' else 0o755)
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
        self.directory_link(linked, self.target)
        with self.assertRaises(m.MaintenanceError):
            m.make_plan(self.home, linked, self.candidate)
        self.directory_link(self.candidate / 'escape', self.root)
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
        self.directory_link(plugin.parent / 'latest', plugin)
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

    def test_help_does_not_require_unix_fcntl(self):
        code = "import runpy, sys; sys.modules['fcntl'] = None; sys.argv = [sys.argv[1], '--help']; runpy.run_path(sys.argv[0], run_name='__main__')"
        result = subprocess.run([sys.executable, '-B', '-c', code, str(SCRIPT)], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))

    def test_unicode_paths_and_json_with_legacy_stdout_encoding(self):
        self.home = self.root / '中文 繁體 日本語'
        self.target = self.skill(self.home / 'skills' / 'demo', '中文 繁體 日本語 🐈')
        result = subprocess.run([sys.executable, '-B', str(SCRIPT), '--home', str(self.home), 'inventory'],
                                env={**os.environ, 'PYTHONIOENCODING': 'cp1252', 'PYTHONUTF8': '0'}, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode('ascii', errors='replace'))
        report = json.loads(result.stdout)
        self.assertEqual(report['codex_home'], str(self.home))
        self.assertFalse(report['errors'])
        entry = m.register(self.home, self.target, note='简体 繁體 日本語 🐈')
        saved = json.loads((self.home / 'maintenance' / 'registry.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['entries'][entry['id']]['note'], entry['note'])
        receipt = m.apply_plan(self.home, self.plan())
        m.rollback(self.home, receipt['id'])
        self.assertIn('🐈', (self.target / 'SKILL.md').read_text(encoding='utf-8'))

    def test_utf8_bom_crlf_metadata_and_config(self):
        (self.target / 'SKILL.md').write_bytes('---\r\nname: demo\r\ndescription: 中文\r\n---\r\n日本語\r\n'.encode('utf-8-sig'))
        (self.home / 'config.toml').write_bytes('[mcp_servers.demo]\r\nenabled=false\r\n'.encode('utf-8-sig'))
        report = m.inventory(self.home)
        self.assertFalse(report['errors'])
        self.assertFalse(report['mcp'][0]['enabled'])

    def test_unknown_local_executable_bit_does_not_fake_customization(self):
        upstream = {'SKILL.md': {'git_sha': 'a' * 40, 'executable': True}}
        local = {'SKILL.md': {'git_sha': 'a' * 40, 'executable': None}}
        with patch.object(m, 'github_tree', return_value=upstream), patch.object(m, 'git_files', return_value=local):
            entry = m.register(self.home, self.target, 'owner/repo', 'skills/demo', 'a' * 40)
            self.assertEqual(entry['customization']['changed'], [])
            with patch.object(m, 'resolve_ref', return_value='b' * 40):
                changed = {'SKILL.md': {'git_sha': 'a' * 40, 'executable': False}}
                with patch.object(m, 'github_tree', return_value=changed):
                    check = m.check_updates(self.home)['entries'][0]
                self.assertTrue(check['upstream_changed'])
                self.assertEqual(check['local_vs_upstream']['changed'], [])

    def test_real_process_lock_contention_and_exit_release(self):
        code = ("import importlib.util, pathlib, sys; s=importlib.util.spec_from_file_location('m', sys.argv[1]); "
                "m=importlib.util.module_from_spec(s); s.loader.exec_module(m)\n"
                "with m.state_lock(pathlib.Path(sys.argv[2])):\n print('locked', flush=True); sys.stdin.readline()\n")
        child = subprocess.Popen([sys.executable, '-B', '-c', code, str(SCRIPT), str(self.home)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), 'locked')
            with self.assertRaises(m.MaintenanceError):
                with m.state_lock(self.home):
                    self.fail('Competing process acquired the lock')
            child.kill()
            child.communicate(timeout=10)
            with m.state_lock(self.home):
                pass
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)

    def test_inventory_does_not_cross_linked_parent_roots(self):
        outside = self.root / 'outside-skills'
        self.skill(outside / '.system' / 'external', 'external')
        self.home = self.root / 'linked-home'
        self.home.mkdir()
        self.directory_link(self.home / 'skills', outside)
        report = m.inventory(self.home)
        self.assertEqual(report['counts'], {'user': 0, 'system': 0, 'plugin': 0})
        self.assertTrue(report['aliases'])

    def test_plugin_metadata_and_nested_links_stay_outside_inventory(self):
        plugin = self.home / 'plugins' / 'cache' / 'market' / 'plugin' / '1'
        plugin.mkdir(parents=True)
        outside = self.root / 'outside-plugin'
        outside.mkdir()
        (outside / 'plugin.json').write_text('{}', encoding='utf-8')
        self.directory_link(plugin / '.codex-plugin', outside)
        report = m.inventory(self.home)
        self.assertEqual(report['plugins'], [])
        self.assertTrue(report['aliases'])

    @unittest.skipUnless(os.name == 'nt', 'Native Windows filesystem semantics')
    def test_windows_absolute_command_and_case_insensitive_id(self):
        rows = m.mcp_rows({'python': {'command': sys.executable}, 'missing': {'command': str(self.root / 'absent.exe')}}, 'test')
        self.assertTrue(rows[0]['absolute_command_exists'])
        self.assertFalse(rows[1]['absolute_command_exists'])
        self.assertEqual(m.instance_id(self.target), m.instance_id(str(self.target).upper()))

    @unittest.skipUnless(os.name == 'nt', 'Native Windows junctions')
    def test_windows_junctions_are_not_traversed_or_replaced(self):
        def junction(link, destination):
            subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(destination)], check=True, capture_output=True)
            self.addCleanup(lambda: os.rmdir(link) if os.path.lexists(link) else None)
        linked = self.home / 'skills' / 'linked'
        junction(linked, self.target)
        report = m.inventory(self.home)
        self.assertEqual(len(report['skills']), 1)
        self.assertTrue(report['aliases'])
        with self.assertRaises(m.MaintenanceError):
            m.make_plan(self.home, linked, self.candidate)
        junction(self.candidate / 'escape', self.target)
        with self.assertRaises(m.MaintenanceError):
            self.plan()
        outside = self.root / 'outside-state'
        outside.mkdir()
        junction(self.home / 'maintenance', outside)
        with self.assertRaises(m.MaintenanceError):
            with m.state_lock(self.home):
                self.fail('Junction state accepted')
        self.assertFalse(list(outside.iterdir()))

    @unittest.skipUnless(os.name == 'nt', 'Native Windows file sharing')
    def test_windows_open_file_prevents_update_and_keeps_original(self):
        p = self.plan()
        with (self.target / 'SKILL.md').open('rb'):
            with self.assertRaises(OSError):
                m.apply_plan(self.home, p)
        self.assertIn('old', (self.target / 'SKILL.md').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
