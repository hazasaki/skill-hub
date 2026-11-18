"""R4-C01/C04: the Baseten system-one transport, its error vocabulary and its budget guard.

Three jobs, deliberately separated from the decision contract so each can be tested alone.

**Transport.** One POST to `https://inference.baseten.co/v1/systemone` with the provider's own
payload. The key is read from a file path supplied by the caller, never from this module and
never written back out: every recorded error carries the error class and HTTP status only,
because a provider error body can echo request headers or account identifiers.

**Distinct failures.** A timeout, a 401, a 429, a 5xx and a budget refusal are different
events and must stay different, because the resolver has to decide which of them may fall
back and which must not be scored. Note in particular:

* a timeout or any other transport failure is **not** a `no_skill` label;
* a 401 is an access problem, not a routing answer;
* a 429 or quota response is retryable but must not retry without a bound.

**Shadow.** Adapter results feed a shadow record. Nothing here can change what the caller
executes: `dispatch_influenced` is false by construction, because this module returns a
record and holds no handle to any dispatch path.

**Budget.** A guard is checked *before* the request is sent, reserving a worst-case cost in
addition to the cost already spent. The estimate is a price-table arithmetic on input tokens,
not a billing reconciliation: the recorded `cost_provenance` says so.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

SCHEMA_VERSION = 'openbot.r4.elephant_shadow.v1'
ENDPOINT = 'https://inference.baseten.co/v1/systemone'
MODEL = 'decision-models/baseten-elephant-9b-latest'
LIQUID_ENDPOINT = 'https://api.liquid.ai/typesafe/v1/systemone'
LIQUID_MODEL = 'd1:latest'
LIQUID_PRICE_USD_PER_MILLION_INPUT = 0.02
PRICE_USD_PER_MILLION_INPUT = 0.035
DEFAULT_TIMEOUT_SECONDS = 15
DEFAULT_MAX_RETRIES = 0
# The reserved cost of a request before its real token count is known. The output side is
# small on purpose: the probed answers return one or two output tokens, so reserving thousands
# would refuse every request under a per-query cost cap that the real spend fits inside
# comfortably. The input side reserves one token per byte, which is the pessimistic reading of
# UTF-8 text and never understates it for ASCII or Vietnamese.
RESERVED_OUTPUT_TOKENS = 64
BYTES_PER_TOKEN_FLOOR = 1.0

ERROR_CLASSES = ('TIMEOUT', 'AUTH', 'RATE_LIMIT', 'QUOTA', 'SERVER_ERROR', 'CLIENT_ERROR',
                 'TRANSPORT', 'BUDGET_EXCEEDED', 'INVALID_RESPONSE')
# Which classes may be retried, and which must not be retried at all. A 401 retried is a
# credential sent to a server that already refused it.
RETRYABLE = frozenset({'TIMEOUT', 'RATE_LIMIT', 'SERVER_ERROR', 'TRANSPORT'})


class ShadowError(RuntimeError):
    """A transport or budget refusal, carrying the class that decides the fallback."""

    def __init__(self, error_class, message, *, status=None, retryable=None):
        super().__init__(message)
        assert error_class in ERROR_CLASSES, error_class
        self.error_class = error_class
        self.status = status
        self.retryable = error_class in RETRYABLE if retryable is None else retryable

    def record(self):
        return {'error_class': self.error_class, 'status': self.status,
                'retryable': self.retryable, 'message': str(self)}


def classify_status(status):
    """HTTP status to error class. An unlisted 4xx is a client error, never a routing label."""
    if status == 401 or status == 403:
        return 'AUTH'
    if status == 429:
        return 'RATE_LIMIT'
    if status == 402:
        return 'QUOTA'
    if 500 <= status <= 599:
        return 'SERVER_ERROR'
    if 400 <= status <= 499:
        return 'CLIENT_ERROR'
    return 'TRANSPORT'


class CostBudget:
    """Spend so far plus a worst-case reservation, refused before the request is sent.

    `reserve` is called with the payload bytes *before* transport and adds a conservative
    ceiling to `committed`; `settle` replaces that ceiling with the real usage from the
    response. A crash between the two leaves the ceiling counted, which over-reports rather
    than under-reports spend.
    """

    def __init__(self, limit_usd, *, price_per_million_input=PRICE_USD_PER_MILLION_INPUT,
                 spent_usd=0.0):
        if limit_usd is not None and (not isinstance(limit_usd, (int, float)) or limit_usd <= 0):
            raise ShadowError('BUDGET_EXCEEDED', 'a positive budget limit is required')
        self.limit_usd = None if limit_usd is None else float(limit_usd)
        self.price = float(price_per_million_input)
        self.spent_usd = float(spent_usd)
        self.committed_usd = 0.0
        self.requests = 0
        self.input_tokens = 0

    def _cost(self, tokens):
        return tokens * self.price / 1_000_000

    def reserve(self, payload_bytes):
        """Ceiling for one request, or `ShadowError(BUDGET_EXCEEDED)` with the figures."""
        ceiling_tokens = payload_bytes / BYTES_PER_TOKEN_FLOOR + RESERVED_OUTPUT_TOKENS
        ceiling = self._cost(ceiling_tokens)
        projected = self.spent_usd + self.committed_usd + ceiling
        if self.limit_usd is not None and projected > self.limit_usd:
            raise ShadowError(
                'BUDGET_EXCEEDED',
                f'projected {projected:.8f} USD would exceed limit {self.limit_usd:.4f} USD')
        self.committed_usd += ceiling
        return ceiling

    def settle(self, reservation_usd, input_tokens, output_tokens=0):
        """Replace the reservation with the real input-token cost."""
        real = self._cost(input_tokens or 0)
        self.committed_usd = max(0.0, self.committed_usd - reservation_usd)
        self.spent_usd += real
        self.input_tokens += int(input_tokens or 0)
        self.requests += 1
        return real

    def state(self):
        return {'schema_version': SCHEMA_VERSION, 'limit_usd': self.limit_usd,
                'spent_usd': self.spent_usd, 'committed_usd': self.committed_usd,
                'requests': self.requests, 'input_tokens': self.input_tokens,
                'price_usd_per_million_input': self.price,
                'cost_provenance': 'price-table estimate on input tokens, not a billing reconciliation'}


def load_key(key_path):
    """Read the provider key from the caller's path. The value never leaves this call."""
    path = Path(key_path)
    try:
        key = path.read_text(encoding='utf-8').strip()
    except OSError as error:
        raise ShadowError('AUTH', f'provider key not readable: {type(error).__name__}') from error
    if not key:
        raise ShadowError('AUTH', 'provider key is empty')
    return key


