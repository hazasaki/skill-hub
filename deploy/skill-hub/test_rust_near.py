import ctypes
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'app'))
import catalog_cleanup as cleanup
import rust_near


class NearTests(unittest.TestCase):
    def test_python_parity_for_unicode_empty_and_repeated_words(self):
        if rust_near._near is None:
            self.skipTest('native library unavailable')
        values = ['', 'a', ' a\t b\n c ', 'a b c', 'a b c d e a b c d e',
                  'a\u001cb\u00a0c\u2003d\u2029e', 'a b c d e', 'xin chào thế giới nhé',
                  'a\x00b c d e f', '😄 日本 ไทย 한국']
        rng = random.Random(281)
        tokens = ['a', 'b', 'á', '中', '😄']
        values.extend(' '.join(rng.choices(tokens, k=rng.randrange(15))) for _ in range(100))
        for start in range(0, len(values), 30):
            batch = values[start:start + 30]
            actual = rust_near.similarities(batch)
            for i, left in enumerate(batch):
                for j in range(i + 1, len(batch)):
                    expected = cleanup.jaccard(cleanup._shingles(left), cleanup._shingles(batch[j]))
                    self.assertEqual(actual[i * len(batch) + j], expected)
        self.assertIsNone(rust_near.similarities(values))

    def test_native_rejects_size_t_wraparound(self):
        if rust_near._near is None:
            self.skipTest('native library unavailable')
        pointers = (ctypes.c_char_p * 1)(b'x')
        lengths = (ctypes.c_size_t * 1)(ctypes.c_size_t(-1).value)
        self.assertEqual(rust_near._near(pointers, lengths, 1, (ctypes.c_double * 1)()), 1)

    def test_fallback_on_unavailable_library_and_invalid_unicode(self):
        original = rust_near._near
        try:
            rust_near._near = None
            self.assertIsNone(rust_near.similarities(['anything']))
        finally:
            rust_near._near = original
        self.assertIsNone(rust_near.similarities(['\ud800']))


if __name__ == '__main__':
    unittest.main()
