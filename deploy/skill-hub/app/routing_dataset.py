"""R4-C09: turn recorded routing runs into a reviewable dataset, without inventing labels.

The pipeline is: **record -> review -> eligibility -> split -> export**. Each step has one job
and refuses the shortcut the next step would otherwise have to trust.

**Teacher output is not gold.** A record carries the teacher's prediction, the resolver's
outcome, the reviewer's label and the actual load/application references as *separate* fields
with separate provenance. `GOLD` requires an independent reviewer label; a completed task, a
community prompt or an `HTTP 200` never produces one. `label()` refuses to upgrade.

**Eligibility is three separate permissions, not one flag.** Evaluation retention, dataset
rights and training/distillation rights are decided independently. Training stays `DENY` until
it is explicitly granted, and this module grants nothing: a record's training permission is
whatever the caller recorded, and an absent value is `PENDING`.

**Splits must not leak.** A near-duplicate check runs over the dimensions that actually leak
between train and holdout: task family, session, source, and time bucket, plus skill and prompt
versions. A duplicate detected by a shared hash belongs on the same side; the splitter is
asked to keep it there, and `check_leakage()` proves it did.

Nothing here trains anything. R4 does not need a trainer to pass.
"""
from __future__ import annotations

import hashlib
import json
import re

SCHEMA_VERSION = 'openbot.r4.routing_dataset.v1'

PROVENANCE = ('REVIEWER_LABEL', 'TEACHER_PREDICTION', 'RESOLVER_OUTCOME', 'TASK_COMPLETION',
              'COMMUNITY_PROMPT', 'UNKNOWN')
# The only provenances that may become a gold label.
GOLD_PROVENANCE = frozenset({'REVIEWER_LABEL'})

TRAINING = ('DENY', 'PENDING', 'ALLOWED')
SPLITS = ('train', 'validation', 'holdout')
AXES = ('routing_quality', 'application_evidence', 'output_quality')
OUTCOME_FIELDS = ('candidate_recall', 'selection', 'application', 'output_quality')

_HEX64 = re.compile(r'[0-9a-f]{64}', re.ASCII)


class DatasetError(ValueError):
    """A record or split this pipeline refuses."""


def _require(condition, message):
    if not condition:
        raise DatasetError(message)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def record(*, task_id, state_text, state_sha256=None, candidates, outcome, labels=None,
           teacher=None, reviewer=None, execution=None, source=None, session=None,
           family=None, time_bucket=None, permissions=None):
    """One dataset record, with the raw context replaced by its digest.

    `state_text` is hashed and dropped: a dataset that kept the raw context would grow a copy
    of every conversation the router ever saw. `candidates` must be the pinned snapshot, so a
    record can be re-scored against the exact catalog the router saw.
    """
    _require(isinstance(task_id, str) and task_id, 'task_id is required')
    _require(isinstance(state_text, str) and state_text, 'state text is required to hash')
    computed = hashlib.sha256(state_text.replace('\r\n', '\n').replace('\r', '\n')
                              .encode('utf-8')).hexdigest()
    if state_sha256 is not None:
        _require(state_sha256 == computed, 'supplied state_sha256 does not match the state text')
    _require(isinstance(candidates, list) and candidates, 'a record needs its candidate snapshot')
    for candidate in candidates:
        for field in ('type', 'id', 'version'):
            _require(isinstance(candidate.get(field), str) and candidate[field],
                     f'candidate needs a {field}')
    _require(isinstance(outcome, dict), 'an outcome is required')

    permissions = dict(permissions or {})
    for key in ('evaluation_retention', 'dataset_rights', 'training_distillation'):
        permissions.setdefault(key, 'PENDING')
        _require(permissions[key] in (True, False, 'PENDING', 'DENY', 'ALLOWED'),
                 f'permission {key} has an unknown value')
    training = permissions['training_distillation']
    if training is True:
        training = 'ALLOWED'
    elif training is False:
        training = 'DENY'
    permissions['training_distillation'] = training

    return {
        'schema_version': SCHEMA_VERSION,
        'task_id': task_id,
        'state_sha256': computed,
        'state_bytes': len(state_text.encode('utf-8')),
        'candidates': [{'type': c['type'], 'id': c['id'], 'version': c['version'],
                        'content_sha256': c.get('content_sha256'),
                        'bundle_sha256': c.get('bundle_sha256'),
                        'prompt': c.get('prompt')} for c in candidates],
        'outcome': {field: outcome.get(field, 'UNKNOWN') for field in OUTCOME_FIELDS},
        'prediction': _prediction(teacher),
        'labels': _labels(labels),
        'reviewer': _reviewer(reviewer),
        'execution': dict(execution or {}),
        'source': source,
        'session': session,
        'family': family,
        'time_bucket': time_bucket,
        'permissions': permissions,
        'training_eligible': permissions['training_distillation'] == 'ALLOWED',
        'limits': ['Teacher prediction is a prediction; it is not a label and never gold.'],
    }


