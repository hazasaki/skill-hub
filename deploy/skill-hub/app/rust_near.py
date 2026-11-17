"""Optional native 5-word shingle similarity for catalog review."""
import ctypes
import os
from pathlib import Path

try:
    library = 'skill_hub_near.dll' if os.name == 'nt' else 'libskill_hub_near.so'
    library_path = os.environ.get('SKILL_HUB_RUST_NEAR', str(Path(__file__).with_name(library)))
    _near = ctypes.CDLL(library_path).skill_hub_jaccard
    _near.argtypes = [ctypes.POINTER(ctypes.c_char_p), ctypes.POINTER(ctypes.c_size_t),
                      ctypes.c_size_t, ctypes.POINTER(ctypes.c_double)]
    _near.restype = ctypes.c_int
except (OSError, AttributeError):
    _near = None


def similarities(texts):
    if _near is None:
        return None
    try:
        encoded = [text.encode('utf-8') for text in texts]
    except UnicodeEncodeError:
        return None
    size = len(encoded)
    if size > 40:
        return None
    pointers = (ctypes.c_char_p * size)(*encoded)
    lengths = (ctypes.c_size_t * size)(*(len(text) for text in encoded))
    output = (ctypes.c_double * (size * size))()
    if _near(pointers, lengths, size, output):
        return None
    return output
