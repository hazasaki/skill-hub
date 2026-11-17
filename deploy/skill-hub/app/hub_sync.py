"""Automatic incremental synchronisation of the shared agent hub.

Ownership is deliberately narrow. This module owns:

* ``<hub>/state/autosync.json`` - daemon state and last cycle summary
* ``<hub>/state/autosync.log``  - append-only JSONL cycle log (rotated once)
* ``<hub>/state/autosync.pid``  - live worker pid
* its own at-login registration and hidden worker process

It owns nothing else under the hub. Every resource, manifest, provenance and
status write goes through :mod:`shared_resources` (``ingest``/``sync``/
``bootstrap``), which stays the sole writer of ``manifest.json``,
``provenance.json``, ``skills/``, ``prompts/`` and ``state/sync-status.json``.

Why ``bootstrap`` and not ``sync`` on a changed source root: ``sync`` only
distributes what the hub already holds, so a skill added to a host root would
never reach the hub and would look "already in sync" forever. One pass in which
a source changed therefore always runs ``ingest`` + ``sync``.

Single instance: a Windows named mutex (``Local\\AgentHubAutosync`` by default)
held for the life of the process, so an at-login start and a manual start
cannot both run. Non-Windows falls back to an exclusive pid file.

Portable content belongs to ``shared_resources``; native hook projections belong
to ``resource_hooks``. Both run under the same cycle lock. Discovery executes no
plugin code and never copies credentials or native trust grants.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import shared_resources as sr  # noqa: E402  (same directory, single installed layout)

SCHEMA = 1
DEFAULT_SINGLETON = 'Local\\AgentHubAutosync'
DEFAULT_INTERVAL = 300
DEFAULT_DEBOUNCE = 30
DEFAULT_STALE = 600
DEFAULT_STOP_WAIT = 20
# Serialization-only slack for the recorded creation time, in seconds. The recorded
# value is the OS creation time read back after spawn, so this never has to absorb
# spawn latency: 2.0 s admits ISO rounding and clock granularity, not another process.
WORKER_IDENTITY_WAIT = 5
LOG_MAX_BYTES = 1024 * 1024
AUTOSTART_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
AUTOSTART_NAME = 'AgentHubAutosync'
KILL_FILE = 'taskkill.exe'
# The only paths this module may write inside the hub. Everything else raises.
OWNED_STATE = ('state/autosync.json', 'state/autosync.log', 'state/autosync.log.1',
               'state/autosync.pid', 'state/autosync.stop', 'state/cycle.lock')
# Fingerprinting bounds. These are *safety* bounds against a pathological tree, not
# expected sizes: a real root already exceeds 4000 files (antigravity), and a cap that
# low would hide every edit past it - the signature would look stable while a source
# the hub actually distributes had changed. The per-root budget keeps one huge root
# from consuming the allowance of the roots after it; the overall budget is the last
# defence against a runaway filesystem walk. A truncated signature is reported as
# `truncated`/`incomplete`, never silently trusted as complete.
MAX_FINGERPRINT_ENTRIES = 100_000
MAX_FINGERPRINT_TOTAL = 250_000
# Directories that are never user content and can be arbitrarily large.
_FINGERPRINT_PRUNE = frozenset({'__pycache__', '.git', 'node_modules', '.pytest_cache',
                                '.mypy_cache', '.ruff_cache', '.venv', 'venv'})
# Shared with shared_resources so a manual onboarding run and the watcher can never
# write the hub at the same time. Same directory, same naming, one owner: whichever
# pass holds it, the other waits or skips.
CYCLE_LOCK = 'state/cycle.lock'


class StateError(sr.HubError):
    """A write outside this module's owned state was attempted."""


def _walk_tree(root, problems=None):
    """Stream files of one source tree, pruning caches *during* the walk.

    ``Path.rglob`` builds the entire match list first, so a ``node_modules`` or
    ``.git`` inside a source tree would be materialised in memory before any budget
    could apply. This walks with ``os.scandir`` and never descends into a pruned
    directory, so memory stays proportional to one directory's children.
    Symlinks are not followed (a link cannot be fingerprinted deterministically and
    shared_resources refuses them as a source). An unreadable directory is recorded
    as a problem and skipped, never turned into a silent "no files" verdict.
    """
    stack = [Path(root)]
    while stack:
        current = stack.pop()
        try:
            children = sorted(os.scandir(current), key=lambda entry: entry.name)
        except OSError as error:
            if problems is not None:
                problems.append({'reason': f'unreadable directory: {error.__class__.__name__}',
                                 'path': str(current)})
            continue
        directories = []
        for entry in children:
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if is_dir:
                if entry.name.lower() not in _FINGERPRINT_PRUNE:
                    directories.append(Path(entry.path))
                continue
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
            except OSError:
                continue
            yield Path(entry.path)
        # Deterministic order without materialising the whole tree: push in reverse so
        # a LIFO stack visits the alphabetically-first subdirectory next.
        stack.extend(reversed(directories))


# --------------------------------------------------------------------------- state paths

def state_paths(hub=None):
    root = sr.hub_paths(hub)['root']
    return {'root': root, 'state': root / 'state', 'json': root / 'state/autosync.json',
            'log': root / 'state/autosync.log', 'pid': root / 'state/autosync.pid'}


def _own(path, hub=None):
    """Refuse any hub write that is not one of this module's own state files."""
    root = sr.hub_paths(hub)['root'].resolve()
    target = Path(path).resolve()
    try:
        rel = target.relative_to(root).as_posix()
    except ValueError:
        return target  # outside the hub: caller's own directory, always allowed
    if rel not in OWNED_STATE:
        raise StateError(f'refusing to write {rel}: not autosync state')
    return target


def write_state(hub, state):
    paths = state_paths(hub)
    paths['state'].mkdir(parents=True, exist_ok=True)
    target = _own(paths['json'], hub)
    text = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + '\n'
    before = target.read_text(encoding='utf-8') if target.exists() else None
    if before == text:
        return False
    temp = target.with_name(target.name + '.tmp')
    temp.write_text(text, encoding='utf-8')
    os.replace(temp, target)
    return True


def read_state(hub=None):
    path = state_paths(hub)['json']
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def append_log(hub, event):
    paths = state_paths(hub)
    paths['state'].mkdir(parents=True, exist_ok=True)
    target = _own(paths['log'], hub)
    try:
        if target.exists() and target.stat().st_size > LOG_MAX_BYTES:
            rotate = _own(paths['state'] / 'autosync.log.1', hub)
            os.replace(target, rotate)
    except OSError:
        pass
    line = json.dumps(event, ensure_ascii=False, sort_keys=True)
    with target.open('a', encoding='utf-8') as handle:
        handle.write(line + '\n')


