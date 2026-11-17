"""Build the TEMPR graph/temporal projection artifacts from a live catalog, read-only.

Run from the graph worktree:

    python runtime/build_graph_projection.py \
        --hub /data/skill-hub \
        --index /data/skill-hub/state/skill-index.db \
        --out  /data/retrieval/graph

The Hub is opened read-only; the only writes are the projection files under --out.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import retrieval_projection as projection  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hub', required=True)
    parser.add_argument('--index', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--ontology', default=None,
                        help='JSON file holding the pinned Auto Ontology block')
    parser.add_argument('--max-scan', type=int, default=None)
    args = parser.parse_args(argv)

    ontology = None
    if args.ontology:
        ontology = json.loads(Path(args.ontology).read_text(encoding='utf-8'))

    started = time.perf_counter()
    document, report = projection.build(
        args.hub, args.index, ontology=ontology, max_scan=args.max_scan)
    build_ms = (time.perf_counter() - started) * 1000.0

    out = Path(args.out)
    written = projection.write_projection(document,
                                         out / 'graph-projection.json',
                                         out / 'temporal-projection.json')
    report = {**report, 'build_ms': round(build_ms, 1), 'written': written}
    (out / 'projection-report.json').write_text(
        json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True), encoding='utf-8')
    print(json.dumps({key: report[key] for key in
                      ('built_at', 'edges', 'concepts', 'coverage', 'build_ms', 'written')},
                     indent=2, ensure_ascii=False))
    print('edge_types', report['edge_types'])
    print('gaps', json.dumps(report['gaps'][:8], ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
