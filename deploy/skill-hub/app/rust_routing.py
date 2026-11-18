"""Optional native Rust accelerators for TEMPR graph, semantic aggregation, and bundle SHA-256."""
from __future__ import annotations

import ctypes
import json
import os
import struct
from pathlib import Path
from enum import IntEnum

try:
    if os.name == 'nt':
        raise OSError('Windows Skill Hub native execution is retired')
    _LIB_NAME = "libskill_hub_routing.so"
    _DEFAULT_LIB = Path(__file__).with_name(_LIB_NAME)
    if not _DEFAULT_LIB.is_file():
        for _alt in (
            Path(__file__).resolve().parent.parent / "rust" / "build" / _LIB_NAME,
            Path(__file__).resolve().parent.parent / "deploy" / "skill-hub" / "rust" / "build" / _LIB_NAME,
        ):
            if _alt.is_file():
                _DEFAULT_LIB = _alt
                break
    _LIB_PATH = os.environ.get("SKILL_HUB_RUST_ROUTING", str(_DEFAULT_LIB))
    _CDLL = ctypes.CDLL(_LIB_PATH)

    _free_buf = _CDLL.skill_hub_free_buffer
    _free_buf.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    _free_buf.restype = None

    _sem_topk = _CDLL.skill_hub_semantic_score_topk
    _sem_topk.argtypes = [
        ctypes.POINTER(ctypes.c_float),
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_int32),
        ctypes.POINTER(ctypes.c_uint8),
        ctypes.c_size_t,
        ctypes.c_size_t,
        ctypes.c_size_t,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_double),
        ctypes.POINTER(ctypes.c_size_t),
        ctypes.POINTER(ctypes.c_size_t),
        ctypes.POINTER(ctypes.c_size_t),
    ]
    _sem_topk.restype = ctypes.c_int

    _graph_create = _CDLL.skill_hub_graph_index_create
    _graph_create.argtypes = [ctypes.c_char_p, ctypes.c_size_t]
    _graph_create.restype = ctypes.c_void_p

    _graph_free = _CDLL.skill_hub_graph_index_free
    _graph_free.argtypes = [ctypes.c_void_p]
    _graph_free.restype = None

    _graph_query = _CDLL.skill_hub_graph_index_query
    _graph_query.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_size_t,
        ctypes.c_char_p,
        ctypes.c_size_t,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_size_t),
    ]
    _graph_query.restype = ctypes.c_int

    _sha256_files = _CDLL.skill_hub_sha256_files
    _sha256_files.argtypes = [
        ctypes.c_char_p,
        ctypes.c_size_t,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_uint8),
    ]
    _sha256_files.restype = ctypes.c_int
except (OSError, AttributeError):
    _CDLL = None
    _free_buf = None
    _sem_topk = None
    _graph_create = None
    _graph_free = None
    _graph_query = None
    _sha256_files = None


def available() -> bool:
    return _CDLL is not None