# --------------------------------------------------------------------------- singleton

_held = {}
_hold_depth = {}


def acquire_singleton(name=DEFAULT_SINGLETON):
    """Hold the named singleton for this process. Returns (acquired, handle).

    Reentrant with reference counting: a nested acquire increments a depth counter
    instead of taking a second Windows handle. Without this, an inner release would
    drop the outer hold and let another process in while work was still running.
    """
    if name in _held:
        _hold_depth[name] = _hold_depth.get(name, 1) + 1
        return True, _held[name]
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.CreateMutexW(None, False, name)
        error = ctypes.get_last_error()
        if not handle:
            raise OSError(f'CreateMutexW failed ({error})')
        if error == 183:  # ERROR_ALREADY_EXISTS: another worker holds it
            kernel32.CloseHandle(handle)
            return False, None
        _held[name] = handle
        return True, handle
    path = state_paths()['pid']
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False, None
    with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
        handle.write(str(os.getpid()))
    _held[name] = path
    return True, path


def release_singleton(name=DEFAULT_SINGLETON):
    """Release one level. The OS handle is dropped only when the depth reaches zero."""
    handle = _held.get(name)
    if handle is None:
        return False
    depth = _hold_depth.get(name, 1) - 1
    if depth > 0:
        _hold_depth[name] = depth
        return False  # still held by an outer frame
    _hold_depth.pop(name, None)
    _held.pop(name, None)
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.ReleaseMutex(handle)
        kernel32.CloseHandle(handle)
    else:
        Path(handle).unlink(missing_ok=True)
    return True


# --------------------------------------------------------------------------- cycle lock

def cycle_lock(hub=None):
    """An exclusive, hub-scoped lock for every writer of the hub.

    One lock for both this module's automatic cycles and a manual
    ``shared_resources.bootstrap``/``sync``/``ingest`` run, so no two processes
    can write resources, manifest or provenance concurrently. It is a named
    Windows mutex derived from the hub path (not a file) so an abandoned lock
    cannot outlive its process.
    """
    root = Path(sr.hub_paths(hub)['root']).resolve()
    # A stable, path-derived name: same hub, same lock, in every process.
    digest = hashlib.sha256(str(root).casefold().encode('utf-8')).hexdigest()[:16]
    return f'Local\\AgentHubCycle-{digest}'


class CycleBusy(RuntimeError):
    """Another writer holds the hub cycle lock."""


def acquire_cycle_lock(hub=None, timeout=0):
    """Try to take the hub cycle lock. Returns a handle, or raises CycleBusy.

    Reentrant in-process through the same counted path as the worker singleton, so
    install code can nest a cycle inside a lock it already holds.
    """
    name = cycle_lock(hub)
    deadline = time.monotonic() + max(0, timeout)
    while True:
        acquired, handle = acquire_singleton(name)
        if acquired:
            return handle
        if time.monotonic() >= deadline:
            raise CycleBusy(f'another hub writer holds {name}')
        time.sleep(min(1.0, max(0.1, deadline - time.monotonic())))


def release_cycle_lock(hub=None):
    return release_singleton(cycle_lock(hub))


class hub_cycle:
    """Context manager: exclusive hub write for one cycle.

    Yields True when the lock was taken. With ``wait=True`` it blocks up to
    ``timeout``; otherwise a busy hub yields False without running the cycle.
    """

    def __init__(self, hub=None, *, wait=False, timeout=0):
        self.hub, self.wait, self.timeout = hub, wait, timeout
        self.acquired = False

    def __enter__(self):
        try:
            acquire_cycle_lock(self.hub, timeout=self.timeout if self.wait else 0)
            self.acquired = True
        except CycleBusy:
            self.acquired = False
        return self.acquired

    def __exit__(self, *exc):
        if self.acquired:
            release_cycle_lock(self.hub)
        return False


# --------------------------------------------------------------------------- fingerprint

def _source_pairs(home=None):
    """Normalise ``shared_resources.discovery_sources()`` into (origin, path) pairs.

    The frozen contract is ``(kind, origin, path, suffixes)``. It was ``(origin, path)``
    and briefly ``(origin, path, extra)`` during the rollout, so read it positionally
    when the documented shape matches and fall back to structural detection otherwise.
    An unrecognised row is reported, never silently dropped.

    Positional reading matters: a structural guess treated the literal origin ``agents``
    as a resource *kind* and mislabelled the source as the directory name ``.agents``.
    """
    home = Path(home) if home is not None else Path.home()
    try:
        sources, problems = sr.discovery_sources(home)
    except Exception as error:  # a discovery failure must not kill the daemon
        return [], [{'reason': f'discovery failed: {error.__class__.__name__}'}]
    kinds = {'skill', 'agent', 'prompt'}
    pairs = []
    for row in sources or []:
        if isinstance(row, (str, Path)):
            pairs.append((Path(row).name, Path(row)))
            continue
        if not isinstance(row, (tuple, list)):
            problems = list(problems or []) + [{'reason': 'unrecognised discovery row'}]
            continue
        # Documented shape: (kind, origin, path, suffixes) or (origin, path[, extra]).
        if len(row) >= 3 and isinstance(row[0], str) and row[0].lower() in kinds:
            kind, origin, path = row[0], row[1], row[2]
            if isinstance(path, (str, Path)):
                pairs.append((str(origin), Path(path)))
                continue
        path = next((v for v in row if isinstance(v, Path)), None)
        if path is None:
            path = next((Path(v) for v in row if isinstance(v, str) and
                         ('/' in v or '\\' in v)), None)
        origin = next((v for v in row if isinstance(v, str) and v.lower() not in kinds
                       and (path is None or v != str(path))), None)
        if path is None:
            problems = list(problems or []) + [{'reason': 'discovery row without a path'}]
            continue
        pairs.append((origin or path.parent.name, path))
    return pairs, list(problems or [])


