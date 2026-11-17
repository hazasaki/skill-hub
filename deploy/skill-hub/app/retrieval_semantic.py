"""runtime/retrieval_semantic.py

TEMPR Semantic Retrieval Engine using BGE-M3 embeddings.
Part of AdaptiveSkillReflex TEMPR architecture.

Contract:
    retrieve(text, entries, *, index_path, manifest, limit=250, deadline=None, embed=None) -> EngineResult
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, Union

import numpy as np

ENGINE_NAME = "semantic"
DEFAULT_MODEL = "bge-m3:latest"
DEFAULT_DIGEST = "7907646426070047a77226ac3e684fbbe8410524f7b4a74d02837e43f2146bab"
DEFAULT_DIMENSION = 1024
DEFAULT_ENDPOINT = os.environ.get("SKILL_HUB_OLLAMA_ENDPOINT") or "http://127.0.0.1:11434"


class SemanticRetrievalError(Exception):
    """Base error for semantic retrieval failures."""
    pass


class ManifestValidationError(SemanticRetrievalError):
    """Raised when manifest validation fails."""
    pass


def _residency(endpoint, model, timeout):
    """The pinned model's /api/ps row, or None when it is not loaded yet."""
    req = urllib.request.Request(f"{endpoint.rstrip('/')}/api/ps")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        resident = json.loads(response.read()).get('models', [])
    for row in resident:
        if (row.get('name') or row.get('model')) == model:
            return row
    return None


MAX_OFFLOAD_RAM_BYTES = int(os.environ.get("SKILL_HUB_MAX_RAM_OFFLOAD_BYTES") or 1024 * 1024 * 1024)
MIN_OFFLOAD_VRAM_BYTES = int(os.environ.get("SKILL_HUB_MIN_VRAM_BYTES") or 1)


def check_residency(row, max_ram_bytes=None, min_vram_bytes=None):
    """Evaluate pinned model residency against GPU-preferred, resource-bounded policy.

    Returns:
        (is_ready: bool, gpu_resident: bool, reason: Optional[str], ram_estimate: int)
    """
    if max_ram_bytes is None:
        max_ram_bytes = MAX_OFFLOAD_RAM_BYTES
    if min_vram_bytes is None:
        min_vram_bytes = MIN_OFFLOAD_VRAM_BYTES
    if not row:
        return False, False, 'Model not loaded in Ollama', 0
    digest = row.get('digest')
    if digest != DEFAULT_DIGEST:
        return False, False, f'WRONG_DIGEST: resident digest {digest} != pinned {DEFAULT_DIGEST}', 0
    try:
        size = int(row.get('size', 0))
        vram = int(row.get('size_vram', 0))
    except (ValueError, TypeError):
        return False, False, 'Invalid size or size_vram in residency row', 0
    if size <= 0:
        return False, False, 'Invalid model size in residency row', 0
    if vram < min_vram_bytes:
        return False, False, f'GPU_REQUIRED: pinned model is resident but not fully on GPU (size_vram={vram} < {min_vram_bytes})', size
    ram_estimate = max(0, size - vram)
    gpu_resident = bool(vram >= size)
    if ram_estimate > max_ram_bytes:
        return False, gpu_resident, f'RESOURCE_EXCEEDED: model requires {ram_estimate} bytes host RAM, exceeds budget {max_ram_bytes}', ram_estimate
    return True, gpu_resident, None, ram_estimate


def _fully_on_gpu(row):
    """True only for the pinned digest loaded with every byte in VRAM."""
    if not row:
        return False
    size, vram = row.get('size', 0), row.get('size_vram', 0)
    return bool(size > 0 and vram >= size and row.get('digest') == DEFAULT_DIGEST)


def require_gpu(endpoint, model, timeout, max_ram_bytes=None, min_vram_bytes=None):
    """Verify pinned model is loaded within GPU-preferred, bounded-RAM policy."""
    row = _residency(endpoint, model, timeout)
    is_ready, gpu_resident, reason, _ = check_residency(
        row, max_ram_bytes=max_ram_bytes, min_vram_bytes=min_vram_bytes
    )
    if not is_ready:
        raise SemanticRetrievalError(reason or 'Pinned model is not ready within resource limits')
    return gpu_resident


