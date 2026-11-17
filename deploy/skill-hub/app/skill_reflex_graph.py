"""R4-C06: the Skill Reflex Graph projection and the ViewModel the standalone UI reads.

A projection, never a producer. Every stage it shows was already recorded by the pipeline that
did the work; this module joins those records and refuses to fill a gap. The stage vocabulary
is fixed and each stage is a *separate* field of a node, because collapsing them is exactly
the mistake the graph exists to prevent:

    candidate -> proposed -> selected -> loaded -> applied -> evaluated

A candidate that was offered is not a proposal; a proposal that was selected need not have been
loaded; a skill that was loaded need not have been applied; and an applied skill whose output
was never evaluated has no verdict. Every one of those gaps is a distinct state and is rendered
as `UNKNOWN`, with the stage that stopped it named in `unknown_reason`.

Stable identity. A node is keyed by its candidate identity and version, so replaying the same
fixtures, or re-projecting the same records, yields the same node ids. A projection that
re-keyed nodes per run would make two screenshots of one graph incomparable.

Sensitive data. This module never copies raw context. A node carries `state_sha256` only, and
`redact()` is applied to any free text that reaches an exported field.
"""
from __future__ import annotations

import hashlib
import json
import re

SCHEMA_VERSION = 'openbot.r4.skill_reflex_graph.v1'
STAGES = ('candidate', 'proposed', 'selected', 'loaded', 'applied', 'evaluated')
# Ordered weakest to strongest. A stage may only be claimed at or above the strongest evidence
# recorded for that node, which is what makes a jump a defect rather than a shortcut.
STAGE_RANK = {stage: rank for rank, stage in enumerate(STAGES)}
UNKNOWN = 'UNKNOWN'

EVIDENCE_FOR_STAGE = ('shortlist', 'system1_answer', 'resolver', 'load_receipt',
                      'application_evidence', 'evaluation_receipt')
MAX_TEXT = 280

_EMAIL = re.compile(r'[\w.+-]+@[\w-]+\.[\w.]+')
_BEARER = re.compile(r'\b(?:Bearer|Api-Key|sk-)\s*[A-Za-z0-9._\-]{8,}', re.IGNORECASE)
_HEXKEY = re.compile(r'\b[0-9a-fA-F]{32,}\b')
_ABS_PATH = re.compile(r'\b[A-Za-z]:\\[^\s"\']+')


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def text_digest(text):
    return hashlib.sha256(str(text).replace('\r\n', '\n').replace('\r', '\n').encode('utf-8')).hexdigest()


def redact(text):
    """Strip the things that must not appear in a graph export, or return None.

    Deliberately blunt: an email, a bearer/api key, a long hex run or an absolute path is
    replaced wholesale. A partial mask would still leak the part it kept.
    """
    if text is None:
        return None
    text = str(text)
    text = _BEARER.sub('[redacted-key]', text)
    text = _EMAIL.sub('[redacted-email]', text)
    text = _HEXKEY.sub('[redacted-token]', text)
    text = _ABS_PATH.sub('[redacted-path]', text)
    return text[:MAX_TEXT] if len(text) > MAX_TEXT else text


def node_id(candidate_type, candidate_id, version):
    """Stable across replays: identity and version, nothing derived from this run."""
    return f'{candidate_type}:{candidate_id}@{version}'


def _pin(candidate):
    for field in ('type', 'id', 'version'):
        if not isinstance(candidate.get(field), str) or not candidate.get(field):
            raise ValueError(f'candidate needs a {field}')
    return candidate