def source_fingerprint(home=None):
    """Bounded recursive signature of every discoverable portable source.

    Every file inside a source tree is fingerprinted (relative path, mtime, size),
    not just ``SKILL.md``: editing a supporting script, a ``references/`` file or
    a command/prompt asset is a real source change and must be ingested.

    A *top-level* portable file is fingerprinted too. Agents and prompts are single
    files (``.codex/agents/*.toml``, ``.codex/prompts/*.md``, ``.claude/agents/*.md``),
    not trees, so the tree walk never reached them: adding or editing one left the
    signature untouched and the change was never ingested. They are matched by the
    supported portable suffixes, and hidden names (``.hub-index.json``, our own
    marker) and caches are skipped because they are distribution bookkeeping rather
    than user content.

    A child carrying our own ``hub-marker.json`` is a *distributed copy*, not a
    source: distributing into a native root would otherwise change the signature
    and make every cycle look like a source change forever. Single-file resources
    are distributed without a per-file marker, so a freshly written copy does
    register as one source change; it settles on the following cycle because
    distribution is idempotent and does not rewrite an unchanged file.

    The walk is bounded by ``MAX_FINGERPRINT_ENTRIES`` per root and
    ``MAX_FINGERPRINT_TOTAL`` overall so a pathological tree cannot stall a cycle.
    Both bounds are safety limits far above any real root, so a source edit is
    detected, not hidden. Caches and VCS metadata are pruned rather than counted.
    A truncated signature is reported as ``truncated``/``incomplete`` and never
    silently trusted as complete: a caller can then say "the signature is not
    authoritative" instead of claiming the sources are stable.
    """
    pairs, problems = _source_pairs(home)
    digest = hashlib.sha256()
    trees = files = 0
    truncated = False
    read_errors = []
    marker = getattr(sr, 'MARKER', 'hub-marker.json')
    portable = ('.md', '.toml')  # the only single-file resources this hub distributes
    for origin, root in pairs:
        root = Path(root)
        try:
            present = root.is_dir()
        except OSError as error:
            read_errors.append({'reason': f'unreadable root: {error.__class__.__name__}',
                                'path': str(root)})
            present = False
        digest.update(f'{origin}|{root}|{present}\n'.encode('utf-8'))
        if not present:
            continue
        try:
            children = sorted(root.iterdir(), key=lambda p: p.name)
        except OSError as error:
            read_errors.append({'reason': f'unreadable root: {error.__class__.__name__}',
                                'path': str(root)})
            continue
        # Budget per root, not per pass: a huge root must not silence the roots after it.
        counted = 0
        root_truncated = False
        for child in children:
            try:
                is_tree = child.is_dir()
            except OSError as error:
                read_errors.append({'reason': f'unreadable entry: {error.__class__.__name__}',
                                    'path': str(child)})
                continue
            if not is_tree:
                # A top-level single-file resource (an agent or a prompt) is its own
                # source. Bookkeeping and caches are not user content.
                name = child.name
                if (name.startswith('.') or name == marker
                        or child.suffix.lower() not in portable):
                    continue
                if (counted >= MAX_FINGERPRINT_ENTRIES
                        or files >= MAX_FINGERPRINT_TOTAL):
                    root_truncated = True
                    break
                try:
                    stat = child.stat()
                except OSError as error:
                    read_errors.append({'reason': f'unreadable file: {error.__class__.__name__}',
                                        'path': str(child)})
                    continue
                if not child.is_file():
                    continue
                counted += 1
                files += 1
                # `file:` keeps a document distinct from a tree of the same name.
                digest.update(f'{origin}/file:{name}|{stat.st_mtime_ns}|{stat.st_size}\n'
                              .encode('utf-8'))
                continue
            if (child / marker).is_file():
                continue  # our own distributed copy
            if child.name.lower() in _FINGERPRINT_PRUNE:
                continue  # a cache/VCS directory at the root is not a resource either
            trees += 1
            # Streamed, pruned walk: caches are never entered, so the budget applies
            # during traversal instead of after a full tree has been listed. Read
            # failures go to `read_errors` (which makes the scan incomplete), not to
            # `problems` (benign discovery skips such as a disabled plugin).
            for path in _walk_tree(child, read_errors):
                if path.name == marker:
                    continue
                if (counted >= MAX_FINGERPRINT_ENTRIES
                        or files >= MAX_FINGERPRINT_TOTAL):
                    root_truncated = True
                    break
                try:
                    stat = path.stat()
                except OSError as error:
                    read_errors.append({'reason': f'unreadable file: {error.__class__.__name__}',
                                        'path': str(path)})
                    continue
                counted += 1
                files += 1
                rel = path.relative_to(child).as_posix()
                digest.update(f'{origin}/{child.name}/{rel}|{stat.st_mtime_ns}|'
                              f'{stat.st_size}\n'.encode('utf-8'))
            if root_truncated:
                break
        if root_truncated:
            # Recorded so a bounded (incomplete) signature never reads as a complete one.
            truncated = True
            digest.update(f'truncated|{origin}|{root}|{counted}\n'.encode('utf-8'))
    for problem in problems:
        digest.update(('problem|' + json.dumps(problem, sort_keys=True, default=str) + '\n')
                      .encode('utf-8'))
    # Hooks, manifests and support scripts can change without any SKILL.md changing.
    try:
        import resource_hooks
        hook_signature = resource_hooks.source_signature(home)
        digest.update(('hooks|' + json.dumps(hook_signature, sort_keys=True, default=str))
                      .encode('utf-8'))
        if isinstance(hook_signature, dict):
            read_errors.extend({'reason': str(error)[:300]}
                               for error in hook_signature.get('errors', []))
    except Exception as error:
        read_errors.append({'reason': f'hook discovery failed: {error.__class__.__name__}'})
    # An unreadable accepted source is not the same as a deliberately disabled plugin:
    # it means part of the signature is missing, so `incomplete` must say so.
    for error in read_errors:
        digest.update(('read-error|' + json.dumps(error, sort_keys=True, default=str) + '\n')
                      .encode('utf-8'))
    incomplete = truncated or bool(read_errors)
    return {'sha256': digest.hexdigest(), 'trees': trees, 'files': files,
            'truncated': truncated, 'incomplete': incomplete,
            'read_errors': [{k: v for k, v in row.items() if k == 'reason'}
                            for row in read_errors[:20]],
            'limits': {'per_root': MAX_FINGERPRINT_ENTRIES, 'total': MAX_FINGERPRINT_TOTAL},
            'sources': len(pairs)}


