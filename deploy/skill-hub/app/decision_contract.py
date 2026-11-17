"""R4-C02: the versioned request, state and candidate snapshot one System1 decision rides on.

The request is all the provider ever sees, so it must carry everything a later reader needs
to re-ask the same question and nothing the provider does not need. Three parts are pinned
separately:

* the **state** — the caller's text. Its exact bytes are hashed, and the raw text is kept out
  of every exported record; a reader re-checks `state_sha256` instead of reading context.
* the **candidate snapshot** — id, version and both hashes per candidate, plus the catalog
  revision and shortlist digests the R3 registry already produced. A decision is only
  meaningful against a pinned candidate set, so a request without one is refused.
* the **question spec** — what was asked, in the provider's own shape.

Two things the provider does *not* establish, which this module refuses to assume:

* **joint multi-select.** The probe issued two independent `noul` questions in one HTTP
  request and received two independent answers. That is per-question support, not a joint
  multi-label decision, so `multi_selection_support` is `PER_QUESTION_NOUL` and never
  `JOINT`.
* **calibrated confidence.** `confidence` is not the top probability: the ambiguous probe
  returned confidence 0.471 against a top probability of 0.647. Both are recorded and
  `confidence_is_max_probability` reports whether they agreed, so a reader never substitutes
  one for the other.

Nothing here chooses a skill, applies a threshold or calls a network: it builds the question
and normalizes the answer. Selection belongs to `decision_resolver`.
"""
from __future__ import annotations

import hashlib
import json

SCHEMA_VERSION = 'openbot.r4.decision.v1'

PROBABILITY_AVAILABILITY = ('AVAILABLE', 'UNAVAILABLE', 'UNKNOWN')
SELECTION_MODES = ('single', 'multi')
# What the provider actually demonstrated. Anything the caller has not probed stays NOT_PROBED.
MULTI_SELECTION_SUPPORT = ('PER_QUESTION_NOUL', 'JOINT_PROBABILITIES', 'NOT_PROBED')
QUESTION_TYPES = ('choice', 'noul')

ENDPOINT = 'https://inference.baseten.co/v1/systemone'
MODEL = 'decision-models/baseten-elephant-9b-latest'


class ContractError(ValueError):
    """A request or answer this module refuses. Never a silent default."""


def _require(condition, message):
    if not condition:
        raise ContractError(message)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def text_digest(text):
    """Identity of a state string under the hub normalization: line ends collapse, rest stays."""
    return hashlib.sha256(text.replace('\r\n', '\n').replace('\r', '\n').encode('utf-8')).hexdigest()


def candidate_snapshot(shortlist):
    """The pinned candidate set from `unified_registry.shortlist()`, or raise.

    `NO_CANDIDATE` and `MISSING_RESOURCE` describe the catalog and must be handled before a
    question is ever asked; passing them here would ask a model to choose from nothing and
    then score the answer.
    """
    _require(isinstance(shortlist, dict), 'shortlist must be a shortlist() result')
    outcome = shortlist.get('outcome')
    _require(outcome == 'CANDIDATES',
             f'only a CANDIDATES shortlist can be asked about, not {outcome}')
    records = shortlist.get('candidates') or []
    _require(records, 'a CANDIDATES shortlist must carry at least one candidate')
    pinned = [{'type': record['type'], 'id': record['id'], 'version': record['version'],
               'content_sha256': record['content_sha256'],
               'bundle_sha256': record['bundle_sha256'],
               'prompt': record.get('prompt'),
               # Carried so the resolver can refuse a selection that is not active without
               # re-reading the registry the snapshot was taken from.
               'state': record.get('state')}
              for record in records]
    return {
        'schema_version': SCHEMA_VERSION,
        'catalog_revision': shortlist['catalog_revision'],
        'query_sha256': shortlist['query_sha256'],
        'pinned_catalog_sha256': shortlist['pinned_catalog_sha256'],
        'candidates': pinned,
        'candidate_count': len(pinned),
        'truncated': bool(shortlist.get('truncated')),
        'snapshot_sha256': digest(pinned),
    }


