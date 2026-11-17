"""R4-C03: turn one shadow answer into exactly one of the six routing outcomes.

The six are separate because they call for different actions, and collapsing any two hides a
fact an evaluation needs:

| outcome | what it means | what may happen next |
| --- | --- | --- |
| `SELECTED` | the model named a candidate that the snapshot pinned | materialize that candidate |
| `NO_SKILL` | the model answered that no skill applies | run without a skill |
| `NO_CANDIDATE` | the catalog offered nothing, so no question was asked | run without a skill; a recall fact |
| `ABSTAIN_LOW_CONFIDENCE` | an answer arrived but did not clear the threshold | run without a skill |
| `POLICY_DENIED` | host policy or a missing permission forbids the action | refuse; never fall back into it |
| `RESOURCE_UNAVAILABLE` | the catalog named a resource it cannot serve | fall back to the approved flow |
| `ROUTER_ERROR` | the provider failed, timed out, or answered unusably | fall back to the approved flow |

Two rules decide the whole module.

**A provider failure is not a negative label.** `TIMEOUT`, `AUTH`, `RATE_LIMIT`, `QUOTA` and
`SERVER_ERROR` all resolve to `ROUTER_ERROR`, never to `NO_SKILL`. Scoring an outage as "the
model correctly declined" is how a broken router reports excellent precision.

**Policy is checked before and after selection.** A candidate the shortlist should never have
offered, or an action the host does not permit, is `POLICY_DENIED` whether or not the model
picked it, and a fallback may not re-enter that path.

The threshold is a required argument wherever confidence is compared. There is no default:
a default would be an invented acceptance number, and R4 requires thresholds to be frozen on
validation before any holdout is read.
"""
from __future__ import annotations

import math

SCHEMA_VERSION = 'openbot.r4.resolver.v1'

OUTCOMES = ('SELECTED', 'NO_SKILL', 'NO_CANDIDATE', 'ABSTAIN_LOW_CONFIDENCE', 'POLICY_DENIED',
            'RESOURCE_UNAVAILABLE', 'ROUTER_ERROR')
# The outcomes a caller may continue from without a skill, and the ones that fall back.
FALLBACK_OUTCOMES = frozenset({'ROUTER_ERROR', 'RESOURCE_UNAVAILABLE'})
NO_SKILL_OUTCOMES = frozenset({'NO_SKILL', 'NO_CANDIDATE', 'ABSTAIN_LOW_CONFIDENCE'})
# Answers that mean "nothing applies". These are the model's own refusal vocabulary, not
# aliases for an error: a transport failure never reaches this set.
DECLINE_ANSWERS = frozenset({'no_skill', 'none', 'no', 'null', 'no_match', 'unknown'})

ERROR_TO_OUTCOME = {
    'TIMEOUT': 'ROUTER_ERROR', 'AUTH': 'ROUTER_ERROR', 'RATE_LIMIT': 'ROUTER_ERROR',
    'QUOTA': 'ROUTER_ERROR', 'SERVER_ERROR': 'ROUTER_ERROR', 'CLIENT_ERROR': 'ROUTER_ERROR',
    'TRANSPORT': 'ROUTER_ERROR', 'INVALID_RESPONSE': 'ROUTER_ERROR',
    'BUDGET_EXCEEDED': 'ROUTER_ERROR',
}


class ResolverError(ValueError):
    """A resolver input it refuses: an unscored threshold or a malformed snapshot."""