def _summary(report):
    """Bounded, sanitised view of a shared_resources report (no file contents).

    ``variants`` is reported separately from ``conflicts``: a benign divergent copy of
    the same slug is archived by the hub owner and must not read as a failure.
    """
    if not isinstance(report, dict):
        return None
    unsupported = [u for u in (report.get('unsupported') or []) if isinstance(u, dict)]
    host_counts = report.get('host_counts')
    if not isinstance(host_counts, dict):
        # `counts` is not per host in every report shape (ingest reports one set).
        # Derive a per-host view from `resources[].per_host` so a host-scoped refusal
        # is still visible through the summary instead of collapsing into a total.
        host_counts = {}
        for row in report.get('resources') or []:
            if not isinstance(row, dict):
                continue
            for host, action in (row.get('per_host') or {}).items():
                host_counts.setdefault(str(host), {})
                host_counts[str(host)][str(action)] = \
                    host_counts[str(host)].get(str(action), 0) + 1
    return {'ok': report.get('ok'), 'dry_run': report.get('dry_run'),
            'counts': report.get('counts', {}),
            'host_counts': host_counts,
            'hosts': report.get('hosts') if isinstance(report.get('hosts'), dict) else None,
            'conflicts': report.get('conflicts', [])[:20],
            'variants': [{'name': v.get('name'), 'origin': v.get('origin'),
                          'archived': v.get('archive') or v.get('path')}
                         for v in (report.get('variants') or [])[:20]
                         if isinstance(v, dict)] if isinstance(report.get('variants'), list)
            else report.get('variants'),
            'unsupported': [{'name': u.get('name'), 'kind': u.get('kind'),
                             'scope': u.get('scope'), 'host': u.get('host'),
                             'path': u.get('path'), 'reason': u.get('reason')}
                            for u in unsupported[:20]],
            'warnings': [{'name': w.get('name'), 'kind': w.get('kind'),
                          'host': w.get('host'), 'reason': w.get('reason')}
                         for w in (report.get('warnings') or [])[:20]
                         if isinstance(w, dict)],
            'errors': [str(e)[:300] for e in (report.get('errors') or [])[:10]]}


# --------------------------------------------------------------------------- one cycle

def cycle(home=None, hub=None, dry_run=False, *, force=False, debounce=None):
    """Run exactly one synchronisation pass and return structured status.

    ``bootstrap`` (ingest + sync) is used on the first pass, whenever the
    discovered source fingerprint moved, or when forced; otherwise ``sync``.
    """
    home = Path(home) if home is not None else Path.home()
    paths = state_paths(hub)
    previous = read_state(hub)
    debounce = DEFAULT_DEBOUNCE if debounce is None else debounce
    interval = previous.get('interval_s') or DEFAULT_INTERVAL
    started_epoch = time.time()
    stamp = dt.datetime.now(dt.timezone.utc).isoformat()
    result = {'schema': SCHEMA, 'ok': True, 'dry_run': bool(dry_run), 'hub': str(paths['root']),
              'home': str(home), 'mode': None, 'trigger': None, 'started_at': stamp,
              'finished_at': None, 'duration_ms': None,
              'fingerprint': None, 'ingest': None, 'sync': None,
              'counts': {}, 'conflicts': [], 'variants': [], 'errors': [],
              'skipped_reason': None}
    if not force and debounce > 0:
        last = previous.get('last_finished_epoch')
        if isinstance(last, (int, float)) and started_epoch - last < debounce:
            result.update({'mode': 'debounced', 'ok': True, 'completed': False,
                           'outcome': 'debounced',
                           'skipped_reason': f'last cycle {started_epoch - last:.1f}s ago '
                                             f'< debounce {debounce}s',
                           'finished_at': stamp, 'duration_ms': 0})
            return result
    if not dry_run:
        # Refuse a busy writer before scanning thousands of sources; recheck before writing.
        with hub_cycle(hub) as acquired:
            if not acquired:
                result.update({'mode': 'busy', 'ok': False, 'completed': False,
                               'outcome': 'busy',
                               'skipped_reason': 'another hub writer holds the cycle lock',
                               'finished_at': stamp, 'duration_ms': 0})
                return result
    fingerprint = source_fingerprint(home)
    result['fingerprint'] = fingerprint
    previous_fp = previous.get('source_fingerprint')
    if previous_fp is None:
        mode, trigger = 'bootstrap', 'first-run'
    elif previous_fp != fingerprint['sha256']:
        mode, trigger = 'bootstrap', 'source-changed'
    else:
        mode, trigger = 'sync', 'periodic'
    if fingerprint.get('incomplete'):
        # A partial signature is not authoritative: a part of an accepted source was not
        # read (bounded, or unreadable), so an edit there never entered it and "no change"
        # cannot be trusted. Reported as an explicit error rather than silently keeping
        # `ok=True`, and `_finish_cycle` refuses to advance the stored signature from an
        # incomplete one.
        result['incomplete_fingerprint'] = True
        detail = (f'bounded at {fingerprint.get("limits")}' if fingerprint.get('truncated')
                  else 'unreadable source')
        result['errors'] = [f'fingerprint incomplete ({detail}); part of the sources was '
                            'not read, so a "stable" verdict is not claimed']
    result['mode'], result['trigger'] = mode, trigger
    # `completed` is the honest field: it is true only when a report actually came
    # back. `ok` alone conflated "nothing failed" with "something ran", which let a
    # skipped (busy/debounced) cycle print as a success and exit 0.
    result.update({'completed': False, 'outcome': None})
    if dry_run:
        # A dry run performs the real read-only pass: it must not take the writer
        # lock (it writes nothing) and must not record state. Acquiring and releasing
        # the lock here would have been theatre - it proved nothing about the hub.
        _run_report(home, hub, mode, result, dry_run=True)
        result['outcome'] = 'dry-run' if result['ok'] else 'failed'
        _apply_fingerprint_verdict(result)
        result['completed'] = True
        return _finish_cycle(home, hub, paths, previous, result, fingerprint,
                             started_epoch, debounce, interval, dry_run=True)
    with hub_cycle(hub) as acquired:
        if not acquired:
            result.update({'mode': 'busy', 'ok': False, 'completed': False,
                           'outcome': 'busy',
                           'skipped_reason': 'another hub writer holds the cycle lock',
                           'finished_at': dt.datetime.now(dt.timezone.utc).isoformat(),
                           'duration_ms': 0})
            return result
        _run_report(home, hub, mode, result, dry_run=False)
        result['outcome'] = 'ok' if result['ok'] else 'failed'
        _apply_fingerprint_verdict(result)
        result['completed'] = True
        return _finish_cycle(home, hub, paths, previous, result, fingerprint,
                             started_epoch, debounce, interval, dry_run=False)


