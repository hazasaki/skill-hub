"""Use the native batch matcher for ASCII words; preserve Python Unicode semantics."""
import ctypes
import os
from pathlib import Path
import re

try:
    library = "skill_hub_metadata.dll" if os.name == "nt" else "libskill_hub_metadata.so"
    path = os.environ.get("SKILL_HUB_RUST_METADATA", str(Path(__file__).with_name(library)))
    _match = ctypes.CDLL(path).metadata_hits
    _match.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p, ctypes.c_size_t,
                       ctypes.POINTER(ctypes.c_uint64), ctypes.c_size_t]
    _match.restype = ctypes.c_int
except (OSError, AttributeError):
    _match = None


def _masks(entries, terms):
    if _match is None or not entries or not 0 < len(terms) <= 63 or not all(t.isascii() and "\0" not in t for t in terms):
        return None
    fields = [value.lower() for e in entries for value in (e["resource_id"], e.get("description", ""))]
    if any("\0" in value for value in fields):
        return None
    query = "\0".join(terms).encode("ascii")
    # Non-ASCII rows use Python re below. A constant marker also handles surrogate code points.
    data = b"\0".join(value.encode("ascii") if value.isascii() else b"\xff" for value in fields)
    masks = (ctypes.c_uint64 * (2 * len(entries)))()
    return masks if _match(query, len(query), data, len(data), masks, len(entries)) == 0 else None


def query_hits(entries, terms):
    words = tuple(sorted(terms))
    masks = _masks(entries, words)
    decoded = {0: frozenset()}

    def decode(bits):
        if bits not in decoded:
            decoded[bits] = frozenset(word for i, word in enumerate(words) if bits & (1 << i))
        return decoded[bits]

    for i, entry in enumerate(entries):
        if masks is not None and masks[2 * i] != 2**64 - 1:
            yield decode(masks[2 * i]), decode(masks[2 * i + 1])
        else:
            names = terms.intersection(re.findall(r'\w+', entry['resource_id'].lower()))
            yield names, names | terms.intersection(re.findall(r'\w+', entry.get('description', '').lower()))
