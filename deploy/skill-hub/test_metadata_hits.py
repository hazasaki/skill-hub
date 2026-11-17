import ctypes
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).parent / "app"))
import metadata_hits


class MetadataHitsTest(unittest.TestCase):
    def test_native_matches_python_word_boundaries(self):
        rows = [{"resource_id": name, "description": text} for name, text in
                [("test_python", "TEST python"), ("python-test", "Tiếng Việt"),
                 ("機器", "foo\0bar"), ("a.b", "x42"), ("", "")]]
        for terms in ({"python", "test", "x42"}, {"機器"}, set(), {f"word{i}" for i in range(70)}):
            with patch.object(metadata_hits, "_match", None):
                expected = list(metadata_hits.query_hits(rows, terms))
            self.assertEqual(expected, list(metadata_hits.query_hits(rows, terms)))
            self.assertEqual(expected[:2], list(metadata_hits.query_hits(rows[:2], terms)))

    def test_native_guards_and_nul_query(self):
        if metadata_hits._match is None:
            self.skipTest('native matcher unavailable')
        out = (ctypes.c_uint64 * 2)()
        huge = ctypes.c_size_t(-1).value
        self.assertEqual(metadata_hits._match(b'a', huge, b'b', 1, out, 1), 1)
        self.assertEqual(metadata_hits._match(b'a', 1, b'b', huge, out, 1), 1)
        rows = [{"resource_id": "a b", "description": "c"}]
        self.assertEqual(list(metadata_hits.query_hits(rows, {"a\\0b"})), [(set(), set())])