def _run_report(home, hub, mode, result, *, dry_run):
    """Run the ingest/sync report for `mode` and fold it into `result` in place.

    `conflicts` is assembled *before* the signature is taken and the signature is
    stored on the result itself, so two cycles that hit the same unresolved
    conflict compare equal. A failed cycle carries no report and no signature;
    `_finish_cycle` then holds the fingerprint back, which is what triggers a retry.
    """
    try:
        if mode == 'bootstrap':
            report = sr.bootstrap(home=home, hub=hub, dry_run=dry_run)
            result['ingest'] = _summary(report.get('ingest'))
            result['sync'] = _summary(report.get('sync'))
            result['ok'] = bool(report.get('ok'))
            result['counts'] = {'ingest': (report.get('ingest') or {}).get('counts', {}),
                                'sync': (report.get('sync') or {}).get('counts', {})}
            result['conflicts'] = ((report.get('ingest') or {}).get('conflicts', [])
                                   + (report.get('sync') or {}).get('conflicts', []))[:40]
            # Benign divergent copies are archived by the hub owner; report, never fail.
            result['variants'] = (report.get('ingest') or {}).get('variants') or []
            ingest_errors = (report.get('ingest') or {}).get('errors') or []
            sync_errors = (report.get('sync') or {}).get('errors') or []
            result['errors'] = [str(e)[:300] for e in
                                (ingest_errors if isinstance(ingest_errors, list)
                                 else [ingest_errors])
                                + (sync_errors if isinstance(sync_errors, list)
                                   else [sync_errors])][:10]
        else:
            report = sr.sync(home=home, hub=hub, dry_run=dry_run)
            result['sync'] = _summary(report)
            result['ok'] = bool(report.get('ok'))
            result['counts'] = {'sync': report.get('counts', {})}
            result['conflicts'] = report.get('conflicts', [])[:40]
            result['variants'] = report.get('variants') or []
            result['errors'] = [str(e)[:300] for e in report.get('errors', [])[:10]]
    except sr.HubError as error:
        result.update({'ok': False, 'errors': [f'HubError: {error}']})
    except Exception as error:  # a daemon must record, not die on, an unexpected failure
        result.update({'ok': False,
                       'errors': [f'{error.__class__.__name__}: {str(error)[:300]}']})
    try:
        import resource_hooks
        hooks = resource_hooks.sync(home=home, hub=hub, dry_run=dry_run)
        result['hooks'] = hooks
        result.setdefault('counts', {})['hooks'] = hooks.get('counts', {})
        result['ok'] = bool(result.get('ok')) and bool(hooks.get('ok'))
        result.setdefault('errors', []).extend(str(error)[:300]
                                              for error in hooks.get('errors', [])[:10])
        result.setdefault('conflicts', []).extend(hooks.get('conflicts', [])[:40])
    except Exception as error:
        result['ok'] = False
        result.setdefault('errors', []).append(
            f'hook sync failed: {error.__class__.__name__}: {str(error)[:200]}')
    result['conflict_keys'] = _conflict_keys(result)


def _conflict_keys(result):
    """Canonical, content-free identity keys for one cycle's unresolved conflicts.

    Keyed by (name, host, reason) and sorted, so the same disagreement compares equal
    even when one cycle reports it from ingest and the next from attach: the shapes
    differ, the underlying problem does not.
    """
    keys = set()
    for row in result.get('conflicts') or []:
        if isinstance(row, dict):
            keys.add(('name', str(row.get('name') or row.get('id')), 'host', str(row.get('host')),
                      'reason', str(row.get('reason'))))
        else:
            keys.add(('row', str(row)))
    return sorted(keys)


def _apply_fingerprint_verdict(result):
    """Fold the bounded-fingerprint verdict into `ok` after the report ran.

    The report may have come back clean (`ok=True`) over a *partial* file view, which
    would be a false "sources stable". Marking the cycle not-ok here keeps that from
    being reported as success; `_finish_cycle` separately refuses to advance the
    stored signature from an incomplete one.
    """
    if result.get('incomplete_fingerprint'):
        if result.get('ok'):
            result['ok'] = False
        result['outcome'] = 'incomplete'


def _fingerprint_meta(fingerprint):
    """The bounded, content-free part of a fingerprint fit for state and status."""
    if not isinstance(fingerprint, dict):
        return None
    return {'sha256': fingerprint.get('sha256'), 'trees': fingerprint.get('trees'),
            'files': fingerprint.get('files'), 'sources': fingerprint.get('sources'),
            'truncated': bool(fingerprint.get('truncated')),
            'incomplete': bool(fingerprint.get('incomplete')),
            'read_errors': fingerprint.get('read_errors') or [],
            'limits': fingerprint.get('limits'),
            'complete': not bool(fingerprint.get('incomplete'))}