def project(*, shortlist=None, answer=None, resolution=None, load_receipt=None,
            application=None, evaluation=None, scenario_id=None):
    """One graph per resolved request. Missing records leave stages `UNKNOWN`, never filled.

    `shortlist` is a `unified_registry.shortlist()` result, `answer` a normalized answer,
    `resolution` a `decision_resolver.resolve()` result, and the last three are optional
    records from stages that may not have run at all.
    """
    stages, nodes, unknown = {}, [], []

    candidates = (shortlist or {}).get('candidates') or []
    for candidate in candidates:
        _pin(candidate)
        identifier = node_id(candidate['type'], candidate['id'], candidate['version'])
        nodes.append({
            'node_id': identifier,
            'elapsed': 'UNKNOWN',
            'stages': {stage: {'state': UNKNOWN, 'evidence': None, 'reason': 'not recorded'}
                       for stage in STAGES},
        })
    by_id = {node['node_id']: node for node in nodes}

    # candidate: recorded by the shortlist itself.
    for candidate in candidates:
        node = by_id[node_id(candidate['type'], candidate['id'], candidate['version'])]
        node['stages']['candidate'] = {
            'state': 'PRESENT', 'evidence': 'shortlist',
            'content_sha256': candidate.get('content_sha256'),
            'bundle_sha256': candidate.get('bundle_sha256'),
            'prompt': candidate.get('prompt'),
            'reason': None}

    if not nodes:
        unknown.append({'stage': 'candidate', 'reason': 'the catalog offered no candidate'})

    # proposed: the answer named it, whether or not it survived resolution.
    if isinstance(answer, dict) and isinstance(answer.get('answers'), dict):
        named = set()
        if answer.get('selection_mode') == 'multi':
            named = {name for name, item in answer['answers'].items() if item.get('noul') is not None}
        else:
            named = {item.get('choice') for item in answer['answers'].values()
                     if isinstance(item, dict) and item.get('choice')}
        for node in nodes:
            candidate_id = node['node_id'].split(':', 1)[1].rsplit('@', 1)[0]
            if candidate_id in named:
                node['stages']['proposed'] = {
                    'state': 'PROPOSED', 'evidence': 'system1_answer',
                    'probability_availability': answer.get('probability_availability'),
                    'reason': None}
        if not any(node['stages']['proposed']['state'] != UNKNOWN for node in nodes):
            unknown.append({'stage': 'proposed',
                            'reason': 'the answer named no candidate in the snapshot'})
    else:
        unknown.append({'stage': 'proposed', 'reason': 'no answer recorded'})

    # selected: only the resolver's own decision marks a node selected.
    if isinstance(resolution, dict):
        if resolution.get('outcome') == 'SELECTED':
            chosen = resolution.get('selected')
            chosen_list = chosen if isinstance(chosen, list) else [chosen]
            for record in chosen_list:
                if not isinstance(record, dict):
                    continue
                identifier = node_id(record['type'], record['id'], record['version'])
                node = by_id.get(identifier)
                if node is None:
                    continue
                node['stages']['selected'] = {
                    'state': 'SELECTED', 'evidence': 'resolver',
                    'confidence': resolution.get('confidence'),
                    'confidence_is_max_probability': resolution.get('confidence_is_max_probability'),
                    'threshold': resolution.get('threshold'),
                    'reason': None}
        else:
            unknown.append({'stage': 'selected', 'reason': resolution.get('reason') or resolution.get('outcome')})
    else:
        unknown.append({'stage': 'selected', 'reason': 'no resolution recorded'})

    # loaded: needs the load receipt's own hashes to match the pin, or it is not the same bytes.
    if isinstance(load_receipt, dict):
        identifier = load_receipt.get('node_id')
        node = by_id.get(identifier)
        if node is None:
            unknown.append({'stage': 'loaded', 'reason': 'load receipt names an unknown node'})
        else:
            candidate = node['stages']['candidate']
            bundle_pin = candidate.get('bundle_sha256')
            matched = (candidate.get('content_sha256') is not None
                       and load_receipt.get('content_sha256') == candidate['content_sha256']
                       and bundle_pin in (None, load_receipt.get('bundle_sha256')))
            node['stages']['loaded'] = {
                'state': 'LOADED' if matched else 'MISMATCH', 'evidence': 'load_receipt',
                'content_sha256': load_receipt.get('content_sha256'),
                'bundle_sha256': load_receipt.get('bundle_sha256'),
                'bundle_pinned': bundle_pin is not None,
                'reason': None if matched else 'served bytes do not match the pinned hashes'}
            if not matched:
                unknown.append({'stage': 'loaded', 'reason': 'served bytes do not match the pin'})
    else:
        unknown.append({'stage': 'loaded', 'reason': 'no load receipt recorded'})

    # applied: an execution reference is required; a load alone never implies application.
    if isinstance(application, dict) and application.get('applied'):
        identifier = application.get('node_id')
        node = by_id.get(identifier)
        if node is None:
            unknown.append({'stage': 'applied', 'reason': 'application names an unknown node'})
        else:
            node['stages']['applied'] = {
                'state': 'APPLIED', 'evidence': 'application_evidence',
                'execution_ref': redact(application.get('execution_ref')),
                'reason': None}
    else:
        unknown.append({'stage': 'applied', 'reason': 'no application evidence recorded'})

    # evaluated: a verdict, or nothing. A missing evaluation is not a passing one.
    if isinstance(evaluation, dict) and evaluation.get('verdict'):
        identifier = evaluation.get('node_id')
        node = by_id.get(identifier)
        if node is None:
            unknown.append({'stage': 'evaluated', 'reason': 'evaluation names an unknown node'})
        else:
            node['stages']['evaluated'] = {
                'state': 'EVALUATED', 'evidence': 'evaluation_receipt',
                'verdict': evaluation['verdict'],
                'axes': evaluation.get('axes'),
                'reason': None}
    else:
        unknown.append({'stage': 'evaluated', 'reason': 'no evaluation recorded'})

    for node in nodes:
        node['furthest_stage'] = _furthest(node)
    for stage in STAGES:
        stages[stage] = sum(1 for node in nodes if node['stages'][stage]['state'] not in
                            (UNKNOWN, 'MISMATCH'))

    view = {
        'schema_version': SCHEMA_VERSION,
        'scenario_id': scenario_id,
        'simulated': True,
        'banner': 'SIMULATED RECORDED DATA — not a live execution',
        'integration': 'NOT_INTEGRATED',
        'stages': stages,
        'nodes': sorted(nodes, key=lambda node: node['node_id']),
        'unknown_gaps': unknown,
        'probability_availability': (answer or {}).get('probability_availability', UNKNOWN),
        'limits': ['A projection of recorded stages; it produces no evidence of its own.',
                   'Global Skill Hub Graph integration is NOT_INTEGRATED.'],
    }
    view['view_sha256'] = digest({k: v for k, v in view.items() if k != 'view_sha256'})
    return view


