"""Read a catalog-quality receipt only while its catalog and manifest match."""
from collections import Counter
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path

SCHEMA = 'skill-hub.catalog-quality.v1'
RECORD_FIELDS = ('id', 'indexed_active', 'index_body_empty', 'index_document_sha256',
                 'index_bundle_sha256', 'disposition', 'source_status', 'source_body_empty',
                 'source_document_sha256', 'source_bundle_sha256', 'document_matches_index',
                 'explicitly_disabled', 'source_valid', 'missing_reference_count',
                 'escaping_reference_count', 'error_count')
MEMBER_FIELDS = ('resource_id', 'active', 'content_sha256', 'bundle_sha256',
                 'body_sha256', 'risk', 'canonical_name', 'is_alias')


def _stamp(stat):
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns,
            stat.st_ctime_ns if os.name == 'posix' else None)


def _checked_bytes(path, limit=None):
    with path.open('rb') as stream:
        before = _stamp(os.fstat(stream.fileno()))
        if limit is not None and before[2] > limit:
            raise OSError('source too large')
        data = stream.read()
        after = _stamp(os.fstat(stream.fileno()))
    if before != after or before != _stamp(path.stat()):
        raise OSError('source changed')
    return data, before


def _read_digest(path, expected_stamp):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        before = _stamp(os.fstat(stream.fileno()))
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
        after = _stamp(os.fstat(stream.fileno()))
    if before != expected_stamp or before != after or before != _stamp(path.stat()):
        raise OSError('catalog changed')
    return digest.hexdigest(), before


_cached_digest = lru_cache(maxsize=4)(_read_digest)


def _checked_digest(path):
    if any(Path(str(path) + suffix).exists() for suffix in ('-wal', '-journal')):
        raise OSError('catalog snapshot has a journal')
    # POSIX ctime also changes when an existing file is rewritten with the old mtime.
    reader = _cached_digest if os.name == 'posix' else _read_digest
    return reader(path, _stamp(path.stat()))


def _public_fields(row, fields, clean):
    if not isinstance(row, dict):
        raise OSError('malformed record')
    result = {}
    for key in fields:
        if key not in row:
            continue
        value = row[key]
        if isinstance(value, str):
            result[key] = clean(value[:1024])
        elif value is None or type(value) in (bool, int):
            result[key] = value
        else:
            raise OSError('malformed field')
    return result


def read_receipt(catalog, receipt, query, clean):
    catalog, receipt = Path(catalog), Path(receipt)
    manifest_path = receipt.parent / 'manifest.json'
    try:
        manifest_bytes, manifest_stamp = _checked_bytes(manifest_path, 1_000_000)
        manifest = json.loads(manifest_bytes.decode('utf-8-sig'))
        receipt_bytes, receipt_stamp = _checked_bytes(receipt, 16_000_000)
        pins = manifest['sha256']
        if hashlib.sha256(receipt_bytes).hexdigest() != pins['catalog-quality.json']:
            raise OSError('receipt hash mismatch')
        payload = json.loads(receipt_bytes.decode('utf-8-sig'))
        index_hash, catalog_stamp = _checked_digest(catalog)
        if (payload['schema'] != SCHEMA or payload['index_sha256'] != index_hash
                or pins['skill-index.db'] != index_hash):
            raise OSError('catalog hash mismatch')
        if any(_stamp(path.stat()) != stamp for path, stamp in
               ((manifest_path, manifest_stamp), (receipt, receipt_stamp), (catalog, catalog_stamp))):
            raise OSError('source changed')
        records, groups = payload['records'], payload['body_duplicate_groups']
        if not isinstance(records, list) or not isinstance(groups, list) or len(records) > 100000 or len(groups) > 100000:
            raise OSError('malformed receipt')
        clean_records = [_public_fields(row, RECORD_FIELDS, clean) for row in records]
        for source, record in zip(records, clean_records):
            areas = source.get('missing_by_area')
            if areas is not None:
                if not isinstance(areas, dict) or any(type(areas.get(k)) is not int or areas[k] < 0 for k in ('scripts', 'references', 'resources', 'other')):
                    raise OSError('malformed missing-reference counts')
                record['missing_by_area'] = {k: areas[k] for k in ('scripts', 'references', 'resources', 'other')}
        if any(not isinstance(row.get('id'), str) or not isinstance(row.get('disposition'), str) for row in clean_records):
            raise OSError('malformed identity')
        summary = {'quarantined': len(clean_records),
                   'dispositions': dict(Counter(row['disposition'] for row in clean_records)),
                   'index_body_empty': sum(row.get('index_body_empty') is True for row in clean_records),
                   'body_duplicate_groups': len(groups),
                   'missing_by_area': {k: sum(row.get('missing_by_area', {}).get(k, 0) for row in clean_records)
                                       for k in ('scripts', 'references', 'resources', 'other')}}
        view = query.get('view', 'records')
        if view not in ('records', 'groups'):
            raise ValueError('invalid catalog-quality view')
        disposition = query.get('disposition', '')[:100]
        search = query.get('q', '')[:200].casefold()
        offset = max(0, min(100000, int(query.get('offset', 0))))
        limit = max(1, min(100, int(query.get('limit', 50))))
        if view == 'records':
            items = [row for row in clean_records if (not disposition or row.get('disposition') == disposition)
                     and (not search or search in row.get('id', '').casefold())]
        else:
            items = []
            for row in groups:
                item = _public_fields(row, ('group_id', 'distinct_bundle_count', 'disposition'), clean)
                members = row.get('members')
                if not isinstance(members, list) or len(members) > 1000:
                    raise OSError('malformed group')
                item['members'] = [_public_fields(member, MEMBER_FIELDS, clean) for member in members]
                if (not isinstance(item.get('group_id'), str) or not isinstance(item.get('disposition'), str)
                        or any(not isinstance(member.get('resource_id'), str) for member in item['members'])):
                    raise OSError('malformed group identity')
                if disposition and item.get('disposition') != disposition:
                    continue
                if search and search not in item.get('group_id', '').casefold() and not any(
                        search in member.get('resource_id', '').casefold() for member in item['members']):
                    continue
                items.append(item)
        return {'view': view, 'summary': summary, 'items': items[offset:offset + limit], 'total': len(items),
                'offset': offset, 'limit': limit, 'observed_at': clean(str(payload['observed_at'])[:100]),
                'index_sha256': index_hash,
                'limits': ['Historical source observations; revalidate before action.',
                           'Quarantine and source permissions remain unchanged.']}
    except (KeyError, TypeError, UnicodeError, json.JSONDecodeError) as error:
        raise OSError('invalid catalog-quality receipt') from error