def _finish_cycle(home, hub, paths, previous, result, fingerprint, started_epoch, debounce,
                  interval, dry_run):
    """Record the cycle and decide whether the stored fingerprint may advance.

    A cycle that *failed* (a raised HubError/exception) must not look "already
    ingested": if the stored signature moved, the next cycle would downgrade to
    ``sync`` and never retry the ingest that failed. Keeping the old signature
    forces a retry.

    An *unresolved conflict* is different: it is a stable disagreement between the
    hub and some source or host copy (a local edit, a pre-existing file). The daemon
    cannot resolve it and re-running bootstrap does not change it, so treating every
    conflict as a retry signal looped bootstrap forever and burned a full ingest on
    every cycle. The rule is therefore: hold the fingerprint back only while the
    cycle failed or while the conflict set is still changing; once the same conflict
    set repeats, one more cycle proves no progress is possible and the fingerprint
    advances (the conflict is still reported and still logged). A later source
    change moves the fingerprint again and triggers a fresh bootstrap.
    """
    finished_epoch = time.time()
    result['finished_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
    result['duration_ms'] = int((finished_epoch - started_epoch) * 1000)
    if dry_run:
        return result
    state = dict(previous)
    keys = tuple(tuple(part) for part in (result.get('conflict_keys') or []))
    previous_keys = tuple(tuple(part) for part in (previous.get('conflict_keys') or []))
    accepted = tuple(tuple(part) for part in (previous.get('accepted_conflict_keys') or []))
    conflicts = bool(result.get('conflicts')) or not result.get('ok')
    failed = bool(result.get('errors'))
    incomplete = bool(fingerprint.get('incomplete'))
    # A conflict set that is unchanged since the last cycle, or that is a subset of a
    # set already accepted as unresolvable, is settled: no further bootstrap can help.
    settled = conflicts and bool(keys) and (
        keys == previous_keys or (bool(accepted) and set(keys) <= set(accepted)))
    # An incomplete fingerprint must never be recorded as the accepted baseline: later
    # cycles would compare against a partial view and could miss a real edit.
    advanced = (bool(result['ok'])
                or (bool(result.get('completed')) and not failed and settled)) and not incomplete
    state.update({
        'schema': SCHEMA, 'hub': str(paths['root']), 'home': str(home),
        'interval_s': interval, 'debounce_s': debounce,
        'singleton': previous.get('singleton', DEFAULT_SINGLETON),
        'source_fingerprint': fingerprint['sha256'] if advanced
        else previous.get('source_fingerprint'),
        'pending_fingerprint': None if advanced else fingerprint['sha256'],
        'conflict_keys': [list(part) for part in keys] if conflicts else [],
        'accepted_conflict_keys': ([list(part) for part in keys] if (advanced and settled)
                                   else [list(part) for part in accepted]),
        # Recorded completeness, not a default: health integration must be able to tell
        # "unknown" from "checked and complete". Metadata only, never file contents.
        'last_fingerprint': _fingerprint_meta(fingerprint),
        'last_variants': (result.get('variants') or [])[:20],
        'first_run_at': previous.get('first_run_at') or result['started_at'],
        'last_cycle': {k: result[k] for k in
                       ('mode', 'trigger', 'ok', 'completed', 'outcome', 'started_at',
                        'finished_at', 'duration_ms', 'counts', 'errors', 'hooks')
                       if k in result},
        'last_finished_epoch': finished_epoch,
        'cycles': int(previous.get('cycles') or 0) + 1,
        'last_error': (result['errors'] or [None])[0],
    })
    state.setdefault('enabled', False)
    write_state(hub, state)
    append_log(hub, {'event': 'cycle', **{k: result[k] for k in
                                          ('mode', 'trigger', 'ok', 'duration_ms',
                                           'counts', 'errors')},
                     'variants': len(result.get('variants') or []),
                     'conflicts': len(result.get('conflicts') or []),
                     'fingerprint_advanced': advanced,
                     'at': result['finished_at']})
    return result


def status(home=None, hub=None):
    """Report daemon state without writing anything or running a cycle."""
    home = Path(home) if home is not None else Path.home()
    paths = state_paths(hub)
    state = read_state(hub)
    pid = worker_pid(hub)
    # Report what is actually registered, not the command we would write. Comparing the
    # desired command with itself reported "installed" on a hub that was never enabled.
    # The interval must come from state: a hub enabled with --interval 60 would otherwise
    # look mismatched purely because status used its default interval.
    interval = state.get('interval_s', DEFAULT_INTERVAL)
    registered = autostart_read()
    desired = state.get('autostart_command') or autostart_command(home, hub, interval)
    return {'schema': SCHEMA, 'hub': str(paths['root']), 'home': str(home),
            'enabled': bool(state.get('enabled')),
            'running': bool(pid),
            'worker_verified': bool(pid) and verify_worker(pid, hub),
            'pid': pid,
            'cycles': state.get('cycles', 0),
            'interval_s': state.get('interval_s', DEFAULT_INTERVAL),
            'debounce_s': state.get('debounce_s', DEFAULT_DEBOUNCE),
            'first_run_at': state.get('first_run_at'),
            'last_cycle': state.get('last_cycle'),
            'last_variants': len(state.get('last_variants') or []),
            'last_error': state.get('last_error'),
            'source_fingerprint': state.get('source_fingerprint'),
            'pending_fingerprint': state.get('pending_fingerprint'),
            # Recorded completeness of the last source scan; None means "never scanned",
            # which a health reader must treat as unknown, not as complete.
            'last_fingerprint': state.get('last_fingerprint'),
            'fingerprint_complete': (state.get('last_fingerprint') or {}).get('complete')
            if isinstance(state.get('last_fingerprint'), dict) else None,
            'conflicts': len(state.get('conflict_keys') or []),
            'state_file': str(paths['json']),
            'log_file': str(paths['log']),
            'autostart_registered': registered,
            'autostart_expected': desired,
            'autostart_matches': bool(registered) and registered == desired,
            'limits': ['disk state only; does not prove a cycle is currently healthy',
                       'at-login restart requires the Run key and a live login session']}


# --------------------------------------------------------------------------- worker

def worker_pid(hub=None):
    path = state_paths(hub)['pid']
    if not path.is_file():
        return None
    try:
        pid = int(path.read_text(encoding='utf-8').strip() or 0)
    except (OSError, ValueError):
        return None
    if pid <= 0 or not pid_alive(pid):
        return None
    return pid


def pid_alive(pid):
    if os.name != 'nt':
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    try:
        result = subprocess.run(['tasklist', '/FI', f'PID eq {pid}', '/NH'],
                                capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False
    return str(pid) in (result.stdout or '')


def python_launcher(hidden=True):
    """Prefer pythonw.exe so an at-login worker never opens a console window."""
    exe = Path(sys.executable)
    if hidden and os.name == 'nt':
        candidate = exe.with_name('pythonw.exe')
        if candidate.is_file():
            return str(candidate)
    return str(exe)


def autostart_command(home=None, hub=None, interval=None):
    """The exact command registered at login; also what the worker is started with."""
    home = Path(home) if home is not None else Path.home()
    hub = Path(hub) if hub is not None else sr.hub_paths()['root']
    parts = [f'"{python_launcher()}"', f'"{_HERE / "hub_sync.py"}"', '--watch',
             '--home', f'"{home}"', '--hub', f'"{hub}"',
             '--interval', str(interval or DEFAULT_INTERVAL)]
    return ' '.join(parts)


def _winreg():
    import winreg
    return winreg


def autostart_read(name=AUTOSTART_NAME):
    if os.name != 'nt':
        return None
    winreg = _winreg()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY) as key:
            value, _ = winreg.QueryValueEx(key, name)
            return value
    except OSError:
        return None


def autostart_write(command, name=AUTOSTART_NAME):
    winreg = _winreg()
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, command)
    return True


def autostart_delete(name=AUTOSTART_NAME):
    winreg = _winreg()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, name)
            return True
    except FileNotFoundError:
        return False


def start_worker(home=None, hub=None, interval=None, *, spawn=None):
    """Start the hidden watcher now unless a verified worker is already running.

    The identity stored is the process's *actual* creation time read back from the
    OS after the spawn, not the wall-clock instant before it. That is what makes an
    exact comparison possible later: a recycled pid differs by definition, and no
    generous window is needed to absorb the spawn latency.
    """
    pid = worker_pid(hub)
    if pid and verify_worker(pid, hub):
        return {'started': False, 'pid': pid, 'reason': 'already running'}
    if pid:
        # A live pid we cannot vouch for is a stale file, not our worker.
        (state_paths(hub)['pid']).unlink(missing_ok=True)
    clear_stop(hub)
    command = autostart_command(home, hub, interval)
    requested_at = dt.datetime.now(dt.timezone.utc).isoformat()
    state = read_state(hub)
    state['worker'] = {'command': command, 'requested_at': requested_at,
                       'interval_s': int(interval or DEFAULT_INTERVAL)}
    write_state(hub, state)
    if spawn is None:
        flags = 0
        if os.name == 'nt':
            flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) | subprocess.DETACHED_PROCESS
        process = subprocess.Popen(command, shell=False,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, creationflags=flags,
                                   close_fds=True)
    else:
        process = spawn(command)
    pid = getattr(process, 'pid', None)
    identity = {'requested_at': requested_at, 'created_at': None}
    if pid:
        # Read the real creation time back. A short bounded wait covers the moment
        # between Popen returning and the child being queryable.
        deadline = time.monotonic() + WORKER_IDENTITY_WAIT
        while time.monotonic() < deadline:
            actual = process_identity(pid)
            if actual and actual[0]:
                identity['created_at'] = actual[0]
                break
            time.sleep(0.1)
        state['pid'] = pid
        state['worker'] = {**state['worker'], **identity, 'pid': pid}
        write_state(hub, state)
    return {'started': True, 'pid': pid, 'command': command, 'identity': identity,
            'identity_read': bool(identity['created_at'])}