def project_route(result):
    """Export one observed MCP routing run, without the request text or resource body."""
    resolution = result['resolution']
    selected = resolution.get('selected')
    load = result.get('load')
    receipt = None
    if isinstance(selected, dict) and load and load.get('load_verified') is True:
        receipt = {
            'node_id': node_id(selected['type'], selected['id'], selected['version']),
            'content_sha256': load['served_content_sha256'],
            'bundle_sha256': load.get('bundle_sha256'),
        }
    view = project(shortlist=result['shortlist'], answer=result.get('answer'),
                   resolution={**resolution, 'reason': resolution['outcome']},
                   load_receipt=receipt)
    view.update(
        simulated=False,
        banner='OBSERVED MCP ROUTING — execution and evaluation require separate evidence',
        integration='MCP_EXPORTED',
        route_outcome=resolution['outcome'],
        route_event_sha256=result['events'][-1]['event_sha256'],
        limits=['Observed route_task stages only. The projection does not attest agent execution.'],
    )
    view.pop('view_sha256')
    view['view_sha256'] = digest(view)
    return view


def _furthest(node):
    """The strongest stage this node actually reached, or UNKNOWN when none was recorded."""
    reached = [stage for stage in STAGES
               if node['stages'][stage]['state'] not in (UNKNOWN, 'MISMATCH')]
    return reached[-1] if reached else UNKNOWN
