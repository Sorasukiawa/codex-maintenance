#!/usr/bin/env python3
"""Codex component inventory and staged user-skill maintenance. Python 3.11+."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import errno
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
from urllib.parse import quote
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import uuid


class MaintenanceError(Exception):
    def __init__(self, message, code='maintenance-error'):
        super().__init__(message)
        self.code = code


def error_info(error):
    """Only our own messages are safe to report; parser/OS payloads may contain secrets."""
    if isinstance(error, MaintenanceError):
        return {'code': error.code, 'error': str(error)}
    codes = {PermissionError: 'permission-denied', FileNotFoundError: 'path-missing',
             TimeoutError: 'timeout', subprocess.TimeoutExpired: 'timeout',
             ValueError: 'invalid-data', TypeError: 'invalid-data', KeyError: 'invalid-data',
             OSError: 'filesystem-error'}
    code = next((code for kind, code in codes.items() if isinstance(error, kind)), 'unexpected-error')
    return {'code': code, 'error': type(error).__name__ + '; details omitted'}


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def absolute(path):
    return Path(os.path.abspath(Path(path).expanduser()))


def is_link(path, info=None):
    """Include Windows junctions and other reparse points on Python 3.11+."""
    try:
        info = info or Path(path).lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def linked_path(path):
    return any(is_link(p) for p in (path, *path.parents))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.maintenance-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def snapshot(path):
    """Hash all entries, modes and link text; never traverse directory symlinks."""
    path = absolute(path)
    root = path.lstat()
    if is_link(path, root) or not stat.S_ISDIR(root.st_mode):
        raise MaintenanceError('Snapshot root must be a real directory')
    files = {}
    def walk(folder):
        for item in sorted(folder.iterdir()):
            info = item.lstat()
            row = {'mode': stat.S_IMODE(info.st_mode)}
            if stat.S_ISLNK(info.st_mode):
                row.update(type='symlink', target=os.readlink(item))
            elif is_link(item, info):
                row.update(type='reparse-point', tag=getattr(info, 'st_reparse_tag', None))
            elif stat.S_ISDIR(info.st_mode):
                row.update(type='directory')
            elif stat.S_ISREG(info.st_mode):
                data = item.read_bytes()
                row.update(type='file', size=len(data), sha256=hashlib.sha256(data).hexdigest(),
                           git_sha=hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest())
            else:
                raise MaintenanceError('Unsupported special file in directory')
            files[item.relative_to(path).as_posix()] = row
            if row['type'] == 'directory':
                walk(item)
    walk(path)
    mode = stat.S_IMODE(root.st_mode)
    return {'path': str(path), 'identity': [root.st_dev, root.st_ino], 'mode': mode,
            'files': files, 'fingerprint': digest({'mode': mode, 'files': files})}


def diff_maps(before, after):
    return {'added': sorted(after.keys() - before.keys()), 'removed': sorted(before.keys() - after.keys()),
            'changed': sorted(k for k in before.keys() & after.keys() if before[k] != after[k])}


def compare(before, after):
    left, right = snapshot(before), snapshot(after)
    return {**diff_maps(left['files'], right['files']), 'root_mode_changed': left['mode'] != right['mode'],
            'before': left, 'after': right}


def parse_yaml_batch(texts):
    """Parse independently, amortizing Ruby startup without caching mutable files."""
    if not texts:
        return []
    try:
        import yaml
    except ImportError:
        ruby = shutil.which('ruby')
        if not ruby:
            raise MaintenanceError('YAML parser unavailable: use an existing PyYAML or Ruby Psych runtime',
                                   'yaml-parser-unavailable')
        code = """values = JSON.parse(STDIN.read).map do |text|
  begin
    value = YAML.safe_load(text, permitted_classes: [], aliases: false)
    JSON.parse(JSON.generate({value: value}))
  rescue Psych::Exception, ArgumentError, JSON::GeneratorError, EncodingError
    {error: true}
  end