def process_identity(pid):
    """Return (startup_iso, command_hint) for a live pid, or None.

    Used so a stale pid file can never make us kill an unrelated process that
    happened to inherit the pid. Command line is truncated to a boolean hint,
    never stored: it can contain paths.
    """
    if os.name != 'nt':
        try:
            os.kill(pid, 0)
        except OSError:
            return None
        return ('', 'hub_sync')
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            creation, exit_code = wintypes.FILETIME(), wintypes.DWORD()
            kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE,
                                                    ctypes.POINTER(wintypes.DWORD)]
            kernel32.GetProcessTimes.argtypes = [
                wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME)]
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return None
            if exit_code.value != 259:  # STILL_ACTIVE
                return None
            created = wintypes.FILETIME()
            unused = [wintypes.FILETIME() for _ in range(3)]
            if not kernel32.GetProcessTimes(handle, ctypes.byref(created),
                                            ctypes.byref(unused[0]),
                                            ctypes.byref(unused[1]),
                                            ctypes.byref(unused[2])):
                return None
            ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
            return (dt.datetime.fromtimestamp(ticks / 1e7 - 11644473600,
                                              dt.timezone.utc).isoformat(), None)
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None


def worker_record(hub=None):
    """The recorded identity of the worker this hub last started."""
    state = read_state(hub)
    record = state.get('worker')
    return record if isinstance(record, dict) else {}


def verify_worker(pid, hub=None):
    """True only when `pid` is alive AND is the exact process we started.

    The recorded identity is the creation time read back from the OS right after the
    spawn and serialised without losing precision, so compare for equality rather
    than accepting a different process created within a timing window.

    A missing or unreadable recorded creation time is never treated as a match -
    without an identity there is no proof the process is ours.
    """
    if not pid or not pid_alive(pid):
        return False
    record = worker_record(hub)
    expected = record.get('created_at')
    if not expected:
        return False  # no verified identity on record: never assume it is ours
    if record.get('pid') not in (None, int(pid)):
        return False  # the record names a different process
    actual = process_identity(pid)
    if not actual or not actual[0]:
        return False
    try:
        expected_dt = dt.datetime.fromisoformat(expected)
        actual_dt = dt.datetime.fromisoformat(actual[0])
    except (TypeError, ValueError):
        return False
    if expected_dt.tzinfo is None:
        expected_dt = expected_dt.replace(tzinfo=dt.timezone.utc)
    if actual_dt.tzinfo is None:
        actual_dt = actual_dt.replace(tzinfo=dt.timezone.utc)
    return actual_dt == expected_dt


def request_stop(hub=None):
    """Cooperative stop: drop a stop file the worker polls between cycles."""
    paths = state_paths(hub)
    paths['state'].mkdir(parents=True, exist_ok=True)
    target = _own(paths['state'] / 'autosync.stop', hub)
    target.write_text(dt.datetime.now(dt.timezone.utc).isoformat(), encoding='utf-8')
    return target


def stop_requested(hub=None):
    return (state_paths(hub)['state'] / 'autosync.stop').is_file()


def clear_stop(hub=None):
    (state_paths(hub)['state'] / 'autosync.stop').unlink(missing_ok=True)


def stop_worker(hub=None, *, wait=DEFAULT_STOP_WAIT, sleep=None):
    """Stop the worker we started: ask first, kill only a verified pid, then confirm.

    Returns a report that states exactly what happened. `stopped=True` is only
    returned when the process is confirmed gone; a cooperative request that the
    worker honoured reports `signal='cooperative'`, and a forced kill reports
    `signal='forced'`.
    """
    sleep = time.sleep if sleep is None else sleep
    pid = worker_pid(hub)
    if not pid:
        clear_stop(hub)
        return {'stopped': False, 'reason': 'no live worker', 'signal': None, 'pid': None}
    if not verify_worker(pid, hub):
        return {'stopped': False, 'pid': pid, 'signal': None,
                'reason': 'pid is alive but does not match the recorded worker identity; '
                          'refusing to kill it'}
    request_stop(hub)
    deadline = time.monotonic() + max(1, wait)
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            clear_stop(hub)
            return {'stopped': True, 'pid': pid, 'signal': 'cooperative'}
        sleep(0.5)
    # The worker did not honour the request in the window: force only this verified pid.
    if os.name == 'nt':
        subprocess.run([KILL_FILE, '/PID', str(pid), '/F'], capture_output=True, timeout=20)
    else:
        try:
            os.kill(pid, 15)
        except OSError:
            pass
    forced_deadline = time.monotonic() + 10
    while time.monotonic() < forced_deadline:
        if not pid_alive(pid):
            clear_stop(hub)
            return {'stopped': True, 'pid': pid, 'signal': 'forced'}
        sleep(0.5)
    clear_stop(hub)
    return {'stopped': False, 'pid': pid, 'signal': 'forced',
            'reason': 'process still alive after a verified kill request'}


def enable(home=None, hub=None, interval=DEFAULT_INTERVAL, *, spawn=None, register=None):
    """Register at-login autostart and start a worker immediately.

    An existing Run value that is not ours is preserved: it is captured into state
    (``autostart_previous``) before being replaced, so ``disable`` can put it back.
    Overwriting a person's unrelated autostart entry silently would be data loss.
    """
    paths = state_paths(hub)
    paths['state'].mkdir(parents=True, exist_ok=True)
    command = autostart_command(home, hub, interval)
    previous = autostart_read()
    state = read_state(hub)
    preserved = state.get('autostart_previous')
    if previous and previous != command and not preserved:
        preserved = previous
    write = register if register is not None else autostart_write
    write(command)
    started = start_worker(home, hub, interval, spawn=spawn)
    state = read_state(hub)
    state.update({'schema': SCHEMA, 'enabled': True, 'hub': str(paths['root']),
                  'home': str(Path(home) if home is not None else Path.home()),
                  'interval_s': int(interval), 'debounce_s': state.get('debounce_s',
                                                                        DEFAULT_DEBOUNCE),
                  'singleton': state.get('singleton', DEFAULT_SINGLETON),
                  'autostart_command': command,
                  'autostart_previous': preserved})
    if started.get('pid'):
        state['pid'] = started['pid']
    write_state(hub, state)
    append_log(hub, {'event': 'enable', 'interval': int(interval),
                     'worker': started, 'at': dt.datetime.now(dt.timezone.utc).isoformat()})
    return {'schema': SCHEMA, 'ok': True, 'enabled': True, 'autostart': command,
            'autostart_previous_preserved': preserved,
            'worker': started, 'state_file': str(paths['json'])}