def _question_spec(name, spec):
    _require(isinstance(spec, dict), f'question {name!r} must be an object')
    kind = spec.get('type')
    _require(kind in QUESTION_TYPES, f'question {name!r} type must be one of {QUESTION_TYPES}')
    instructions = spec.get('instructions')
    _require(isinstance(instructions, str) and instructions.strip(),
             f'question {name!r} needs instructions')
    built = {'type': kind, 'instructions': instructions.strip()}
    if kind == 'choice':
        criteria = spec.get('criteria')
        _require(isinstance(criteria, dict) and criteria,
                 f'choice question {name!r} needs criteria')
        _require(all(isinstance(v, str) and v.strip() for v in criteria.values()),
                 f'choice question {name!r} criteria must be nonempty strings')
        built['criteria'] = dict(criteria)
    return built


def build_request(*, state, snapshot, questions, selection_mode='single',
                  multi_selection_support='NOT_PROBED', model=MODEL, endpoint=ENDPOINT,
                  language='unknown', request_id=None):
    """One versioned request, refused rather than defaulted when a part is missing.

    `selection_mode='multi'` requires `PER_QUESTION_NOUL` support and one `noul` question per
    candidate: a set that is only jointly probed would have to be read from probabilities the
    provider never returned.
    """
    _require(isinstance(state, str) and state.strip(), 'state must be nonempty text')
    _require(not state.strip().lower().startswith('{'), 'state is text, not a serialized object')
    _require(selection_mode in SELECTION_MODES, f'selection_mode must be one of {SELECTION_MODES}')
    _require(multi_selection_support in MULTI_SELECTION_SUPPORT,
             f'multi_selection_support must be one of {MULTI_SELECTION_SUPPORT}')
    _require(isinstance(snapshot, dict) and snapshot.get('candidates'),
             'a request needs a pinned candidate snapshot')

    _require(isinstance(questions, dict) and questions, 'questions must be a nonempty object')
    built = {name: _question_spec(name, spec) for name, spec in sorted(questions.items())}

    if selection_mode == 'multi':
        _require(multi_selection_support == 'PER_QUESTION_NOUL',
                 'multi selection needs PER_QUESTION_NOUL support; joint support is NOT_PROBED')
        _require(all(q['type'] == 'noul' for q in built.values()),
                 'multi selection is built from one noul question per candidate')
        candidate_ids = {c['id'] for c in snapshot['candidates']}
        _require(set(built) <= candidate_ids,
                 'multi questions must name pinned candidates')
    else:
        _require(len(built) == 1 and built[next(iter(built))]['type'] == 'choice',
                 'single selection asks one choice question')

    request = {
        'schema_version': SCHEMA_VERSION,
        'endpoint': endpoint,
        'model': model,
        'state': {'text': state, 'sha256': text_digest(state), 'bytes': len(state.encode('utf-8')),
                  'language': language},
        'candidate_snapshot': snapshot,
        'questions': built,
        'selection_mode': selection_mode,
        'multi_selection_support': multi_selection_support,
        'probability_availability': 'UNKNOWN',
        'limits': ['One HTTP request does not prove one physical forward pass.',
                   'Alias-only model identity; the provider exposes no immutable revision.'],
    }
    request['request_sha256'] = digest({k: v for k, v in request.items() if k != 'request_sha256'})
    request['request_id'] = request_id or request['request_sha256'][:32]
    return request


def snapshot_question_names(snapshot):
    """Question names a multi selection would use: the pinned candidate ids."""
    return [candidate['id'] for candidate in snapshot['candidates']]


def provider_payload(request):
    """The provider's own shape: `{model, state, questions}`. Nothing else is sent."""
    _require(isinstance(request, dict) and 'questions' in request, 'not a built request')
    return {'model': request['model'], 'state': request['state']['text'],
            'questions': request['questions']}


