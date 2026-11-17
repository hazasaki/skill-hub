"""One central hub for portable agent resources, attached into each detected host.

The hub holds real bytes (skill and prompt trees), never a catalog of them. Copying is
directional: hub -> host root. A host copy carries a `hub-marker.json` naming the hub, the
resource and the content hash that was distributed, which is the only thing that lets a later
run tell "our copy, untouched" (safe to update) from "someone edited it" (conflict, never
overwrite).

Native runtime binaries, plugin code and credentials never enter the hub: a plugin is reported
by name and enablement only, and its files stay where the host installed them.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import tempfile

from portable_paths import portable_path

SCHEMA = 1
DEFAULT_HUB = portable_path(os.environ.get('SKILL_HUB_ROOT') or Path.home() / '.local/share/skill-hub')
MARKER = 'hub-marker.json'
MANIFEST = 'manifest.json'
STATUS = 'state/sync-status.json'
KINDS = ('skills', 'agents', 'prompts')
# Where each kind is distributed, and which suffixes a host can actually read. Host-native
# syntax is not portable: a Codex agent is TOML, a Claude agent is Markdown with frontmatter.
# Copying one into the other host would produce a file that host silently ignores, so a
# format mismatch is reported unsupported instead.
DISTRIBUTION = {
    'skill': (('claude', '.claude/skills', None), ('codex', '.agents/skills', None),
              ('antigravity', '.gemini/config/skills', None),
              ('hermes', '.hermes/skills', None)),
    'agent': (('codex', '.codex/agents', {'.toml'}), ('claude', '.claude/agents', {'.md'})),
    'prompt': (('claude', '.claude/commands', {'.md'}), ('codex', '.codex/prompts', {'.md'})),
}
# Where each kind is read from when ingesting.
INGEST_ROOTS = {
    'skill': (('agents', '.agents/skills'), ('claude', '.claude/skills'),
              ('codex', '.codex/skills'), ('antigravity', '.gemini/config/skills'),
              ('hermes', '.hermes/skills')),
    'agent': (('codex', '.codex/agents'), ('claude', '.claude/agents')),
    'prompt': (('claude', '.claude/commands'), ('codex', '.codex/prompts')),
}
# A resource tree is copied file-by-file only when it is small text. Anything that looks like a
# credential store is refused rather than centralised; report it, do not copy it.
SECRET_NAMES = {'.env', 'credentials', 'credentials.json', '.netrc', 'id_rsa', 'id_ed25519',
                'auth.json', 'token', 'tokens.json', 'secrets.json'}
SECRET_SUFFIXES = {'.pem', '.key', '.pfx', '.p12', '.kdbx'}
# Native executables are not portable resources: the hub carries text and inert assets, never a
# compiled program that a host might later run.
EXECUTABLE_SUFFIXES = {'.exe', '.dll', '.so', '.dylib', '.node', '.bat', '.cmd', '.ps1',
                       '.msi', '.scr', '.com', '.jar'}
MAX_FILE = 1024 * 1024
MAX_FILES = 2000
MAX_TREE = 20 * 1024 * 1024
# Native skill roots, in ownership order. The first source that supplies a slug owns it; a later
# source with a different byte tree is a conflict, not a silent replacement.
NATIVE_ROOTS = (('agents', '.agents/skills'), ('claude', '.claude/skills'),
                ('codex', '.codex/skills'), ('antigravity', '.gemini/config/skills'),
                ('hermes', '.hermes/skills'))


class HubError(RuntimeError):
    """A hub operation was refused before any write."""


def hub_paths(hub=None):
    hub = Path(hub) if hub is not None else DEFAULT_HUB
    return {'root': hub, 'skills': hub / 'skills', 'agents': hub / 'agents',
            'prompts': hub / 'prompts', 'variants': hub / 'variants',
            'state': hub / 'state', 'manifest': hub / MANIFEST}


class UnsafeTree(HubError):
    """A resource tree contains a link that would leave the tree."""


class TooLarge(HubError):
    """A resource tree exceeds the traversal/file/byte budget."""


# Directories that are never part of a portable resource and can be enormous. Pruning them is
# what keeps a bounded scan bounded: a skill vendored next to a repository checkout must not make
# discovery walk that checkout.
PRUNED_DIRS = {'.git', '.hg', '.svn', 'node_modules', '__pycache__', '.venv', 'venv',
               '.mypy_cache', '.pytest_cache', '.ruff_cache', '.tox', '.idea', '.next',
               'dist-packages', 'site-packages'}
MAX_ENTRIES = 5000


def _iter_files(root):
    """Bounded walk of a tree. Refuses links and prunes directories that are never content.

    Budgets are enforced *during* traversal, not after it: hashing an entire tree and only then
    comparing its size made one oversized skill stall the whole discovery pass.
    """
    root = Path(root)
    entries = 0
    count = 0
    total = 0
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            children = sorted(os.scandir(current), key=lambda entry: entry.name)
        except OSError as error:
            raise UnsafeTree(f'unreadable directory: {error.__class__.__name__}') from None
        for child in children:
            entries += 1
            if entries > MAX_ENTRIES:
                raise TooLarge(f'traversal budget exceeded: >{MAX_ENTRIES} entries')
            path = Path(child.path)
            if child.is_symlink():
                raise UnsafeTree('link in tree: ' + path.relative_to(root).as_posix())
            if child.is_dir(follow_symlinks=False):
                if child.name.lower() not in PRUNED_DIRS:
                    stack.append(path)
                continue
            if not child.is_file(follow_symlinks=False) or path.name == MARKER:
                continue
            count += 1
            if count > MAX_FILES:
                raise TooLarge(f'too many files: >{MAX_FILES}')
            size = child.stat(follow_symlinks=False).st_size
            total += size
            if size > MAX_FILE:
                raise TooLarge('oversized file: ' + path.relative_to(root).as_posix())
            if total > MAX_TREE:
                raise TooLarge(f'tree too large: >{MAX_TREE} bytes')
            yield path


def tree_sha(root):
    """Content hash of a resource tree: relative path plus bytes, marker excluded.

    A single bounded pass does the traversal, the size checks and the hashing together, so a
    tree that is too large is rejected during the walk instead of after it.
    """
    root = Path(root)
    digest = hashlib.sha256()
    for path in _iter_files(root):
        digest.update(path.relative_to(root).as_posix().encode('utf-8'))
        digest.update(b'\0')
        digest.update(path.read_bytes())
        digest.update(b'\0')
    return digest.hexdigest()


# Binary payloads that legitimately travel with a portable skill: icons, screenshots, fonts,
# archives. They are copied byte-for-byte and never executed. Anything else that is not UTF-8
# text is refused, because a skill has no reason to ship an opaque binary.
ASSET_SUFFIXES = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.ico', '.bmp',
                  '.woff', '.woff2', '.ttf', '.otf', '.eot',
                  '.mp3', '.mp4', '.webm', '.wav', '.ogg',
                  '.zip', '.gz', '.pdf'}


def _refused_name(path):
    name = path.name.lower()
    return name in SECRET_NAMES or path.suffix.lower() in SECRET_SUFFIXES or \
        path.suffix.lower() in EXECUTABLE_SUFFIXES


def inspect_and_hash(path):
    """Validate a resource and compute its content hash in one pass. Returns (sha|None, problem).

    Both a directory tree and a single file go through this. Previously inspection and hashing
    were separate walks of the same tree, which doubled discovery cost on this machine.
    """
    path = Path(path)
    digest = hashlib.sha256()
    if path.is_dir() and (path / 'SKILL.md').is_file():
        problem = skill_frontmatter_problem(path / 'SKILL.md')
        if problem:
            return None, problem
    if path.is_file():
        if _refused_name(path):
            return None, 'refused file: ' + path.name
        try:
            data = path.read_bytes()
        except OSError as error:
            return None, f'unreadable file: {error.__class__.__name__}'
        if len(data) > MAX_FILE:
            return None, 'oversized file: ' + path.name
        if path.suffix.lower() not in ASSET_SUFFIXES:
            try:
                data.decode('utf-8')
            except UnicodeDecodeError:
                return None, 'non-text file: ' + path.name
        return hashlib.sha256(data).hexdigest(), None
    try:
        for member in _iter_files(path):
            rel = member.relative_to(path).as_posix()
            if _refused_name(member):
                return None, 'refused file: ' + rel
            try:
                data = member.read_bytes()
            except OSError as error:
                return None, f'unreadable file: {error.__class__.__name__}'
            if len(data) > MAX_FILE:
                return None, 'oversized file: ' + rel
            if member.suffix.lower() not in ASSET_SUFFIXES:
                try:
                    data.decode('utf-8')
                except UnicodeDecodeError:
                    return None, 'non-text file: ' + rel
            digest.update(rel.encode('utf-8'))
            digest.update(b'\0')
            digest.update(data)
            digest.update(b'\0')
    except (UnsafeTree, TooLarge) as error:
        return None, str(error)
    return digest.hexdigest(), None


def skill_frontmatter_problem(path):
    """Validate declared YAML; legacy plain Markdown remains portable content."""
    path = Path(path)
    try:
        if path.is_symlink():
            return 'link in tree: SKILL.md'
        if path.stat().st_size > MAX_FILE:
            return 'oversized file: SKILL.md'
        text = path.read_text(encoding='utf-8-sig')
    except (OSError, UnicodeError) as error:
        return f'unreadable SKILL.md: {error.__class__.__name__}'
    lines = text.splitlines()
    if not lines or not lines[0].startswith('---'):
        return None
    if lines[0] != '---':
        return 'invalid SKILL.md frontmatter: opening delimiter must be exactly ---'
    try:
        end = lines.index('---', 1)
    except ValueError:
        return 'invalid SKILL.md frontmatter: missing closing delimiter'
    try:
        import yaml
    except ImportError:
        return 'SKILL.md YAML validation unavailable: PyYAML is required for declared frontmatter'
    try:
        metadata = yaml.safe_load('\n'.join(lines[1:end]))
    except yaml.YAMLError:
        return 'invalid SKILL.md frontmatter: malformed YAML'
    if not isinstance(metadata, dict):
        return 'invalid SKILL.md frontmatter: expected a mapping'
    for key in ('name', 'description'):
        if key in metadata and not isinstance(metadata[key], str):
            return f'invalid SKILL.md frontmatter: {key} must be text'
    return None


def inspect_tree(root):
    """Decide whether a tree may be centralised. Returns (ok, reason). Fails closed.

    Text files are validated as UTF-8; known inert asset types (icons, fonts, media, archives) are
    copied as bytes. Any other non-text file rejects the tree, since a skill has no reason to
    carry an opaque binary.
    """
    root = Path(root)
    if (root / 'SKILL.md').is_file():
        problem = skill_frontmatter_problem(root / 'SKILL.md')
        if problem:
            return False, problem
    try:
        for path in _iter_files(root):
            if _refused_name(path):
                return False, 'refused file: ' + path.relative_to(root).as_posix()
            if path.suffix.lower() in ASSET_SUFFIXES:
                continue
            try:
                data = path.read_bytes()
            except OSError as error:
                return False, f'unreadable file: {error.__class__.__name__}'
            if len(data) > MAX_FILE:
                return False, 'oversized file: ' + path.relative_to(root).as_posix()
            try:
                data.decode('utf-8')
            except UnicodeDecodeError:
                return False, 'non-text file: ' + path.relative_to(root).as_posix()
    except (UnsafeTree, TooLarge) as error:
        return False, str(error)
    return True, None


def inspect_file(path):
    """The single-file counterpart of `inspect_tree`: same credential, size and text rules."""
    path = Path(path)
    if _refused_name(path):
        return False, 'refused file: ' + path.name
    try:
        data = path.read_bytes()
    except OSError as error:
        return False, f'unreadable file: {error.__class__.__name__}'
    if len(data) > MAX_FILE:
        return False, 'oversized file: ' + path.name
    if path.suffix.lower() in ASSET_SUFFIXES:
        return True, None
    try:
        data.decode('utf-8')
    except UnicodeDecodeError:
        return False, 'non-text file: ' + path.name
    return True, None


def inspect_file(path):
    """The single-file counterpart of `inspect_tree`: same credential, size and text rules."""
    path = Path(path)
    if _refused_name(path):
        return False, 'credential-shaped file: ' + path.name
    try:
        data = path.read_bytes()
    except OSError as error:
        return False, f'unreadable file: {error.__class__.__name__}'
    if len(data) > MAX_FILE:
        return False, 'oversized file: ' + path.name
    try:
        data.decode('utf-8')
    except UnicodeDecodeError:
        return False, 'non-text file: ' + path.name
    return True, None


def inspect_any(path):
    path = Path(path)
    return inspect_file(path) if path.is_file() else inspect_tree(path)


def read_json(path):
    path = Path(path)
    if not path.exists():
        return {}
    text = path.read_text(encoding='utf-8-sig')
    if not text.strip():
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise HubError(f'Expected a JSON object in {path}')
    return data


def write_bytes(path, data):
    """Atomic byte-exact replace that keeps an existing file's Windows DACL.

    Resource content must round-trip byte for byte. Text-mode writes translate CRLF to LF on
    Windows, which silently changed every copied file's hash and made each later pass look like
    a local edit.
    """
    path = Path(path)
    before = path.read_bytes() if path.exists() else None
    if before == data:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile('wb', dir=path.parent, delete=False)
    temp = Path(handle.name)
    try:
        with handle:
            handle.write(data)
        if before is not None and os.name == 'nt':
            _copy_dacl(path, temp)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return True


def write_text(path, text):
    return write_bytes(path, text.encode('utf-8'))


def write_json(path, data):
    return write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def _copy_dacl(source, target):
    """Atomic config replacement must keep an existing Windows file's access restrictions."""
    if os.name != 'nt':
        shutil.copymode(source, target)
        return
    import ctypes
    from ctypes import wintypes
    api = ctypes.WinDLL('advapi32', use_last_error=True)
    get = api.GetFileSecurityW
    get.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                   ctypes.POINTER(wintypes.DWORD)]
    get.restype = wintypes.BOOL
    needed = wintypes.DWORD()
    get(str(source), 4, None, 0, ctypes.byref(needed))
    if not needed.value:
        raise ctypes.WinError(ctypes.get_last_error())
    descriptor = ctypes.create_string_buffer(needed.value)
    if not get(str(source), 4, descriptor, needed.value, ctypes.byref(needed)):
        raise ctypes.WinError(ctypes.get_last_error())
    control, revision = wintypes.WORD(), wintypes.DWORD()
    api.GetSecurityDescriptorControl.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.WORD),
                                                ctypes.POINTER(wintypes.DWORD)]
    if not api.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision)):
        raise ctypes.WinError(ctypes.get_last_error())
    set_security = api.SetFileSecurityW
    set_security.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPVOID]
    set_security.restype = wintypes.BOOL
    # Keep the original DACL's protected/inherited state as well as its entries.
    flags = 4 | (0x80000000 if control.value & 0x1000 else 0x20000000)
    if not set_security(str(target), flags, descriptor):
        raise ctypes.WinError(ctypes.get_last_error())