def disable(home=None, hub=None, *, unregister=None, restore=None):
    """Stop the worker and remove the at-login registration.

    If a foreign Run value was preserved by ``enable``, it is restored rather than
    dropped, so disabling this daemon cannot delete somebody else's autostart entry.
    """
    state = read_state(hub)
    preserved = state.get('autostart_previous')
    if restore is not None:
        put_back = restore
    elif preserved:
        def put_back():
            autostart_write(preserved)
            return True
    else:
        put_back = None
    delete = unregister if unregister is not None else autostart_delete
    removed = bool(delete())
    restored = bool(put_back()) if put_back is not None else False
    stopped = stop_worker(hub)
    state.update({'schema': SCHEMA, 'enabled': False})
    state.pop('pid', None)
    state.pop('autostart_previous', None)
    paths = state_paths(hub)
    if paths['root'].is_dir():
        write_state(hub, state)
        append_log(hub, {'event': 'disable', 'autostart_removed': removed,
                         'autostart_restored': restored, 'worker': stopped,
                         'at': dt.datetime.now(dt.timezone.utc).isoformat()})
    return {'schema': SCHEMA, 'ok': True, 'enabled': False, 'autostart_removed': removed,
            'autostart_restored': restored, 'worker': stopped}


def watch(home=None, hub=None, interval=DEFAULT_INTERVAL, *, once=False,
          max_cycles=None, sleep=None, singleton=DEFAULT_SINGLETON,
          debounce=DEFAULT_DEBOUNCE):
    """Long-lived worker: one cycle at start, then one per interval.

    Exits quietly (ok, no work) when another worker already holds the singleton,
    which is what makes an at-login start plus a manual start harmless.
    """
    sleep = time.sleep if sleep is None else sleep
    acquired, _handle = acquire_singleton(singleton)
    if not acquired:
        return {'schema': SCHEMA, 'ok': True, 'started': False,
                'reason': 'another autosync worker holds the singleton',
                'singleton': singleton}
    paths = state_paths(hub)
    paths['state'].mkdir(parents=True, exist_ok=True)
    try:
        _own(paths['pid'], hub).write_text(str(os.getpid()), encoding='utf-8')
    except OSError:
        pass
    state = read_state(hub)
    # Login starts --watch directly, without start_worker recording its identity.
    actual = process_identity(os.getpid())
    state['worker'] = {
        'command': autostart_command(home, hub, interval), 'pid': os.getpid(),
        'created_at': actual[0] if actual and actual[0] else None,
        'interval_s': int(interval)}
    state.update({'schema': SCHEMA, 'enabled': True, 'pid': os.getpid(),
                  'interval_s': int(interval), 'debounce_s': int(debounce),
                  'singleton': singleton})
    write_state(hub, state)
    cycles = []
    stop_reason = None
    try:
        index = 0
        while max_cycles is None or index < max_cycles:
            if stop_requested(hub):
                stop_reason = 'stop requested'
                break
            result = cycle(home=home, hub=hub, force=(index == 0), debounce=debounce)
            cycles.append({'mode': result['mode'], 'ok': result['ok'],
                           'duration_ms': result['duration_ms'],
                           'skipped_reason': result['skipped_reason']})
            index += 1
            if once or (max_cycles is not None and index >= max_cycles):
                break
            # Sleep in short slices so a stop request is honoured promptly
            # instead of after a whole interval.
            slept = 0.0
            while slept < interval:
                if stop_requested(hub):
                    break
                step = min(1.0, interval - slept)
                sleep(step)
                slept += step
    finally:
        state = read_state(hub)
        state.pop('pid', None)
        try:
            write_state(hub, state)
            _own(state_paths(hub)['pid'], hub).unlink(missing_ok=True)
        except OSError:
            pass
        release_singleton(singleton)
    return {'schema': SCHEMA, 'ok': all(c['ok'] for c in cycles), 'started': True,
            'pid': os.getpid(), 'cycles': cycles, 'interval_s': int(interval),
            'stop_reason': stop_reason}


# --------------------------------------------------------------------------- CLI

def main(argv=None):
    parser = argparse.ArgumentParser(description='Automatic shared-hub synchronisation.')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--sync', action='store_true', help='run one cycle now')
    group.add_argument('--status', action='store_true', help='report daemon state')
    group.add_argument('--enable', action='store_true',
                       help='register at-login autostart and start a worker')
    group.add_argument('--disable', action='store_true',
                       help='stop the worker and remove autostart')
    group.add_argument('--watch', action='store_true', help='run the long-lived worker')
    parser.add_argument('--home', default=None)
    parser.add_argument('--hub', default=None)
    parser.add_argument('--interval', type=int, default=DEFAULT_INTERVAL)
    parser.add_argument('--debounce', type=int, default=DEFAULT_DEBOUNCE)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--once', action='store_true', help='watch: one cycle then exit')
    parser.add_argument('--force', action='store_true', help='ignore the debounce window')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    if args.interval < 5:
        parser.error('--interval must be at least 5 seconds')
    if args.enable:
        report = enable(args.home, args.hub, args.interval)
    elif args.disable:
        report = disable(args.home, args.hub)
    elif args.status:
        report = status(args.home, args.hub)
    elif args.watch:
        report = watch(args.home, args.hub, args.interval, once=args.once, debounce=args.debounce)
    else:
        report = cycle(args.home, args.hub, args.dry_run, force=True, debounce=args.debounce)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        mode = report.get('mode') or ('enabled' if report.get('enabled') else
                                      'disabled' if 'enabled' in report else '-')
        # Say plainly whether work ran. A skipped cycle is not a success: printing
        # `ok=True` for `busy`/`debounced` read as a completed sync that never happened.
        summary = (f"autosync {mode} ok={report.get('ok')} "
                   f"counts={report.get('counts')} hub={report.get('hub')}")
        if 'completed' in report:
            summary += f" completed={report['completed']}"
            if not report['completed'] and report.get('skipped_reason'):
                summary += f" skipped={report['skipped_reason']}"
        print(summary)
    # Exit 0 only for a run that actually completed successfully; a skipped or failed
    # cycle is non-zero so a caller or scheduler can tell "did nothing" from "did fine".
    if 'completed' in report:
        return 0 if (report.get('ok') and report.get('completed')) else 1
    return 0 if report.get('ok', True) else 1


if __name__ == '__main__':
    raise SystemExit(main())