def semantic_score_topk(
    scores,
    chunk_res_idx,
    res_first_order,
    res_group_idx,
    res_eligible,
    num_groups: int,
    limit: int,
):
    """Aggregate chunk scores to resources and equivalence groups in Rust.

    Returns (out_res_idx, out_chunk_idx, out_scores, matched_res, unique_groups) or None.
    """
    if _sem_topk is None:
        return None
    import numpy as np

    num_chunks = int(len(scores))
    num_res = int(len(res_first_order))
    if num_chunks == 0 or num_res == 0:
        return [], [], [], 0, 0

    scores_arr = np.ascontiguousarray(scores, dtype=np.float32)
    chunk_arr = np.ascontiguousarray(chunk_res_idx, dtype=np.uint32)
    order_arr = np.ascontiguousarray(res_first_order, dtype=np.uint32)
    group_arr = np.ascontiguousarray(res_group_idx, dtype=np.int32)
    elig_arr = np.ascontiguousarray(res_eligible, dtype=np.uint8)

    cap = min(max(0, int(limit)), num_res)
    out_r = np.empty(max(1, cap), dtype=np.uint32)
    out_c = np.empty(max(1, cap), dtype=np.uint32)
    out_s = np.empty(max(1, cap), dtype=np.float64)
    out_count = ctypes.c_size_t(0)
    out_matched = ctypes.c_size_t(0)
    out_groups = ctypes.c_size_t(0)

    rc = _sem_topk(
        scores_arr.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
        chunk_arr.ctypes.data_as(ctypes.POINTER(ctypes.c_uint32)),
        order_arr.ctypes.data_as(ctypes.POINTER(ctypes.c_uint32)),
        group_arr.ctypes.data_as(ctypes.POINTER(ctypes.c_int32)),
        elig_arr.ctypes.data_as(ctypes.POINTER(ctypes.c_uint8)),
        num_chunks,
        num_res,
        int(num_groups),
        cap,
        out_r.ctypes.data_as(ctypes.POINTER(ctypes.c_uint32)),
        out_c.ctypes.data_as(ctypes.POINTER(ctypes.c_uint32)),
        out_s.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
        ctypes.byref(out_count),
        ctypes.byref(out_matched),
        ctypes.byref(out_groups),
    )
    if rc != 0:
        return None
    n = int(out_count.value)
    return (
        out_r[:n].tolist(),
        out_c[:n].tolist(),
        out_s[:n].tolist(),
        int(out_matched.value),
        int(out_groups.value),
    )


_REL_TO_CODE = {
    "provides": 0,
    "specializes": 1,
    "uses_tool": 2,
    "requires_tool": 3,
    "depends_on": 4,
    "alias_of": 5,
}
_CODE_TO_REL = {v: k for k, v in _REL_TO_CODE.items()}
_CONCEPT_PREFIXES = ("cap:", "tool:", "dom:", "dep:", "ext:")