def _prediction(teacher):
    if not isinstance(teacher, dict):
        return {'provenance': 'UNKNOWN', 'model': None, 'choice': None, 'confidence': None,
                'probability_availability': 'UNKNOWN', 'request_sha256': None}
    return {'provenance': 'TEACHER_PREDICTION', 'model': teacher.get('model'),
            'choice': teacher.get('choice'),
            'confidence': teacher.get('confidence'),
            'probability_availability': teacher.get('probability_availability', 'UNKNOWN'),
            'request_sha256': teacher.get('request_sha256')}


def _labels(labels):
    if labels is None:
        return []
    _require(isinstance(labels, list), 'labels must be a list')
    out = []
    for label in labels:
        _require(isinstance(label, dict), 'each label must be an object')
        provenance = label.get('provenance')
        _require(provenance in PROVENANCE, f'unknown label provenance {provenance!r}')
        is_gold = bool(label.get('gold'))
        if is_gold:
            # The one rule this module exists to enforce.
            _require(provenance in GOLD_PROVENANCE,
                     f'{provenance} may not be a gold label; only an independent reviewer may')
        out.append({'provenance': provenance, 'value': label.get('value'),
                    'gold': is_gold, 'reviewer_id': label.get('reviewer_id'),
                    'reason': label.get('reason')})
    return out


def _reviewer(reviewer):
    if not isinstance(reviewer, dict):
        return {'reviewer_id': None, 'independent': False, 'reviewed_at': None}
    _require(isinstance(reviewer.get('reviewer_id'), str) and reviewer['reviewer_id'],
             'a reviewer record needs a reviewer_id')
    return {'reviewer_id': reviewer['reviewer_id'],
            'independent': bool(reviewer.get('independent')),
            'reviewed_at': reviewer.get('reviewed_at')}


def label(record_value, *, value, reviewer_id, gold=False, reason=None, reviewed_at=None):
    """Attach a reviewer label. `gold` requires an identified, independent reviewer."""
    _require(isinstance(record_value, dict) and record_value.get('schema_version') == SCHEMA_VERSION,
             'not a dataset record')
    _require(isinstance(reviewer_id, str) and reviewer_id, 'a label needs a reviewer_id')
    if gold:
        _require(record_value.get('reviewer', {}).get('independent'),
                 'a gold label needs an independent reviewer')
    updated = json.loads(json.dumps(record_value))
    updated['labels'] = list(updated.get('labels') or []) + [{
        'provenance': 'REVIEWER_LABEL', 'value': value, 'gold': bool(gold),
        'reviewer_id': reviewer_id, 'reason': reason}]
    updated['reviewer'] = {'reviewer_id': reviewer_id,
                           'independent': record_value.get('reviewer', {}).get('independent', False),
                           'reviewed_at': reviewed_at}
    return updated


def eligibility(record_value):
    """The three permissions, reported separately. Training is not implied by the other two."""
    _require(isinstance(record_value, dict), 'not a dataset record')
    permissions = dict(record_value.get('permissions') or {})
    training = permissions.get('training_distillation', 'PENDING')
    return {
        'evaluation_retention': permissions.get('evaluation_retention', 'PENDING'),
        'dataset_rights': permissions.get('dataset_rights', 'PENDING'),
        'training_distillation': training,
        'training_eligible': training == 'ALLOWED',
        'evaluation_eligible': permissions.get('evaluation_retention') in (True, 'ALLOWED'),
        'reasons': [] if training == 'ALLOWED' else [
            'training/distillation stays ' + str(training) + ' until it is granted explicitly'],
    }