def resolve(*, shortlist_outcome, threshold, snapshot=None, normalized=None, shadow_error=None,
            policy=None, skill_threshold=None, prompt_threshold=None):
    """Exactly one outcome, with the reason and the fallback already decided.

    `policy` is an `evaluate_policy_and_permissions()` decision (R3-C05) and is not optional:
    an action the host refuses cannot be reported as a successful selection.
    """
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
        raise ResolverError('threshold is required and numeric; it must be frozen, not defaulted')
    if not 0.0 <= float(threshold) <= 1.0:
        raise ResolverError('threshold must be between 0 and 1')

    common = {'schema_version': SCHEMA_VERSION, 'threshold': float(threshold),
              'skill_threshold': None if skill_threshold is None else float(skill_threshold),
              'prompt_threshold': None if prompt_threshold is None else float(prompt_threshold),
              'fallback': None, 'selected': None, 'reason': None,
              'dispatch_influenced': False,
              'limits': ['Selection is a routing fact; no execution is claimed here.']}

    # 1. Policy first: a denied action is denied whatever the model said, or failed to say.
    if policy is not None and not policy.get('allowed', True):
        return {**common, 'outcome': 'POLICY_DENIED', 'fallback': None,
                'reasons': list(policy.get('reasons') or []),
                'reason': 'host policy or permissions refused the action'}

    # 2. The catalog had nothing. No question was asked, so no router error is involved.
    if shortlist_outcome == 'NO_CANDIDATE':
        return {**common, 'outcome': 'NO_CANDIDATE',
                'fallback': None, 'reason': 'the catalog offered no active candidate'}

    # 3. The shortlist pointed at a resource the registry cannot serve. A catalog defect.
    if shortlist_outcome == 'MISSING_RESOURCE':
        return {**common, 'outcome': 'RESOURCE_UNAVAILABLE', 'fallback': 'APPROVED_DISCOVERY',
                'reason': 'the shortlist referenced a resource the registry cannot resolve'}
    if shortlist_outcome == 'INVALID_REQUEST':
        return {**common, 'outcome': 'ROUTER_ERROR', 'fallback': 'APPROVED_DISCOVERY',
                'reason': 'the request was unusable, so the model could not be asked'}

    # 4. A provider failure. Never a negative label.
    if shadow_error is not None:
        error_class = (shadow_error.get('error_class') if isinstance(shadow_error, dict)
                       else getattr(shadow_error, 'error_class', 'TRANSPORT'))
        return {**common, 'outcome': ERROR_TO_OUTCOME.get(error_class, 'ROUTER_ERROR'),
                'fallback': 'APPROVED_DISCOVERY',
                'reason': f'provider failure: {error_class}',
                'error_class': error_class}

    if not isinstance(snapshot, dict) or not snapshot.get('candidates'):
        return {**common, 'outcome': 'ROUTER_ERROR', 'fallback': 'APPROVED_DISCOVERY',
                'reason': 'no pinned candidate snapshot accompanies the answer'}
    if not isinstance(normalized, dict) or not normalized.get('answers'):
        return {**common, 'outcome': 'ROUTER_ERROR', 'fallback': 'APPROVED_DISCOVERY',
                'reason': 'no normalized answer to resolve'}

    pinned = {candidate['id']: candidate for candidate in snapshot['candidates']}

    # 5. Multi mode: every question at or over its own kind's threshold is selected.
    if normalized.get('selection_mode') == 'multi':
        return _resolve_multi(common, normalized, pinned, float(threshold),
                              skill_threshold=skill_threshold,
                              prompt_threshold=prompt_threshold)

    answers = [answer for answer in normalized['answers'].values() if answer.get('type') == 'choice']
    if len(answers) != 1:
        return {**common, 'outcome': 'ROUTER_ERROR', 'fallback': 'APPROVED_DISCOVERY',
                'reason': 'single mode needs exactly one choice answer'}
    answer = answers[0]
    choice = answer.get('choice')

    # 6. The model declined. This is an answer, and it is not an error.
    if isinstance(choice, str) and choice.lower() in DECLINE_ANSWERS:
        return {**common, 'outcome': 'NO_SKILL', 'fallback': None,
                'reason': f'model answered {choice}', 'answer': choice,
                'confidence': answer.get('confidence'),
                'confidence_is_max_probability': answer.get('confidence_is_max_probability')}

    # 7. An answer naming something outside the pinned snapshot is a selection error, and the
    #    caller must be told, not silently given the nearest match.
    if choice not in pinned:
        return {**common, 'outcome': 'ROUTER_ERROR', 'fallback': 'APPROVED_DISCOVERY',
                'reason': 'answer named a candidate outside the pinned snapshot',
                'answer': choice, 'is_selection_error': True}

    # 8. Confidence gates the selection. The provider's confidence is used as reported; it is
    #    not calibrated here, and both confidence and max probability are kept for the record.
    confidence = answer.get('confidence')
    if confidence is None:
        return {**common, 'outcome': 'ROUTER_ERROR', 'fallback': 'APPROVED_DISCOVERY',
                'reason': 'choice answer carried no confidence', 'answer': choice}
    if float(confidence) < float(threshold):
        return {**common, 'outcome': 'ABSTAIN_LOW_CONFIDENCE', 'fallback': None,
                'reason': f'confidence {confidence:.6f} below threshold {threshold}',
                'answer': choice, 'confidence': confidence,
                'confidence_is_max_probability': answer.get('confidence_is_max_probability')}

    selected = pinned[choice]
    # 9. A selected resource must still be one the policy lets this caller load.
    if selected.get('state') != 'active':
        return {**common, 'outcome': 'POLICY_DENIED', 'fallback': None,
                'reason': f'selected resource is {selected.get("state")}, not active',
                'answer': choice}
    return {**common, 'outcome': 'SELECTED', 'selected': selected, 'answer': choice,
            'confidence': confidence,
            'confidence_is_max_probability': answer.get('confidence_is_max_probability'),
            'probability_availability': normalized.get('probability_availability'),
            'reason': 'answer named a pinned active candidate and cleared the threshold'}


