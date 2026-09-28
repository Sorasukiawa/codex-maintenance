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

    def plugin(self, name='portable', portable=True, **fields):
        root = self.home / 'plugins' / 'cache' / 'market' / name / '1.0'
        root.mkdir(parents=True)
        data = {'name': name, 'version': '1.0', **fields}
        manifest = root / 'plugin.json'
        if portable:
            data['$schema'] = m.PORTABLE_PLUGIN_SCHEMA
        else:
            manifest = root / '.codex-plugin' / 'plugin.json'
            manifest.parent.mkdir()
        manifest.write_text(json.dumps(data), encoding='utf-8')
        return root

    def test_portable_components_ignore_legacy_and_inline_overrides(self):
        root = self.plugin(extensions={'com.openai': {'skills': './ignored', 'mcpServers': './ignored.json'}})
        self.skill(root / 'skills' / 'actual', 'yes', name='actual')
        self.skill(root / 'ignored' / 'wrong', 'no', name='wrong')
        (root / 'mcp.json').write_text(json.dumps({'mcpServers': {
            'actual': {'type': 'streamable-http', 'url': 'https://SECRET.example/mcp', 'headers': {'TOKEN': 'SECRET'}}}}))
        (root / '.codex-plugin').mkdir()
        (root / '.codex-plugin' / 'plugin.json').write_text(json.dumps({'skills': './ignored'}))
        (root / '.mcp.json').write_text(json.dumps({'mcpServers': {'wrong': {'command': 'SECRET'}}}))
        data = m.inventory(self.home)
        self.assertEqual([x['name'] for x in data['skills'] if x['owner'] == 'plugin'], ['actual'])
        self.assertEqual([x['name'] for x in data['mcp']], ['actual'])
        self.assertEqual(data['mcp'][0]['transport'], 'streamable-http')
        self.assertEqual(len(data['plugins']), 1)
        self.assertEqual(data['plugins'][0]['format'], 'portable')
        self.assertFalse(data['errors'])
        self.assertNotIn('SECRET', json.dumps(data))

    def test_legacy_declared_component_paths_and_inline_mcp(self):
        root = self.plugin('legacy', portable=False, skills=['./workflows/', './extra'],
                           mcpServers=['./server.json', './second.json'])
        self.skill(root / 'workflows' / 'one', 'yes', name='one')
        self.skill(root / 'extra', 'yes', name='two')
        self.skill(root / 'examples' / 'ignored', 'no')
        for filename, name in [('server.json', 'one'), ('second.json', 'two')]:
            (root / filename).write_text(json.dumps({'mcpServers': {name: {'command': 'echo'}}}))
        other = self.plugin('inline', portable=False, mcpServers={'inline': {'command': 'echo'}})
        data = m.inventory(self.home)
        self.assertEqual({x['name'] for x in data['skills'] if x['owner'] == 'plugin'}, {'one', 'two'})
        self.assertEqual({x['name'] for x in data['mcp']}, {'one', 'two', 'inline'})
        self.assertFalse(data['errors'])

    def test_invalid_component_path_does_not_escape_or_leak(self):
        root = self.plugin(portable=False, skills=['./../SECRET', './good'], mcpServers='https://SECRET.example')
        self.skill(root / 'good', 'yes', name='good')
        data = m.inventory(self.home)
        self.assertEqual(data['counts']['plugin'], 1)
        self.assertEqual(len(data['errors']), 2)
        self.assertFalse(data['complete'])
        self.assertNotIn('SECRET', json.dumps(data))

    def test_unknown_portable_schema_is_reported(self):
        root = self.plugin()
        (root / 'plugin.json').write_text('{"$schema":"SECRET-unknown-version"}')
        data = m.inventory(self.home)
        self.assertEqual(data['plugins'], [])
        self.assertEqual(data['errors'][0]['code'], 'unsupported-plugin-schema')
        self.assertNotIn('SECRET', json.dumps(data))

    def test_metadata_inventory_skips_hashing_deep_detects_content(self):
        with patch.object(m, 'snapshot', side_effect=AssertionError('should not hash')):
            data = m.inventory(self.home)
        self.assertFalse(data['skills'][0]['content_hashed'])
        self.assertNotIn('fingerprint', data['skills'][0])
        before = m.inventory(self.home, deep=True)['skills'][0]['fingerprint']
        (self.target / 'helper').write_text('changed')
        after = m.inventory(self.home, deep=True)['skills'][0]
        self.assertTrue(after['content_hashed'])
        self.assertNotEqual(before, after['fingerprint'])

    def test_unreadable_plugin_subtree_preserves_other_results(self):
        root = self.plugin('broken')
        self.skill(root / 'skills' / 'bad', 'x')
        good = self.plugin('good')
        self.skill(good / 'skills' / 'good', 'x', name='good')
        original = Path.iterdir
        def denied(path):
            if path == root / 'skills':
                raise PermissionError('SECRET error payload')
            return original(path)
        with patch.object(Path, 'iterdir', denied):
            data = m.inventory(self.home)
        self.assertEqual(data['counts']['user'], 1)
        self.assertEqual(data['counts']['plugin'], 1)
        self.assertEqual(len(data['plugins']), 2)
        self.assertEqual(data['errors'][0]['code'], 'permission-denied')
        self.assertFalse(data['complete'])
        self.assertNotIn('SECRET', json.dumps(data))

    def test_deep_hash_failure_is_isolated(self):
        other = self.skill(self.home / 'skills' / 'other', 'x', name='other')
        original = m.snapshot
        def denied(path):
            if path == self.target:
                raise PermissionError('SECRET')
            return original(path)
        with patch.object(m, 'snapshot', side_effect=denied):
            data = m.inventory(self.home, deep=True)
        self.assertEqual(data['counts']['user'], 2)
        self.assertTrue(next(row for row in data['skills'] if row['path'] == str(other))['content_hashed'])
        self.assertFalse(data['complete'])
        self.assertNotIn('SECRET', json.dumps(data))

    def test_bad_mcp_file_keeps_skills_and_other_plugins(self):
        root = self.plugin('bad')
        self.skill(root / 'skills' / 'kept', 'x')
        (root / 'mcp.json').write_text('{SECRET-invalid-json')
        self.plugin('good')
        data = m.inventory(self.home)
        self.assertEqual(len(data['plugins']), 2)
        self.assertEqual(data['counts']['plugin'], 1)
        self.assertEqual(data['errors'][0]['code'], 'invalid-data')
        self.assertNotIn('SECRET', json.dumps(data))

    def test_unregistered_skills_are_in_update_coverage_without_writes(self):
        data = m.check_updates(self.home)
        self.assertEqual(data['entries'][0]['status'], 'unregistered')
        self.assertEqual(data['summary']['installed_user_skills'], 1)
        self.assertEqual(data['summary']['checked'], 0)
        self.assertEqual(data['summary']['unregistered'], 1)
        self.assertFalse((self.home / 'maintenance').exists())

    def test_update_coverage_distinguishes_registered_local_and_missing(self):
        m.register(self.home, self.target)
        other = self.skill(self.home / 'skills' / 'other', 'x', name='other')
        m.register(self.home, other)
        other.rename(self.root / 'retained')
        data = m.check_updates(self.home)
        self.assertEqual(data['summary']['registered_entries'], 2)
        self.assertEqual(data['summary']['local-or-unknown-source'], 1)
        self.assertEqual(data['summary']['unknown'], 1)
        self.assertEqual(next(row for row in data['entries'] if row['status'] == 'unknown')['code'], 'path-missing')

    def test_corrupt_registry_does_not_claim_unregistered_or_up_to_date(self):
        folder = self.home / 'maintenance'
        folder.mkdir()
        path = folder / 'registry.json'
        path.write_text('{SECRET')
        data = m.check_updates(self.home)
        self.assertFalse(data['summary']['registry_readable'])
        self.assertEqual(data['entries'][0]['status'], 'registry-unavailable')
        self.assertEqual(path.read_text(), '{SECRET')
        self.assertNotIn('SECRET', json.dumps(data))

    def test_malformed_registry_entry_does_not_hide_good_entries(self):
        m.register(self.home, self.target)
        path = self.home / 'maintenance' / 'registry.json'
        data = json.loads(path.read_text())
        data['entries']['broken'] = 42
        path.write_text(json.dumps(data))
        result = m.check_updates(self.home)
        self.assertEqual(result['summary']['local-or-unknown-source'], 1)
        self.assertEqual(result['summary']['unknown'], 1)

    def test_update_error_preserves_safe_reason_code(self):
        upstream = {'SKILL.md': {'git_sha': '0' * 40, 'executable': False}}
        with patch.object(m, 'github_tree', return_value=upstream):
            m.register(self.home, self.target, 'owner/repo', 'skills/demo', 'a' * 40)
        with patch.object(m, 'resolve_ref', side_effect=m.MaintenanceError('GitHub request timed out', 'network-timeout')):
            data = m.check_updates(self.home)
        self.assertEqual(data['entries'][0]['code'], 'network-timeout')
        self.assertEqual(data['summary']['unknown'], 1)
        self.assertIn('timed out', data['entries'][0]['error'])

    def test_github_http_and_network_errors_are_classified_and_redacted(self):
        from urllib.error import HTTPError, URLError
        cases = [(HTTPError('https://SECRET', 429, 'SECRET', {}, None), 'github-rate-limited'),
                 (HTTPError('https://SECRET', 403, 'SECRET', {'X-RateLimit-Remaining': '0'}, None), 'github-rate-limited'),
                 (HTTPError('https://SECRET', 404, 'SECRET', {}, None), 'upstream-not-found'),
                 (HTTPError('https://SECRET', 500, 'SECRET', {}, None), 'github-http-error'),
                 (URLError('SECRET'), 'network-error'), (TimeoutError('SECRET'), 'network-timeout')]
        for error, code in cases:
            with self.subTest(code=code):
                m.github_json.cache_clear()
                with patch.object(m, 'urlopen', side_effect=error):
                    with self.assertRaises(m.MaintenanceError) as raised:
                        m.github_json('owner/repo/commits/main')
                self.assertEqual(raised.exception.code, code)
                self.assertNotIn('SECRET', str(raised.exception))

    def test_upstream_path_missing_and_truncated_have_different_codes(self):
        for payload, code in [({'tree': []}, 'upstream-path-missing'),
                              ({'tree': [], 'truncated': True}, 'upstream-tree-incomplete')]:
            with patch.object(m, 'github_json', return_value=payload):
                with self.assertRaises(m.MaintenanceError) as raised:
                    m.github_tree('owner/repo', 'skills/demo', 'a' * 40)
            self.assertEqual(raised.exception.code, code)

    def test_update_discovery_failure_is_visible_with_partial_results(self):
        m.register(self.home, self.target)
        original = Path.iterdir
        def denied(path):
            if path == self.home / 'skills':
                raise PermissionError('SECRET')
            return original(path)
        with patch.object(Path, 'iterdir', denied):
            data = m.check_updates(self.home)
        self.assertFalse(data['summary']['discovery_complete'])
        self.assertEqual(data['summary']['local-or-unknown-source'], 1)
        self.assertEqual(data['errors'][0]['code'], 'permission-denied')

    def test_linked_components_are_not_read(self):
        root = self.plugin()
        outside = self.skill(self.root / 'outside', 'SECRET', name='outside')
        self.directory_link(root / 'skills', outside)
        data = m.inventory(self.home)
        self.assertEqual(data['counts']['plugin'], 0)
        self.assertTrue(data['aliases'])
        self.assertFalse(data['complete'])

    def test_cli_metadata_and_deep_modes(self):
        for flags, hashed in [([], False), (['--deep'], True)]:
            run = subprocess.run([sys.executable, '-B', str(SCRIPT), '--home', str(self.home), 'inventory', *flags],
                                 capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(run.stdout)['skills'][0]['content_hashed'], hashed)


    def test_explicit_missing_components_are_incomplete(self):
        self.plugin(portable=False, skills='./absent', mcpServers='./absent.json')
        data = m.inventory(self.home)
        self.assertEqual(len(data['plugins']), 1)
        self.assertEqual([row['code'] for row in data['errors']], ['component-missing', 'component-missing'])
        self.assertFalse(data['complete'])

    def test_update_discovery_ignores_unrelated_mcp_config(self):
        (self.home / 'config.toml').write_text('invalid SECRET config')
        data = m.check_updates(self.home)
        self.assertTrue(data['summary']['discovery_complete'])
        self.assertFalse(data['errors'])
        self.assertEqual(data['summary']['unregistered'], 1)

    def test_inaccessible_mcp_command_preserves_other_rows(self):
        errors = []
        with patch.object(Path, 'is_file', side_effect=PermissionError('SECRET')):
            rows = m.mcp_rows({'bad': {'command': sys.executable}, 'good': {'command': 'relative'}}, 'test', errors)
        self.assertEqual(len(rows), 2)
        self.assertIsNone(rows[0]['absolute_command_exists'])
        self.assertEqual(errors[0]['code'], 'permission-denied')
        self.assertNotIn('SECRET', json.dumps(errors))

    @unittest.skipUnless(m.shutil.which('ruby'), 'Existing Ruby Psych runtime')
    def test_batch_yaml_keeps_valid_documents_next_to_invalid_yaml(self):
        with patch.dict(sys.modules, {'yaml': None}):
            values = m.parse_yaml_batch(['name: one', 'invalid: [SECRET', 'policy:\n  allow_implicit_invocation: false'])
        self.assertEqual(values[0], {'name': 'one'})
        self.assertIsInstance(values[1], m.MaintenanceError)
        self.assertNotIn('SECRET', str(values[1]))
        self.assertFalse(values[2]['policy']['allow_implicit_invocation'])

    def test_batched_inventory_preserves_valid_skills_and_policy(self):
        other = self.skill(self.home / 'skills' / 'other', 'good', name='other')
        (other / 'agents').mkdir()
        (other / 'agents' / 'openai.yaml').write_text('policy:\n  allow_implicit_invocation: false\n')
        (self.target / 'SKILL.md').write_text('---\nname: [SECRET\ndescription: test\n---\n')
        result = m.inventory(self.home, deep=True)
        good = next(row for row in result['skills'] if row.get('name') == 'other')
        self.assertFalse(good['implicit_invocation'])
        self.assertTrue(good['content_hashed'])
        bad = next(row for row in result['skills'] if row['path'] == str(self.target))
        self.assertEqual(bad['code'], 'invalid-yaml')
        self.assertFalse(bad['content_hashed'])
        self.assertNotIn('SECRET', json.dumps(result))

    @unittest.skipUnless(m.shutil.which('ruby'), 'Existing Ruby Psych runtime')
    def test_large_inventory_batches_keep_all_metadata(self):
        for index in range(70):
            skill = self.skill(self.home / 'skills' / f'demo-{index}', 'body', name=f'demo-{index}')
            (skill / 'agents').mkdir()
            (skill / 'agents' / 'openai.yaml').write_text('policy:\n  allow_implicit_invocation: false\n')
        original = m.subprocess.run
        with patch.dict(sys.modules, {'yaml': None}), patch.object(m.subprocess, 'run', wraps=original) as runner:
            result = m.inventory(self.home)
        self.assertEqual(result['counts']['user'], 71)
        self.assertFalse(result['errors'])
        self.assertEqual(sum(row['implicit_invocation'] is False for row in result['skills']), 70)
        self.assertLessEqual(runner.call_count, 2)

    def test_missing_yaml_parser_returns_partial_report(self):
        with patch.dict(sys.modules, {'yaml': None}), patch.object(m.shutil, 'which', return_value=None):
            result = m.inventory(self.home)
        self.assertEqual(result['counts']['user'], 1)
        self.assertEqual(result['skills'][0]['code'], 'yaml-parser-unavailable')
        self.assertFalse(result['complete'])

    def test_inventory_rereads_changed_invocation_metadata(self):
        (self.target / 'agents').mkdir()
        path = self.target / 'agents' / 'openai.yaml'
        path.write_text('policy:\n  allow_implicit_invocation: true\n')
        self.assertTrue(m.inventory(self.home)['skills'][0]['implicit_invocation'])
        path.write_text('policy:\n  allow_implicit_invocation: false\n')
        self.assertFalse(m.inventory(self.home)['skills'][0]['implicit_invocation'])

    def test_parser_timeout_is_reported_without_payload(self):
        with patch.object(m, 'parse_yaml_batch', side_effect=subprocess.TimeoutExpired('SECRET', 20, output='SECRET')):
            result = m.inventory(self.home)
        self.assertEqual(result['skills'][0]['code'], 'timeout')
        self.assertFalse(result['complete'])
        self.assertNotIn('SECRET', json.dumps(result))


    @unittest.skipUnless(m.shutil.which('ruby'), 'Existing Ruby Psych runtime')
    def test_non_utf8_yaml_binary_does_not_fail_other_documents(self):
        with patch.dict(sys.modules, {'yaml': None}):
            values = m.parse_yaml_batch(['name: good', 'name: !!binary //8=', 'name: other'])
        self.assertEqual(values[0], {'name': 'good'})
        self.assertIsInstance(values[1], m.MaintenanceError)
        self.assertEqual(values[2], {'name': 'other'})


if __name__ == '__main__':
    unittest.main()
