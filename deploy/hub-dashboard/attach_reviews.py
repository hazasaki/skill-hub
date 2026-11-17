"""Create a new immutable dashboard input version with locally generated review receipts."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tempfile


def attach_reviews(previous, destination, quality, security, decisions=None):
    if destination.exists():
        raise FileExistsError(destination)
    manifest = json.loads((previous / 'manifest.json').read_bytes())
    pins = manifest['sha256']
    with tempfile.TemporaryDirectory(dir=destination.parent) as scratch:
        bundle = Path(scratch) / 'inputs'
        bundle.mkdir()
        for name in ('native.json', 'AUDIT.json', 'skill-index.db'):
            target = bundle / name
            shutil.copyfile(previous / name, target)
            with target.open('rb') as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != pins[name]:
                    raise ValueError(f'Base input changed: {name}')
        for name, path, key in [('catalog-quality.json', quality, 'index_sha256'),
                                ('security-review.json', security, 'catalog_sha256')]:
            if path.stat().st_size > 32 * 1024 * 1024:
                raise ValueError('Review receipt too large')
            raw = path.read_bytes()
            if json.loads(raw)[key] != pins['skill-index.db']:
                raise ValueError(f'Review references a different catalog: {name}')
            (bundle / name).write_bytes(raw)
            pins[name] = hashlib.sha256(raw).hexdigest()
        if decisions is None and 'original-decisions.json' in pins:
            decisions=previous/'original-decisions.json'
            if hashlib.sha256(decisions.read_bytes()).hexdigest()!=pins['original-decisions.json']: raise ValueError('Previous decisions receipt changed')
        if decisions is not None:
            if decisions.stat().st_size>8_000_000: raise ValueError('Decisions receipt too large')
            raw=decisions.read_bytes(); payload=json.loads(raw)
            if set(payload)!={'schema','entries'} or payload['schema']!='skill-hub.original-decisions.v1' or not isinstance(payload['entries'],dict): raise ValueError('Invalid decisions receipt')
            (bundle/'original-decisions.json').write_bytes(raw)
            pins['original-decisions.json']=hashlib.sha256(raw).hexdigest()
        else:
            pins.pop('original-decisions.json',None)
        (bundle / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
        bundle.rename(destination)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('previous', 'destination', 'quality', 'security'):
        parser.add_argument(key, type=Path)
    parser.add_argument('--decisions',type=Path)
    args = parser.parse_args()
    attach_reviews(args.previous, args.destination, args.quality, args.security, args.decisions)
