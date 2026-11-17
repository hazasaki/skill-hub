#!/usr/bin/env python3
"""Round-04 runner: one worker-only case, one fresh native Codex CLI process.

The case file the worker receives may contain ONLY a request, a workspace, and the
authorized scope. Any other key is refused, so a gold expectation cannot reach the worker
through this path — the isolation is structural, not a convention.

The harness records sanitized metadata: process exit, duration, the thread/session id the
CLI actually reported, an event/item histogram, tool and wrapper items including unnamed
ones, an explicit coverage verdict, and a rehash of the workspace tree. It never stores the
transcript, prompts, arguments or command output.

Label: this is the native Codex CLI. It is NOT OpenBot agent routing.

Observation (parsing, snapshotting, session reading) lives in `skill_replay_observe`; this
module owns the launch, the retry loop and the gate. Public names are re-exported, so
importing them from here still works.

Run: python skill_replay_runner.py --case CASE.json --out DIR [--timeout 900]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

WORKER_KEYS = ('case_id', 'request', 'workspace', 'authorized_scope')
# Any key hinting at judging material means this is not a worker payload.
FORBIDDEN_HINTS = ('gold', 'expect', 'solution', 'answer', 'skill_name', 'source', 'hash',
                   'checks', 'rubric', 'desired', 'target_skill')


def load_case(path):
    """Load a worker-only case; refuse anything that could carry gold expectations."""
    case = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(case, dict):
        raise ValueError('case must be a JSON object')
    extra = sorted(set(case) - set(WORKER_KEYS))
    if extra:
        raise ValueError(f'worker case carries non-worker key(s): {", ".join(extra)}')
    leaked = sorted(key for key in case if any(hint in key.lower() for hint in FORBIDDEN_HINTS))
    if leaked:
        raise ValueError(f'worker case names judging material: {", ".join(leaked)}')
    for field in ('case_id', 'request', 'workspace'):
        if not isinstance(case.get(field), str) or not case[field].strip():
            raise ValueError(f'{field} must be a nonempty string')
    return case


def build_prompt(case):
    """Worker brief: the request, a write boundary, and genuine installed-skill discovery.

    Reads stay open on purpose: forbidding them would make "use the skills normally
    installed on this machine" impossible to carry out.
    """
    scope = case.get('authorized_scope') or 'the working directory you were given'
    return (f'{case["request"]}\n\n'
            f'Your working directory is the folder you were started in. '
            f'Write only inside it ({scope}). '
            f'You may read your normal installed skills and their native config so that '
            f'your usual skill discovery works; do not read other session data, '
            f'credentials or anything outside your normal tooling.\n'
            f'Use the skills normally installed on this machine if any apply; '
            f'pick whatever you judge appropriate.\n'
            f'When finished, reply with a one-line summary of what you created.')




# Observation half lives in `skill_replay_observe` (round 12 split); re-exported so every
# existing caller and the six external consumers keep importing from here unchanged.
from skill_replay_observe import (classify_stderr, collect_native_session, compare_trees,
                                  snapshot_dirs, snapshot_tree, summarize_events, tool_coverage)


def build_command(workspace, executable=None):
    """Default native configuration: no model, sandbox, approval or config overrides.

    Windows resolves `codex` to a `.CMD` shim, which CreateProcess cannot run directly, so
    the executable is resolved first and the resolved path is what gets reported.
    """
    exe = executable or shutil.which('codex') or 'codex'
    return [exe, 'exec', '--json', '--skip-git-repo-check', '--cd', str(workspace), '-']


def sanitized_argv(argv, workspace):
    """The real argv with the workspace path and any home path folded to placeholders."""
    workspace_text = str(Path(workspace))
    home = str(Path.home())
    cleaned = []
    for argument in argv:
        text = str(argument).replace(workspace_text, '<workspace>').replace(home, '~')
        cleaned.append(text)
    return cleaned






def execution_preflight(native, exit_code, requires_write=False):
    """A zero process exit cannot override measured native policy rejection."""
    if native.get('policy_rejection_count'):
        return {'status': 'blocked', 'reason': 'measured_native_policy_rejection', 'stop_campaign': True}
    if requires_write and any(c.get('sandbox_policy', {}).get('type') == 'read-only'
                              for c in native.get('contexts', [])):
        return {'status': 'blocked', 'reason': 'required_write_native_read_only', 'stop_campaign': True}
    if exit_code != 0:
        return {'status': 'blocked', 'reason': 'native_process_failed', 'stop_campaign': True}
    if native.get('hub_surface_observed') is not True:
        return {'status': 'blocked', 'reason': 'child_surface_missing_skill_hub',
                'stop_campaign': True,
                'retryable': native.get('coverage') == 'observed_partial'
                             and native.get('hub_surface_observed') is False}
    return {'status': 'unknown', 'reason': 'capability_not_proven',
            'stop_campaign': native.get('coverage') != 'observed_partial'}


def run_case(case, out_dir, timeout=900, command=None, requires_write=False):
    """Run one case in a fresh process; return sanitized metadata and the artifact tree."""
    workspace = Path(case['workspace']).resolve()
    if not workspace.is_dir():
        raise ValueError(f'workspace does not exist: {workspace}')
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    before = snapshot_tree(workspace)
    argv = command or build_command(workspace)
    env = dict(os.environ, PYTHONUTF8='1')
    started = time.time()
    timed_out = False
    try:
        done = subprocess.run(argv, input=build_prompt(case), capture_output=True, text=True,
                              timeout=timeout, env=env, encoding='utf-8', errors='replace',
                              cwd=str(workspace))
        exit_code, stdout = done.returncode, done.stdout
        stderr_meta = classify_stderr(done.stderr)
    except subprocess.TimeoutExpired as expired:
        exit_code, timed_out = None, True
        stdout = expired.stdout or ''
        stdout = stdout.decode('utf-8', 'replace') if isinstance(stdout, bytes) else stdout
        stderr_meta = classify_stderr('timeout')
    except (FileNotFoundError, OSError) as error:
        # Report the class only: the message can carry a local path.
        return {'case_id': case['case_id'], 'runner_label': 'native Codex CLI',
                'status': 'blocked', 'blocker': type(error).__name__,
                'resolved_executable': Path(str(argv[0])).name if argv else None,
                'note': 'Codex CLI is a separate native host, not OpenBot agent routing.'}
    duration = round(time.time() - started, 3)
    events = summarize_events(stdout.splitlines())
    thread_id = events['thread_ids'][0] if len(events['thread_ids']) == 1 else None
    native = collect_native_session(thread_id, started, env)
    preflight = execution_preflight(native, exit_code, requires_write)
    after = snapshot_tree(workspace)
    diff = compare_trees(before, after)
    report = {
        'schema_version': 2,
        'started_at': datetime.fromtimestamp(started, timezone.utc).isoformat(),
        'ended_at': datetime.now(timezone.utc).isoformat(),
        'native_observation': native,
        'execution_preflight': preflight,
        'status': preflight['status'],
        'blocker': preflight['reason'] if preflight['status'] == 'blocked' else None,
        'case_id': case['case_id'],
        'runner_label': 'native Codex CLI',
        'resolved_executable': Path(str(argv[0])).name if argv else None,
        'argv': sanitized_argv(argv, workspace),
        'exit_code': exit_code,
        'timed_out': timed_out,
        'duration_seconds': duration,
        'stderr': stderr_meta,
        'fresh_process': True,
        'thread_id': events['thread_ids'][0] if events['thread_ids'] else None,
        'thread_id_count': len(events['thread_ids']),
        'model_reported': events['models'],
        'event_types': events['event_types'],
        'item_types': events['item_types'],
        'tool_calls': events['tool_calls'],
        'tool_coverage': tool_coverage(events),
        'skill_identity_hashes': events['identity_hashes'],
        'unparsed_event_lines': events['unparsed_lines'],
        'artifacts': diff | {'empty_directories': snapshot_dirs(workspace)},
        'workspace_file_count': len(after),
        'limits': [
            'Native Codex CLI; proves this CLI ran a case, never OpenBot agent routing.',
            'A wrapper name is what the CLI ran, not a nested tool it was observed to call.',
            'No identity hash means unobserved, not "no skill was used".',
            'Transcript, prompts, arguments and command output are not stored.',
            'Identity of any skill the worker chose is not inferred from artifacts.',
        ],
    }
    (out / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    return report


def run_case_with_retries(case, out_dir, timeout=900, requires_write=False, max_attempts=3):
    """Bounded availability retries, preserving every attempt and its isolated artifacts.

    Opt-in for future orchestration: historical callers/workspaces are unchanged.
    No attempt is a skill verdict; a surface not observed is not proven absent.
    """
    if type(max_attempts) is not int or not 1 <= max_attempts <= 3:
        raise ValueError('max_attempts must be between 1 and 3')
    source = Path(case['workspace']).resolve(strict=True)
    out = Path(out_dir).resolve()
    if out == source or out.is_relative_to(source) or source.is_relative_to(out):
        raise ValueError('retry output and fixture workspace must be disjoint')
    if not source.is_dir() or any(p.is_symlink() for p in source.rglob('*')):
        raise ValueError('retry fixtures must be a directory without symlinks')
    out.mkdir(parents=True, exist_ok=False)
    fixtures = snapshot_tree(source)
    attempts, seen_threads = [], set()
    for number in range(1, max_attempts + 1):
        if snapshot_tree(source) != fixtures:
            raise ValueError('retry fixture source changed')
        dest = out / f'attempt-{number}'
        workspace = dest / 'workspace'
        shutil.copytree(source, workspace)
        worker = dict(case, workspace=str(workspace))
        report = run_case(worker, dest, timeout=timeout, requires_write=requires_write)
        tid = report.get('thread_id')
        eligible = not report.get('execution_preflight', {}).get('stop_campaign', True)
        if not tid or tid in seen_threads:
            report.update({'status': 'blocked', 'blocker': 'fresh_session_identity_unproven',
                           'execution_preflight': {'stop_campaign': True, 'retryable': False,
                               'reason': 'fresh_session_identity_unproven', 'status': 'blocked'}})
            eligible = False
        if tid:
            seen_threads.add(tid)
        report.update({'attempt': number, 'case_verdict': 'unknown',
                       'eligible_for_case_review': eligible,
                       'workspace_ref': str(workspace)})
        (dest / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        attempts.append({'attempt': number, 'thread_id': tid,
                         'discarded_for_availability': report.get('execution_preflight', {}).get('retryable', False),
                         'eligible_for_case_review': eligible,
                         'reason': report.get('blocker'), 'report_ref': str(dest / 'report.json')})
        summary = {'case_id': case['case_id'], 'case_verdict': 'unknown',
                   'attempts': attempts, 'eligible_attempt': number if eligible else None,
                   'availability_status': 'observed' if eligible else 'blocked',
                   'selection_limit': 'Conditional on output-observed Hub surface; retain all attempts, not an unconditional routing rate.'}
        (out / 'attempts.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
        if eligible or not report.get('execution_preflight', {}).get('retryable', False):
            break
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--timeout', type=int, default=900)
    args = parser.parse_args(argv)
    try:
        case = load_case(args.case)
        report = run_case(case, args.out, args.timeout)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(json.dumps({'error': str(error)}))
        return 2
    print(json.dumps({key: report.get(key) for key in
                      ('case_id', 'status', 'blocker', 'exit_code', 'duration_seconds',
                       'thread_id', 'workspace_file_count')}))
    return 0 if (report.get('exit_code') == 0 and report.get('status') != 'blocked'
                 and not report.get('execution_preflight', {}).get('stop_campaign')) else 1


if __name__ == '__main__':
    sys.exit(main())