def copy_tree(source, target):
    """Materialise `source` at `target`, removing files no longer present in the source."""
    source, target = Path(source), Path(target)
    wanted = set()
    written = 0
    for path in _iter_files(source):
        rel = path.relative_to(source)
        wanted.add(rel.as_posix())
        if write_bytes(target / rel, path.read_bytes()):
            written += 1
    if target.exists():
        for path in sorted(target.rglob('*'), reverse=True):
            rel = path.relative_to(target).as_posix()
            if rel == MARKER:
                continue
            if path.is_file() and rel not in wanted:
                path.unlink()
            elif path.is_dir() and not any(path.iterdir()):
                path.rmdir()
    return written


def mark(target, *, hub, name, kind, sha, origin=None):
    data = {'schema': SCHEMA, 'hub': str(Path(hub)), 'name': name, 'kind': kind,
            'sha256': sha, 'written_at': dt.datetime.now(dt.timezone.utc).isoformat()}
    if origin is not None:
        data['origin'] = origin
    write_json(Path(target) / MARKER, data)


def marker_of(target):
    path = Path(target) / MARKER
    if not path.is_file():
        return {}
    try:
        return read_json(path)
    except (ValueError, HubError):
        return {'corrupt': True}


def host_roots(home=None, kind='skill'):
    """Detected distribution roots for one kind. Detection is directory presence only."""
    home = Path(home) if home is not None else Path.home()
    local = Path(os.environ.get('LOCALAPPDATA', home / 'AppData/Local'))
    if home.resolve() != Path.home().resolve():
        local = home / 'AppData/Local'
    roots = []
    if kind != 'skill':
        # A host's agent/prompt directory is only a target when the host itself is installed;
        # creating `.claude/agents` on a machine without Claude would be inventing a host.
        for host, rel, suffixes in DISTRIBUTION[kind]:
            host_dir = rel.split('/')[0]
            if (home / host_dir).exists():
                roots.append((host, home / rel, suffixes))
        return roots, []
    if (home / '.claude').is_dir():
        roots.append(('claude', home / '.claude/skills', None))
    if (home / '.codex').is_dir() or (home / '.agents').is_dir():
        roots.append(('codex', home / '.agents/skills', None))
    if (home / '.gemini').is_dir():
        roots.append(('antigravity', home / '.gemini/config/skills', None))
    hermes = home / '.hermes'
    if hermes.is_dir():
        roots.append(('hermes', hermes / 'skills', None))
        for profile in sorted((hermes / 'profiles').glob('*/config.yaml')):
            roots.append(('hermes:' + profile.parent.name, profile.parent / 'skills', None))
    # AGY Multi profiles inherit the Antigravity skill root through native skills.json, so they
    # are reported as attached by inheritance rather than copied again.
    inherit = sorted(p.parent.name for p in (local / 'AgyMulti/profiles').glob('*/gemini')
                     if p.is_dir())
    return roots, inherit


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def resource_sha(path):
    path = Path(path)
    return file_sha(path) if path.is_file() else tree_sha(path)


