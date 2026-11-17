"""Codex-primary hook inventory and conservative native-host projections.

Plugin code stays in its native cache. Generated commands name immutable specs and
pin hook/support bytes; changed source requires a newly registered native command.
No native trust/approval file is modified here.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

import shared_resources as sr

SCHEMA = 1
MAX_FILES = 5000
MAX_BYTES = 32 * 1024 * 1024
CLAUDE_EVENTS = {'SessionStart', 'SessionEnd', 'UserPromptSubmit', 'SubagentStart',
                 'SubagentStop', 'Stop', 'PreToolUse', 'PostToolUse', 'PreCompact',
                 'Notification', 'PermissionRequest', 'PostToolUseFailure'}
STATE = 'state/hook-sync.json'


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def _read(path):
    path = Path(path)
    if not path.exists():
        return {}
    text = path.read_text(encoding='utf-8-sig')
    if not text.strip():
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise sr.HubError(f'Expected a JSON object in {path}')
    return data


def _read_source_json(path):
    path = Path(path)
    if not path.is_file():
        raise sr.HubError(f'missing hook source file: {path}')
    text = path.read_text(encoding='utf-8-sig')
    if not text.strip():
        raise sr.HubError(f'empty hook source file: {path}')
    data = json.loads(text)
    if not isinstance(data, dict):
        raise sr.HubError(f'Expected a JSON object in {path}')
    return data


SUPPORT_FOLDERS = ('hooks', 'scripts', 'lib', 'src', 'bin', '.codex-plugin', '.claude-plugin')


def _source_files(root, manifest=None):
    """Bounded executable support inventory, excluding skills and credential names."""
    root = Path(root)
    if root.is_symlink():
        raise sr.HubError('linked plugin root')
    paths = {Path(manifest)} if manifest else set()
    try:
        for entry in root.iterdir():
            if entry.is_symlink():
                raise sr.HubError('linked hook support file in plugin root')
            if entry.is_file():
                if entry.name.lower() in sr.SECRET_NAMES or entry.suffix.lower() in sr.SECRET_SUFFIXES:
                    continue
                paths.add(entry)
    except OSError as error:
        raise sr.HubError(f'unreadable plugin root: {error}')

    for folder in SUPPORT_FOLDERS:
        base = Path(root) / folder
        if base.is_symlink():
            raise sr.HubError('linked hook support directory')
        if not base.is_dir():
            continue
        for current, dirs, files in os.walk(base, followlinks=False):
            if any((Path(current) / d).is_symlink() for d in dirs):
                raise sr.HubError('linked hook support directory')
            dirs[:] = sorted(d for d in dirs if d not in sr.PRUNED_DIRS)
            for name in sorted(files):
                path = Path(current) / name
                if path.name.lower() in sr.SECRET_NAMES or path.suffix.lower() in sr.SECRET_SUFFIXES:
                    continue
                paths.add(path)
                if len(paths) > MAX_FILES:
                    raise sr.HubError('hook support inventory file budget exceeded')
    result, total = [], 0
    for path in sorted(paths):
        if path.is_symlink() or not path.is_file():
            raise sr.HubError('linked or missing hook support file')
        if path.stat().st_size > sr.MAX_FILE:
            raise sr.HubError('hook support file size budget exceeded')
        total += path.stat().st_size
        if total > MAX_BYTES:
            raise sr.HubError('hook support inventory byte budget exceeded')
        result.append([str(path.resolve()), sr.file_sha(path)])
    return result


def _groups(data, origin, root, source, files, output):
    hooks = data.get('hooks', data)
    if not isinstance(hooks, dict):
        raise sr.HubError('hooks must be an object')
    if not hooks:
        raise sr.HubError('empty hook declaration')
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            raise sr.HubError('hook event must contain a list')
        for gi, group in enumerate(groups):
            if not isinstance(group, dict):
                raise sr.HubError('hook group must be an object')
            handlers = group.get('hooks', [group] if 'type' in group else [])
            if not isinstance(handlers, list):
                raise sr.HubError('hook handlers must contain a list')
            for hi, handler in enumerate(handlers):
                if not isinstance(handler, dict):
                    raise sr.HubError('hook handler must be an object')
                item = {'id': f'{origin}:{event}:{gi}:{hi}', 'origin': origin,
                        'event': event, 'matcher': group.get('matcher', ''),
                        'handler': copy.deepcopy(handler), 'source': str(source),
                        'root': str(root) if root else None, 'files': files,
                        'enabled': group.get('enabled', True) is not False and
                                   handler.get('enabled', True) is not False and
                                   not group.get('disabled', False) and not handler.get('disabled', False)}
                item['source_hash'] = _hash(item)
                output.append(item)


def _has_command(value):
    if isinstance(value, dict):
        return value.get('type') == 'command' or any(_has_command(v) for v in value.values())
    return isinstance(value, list) and any(_has_command(v) for v in value)


def discover(home=None):
    home = Path(home) if home is not None else Path.home()
    result = {'hooks': [], 'plugins': [], 'errors': [], 'unsupported': []}
    user = home / '.codex/hooks.json'
    if user.is_file():
        try:
            data = _read_source_json(user)
            if not data or not data.get('hooks', data):
                raise sr.HubError('empty user hooks file')
            _groups(data, 'codex:user', None, user,
                    [[str(user.resolve()), sr.file_sha(user)]], result['hooks'])
        except (OSError, ValueError, sr.HubError) as error:
            result['errors'].append(f'Codex user hooks: {type(error).__name__}')
    try:
        roots, problems = sr.codex_plugin_roots(home)
    except AttributeError:
        roots, problems = [], [{'reason': 'codex_plugin_roots resolver unavailable'}]
    result['unsupported'].extend(problems)
    for problem in problems:
        if any(word in problem.get('reason', '') for word in ('unreadable', 'invalid', 'must be')):
            result['errors'].append('Codex plugin discovery: ' + problem.get('reason', 'invalid source'))
    for origin, root in roots:
        root = Path(root)
        manifest = root / '.codex-plugin/plugin.json'
        if not manifest.is_file():
            manifest = root / 'plugin.json'
        plugin = {'origin': origin, 'root': str(root), 'hooks': 0}
        result['plugins'].append(plugin)
        try:
            if not manifest.is_file():
                result['unsupported'].append({
                    'origin': origin.removeprefix('plugin:codex:'),
                    'reason': 'enabled plugin manifest unavailable; legacy skill source only'})
                continue
            data = _read_source_json(manifest)
            if not data:
                raise sr.HubError('empty plugin manifest')
            declaration = data.get('hooks')
            if declaration is None:
                continue
            source = manifest
            if isinstance(declaration, str):
                candidate = root / declaration
                for parent in (candidate, *candidate.parents):
                    if parent == root.parent:
                        break
                    if parent.is_symlink():
                        raise sr.HubError('linked hook manifest path')
                source = candidate.resolve()
                source.relative_to(root.resolve())
                if source.is_symlink():
                    raise sr.HubError('linked hook manifest')
                if not source.is_file():
                    raise sr.HubError(f'missing hook source file: {source}')
                declaration = _read_source_json(source)
            if not isinstance(declaration, dict):
                raise sr.HubError('unsupported hook manifest declaration')
            if not declaration or (isinstance(declaration, dict) and not declaration.get('hooks', declaration)):
                raise sr.HubError('empty hook declaration')
            files = (_source_files(root, manifest) if _has_command(declaration)
                     else [[str(manifest.resolve()), sr.file_sha(manifest)]])
            if source != manifest and str(source.resolve()) not in {p for p, _ in files}:
                files.append([str(source.resolve()), sr.file_sha(source)])
            before = len(result['hooks'])
            _groups(declaration, origin, root, source, files, result['hooks'])
            plugin['hooks'] = len(result['hooks']) - before
            plugin['source_hash'] = _hash(files)
        except (OSError, ValueError, sr.HubError) as error:
            result['errors'].append(f'{origin}: {type(error).__name__}')
    config = home / '.codex/config.toml'
    if config.is_file():
        try:
            import tomllib
            native = tomllib.loads(config.read_text(encoding='utf-8-sig'))
            states = native.get('hooks', {}).get('state', {})
            if not isinstance(states, dict):
                raise sr.HubError('Codex hook states must be an object')
            for hook in result['hooks']:
                event = re.sub(r'(?<!^)(?=[A-Z])', '_', hook['event']).lower()
                gi, hi = hook['id'].rsplit(':', 2)[-2:]
                prefix = (str(user) if hook['origin'] == 'codex:user'
                          else hook['origin'].removeprefix('plugin:codex:'))
                for key, state in states.items():
                    if key.startswith(prefix + ':') and key.endswith(f':{event}:{gi}:{hi}') and \
                            isinstance(state, dict) and state.get('enabled') is False:
                        hook['enabled'] = False
                if native.get('features', {}).get('hooks') is False:
                    hook['enabled'] = False
                hook['source_hash'] = _hash({k: v for k, v in hook.items() if k != 'source_hash'})
        except (OSError, ValueError, TypeError, AttributeError, sr.HubError) as error:
            result['errors'].append(f'Codex hook state: {type(error).__name__}')
    return result


def fingerprint(home=None):
    data = discover(home)
    files = {p: sha for h in data['hooks'] for p, sha in h['files']}
    return {'sha256': _hash({'files': files, 'plugins': data['plugins'],
                            'hooks': [[h['id'], h['source_hash']] for h in data['hooks']],
                            'errors': data['errors'], 'unsupported': data['unsupported']}),
            'files': len(files), 'errors': data['errors']}


def fingerprint_sources(home=None):
    return [Path(p) for h in discover(home)['hooks'] for p, _ in h['files']]


source_signature = fingerprint


def _targets(home):
    if (home / '.claude').is_dir():
        yield 'claude', home / '.claude/settings.json'
    if (home / '.gemini').is_dir():
        yield 'antigravity', home / '.gemini/config/hooks.json'
    local = home / 'AppData/Local'
    if home.resolve() == Path.home().resolve():
        local = Path(os.environ.get('LOCALAPPDATA', local))
    for root in sorted((local / 'AgyMulti/profiles').glob('*/gemini')):
        if root.is_dir():
            yield 'antigravity:' + root.parent.name, root / 'config/hooks.json'
    if (home / '.hermes').is_dir():
        yield 'hermes', home / '.hermes/config.yaml'
    for path in sorted((home / '.hermes/profiles').glob('*/config.yaml')):
        yield 'hermes:' + path.parent.name, path


def _load_target(host, path):
    if host.startswith('hermes'):
        import yaml
        try:
            value = yaml.safe_load(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
        except yaml.YAMLError as error:
            raise sr.HubError('malformed native YAML') from error
        if value is None:
            value = {}
        if not isinstance(value, dict):
            raise sr.HubError('host configuration must be an object')
        return value
    return _read(path)


def _save_target(home, host, path, data):
    if path.exists():
        backup = home / '.agent-memory/backups/hook-sync' / _hash(str(path))[:16]
        backup_file = backup / (sr.file_sha(path) + path.suffix)
        sr.write_bytes(backup_file, path.read_bytes())
        if os.name == 'nt':
            sr._copy_dacl(path, backup_file)
        orig = backup / 'original-path.txt'
        sr.write_text(orig, str(path) + '\n')
        if os.name == 'nt':
            sr._copy_dacl(path, orig)
    if host.startswith('hermes'):
        import yaml
        sr.write_text(path, yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    else:
        sr.write_json(path, data)


def _supported(hook, host, native):
    handler = hook['handler']
    if not hook['enabled']:
        return 'source explicitly disabled'
    if handler.get('type', 'command') != 'command':
        return 'native-only handler type: ' + str(handler.get('type'))
    if not isinstance(handler.get('command'), str) or not handler['command'].strip():
        return 'command missing'
    if set(handler) - {'type', 'command', 'commandWindows', 'timeout', 'statusMessage', 'enabled', 'disabled'}:
        return 'handler options have no verified target translation'
    timeout = handler.get('timeout', 30)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= 300:
        return 'command timeout must be between zero and 300 seconds'
    if not isinstance(hook['matcher'], str):
        return 'matcher must be text'
    try:
        re.compile(hook['matcher'])
    except re.error:
        return 'invalid matcher expression'
    memory_adapter = str(Path(__file__).resolve().with_name('hook.py')).replace('\\', '/').casefold()
    policy_guard = str(Path(__file__).resolve().with_name('policy_guard.py')).replace('\\', '/').casefold()
    if policy_guard in handler['command'].replace('\\', '/').casefold():
        return 'policy protection remains host-owned; do not mirror approval gates across runtimes'
    if hook['origin'] == 'codex:user' and (memory_adapter in handler['command'].replace('\\', '/').casefold() or
                                        handler['command'].startswith('lean-ctx ')):
        return 'user adapters remain host-owned; canonical memory/lean-ctx adapters are not duplicated'
    if host.startswith('hermes'):
        return 'native Hermes lifecycle ignores returned context; context projection unsupported'
    if host == 'claude':
        key = hook['origin'].removeprefix('plugin:codex:')
        enabled = native.get('enabledPlugins', {})
        matches = [flag for name, flag in enabled.items() if name.partition('@')[0] == key.partition('@')[0]]
        if matches:
            return ('native plugin already enabled' if any(flag is True for flag in matches)
                    else 'target native plugin explicitly disabled')
        if hook['event'] not in CLAUDE_EVENTS:
            return 'event has no verified Claude command-hook contract'
    elif hook['event'] != 'SessionStart':
        return 'only SessionStart context projection has a verified cross-host contract'
    return None


def console_python(executable=None):
    """Resolve the canonical console Python interpreter.

    When running under a windowless launcher like pythonw.exe, native hook
    commands must still use the console interpreter (python.exe) because hook
    subprocesses require standard input/output streams. The sibling executable
    is verified on disk; no arbitrary search or fallback is permitted.
    """
    exe = Path(executable if executable is not None else sys.executable)
    name = exe.name.lower()
    if name.startswith('pythonw'):
        candidate_name = 'python.exe' if name.endswith('.exe') else 'python'
        candidate = exe.with_name(candidate_name)
        if candidate.is_file():
            return str(candidate)
        sub_name = re.sub(r'(?i)^pythonw', 'python', exe.name)
        sub_candidate = exe.with_name(sub_name)
        if sub_candidate.is_file():
            return str(sub_candidate)
        raise sr.HubError(f'canonical console Python launcher sibling not found for {exe}')
    if not exe.is_file():
        raise sr.HubError(f'console Python launcher not found: {exe}')
    return str(exe)


def _entry(host, hook, spec, digest):
    py_exe = console_python()
    self_script = Path(__file__).resolve()
    spec_path = Path(spec)
    if host == 'claude':
        command = shlex.join([Path(py_exe).as_posix(), self_script.as_posix(),
                             '--run', spec_path.as_posix(), '--sha256', digest])
    else:
        command = subprocess.list2cmdline([py_exe, str(self_script),
                                          '--run', str(spec_path), '--sha256', digest])
    handler = {'type': 'command', 'command': command,
               'timeout': hook['handler'].get('timeout', 30) + 5}
    if host == 'claude':
        entry = {'hooks': [handler]}
        if hook['matcher']:
            entry['matcher'] = hook['matcher']
        return hook['event'], entry
    if host.startswith('antigravity'):
        return 'agent-hub-' + _hash(hook['id'])[:20], {'SessionStart': [handler]}
    handler.pop('type')
    return 'on_session_start', handler


def _location(data, host, event):
    if host.startswith('antigravity'):
        return data
    hooks = data.setdefault('hooks', {})
    if not isinstance(hooks, dict):
        raise sr.HubError('host hooks must be an object')
    entries = hooks.setdefault(event, [])
    if not isinstance(entries, list):
        raise sr.HubError('host hook event must contain a list')
    return entries


def sync(home=None, hub=None, dry_run=False):
    home = Path(home) if home is not None else Path.home()
    hub = Path(hub) if hub is not None else (sr.DEFAULT_HUB if home.resolve() == Path.home().resolve() else home / '.agent-hub')
    discovered = discover(home)
    report = {'schema': SCHEMA, 'source_host': 'codex', 'ok': not discovered['errors'],
              'errors': list(discovered['errors']), 'unsupported': [], 'conflicts': [],
              'counts': {'create': 0, 'update': 0, 'remove': 0, 'unchanged': 0},
              'native_trust_modified': False, 'runtime_verified': False,
              'hooks': [{k: h[k] for k in ('id', 'event', 'origin', 'source', 'source_hash')}
                        for h in discovered['hooks']], 'targets': []}
    report['unsupported'].extend(discovered['unsupported'])
    ledger_path = hub / STATE
    try:
        ledger = _read(ledger_path) if ledger_path.exists() else {}
    except (OSError, ValueError, sr.HubError):
        report['errors'].append('hook ownership ledger unreadable; no targets written')
        report['ok'] = False
        return report
    old_targets = ledger.get('targets', {})
    if (ledger_path.exists() and ledger.get('schema') != SCHEMA) or \
            not isinstance(old_targets, dict) or any(
                not isinstance(records, dict) or any(
                    not isinstance(record, dict) or not isinstance(record.get('event'), str) or
                    not isinstance(record.get('entry'), dict) for record in records.values())
                for records in old_targets.values()):
        report['errors'].append('hook ownership ledger schema invalid; no targets written')
        report['ok'] = False
        return report
    if not (home / '.codex/config.toml').is_file() and any(
            identity.startswith('plugin:codex:') for records in old_targets.values() for identity in records):
        report['errors'].append('Codex plugin configuration missing; existing target hooks preserved')
        report['ok'] = False
        return report
    if not (home / '.codex/hooks.json').is_file() and any(
            identity.startswith('codex:user:') for records in old_targets.values() for identity in records):
        report['errors'].append('Codex user hooks file missing; existing target hooks preserved')
        report['ok'] = False
        return report
    new_targets = copy.deepcopy(old_targets)
    if discovered['errors']:
        return report  # Incomplete discovery cannot authorize deleting old registrations.
    for host, path in _targets(home):
        key = str(path)
        try:
            native = _load_target(host, path)
            if host == 'claude' and (not isinstance(native.get('enabledPlugins', {}), dict) or
                                     not isinstance(native.get('disableAllHooks', False), bool)):
                raise sr.HubError('malformed native plugin/hook disable settings')
            before = copy.deepcopy(native)
            previous = old_targets.get(key, {})
            current = {}
            desired = {}
            if host == 'claude' and native.get('disableAllHooks') is True:
                report['unsupported'].append({'host': host, 'reason': 'target disableAllHooks is true'})
                continue
            for hook in discovered['hooks']:
                reason = _supported(hook, host, native)
                if reason:
                    report['unsupported'].append({'host': host, 'id': hook['id'], 'reason': reason})
                    continue
                spec_data = {'schema': SCHEMA, 'host': host, 'hook': hook}
                if host != 'claude':
                    spec_data['target_state'] = str((hub / 'state/hook-sandbox' / host.replace(':', '_')).resolve())
                digest = _hash(spec_data)
                spec = hub / 'hooks/specs' / (digest + '.json')
                event, entry = _entry(host, hook, spec, digest)
                desired[hook['id']] = {'event': event, 'entry': entry}
                if not dry_run:
                    sr.write_json(spec, spec_data)
            for identity in sorted(set(previous) | set(desired)):
                old, wanted = previous.get(identity), desired.get(identity)
                if old and any(identity.startswith('plugin:codex:' + p.get('origin', '') + ':')
                               for p in discovered['unsupported']):
                    current[identity] = old
                    report['unsupported'].append({'host': host, 'id': identity,
                                                  'reason': 'enabled source unresolved; existing registration preserved'})
                    continue
                if old and old.get('disabled'):
                    current[identity] = old
                    report['unsupported'].append({'host': host, 'id': identity,
                                                  'reason': 'target hook disabled by local removal or edit'})
                    continue
                if old:
                    entries = _location(native, host, old['event'])
                    entry = old['entry']
                    found = entries.get(old['event']) if isinstance(entries, dict) else next(
                        (e for e in entries if e == entry), None)
                    if found != entry:
                        # Removed or edited entries are user choices, never replaced on an upgrade.
                        current[identity] = {**old, 'disabled': True}
                        report['conflicts'].append({'host': host, 'id': identity,
                                                    'reason': 'managed hook removed or locally edited'})
                        continue
                    if wanted == old:
                        current[identity] = old
                        report['counts']['unchanged'] += 1
                        continue
                    if isinstance(entries, dict):
                        del entries[old['event']]
                    else:
                        entries.remove(entry)
                    report['counts']['update' if wanted else 'remove'] += 1
                if wanted:
                    entries = _location(native, host, wanted['event'])
                    if isinstance(entries, dict):
                        if wanted['event'] in entries:
                            report['conflicts'].append({'host': host, 'id': identity,
                                                       'reason': 'unowned native hook name exists'})
                            continue
                        entries[wanted['event']] = wanted['entry']
                    else:
                        entries.append(wanted['entry'])
                    current[identity] = wanted
                    if not old:
                        report['counts']['create'] += 1
            if native != before and not dry_run:
                _save_target(home, host, path, native)
            new_targets[key] = current
            report['targets'].append({'host': host, 'path': key,
                                      'registered': sum(not r.get('disabled', False) for r in current.values()),
                                      'approval': 'native approval required; never granted by sync',
                                      'runtime_verified': False})
        except (OSError, ValueError, TypeError, sr.HubError) as error:
            report['errors'].append(f'{host}: {type(error).__name__}')
    report['ok'] = not report['errors'] and not report['conflicts']
    if not dry_run:
        sr.write_json(ledger_path, {'schema': SCHEMA, 'targets': new_targets})
        sr.write_json(hub / 'state/hook-sync-status.json', report)
    return report


def _context_output(host, stdout):
    if host == 'claude':
        return stdout
    if not stdout.strip():
        return '{}'
    try:
        data = json.loads(stdout)
    except ValueError:
        context = stdout.strip()
    else:
        if not isinstance(data, dict):
            raise sr.HubError('hook context output must be an object')
        extra = data.get('hookSpecificOutput', {})
        if not isinstance(extra, dict):
            raise sr.HubError('hookSpecificOutput must be an object')
        if set(data) - {'hookSpecificOutput', 'systemMessage', 'suppressOutput'} or \
                set(extra) - {'hookEventName', 'additionalContext'}:
            raise sr.HubError('hook output directive has no verified target translation')
        context = extra.get('additionalContext', data.get('systemMessage', ''))
        if not isinstance(context, str):
            raise sr.HubError('hook context must be text')
    if host.startswith('antigravity'):
        return json.dumps({'injectSteps': [{'systemMessage': {'systemMessage': context}}]})
    # Hermes on_session_start projection: emit {'context': context}, but note that
    # Hermes's native conversation loop may drop this output; runtime context injection
    # is not claimed as verified.
    return json.dumps({'context': context})


def run(spec_path, digest, stdin):
    data = _read_source_json(spec_path)
    if _hash(data) != digest:
        raise sr.HubError('hook spec content changed; native registration must be reviewed again')
    hook, host = data['hook'], data['host']
    if hook['root']:
        manifest = next((Path(p) for p, _ in hook['files']
                         if Path(p).name == 'plugin.json' and Path(p).parent.name == '.codex-plugin'),
                        Path(hook['root']) / 'plugin.json')
        current = _source_files(Path(hook['root']), manifest)
        if str(Path(hook['source']).resolve()) not in {p for p, _ in current}:
            current.append([str(Path(hook['source']).resolve()), sr.file_sha(hook['source'])])
        if sorted(current) != sorted(hook['files']):
            raise sr.HubError('hook support inventory changed; synchronize and review the new native command')
    for path, expected in hook['files']:
        if Path(path).is_symlink() or sr.file_sha(path) != expected:
            raise sr.HubError('hook support content changed; synchronize and review the new native command')
    payload = json.loads(stdin or '{}')
    if not isinstance(payload, dict):
        raise sr.HubError('hook stdin must be an object')
    if host != 'claude':
        paths = payload.get('workspacePaths') or []
        if not isinstance(paths, list) or any(not isinstance(p, str) for p in paths):
            raise sr.HubError('workspacePaths must be a list of paths')
        payload = {'session_id': payload.get('conversationId', payload.get('session_id', '')),
                   'cwd': (paths[0] if paths else payload.get('cwd', os.getcwd())),
                   'transcript_path': payload.get('transcriptPath', payload.get('transcript_path', '')),
                   'hook_event_name': 'SessionStart', 'source': 'startup'}
        matcher = hook.get('matcher')
        if matcher and not re.search(matcher, 'startup'):
            return '', 0
    env = os.environ.copy()
    for name in ('PLUGIN_DATA', 'COPILOT_PLUGIN_DATA', 'QODER_SESSION_ID',
                 'CODEBUDDY_PLUGIN_ROOT', 'CURSOR_VERSION'):
        env.pop(name, None)
    if hook['root']:
        env.update({'PLUGIN_ROOT': hook['root'], 'CLAUDE_PLUGIN_ROOT': hook['root']})
    if host != 'claude':
        target_state = data.get('target_state')
        if not target_state:
            hub = Path(spec_path).resolve().parent.parent
            target_state = str(hub / 'state/hook-sandbox' / host.replace(':', '_'))
        Path(target_state).mkdir(parents=True, exist_ok=True)
        env['CLAUDE_CONFIG_DIR'] = target_state
        env['CLAUDE_PROJECT_DIR'] = str(payload.get('cwd') or target_state)
        # Keep the source Codex protocol without touching its real plugin state.
        plugin_data = Path(target_state) / 'plugin-data' / _hash(hook['origin'])[:16]
        plugin_data.mkdir(parents=True, exist_ok=True)
        env['PLUGIN_DATA'] = str(plugin_data)
    command = hook['handler']['command']
    # command is the source's portable shell form; commandWindows may be PowerShell-only.
    if os.name == 'nt' and hook['handler'].get('commandWindows'):
        argv = ['pwsh', '-NoProfile', '-NonInteractive', '-Command', hook['handler']['commandWindows']]
        shell_options = {}
    elif os.name == 'nt':
        # A list passed to cmd /c escapes embedded quotes for C argv, breaking node -e.
        argv = command
        shell_options = {'shell': True, 'executable': os.environ.get('COMSPEC', 'cmd.exe')}
    else:
        argv = ['/bin/sh', '-c', command]
        shell_options = {}
    result = subprocess.run(argv, input=json.dumps(payload), text=True, encoding='utf-8',
                            capture_output=True, env=env, cwd=payload.get('cwd') or None,
                            timeout=hook['handler'].get('timeout', 30), **shell_options)
    if result.stderr:
        sys.stderr.write(result.stderr)
    return _context_output(host, result.stdout), result.returncode


def main():
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path)
    parser.add_argument('--sha256')
    parser.add_argument('--home', type=Path)
    parser.add_argument('--hub', type=Path)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    try:
        if args.run:
            if not args.sha256:
                parser.error('--run requires --sha256')
            output, code = run(args.run, args.sha256, sys.stdin.read(1024 * 1024 + 1))
            print(output, end='')
            return code
        report = sync(args.home, args.hub, args.dry_run)
        print(json.dumps(report, ensure_ascii=False))
        return 0 if report['ok'] else 1
    except (OSError, ValueError, sr.HubError, subprocess.TimeoutExpired) as error:
        print(f'hook bridge refused: {type(error).__name__}: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