class GraphNativeIndex:
    """Immutable compiled Rust graph index tied to one projection snapshot."""

    def __init__(self, projection: dict, tokenize_fn):
        if _graph_create is None or _graph_query is None or _free_buf is None:
            raise RuntimeError("native graph index unavailable")

        nodes_dict = projection.get("nodes") or {}
        concepts_dict = projection.get("concepts") or {}
        edges_list = projection.get("edges") or []

        self.concept_ids = sorted(concepts_dict.keys())
        self.concept_idx_of = {cid: i for i, cid in enumerate(self.concept_ids)}
        self.node_ids = sorted(nodes_dict.keys())
        self.node_idx_of = {nid: i for i, nid in enumerate(self.node_ids)}
        self.node_groups = [
            (nodes_dict[nid] or {}).get("g") for nid in self.node_ids
        ]
        self.num_nodes = len(self.node_ids)
        self.num_concepts = len(self.concept_ids)
        self.num_edges = len(edges_list)

        parts = [
            struct.pack(
                "<III",
                self.num_concepts,
                self.num_nodes,
                0,  # placeholder for edge count
            )
        ]

        for cid in self.concept_ids:
            info = concepts_dict[cid] or {}
            fanout = int(info.get("n") or 0)
            c_tokens = tokenize_fn(info.get("label") or "") or tokenize_fn(
                cid.split(":", 1)[-1].replace("-", " ")
            )
            c_list = sorted(c_tokens)
            parts.append(struct.pack("<III", fanout, len(c_tokens), len(c_list)))
            for tok in c_list:
                tb = tok.encode("utf-8")
                parts.append(struct.pack("<I", len(tb)))
                parts.append(tb)

        collapse_keys = {}
        for nid in self.node_ids:
            node = nodes_dict[nid] or {}
            active = bool(node.get("act", 1))
            is_alias = bool(node.get("al") or 0)
            is_cpref = nid.startswith(_CONCEPT_PREFIXES)
            flags = (1 if active else 0) | (2 if is_alias else 0) | (4 if is_cpref else 0)

            group = node.get("g")
            if group:
                ckey = ("group", group)
            else:
                cname = node.get("can") or node.get("n")
                kind = node.get("t")
                ckey = ("canonical", kind, cname) if cname else ("id", nid)
            ckey_id = collapse_keys.get(ckey)
            if ckey_id is None:
                ckey_id = len(collapse_keys)
                collapse_keys[ckey] = ckey_id

            raw_n = node.get("n") or ""
            n_tokens = tokenize_fn(raw_n.replace("-", " ")) if raw_n else set()
            n_list = sorted(n_tokens)
            parts.append(struct.pack("<BIII", flags, ckey_id, len(n_tokens), len(n_list)))
            for tok in n_list:
                tb = tok.encode("utf-8")
                parts.append(struct.pack("<I", len(tb)))
                parts.append(tb)

            seed_name = raw_n or nid.split(":", 1)[-1]
            if seed_name == raw_n:
                parts.append(b"\x01")
            else:
                parts.append(b"\x00")
                s_tokens = tokenize_fn(seed_name.replace("-", " "))
                s_list = sorted(s_tokens)
                parts.append(struct.pack("<II", len(s_tokens), len(s_list)))
                for tok in s_list:
                    tb = tok.encode("utf-8")
                    parts.append(struct.pack("<I", len(tb)))
                    parts.append(tb)

        edge_count = 0
        edge_parts = []
        for edge in edges_list:
            src = edge["s"]
            dst = edge["d"]
            rel_code = _REL_TO_CODE.get(edge["r"], 255)
            if src in self.node_idx_of:
                s_is_c, s_idx = 0, self.node_idx_of[src]
            elif src in self.concept_idx_of:
                s_is_c, s_idx = 1, self.concept_idx_of[src]
            else:
                continue
            if dst in self.concept_idx_of:
                d_is_c, d_idx = 1, self.concept_idx_of[dst]
            elif dst in self.node_idx_of:
                d_is_c, d_idx = 0, self.node_idx_of[dst]
            else:
                continue
            edge_parts.append(struct.pack("<BIBIB", s_is_c, s_idx, d_is_c, d_idx, rel_code))
            edge_count += 1

        parts[0] = struct.pack("<III", self.num_concepts, self.num_nodes, edge_count)
        blob = b"".join(parts) + b"".join(edge_parts)
        handle = _graph_create(blob, len(blob))
        if not handle:
            raise RuntimeError("failed to initialize native graph index")
        self._handle = handle

    def close(self):
        handle = getattr(self, "_handle", None)
        if handle and _graph_free is not None:
            self._handle = None
            _graph_free(handle)

    def __del__(self):
        self.close()

    def query(self, q_tokens, eligible_ids: set[str], limit: int):
        if not self._handle:
            return None
        mask = bytearray(self.num_nodes)
        node_idx_of = self.node_idx_of
        for eid in eligible_ids:
            idx = node_idx_of.get(eid)
            if idx is not None:
                mask[idx] = 1

        q_list = sorted(q_tokens)
        q_parts = [struct.pack("<I", len(q_list))]
        for tok in q_list:
            tb = tok.encode("utf-8")
            q_parts.append(struct.pack("<I", len(tb)))
            q_parts.append(tb)
        q_blob = b"".join(q_parts)
        mask_bytes = bytes(mask)

        out_ptr = ctypes.c_void_p()
        out_len = ctypes.c_size_t(0)
        rc = _graph_query(
            self._handle,
            q_blob,
            len(q_blob),
            mask_bytes,
            self.num_nodes,
            max(0, int(limit)),
            ctypes.byref(out_ptr),
            ctypes.byref(out_len),
        )
        if rc != 0 or not out_ptr.value:
            return None
        try:
            raw = ctypes.string_at(out_ptr.value, out_len.value)
        finally:
            _free_buf(out_ptr.value, out_len.value)

        if len(raw) < 20:
            return None
        matched_concepts, seeds, candidates, truncated_concepts, hit_count = struct.unpack_from("<IIIII", raw, 0)
        offset = 20
        rec_fmt = "<IdBiBIIIBB"
        rec_size = struct.calcsize(rec_fmt)
        if len(raw) != 20 + hit_count * rec_size:
            return None

        hits = []
        node_ids = self.node_ids
        concept_ids = self.concept_ids
        node_groups = self.node_groups
        for rank in range(1, hit_count + 1):
            n_idx, raw_score, rel_code, via_idx, path_len, pn0, pn1, pn2, pr0, pr1 = struct.unpack_from(
                rec_fmt, raw, offset
            )
            offset += rec_size
            rid = node_ids[n_idx]
            rel_name = _CODE_TO_REL.get(rel_code, "provides")
            via = concept_ids[via_idx] if via_idx >= 0 else None
            if path_len <= 1:
                path = [node_ids[pn0]]
            elif path_len == 3:
                path = [node_ids[pn0], _CODE_TO_REL.get(pr0, "provides"), node_ids[pn1]]
            else:
                path = [
                    node_ids[pn0],
                    _CODE_TO_REL.get(pr0, "provides"),
                    node_ids[pn1],
                    _CODE_TO_REL.get(pr1, "provides"),
                    node_ids[pn2],
                ]
            hits.append({
                "id": rid,
                "rank": rank,
                "raw_score": raw_score,
                "evidence": {
                    "path": path,
                    "relation": rel_name,
                    "hops": max(0, (len(path) - 1) // 2),
                    "via": via,
                    "group": node_groups[n_idx],
                },
                "equivalence_group_id": node_groups[n_idx],
            })

        return hits, {
            "matched_concepts": matched_concepts,
            "seeds": seeds,
            "candidates": candidates,
            "truncated_concepts": truncated_concepts,
        }


def sha256_files(paths) -> list[str] | None:
    """Compute SHA-256 hex digests for a sequence of file paths in Rust."""
    if _sha256_files is None:
        return None
    count = len(paths)
    if count == 0:
        return []
    encoded = []
    for p in paths:
        s = os.fspath(p)
        if not s or "\0" in s:
            return None
        try:
            b = s.encode("utf-8")
        except UnicodeEncodeError:
            return None
        encoded.append(b)
    buf = b"\0".join(encoded)
    out = (ctypes.c_uint8 * (count * 32))()
    if _sha256_files(buf, len(buf), count, out) != 0:
        return None
    raw = bytes(out)
    return [raw[i * 32 : (i + 1) * 32].hex() for i in range(count)]


# Required by the route server; metadata utilities may still use the older optional ABI.
_decide = getattr(_CDLL, "skill_hub_decide_v1", None)
if _decide is not None:
    _decide.argtypes = [ctypes.c_uint32, ctypes.c_char_p, ctypes.c_size_t,
                        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_size_t)]
    _decide.restype = ctypes.c_int


class Operation(IntEnum):
    DIGEST = 0
    FUSION = 1
    COALESCE = 3
    STAGE_INIT = 4
    STAGE_NEXT = 5
    RESOURCE_CHOICE = 6
    CACHE_IDENTITY = 7
    PROVIDER_SCORES = 8
    EXPANSION_QUERY = 9
    EXPAND = 10
    INTERLEAVE = 11
    METADATA_RANK = 12
    QUALIFICATION_CLUSTERS = 13
    EQUIVALENCE = 14
    ADAPTIVE_ROUND = 15
    NO_RESOURCE_CONSTRAINT = 16


def decision(operation, value):
    if _decide is None:
        raise RuntimeError("RUST_ROUTING_UNAVAILABLE: use the Linux Skill Hub image")
    data = value if isinstance(value, bytes) else canonical_json(value)
    pointer, size = ctypes.c_void_p(), ctypes.c_size_t()
    status = _decide(operation, data, len(data), ctypes.byref(pointer), ctypes.byref(size))
    if status != 0:
        raise RuntimeError(f"RUST_ROUTING_FAILED: operation={operation} status={status}")
    try:
        return json.loads(ctypes.string_at(pointer, size.value))
    finally:
        _free_buf(pointer.value, size.value)


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')