def normalize_answer(raw, request=None):
    """Provider answer to a recorded, comparable form; raw bytes are hashed, not reinterpreted.

    A `choice` answer carries `probabilities` in the probed contract. An `noul` answer carries
    a single float and no distribution at all, so its probability availability is
    `UNAVAILABLE` rather than a fabricated one-hot vector.
    """
    _require(isinstance(raw, dict), 'answer must be an object')
    answers = raw.get('answers')
    _require(isinstance(answers, dict) and answers, 'answer must carry at least one question')

    normalized, availabilities = {}, set()
    for name, answer in sorted(answers.items()):
        _require(isinstance(answer, dict), f'answer {name!r} must be an object')
        kind = answer.get('type')
        _require(kind in QUESTION_TYPES, f'answer {name!r} has unknown type {kind!r}')
        if kind == 'choice':
            choice = answer.get('choice')
            _require(isinstance(choice, str) and choice, f'choice answer {name!r} has no choice')
            probabilities = answer.get('probabilities')
            available = (isinstance(probabilities, dict) and probabilities
                         and all(isinstance(v, (int, float)) for v in probabilities.values()))
            confidence = answer.get('confidence')
            confidence = float(confidence) if isinstance(confidence, (int, float)) else None
            top = max(probabilities.values()) if available else None
            normalized[name] = {
                'type': 'choice', 'choice': choice, 'confidence': confidence,
                'probabilities': dict(probabilities) if available else None,
                'probability_availability': 'AVAILABLE' if available else 'UNAVAILABLE',
                'max_probability': top,
                # Both are kept because they disagree in the probe (.471 vs .647).
                'confidence_is_max_probability': (None if confidence is None or top is None
                                                  else abs(confidence - top) < 1e-9),
            }
            availabilities.add(normalized[name]['probability_availability'])
        else:
            noul = answer.get('noul')
            _require(isinstance(noul, (int, float)), f'noul answer {name!r} has no score')
            normalized[name] = {'type': 'noul', 'noul': float(noul),
                                'probability_availability': 'UNAVAILABLE',
                                'confidence': None, 'probabilities': None}
            availabilities.add('UNAVAILABLE')

    availability = availabilities.pop() if len(availabilities) == 1 else 'UNKNOWN'
    usage = raw.get('usage') if isinstance(raw.get('usage'), dict) else None
    result = {
        'schema_version': SCHEMA_VERSION,
        'model': raw.get('model'),
        'answers': normalized,
        'probability_availability': availability,
        'usage': {'input_tokens': usage.get('input_tokens'),
                  'output_tokens': usage.get('output_tokens')} if usage else None,
        'raw_sha256': digest(raw),
        'limits': ['confidence is reported by the provider, not calibrated on this data.',
                   'A noul answer carries no distribution; probabilities stay UNAVAILABLE.'],
    }
    if request is not None:
        result['request_sha256'] = request['request_sha256']
        result['selection_mode'] = request['selection_mode']
    return result


def selected_labels(normalized, *, noul_threshold=None):
    """The labels one answer selected, without deciding whether that was right.

    Single mode takes the `choice`. Multi mode takes every `noul` question at or above an
    explicit threshold; the threshold is a required argument because a default would be an
    invented acceptance number.
    """
    _require(isinstance(normalized, dict) and normalized.get('answers'), 'not a normalized answer')
    mode = normalized.get('selection_mode', 'single')
    answers = normalized['answers']
    if mode == 'single':
        choices = [answer['choice'] for answer in answers.values() if answer['type'] == 'choice']
        _require(len(choices) == 1, 'single mode must carry exactly one choice answer')
        return list(choices)
    _require(isinstance(noul_threshold, (int, float)),
             'multi mode needs an explicit noul_threshold')
    return sorted(name for name, answer in answers.items()
                  if answer['type'] == 'noul' and answer['noul'] >= noul_threshold)