class _HashCache:
    """Reuse validated hashes only while every file's identity and timestamps match."""

    def __init__(self, path):
        self.path = Path(path)
        try:
            self.entries = read_json(self.path).get('entries', {})
        except (OSError, ValueError):
            self.entries = {}
        if not isinstance(self.entries, dict):
            self.entries = {}
        self.dirty = False

    def hash(self, path):
        path = Path(path)
        signature = hashlib.sha256()
        members = [path] if path.is_file() else _iter_files(path)
        for member in members:
            info = member.stat()
            relative = member.name if member == path else member.relative_to(path).as_posix()
            signature.update(json.dumps([relative, info.st_size, info.st_mtime_ns,
                                         info.st_ctime_ns, info.st_ino, info.st_dev]).encode())
        stamp = signature.hexdigest()
        key = os.path.normcase(os.path.abspath(path))
        previous = self.entries.get(key)
        if isinstance(previous, dict) and previous.get('signature') == stamp:
            value = previous.get('sha256')
            if isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value):
                return value
        value = resource_sha(path)
        self.entries[key] = {'signature': stamp, 'sha256': value}
        self.dirty = True
        return value

    def save(self):
        if self.dirty:
            write_json(self.path, {'schema': 1, 'entries': self.entries})


def _path_identity(path, parents):
    """Resolve shared parent directories once, retaining symlink/junction identity."""
    path = Path(path)
    if path.is_symlink() or path.is_junction():
        return os.path.normcase(str(path.resolve()))
    if path.parent not in parents:
        parents[path.parent] = path.parent.resolve()
    return os.path.normcase(str(parents[path.parent] / path.name))


PINNED = 'state/pinned-skills.json'


def pinned_skills(hub=None):
    """Skill names the host should carry locally rather than fetch from the skill-hub server.

    Opt-in: a hub without the pinned list attaches every skill, which is the historical
    behaviour. Once the list exists it is authoritative, so a host stops paying for the whole
    catalog and retrieval goes through skill_mcp.py instead.
    """
    path = Path(hub or DEFAULT_HUB) / PINNED
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    names = data.get('skills') if isinstance(data, dict) else None
    return {n for n in names if isinstance(n, str)} if isinstance(names, list) else None


def hub_resources(hub=None, hashes=None):
    """Hub content by kind. Skills are directories; agents and prompts are single files."""
    paths = hub_paths(hub)
    pinned = pinned_skills(hub)
    found = []
    for kind in KINDS:
        base = paths[kind]
        if not base.is_dir():
            continue
        if kind == 'skills':
            for entry in sorted(base.iterdir()):
                if entry.is_dir() and (pinned is None or entry.name in pinned):
                    found.append({'name': entry.name, 'kind': 'skill', 'path': entry,
                                  'sha256': hashes.hash(entry) if hashes else tree_sha(entry),
                                  'file': False})
            continue
        for entry in sorted(base.iterdir()):
            if entry.is_file() and entry.suffix in {'.md', '.toml', '.yaml', '.yml', '.txt'}:
                found.append({'name': entry.name, 'kind': kind[:-1], 'path': entry,
                              'sha256': file_sha(entry), 'file': True})
    return found


INDEX = '.hub-index.json'


def read_index(root):
    try:
        return read_json(Path(root) / INDEX).get('files', {})
    except (ValueError, HubError):
        return {}


def write_index(root, entries):
    root = Path(root)
    if not entries:
        (root / INDEX).unlink(missing_ok=True)
        return
    write_json(root / INDEX, {'schema': SCHEMA, 'files': entries})