def call(*, request_payload, key, budget=None, endpoint=ENDPOINT, timeout=DEFAULT_TIMEOUT_SECONDS,
         max_retries=DEFAULT_MAX_RETRIES, transport=None, language='unknown', scenario='unknown'):
    """One bounded shadow call. Returns a record; never raises a provider error unclassified.

    `transport(payload_bytes, headers, timeout)` is injectable so the negative paths (timeout,
    401, 429, 5xx) are testable without a network call and without spending budget.
    """
    body = json.dumps(request_payload, ensure_ascii=False).encode('utf-8')
    if not key:
        raise ShadowError('AUTH', 'no provider key supplied')
    liquid = endpoint == LIQUID_ENDPOINT
    if liquid and request_payload.get('model') != LIQUID_MODEL:
        raise ShadowError('CLIENT_ERROR', 'Skill Hub requires the Liquid d1:latest model')
    if budget is not None:
        reservation = budget.reserve(len(body))
    else:
        reservation = None
    authorization = ('Bearer ' if liquid else 'Api-Key ') + key
    headers = {'Content-Type': 'application/json', 'Authorization': authorization}
    send = transport or (_liquid_transport if liquid else _urllib_transport)
    attempts, last = 0, None
    started = time.perf_counter()
    while attempts <= max(0, int(max_retries)):
        # One deadline for the whole attempt sequence, not one per attempt: a retry must spend
        # what is left of the caller's budget of time, and a late answer is discarded rather
        # than accepted after the deadline it was supposed to fit in.
        remaining = timeout - (time.perf_counter() - started)
        if remaining <= 0:
            last = ShadowError('TIMEOUT', f'deadline {timeout}s reached before attempt '
                                          f'{attempts + 1}')
            if budget is not None and reservation is not None:
                budget.committed_usd = max(0.0, budget.committed_usd - reservation)
            raise last
        attempts += 1
        try:
            raw, status = send(body, headers, remaining)
            if time.perf_counter() - started > timeout:
                last = ShadowError('TIMEOUT', f'late response after deadline {timeout}s')
                if budget is not None and reservation is not None:
                    budget.committed_usd = max(0.0, budget.committed_usd - reservation)
                raise last
            break
        except ShadowError as error:
            last = error
            if not error.retryable or attempts > max(0, int(max_retries)):
                if budget is not None and reservation is not None:
                    budget.committed_usd = max(0.0, budget.committed_usd - reservation)
                raise
        except TimeoutError as error:
            last = ShadowError('TIMEOUT', type(error).__name__)
            if attempts > max(0, int(max_retries)):
                if budget is not None and reservation is not None:
                    budget.committed_usd = max(0.0, budget.committed_usd - reservation)
                raise last from error
    else:  # pragma: no cover - the loop always breaks or raises
        raise last

    latency_ms = round((time.perf_counter() - started) * 1000, 3)
    if not isinstance(raw, dict):
        raise ShadowError('INVALID_RESPONSE', 'provider answer is not an object', status=status)
    usage = raw.get('usage') if isinstance(raw.get('usage'), dict) else {}
    input_tokens = usage.get('input_tokens')
    output_tokens = usage.get('output_tokens')
    if budget is not None:
        budget.settle(reservation if reservation is not None else 0.0,
                      input_tokens if isinstance(input_tokens, int) else 0,
                      output_tokens if isinstance(output_tokens, int) else 0)
    return {
        'schema_version': SCHEMA_VERSION,
        'endpoint': endpoint, 'model': request_payload.get('model', MODEL),
        **({'resolved_model': raw.get('model')} if liquid else {}),
        'language': language, 'scenario': scenario,
        'status': status, 'attempts': attempts, 'retries': attempts - 1,
        'latency_ms': latency_ms, 'timeout_seconds': timeout,
        'input_tokens': input_tokens, 'output_tokens': output_tokens,
        'payload_bytes': len(body), 'raw': raw,
        'revision_visibility': 'ALIAS_ONLY',
        'physical_forward_count': 'UNKNOWN',
        'shadow': True, 'dispatch_influenced': False,
        'cost_usd': None if budget is None else budget.spent_usd,
        'budget': None if budget is None else budget.state(),
        'limits': ['One HTTP request does not prove one physical forward pass.',
                   'Alias-only provider identity; no immutable revision is claimed.'],
    }


