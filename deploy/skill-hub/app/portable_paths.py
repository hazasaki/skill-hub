"""Explicit deployment-root mappings for persisted Windows artifact paths."""
import json
import os
from pathlib import Path, PureWindowsPath


def portable_path(value):
    raw = os.fspath(value)
    windows = PureWindowsPath(raw)
    if not windows.drive:
        if raw.startswith('\\'):
            raise ValueError('rooted Windows path requires a mapped drive')
        return Path(raw)
    if not windows.is_absolute() or '..' in windows.parts:
        raise ValueError('ambiguous or traversing Windows path')
    configured = os.environ.get('SKILL_HUB_PATH_MAP')
    if not configured and os.name == 'nt':
        return Path(raw)
    mappings = json.loads(configured or '{}')
    if not isinstance(mappings, dict):
        raise ValueError('SKILL_HUB_PATH_MAP must be an object')
    for source, destination in sorted(mappings.items(), key=lambda item: len(item[0]), reverse=True):
        prefix = PureWindowsPath(source)
        if not prefix.is_absolute() or '..' in prefix.parts:
            raise ValueError('mapping source must be an absolute Windows root')
        try:
            suffix = windows.relative_to(prefix)
        except ValueError:
            continue
        target = Path(destination)
        if not target.is_absolute():
            raise ValueError('mapping destination must be absolute')
        result = target.joinpath(*suffix.parts)
        if not result.resolve().is_relative_to(target.resolve()):
            raise ValueError('mapped path escapes mount root')
        return result
    raise ValueError('Windows path has no configured mount mapping')