def _is_category_container(target):
    """True when a path is a directory holding nested skill directories rather than a skill.

    A directory with no `SKILL.md` of its own but whose children have one is a category container
    (Hermes' layout). A directory with unrelated loose files is not — that is a real edit we must
    keep reporting as a conflict.
    """
    target = Path(target)
    if not target.is_dir() or (target / 'SKILL.md').is_file():
        return False
    try:
        return any(child.is_dir() and (child / 'SKILL.md').is_file()
                   for child in target.iterdir())
    except OSError:
        return False


def _plan_file(resource, target, index):
    """Classify one hub file copied as a single file (agent or prompt)."""
    sha, name = resource['sha256'], resource['name']
    record = index.get(name)
    if not target.exists():
        return 'create', None
    if not target.is_file():
        return 'conflict', 'target exists and is not a file'
    current = file_sha(target)
    if record:
        if record.get('disabled'):
            return 'skipped_disabled', 'host turned this resource off'
        if current == sha:
            return 'unchanged', None
        if current == record.get('sha256'):
            return ('unchanged' if current == sha else 'update'), None
        return 'conflict', 'local edits since the hub copy'
    return ('adopted', None) if current == sha else (
        'conflict', 'pre-existing file without a hub record')


def _plan_one(resource, target, hashes=None, metadata=None):
    """Classify one hub resource copied as a directory (a skill)."""
    sha = resource['sha256']
    if not target.exists():
        return 'create', None
    if not target.is_dir():
        return 'conflict', 'target exists and is not a directory'
    meta = marker_of(target) if metadata is None else metadata
    if meta.get('corrupt'):
        return 'conflict', 'unreadable hub marker'
    if meta.get('disabled'):
        return 'skipped_disabled', 'host turned this resource off'
    if meta.get('name') and meta.get('name') != resource['name']:
        return 'conflict', 'marker names a different resource'
    current = hashes.hash(target) if hashes is not None else tree_sha(target)
    if meta.get('sha256'):
        if current == sha:
            return 'unchanged', None
        if current == meta['sha256']:
            return ('unchanged' if meta['sha256'] == sha else 'update'), None
        return 'conflict', 'local edits since the hub copy'
    # No marker: adopt only an exact byte-for-byte match, never overwrite a stranger.
    if current == sha:
        return 'adopted', None
    return 'conflict', 'pre-existing resource without a hub marker'


def sync(home=None, hub=None, dry_run=False):
    """Attach every hub resource to every detected host root. Idempotent and reversible.

    Returns structured status only; it raises `HubError` for a refused hub (credential-shaped
    content), and reports per-resource conflicts instead of overwriting them.
    """
    home = Path(home) if home is not None else Path.home()
    paths = hub_paths(hub)
    skill_roots, inherit = host_roots(home, 'skill')
    report = {'schema': SCHEMA, 'ok': True, 'dry_run': bool(dry_run),
              'hub': str(paths['root']), 'home': str(home),
              'generated_at': dt.datetime.now(dt.timezone.utc).isoformat(),
              'resources': [], 'hosts': {}, 'counts': {k: 0 for k in
              ('create', 'update', 'unchanged', 'adopted', 'preserved_variant', 'conflict',
               'skipped_disabled', 'unsupported')},
              'host_counts': {},
              'conflicts': [], 'unsupported': [], 'warnings': [], 'errors': [],
              'manifest': str(paths['manifest'])}
    for host, root, _suffixes in skill_roots:
        report['hosts'][host] = {'attached': True, 'root': str(root), 'reason': None}
    if inherit:
        report['hosts']['antigravity:agy-multi'] = {
            'attached': bool(skill_roots), 'root': None,
            'reason': ('inherits the Antigravity skills manifest' if skill_roots
                       else 'no antigravity root')}
    hashes = _HashCache(paths['state'] / 'hash-cache.json')
    resources = hub_resources(hub, hashes)
    owners = load_provenance(hub)
    if not resources:
        report['errors'].append('hub holds no resources; ingest sources before attaching')
        report['ok'] = False
    file_indexes = {}
    roots_by_kind = {'skill': skill_roots}
    variant_maps = {}
    resolved_parents = {}
    manifest_resources = {}
    for resource in resources:
        entry = {'name': resource['name'], 'kind': resource['kind'],
                 'sha256': resource['sha256'], 'action': 'unchanged', 'hosts': [],
                 'detail': None, 'per_host': {}}
        key = f"{resource['kind']}/{resource['name']}"
        owner = owners.get(key) or {}
        origin, origin_path, recorded = (owner.get('origin'), owner.get('source'),
                                         owner.get('sha256'))
        nesting = (owner.get('nesting') or '').strip('/')
        # Each kind is inspected by its own rules; a directory tree needs the link/secret walk,
        # a single prompt or agent file only needs the name and size checks.
        if resource['file']:
            allowed, reason = (False, 'credential-shaped file: ' + resource['name']) if \
                _refused_name(resource['path']) else inspect_file(resource['path'])
        else:
            allowed, reason = inspect_tree(resource['path'])
        if not allowed:
            entry['action'] = 'unsupported'
            entry['detail'] = reason
            report['unsupported'].append({'name': resource['name'], 'kind': resource['kind'],
                                          'reason': reason, 'scope': 'resource'})
            report['counts']['unsupported'] += 1
            report['resources'].append(entry)
            continue
        manifest_resources[key] = {'kind': resource['kind'], 'sha256': resource['sha256']}
        # The origin is never written to, but it is not automatically healthy either. A source
        # edit on its own is the normal workflow (edit, then ingest picks it up). Both sides
        # drifting from what the hub recorded means two independent edits; that must not be
        # reported as healthy.
        origin_sha, hub_drifted = None, bool(recorded) and resource['sha256'] != recorded
        if origin_path and Path(origin_path).exists():
            origin_sha = inspect_and_hash(origin_path)[0]
        if recorded and origin_sha is not None and origin_sha != recorded and hub_drifted:
            detail = 'origin and hub edited independently'
            report['conflicts'].append({'name': resource['name'], 'host': origin,
                                        'path': str(origin_path), 'reason': detail})
            entry['action'] = 'conflict'
            entry['detail'] = detail
            report['counts']['conflict'] += 1
            report['resources'].append(entry)
            continue
        origin_identity = _path_identity(origin_path, resolved_parents) if origin_path else None
        if resource['kind'] not in roots_by_kind:
            roots_by_kind[resource['kind']] = host_roots(home, resource['kind'])[0]
        roots = roots_by_kind[resource['kind']]
        # A slug may legitimately exist natively on several hosts with different content. Ingest
        # archived the losers under `variants/`; those exact bytes must be recognised here as a
        # preserved native alternative, not as an unresolved conflict, or onboarding fails on a
        # difference nobody needs to resolve. Recognised by content hash, so a genuine edit is
        # still a conflict.
        if resource['kind'] not in variant_maps:
            variant_maps[resource['kind']] = _variant_hash_map(paths, resource['kind'])
        known_variants = variant_maps[resource['kind']].get(resource['name'], set())
        actions = {}
        target_markers = {}
        for host, root, suffixes in roots:
            if suffixes and resource['path'].suffix not in suffixes:
                # Host-native syntax: a Codex TOML agent cannot be installed into a Markdown
                # agent directory. Report it instead of writing a file the host ignores.
                actions[host] = ('unsupported', 'host does not read this format', None)
                continue
            target = root / resource['name']
            # A host whose root is grouped by category keeps its own layout: writing the flat
            # name would collide with the category directory that already exists there.
            if nesting and host == origin:
                target = root / nesting / resource['name']
            # The directory a resource was ingested from is its origin, not a distribution
            # target: writing a marker back into a person's own tree would be a surprise.
            if origin_identity and target.exists() and \
                    _path_identity(target, resolved_parents) == origin_identity:
                actions[host] = ('unchanged', 'origin source', target)
                continue
            if resource['file']:
                if root not in file_indexes:
                    file_indexes[root] = read_index(root)
                index = file_indexes[root]
                action, detail = _plan_file(resource, target, index)
            else:
                metadata = marker_of(target)
                target_markers[target] = metadata
                action, detail = _plan_one(resource, target, hashes, metadata)
            if action == 'conflict' and _is_category_container(target) and not target_markers.get(target):
                # The name is already a container of skills in this host (Hermes groups skills by
                # category). Writing a skill here would destroy unrelated content, so this host
                # simply cannot receive that slug. Reported, never overwritten.
                action, detail = 'unsupported', 'name already used by a category directory'
            if action == 'conflict' and not target_markers.get(target) and \
                    (target / 'plugin.json').is_file() and not (target / 'SKILL.md').exists():
                action, detail = 'unsupported', 'name reserved by a native plugin directory'
            if action == 'conflict' and target.exists() and known_variants:
                # Exact known native alternative: keep it, report it, never overwrite it.
                try:
                    if hashes.hash(target) in known_variants:
                        action, detail = 'preserved_variant', 'native alternative kept'
                except (UnsafeTree, TooLarge, OSError):
                    pass
            actions[host] = (action, detail, target)
            entry['hosts'].append(host)
            if action == 'conflict':
                report['conflicts'].append({'name': resource['name'], 'host': host,
                                            'path': str(target), 'reason': detail})
        # One resource may be fine on one host and conflicted on another; never let a single
        # host's local edit block the healthy ones.
        if not dry_run:
            for host, (action, detail, target) in actions.items():
                if action == 'unchanged' and detail != 'origin source' and target is not None:
                    if resource['file']:
                        root = next(r for h, r, _ in roots if h == host)
                        if root not in file_indexes:
                            file_indexes[root] = read_index(root)
                        index = file_indexes[root]
                        if index.get(resource['name'], {}).get('sha256') != resource['sha256']:
                            index[resource['name']] = {'sha256': resource['sha256'],
                                                      'kind': resource['kind'], 'origin': origin}
                    elif target_markers[target].get('sha256') != resource['sha256']:
                        mark(target, hub=paths['root'], name=resource['name'],
                             kind=resource['kind'], sha=resource['sha256'], origin=origin)
                if action not in ('create', 'update', 'adopted'):
                    continue
                if resource['file']:
                    root = next(r for h, r, _ in roots if h == host)
                    if root not in file_indexes:
                        file_indexes[root] = read_index(root)
                    index = file_indexes[root]
                    write_bytes(target, resource['path'].read_bytes())
                    index[resource['name']] = {'sha256': resource['sha256'], 'kind':
                                               resource['kind'], 'origin': origin}
                else:
                    copy_tree(resource['path'], target)
                    mark(target, hub=paths['root'], name=resource['name'],
                         kind=resource['kind'], sha=resource['sha256'], origin=origin)
        for host, (action, detail, target) in actions.items():
            counts = report['host_counts'].setdefault(host, {})
            counts[action] = counts.get(action, 0) + 1
            if action == 'unsupported':
                report['unsupported'].append({'name': resource['name'], 'kind': resource['kind'],
                                              'host': host, 'path': str(target) if target else None,
                                              'reason': detail, 'scope': 'host'})
            elif action == 'preserved_variant':
                report['warnings'].append({'name': resource['name'], 'kind': resource['kind'],
                                           'host': host, 'path': str(target),
                                           'action': 'preserved_variant',
                                           'reason': 'host uses an archived alternative to canonical content'})
        supported_actions = {v[0] for v in actions.values()} - {'unsupported'}
        worst = next((a for a in ('conflict', 'skipped_disabled',
                                  'preserved_variant', 'update', 'create', 'adopted')
                      if a in supported_actions),
                     'unchanged' if supported_actions or not actions else 'unsupported')
        entry['action'] = worst
        entry['detail'] = next((d for a, d, _ in actions.values() if a == worst and d), None)
        entry['per_host'] = {host: action for host, (action, _, _) in actions.items()}
        report['counts'][worst] += 1
        report['resources'].append(entry)
    if not dry_run:
        for root, index in file_indexes.items():
            write_index(root, index)
        hashes.save()
    if not report['hosts']:
        report['errors'].append('no supported host detected')
        report['ok'] = False
    report['ok'] = report['ok'] and not report['conflicts']
    if not dry_run and paths['root'].is_dir():
        paths['state'].mkdir(parents=True, exist_ok=True)
        write_json(paths['root'] / STATUS, report)
        # The manifest is the stable managed state; it carries no timestamp so a repeat pass
        # over unchanged content leaves it byte-identical.
        write_json(paths['manifest'], {
            'schema': 2,
            'resources': manifest_resources})
    return report