def _multi_threshold(kind, pinned, threshold, skill_threshold, prompt_threshold):
    """The threshold a candidate of this kind must clear, and whether it is a strict one.

    Skills are strictly greater than their bar and prompts at or above theirs, which is the
    contract exactly: a skill scoring 0.80 is not selected, a prompt scoring 0.75 is. When the
    caller supplies no split, the one `threshold` applies to both, at or above.
    """
    record = pinned.get(kind) or {}
    if record.get('type') == 'skill' and skill_threshold is not None:
        return float(skill_threshold), True
    if record.get('type') == 'prompt' and prompt_threshold is not None:
        return float(prompt_threshold), False
    return float(threshold), False


def _resolve_multi(common, normalized, pinned, threshold, *, skill_threshold=None,
                   prompt_threshold=None):
    """Every noul question over its kind's bar is selected; none at all is a decline.

    A score that is not a finite number never clears a threshold: a NaN compares false and an
    infinity would otherwise select everything, so both are recorded as unusable instead.
    """
    above, unusable = [], {}
    for name, answer in sorted(normalized['answers'].items()):
        if answer.get('type') != 'noul':
            return {**common, 'outcome': 'ROUTER_ERROR', 'fallback': 'APPROVED_DISCOVERY',
                    'reason': 'multi mode needs only noul answers'}
        if name not in pinned:
            return {**common, 'outcome': 'ROUTER_ERROR', 'fallback': 'APPROVED_DISCOVERY',
                    'reason': 'noul question named a candidate outside the pinned snapshot',
                    'is_selection_error': True}
        raw = answer.get('noul')
        try:
            score = float(raw)
        except (TypeError, ValueError):
            unusable[name] = 'not_a_number'
            continue
        if isinstance(raw, bool) or not math.isfinite(score) or not 0 <= score <= 1:
            unusable[name] = 'invalid_probability'
            continue
        bar, strict = _multi_threshold(name, pinned, threshold, skill_threshold, prompt_threshold)
        if (score > bar) if strict else (score >= bar):
            above.append((name, score))
    if not above:
        result = {**common, 'outcome': 'ABSTAIN_LOW_CONFIDENCE', 'fallback': None,
                  'reason': 'no candidate cleared its threshold',
                  'scores': {n: a.get('noul') for n, a in normalized['answers'].items()}}
        if unusable:
            result['unusable_scores'] = unusable
        return result
    selected = [pinned[name] for name, _ in above]
    if any(record.get('state') != 'active' for record in selected):
        return {**common, 'outcome': 'POLICY_DENIED', 'fallback': None,
                'reason': 'a selected resource is not active'}
    result = {**common, 'outcome': 'SELECTED', 'selected': selected,
              'scores': {name: score for name, score in above},
              'probability_availability': 'UNAVAILABLE',
              'reason': 'one or more candidates cleared their threshold'}
    if unusable:
        result['unusable_scores'] = unusable
    return result
