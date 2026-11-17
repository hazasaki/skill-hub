"""Compare original catalog cleanup with Rust and a tuple-based Python variant."""
import importlib.util
import os
import sqlite3
import statistics
import time

import catalog_cleanup as current
import rust_near

baseline_path = os.environ.get('SKILL_HUB_BASELINE_MODULE')
if not baseline_path:
    raise SystemExit('Set SKILL_HUB_BASELINE_MODULE to the original catalog_cleanup.py')
spec = importlib.util.spec_from_file_location('original_cleanup', baseline_path)
old = importlib.util.module_from_spec(spec)
spec.loader.exec_module(old)
db_path = os.environ.get('SKILL_HUB_TEST_DB', '/tmp/source.db')
con = sqlite3.connect('file:' + db_path + '?mode=ro', uri=True)
con.row_factory = sqlite3.Row
families = old.build_family_groups(con)
original = old._shingles
native = rust_near._near
assert native is not None


def tuple_shingles(text):
    words = text.split()
    if len(words) < old.NEAR_SHINGLE:
        return {tuple(words)} if words else set()
    return {tuple(words[i:i + old.NEAR_SHINGLE])
            for i in range(len(words) - old.NEAR_SHINGLE + 1)}


baseline = None
times = {name: [] for name in ('python', 'python_tuple', 'rust')}
for iteration in range(5):
    order = ['python', 'python_tuple', 'rust']
    if iteration % 2:
        order.reverse()
    for name in order:
        old._shingles = tuple_shingles if name == 'python_tuple' else original
        start = time.perf_counter()
        result = (current if name == 'rust' else old).build_near_duplicate_pairs(con, families)
        times[name].append((time.perf_counter() - start) * 1000)
        if baseline is None:
            baseline = result
        assert result == baseline, name
print('families', len(families), 'pairs', len(baseline), 'full output parity passed')
for name, values in times.items():
    print(name, 'median_ms', round(statistics.median(values), 2),
          'samples', *[round(value, 2) for value in values])