def _latest(paths):
    """Pick the newest version directory by natural ordering of its name."""
    def key(path):
        return [int(part) if part.isdigit() else part for part in
                path.name.replace('-', '.').split('.')]
    candidates = [p for p in paths if p.is_dir()]
    if not candidates:
        return None
    try:
        return sorted(candidates, key=key)[-1]
    except TypeError:
        return sorted(candidates, key=lambda p: p.name)[-1]


def claude_plugin_sources(home):
    """Skill dirs of Claude plugins the user enabled, resolved through the native registry."""
    home = Path(home)
    settings = home / '.claude/settings.json'
    registry = home / '.claude/plugins/installed_plugins.json'
    try:
        enabled = {name for name, flag in read_json(settings).get('enabledPlugins', {}).items()
                   if flag is True}
        installed = read_json(registry).get('plugins', {})
    except (ValueError, HubError):
        return [], [{'origin': 'claude-plugins', 'reason': 'unreadable plugin settings'}]
    found, problems = [], []
    for name in sorted(enabled):
        records = installed.get(name) or []
        record = records[-1] if isinstance(records, list) and records else {}
        install = record.get('installPath')
        if not install:
            problems.append({'origin': name, 'reason': 'enabled but no install path recorded'})
            continue
        skills = Path(install) / 'skills'
        if not skills.is_dir():
            problems.append({'origin': name, 'reason': 'enabled plugin ships no skills directory'})
            continue
        found.append(('plugin:claude:' + name, skills))
    return found, problems