def _urllib_transport(body, headers, timeout):
    """The real transport. A provider error body is discarded, never recorded."""
    return _systemone_post(ENDPOINT, urllib.request.urlopen, body, headers, timeout)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _liquid_transport(body, headers, timeout):
    opener = urllib.request.build_opener(_NoRedirect())
    raw, status = _systemone_post(LIQUID_ENDPOINT, opener.open, body, headers, timeout)
    model = raw.get('model') if isinstance(raw, dict) else None
    if not isinstance(model, str) or not re.fullmatch(r'd1(?::latest|(?:\.[0-9]+)*)', model):
        raise ShadowError('INVALID_RESPONSE', 'unexpected Liquid model identity', status=status)
    return raw, status


def _systemone_post(endpoint, send, body, headers, timeout):
    request = urllib.request.Request(endpoint, data=body, headers=headers, method='POST')
    try:
        with send(request, timeout=timeout) as response:
            return json.loads(response.read()), response.status
    except urllib.error.HTTPError as error:
        raise ShadowError(classify_status(error.code), 'provider refused the request',
                          status=error.code) from None
    except TimeoutError:
        raise ShadowError('TIMEOUT', 'provider did not answer in time') from None
    except urllib.error.URLError as error:
        if isinstance(getattr(error, 'reason', None), TimeoutError):
            raise ShadowError('TIMEOUT', 'provider did not answer in time') from None
        raise ShadowError('TRANSPORT', type(error).__name__) from None
    except json.JSONDecodeError:
        raise ShadowError('INVALID_RESPONSE', 'provider answer was not JSON') from None


def percentile(values, fraction):
    """Nearest-rank percentile of the successes; `None` for no samples, never 0."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, -(-len(ordered) * fraction // 1))
    return ordered[int(rank) - 1]


def metrics(records):
    """C04 metrics over shadow records, counting failures in their own classes."""
    successes = [r for r in records if r.get('status') == 200]
    errors = [r for r in records if r.get('status') != 200]
    latencies = [r['latency_ms'] for r in successes if isinstance(r.get('latency_ms'), (int, float))]
    by_class = {}
    for record in errors:
        error_class = record.get('error_class', 'UNKNOWN')
        by_class[error_class] = by_class.get(error_class, 0) + 1
    return {
        'schema_version': SCHEMA_VERSION,
        'requests': len(records), 'successes': len(successes), 'errors': len(errors),
        'retries': sum(int(r.get('retries') or 0) for r in records),
        'error_classes': by_class,
        'latency_p50_ms': percentile(latencies, 0.50),
        'latency_p95_ms': percentile(latencies, 0.95),
        'percentile_method': 'nearest_rank',
        'input_tokens': sum(int(r.get('input_tokens') or 0) for r in successes),
        'revision_visibility': 'ALIAS_ONLY',
        'physical_forward_count': 'UNKNOWN',
        'dispatch_influenced': False,
    }
