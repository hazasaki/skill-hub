"""Bounded host-side direct artifact writer for replay integration.

This module publishes a validated batch of text artifacts into a *new* descendant
directory inside an explicitly supplied trusted ``authorized_base`` directory.

Security and boundary rules:
- ``authorized_base`` is supplied separately by the trusted host caller, never read from
  an untrusted model payload.
- ``destination`` must resolve to a *new* (non-existent) strict descendant inside
  ``authorized_base`` (never ``authorized_base`` itself, never outside it, and never
  traversing a symlink, junction, or reparse point).
- ``artifacts`` must be a non-empty list (up to 20 items) of exact ``{"path": str,
  "content": str}`` dicts, where each ``content`` is at most 1 MiB UTF-8 and total
  UTF-8 content across the batch is at most 4 MiB.
- Every artifact ``path`` must be a safe relative path: no absolute/drive/UNC roots,
  no ``.`` or ``..`` segments, no empty segments, no control/null characters, no Windows
  invalid characters (``<>:"|?*`` — which also rejects NTFS Alternate Data Streams),
  no trailing dots or spaces on any segment, no Windows reserved device names
  (``CON``, ``PRN``, ``AUX``, ``NUL``, ``COM1``..``COM9``, ``LPT1``..``LPT9``,
  ``CONIN$``, ``CONOUT$``, ``CLOCK$``, with or without extensions), and no case-folded
  path collisions or file/directory prefix collisions within the batch.
- The **entire batch** is validated before any directory is created or any file is written.
- Content is written as raw UTF-8 bytes with exclusive creation (``"xb"``) and verified
  by reading back the written bytes from disk and computing SHA-256.
- Nothing in ``artifacts`` is executed, imported, or interpreted as commands.
- Internal host helper only: never registered or exposed as a remote MCP tool.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath, PureWindowsPath

MAX_FILES = 20
MAX_FILE_UTF8_BYTES = 1_048_576      # 1 MiB
MAX_TOTAL_UTF8_BYTES = 4_194_304     # 4 MiB

_WINDOWS_RESERVED_NAMES = frozenset({
    'con', 'prn', 'aux', 'nul', 'conin$', 'conout$', 'clock$',
    *(f'com{i}' for i in range(1, 10)),
    *(f'lpt{i}' for i in range(1, 10)),
    'com\xb9', 'com\xb2', 'com\xb3',
    'lpt\xb9', 'lpt\xb2', 'lpt\xb3',
})

_INVALID_SEGMENT_CHARS = frozenset('<>:"|?*')


class PublishError(ValueError):
    """Raised when artifact publication fails validation or host I/O verification."""


def _is_reparse_or_link(path: Path) -> bool:
    """True if ``path`` is a symlink, junction, or Windows reparse point."""
    try:
        if path.is_symlink():
            return True
        is_junc = getattr(path, 'is_junction', None)
        if callable(is_junc) and is_junc():
            return True
        st = os.lstat(path)
    except OSError:
        return False
    if getattr(st, 'st_reparse_tag', 0) != 0:
        return True
    file_attrs = getattr(st, 'st_file_attributes', 0)
    if file_attrs & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT
        return True
    return False


def _validate_segment(segment: str, context: str) -> None:
    if not segment:
        raise PublishError(f'{context}: empty path segment is not allowed')
    if segment in ('.', '..'):
        raise PublishError(f'{context}: traversal segment is not allowed')
    if segment[-1] in ('.', ' '):
        raise PublishError(f'{context}: trailing dot or space in path segment is not allowed')
    for ch in segment:
        if ord(ch) < 32 or ord(ch) == 127 or ch in _INVALID_SEGMENT_CHARS:
            raise PublishError(f'{context}: invalid or ADS character in path segment')
    stem = segment.split('.', 1)[0].strip(' .').casefold()
    full_fold = segment.strip(' .').casefold()
    if stem in _WINDOWS_RESERVED_NAMES or full_fold in _WINDOWS_RESERVED_NAMES:
        raise PublishError(f'{context}: Windows reserved device name is not allowed')


def _parse_relative_segments(raw_path: str, context: str) -> tuple[str, ...]:
    if not isinstance(raw_path, str):
        raise PublishError(f'{context}: path must be a string')
    if not raw_path or not raw_path.strip():
        raise PublishError(f'{context}: path must not be empty')
    if '\x00' in raw_path:
        raise PublishError(f'{context}: null byte in path is not allowed')
    if ':' in raw_path:
        raise PublishError(f'{context}: drive prefix or NTFS alternate data stream is not allowed')
    if raw_path.startswith(('/', '\\')):
        raise PublishError(f'{context}: absolute or root-anchored path is not allowed')

    win_path = PureWindowsPath(raw_path)
    posix_path = PurePosixPath(raw_path)
    if win_path.is_absolute() or posix_path.is_absolute() or win_path.drive or win_path.root:
        raise PublishError(f'{context}: absolute, drive, or UNC path is not allowed')

    normalized = raw_path.replace('\\', '/')
    if normalized.startswith('/') or normalized.endswith('/'):
        raise PublishError(f'{context}: leading or trailing slash is not allowed')
    segments = tuple(normalized.split('/'))
    for segment in segments:
        _validate_segment(segment, context)
    return segments


def _validate_authorized_base(authorized_base) -> Path:
    if not isinstance(authorized_base, (str, os.PathLike)):
        raise PublishError('authorized_base must be a path string or PathLike')
    base_str = os.fspath(authorized_base)
    if not isinstance(base_str, str) or not base_str.strip() or '\x00' in base_str:
        raise PublishError('authorized_base must be a non-empty path')
    base_raw = Path(base_str)
    if not base_raw.is_absolute():
        raise PublishError('authorized_base must be an explicit absolute path')
    try:
        exists = base_raw.exists()
        is_dir = base_raw.is_dir()
    except OSError as exc:
        raise PublishError('authorized_base is not accessible') from exc
    if not exists or not is_dir:
        raise PublishError('authorized_base must be an existing directory')
    if _is_reparse_or_link(base_raw):
        raise PublishError('authorized_base must not be a symlink or junction')
    base_resolved = base_raw.resolve(strict=True)
    # Also ensure no ancestor or base itself is a symlink/junction mismatch.
    if _is_reparse_or_link(base_resolved):
        raise PublishError('authorized_base resolved to a symlink or junction')
    return base_resolved


def _validate_destination(base_resolved: Path, destination) -> Path:
    if not isinstance(destination, (str, os.PathLike)):
        raise PublishError('destination must be a path string or PathLike')
    dest_str = os.fspath(destination)
    if not isinstance(dest_str, str) or not dest_str.strip() or '\x00' in dest_str:
        raise PublishError('destination must be a non-empty path')

    dest_raw = Path(dest_str)
    win_dest = PureWindowsPath(dest_str)

    if dest_raw.is_absolute() or win_dest.is_absolute() or win_dest.drive or win_dest.root:
        if not dest_raw.is_absolute():
            raise PublishError('destination has drive or root anchor without full absolute path')
        # Check raw lexical parts for '..' or '.' before resolution
        if '..' in dest_raw.parts:
            raise PublishError('destination must not contain parent traversal segments')
        candidate = dest_raw
        try:
            rel_to_base = candidate.resolve(strict=False).relative_to(base_resolved)
        except ValueError as exc:
            raise PublishError('destination must be inside authorized_base') from exc
        rel_segments = rel_to_base.parts
        if not rel_segments:
            raise PublishError('destination must be a strict descendant of authorized_base, not base itself')
        for seg in rel_segments:
            _validate_segment(seg, 'destination')
    else:
        rel_segments = _parse_relative_segments(dest_str, 'destination')
        candidate = base_resolved.joinpath(*rel_segments)

    # Walk from base_resolved down each relative segment to verify no symlink/junction escape
    current = base_resolved
    for index, seg in enumerate(rel_segments):
        current = current / seg
        try:
            lexists = os.path.lexists(current)
        except OSError as exc:
            raise PublishError('destination path check failed') from exc
        if lexists:
            if _is_reparse_or_link(current):
                raise PublishError('destination path traverses a symlink, junction, or reparse point')
            if index == len(rel_segments) - 1:
                raise PublishError('destination already exists; refusing to overwrite or merge')
            if not current.is_dir():
                raise PublishError('destination ancestor exists and is not a directory')
            resolved_step = current.resolve(strict=True)
            try:
                resolved_step.relative_to(base_resolved)
            except ValueError as exc:
                raise PublishError('destination ancestor escapes authorized_base') from exc

    resolved_dest = current.resolve(strict=False)
    if resolved_dest == base_resolved:
        raise PublishError('destination must be a strict descendant of authorized_base, not base itself')
    try:
        resolved_dest.relative_to(base_resolved)
    except ValueError as exc:
        raise PublishError('destination escapes authorized_base') from exc

    return current


def _validate_artifacts_batch(artifacts) -> list[tuple[str, tuple[str, ...], bytes, str]]:
    if not isinstance(artifacts, list):
        raise PublishError('artifacts must be a list of {path, content} objects')
    if not artifacts:
        raise PublishError('artifacts list must not be empty')
    if len(artifacts) > MAX_FILES:
        raise PublishError(f'artifacts count {len(artifacts)} exceeds max_files ({MAX_FILES})')

    seen_files_fold: set[str] = set()
    seen_dirs_fold: set[str] = set()
    total_bytes = 0
    prepared: list[tuple[str, tuple[str, ...], bytes, str]] = []

    for index, entry in enumerate(artifacts):
        ctx = f'artifacts[{index}]'
        if not isinstance(entry, dict):
            raise PublishError(f'{ctx}: entry must be a dict')
        if set(entry.keys()) != {'path', 'content'}:
            raise PublishError(f'{ctx}: entry must have exact keys "path" and "content"')
        raw_path = entry['path']
        content = entry['content']
        if not isinstance(raw_path, str):
            raise PublishError(f'{ctx}: path must be a string')
        if not isinstance(content, str):
            raise PublishError(f'{ctx}: content must be a string')

        segments = _parse_relative_segments(raw_path, ctx)
        norm_rel = '/'.join(segments)
        fold_segments = tuple(s.casefold() for s in segments)
        fold_rel = '/'.join(fold_segments)

        if fold_rel in seen_files_fold:
            raise PublishError(f'{ctx}: duplicate artifact path (case-insensitive)')
        if fold_rel in seen_dirs_fold:
            raise PublishError(f'{ctx}: artifact file path collides with directory path in batch')

        for prefix_len in range(1, len(fold_segments)):
            dir_prefix = '/'.join(fold_segments[:prefix_len])
            if dir_prefix in seen_files_fold:
                raise PublishError(f'{ctx}: artifact directory prefix collides with file path in batch')
            seen_dirs_fold.add(dir_prefix)

        seen_files_fold.add(fold_rel)

        try:
            encoded = content.encode('utf-8')
        except UnicodeEncodeError as exc:
            raise PublishError(f'{ctx}: content is not valid UTF-8') from exc

        byte_len = len(encoded)
        if byte_len > MAX_FILE_UTF8_BYTES:
            raise PublishError(
                f'{ctx}: UTF-8 byte length {byte_len} exceeds max_file_utf8_bytes ({MAX_FILE_UTF8_BYTES})'
            )
        total_bytes += byte_len
        if total_bytes > MAX_TOTAL_UTF8_BYTES:
            raise PublishError(
                f'batch UTF-8 byte length {total_bytes} exceeds max_total_utf8_bytes ({MAX_TOTAL_UTF8_BYTES})'
            )

        expected_sha = hashlib.sha256(encoded).hexdigest()
        prepared.append((norm_rel, segments, encoded, expected_sha))

    return prepared


def _ensure_safe_dir(base_resolved: Path, target_dir: Path) -> None:
    """Create ``target_dir`` inside ``base_resolved`` step by step, verifying no symlink/junction."""
    rel_parts = target_dir.relative_to(base_resolved).parts
    current = base_resolved
    for part in rel_parts:
        current = current / part
        if os.path.lexists(current):
            if _is_reparse_or_link(current):
                raise PublishError('refusing to traverse symlink or junction during directory creation')
            if not current.is_dir():
                raise PublishError('ancestor path exists and is not a directory')
        else:
            current.mkdir(exist_ok=False)
            if _is_reparse_or_link(current):
                raise PublishError('created directory became a symlink or junction')


def publish_artifacts(authorized_base, destination, artifacts) -> dict:
    """Validate and write ``artifacts`` into a new ``destination`` inside ``authorized_base``.

    Returns::

        {
            "files": [{"path": <relative posix str>, "bytes": <int>, "sha256": <hex str>}, ...],
            "status": "published",
            "method": "direct_host_write",
        }
    """
    # Phase 1: Validate entire batch and paths before any filesystem mutation.
    base_resolved = _validate_authorized_base(authorized_base)
    dest_path = _validate_destination(base_resolved, destination)
    prepared = _validate_artifacts_batch(artifacts)

    # Phase 2: Create destination and write files with exclusive creation + readback check.
    written_paths: list[str] = []
    published_files: list[dict] = []
    open_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)

    try:
        _ensure_safe_dir(base_resolved, dest_path)

        for norm_rel, segments, encoded, expected_sha in prepared:
            file_path = dest_path.joinpath(*segments)
            _ensure_safe_dir(base_resolved, file_path.parent)
            if os.path.lexists(file_path):
                raise PublishError(f'target file already exists: {norm_rel}')

            fd = os.open(file_path, open_flags, 0o666)
            try:
                view = memoryview(encoded)
                total_written = 0
                while total_written < len(encoded):
                    n = os.write(fd, view[total_written:])
                    if n <= 0:
                        raise OSError('short write while publishing artifact')
                    total_written += n
            finally:
                os.close(fd)

            written_paths.append(norm_rel)

            if _is_reparse_or_link(file_path):
                raise PublishError(f'written file is a symlink or junction: {norm_rel}')

            readback = file_path.read_bytes()
            readback_len = len(readback)
            readback_sha = hashlib.sha256(readback).hexdigest()
            if readback_len != len(encoded) or readback_sha != expected_sha:
                raise PublishError(f'readback verification mismatch for {norm_rel}')

            published_files.append({
                'path': norm_rel,
                'bytes': readback_len,
                'sha256': readback_sha,
            })
    except PublishError as exc:
        exc.partial_files = list(written_paths)
        raise
    except OSError as exc:
        err = PublishError(
            f'host I/O error during direct write ({type(exc).__name__}); '
            f'partial_files={written_paths!r}'
        )
        err.partial_files = list(written_paths)
        raise err from exc

    return {
        'files': published_files,
        'status': 'published',
        'method': 'direct_host_write',
    }
