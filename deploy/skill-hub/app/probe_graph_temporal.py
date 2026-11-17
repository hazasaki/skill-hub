"""Real-projection query evidence: graph and temporal results from the built artefacts.

Run from the graph worktree after `runtime/build_graph_projection.py`:

    PYTHONIOENCODING=utf-8 python runtime/probe_graph_temporal.py \
        --projection-dir /data/retrieval/graph \
        --live-index /data/skill-hub/state/skill-index.db \
        --out .../query-evidence.json

Read-only against the Hub. The evidence file records the exact manifest, per-query status,
reason, candidate counts and the top hits with their evidence paths and dates.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import retrieval_graph as graph  # noqa: E402
import retrieval_projection as projection  # noqa: E402
import retrieval_temporal as temporal  # noqa: E402

GRAPH_QUERIES = [
    ('en-exact-name', 'review pull request'),
    ('en-paraphrase', 'review pull request security vulnerabilities in code'),
    ('en-tool', 'kubernetes deployment helm chart rollback'),
    ('vn-paraphrase', 'phân tích dữ liệu bán hàng và vẽ biểu đồ'),
    ('no-skill', 'nấu phở bò tại nhà'),
]

TEMPORAL_QUERIES = [
    ('en-recency', 'what is the latest version of the xlsx skill'),
    ('vn-recency', 'skill mới nhất về kubernetes'),
    ('en-updated', 'which skill was updated recently for security'),
    ('en-neutral', 'review pull request code'),
    ('vn-neutral', 'nấu phở bò'),
]


def load_entries(live_index):
    con = sqlite3.connect(Path(live_index).resolve().as_uri() + '?mode=ro', uri=True)
    try:
        entries = []
        for name, description, tags in con.execute(
                'SELECT name, description, tags FROM skill_resources WHERE active=1'):
            entries.append({'id': f'skill:{name}', 'type': 'skill', 'resource_id': name,
                            'description': description or '', 'tags': tags or ''})
        for name, description, tags in con.execute(
                'SELECT name, description, tags FROM prompt_resources WHERE active=1'):
            entries.append({'id': f'prompt:{name}', 'type': 'prompt', 'resource_id': name,
                            'description': description or '', 'tags': tags or ''})
    finally:
        con.close()
    return entries


def run(projection_dir, live_index, out_path, repeats=5):
    projection_dir = Path(projection_dir)
    entries = load_entries(live_index)
    report = {'entry_count': len(entries), 'queries': [], 'timings': {}}

    graph_doc = projection.load_projection(projection_dir / 'graph-projection.json',
                                          expected_kind='graph')
    temporal_doc = projection.load_projection(projection_dir / 'temporal-projection.json',
                                              expected_kind='temporal')
    report['manifest'] = graph_doc['manifest']
    report['projection_hashes'] = {
        'graph': graph_doc['_loaded']['sha256'],
        'temporal': temporal_doc['_loaded']['sha256'],
    }

    for label, text in GRAPH_QUERIES:
        started = time.perf_counter()
        result = graph.retrieve(text, entries,
                                projection_path=projection_dir / 'graph-projection.json',
                                manifest=graph_doc['manifest'], limit=10)
        report['queries'].append({'engine': 'graph', 'label': label, 'query': text,
                                  'wall_ms': round((time.perf_counter() - started) * 1000, 1),
                                  'status': result['status'], 'reason': result['reason'],
                                  'elapsed_ms': result['elapsed_ms'],
                                  'coverage': result['coverage'],
                                  'hits': [{'id': hit['id'], 'rank': hit['rank'],
                                            'score': hit['raw_score'],
                                            'evidence': hit['evidence']}
                                           for hit in result['hits']]})

    for label, text in TEMPORAL_QUERIES:
        started = time.perf_counter()
        result = temporal.retrieve(text, entries,
                                   projection_path=projection_dir / 'temporal-projection.json',
                                   manifest=temporal_doc['manifest'], limit=5)
        report['queries'].append({'engine': 'temporal', 'label': label, 'query': text,
                                  'wall_ms': round((time.perf_counter() - started) * 1000, 1),
                                  'status': result['status'], 'reason': result['reason'],
                                  'elapsed_ms': result['elapsed_ms'],
                                  'coverage': result['coverage'],
                                  'hits': [{'id': hit['id'], 'rank': hit['rank'],
                                            'score': hit['raw_score'],
                                            'evidence': hit['evidence']}
                                           for hit in result['hits']]})

    # Repeated timings isolate cold (first load) from warm steady state.
    for engine, path, kind, query in (
            ('graph', projection_dir / 'graph-projection.json', 'graph',
             'review pull request security'),
            ('temporal', projection_dir / 'temporal-projection.json', 'temporal',
             'latest security audit')):
        module = graph if engine == 'graph' else temporal
        document = graph_doc if engine == 'graph' else temporal_doc
        samples = []
        for index in range(repeats):
            started = time.perf_counter()
            module.retrieve(query, entries, projection_path=path,
                            manifest=document['manifest'], limit=10)
            samples.append((time.perf_counter() - started) * 1000)
        samples_sorted = sorted(samples)
        report['timings'][engine] = {
            'samples_ms': [round(value, 1) for value in samples],
            'cold_ms': round(samples[0], 1),
            'warm_median_ms': round(samples_sorted[len(samples_sorted) // 2], 1),
            'warm_p95_ms': round(samples_sorted[min(len(samples_sorted) - 1,
                                                    int(len(samples_sorted) * 0.95))], 1),
            'loads_projection_per_call': True,
        }

    text = json.dumps(report, indent=2, ensure_ascii=False)
    if out_path:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_text(text, encoding='utf-8')
    print(text)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--projection-dir', required=True)
    parser.add_argument('--live-index', required=True)
    parser.add_argument('--out', default=None)
    parser.add_argument('--repeats', type=int, default=5)
    args = parser.parse_args(argv)
    run(args.projection_dir, args.live_index, args.out, args.repeats)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
