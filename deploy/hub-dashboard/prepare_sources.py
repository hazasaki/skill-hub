"""Export private, allowlisted native metadata for the Linux dashboard."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src/hub-dashboard'))
from server import Dashboard
from sources import AUDIT, read_json


def aggregate_audit(raw):
    # Remove case payloads before crossing the container mount boundary.
    fields = ('verified_at_utc', 'mode', 'verdict', 'cases', 'selection_recall',
              'loaded_recall', 'candidate_recall', 'broad_relevance_precision',
              'provenance_limit', 'runtime_limit')
    result = {key: raw[key] for key in fields if key in raw and
              (raw[key] is None or type(raw[key]) in (str, int, float, bool))}
    result['timing_ms'] = {key: value for key, value in raw.get('timing_ms', {}).items()
                           if type(value) in (int, float)}
    return result


def snapshot_catalog(source, destination):
    deadline = time.monotonic() + 30
    def progress(*_):
        if time.monotonic() > deadline:
            raise TimeoutError('Catalog snapshot exceeded 30 seconds')
    with closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True,
                                timeout=3)) as src:
        src.execute('PRAGMA query_only=ON')
        with closing(sqlite3.connect(destination)) as dst:
            src.backup(dst, pages=512, progress=progress)
            if dst.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise ValueError('Catalog snapshot failed integrity check')


def write_bundle(destination, native, audit, catalog=None):
    destination.mkdir(parents=True)  # A new version only; never replace prior inputs.
    files = {}
    for name, value in (('native.json', native), ('AUDIT.json', audit)):
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
        with (destination / name).open('xb') as output:
            output.write(raw)
        files[name] = hashlib.sha256(raw).hexdigest()
    if catalog is not None:
        path = destination / 'skill-index.db'
        snapshot_catalog(catalog, path)
        with path.open('rb') as stream:
            files[path.name] = hashlib.file_digest(stream, 'sha256').hexdigest()
    manifest = {'schema': 'skill-hub.dashboard.inputs.v1',
                'recorded_at': datetime.now(timezone.utc).isoformat(), 'sha256': files,
                'limits': ['Configured snapshots, not runtime or all-host plugin coverage',
                           'Historical regression aggregates, not a new evaluation']}
    with (destination / 'manifest.json').open('x', encoding='utf-8') as output:
        json.dump(manifest, output, ensure_ascii=False, allow_nan=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', type=Path,
                        default=Path(__file__).resolve().parents[2])
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    app = Dashboard(project=args.project)
    native = app.sources.export()
    audit = aggregate_audit(read_json(args.project / AUDIT, 1000000))
    write_bundle(args.output, native, audit, app.catalog)
    print('Exported sanitized dashboard inputs to ' + str(args.output))


if __name__ == '__main__':
    main()
