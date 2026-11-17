"""Deterministic fixture materialization for round-03 replay cases.

Fixed stdlib operations only: every fixture is a declared ``mkdir``/``copy_tree``/
``write_text`` step from this module's own tables. No arbitrary command string from a
JSON file is ever executed — the v1 set's ``create.command`` strings are deliberately not
carried over, because running them would mean executing data as code.

Frozen inputs live under ``replay-round03/fixtures`` and never change during a replay.
A case workspace is built by copying from there, so a replay input cannot depend on live
library state.
"""
from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

FIXTURE_ROOT = Path(__file__).resolve().parent / "replay-round03" / "fixtures"


class FixtureError(ValueError):
    """A fixture spec is malformed or points outside the frozen fixture tree."""


def _safe_rel(rel):
    """Reject absolute paths and traversal; fixtures may only name paths inside the tree."""
    if not isinstance(rel, str) or not rel:
        raise FixtureError("fixture path must be a nonempty string")
    p = Path(rel)
    if p.is_absolute() or ".." in p.parts:
        raise FixtureError(f"fixture path escapes the fixture tree: {rel!r}")
    return p


def _frozen(*parts):
    """Resolve a path inside the frozen tree, refusing to leave it."""
    target = FIXTURE_ROOT.joinpath(*parts)
    if not target.exists():
        raise FixtureError(f"missing frozen fixture: {'/'.join(parts)}")
    return target


_ACTIONS = {
    # copy a frozen directory into the workspace as <dest>
    "copy_tree": lambda ws, src, dest: shutil.copytree(_frozen(*_safe_rel(src).parts),
                                                       ws / _safe_rel(dest), dirs_exist_ok=True),
    # copy one frozen file into the workspace at <dest>
    "copy_file": lambda ws, src, dest: _copy_file(ws, src, dest),
    # literal content; `content` is data, never interpreted
    "write_text": lambda ws, dest, content: _write_text(ws, dest, content),
    # pre-existing output directory that a case must NOT overwrite
    "mkdir": lambda ws, dest: (ws / _safe_rel(dest)).mkdir(parents=True, exist_ok=True),
}


def _copy_file(ws, src, dest):
    dst = ws / _safe_rel(dest)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(_frozen(*_safe_rel(src).parts), dst)


def _write_text(ws, dest, content):
    if not isinstance(content, str):
        raise FixtureError("write_text content must be a string")
    dst = ws / _safe_rel(dest)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(content, encoding="utf-8", newline="")


def materialize(workspace, steps):
    """Apply a list of fixture steps into ``workspace`` (created if absent).

    Each step is ``{"op": <one of _ACTIONS>, ...}``. Anything else raises FixtureError,
    including a step that carries an executable command field.
    """
    ws = Path(workspace)
    if ws.exists() and any(ws.iterdir()):
        raise FixtureError(f"workspace is not empty: {ws}")
    ws.mkdir(parents=True, exist_ok=True)
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            raise FixtureError(f"step {index} must be an object")
        if "command" in step:
            raise FixtureError(
                f"step {index} carries a 'command' field; fixture steps are data only, "
                "never executed")
        op = step.get("op")
        action = _ACTIONS.get(op)
        if action is None:
            raise FixtureError(f"step {index}: unknown op {op!r}; allowed: {sorted(_ACTIONS)}")
        args = {k: v for k, v in step.items() if k != "op"}
        allowed = {"copy_tree": {"src", "dest"}, "copy_file": {"src", "dest"},
                   "write_text": {"dest", "content"}, "mkdir": {"dest"}}[op]
        extra = set(args) - allowed
        if extra:
            raise FixtureError(f"step {index}: unexpected field(s) {sorted(extra)} for {op}")
        try:
            action(ws, **args)
        except TypeError as error:
            raise FixtureError(f"step {index}: {error}") from None
    return ws


def worker_payload(case):
    """Reduce a full case to what a replay worker may see.

    Keeps only the id, the natural request, and the fixture steps. Deliberately drops
    ``skill``, ``source_dir``, expected output, goldens, check lists and any hash — handing
    those over would prime routing and leak the answer.
    """
    return {
        "case_id": case["id"],
        "request": case["request"],
        "fixture_steps": copy.deepcopy(case.get("fixture_steps", [])),
        "workspace_placeholder": "<fresh empty dir assigned by the runner>",
        "scope": "local filesystem only; no network, no external sends",
    }


# Fields that must never appear in a worker payload.
FORBIDDEN_WORKER_KEYS = frozenset({
    "skill", "source_dir", "source_file_sha256", "expected_output", "expected_artifacts",
    "expected_command_shape", "artifact_checks", "golden", "goldens", "body_sha256",
    "returned_body_sha256", "expected", "cheat_sheet",
})


def assert_worker_payload_safe(payload):
    """Raise if a worker payload carries a field that would prime or leak."""
    if not isinstance(payload, dict):
        raise FixtureError("worker payload must be an object")
    leaked = sorted(set(payload) & FORBIDDEN_WORKER_KEYS)
    if leaked:
        raise FixtureError(f"worker payload leaks reviewer-only field(s): {leaked}")
    blob = json.dumps(payload, ensure_ascii=False).lower()
    for token in ("golden", "expected_output", "expected_artifacts", "skill-creator",
                  "skill-installer", "fastmcp/scripts", "source_dir"):
        if token in blob:
            raise FixtureError(f"worker payload text contains {token!r}")
    return payload
