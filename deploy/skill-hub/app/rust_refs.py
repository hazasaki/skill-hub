"""Optional native scanner for local bundle path references."""
import ctypes
import os
from pathlib import Path

try:
    library = 'skill_hub_refs.dll' if os.name == 'nt' else 'libskill_hub_refs.so'
    path = os.environ.get('SKILL_HUB_RUST_REFS', str(Path(__file__).with_name(library)))
    _scan = ctypes.CDLL(path).skill_hub_reference_offsets_v3
    _scan.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t), ctypes.c_size_t]
    _scan.restype = ctypes.c_size_t
except (OSError, AttributeError):
    _scan = None


def offsets(text):
    if _scan is None:
        return None
    try:
        data = text.encode('utf-8')
    except UnicodeEncodeError:
        return None
    if len(data) > 64 * 1024 * 1024:
        return None
    capacity = 32
    out = (ctypes.c_size_t * (capacity * 2))()
    count = _scan(data, len(data), out, capacity)
    if count > len(data):
        return None
    if count > capacity:
        capacity = count
        out = (ctypes.c_size_t * (capacity * 2))()
        if _scan(data, len(data), out, capacity) != count:
            return None
    return data, [(out[2 * i], out[2 * i + 1]) for i in range(count)]
