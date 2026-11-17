"""Catalog Maintenance Module for AdaptiveSkillReflex Skill Hub.

Implements D9 requirement:
- Safe opt-in retention and compaction with dry-run mode.
- Mandatory backup creation before any mutation.
- Atomic SQLite VACUUM with integrity check and a preserved recovery snapshot.
- Protected live DB boundary: refuses mutation of live DB without explicit authorization.
- Safe backup retention: keeps newest N backups, never touches unrelated user backups.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import hashlib
import sqlite3
import sys
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any, Dict, List, Optional

from shared_resources import DEFAULT_HUB

LIVE_DB_CANONICAL = (DEFAULT_HUB / 'state/skill-index.db').resolve()

BACKUP_PATTERN = re.compile(r'^skill-index\.db\.bak-\d{8}-\d{6}$')


def is_live_database(path: Path | str) -> bool:
    """Check if the target path resolves to the canonical live database.
    
    Uses os.path.samefile when both paths exist to compare filesystem dev/ino
    and resolve junctions/symlinks, preventing accidental live DB mutations.
    """
    try:
        candidate = Path(path).resolve()
        if not candidate.exists():
            return False
        if LIVE_DB_CANONICAL.exists():
            try:
                if os.path.samefile(candidate, LIVE_DB_CANONICAL):
                    return True
            except (OSError, ValueError):
                pass
        return candidate.as_posix().lower() == LIVE_DB_CANONICAL.as_posix().lower()
    except (OSError, ValueError):
        return False


def compact_index(
    db_path: Path | str,
    *,
    dry_run: bool = True,
    retain_backups: int = 2,
    backup_dir: Optional[Path | str] = None,
    allow_live: bool = False,
    prune_created_backups: bool = False,
) -> Dict[str, Any]:
    """Execute safe opt-in compaction and backup retention on an SQLite index database.
    
    Guards:
    - If db is live and allow_live is False, refuses to mutate.
    - If dry_run is True, performs no writes/deletes.
    - Creates backup before VACUUM.
    - Runs PRAGMA integrity_check; failed VACUUM is atomic and its recovery snapshot is preserved.
    - Pruning is disabled unless explicitly requested and only touches digest-verified tool snapshots.
    """
    path = Path(db_path).resolve()
    if isinstance(retain_backups, bool) or not isinstance(retain_backups, int) or retain_backups < 1:
        raise ValueError('retain_backups must be a positive integer')
    if not path.is_file():
        raise FileNotFoundError(f'Database file not found: {path}')

    is_live = is_live_database(path)
    if is_live and not allow_live and not dry_run:
        raise PermissionError(
            'Live DB mutation forbidden: D9 requires running maintenance on copied DB or explicit allow_live flag.'
        )

    bdir = (Path(backup_dir) if backup_dir else path.parent).resolve()

    current_size = path.stat().st_size

    # Inspect current fragmentation via page_size and freelist_count
    freelist_count = 0
    page_size = 4096
    try:
        con = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
        try:
            page_size = con.execute('PRAGMA page_size').fetchone()[0]
            freelist_count = con.execute('PRAGMA freelist_count').fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error:
        pass

    reclaimable_bytes = freelist_count * page_size
    estimated_size = max(0, current_size - reclaimable_bytes)

    # Names alone do not establish ownership of user backups. Pruning requires an
    # explicit flag and a tool receipt whose digest still matches the file.
    receipt = bdir / ('.' + path.name + '.catalog-maintenance.json')
    owned = []
    if receipt.is_file() and not receipt.is_symlink():
        record = json.loads(receipt.read_text(encoding='utf-8'))
        if record.get('database') == str(path):
            for item in record.get('backups', []):
                name = item.get('name', '')
                candidate = bdir / name
                if (Path(name).name == name and name.startswith(path.name + '.snapshot-')
                        and candidate.is_file() and not candidate.is_symlink()
                        and hashlib.sha256(candidate.read_bytes()).hexdigest() == item.get('sha256')):
                    owned.append(item)
    existing_backups = [bdir / item['name'] for item in owned]

    if dry_run:
        backups_to_prune = (
            [p.name for p in existing_backups[:max(0, len(existing_backups) - retain_backups + 1)]]
            if prune_created_backups and len(existing_backups) >= retain_backups else []
        )
        return {
            'status': 'dry_run',
            'database': str(path),
            'is_live': is_live,
            'current_size_bytes': current_size,
            'freelist_pages': freelist_count,
            'estimated_reclaimable_bytes': reclaimable_bytes,
            'estimated_size_bytes': estimated_size,
            'retain_backups_limit': retain_backups,
            'existing_managed_backups': [p.name for p in existing_backups],
            'backups_planned_to_prune': backups_to_prune,
            'action': 'Dry-run inspection complete. No changes made.',
        }

    # Non-dry-run execution
    bdir.mkdir(parents=True, exist_ok=True)
    backup_file = bdir / f'{path.name}.snapshot-{uuid.uuid4().hex}.db'
    
    # SQLite's backup API includes committed WAL pages; a file copy does not.
    with backup_file.open('xb'):
        pass
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as source:
        with closing(sqlite3.connect(backup_file)) as backup:
            source.backup(backup)
            if backup.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                raise sqlite3.DatabaseError('Backup integrity check failed')
    owned.append({'name': backup_file.name,
                  'sha256': hashlib.sha256(backup_file.read_bytes()).hexdigest()})

    # 2. Transactional vacuum with integrity check
    rolled_back = False
    try:
        con = sqlite3.connect(str(path))
        try:
            con.execute('PRAGMA temp_store = MEMORY')
            con.execute('VACUUM')
            check = con.execute('PRAGMA integrity_check').fetchall()
            if not check or check[0][0] != 'ok':
                raise sqlite3.DatabaseError(f'Integrity check failed: {check}')
        finally:
            con.close()
    except Exception as err:
        # VACUUM is atomic; failed VACUUM leaves the source intact. Preserve the
        # snapshot for recovery instead of overwriting a database and its WAL.
        raise RuntimeError(f'Compaction failed: {err}. Snapshot preserved at {backup_file}') from err

    new_size = path.stat().st_size
    actual_reclaimed = current_size - new_size

    # 4. Backup retention cleanup
    pruned_backups = []
    if prune_created_backups and len(owned) > retain_backups:
        excess = len(owned) - retain_backups
        for item in owned[:excess]:
            b = bdir / item['name']
            b.unlink()
            pruned_backups.append(b.name)
        owned = owned[excess:]

    # Exclusive temporary file + replace prevents truncated ownership receipts.
    receipt_temp = receipt.with_name(receipt.name + '.' + uuid.uuid4().hex + '.tmp')
    with receipt_temp.open('x', encoding='utf-8') as handle:
        json.dump({'database': str(path), 'backups': owned}, handle)
    receipt_temp.replace(receipt)
    remaining_backups = [item['name'] for item in owned]

    return {
        'status': 'compacted',
        'database': str(path),
        'is_live': is_live,
        'size_before_bytes': current_size,
        'size_after_bytes': new_size,
        'bytes_reclaimed': actual_reclaimed,
        'backup_created': str(backup_file),
        'backups_pruned': pruned_backups,
        'backups_retained': remaining_backups,
        'rolled_back': rolled_back,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description='Safe index compaction and retention tooling (D9)')
    parser.add_argument('--db', required=True, type=Path, help='Target SQLite database')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true', help='Inspect without changes (default)')
    mode.add_argument('--apply', action='store_true', help='Apply compaction on an offline database')
    parser.add_argument('--retain-backups', type=int, default=2, help='Number of backups to retain')
    parser.add_argument('--backup-dir', type=Path, default=None, help='Directory for backups')
    parser.add_argument('--allow-live', action='store_true', default=False, help='Explicitly allow live DB operation')
    parser.add_argument('--prune-created-backups', action='store_true', help='Prune only receipt-verified tool snapshots')
    args = parser.parse_args(argv)

    try:
        result = compact_index(
            args.db,
            dry_run=not args.apply,
            retain_backups=args.retain_backups,
            backup_dir=args.backup_dir,
            allow_live=args.allow_live,
            prune_created_backups=args.prune_created_backups,
        )
        print(json.dumps(result, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({'error': str(exc)}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