def load_on_gpu(endpoint, model, timeout):
    """Make the pinned model resident without submitting a real embedding query.

    A cold Ollama host has loaded nothing, so a residency check on its own reports
    GPU_REQUIRED for a host that would in fact load this model onto the GPU.
    That turned a cold start into a permanent semantic-engine ERROR on every task.

    This asks Ollama to load the model with num_gpu=-1 and an empty input list, which
    performs the load and returns no vectors. It is a load, not a fallback: the caller
    still verifies residency afterwards and fails closed if digest mismatches or RAM
    limits are exceeded. An empty list is unsupported by some builds, so a single space
    is used as the last resort; that embeds one throwaway token, never query text.
    """
    url = f"{endpoint.rstrip('/')}/api/embed"
    for payload_input in ([], [' ']):
        payload = {'model': model, 'input': payload_input,
                   'options': {'num_gpu': -1}, 'keep_alive': '10m'}
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode('utf-8'),
            headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                resp.read()
            return
        except urllib.error.HTTPError:
            continue
        except urllib.error.URLError as exc:
            raise SemanticRetrievalError(f'Ollama model load failed: {exc}') from exc


def ensure_gpu_resident(endpoint, model, timeout, max_ram_bytes=None, min_vram_bytes=None):
    """Ensure the pinned model is loaded in Ollama with GPU preference and bounded host RAM.

    Allows pinned model partial offload / CPU coordination within max_ram_bytes budget.
    Returns:
        {'ready': True, 'gpu_resident': bool, 'ram_estimate': int, 'size_vram': int}
    """
    row = _residency(endpoint, model, timeout)
    if row is not None:
        is_ready, gpu_resident, reason, ram_est = check_residency(
            row, max_ram_bytes=max_ram_bytes, min_vram_bytes=min_vram_bytes
        )
        if not is_ready:
            raise SemanticRetrievalError(
                reason or f'GPU_REQUIRED: pinned model is resident on {endpoint} but not fully on GPU'
            )
        return {'ready': True, 'gpu_resident': gpu_resident, 'ram_estimate': ram_est, 'size_vram': row.get('size_vram', 0)}

    load_on_gpu(endpoint, model, timeout)
    row = _residency(endpoint, model, timeout)
    is_ready, gpu_resident, reason, ram_est = check_residency(
        row, max_ram_bytes=max_ram_bytes, min_vram_bytes=min_vram_bytes
    )
    if not is_ready:
        raise SemanticRetrievalError(
            reason or f'GPU_REQUIRED: pinned model failed to become ready within resource limits after load'
        )
    return {'ready': True, 'gpu_resident': gpu_resident, 'ram_estimate': ram_est, 'size_vram': (row or {}).get('size_vram', 0)}