def codex_plugin_roots(home=None):
    """Enabled Codex cache roots, including plugins that only declare hooks.

    The cache has no active-version registry here: retain its natural version ordering,
    including the native ``latest`` alias, rather than inferring activity from mtime.
    """
    home = Path(home) if home is not None else Path.home()
    config = home / '.codex/config.toml'
    cache = home / '.codex/plugins/cache'
    if not config.is_file():
        return [], []
    try:
        import tomllib
        data = tomllib.loads(config.read_text(encoding='utf-8-sig'))
    except (ValueError, OSError):
        return [], [{'origin': 'codex-plugins', 'reason': 'unreadable config.toml'}]
    found, problems = [], []
    plugins = data.get('plugins', {})
    if not isinstance(plugins, dict):
        return [], [{'origin': 'codex-plugins', 'reason': 'plugins must be a table'}]
    for key, value in sorted(plugins.items()):
        if not isinstance(value, dict) or ('enabled' in value and
                                         not isinstance(value['enabled'], bool)):
            problems.append({'origin': key, 'reason': 'invalid plugin enablement table'})
            continue
        if value.get('enabled') is not True:
            continue
        name, _, marketplace = str(key).partition('@')
        if not all(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', part)
                   and part not in ('.', '..') for part in (name, marketplace)):
            problems.append({'origin': key, 'reason': 'invalid plugin name or marketplace'})
            continue
        plugin_dir = cache / marketplace / name
        if not plugin_dir.is_dir():
            problems.append({'origin': key, 'reason': 'enabled but not present in the cache'})
            continue
        try:
            plugin_dir.resolve().relative_to(cache.resolve())
            if (plugin_dir / '.codex-plugin/plugin.json').is_file() or \
                    (plugin_dir / 'skills').is_dir():
                root = plugin_dir
            else:
                root = _latest(p for p in plugin_dir.iterdir() if p.is_dir() and
                               ((p / '.codex-plugin/plugin.json').is_file() or
                                (p / 'skills').is_dir()))
            if root is None:
                problems.append({'origin': key, 'reason': 'cache has no plugin manifest or skills'})
                continue
            root.resolve().relative_to(plugin_dir.resolve())
            manifest = root / '.codex-plugin/plugin.json'
            if manifest.is_file():
                manifest.resolve().relative_to(root.resolve())
                if manifest.stat().st_size > MAX_FILE:
                    raise HubError('plugin manifest exceeds file budget')
                read_json(manifest)
        except (ValueError, OSError, HubError) as error:
            problems.append({'origin': key, 'reason':
                             'invalid or unreadable plugin cache: ' + error.__class__.__name__})
            continue
        found.append(('plugin:codex:' + str(key), root))
    return found, problems


def codex_plugin_sources(home):
    """Skill dirs of enabled Codex roots; hook-only roots remain discoverable separately."""
    roots, problems = codex_plugin_roots(home)
    found = []
    for origin, root in roots:
        skills = root / 'skills'
        if not skills.is_dir():
            problems.append({'origin': origin, 'reason': 'enabled plugin ships no skills directory'})
            continue
        found.append((origin, skills))
    return found, problems


def enabled_plugins(home):
    """Every enabled-plugin skill source across supported hosts, plus discovery problems."""
    found, problems = [], []
    for resolver in (codex_plugin_sources, claude_plugin_sources):
        host_found, host_problems = resolver(home)
        found.extend(host_found)
        problems.extend(host_problems)
    return found, problems


def discovery_sources(home=None):
    """Every supported portable skill source, in ownership order.

    Native host roots come first so a person's own edit outranks a plugin's copy of the same
    slug. Plugin sources are read as content only: their native runtime is never touched, and a
    plugin the user disabled is never read at all.
    """
    home = Path(home) if home is not None else Path.home()
    sources, skipped = [], []
    for origin, rel in INGEST_ROOTS['skill']:
        path = home / rel
        if path.is_dir():
            sources.append(('skill', origin, path, None))
    # Hermes profile skill roots are distribution targets, so they must also be ingest sources.
    # Without this, a profile that already held its own copy of a slug would show up as an
    # unmarked conflict on every attach, and its real content would never be archived.
    hermes = home / '.hermes'
    if hermes.is_dir():
        for profile in sorted((hermes / 'profiles').glob('*/config.yaml')):
            skills = profile.parent / 'skills'
            if skills.is_dir():
                sources.append(('skill', 'hermes:' + profile.parent.name, skills, None))
    plugins, problems = enabled_plugins(home)
    sources.extend(('skill', origin, path, None) for origin, path in plugins)
    skipped.extend(problems)
    # Agent and prompt files are host-native syntax; each is imported so it can be owned and
    # reported, even where only one host reads that format.
    for kind in ('agent', 'prompt'):
        for origin, rel in INGEST_ROOTS[kind]:
            path = home / rel
            if path.is_dir():
                sources.append((kind, origin, path,
                                {'.md'} if kind == 'prompt' else {'.md', '.toml'}))
    return sources, skipped


def load_provenance(hub=None):
    """Persistent owner record, keyed `<kind>/<name>`: which origin supplied it, at what hash."""
    try:
        data = read_json(hub_paths(hub)['root'] / 'provenance.json')
    except (ValueError, HubError):
        return {}
    return data.get('resources') or data.get('slugs') or {}


def _is_managed_replica(entry, hub):
    """A host copy this hub itself distributed must never be re-ingested as an independent source.

    Without this, a skill propagated to `.claude/skills` would appear earlier in discovery order
    than its true owner and quietly take ownership of the slug from the hub's own copy.
    """
    meta = marker_of(entry)
    if not meta or meta.get('corrupt'):
        return False
    recorded = str(meta.get('hub', ''))
    if recorded and os.path.normcase(os.path.abspath(recorded)) == \
            os.path.normcase(os.path.abspath(hub)):
        return True
    try:
        return Path(recorded).resolve() == Path(hub).resolve()
    except OSError:
        return False


def _ingest_candidates(kind, root, suffixes):
    """The directories (skills) or files (agents, prompts) a source root actually offers.

    A skill must have a `SKILL.md`: a bare directory in a skills root is usually a container or a
    stray folder, and treating one as a skill was how `.system` (a container that holds nested
    skills, not a skill) entered the hub. Dot-directories are never resources.
    """
    root = Path(root)
    if kind == 'skill':
        candidates = []
        for entry in sorted(root.iterdir()):
            if not entry.is_dir() or entry.name.startswith('.') or entry.is_symlink():
                continue
            if (entry / 'SKILL.md').is_file():
                candidates.append(entry)
            else:
                # A container of nested skills is descended one level. Dot-directories are
                # skipped whole: `.system` is a runtime container, not a skill, and neither it
                # nor its contents belong in the portable set.
                candidates.extend(
                    nested for nested in sorted(entry.iterdir())
                    if nested.is_dir() and not nested.name.startswith('.')
                    and not nested.is_symlink() and (nested / 'SKILL.md').is_file())
        return candidates
    return [p for p in sorted(root.iterdir())
            if p.is_file() and not p.name.startswith('.')
            and (suffixes is None or p.suffix in suffixes)]


def _resource_signature(path):
    """Content hash and budget verdict in one bounded pass (the budget is enforced inside it)."""
    path = Path(path)
    try:
        return (tree_sha(path) if path.is_dir() else file_sha(path)), None
    except (UnsafeTree, TooLarge) as error:
        return None, str(error)


def _nesting(entry, root):
    """Relative parent of a resource inside its source root, '' when it sits at the top.

    Some skill roots are grouped by category (Hermes keeps `<root>/<category>/<skill>`). The hub
    stores every skill flat, but a host copy has to be written back at the layout that host
    reads, or the category directory is overwritten by a skill of the same name.
    """
    parent = Path(entry).parent
    try:
        rel = parent.relative_to(Path(root)).as_posix()
    except ValueError:
        return ''
    return '' if rel == '.' else rel


