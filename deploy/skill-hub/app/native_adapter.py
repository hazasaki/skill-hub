"""Bind a native OpenBot channel task to the one MCP tool call that answered it.

The host records the chain itself: a channel assignment names the `turnId` it ran in
(`projection_channel_assignments`), the turn names its provider session
(`projection_turns`), and the provider staging holds the tool-call items of that turn
(`provider_history_staging`). This module reads those rows in one consistent read-only
snapshot and emits a receipt keyed by the four identifiers the caller asked for:
`task_id`, `turn_id`, `call_id`, `server_id`.

What the receipt proves and what it does not:

* it proves this host recorded that task, that turn and that tool call, and it carries a
  digest of the result bytes the host stored;
* it does not prove the bytes came from the server. The transport is the provider's, not
  the host's, so `transport_authenticated` is reported `UNKNOWN` and a reader must not
  present this receipt as authenticated transport evidence.

Two things are deliberately refused rather than approximated:

* an external `codex exec` / CLI run has no channel assignment, so it resolves to
  `NOT_NATIVE` instead of being relabelled as a native task. `trace/skill_usage.py` keeps
  serving the legacy CLI/trace path and is not touched here;
* a turn whose recorded status is `running`, absent or unrecognised is `PENDING` or
  `UNKNOWN`, never a terminal verdict. Only `complete`, `failed` and `interrupted` are
  terminal.

Every write here is none: the database is opened `mode=ro` with `query_only`, and no
migration, checkpoint or projection row is touched. Direct task insertion is out of
scope by construction — this module has no write path at all.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path

SCHEMA_VERSION = 1
DEFAULT_DB = Path(os.environ.get('APPDATA', Path.home() / 'AppData/Roaming')) / 'OpenBot/openbot.db'
MCP_TOOL_TYPE = 'mcpToolCall'
# The shape an Antigravity host records instead: no `server` field, the server and tool
# folded into `name` as `server_tool`. Kept a separate constant so the two are never confused.
ANTIGRAVITY_TOOL_TYPE = 'toolCall'
# The recorded vocabulary, mapped to the receipt vocabulary. Anything absent is unknown.
TERMINAL_STATUS = {'completed': 'COMPLETE', 'failed': 'FAILED', 'interrupted': 'INTERRUPTED'}
NON_TERMINAL_STATUS = {'running': 'PENDING', 'queued': 'PENDING'}
TERMINAL = frozenset(TERMINAL_STATUS.values())
_LABEL = re.compile(r'[\w.:/-]{1,180}', re.ASCII)


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def digest(value):
    """Digest of a recorded value under one canonical encoding. A label, not a signature."""
    return hashlib.sha256(canonical_json(value)).hexdigest()


def label(value):
    """An identifier safe to export; free text never leaves this module."""
    return value if isinstance(value, str) and _LABEL.fullmatch(value) else None


def _connect(db):
    db = Path(db).resolve(strict=True)
    con = sqlite3.connect(db.as_uri() + '?mode=ro', uri=True, timeout=3)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA query_only=ON')
    con.execute('BEGIN')
    return con


def _assignments(con, task_id):
    """Every assignment row of this task, in revision order.

    One task is dispatched more than once when it is retried, and each dispatch is its own
    turn. Treating the newest row as *the* turn hid the earlier ones and could report a retry
    as if it were the first attempt, so all of them are kept and ordered.
    """
    rows = con.execute('SELECT rowid, assignment_json FROM projection_channel_assignments '
                       'WHERE task_id=? ORDER BY rowid', (task_id,)).fetchall()
    parsed = []
    for row in rows:
        try:
            value = json.loads(row['assignment_json'])
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict):
            revision = value.get('taskRevision')
            parsed.append(((revision if type(revision) is int else -1, row['rowid']), value))
    return [value for _, value in sorted(parsed, key=lambda pair: pair[0])]


def _item(row):
    try:
        item = json.loads(row['item_json'])
    except (ValueError, TypeError):
        return None
    return item if isinstance(item, dict) else None


def _is_error(item):
    """The recorded error state, or None when the record does not state one.

    Live rows carry `error: null` on a successful call and a non-null value on a failure, so
    a present `error` key is a statement either way. `result.isError` is the MCP-level flag
    and is honoured first when it is present. Absence of both is `None`, not `False`.
    """
    for holder in (item, item.get('result')):
        if isinstance(holder, dict):
            flag = holder.get('isError', holder.get('is_error'))
            if flag is not None:
                return flag is not False
    if isinstance(item.get('result'), dict) and 'isError' in item['result']:
        return False
    if 'error' in item:
        return item['error'] is not None
    return None


def _split_server_tool(name, known_servers):
    """Split a host's `server_tool` tool name into (server, tool), or (None, name).

    A provider that does not emit Codex `_meta` still names the originating MCP server inside
    the tool name itself: Antigravity records `skill-hub_get_skill` as a `toolCall` with no
    `server` field. Splitting on the known server prefix recovers the server, which is what
    makes the call attributable to a server on that host at all.

    The split is conservative: a name with no known prefix, or one whose remainder is empty,
    keeps its whole name and reports no server, because guessing would attribute a call to a
    server that never served it.
    """
    if not isinstance(name, str) or not name:
        return None, name
    for server in known_servers:
        prefix = server + '_'
        if name.startswith(prefix) and len(name) > len(prefix):
            return server, name[len(prefix):]
    return None, name


def _known_servers(con):
    """The MCP servers this host has actually registered, so a prefix split is not invented."""
    try:
        rows = con.execute('SELECT name FROM projection_mcp_servers').fetchall()
    except sqlite3.Error:
        return ()
    names = set()
    for row in rows:
        value = row['name'] if 'name' in row.keys() else row[0]
        if isinstance(value, str) and value:
            names.add(value)
    return sorted(names)


def _find_rollout_file(external_session_id, *, account_id=None, provider=None, appdata_dir=None, codex_home=None):
    if not external_session_id or not _LABEL.fullmatch(external_session_id):
        return None
    if provider != 'codex':
        return None

    if account_id:
        if not _LABEL.fullmatch(account_id):
            return None
        accounts_root = Path(appdata_dir) if appdata_dir else (
            Path(os.environ.get('APPDATA', Path.home() / 'AppData/Roaming')) / 'OpenBot/provider-accounts'
        )
        account_dir = accounts_root / account_id
        if not account_dir.is_dir():
            return None
        matches = list(account_dir.glob(f'**/rollout-*-{external_session_id}.jsonl'))
        if len(matches) == 1:
            return matches[0]
        # Ambiguous/duplicate (>1) or missing (0): never scan another account as fallback.
        return None

    # Standalone lookup requires an explicitly supplied sessions root.
    if codex_home is None:
        return None
    codex_sessions = Path(codex_home)
    if codex_sessions.is_dir():
        matches = list(codex_sessions.glob(f'**/rollout-*-{external_session_id}.jsonl'))
        if len(matches) == 1:
            return matches[0]
        return None

    return None


def _rollout_items(con, turn_id):
    try:
        t_row = con.execute('SELECT thread_id, provider_session_id FROM projection_turns WHERE turn_id=?',
                            (turn_id,)).fetchone()
    except sqlite3.Error:
        return []
    if not t_row:
        return []
    thread_id = (t_row['thread_id'] if hasattr(t_row, 'keys') else t_row[0]) if t_row else None
    session_id = (t_row['provider_session_id'] if hasattr(t_row, 'keys') else t_row[1]) if t_row else None
    if not session_id:
        return []
    try:
        s_row = con.execute('SELECT provider, external_session_id FROM projection_provider_sessions WHERE id=?',
                            (session_id,)).fetchone()
    except sqlite3.Error:
        return []
    if not s_row:
        return []
    provider = (s_row['provider'] if hasattr(s_row, 'keys') else s_row[0])
    ext_id = (s_row['external_session_id'] if hasattr(s_row, 'keys') else s_row[1])
    if not ext_id:
        return []

    owners = set()
    try:
        if thread_id:
            for row in con.execute('SELECT agent_id FROM projection_threads WHERE thread_id=?', (thread_id,)):
                if row[0]:
                    owners.add(row[0])
        for row in con.execute('SELECT assignment_json FROM projection_channel_assignments'):
            data = json.loads(row[0])
            if data.get('turnId') == turn_id:
                if not data.get('agentId'):
                    return []
                owners.add(data['agentId'])
        if len(owners) != 1 or provider != 'codex':
            return []
        rows = con.execute('SELECT agent_json FROM projection_agents WHERE agent_id=?',
                           (next(iter(owners)),)).fetchall()
        if len(rows) != 1:
            return []
        agent = json.loads(rows[0][0])
        account_id = agent.get('providerAccountId')
        if not account_id or agent.get('provider') != 'codex':
            return []
    except (sqlite3.Error, ValueError, TypeError, AttributeError):
        return []

    rollout_path = _find_rollout_file(ext_id, account_id=account_id, provider=provider)
    if not rollout_path:
        return []
    items = []
    try:
        with open(rollout_path, 'r', encoding='utf-8') as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                except (ValueError, TypeError):
                    continue
                payload = data.get('payload') if isinstance(data, dict) else None
                if not isinstance(payload, dict):
                    continue
                if payload.get('turn_id') != turn_id or payload.get('type') != 'item_completed':
                    continue
                raw_item = payload.get('item')
                if not isinstance(raw_item, dict):
                    continue
                itype = raw_item.get('type')
                if itype == 'McpToolCall':
                    call_item = {
                        'type': MCP_TOOL_TYPE,
                        'id': raw_item.get('id'),
                        'server': raw_item.get('server'),
                        'tool': raw_item.get('tool'),
                        'arguments': raw_item.get('arguments'),
                        'status': raw_item.get('status'),
                        'result': raw_item.get('result'),
                    }
                    if 'error' in raw_item:
                        call_item['error'] = raw_item['error']
                    items.append(call_item)
                elif itype == 'CommandExecution':
                    cmd = raw_item.get('command')
                    if isinstance(cmd, list) and len(cmd) >= 3 and cmd[1].lower() in ('-command', '-c'):
                        cmd_str = cmd[2]
                    elif isinstance(cmd, list):
                        cmd_str = ' '.join(cmd)
                    else:
                        cmd_str = str(cmd or '')
                    cwd = raw_item.get('cwd') or ''
                    if isinstance(cwd, str) and cwd.startswith('file:///'):
                        cwd = cwd[8:]
                        if cwd and cwd[0] == '/' and len(cwd) > 2 and cwd[2] == ':':
                            cwd = cwd[1:]
                    cmd_item = {
                        'type': 'commandExecution',
                        'id': raw_item.get('id'),
                        'command': cmd_str,
                        'status': raw_item.get('status'),
                        'cwd': cwd,
                    }
                    exit_code = raw_item.get('exit_code') if 'exit_code' in raw_item else raw_item.get('exitCode')
                    if type(exit_code) is int:
                        cmd_item['exitCode'] = exit_code
                    items.append(cmd_item)
    except (OSError, UnicodeDecodeError):
        return []
    return items


def _calls(con, turn_id, wanted=None):
    """MCP tool-call items of one turn, in recorded order, with no body or arguments exported.

    Two recorded shapes are accepted, and they are not equivalent:

    * `mcpToolCall` — the host recorded a server and a tool separately, and the provider may
      also have emitted native ids. This is the strong shape.
    * `toolCall` whose name carries a known `server_tool` prefix — the shape an Antigravity
      host produces. The server and tool are recovered from the name, and `identity_source`
      says so, because the name is the only evidence there is.
    """
    servers = _known_servers(con)
    rows = con.execute('SELECT item_index, item_json FROM provider_history_staging '
                       'WHERE turn_id=? ORDER BY item_index', (turn_id,)).fetchall()
    raw_items = []
    if rows:
        for row in rows:
            it = _item(row)
            if it is not None:
                raw_items.append((row['item_index'], it))
    else:
        return _call_records(enumerate(_rollout_items(con, turn_id)), servers,
                             result_source='provider_rollout')

    calls = _call_records(raw_items, servers)
    matching = [call for call in calls if call['tool_name'] == wanted]
    if wanted is None or (matching and matching[-1]['result_is_payload']):
        return calls

    rollout = _call_records(enumerate(_rollout_items(con, turn_id)), servers,
                            result_source='provider_rollout')
    originals = [call for call in rollout
                 if call['tool_name'] == wanted and call['call_id'] is not None]
    if not matching:
        return calls + originals
    staged = matching[-1]
    exact = [call for call in originals if call['call_id'] == staged['call_id']
             and call['server_id'] == staged['server_id']]
    # A staging summary can borrow only the payload of the same recorded call.
    if (len(exact) == 1 and exact[0]['result_is_payload']
            and exact[0]['isError'] is staged['isError']
            and exact[0]['recorded_status'] == staged['recorded_status']):
        staged['result_sha256'] = exact[0]['result_sha256']
        staged['result_is_payload'] = True
        staged['result_source'] = 'provider_rollout'
    return calls


def _call_records(raw_items, servers, *, result_source='provider_history_staging'):
    calls, seen = [], set()
    for index, item in raw_items:
        kind = item.get('type')
        if kind == MCP_TOOL_TYPE:
            server = label(item.get('server'))
            tool = label(item.get('tool') or item.get('name'))
            source = 'recorded_server_field'
        elif kind == ANTIGRAVITY_TOOL_TYPE:
            server, tool = _split_server_tool(item.get('name'), servers)
            if server is None:
                continue
            tool = label(tool)
            source = 'tool_name_prefix'
        else:
            continue
        if index in seen:
            # Two rows for one index is an ambiguous record, not a second call.
            continue
        seen.add(index)
        result = item.get('result')
        calls.append({'item_index': index,
                      'call_id': label(item.get('id')),
                      'server_id': server,
                      'tool_name': tool,
                      'identity_source': source,
                      'recorded_status': label(item.get('status')),
                      'isError': _is_error(item),
                      # A summary string is not a result payload; its digest would look like
                      # one, so it is withheld and the receipt says why.
                      'result_sha256': digest(result) if isinstance(result, (dict, list)) else None,
                      'result_is_payload': isinstance(result, (dict, list)),
                      'result_source': result_source if isinstance(result, (dict, list)) else None})
    return calls


def _turn(con, turn_id):
    return con.execute('SELECT thread_id, provider_session_id, status FROM projection_turns '
                       'WHERE turn_id=?', (turn_id,)).fetchone()


def _provider_turn(con, turn_id):
    return con.execute('SELECT turn_status, complete FROM provider_history_turns '
                       'WHERE turn_id=?', (turn_id,)).fetchone()


def _terminal(row, provider):
    """Terminal status from the recorded vocabulary, with the source that decided it."""
    candidates = [('projection_turns', row['status'] if row is not None else None)]
    if provider is not None:
        candidates.append(('provider_history_turns', provider['turn_status']))
    decided = [(source, value) for source, value in candidates if isinstance(value, str) and value]
    for source, value in decided:
        if value in TERMINAL_STATUS:
            return TERMINAL_STATUS[value], source
    for source, value in decided:
        if value in NON_TERMINAL_STATUS:
            return NON_TERMINAL_STATUS[value], source
    return 'UNKNOWN', None


def task_turn(db, task_id):
    """Every turn this native channel task ran in, oldest revision first.

    A CLI run never gets here: it has no channel task and no assignment naming a turn, so it
    cannot be relabelled as native by passing its own identifier.
    """
    task_id = label(task_id)
    if task_id is None:
        raise ValueError('task id must be an identifier')
    with closing(_connect(db)) as con:
        declaration = con.execute('SELECT task_json FROM projection_channel_tasks WHERE task_id=?',
                                  (task_id,)).fetchone()
        turns = []
        for assignment in _assignments(con, task_id):
            turn_id = label(assignment.get('turnId'))
            row = _turn(con, turn_id) if turn_id else None
            if row is None:
                continue
            turns.append({'turn_id': turn_id, 'channel_id': label(assignment.get('channelId')),
                          'agent_id': label(assignment.get('agentId')),
                          'thread_id': label(row['thread_id']),
                          'provider_session_id': label(row['provider_session_id']),
                          'assignment_state': label(assignment.get('state')),
                          'task_revision': assignment.get('taskRevision')
                          if type(assignment.get('taskRevision')) is int else None})
        if declaration is None or not turns:
            return {'task_id': task_id, 'status': 'NOT_NATIVE', 'turns': [], 'turn_id': None,
                    'reason': 'no channel assignment naming a turn',
                    'limits': ['A CLI or harness run has no channel task; it is not native evidence.']}
        owner = None
        try:
            task = json.loads(declaration['task_json'])
        except (ValueError, TypeError):
            task = None
        if isinstance(task, dict):
            owner = label(task.get('ownerAgentId'))
        latest = turns[-1]
        return {'task_id': task_id, 'status': 'NATIVE', 'turns': turns, **latest,
                'owner_agent_id': owner, 'turn_count': len(turns)}


def terminal_receipt(db, task_id, tool_name=None):
    """One receipt for this task's terminal state and the tool call that answered it.

    The task's state is the highest-revision turn's state, because a retry supersedes the
    attempt it replaced. The call is looked up across every turn the task ran: a retried task
    often makes its tool calls in the first attempt and concludes in a later one, so searching
    only the last turn reported `call_id: null` for a task that did record the call.

    `terminal_status` is `COMPLETE`, `FAILED`, `INTERRUPTED`, `PENDING` or `UNKNOWN`.
    `result_sha256` digests the result value the host recorded; it is not a server signature.
    When `tool_name` is given, the call must be the newest recorded call of that name; when
    the task recorded no call of that name the receipt says so rather than picking another.
    """
    chain = task_turn(db, task_id)
    if chain['status'] == 'NOT_NATIVE':
        # Same field set as a resolved receipt, all empty: a reader that iterates receipts must
        # not have to special-case this shape, and an empty field never reads as a verdict.
        return {**chain, 'schema_version': SCHEMA_VERSION, 'terminal_status': 'NOT_NATIVE',
                'terminal_status_source': None, 'call_id': None, 'server_id': None,
                'tool_name': label(tool_name) if tool_name is not None else None,
                'isError': None, 'result_sha256': None, 'recorded_status': None,
                'result_source': None,
                'is_terminal': False, 'transport_authenticated': 'UNKNOWN'}
    wanted = label(tool_name) if tool_name is not None else None
    with closing(_connect(db)) as con:
        row = _turn(con, chain['turn_id'])
        provider = _provider_turn(con, chain['turn_id'])
        calls = []
        for turn in chain['turns']:
            for call in _calls(con, turn['turn_id'], wanted):
                calls.append({**call, 'turn_id': turn['turn_id']})
    if wanted is not None:
        matching = [call for call in calls if call['tool_name'] == wanted]
        call = matching[-1] if matching else None
    else:
        call = calls[-1] if calls else None
    status, source = _terminal(row, provider)
    receipt = {'schema_version': SCHEMA_VERSION, 'status': 'NATIVE', 'task_id': chain['task_id'],
               'turn_id': call['turn_id'] if call else chain['turn_id'],
               'terminal_turn_id': chain['turn_id'], 'turn_count': chain['turn_count'],
               'turns': [turn['turn_id'] for turn in chain['turns']],
               'channel_id': chain['channel_id'], 'agent_id': chain['agent_id'],
               'thread_id': chain['thread_id'], 'provider_session_id': chain['provider_session_id'],
               'call_id': call['call_id'] if call else None,
               'server_id': call['server_id'] if call else None,
               'tool_name': call['tool_name'] if call else wanted,
               'identity_source': call['identity_source'] if call else None,
               'terminal_status': status, 'terminal_status_source': source,
               'isError': call['isError'] if call else None,
               'result_sha256': call['result_sha256'] if call else None,
               'result_is_payload': call['result_is_payload'] if call else None,
               'result_source': call['result_source'] if call else None,
               'recorded_status': call['recorded_status'] if call else None,
               'is_terminal': status in TERMINAL,
               'transport_authenticated': 'UNKNOWN',
               'call_selection': ('newest call named ' + wanted) if wanted else 'newest recorded call',
               'limits': ['Result digest covers the value this host recorded, not a server signature.',
                          'Host reads no provider-to-server JSON-RPC; transport stays UNKNOWN.']}
    if wanted is not None and call is None:
        receipt['reason'] = 'no recorded call named that tool in this task'
    return receipt


RESOURCE_HASH_KEYS = frozenset({
    'returned_body_sha256',
    'content_sha256',
    'body_sha256',
    'pinned_content_sha256',
    'result_sha256',
})
EVALUATION_VERDICTS = frozenset({'PASS', 'FAIL', 'UNKNOWN'})


def _find_call_payload(db, receipt):
    call_id = receipt.get('call_id')
    if not call_id:
        return None
    turns = []
    if receipt.get('turn_id'):
        turns.append(receipt['turn_id'])
    for t in receipt.get('turns') or []:
        if t not in turns:
            turns.append(t)
    try:
        with closing(_connect(db)) as con:
            for turn_id in turns:
                rows = con.execute(
                    'SELECT item_json FROM provider_history_staging WHERE turn_id=? ORDER BY item_index',
                    (turn_id,)
                ).fetchall()
                for row in rows:
                    item = _item(row)
                    if item and label(item.get('id')) == call_id:
                        res = item.get('result')
                        if isinstance(res, (dict, list)):
                            return res
                        if isinstance(res, str):
                            try:
                                parsed = json.loads(res)
                                if isinstance(parsed, (dict, list)):
                                    return parsed
                            except (ValueError, TypeError):
                                pass
                for item in _rollout_items(con, turn_id):
                    if item and label(item.get('id')) == call_id:
                        res = item.get('result')
                        if res is not None:
                            return res
    except (sqlite3.Error, OSError):
        return None
    return None


def _extract_hashes(payload, extra_sha256=None):
    found = set()
    if extra_sha256 and isinstance(extra_sha256, str):
        found.add(extra_sha256.strip().lower())

    def _walk(val):
        if isinstance(val, dict):
            for k, v in val.items():
                if k in RESOURCE_HASH_KEYS and isinstance(v, str) and v.strip():
                    found.add(v.strip().lower())
                _walk(v)
        elif isinstance(val, list):
            for item in val:
                _walk(item)
        elif isinstance(val, str):
            val_stripped = val.strip()
            if (val_stripped.startswith('{') and val_stripped.endswith('}')) or (
                val_stripped.startswith('[') and val_stripped.endswith(']')
            ):
                try:
                    nested = json.loads(val_stripped)
                    _walk(nested)
                except (ValueError, TypeError):
                    pass

    if payload is not None:
        _walk(payload)

    return found


def bind_evidence_chain(db, task_id, *, tool_name=None, approved_resource_sha256=None,
                        execution_output_sha256=None, evaluation=None):
    """Link the full evidence chain: task, turn, call, approved resource hash, output hash, evaluation.

    Returns a clean dictionary of verified evidence identifiers and bindings.
    """
    receipt = terminal_receipt(db, task_id, tool_name=tool_name)
    chain_verified = True

    # Native task status check: NOT_NATIVE or non-terminal -> False
    if receipt.get('status') == 'NOT_NATIVE' or not receipt.get('is_terminal'):
        chain_verified = False

    # Approved resource hash binding
    if approved_resource_sha256 is not None:
        if not isinstance(approved_resource_sha256, str) or not approved_resource_sha256.strip():
            resource_binding = 'MISMATCHED'
            chain_verified = False
        else:
            call_payload = _find_call_payload(db, receipt)
            extracted_hashes = _extract_hashes(call_payload, receipt.get('result_sha256'))
            if approved_resource_sha256.strip().lower() in extracted_hashes:
                resource_binding = 'MATCHED'
            else:
                resource_binding = 'MISMATCHED'
                chain_verified = False
    else:
        resource_binding = 'UNCHECKED'

    # Execution output hash binding
    if execution_output_sha256 is not None:
        if not isinstance(execution_output_sha256, str) or not execution_output_sha256.strip():
            output_binding = 'MISMATCHED'
            chain_verified = False
        else:
            rec_sha = receipt.get('result_sha256')
            if rec_sha and rec_sha.lower() == execution_output_sha256.strip().lower():
                output_binding = 'MATCHED'
            else:
                output_binding = 'MISMATCHED'
                chain_verified = False
    else:
        output_binding = 'UNCHECKED'

    # Evaluation binding check
    if evaluation is None:
        evaluation_binding = 'UNKNOWN'
        chain_verified = False
    elif not (
        isinstance(evaluation, dict)
        and 'verdict' in evaluation
        and 'reviewer_id' in evaluation
        and 'axes' in evaluation
        and isinstance(evaluation['verdict'], str)
        and evaluation['verdict'] in EVALUATION_VERDICTS
        and len(evaluation['verdict'].strip()) > 0
        and isinstance(evaluation['reviewer_id'], str)
        and len(evaluation['reviewer_id'].strip()) > 0
        and isinstance(evaluation['axes'], dict)
    ):
        evaluation_binding = 'INVALID'
        chain_verified = False
    else:
        executor = receipt.get('agent_id')
        reviewer = evaluation.get('reviewer_id')
        if not executor or not reviewer:
            evaluation_binding = 'UNVERIFIED'
            chain_verified = False
        elif reviewer == executor:
            evaluation_binding = 'REJECTED_SELF_REVIEW'
            chain_verified = False
        elif not evaluation.get('call_id') or not evaluation.get('output_sha256'):
            evaluation_binding = 'UNVERIFIED'
            chain_verified = False
        elif evaluation.get('call_id') != receipt.get('call_id'):
            evaluation_binding = 'MISMATCHED_CALL'
            chain_verified = False
        elif (
            not receipt.get('result_sha256')
            or not isinstance(evaluation.get('output_sha256'), str)
            or evaluation.get('output_sha256').strip().lower() != receipt.get('result_sha256').strip().lower()
        ):
            evaluation_binding = 'MISMATCHED_OUTPUT'
            chain_verified = False
        elif evaluation.get('task_id') and evaluation.get('task_id') != receipt.get('task_id'):
            evaluation_binding = 'MISMATCHED_TASK'
            chain_verified = False
        elif evaluation.get('turn_id') and evaluation.get('turn_id') != receipt.get('turn_id'):
            evaluation_binding = 'MISMATCHED_TURN'
            chain_verified = False
        elif evaluation.get('verdict') in ('PASS', 'FAIL'):
            evaluation_binding = 'VERIFIED'
            if evaluation.get('verdict') != 'PASS':
                chain_verified = False
        else:
            evaluation_binding = 'UNVERIFIED'
            chain_verified = False

    # Set chain_verified = True ONLY when:
    # 1. task is NATIVE
    # 2. terminal_status is 'COMPLETE'
    # 3. call_id is present
    # 4. resource_binding is strictly 'MATCHED' (UNCHECKED must NEVER satisfy chain_verified)
    # 5. output_binding is strictly 'MATCHED' (UNCHECKED must NEVER satisfy chain_verified)
    # 6. evaluation_binding is 'VERIFIED' with verdict == 'PASS'
    if not (
        receipt.get('status') == 'NATIVE'
        and receipt.get('terminal_status') == 'COMPLETE'
        and bool(receipt.get('call_id'))
        and resource_binding == 'MATCHED'
        and output_binding == 'MATCHED'
        and evaluation_binding == 'VERIFIED'
        and isinstance(evaluation, dict)
        and evaluation.get('verdict') == 'PASS'
    ):
        chain_verified = False

    return {
        'task_id': receipt.get('task_id'),
        'turn_id': receipt.get('turn_id'),
        'call_id': receipt.get('call_id'),
        'server_id': receipt.get('server_id'),
        'tool_name': receipt.get('tool_name'),
        'channel_id': receipt.get('channel_id'),
        'agent_id': receipt.get('agent_id'),
        'status': receipt.get('status'),
        'terminal_status': receipt.get('terminal_status'),
        'chain_verified': chain_verified,
        'resource_binding': resource_binding,
        'approved_resource_sha256': approved_resource_sha256,
        'output_binding': output_binding,
        'execution_output_sha256': execution_output_sha256,
        'result_sha256': receipt.get('result_sha256'),
        'evaluation_binding': evaluation_binding,
        'evaluation': evaluation if evaluation_binding == 'VERIFIED' else None,
        'receipt': receipt,
    }


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description='Bind an OpenBot native task to its tool call.')
    parser.add_argument('--task', required=True)
    parser.add_argument('--tool', default=None)
    parser.add_argument('--db', type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        receipt = terminal_receipt(args.db or DEFAULT_DB, args.task, args.tool)
    except (OSError, sqlite3.Error, ValueError) as error:
        print(json.dumps({'error': f'{type(error).__name__}: {error}'}))
        return 2
    print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