end
STDOUT.write(JSON.generate(values))"""
        result = subprocess.run([ruby, '-EUTF-8:UTF-8', '-rjson', '-ryaml', '-e', code],
                                input=json.dumps(texts), encoding='utf-8', capture_output=True, timeout=20)
        if result.returncode:
            raise MaintenanceError('YAML parser failed; details omitted', 'yaml-parser-failed')
        values = json.loads(result.stdout)
        if not isinstance(values, list) or len(values) != len(texts) or not all(isinstance(v, dict) for v in values):
            raise MaintenanceError('Invalid YAML parser response; details omitted', 'yaml-parser-failed')
        return [MaintenanceError('Invalid YAML', 'invalid-yaml') if value.get('error') else value['value']
                for value in values]
    values = []
    for text in texts:
        try:
            values.append(yaml.safe_load(text))
        except yaml.YAMLError:
            values.append(MaintenanceError('Invalid YAML', 'invalid-yaml'))
    return values


def yaml_value(value):
    if isinstance(value, MaintenanceError):
        raise value
    return value


def parse_yaml(text):
    return yaml_value(parse_yaml_batch([text])[0])


def skill_documents(path):
    text = (path / 'SKILL.md').read_text(encoding='utf-8-sig')
    match = re.match(r'\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)', text, re.S)
    if not match:
        raise MaintenanceError('Missing skill YAML frontmatter')
    documents = [match.group(1)]
    ui_path = path / 'agents' / 'openai.yaml'
    if ui_path.exists():
        documents.append(ui_path.read_text(encoding='utf-8-sig'))
    return documents


def metadata_from_values(values):
    data = yaml_value(values[0])
    if not isinstance(data, dict) or not isinstance(data.get('name'), str) or not data['name'].strip():
        raise MaintenanceError('Invalid skill name')
    if not isinstance(data.get('description'), str) or not data['description'].strip():
        raise MaintenanceError('Missing skill description')
    implicit = 'default'
    if len(values) > 1:
        ui = yaml_value(values[1])
        if not isinstance(ui, dict):
            raise MaintenanceError('Invalid agents/openai.yaml mapping')
        policy = ui.get('policy', {})
        if not isinstance(policy, dict):
            raise MaintenanceError('Invalid invocation policy')
        implicit = policy.get('allow_implicit_invocation', 'default')
        if implicit != 'default' and not isinstance(implicit, bool):
            raise MaintenanceError('Invalid invocation policy value')
    return {'name': data['name'], 'implicit_invocation': implicit, 'yaml_valid': True,
            'name_matches_authoring_convention': bool(re.fullmatch(r'[a-z0-9-]{1,64}', data['name']))}


def skill_metadata(path):
    return metadata_from_values(parse_yaml_batch(skill_documents(path)))


def instance_id(path):
    return digest(os.path.normcase(str(absolute(path))))[:20]


def mcp_rows(mapping, source, errors=None, portable=False):
    rows = []
    errors = errors if errors is not None else []
    if not isinstance(mapping, dict):
        errors.append({'source': source, 'code': 'invalid-mcp-config', 'error': 'MCP collection must be a mapping; values omitted'})
        return rows
    for name, value in mapping.items():
        if not isinstance(value, dict):
            errors.append({'source': source, 'name': name, 'code': 'invalid-mcp-config', 'error': 'MCP entry must be a mapping; values omitted'})
            continue
        maps = ('env', 'headers', 'http_headers', 'env_http_headers')
        lists = ('args', 'env_vars')
        valid = all(isinstance(value.get(key, {}), dict) and
                    all(isinstance(k, str) and isinstance(v, str) for k, v in value.get(key, {}).items()) for key in maps)
        valid = valid and all(isinstance(value.get(key, []), list) and
                              all(isinstance(v, str) for v in value.get(key, [])) for key in lists)
        valid = valid and ('enabled' not in value or isinstance(value['enabled'], bool))
        valid = valid and all(key not in value or isinstance(value[key], str) for key in ('command', 'url', 'cwd'))
        if not valid:
            errors.append({'source': source, 'name': name, 'code': 'invalid-mcp-config', 'error': 'Invalid MCP field types; values omitted'})
            continue
        if portable and value.get('type') not in ('stdio', 'streamable-http', 'sse'):
            errors.append({'source': source, 'name': name, 'code': 'invalid-transport',
                           'error': 'Missing or unsupported portable MCP transport; values omitted'})
            continue
        command = value.get('command')
        command_exists = None
        if isinstance(command, str) and Path(command).is_absolute():
            try:
                command_exists = Path(command).is_file()
            except OSError as error:
                errors.append({'source': source, 'name': name, **error_info(error)})
        rows.append({'name': name, 'source': source, 'enabled': value.get('enabled', 'unspecified'),
                     'transport': value['type'] if portable else ('url' if value.get('url') else 'stdio'), 'absolute_command_exists': command_exists,
                     'argument_count': len(value.get('args', [])),
                     'env_keys': sorted(set(value.get('env', {})) | set(value.get('env_vars', []))),
                     'header_keys': sorted(set(value.get('headers', {})) | set(value.get('http_headers', {})) |
                                           set(value.get('env_http_headers', {}))),
                     'connection_tested': False})
    return rows


PORTABLE_PLUGIN_SCHEMA = 'https://agent-plugins.org/schemas/1.0.0/plugin.schema.json'


def package_path(root, value):
    # Check text before normalizing, including Windows drive and backslash escapes.
    if (not isinstance(value, str) or not value.startswith('./') or '\\' in value or
            ':' in value or any(part in ('', '.', '..') for part in value[2:].removesuffix('/').split('/'))):
        raise MaintenanceError('Expected a ./ path inside the plugin', 'invalid-component-path')
    return root / value[2:]


def inventory(home, deep=False, owners=('user', 'system', 'plugin'), include_config=True):
    home = absolute(home)
    report = {'schema': 2, 'created_at': now(), 'codex_home': str(home), 'skills': [], 'plugins': [],
              'mcp': [], 'aliases': [], 'errors': [], 'mode': 'deep' if deep else 'metadata',
              'scope': 'filesystem and user config' if include_config else 'filesystem skills only',
              'runtime_state_checked': False, 'owners': list(owners)}
    caught = (MaintenanceError, OSError, ValueError, TypeError, RuntimeError, subprocess.TimeoutExpired)

    def failure(path, error):
        report['errors'].append({'path': str(path), **error_info(error)})

    def alias(path, owner):
        row = {'path': str(path), 'owner': owner}
        try:
            row['target'] = str(path.resolve())
        except (OSError, RuntimeError) as error:
            failure(path, error)
        if row not in report['aliases']:
            report['aliases'].append(row)

    def available(path, owner):
        if linked_path(path):
            alias(path, owner)
            return False
        try:
            path.stat()
        except FileNotFoundError:
            return False
        return True

    def json_mapping(path):
        data = json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(data, dict):
            raise MaintenanceError('Expected a JSON object; values omitted', 'invalid-data')
        return data

    config = {}
    cfg = home / 'config.toml'
    try:
        if include_config and available(cfg, 'user-config'):
            config = tomllib.loads(cfg.read_text(encoding='utf-8-sig'))
    except caught as error:
        failure(cfg, error)
    if include_config and 'user' in owners:
        report['mcp'] += mcp_rows(config.get('mcp_servers', {}), 'user-config', report['errors'])

    def directories(folder, owner):
        try:
            if not available(folder, owner):
                return
            children = sorted(folder.iterdir())
        except caught as error:
            failure(folder, error)
            return
        for path in children:
            try:
                if is_link(path):
                    alias(path, owner)
                elif path.is_dir():
                    yield path
            except caught as error:
                failure(path, error)

    seen = set()
    pending = []
    def skill(path, owner, plugin_id=None):
        row = {'id': instance_id(path), 'path': str(path), 'owner': owner, 'plugin_id': plugin_id,
               'content_hashed': False}
        try:
            if not available(path / 'SKILL.md', owner):
                return
            real = str(path.resolve())
            if real in seen:
                return
            seen.add(real)
            row['realpath'] = real
            ui = path / 'agents' / 'openai.yaml'
            if linked_path(ui):
                alias(ui, owner)
                raise MaintenanceError('Linked invocation metadata was not read', 'linked-metadata')
            pending.append((path, row, skill_documents(path)))
        except caught as error:
            row.update(error_info(error))
            failure(path, error)
        report['skills'].append(row)

    for base, owner in [(home / 'skills', 'user'), (home / 'skills' / '.system', 'system')]:
        if owner in owners:
            for path in directories(base, owner):
                if not path.name.startswith('.'):
                    skill(path, owner)

    def plugin_roots():
        for market in directories(home / 'plugins' / 'cache', 'plugin'):
            for package in directories(market, 'plugin'):
                yield from directories(package, 'plugin')

    def plugin_skills(folder, required=False):
        try:
            if not available(folder, 'plugin'):
                if required and not linked_path(folder):
                    raise MaintenanceError('Declared skill directory is missing', 'component-missing')
                return
        except caught as error:
            failure(folder, error)
            return
        yield folder
        for child in directories(folder, 'plugin'):
            if not child.name.startswith('.') and child.name not in ('node_modules', '__pycache__'):
                yield from plugin_skills(child)

    def component_paths(root, value):
        values = value if isinstance(value, list) else [value]
        paths = []
        for item in values:
            try:
                path = package_path(root, item)
                if path not in paths:
                    paths.append(path)
            except caught as error:
                # Never include the untrusted value (which may be a URL or secret) in diagnostics.
                failure(root, error)
        return paths

    def read_mcp(path, plugin_id, portable, required=False):
        try:
            if available(path, 'plugin'):
                data = json_mapping(path)
                report['mcp'] += mcp_rows(data.get('mcpServers', {}), plugin_id, report['errors'], portable=portable)
            elif required and not linked_path(path):
                raise MaintenanceError('Declared MCP configuration is missing', 'component-missing')
        except caught as error:
            failure(path, error)

    for root in plugin_roots() if 'plugin' in owners else ():
        manifest = root / 'plugin.json'
        legacy = root / '.codex-plugin' / 'plugin.json'
        plugin_id = root.parent.name + '@' + root.parent.parent.name
        try:
            portable = False
            if available(manifest, 'plugin'):
                data = json_mapping(manifest)
                portable = data.get('$schema') == PORTABLE_PLUGIN_SCHEMA
                if not portable:
                    failure(manifest, MaintenanceError('Unrecognized portable plugin schema', 'unsupported-plugin-schema'))
            if not portable:
                manifest = legacy
                if not available(manifest, 'plugin'):
                    continue
                data = json_mapping(manifest)
            plugin_settings = config.get('plugins', {})
            if not isinstance(plugin_settings, dict):
                raise MaintenanceError('Invalid plugin settings collection; values omitted', 'invalid-data')
            settings = plugin_settings.get(plugin_id, {})
            if not isinstance(settings, dict) or ('enabled' in settings and not isinstance(settings['enabled'], bool)):
                raise MaintenanceError('Invalid plugin settings; values omitted', 'invalid-data')
            report['plugins'].append({'id': plugin_id, 'path': str(root), 'version': data.get('version', root.name),
                                      'format': 'portable' if portable else 'legacy',
                                      'enabled_in_user_config': settings.get('enabled', 'unspecified'),
                                      'managed': True, 'effective_state': 'requires-native-query'})
            # Portable components are fixed. Inline extensions and legacy overlays cannot override them.
            skills = './skills' if portable else data.get('skills', './skills')
            for folder in component_paths(root, skills):
                for path in plugin_skills(folder, required=not portable and 'skills' in data):
                    skill(path, 'plugin', plugin_id)
            mcp = './mcp.json' if portable else data.get('mcpServers', './.mcp.json')
            if isinstance(mcp, dict) and not portable:
                report['mcp'] += mcp_rows(mcp.get('mcpServers', mcp), plugin_id, report['errors'])
            else:
                for path in component_paths(root, mcp):
                    read_mcp(path, plugin_id, portable, required=not portable and 'mcpServers' in data)
        except caught as error:
            failure(manifest, error)
    documents = [text for _, _, texts in pending for text in texts]
    parsed = []
    # Bound each parser request; a malformed document remains an individual failure.
    for start in range(0, len(documents), 128):
        batch = documents[start:start + 128]
        try:
            parsed.extend(parse_yaml_batch(batch))
        except caught as error:
            parsed.extend([MaintenanceError(error_info(error)['error'], error_info(error)['code'])] * len(batch))
    offset = 0
    for path, row, texts in pending:
        values = parsed[offset:offset + len(texts)]
        offset += len(texts)
        try:
            row.update(metadata_from_values(values))
            if deep:
                tree = snapshot(path)
                row.update(fingerprint=tree['fingerprint'], content_hashed=True,
                           file_count=sum(v['type'] == 'file' for v in tree['files'].values()))
        except caught as error:
            row.update(error_info(error))
            failure(path, error)
    report['counts'] = {owner: sum(s['owner'] == owner for s in report['skills']) for owner in ('user', 'system', 'plugin')}
    report['complete'] = not report['errors'] and not report['aliases']
    return report


def git_files(tree):
    # Generated Python caches are excluded from upstream comparison only, never backups/drift checks.
    return {p: {'git_sha': v['git_sha'], 'executable': None if os.name == 'nt' else bool(v['mode'] & 0o111)}
            for p, v in tree['files'].items() if v['type'] == 'file' and not p.endswith('.pyc')
            and '__pycache__' not in Path(p).parts}


def diff_git_maps(before, after):
    # Windows does not expose Git/POSIX executable bits; None means incomparable.
    result = diff_maps(before, after)
    result['changed'] = sorted(k for k in before.keys() & after.keys()
                               if before[k]['git_sha'] != after[k]['git_sha'] or
                               (before[k].get('executable') is not None and after[k].get('executable') is not None
                                and before[k]['executable'] != after[k]['executable']))
    return result


@lru_cache(maxsize=32)
def github_json(route):
    # Share one repository snapshot across skills during this command, reducing API quota use.
    request = Request('https://api.github.com/repos/' + route,
                      headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'codex-maintenance'})
    try:
        with urlopen(request, timeout=25) as response:
            raw = response.read(20_000_001)
        if len(raw) > 20_000_000:
            raise MaintenanceError('GitHub response exceeds size limit')
        return json.loads(raw)
    except MaintenanceError:
        raise
    except HTTPError as error:
        limited = error.code == 429 or (error.code == 403 and
                  (error.headers.get('X-RateLimit-Remaining') == '0' or error.headers.get('Retry-After')))
        code = 'github-rate-limited' if limited else ('upstream-not-found' if error.code == 404 else 'github-http-error')
        raise MaintenanceError('GitHub request failed (HTTP ' + str(error.code) + ')', code) from None
    except (TimeoutError, subprocess.TimeoutExpired):
        raise MaintenanceError('GitHub request timed out', 'network-timeout') from None
    except URLError as error:
        code = 'network-timeout' if isinstance(error.reason, TimeoutError) else 'network-error'
        raise MaintenanceError('GitHub connection failed', code) from None
    except Exception as error:
        raise MaintenanceError('GitHub response could not be read: ' + type(error).__name__, 'github-response-error') from None


def validate_source(repo, subdir, commit):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo or ''):
        raise MaintenanceError('Expected a GitHub owner/repository')
    parts = PurePosixPath(subdir or '')
    if not subdir or parts.is_absolute() or '..' in parts.parts or str(parts) != subdir:
        raise MaintenanceError('Expected a repository-relative skill directory')
    if not re.fullmatch(r'[0-9a-f]{40}', commit or ''):
        raise MaintenanceError('A fixed 40-character commit SHA is required')


def github_tree(repo, subdir, commit):
    validate_source(repo, subdir, commit)
    data = github_json(f'{repo}/git/trees/{commit}?recursive=1')
    if data.get('truncated') or not isinstance(data.get('tree'), list):
        raise MaintenanceError('Incomplete GitHub tree; update status unknown', 'upstream-tree-incomplete')
    prefix = subdir + '/'
    result = {}
    for item in data['tree']:
        if not item['path'].startswith(prefix) or item['type'] == 'tree':
            continue
        if item['type'] != 'blob' or item.get('mode') not in ('100644', '100755'):
            raise MaintenanceError('Upstream symlinks/submodules require manual inspection', 'upstream-special-entry')
        result[item['path'][len(prefix):]] = {'git_sha': item['sha'], 'executable': item['mode'] == '100755'}
    if 'SKILL.md' not in result:
        raise MaintenanceError('No SKILL.md at the confirmed upstream path', 'upstream-path-missing')
    return result


def resolve_ref(repo, ref):
    sha = github_json(f'{repo}/commits/{quote(ref, safe="")}').get('sha', '')
    if not re.fullmatch(r'[0-9a-f]{40}', sha):
        raise MaintenanceError('Unable to resolve upstream ref')
    return sha


def user_target(home, path):
    home, path = absolute(home), absolute(path)
    base = home / 'skills'
    if linked_path(path) or path.parent != base or path.name.startswith('.'):
        raise MaintenanceError('Only real, direct user skill directories are supported; managed paths and links are excluded')
    return path


@contextmanager
def state_lock(home):
    state = absolute(home) / 'maintenance'
    if linked_path(state):
        raise MaintenanceError('Maintenance state must not contain links or reparse points')
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = state / '.lock'
    fd = open_lock(lock)
    try:
        try:
            if os.name == 'nt':
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise MaintenanceError('Another maintenance operation holds the lock') from None
            raise
        yield state
    finally:
        # Closing releases the OS lock, including after process termination.
        os.close(fd)


def open_lock(path):
    if is_link(path):
        raise MaintenanceError('Lock must be a regular file, not a link or reparse point')
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        import msvcrt
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        create = kernel.CreateFileW
        create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                           wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        create.restype = wintypes.HANDLE
        close = kernel.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close.restype = wintypes.BOOL
        # Open the reparse point itself; deny delete-sharing so the lock cannot be replaced while held.
        handle = create(str(path), 0xC0000000, 0x3, None, 4, 0x00200080, None)
        if handle == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY | os.O_NOINHERIT)
        except BaseException:
            close(handle)
            raise
    else:
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info, opened = path.lstat(), os.fstat(fd)
        if is_link(path, info) or not stat.S_ISREG(opened.st_mode) or not os.path.samestat(info, opened):
            raise MaintenanceError('Lock file changed or is not a regular file')
        return fd
    except BaseException:
        os.close(fd)
        raise


def registry(home):
    path = absolute(home) / 'maintenance' / 'registry.json'
    data = json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {'schema': 1, 'entries': {}}
    if not isinstance(data, dict) or data.get('schema') != 1 or not isinstance(data.get('entries'), dict):
        raise MaintenanceError('Invalid or unsupported registry structure', 'invalid-registry')
    return data


def register(home, path, repo=None, subdir=None, commit=None, ref='main', note=''):
    path = user_target(home, path)
    source = None
    upstream = {}
    if repo:
        upstream = github_tree(repo, subdir, commit)
        source = {'repo': repo, 'subdir': subdir, 'commit': commit, 'ref': ref}
    metadata = skill_metadata(path)
    snap = snapshot(path)
    local = git_files(snap)
    entry = {'id': instance_id(path), 'path': str(path), 'name': metadata['name'], 'registered_at': now(),
             'source': source, 'source_status': 'confirmed' if source else 'local-or-unknown', 'note': note,
             'baseline_local': local, 'baseline_fingerprint': snap['fingerprint'], 'upstream_files': upstream,
             'customization': diff_git_maps(upstream, local) if source else None,
             'local_executable_bits_comparable': os.name != 'nt',
             'implicit_invocation': metadata['implicit_invocation']}
    with state_lock(home) as state:
        data = registry(home)
        previous = data['entries'].get(entry['id'])
        if previous:
            # Re-registration is deliberate; retain the former evidence without recursive nesting.
            history = state / 'registry-history' / (uuid.uuid4().hex + '.json')
            write_json(history, previous)
        data['entries'][entry['id']] = entry
        write_json(state / 'registry.json', data)
    return entry


def check_updates(home):
    # Reconcile against discovery without silently registering or changing any baseline.
    discovered = inventory(home, owners=('user',), include_config=False)
    installed = {item['id']: item for item in discovered['skills']}
    rows, errors = [], list(discovered['errors'])
    registry_readable = True
    try:
        entries = registry(home)['entries']
    except (MaintenanceError, OSError, ValueError) as error:
        registry_readable = False
        entries = {}
        errors.append({'path': str(absolute(home) / 'maintenance' / 'registry.json'), **error_info(error)})
    for key, entry in entries.items():
        row = {'id': key}
        try:
            if not isinstance(entry, dict) or not isinstance(entry.get('path'), str):
                raise MaintenanceError('Invalid registry entry', 'invalid-registry-entry')
            row.update(path=entry['path'], name=entry.get('name'))
            path = user_target(home, entry['path'])
            if key != instance_id(path):
                raise MaintenanceError('Registry path and instance ID differ', 'invalid-registry-entry')
            snap = snapshot(path)
            local = git_files(snap)
            row['local_since_registration'] = diff_git_maps(entry['baseline_local'], local)
            row['local_executable_bits_comparable'] = os.name != 'nt'
            row['full_local_fingerprint_changed'] = snap['fingerprint'] != entry['baseline_fingerprint']
            source = entry['source']
            if source:
                validate_source(source['repo'], source['subdir'], source['commit'])
                sha = resolve_ref(source['repo'], source['ref'])
                current = github_tree(source['repo'], source['subdir'], sha)
                difference = diff_maps(entry['upstream_files'], current)
                row.update(status='checked', upstream_commit=sha, upstream_diff=difference,
                           upstream_changed=any(difference.values()), local_vs_upstream=diff_git_maps(current, local))
            else:
                row['status'] = 'local-or-unknown-source'
        except (MaintenanceError, OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            row.update(status='unknown', **error_info(error))
        # Bad metadata cannot be mistaken for a fully checked installation.
        if key in installed and installed[key].get('error'):
            row.update(status='unknown', code=installed[key]['code'], error=installed[key]['error'])
        rows.append(row)
    for key, item in installed.items():
        if key not in entries:
            row = {'id': key, 'path': item['path'], 'name': item.get('name'),
                   'status': 'unregistered' if registry_readable else 'registry-unavailable'}
            if item.get('error'):
                row.update(status='unknown', code=item['code'], error=item['error'])
            rows.append(row)
    statuses = ('checked', 'unregistered', 'local-or-unknown-source', 'unknown', 'registry-unavailable')
    summary = {status: sum(row['status'] == status for row in rows) for status in statuses}
    summary.update(installed_user_skills=len(installed), registered_entries=len(entries),
                   updates_available=sum(row.get('upstream_changed', False) for row in rows),
                   discovery_complete=discovered['complete'], registry_readable=registry_readable)
    return {'schema': 2, 'checked_at': now(), 'entries': rows, 'summary': summary,
            'errors': errors, 'aliases': discovered['aliases'],
            'scope': 'direct user skills in CODEX_HOME/skills; linked, system and plugin skills excluded',
            'writes_performed': False}


def no_links(tree):
    if any(v['type'] in ('symlink', 'reparse-point') for v in tree['files'].values()):
        raise MaintenanceError('Directory contains links or reparse points; inspect and maintain this package manually')


def make_plan(home, target, candidate):
    target = user_target(home, target)
    candidate = absolute(candidate)
    if linked_path(candidate) or candidate == target or target in candidate.parents or candidate in target.parents:
        raise MaintenanceError('Candidate must be a separate real directory')
    before_meta, after_meta = skill_metadata(target), skill_metadata(candidate)
    if before_meta['name'] != after_meta['name']:
        raise MaintenanceError('Candidate skill name differs from installed skill')
    delta = compare(target, candidate)
    no_links(delta['before'])
    no_links(delta['after'])
    if delta['before']['fingerprint'] == delta['after']['fingerprint']:
        raise MaintenanceError('Candidate is identical; no update needed')
    plan = {'schema': 1, 'id': uuid.uuid4().hex, 'created_at': now(), 'operation': 'replace-user-skill',
            'codex_home': str(absolute(home)), 'target': str(target), 'candidate': str(candidate),
            'before_metadata': before_meta, 'after_metadata': after_meta, 'diff': delta,
            'review': 'Review candidate content and preserve custom behavior before applying within user authorization'}
    plan['plan_hash'] = digest(plan)
    return plan


def require_snapshot(path, expected, identity=True):
    current = snapshot(path)
    if current['fingerprint'] != expected['fingerprint'] or (identity and current['identity'] != expected['identity']):
        raise MaintenanceError('Directory changed since the plan; prepare a fresh plan')
    no_links(current)
    return current


def apply_plan(home, plan):
    unsigned = {k: v for k, v in plan.items() if k != 'plan_hash'}
    if digest(unsigned) != plan.get('plan_hash') or plan.get('codex_home') != str(absolute(home)):
        raise MaintenanceError('Plan integrity or CODEX_HOME mismatch')
    target = user_target(home, plan['target'])
    candidate = absolute(plan['candidate'])
    if linked_path(candidate):
        raise MaintenanceError('Candidate path now contains a symlink')
    require_snapshot(target, plan['diff']['before'])
    require_snapshot(candidate, plan['diff']['after'])
    with state_lock(home) as state:
        operation_id = plan['id']
        if not re.fullmatch(r'[0-9a-f]{32}', operation_id):
            raise MaintenanceError('Invalid operation ID')
        folder = state / 'backups' / operation_id
        if linked_path(folder.parent):
            raise MaintenanceError('Backup parent must not be a symlink')
        folder.mkdir(parents=True, mode=0o700, exist_ok=False)
        staged, backup = folder / 'staged', folder / 'original'
        shutil.copytree(candidate, staged, symlinks=True)
        require_snapshot(staged, plan['diff']['after'], identity=False)
        require_snapshot(candidate, plan['diff']['after'])
        require_snapshot(target, plan['diff']['before'])
        receipt = {'schema': 1, 'id': operation_id, 'target': str(target), 'backup': str(backup), 'status': 'prepared',
                   'created_at': now(), 'plan': plan}
        receipt_file = folder / 'receipt.json'
        write_json(receipt_file, receipt)
        try:
            os.replace(target, backup)
            os.replace(staged, target)
            require_snapshot(target, plan['diff']['after'], identity=False)
        except BaseException:
            if not target.exists() and backup.exists():
                os.replace(backup, target)
            receipt['status'] = 'interrupted-needs-inspection'
            write_json(receipt_file, receipt)
            raise
        receipt['status'] = 'applied'
        receipt['after'] = snapshot(target)
        write_json(receipt_file, receipt)
        return receipt


def rollback(home, operation_id):
    if not re.fullmatch(r'[0-9a-f]{32}', operation_id):
        raise MaintenanceError('Invalid operation ID')
    with state_lock(home) as state:
        folder = state / 'backups' / operation_id
        if linked_path(folder):
            raise MaintenanceError('Backup must not be a symlink')
        receipt = json.loads((folder / 'receipt.json').read_text(encoding='utf-8-sig'))
        target = user_target(home, receipt['target'])
        backup, displaced = folder / 'original', folder / 'displaced'
        require_snapshot(backup, receipt['plan']['diff']['before'])
        if displaced.exists() or displaced.is_symlink():
            raise MaintenanceError('Rollback already started; inspect receipt and retained directories')
        if target.exists() or target.is_symlink():
            after = receipt.get('after', receipt['plan']['diff']['after'])
            require_snapshot(target, after, identity='after' in receipt)
            os.replace(target, displaced)
        try:
            os.replace(backup, target)
        except BaseException:
            if not target.exists() and displaced.exists():
                os.replace(displaced, target)
            raise
        receipt.update(status='rolled-back', rolled_back_at=now(), retained_replacement=str(displaced))
        write_json(folder / 'receipt.json', receipt)
        return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, default=Path(os.environ.get('CODEX_HOME', Path.home() / '.codex')))
    parser.add_argument('--output', type=Path, help='Write JSON here; otherwise print. Do not place output inside a compared skill.')
    sub = parser.add_subparsers(dest='command', required=True)
    inv = sub.add_parser('inventory', help='Read-only metadata inventory; effective runtime state needs native queries')
    inv.add_argument('--deep', action='store_true', help='Also hash full skill contents for duplicate/content comparisons')
    sub.add_parser('check-updates', help='Read GitHub metadata for registered sources; never install or change registry')
    reg = sub.add_parser('register', help='Persist verified source or local-only baseline for one user skill')
    reg.add_argument('path', type=Path)
    reg.add_argument('--repo')
    reg.add_argument('--subdir')
    reg.add_argument('--commit')
    reg.add_argument('--ref', default='main')
    reg.add_argument('--note', default='')
    for command in ('compare', 'plan'):
        cmd = sub.add_parser(command, help='Compare whole directories' if command == 'compare' else 'Preview a user skill replacement; no installation writes')
        cmd.add_argument('target', type=Path)
        cmd.add_argument('candidate', type=Path)
    apply = sub.add_parser('apply', help='Apply one reviewed plan within existing user authorization, retaining backup')
    apply.add_argument('plan_file', type=Path)
    restore = sub.add_parser('rollback', help='Restore backup only if the installed replacement has no later changes')
    restore.add_argument('operation_id')
    validate = sub.add_parser('validate', help='Parse skill frontmatter and invocation policy with an existing YAML parser')
    validate.add_argument('path', type=Path)
    args = parser.parse_args()
    try:
        if args.command == 'inventory':
            result = inventory(args.home, deep=args.deep)
        elif args.command == 'register':
            if not args.repo and (args.subdir or args.commit):
                raise MaintenanceError('--subdir/--commit require --repo')
            result = register(args.home, args.path, args.repo, args.subdir, args.commit, args.ref, args.note)
        elif args.command == 'check-updates':
            result = check_updates(args.home)
        elif args.command == 'compare':
            result = compare(args.target, args.candidate)
        elif args.command == 'plan':
            result = make_plan(args.home, args.target, args.candidate)
        elif args.command == 'apply':
            result = apply_plan(args.home, json.loads(args.plan_file.read_text(encoding='utf-8-sig')))
        elif args.command == 'rollback':
            result = rollback(args.home, args.operation_id)
        else:
            result = skill_metadata(args.path)
        if args.output:
            write_json(args.output, result)
        else:
            # ASCII JSON preserves Unicode values across legacy Windows pipes/code pages.
            print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0
    except Exception as error:
        # Parser/network error payloads can contain credentials. Keep diagnostics deliberately narrow.
        print(json.dumps(error_info(error)), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
