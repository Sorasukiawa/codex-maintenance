#!/usr/bin/env python3
"""Codex component inventory and staged user-skill maintenance. Python 3.11+."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
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
from urllib.request import Request, urlopen
import uuid


class MaintenanceError(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def absolute(path):
    return Path(os.path.abspath(Path(path).expanduser()))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.maintenance-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
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
    if not stat.S_ISDIR(root.st_mode):
        raise MaintenanceError('Snapshot root must be a real directory')
    files = {}
    def walk(folder):
        for item in sorted(folder.iterdir()):
            info = item.lstat()
            row = {'mode': stat.S_IMODE(info.st_mode)}
            if stat.S_ISLNK(info.st_mode):
                row.update(type='symlink', target=os.readlink(item))
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


def parse_yaml(text):
    try:
        import yaml
    except ImportError:
        ruby = shutil.which('ruby')
        if not ruby:
            raise MaintenanceError('YAML parser unavailable: use an existing PyYAML or Ruby Psych runtime')
        code = 'v=YAML.safe_load(STDIN.read, permitted_classes: [], aliases: false); STDOUT.write(JSON.generate(v))'
        result = subprocess.run([ruby, '-rjson', '-ryaml', '-e', code], input=text, text=True,
                                capture_output=True, timeout=20)
        if result.returncode:
            raise MaintenanceError('Invalid YAML (parser details omitted to avoid exposing values)')
        return json.loads(result.stdout)
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        raise MaintenanceError('Invalid YAML') from None


def skill_metadata(path):
    text = (path / 'SKILL.md').read_text()
    match = re.match(r'\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)', text, re.S)
    if not match:
        raise MaintenanceError('Missing skill YAML frontmatter')
    data = parse_yaml(match.group(1))
    if not isinstance(data, dict) or not isinstance(data.get('name'), str) or not data['name'].strip():
        raise MaintenanceError('Invalid skill name')
    if not isinstance(data.get('description'), str) or not data['description'].strip():
        raise MaintenanceError('Missing skill description')
    ui_path = path / 'agents' / 'openai.yaml'
    implicit = 'default'
    if ui_path.exists():
        ui = parse_yaml(ui_path.read_text())
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


def instance_id(path):
    return digest(str(absolute(path)))[:20]


def mcp_rows(mapping, source, errors=None):
    rows = []
    errors = errors if errors is not None else []
    if not isinstance(mapping, dict):
        errors.append({'source': source, 'error': 'MCP collection must be a mapping; values omitted'})
        return rows
    for name, value in mapping.items():
        if not isinstance(value, dict):
            errors.append({'source': source, 'name': name, 'error': 'MCP entry must be a mapping; values omitted'})
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
            errors.append({'source': source, 'name': name, 'error': 'Invalid MCP field types; values omitted'})
            continue
        command = value.get('command')
        command_exists = None
        if isinstance(command, str) and command.startswith('/'):
            command_exists = Path(command).is_file()
        rows.append({'name': name, 'source': source, 'enabled': value.get('enabled', 'unspecified'),
                     'transport': 'url' if value.get('url') else 'stdio', 'absolute_command_exists': command_exists,
                     'argument_count': len(value.get('args', [])),
                     'env_keys': sorted(set(value.get('env', {})) | set(value.get('env_vars', []))),
                     'header_keys': sorted(set(value.get('headers', {})) | set(value.get('http_headers', {})) |
                                           set(value.get('env_http_headers', {}))),
                     'connection_tested': False})
    return rows


def inventory(home):
    home = absolute(home)
    report = {'schema': 1, 'created_at': now(), 'codex_home': str(home), 'skills': [], 'plugins': [],
              'mcp': [], 'aliases': [], 'errors': [], 'scope': 'filesystem and user config; not effective runtime state'}
    config = {}
    cfg = home / 'config.toml'
    if cfg.exists():
        try:
            config = tomllib.loads(cfg.read_text())
        except (OSError, ValueError):
            report['errors'].append({'path': str(cfg), 'error': 'config parse failed; values omitted'})
    report['mcp'] += mcp_rows(config.get('mcp_servers', {}), 'user-config', report['errors'])
    seen = set()
    def skill(path, owner, plugin_id=None):
        if path.is_symlink():
            report['aliases'].append({'path': str(path), 'target': str(path.resolve()), 'owner': owner})
            return
        if not (path / 'SKILL.md').is_file():
            return
        real = str(path.resolve())
        if real in seen:
            return
        seen.add(real)
        row = {'id': instance_id(path), 'path': str(path), 'realpath': real, 'owner': owner, 'plugin_id': plugin_id}
        try:
            row.update(skill_metadata(path))
            tree = snapshot(path)
            row.update(fingerprint=tree['fingerprint'], file_count=sum(v['type'] == 'file' for v in tree['files'].values()))
        except (MaintenanceError, OSError, ValueError, subprocess.TimeoutExpired) as error:
            message = str(error) if isinstance(error, MaintenanceError) else type(error).__name__
            row.update(error=message)
            report['errors'].append({'path': str(path), 'error': message})
        report['skills'].append(row)
    for base, owner in [(home / 'skills', 'user'), (home / 'skills' / '.system', 'system')]:
        if base.is_dir():
            for path in sorted(base.iterdir()):
                if not path.name.startswith('.'):
                    skill(path, owner)
    cache = home / 'plugins' / 'cache'
    if cache.is_dir():
        for manifest in sorted(cache.glob('*/*/*/.codex-plugin/plugin.json')):
            root = manifest.parent.parent
            if any(p.is_symlink() for p in [root, root.parent, root.parent.parent]):
                report['aliases'].append({'path': str(root), 'target': str(root.resolve()), 'owner': 'plugin'})
                continue
            plugin_id = root.parent.name + '@' + root.parent.parent.name
            try:
                data = json.loads(manifest.read_text())
                report['plugins'].append({'id': plugin_id, 'path': str(root), 'version': data.get('version', root.name),
                                          'enabled_in_user_config': config.get('plugins', {}).get(plugin_id, {}).get('enabled', 'unspecified'),
                                          'managed': True, 'effective_state': 'requires-native-query'})
                for path in sorted(root.rglob('SKILL.md')):
                    relative = path.relative_to(root)
                    if any(part.startswith('.') or part in ('node_modules', '__pycache__') for part in relative.parts):
                        continue
                    skill(path.parent, 'plugin', plugin_id)
                mcp = root / '.mcp.json'
                if mcp.is_file():
                    report['mcp'] += mcp_rows(json.loads(mcp.read_text()).get('mcpServers', {}), plugin_id, report['errors'])
            except (OSError, ValueError, AttributeError):
                report['errors'].append({'path': str(manifest), 'error': 'plugin parse failed; values omitted'})
    report['counts'] = {owner: sum(s['owner'] == owner for s in report['skills']) for owner in ('user', 'system', 'plugin')}
    return report


def git_files(tree):
    # Generated Python caches are excluded from upstream comparison only, never backups/drift checks.
    return {p: {'git_sha': v['git_sha'], 'executable': bool(v['mode'] & 0o111)}
            for p, v in tree['files'].items() if v['type'] == 'file' and not p.endswith('.pyc')
            and '__pycache__' not in Path(p).parts}


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
    except Exception as error:
        raise MaintenanceError('GitHub read failed: ' + type(error).__name__) from None


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
        raise MaintenanceError('Incomplete GitHub tree; update status unknown')
    prefix = subdir + '/'
    result = {}
    for item in data['tree']:
        if not item['path'].startswith(prefix) or item['type'] == 'tree':
            continue
        if item['type'] != 'blob' or item.get('mode') not in ('100644', '100755'):
            raise MaintenanceError('Upstream symlinks/submodules require manual inspection')
        result[item['path'][len(prefix):]] = {'git_sha': item['sha'], 'executable': item['mode'] == '100755'}
    if 'SKILL.md' not in result:
        raise MaintenanceError('No SKILL.md at the confirmed upstream path')
    return result


def resolve_ref(repo, ref):
    sha = github_json(f'{repo}/commits/{quote(ref, safe="")}').get('sha', '')
    if not re.fullmatch(r'[0-9a-f]{40}', sha):
        raise MaintenanceError('Unable to resolve upstream ref')
    return sha


def user_target(home, path):
    home, path = absolute(home), absolute(path)
    base = home / 'skills'
    if home.resolve() != home or base.resolve() != base or path.parent != base or path.name.startswith('.') or path.is_symlink():
        raise MaintenanceError('Only real, direct user skill directories are supported; managed paths and links are excluded')
    return path


@contextmanager
def state_lock(home):
    state = absolute(home) / 'maintenance'
    if state.resolve() != state:
        raise MaintenanceError('Maintenance state must not be a symlink')
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = state / '.lock'
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield state
    except BlockingIOError:
        raise MaintenanceError('Another maintenance operation holds the lock') from None
    finally:
        os.close(fd)


def registry(home):
    path = absolute(home) / 'maintenance' / 'registry.json'
    return json.loads(path.read_text()) if path.exists() else {'schema': 1, 'entries': {}}


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
             'customization': diff_maps(upstream, local) if source else None,
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
    rows = []
    for entry in registry(home)['entries'].values():
        row = {'id': entry['id'], 'path': entry['path'], 'name': entry['name']}
        try:
            snap = snapshot(entry['path'])
            local = git_files(snap)
            row['local_since_registration'] = diff_maps(entry['baseline_local'], local)
            row['full_local_fingerprint_changed'] = snap['fingerprint'] != entry['baseline_fingerprint']
            source = entry['source']
            if source:
                sha = resolve_ref(source['repo'], source['ref'])
                current = github_tree(source['repo'], source['subdir'], sha)
                difference = diff_maps(entry['upstream_files'], current)
                row.update(status='checked', upstream_commit=sha, upstream_diff=difference,
                           upstream_changed=any(difference.values()), local_vs_upstream=diff_maps(current, local))
            else:
                row['status'] = 'local-or-unknown-source'
        except (MaintenanceError, OSError, ValueError) as error:
            row.update(status='unknown', error=type(error).__name__)
        rows.append(row)
    return {'schema': 1, 'checked_at': now(), 'entries': rows, 'writes_performed': False}


def no_links(tree):
    if any(v['type'] == 'symlink' for v in tree['files'].values()):
        raise MaintenanceError('Directory contains symlinks; inspect and maintain this package manually')


def make_plan(home, target, candidate):
    target = user_target(home, target)
    candidate = absolute(candidate)
    if candidate.resolve() != candidate or candidate == target or target in candidate.parents or candidate in target.parents:
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
    if candidate.resolve() != candidate:
        raise MaintenanceError('Candidate path now contains a symlink')
    require_snapshot(target, plan['diff']['before'])
    require_snapshot(candidate, plan['diff']['after'])
    with state_lock(home) as state:
        operation_id = plan['id']
        if not re.fullmatch(r'[0-9a-f]{32}', operation_id):
            raise MaintenanceError('Invalid operation ID')
        folder = state / 'backups' / operation_id
        if folder.parent.resolve() != folder.parent:
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
        if folder.resolve() != folder:
            raise MaintenanceError('Backup must not be a symlink')
        receipt = json.loads((folder / 'receipt.json').read_text())
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
    sub.add_parser('inventory', help='Read-only filesystem inventory; effective runtime state needs native queries')
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
            result = inventory(args.home)
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
            result = apply_plan(args.home, json.loads(args.plan_file.read_text()))
        elif args.command == 'rollback':
            result = rollback(args.home, args.operation_id)
        else:
            result = skill_metadata(args.path)
        if args.output:
            write_json(args.output, result)
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as error:
        # Parser/network error payloads can contain credentials. Keep diagnostics deliberately narrow.
        print(json.dumps({'error': str(error) if isinstance(error, MaintenanceError) else type(error).__name__}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