def ingest(home=None, hub=None, dry_run=False, sources=None):
    """Centralise every supported resource source into the hub. One command, no arguments.

    Discovery is bounded and conservative: every supported native root, plus the skill
    directories of enabled plugins, is read and copied if it passes the credential/size/text
    checks. Support scripts inside a skill are inert files and are copied unchanged; nothing is
    executed during discovery.

    Ownership is sticky: the origin recorded in `provenance.json` keeps a resource until that
    origin is gone, so a later copy of the same slug (another host root, or a plugin) is a
    conflict rather than a silent takeover, and a hub copy someone edited locally is also a
    conflict.
    """
    home = Path(home) if home is not None else Path.home()
    paths = hub_paths(hub)
    discovered, discovery_skipped = discovery_sources(home)
    if sources is not None:
        discovered = [('skill', str(Path(s).name), Path(s), None) for s in sources]
    # A versioned cache can retain the old source. Only the currently resolved Codex root
    # may advance that plugin's identity; an origin label alone cannot authorise takeover.
    active_codex = {origin: os.path.normcase(os.path.abspath(root / 'skills'))
                    for origin, root in codex_plugin_roots(home)[0]} if sources is None else {}
    owners = load_provenance(hub)
    report = {'schema': SCHEMA, 'ok': True, 'dry_run': bool(dry_run), 'hub': str(paths['root']),
              'home': str(home), 'sources': [], 'imported': [], 'updated': [], 'adopted': [],
              'unchanged': [], 'skipped': [], 'unsupported': [], 'variants': [],
              'conflicts': []}
    report['skipped'].extend(discovery_skipped)
    for kind, origin, root, suffixes in discovered:
        if not Path(root).is_dir():
            report['skipped'].append({'name': None, 'source': origin, 'reason': 'not found'})
            continue
        report['sources'].append({'origin': origin, 'path': str(root), 'kind': kind})
        for entry in _ingest_candidates(kind == 'skill' and 'skill' or kind, root, suffixes):
            name = entry.name
            key = f'{kind}/{name}'
            hub_dir = paths['skills' if kind == 'skill' else kind + 's']
            target = hub_dir / name
            # A tree that declares a foreign repository is project-managed (game-studios); the
            # hub must not become a second owner of it.
            origin_file = entry / 'source.json' if entry.is_dir() else None
            if origin_file is not None and origin_file.is_file():
                try:
                    foreign = read_json(origin_file).get('repository')
                except (ValueError, HubError):
                    foreign = True
                if foreign:
                    report['skipped'].append({'name': name, 'source': origin,
                                              'reason': 'project-managed source.json'})
                    continue
            if entry.is_dir() and _is_managed_replica(entry, paths['root']):
                report['skipped'].append({'name': name, 'source': origin,
                                          'reason': 'managed replica distributed by this hub'})
                continue
            # One bounded pass both validates the content and hashes it.
            sha, problem = inspect_and_hash(entry)
            if problem is not None or sha is None:
                # An unsafe, non-text or oversized tree cannot be centralised: report, never copy.
                report['unsupported'].append({'name': name, 'source': origin,
                                              'reason': problem or 'no content hash'})
                continue
            owner = owners.get(key)
            hub_sha = (resource_sha(target)) if target.exists() else None
            if owner is None:
                if target.exists() and hub_sha != sha:
                    # A hub copy with no owner record cannot be assumed to be ours.
                    report['conflicts'].append({'name': name, 'reason':
                                                'hub copy present without a provenance record',
                                                'kept': 'hub', 'rejected': origin})
                    continue
                action = 'adopted' if target.exists() else 'imported'
                if not dry_run and action == 'imported':
                    _materialise(entry, target, kind)
                owners[key] = {'kind': kind, 'origin': origin, 'source': str(entry),
                               'sha256': sha, 'nesting': _nesting(entry, root)}
                report[action].append(name)
                continue
            same_source = owner.get('source') and os.path.normcase(os.path.abspath(
                owner['source'])) == os.path.normcase(os.path.abspath(entry))
            # Migrate legacy plugin labels only at their recorded source, never by cache order.
            if same_source and str(owner.get('origin', '')).startswith('plugin:') and \
                    origin.startswith('plugin:'):
                owner['origin'] = origin
            same_owner = owner.get('origin') == origin and (same_source or
                (kind == 'skill' and active_codex.get(origin) ==
                 os.path.normcase(os.path.abspath(root))) or
                (origin.startswith(('plugin:claude:', 'plugin:codex:')) and
                 not Path(owner.get('source', '')).exists()))
            if not same_owner:
                if owner.get('sha256') == sha:
                    report['unchanged'].append(name)
                else:
                    # Two hosts hold different versions of one slug. Neither is wrong: keep the
                    # first owner canonical and archive the other under its own source-qualified
                    # namespace. Dropping it would lose work; overwriting would be a silent
                    # takeover; calling it a conflict would make autosync retry forever.
                    fresh = _known_variant(paths, kind, origin, name, sha) is None
                    variant = (_archive_variant(paths, kind, origin, name, entry)
                               if not dry_run else buffered_variant_path(paths, kind, origin, name))
                    if variant is not None:
                        if fresh:
                            report['variants'].append({'name': name, 'kind': kind,
                                                       'canonical': owner.get('origin'),
                                                       'variant': origin, 'sha256': sha,
                                                       'path': str(variant)})
                    else:
                        report['conflicts'].append({
                            'name': name, 'kind': kind,
                            'reason': 'variant could not be archived',
                            'kept': owner.get('origin'), 'rejected': origin})
                continue
            # The recorded owner: the hub copy may legitimately be refreshed, unless a person
            # edited it, which the recorded hash detects.
            if hub_sha is not None and hub_sha != owner.get('sha256'):
                report['conflicts'].append({'name': name, 'reason': 'hub copy edited since ingest',
                                            'kept': 'hub', 'rejected': origin})
                continue
            if sha == owner.get('sha256') and hub_sha == sha:
                if not same_source:
                    owner.update(source=str(entry), nesting=_nesting(entry, root))
                report['unchanged'].append(name)
                continue
            if not dry_run:
                _materialise(entry, target, kind)
            owners[key] = {'kind': kind, 'origin': origin, 'source': str(entry), 'sha256': sha,
                           'nesting': _nesting(entry, root)}
            report['updated'].append(name)
    if not dry_run:
        paths['root'].mkdir(parents=True, exist_ok=True)
        # Deterministic content: no timestamp, so a repeat pass is byte-identical and an
        # idempotence check can compare the file itself.
        write_json(paths['root'] / 'provenance.json',
                   {'schema': SCHEMA, 'resources': {key: owners[key] for key in sorted(owners)}})
    report['counts'] = {key: len(report[key]) for key in
                        ('imported', 'updated', 'adopted', 'unchanged', 'skipped', 'unsupported',
                         'variants', 'conflicts')}
    # Benign cross-host version differences are archived, not failures: onboarding completes and
    # autosync stops retrying them. Only real conflicts keep the run not-ok.
    report['ok'] = not report['conflicts']
    return report