def embed_bge_m3(
    text_or_texts: Union[str, Sequence[str]],
    *,
    endpoint: str = DEFAULT_ENDPOINT,
    model: str = DEFAULT_MODEL,
    timeout: float = 10.0,
    deadline: Optional[float] = None,
) -> Union[List[float], List[List[float]]]:
    """Call local Ollama BGE-M3 embedding endpoint.

    Supports both single text (str) and batch (Sequence[str]).
    Enforces deadline if provided.
    Validates finite floats and dimension 1024.
    """
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Deadline exceeded before embedding request")
        timeout = min(timeout, remaining)

    is_batch = not isinstance(text_or_texts, str)
    ensure_gpu_resident(endpoint, model, timeout)
    if deadline is not None:
        timeout = min(timeout, deadline - time.monotonic())
        if timeout <= 0:
            raise TimeoutError('Deadline exceeded checking GPU residency')
    url = f"{endpoint.rstrip('/')}/api/embed"
    payload = {
        "model": model,
        "input": text_or_texts if is_batch else [text_or_texts],
        "options": {"num_gpu": -1},
        "keep_alive": "10m",
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise SemanticRetrievalError(f"Ollama embedding request failed: {exc}") from exc
    except TimeoutError as exc:
        raise
    except Exception as exc:
        raise SemanticRetrievalError(f"Embedding call failed: {exc}") from exc

    remaining = min(timeout, deadline - time.monotonic()) if deadline is not None else timeout
    if remaining <= 0:
        raise TimeoutError('Deadline exceeded after embedding request')
    require_gpu(endpoint, model, remaining)
    embeddings = body.get("embeddings")
    if not embeddings or not isinstance(embeddings, list):
        raise SemanticRetrievalError(f"Malformed embedding response from Ollama: {body}")

    validated = []
    for vec in embeddings:
        if not isinstance(vec, (list, tuple)):
            raise SemanticRetrievalError("Embedding vector is not a list")
        if len(vec) != DEFAULT_DIMENSION:
            raise SemanticRetrievalError(
                f"Dimension mismatch: expected {DEFAULT_DIMENSION}, got {len(vec)}"
            )
        arr = np.array(vec, dtype=np.float32)
        if not np.all(np.isfinite(arr)):
            raise SemanticRetrievalError("Embedding vector contains NaN or Inf values")
        norm = float(np.linalg.norm(arr))
        if norm > 0:
            arr = arr / norm
        validated.append(arr.tolist())

    return validated if is_batch else validated[0]


class SemanticIndex:
    """Manages reading and scoring against a persisted SQLite vector index."""

    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        self._meta: Optional[Dict[str, Any]] = None
        self._loaded_mtime: float = -1.0
        self._vectors: Optional[np.ndarray] = None
        self._chunk_ids: List[str] = []
        self._resource_ids: List[str] = []
        self._chunk_indices: List[int] = []
        self._chunk_types: List[str] = []
        self._res_map: Dict[str, Dict[str, Any]] = {}

    def get_meta(self) -> Dict[str, Any]:
        """Fetch metadata dictionary from index."""
        if self._meta is not None:
            return self._meta
        if not os.path.exists(self.db_path):
            raise FileNotFoundError(f"Index database not found: {self.db_path}")

        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            cur = conn.cursor()
            meta = {}
            for k, v in cur.execute("SELECT key, value FROM meta"):
                try:
                    meta[k] = json.loads(v)
                except (json.JSONDecodeError, TypeError):
                    meta[k] = v
            self._meta = meta
            return meta
        finally:
            conn.close()

    def load_vectors(self) -> Tuple[np.ndarray, List[str], List[str], List[int], List[str], Dict[str, Dict[str, Any]]]:
        """Load or retrieve cached normalized vector matrix and chunk mappings."""
        mtime = os.path.getmtime(self.db_path)
        if self._vectors is not None and self._loaded_mtime == mtime:
            return (
                self._vectors,
                self._chunk_ids,
                self._resource_ids,
                self._chunk_indices,
                self._chunk_types,
                self._res_map,
            )

        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            cur = conn.cursor()
            # Load resources info
            res_map = {}
            for r_id, name, r_type, desc, tags, cat, grp, active in cur.execute(
                "SELECT id, name, type, description, tags, category, equivalence_group_id, active FROM resources"
            ):
                res_map[r_id] = {
                    "id": r_id,
                    "name": name,
                    "type": r_type,
                    "description": desc,
                    "tags": tags,
                    "category": cat,
                    "equivalence_group_id": grp,
                    "active": bool(active),
                }

            # Load chunks and vectors
            rows = cur.execute(
                "SELECT chunk_id, resource_id, chunk_index, chunk_type, vector FROM chunks ORDER BY rowid"
            ).fetchall()

            if not rows:
                self._vectors = np.empty((0, DEFAULT_DIMENSION), dtype=np.float32)
                self._chunk_ids = []
                self._resource_ids = []
                self._chunk_indices = []
                self._chunk_types = []
                self._res_map = res_map
                self._loaded_mtime = mtime
                return self._vectors, self._chunk_ids, self._resource_ids, self._chunk_indices, self._chunk_types, self._res_map

            chunk_ids = [r[0] for r in rows]
            resource_ids = [r[1] for r in rows]
            chunk_indices = [r[2] for r in rows]
            chunk_types = [r[3] for r in rows]
            blobs = b"".join(r[4] for r in rows)

            mat = np.frombuffer(blobs, dtype=np.float32).reshape(len(rows), -1)
            # Ensure dimension check
            if mat.shape[1] != DEFAULT_DIMENSION:
                raise SemanticRetrievalError(
                    f"Index vector dimension {mat.shape[1]} != expected {DEFAULT_DIMENSION}"
                )

            # L2 normalize rows if not already normalized
            norms = np.linalg.norm(mat, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            mat = mat / norms

            self._vectors = mat
            self._chunk_ids = chunk_ids
            self._resource_ids = resource_ids
            self._chunk_indices = chunk_indices
            self._chunk_types = chunk_types
            self._res_map = res_map
            self._loaded_mtime = mtime
            return mat, chunk_ids, resource_ids, chunk_indices, chunk_types, res_map
        finally:
            conn.close()


_INDEX_CACHE: Dict[str, SemanticIndex] = {}


def get_semantic_index(index_path: str) -> SemanticIndex:
    """Return cached SemanticIndex instance for the given index_path."""
    resolved = os.path.abspath(index_path)
    if os.path.isdir(resolved):
        db_file = os.path.join(resolved, "semantic_index.db")
        if os.path.exists(db_file):
            resolved = db_file
    if resolved not in _INDEX_CACHE:
        _INDEX_CACHE[resolved] = SemanticIndex(resolved)
    return _INDEX_CACHE[resolved]


def validate_manifest(manifest: Dict[str, Any], index_meta: Dict[str, Any]) -> None:
    """Validate that the caller's manifest matches the index dependencies.

    Checks:
    - embedding_dimension == 1024
    - embedding_model_digest matches
    - catalog_sha256 matches (if specified in both)
    - vector_revision matches (if specified in both)
    """
    req_dim = manifest.get("embedding_dimension")
    if req_dim is not None and int(req_dim) != DEFAULT_DIMENSION:
        raise ManifestValidationError(
            f"Manifest embedding_dimension {req_dim} != expected {DEFAULT_DIMENSION}"
        )

    idx_dim = index_meta.get("embedding_dimension")
    if idx_dim is not None and int(idx_dim) != DEFAULT_DIMENSION:
        raise ManifestValidationError(
            f"Index embedding_dimension {idx_dim} != expected {DEFAULT_DIMENSION}"
        )

    req_digest = manifest.get("embedding_model_digest")
    if req_digest and req_digest != DEFAULT_DIGEST:
        raise ManifestValidationError(
            f"Manifest embedding_model_digest mismatch: expected {DEFAULT_DIGEST}, got {req_digest}"
        )

    idx_digest = index_meta.get("embedding_model_digest")
    if idx_digest and req_digest and idx_digest != req_digest:
        raise ManifestValidationError(
            f"Index digest {idx_digest} != manifest digest {req_digest}"
        )

    req_cat = manifest.get("catalog_sha256")
    idx_cat = index_meta.get("catalog_sha256")
    if req_cat and idx_cat and req_cat != idx_cat:
        raise ManifestValidationError(
            f"Catalog sha256 mismatch: manifest {req_cat} != index {idx_cat}"
        )

    req_rev = manifest.get("vector_revision")
    idx_rev = index_meta.get("vector_revision")
    if req_rev and idx_rev and req_rev != idx_rev:
        raise ManifestValidationError(
            f"Vector revision mismatch: manifest {req_rev} != index {idx_rev}"
        )


def retrieve(
    text: str,
    entries: Optional[Sequence[Union[Dict[str, Any], str]]] = None,
    *,
    index_path: str,
    manifest: Dict[str, Any],
    limit: int = 250,
    deadline: Optional[float] = None,
    embed: Optional[Callable[[str], Sequence[float]]] = None,
) -> Dict[str, Any]:
    """Retrieve candidates using semantic vector similarity against BGE-M3 index.

    Conforms to TEMPR retrieval contract:
        retrieve(text, entries, *, index_path, manifest, limit=250, deadline=None, embed=None) -> EngineResult

    Returns:
        {
            "engine": "semantic",
            "status": "OK" | "EMPTY" | "TIMEOUT" | "ERROR" | "UNAVAILABLE",
            "hits": [
                {
                    "id": "skill:example",
                    "rank": 1,
                    "raw_score": 0.85,
                    "evidence": {"chunk_index": 0, "chunk_type": "card"},
                    "equivalence_group_id": null
                }
            ],
            "elapsed_ms": 12.34,
            "reason": null | str,
            "manifest": {...},
            "coverage": {...}
        }
    """
    t0 = time.perf_counter()

    def make_result(
        status: str,
        hits: List[Dict[str, Any]],
        reason: Optional[str] = None,
        coverage: Optional[Dict[str, Any]] = None,
        out_manifest: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        elapsed = round((time.perf_counter() - t0) * 1000, 2)
        return {
            "engine": ENGINE_NAME,
            "status": status,
            "hits": hits,
            "elapsed_ms": elapsed,
            "reason": reason,
            "manifest": out_manifest or manifest or {},
            "coverage": coverage or {},
        }

    # 1. Deadline check before starting work
    if deadline is not None and time.monotonic() >= deadline:
        return make_result("TIMEOUT", [], reason="Deadline exceeded before retrieval started")

    # 2. Query text check
    if not text or not text.strip():
        return make_result("EMPTY", [], reason="Empty query text")

    # 3. Check entries filter if explicitly provided as empty
    if entries is not None and len(entries) == 0:
        return make_result("EMPTY", [], reason="Entries filter provided but empty")

    # 4. Resolve and load index
    try:
        index = get_semantic_index(index_path)
        index_meta = index.get_meta()
    except FileNotFoundError as exc:
        return make_result("UNAVAILABLE", [], reason=f"Index not found: {exc}")
    except Exception as exc:
        return make_result("ERROR", [], reason=f"Failed to access index: {exc}")

    # 5. Validate manifest against index metadata
    try:
        validate_manifest(manifest, index_meta)
    except ManifestValidationError as exc:
        return make_result("UNAVAILABLE", [], reason=f"Manifest validation failed: {exc}")
    except Exception as exc:
        return make_result("ERROR", [], reason=f"Manifest check error: {exc}")

    # 6. Load vector matrix and chunk mappings
    try:
        vectors, chunk_ids, resource_ids, chunk_indices, chunk_types, res_map = index.load_vectors()
    except Exception as exc:
        return make_result("ERROR", [], reason=f"Failed to load vector data: {exc}")

    if len(vectors) == 0:
        return make_result("EMPTY", [], reason="Index contains 0 vector chunks")

    # 7. Check deadline before embedding query
    if deadline is not None and time.monotonic() >= deadline:
        return make_result("TIMEOUT", [], reason="Deadline exceeded before embedding query")

    # 8. Embed query text (only embed query, never re-embed whole corpus!)
    query_vec: Optional[np.ndarray] = None
    try:
        if embed is not None:
            raw_vec = embed(text)
        else:
            raw_vec = embed_bge_m3(text, deadline=deadline)

        if not isinstance(raw_vec, (list, tuple, np.ndarray)):
            return make_result("ERROR", [], reason="Embedding function returned non-array")
        if len(raw_vec) != DEFAULT_DIMENSION:
            return make_result(
                "ERROR",
                [],
                reason=f"Query embedding dimension {len(raw_vec)} != {DEFAULT_DIMENSION}",
            )
        q_arr = np.array(raw_vec, dtype=np.float32)
        if not np.all(np.isfinite(q_arr)):
            return make_result("ERROR", [], reason="Query embedding contains NaN or Inf")
        q_norm = float(np.linalg.norm(q_arr))
        if q_norm == 0:
            return make_result("EMPTY", [], reason="Query embedding norm is zero")
        query_vec = q_arr / q_norm
    except TimeoutError as exc:
        return make_result("TIMEOUT", [], reason=f"Embedding query timed out: {exc}")
    except SemanticRetrievalError as exc:
        return make_result("ERROR", [], reason=str(exc))
    except Exception as exc:
        return make_result("ERROR", [], reason=f"Query embedding failed: {exc}")

    # 9. Compute similarity scores (dot product of normalized vectors = cosine similarity)
    scores = vectors @ query_vec

    # 10. Check deadline before aggregation
    if deadline is not None and time.monotonic() >= deadline:
        return make_result("TIMEOUT", [], reason="Deadline exceeded during scoring")

    # 11. Build eligible filter set from entries
    allowed_ids: Optional[Set[str]] = None
    entries_meta: Dict[str, Dict[str, Any]] = {}
    if entries is not None:
        allowed_ids = set()
        for e in entries:
            if isinstance(e, dict):
                eid = e.get("id") or f"{e.get('type', 'skill')}:{e.get('resource_id', e.get('name', ''))}"
                if eid:
                    allowed_ids.add(eid)
                    entries_meta[eid] = e
            elif isinstance(e, str):
                allowed_ids.add(e)

    # 12. Aggregate chunks to resources (max-pooling best chunk per resource)
    # Mapping: resource_id -> (best_score, chunk_id, chunk_index, chunk_type)
    best_per_resource: Dict[str, Tuple[float, str, int, str]] = {}
    for idx, (res_id, c_id, c_idx, c_type) in enumerate(
        zip(resource_ids, chunk_ids, chunk_indices, chunk_types)
    ):
        # Filter by allowed entries if provided
        if allowed_ids is not None and res_id not in allowed_ids:
            continue

        # Filter out inactive resources if marked in index or entries
        res_info = res_map.get(res_id, {})
        if not res_info.get("active", True):
            continue
        if res_id in entries_meta and not entries_meta[res_id].get("active", True):
            continue

        score = float(scores[idx])
        if res_id not in best_per_resource or score > best_per_resource[res_id][0]:
            best_per_resource[res_id] = (score, c_id, c_idx, c_type)

    if not best_per_resource:
        return make_result(
            "EMPTY",
            [],
            reason="No eligible matching candidates found",
            coverage={"scored_chunks": len(scores), "matching_resources": 0},
        )

    # 13. Equivalence group aggregation (prevent duplicate/chunk alias voting)
    # If equivalence_group_id is present, keep the highest scoring representative
    group_best: Dict[str, Tuple[str, float, str, int, str, Optional[str]]] = {}
    for res_id, (score, c_id, c_idx, c_type) in best_per_resource.items():
        res_info = res_map.get(res_id, {})
        entry_info = entries_meta.get(res_id, {})
        group_id = (
            entry_info.get("equivalence_group_id")
            or res_info.get("equivalence_group_id")
        )
        # Key by group_id if present, else by res_id
        dedup_key = f"group:{group_id}" if group_id else f"res:{res_id}"

        if dedup_key not in group_best or score > group_best[dedup_key][1]:
            group_best[dedup_key] = (res_id, score, c_id, c_idx, c_type, group_id)

    # 14. Rank candidates by score descending (stable tie-break on resource_id)
    sorted_candidates = sorted(
        group_best.values(),
        key=lambda item: (-item[1], item[0]),
    )

    # 15. Formulate hits (rank starts at 1, capped at limit)
    hits = []
    for rank_idx, (res_id, score, c_id, c_idx, c_type, group_id) in enumerate(
        sorted_candidates[:limit], start=1
    ):
        hit = {
            "id": res_id,
            "rank": rank_idx,
            "raw_score": round(score, 6),
            "evidence": {
                "chunk_id": c_id,
                "chunk_index": c_idx,
                "chunk_type": c_type,
            },
            "equivalence_group_id": group_id,
        }
        hits.append(hit)

    coverage = {
        "indexed_chunks": len(vectors),
        "indexed_resources": len(res_map),
        "eligible_resources": len(allowed_ids) if allowed_ids is not None else len(res_map),
        "matched_resources": len(best_per_resource),
        "unique_groups": len(group_best),
        "returned_hits": len(hits),
    }

    bound_manifest = {
        "catalog_sha256": index_meta.get("catalog_sha256"),
        "vector_revision": index_meta.get("vector_revision"),
        "embedding_model_digest": index_meta.get("embedding_model_digest", DEFAULT_DIGEST),
        "embedding_dimension": DEFAULT_DIMENSION,
    }

    return make_result("OK", hits, coverage=coverage, out_manifest=bound_manifest)