def leakage_keys(record_value):
    """The grouping values that make two records the same example.

    Only three things qualify: the task family, the session, and the exact context hash. A
    shared skill or prompt version is *not* a duplicate — the same skill legitimately appears in
    many tasks, and grouping on it would collapse the dataset into one unusable group. Source
    and time bucket are strata to report, not identity.

    Returned as a list rather than one combined key because a record can share a family with one
    neighbour and a session with another; grouping has to follow all of them, or a duplicate
    slips across a split through the dimension that was not checked.
    """
    parts = []
    for field in ('family', 'session'):
        value = record_value.get(field)
        if isinstance(value, str) and value:
            parts.append(f'{field}={value}')
    state = record_value.get('state_sha256')
    if isinstance(state, str) and state:
        parts.append('state=' + state)
    return parts


def strata_keys(record_value):
    """Balance dimensions reported alongside a split: where and when, and which resources."""
    parts = []
    for field in ('source', 'time_bucket'):
        value = record_value.get(field)
        if isinstance(value, str) and value:
            parts.append(f'{field}={value}')
    for candidate in record_value.get('candidates') or []:
        parts.append(f'res={candidate["type"]}/{candidate["id"]}@{candidate["version"]}')
    return parts


def near_duplicate_key(record_value):
    """A stable digest over the record's own grouping values. Used for ordering, not grouping."""
    return digest(sorted(leakage_keys(record_value)))


def _components(records):
    """Union records that share any grouping value, so a duplicate group is one unit."""
    parent = list(range(len(records)))

    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left, right):
        left, right = find(left), find(right)
        if left != right:
            parent[max(left, right)] = min(left, right)

    owner = {}
    for index, record_value in enumerate(records):
        for key in leakage_keys(record_value):
            if key in owner:
                union(index, owner[key])
            else:
                owner[key] = index
    groups = {}
    for index in range(len(records)):
        groups.setdefault(find(index), []).append(records[index])
    return list(groups.values())


def split(records, *, fractions, seed='r4'):
    """Assign each record to train/validation/holdout without splitting a duplicate group.

    Groups are the connected components of shared grouping values, so one shared family puts
    every record in it on one side. `fractions` must be supplied by the caller: the sizes are a
    frozen protocol decision, not a default.
    """
    _require(isinstance(records, list) and records, 'records are required')
    _require(set(fractions) == set(SPLITS), 'fractions must name train, validation and holdout')
    _require(abs(sum(float(v) for v in fractions.values()) - 1.0) < 1e-9,
             'fractions must sum to 1')

    groups = _components(records)
    groups.sort(key=lambda group: digest([seed, near_duplicate_key(group[0])]))
    counts = {name: 0 for name in SPLITS}
    assignment = {}
    for group in groups:
        target = max(SPLITS, key=lambda name: (float(fractions[name]) * len(records) - counts[name],
                                               -SPLITS.index(name)))
        counts[target] += len(group)
        for record_value in group:
            assignment[id(record_value)] = target
    result = {'schema_version': SCHEMA_VERSION, 'seed': seed, 'fractions': fractions,
              'records': [{**record_value, 'split': assignment[id(record_value)]}
                          for record_value in records],
              'group_count': len(groups)}
    result['strata'] = {name: sorted({key for record_value in result['records']
                                      if record_value['split'] == name
                                      for key in strata_keys(record_value)})
                        for name in SPLITS}
    leaked = check_leakage(result['records'])
    _require(not leaked, 'split placed duplicates on both sides: ' + ', '.join(leaked[:3]))
    return result


def check_leakage(records):
    """Any grouping value that appears on two sides of a split is a leak; return them."""
    seen, leaked = {}, set()
    for record_value in records:
        side = record_value.get('split')
        for key in leakage_keys(record_value):
            if key in seen and seen[key] != side:
                leaked.add(key)
            seen[key] = side
    return sorted(leaked)



def export(records, *, include_training=False):
    """A stable, reviewable export. Training-ineligible records are excluded unless asked."""
    _require(isinstance(records, list), 'records must be a list')
    selected = []
    for record_value in records:
        if not include_training and not record_value.get('training_eligible'):
            continue
        if not any(label_item.get('gold') for label_item in record_value.get('labels') or []):
            # An unlabelled record is a sample, not a gold example, and the export says which.
            record_value = {**record_value, 'gold': False}
        selected.append(record_value)
    payload = {'schema_version': SCHEMA_VERSION, 'count': len(selected),
               'records': sorted(selected, key=lambda r: (r.get('split') or '', r['task_id']))}
    payload['export_sha256'] = digest(payload['records'])
    return payload