def _archive_variant(paths, kind, origin, name, entry):
    """Keep a divergent copy of an owned resource under a source-qualified namespace.

    Returns the archived path, or None when nothing could be stored. A variant is evidence, not a
    distribution target: it never reaches a host root, so a benign cross-host version difference
    cannot become a permanent retry loop.
    """
    safe = re.sub(r'[^A-Za-z0-9._-]+', '_', origin) or 'source'
    base = paths['variants'] / (kind + 's') / safe
    try:
        sha = resource_sha(entry)
        known = _known_variant(paths, kind, origin, name, sha)
        if known is not None:
            return known
        target = base / name
        index = _variant_index(base, name, sha, origin)
        record = index['variants'].setdefault(name, {'sha256': sha, 'origin': origin})
        versions = record.setdefault('versions', {})
        if target.exists():
            previous_sha = resource_sha(target)
            versions.setdefault(previous_sha, name)
            if previous_sha != sha:
                target = base / '.versions' / name / sha
        if kind == 'skill':
            copy_tree(entry, target)
        else:
            write_bytes(target, Path(entry).read_bytes())
        versions[sha] = target.relative_to(base).as_posix()
        write_json(base / '_variants.json', index)
        return target
    except (OSError, HubError, UnsafeTree, TooLarge):
        return None


def _variant_hash_map(paths, kind):
    """Every archived alternative for every slug, in one read per source namespace.

    Built once per sync. Reading each source's `_variants.json` per resource made sync
    quadratic: with ~2000 resources and a multi-thousand-entry index that is tens of thousands of
    JSON parses, which is why a dry-run preview stalled.
    """
    base = paths['variants'] / (kind + 's')
    mapping = {}
    if not base.is_dir():
        return mapping
    for source_dir in sorted(base.iterdir()):
        if not source_dir.is_dir():
            continue
        try:
            index = read_json(source_dir / '_variants.json').get('variants', {})
        except (ValueError, HubError):
            index = {}
        for name, record in index.items():
            if isinstance(record, dict) and record.get('sha256'):
                mapping.setdefault(name, set()).add(record['sha256'])
                mapping[name].update(record.get('versions', {}))
    return mapping


def buffered_variant_path(paths, kind, origin, name):
    """Where a variant *would* be archived. Used by a dry run, which must not write."""
    safe = re.sub(r'[^A-Za-z0-9._-]+', '_', origin) or 'source'
    return paths['variants'] / (kind + 's') / safe / name


def _known_variant(paths, kind, origin, name, sha):
    """Path recorded for this exact variant content, if it was already archived."""
    safe = re.sub(r'[^A-Za-z0-9._-]+', '_', origin) or 'source'
    index = paths['variants'] / (kind + 's') / safe / '_variants.json'
    entry = {}
    try:
        entry = read_json(index).get('variants', {}).get(name, {})
    except (ValueError, HubError):
        return None
    relative = entry.get('versions', {}).get(sha)
    target = index.parent / relative if relative else index.parent / name
    if relative or entry.get('sha256') == sha:
        # The index is evidence only; an absent archive must be rebuilt.
        if target.exists() and resource_sha(target) == sha:
            return target
    return None


def _variant_index(base, name, sha, origin):
    """Keep all archived hashes, including legacy source/name layouts."""
    try:
        entries = read_json(base / '_variants.json').get('variants', {})
    except (ValueError, HubError):
        entries = {}
    # Only this resource changes; rehashing every archived skill on each append is quadratic.
    entries.setdefault(name, {'sha256': sha, 'origin': origin})
    return {'schema': SCHEMA, 'origin': origin, 'variants': entries}


def _materialise(entry, target, kind):
    if kind == 'skill':
        copy_tree(entry, target)
    else:
        write_bytes(target, Path(entry).read_bytes())


# Kept for callers that used the first revision of this module; `ingest` is the supported path.
def seed(home=None, hub=None, source=None, dry_run=False):
    return ingest(home=home, hub=hub, dry_run=dry_run,
                  sources=[source] if source is not None else None)


def bootstrap(home=None, hub=None, dry_run=False):
    """The one onboarding command: ingest every source, then attach the hub to every host."""
    ingested = ingest(home=home, hub=hub, dry_run=dry_run)
    attached = sync(home=home, hub=hub, dry_run=dry_run)
    return {'schema': SCHEMA, 'dry_run': bool(dry_run), 'ok': ingested['ok'] and attached['ok'],
            'ingest': ingested, 'sync': attached}


def status(home=None, hub=None):
    """Report the hub and every host attachment without copying anything."""
    home = Path(home) if home is not None else Path.home()
    return sync(home=home, hub=hub, dry_run=True)


def prune(home=None, hub=None, dry_run=False):
    """Remove hub-managed skill copies that the pinned list no longer names.

    Only a directory carrying this hub's marker is removable: a tree without one is someone's
    own content, and a tree whose hash disagrees with its marker was edited locally. Both are
    reported and left untouched.
    """
    home = Path(home) if home is not None else Path.home()
    paths = hub_paths(hub)
    pinned = pinned_skills(hub)
    if pinned is None:
        # No pinned list: sync attaches every skill, so nothing is surplus. Refuse rather than
        # delete trees whose ownership the caller never narrowed.
        report = {'schema': SCHEMA, 'ok': True, 'dry_run': bool(dry_run), 'removed': [],
                  'kept_local': [], 'kept_unmanaged': [], 'errors': [],
                  'skipped': 'no pinned-skills.json; sync attaches every skill'}
        return report
    skill_roots, _inherit = host_roots(home, 'skill')
    hashes = _HashCache(paths['state'] / 'hash-cache.json')
    report = {'schema': SCHEMA, 'ok': True, 'dry_run': bool(dry_run),
              'removed': [], 'kept_local': [], 'kept_unmanaged': [], 'errors': []}
    pinned_keys = set()
    for host, root, _suffixes in skill_roots:
        if not root.is_dir():
            continue
        for entry in sorted(root.iterdir()):
            if not entry.is_dir():
                continue
            marker = marker_of(entry)
            if not marker:
                if entry.name not in pinned:
                    report['kept_unmanaged'].append({'host': host, 'name': entry.name,
                                                     'path': str(entry)})
                continue
            if entry.name in pinned:
                pinned_keys.add(entry.name)
                continue
            try:
                actual = hashes.hash(entry)
            except (UnsafeTree, TooLarge, OSError) as error:
                report['errors'].append(f'{host}:{entry.name}: {type(error).__name__}')
                continue
            if marker.get('sha256') != actual:
                report['kept_local'].append({'host': host, 'name': entry.name, 'path': str(entry),
                                             'reason': 'local edits since the hub copy'})
                continue
            report['removed'].append({'host': host, 'name': entry.name, 'path': str(entry)})
            if not dry_run:
                shutil.rmtree(entry, ignore_errors=True)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',
                        choices=['sync', 'ingest', 'seed', 'status', 'bootstrap', 'prune'])
    parser.add_argument('--home', default=None)
    parser.add_argument('--hub', default=None)
    parser.add_argument('--source', default=None, help='extra skill root to import (seed only)')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    if args.command == 'seed':
        report = seed(home=args.home, hub=args.hub, source=args.source, dry_run=args.dry_run)
    elif args.command == 'ingest':
        report = ingest(home=args.home, hub=args.hub, dry_run=args.dry_run,
                        sources=[args.source] if args.source else None)
    elif args.command == 'bootstrap':
        report = bootstrap(home=args.home, hub=args.hub, dry_run=args.dry_run)
    elif args.command == 'status':
        report = status(home=args.home, hub=args.hub)
    elif args.command == 'prune':
        report = prune(home=args.home, hub=args.hub, dry_run=args.dry_run)
    else:
        report = sync(home=args.home, hub=args.hub, dry_run=args.dry_run)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get('ok') else 1


if __name__ == '__main__':
    raise SystemExit(main())
