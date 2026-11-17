"""Bind an agent-claimed quality record to a claimed ``get_skill`` response.

The round-01 structural validator cannot see a skill body, so a record whose
``body_sha256`` was tampered with still validates. This module closes that gap for a
response the caller supplies: it recomputes the hash from the returned text and compares
it with both the record and the response's own declared identity.

It is pure: no IO, no network, no shell, no reference/evidence reads, no writes, no input
mutation. It also proves nothing beyond internal consistency — see SKILL-QUALITY-BINDING.md.
"""
from __future__ import annotations

import hashlib

import skill_quality

ALGORITHM = 'sha256'
NORMALIZATION = 'CRLF and CR converted to LF; all other characters preserved; UTF-8'
IDENTITY_FIELDS = ('algorithm', 'normalization', 'returned_body_sha256', 'body_truncated')


def _err(message):
    raise ValueError(message)


def normalize_body(body):
    """Newline normalization from the round-01 identity contract; all else preserved."""
    return body.replace('\r\n', '\n').replace('\r', '\n')


def recompute_hash(body):
    return hashlib.sha256(normalize_body(body).encode('utf-8')).hexdigest()


def _check_identity(identity):
    """Strict validation of a PRESENT identity block. Raises ValueError when malformed."""
    if not isinstance(identity, dict):
        _err('identity must be an object')
    missing = [field for field in IDENTITY_FIELDS if field not in identity]
    if missing:
        _err(f'identity: missing field(s): {", ".join(missing)}')
    extra = sorted(set(identity) - set(IDENTITY_FIELDS))
    if extra:
        _err(f'identity: unknown field(s): {", ".join(extra)}')
    if identity['algorithm'] != ALGORITHM or not isinstance(identity['algorithm'], str):
        _err(f'identity.algorithm must be {ALGORITHM!r}')
    if identity['normalization'] != NORMALIZATION or not isinstance(identity['normalization'], str):
        _err('identity.normalization is not the supported round-01 normalization')
    declared = identity['returned_body_sha256']
    if not isinstance(declared, str) or not skill_quality.HEX64.fullmatch(declared):
        _err('identity.returned_body_sha256 must be lowercase 64-hex')
    if not isinstance(identity['body_truncated'], bool):
        _err('identity.body_truncated must be a boolean')


def _response_evidence(response):
    """Extract (name, body, identity) or raise/return None for absent evidence."""
    if not isinstance(response, dict):
        return None
    name = response.get('name')
    body = response.get('body')
    identity = response.get('identity')
    missing = [label for label, value in (('name', name), ('body', body),
                                          ('identity', identity)) if value is None]
    if missing:
        return None
    if not isinstance(name, str) or not name:
        _err('response.name must be a nonempty string')
    if not isinstance(body, str):
        _err('response.body must be a string')
    _check_identity(identity)
    return {'name': name, 'body': body, 'identity': identity}


def _binding_status(record, evidence):
    if evidence is None:
        return 'unknown'
    recomputed = recompute_hash(evidence['body'])
    declared = evidence['identity']['returned_body_sha256']

    # Confirmed mismatches dominate: they stand even when other evidence is missing.
    if record['skill'] != evidence['name']:
        return 'fail'
    if declared != recomputed:
        return 'fail'
    record_hash = record['body_sha256']
    if record_hash is not None and record_hash != recomputed:
        return 'fail'

    # Nothing contradicts; coverage decides. A truncated body hashes only its prefix, so
    # it cannot bind the record to the whole skill. A null record hash binds nothing.
    if evidence['identity']['body_truncated'] or record_hash is None:
        return 'unknown'
    return 'pass'


def validate_binding(record, skill_response):
    """Return ``{'overall_status': ..., 'binding_status': ...}``.

    ``validate_record`` runs first: an invalid record raises ValueError before the response
    is examined, and a structurally invalid record can never come out better than ``fail``.
    A present but malformed identity block also raises ValueError, because silently scoring
    a corrupt response as ``unknown`` would hide the defect.
    """
    structural = skill_quality.validate_record(record)['overall_status']
    binding = _binding_status(record, _response_evidence(skill_response))

    if structural == 'fail' or binding == 'fail':
        overall = 'fail'
    elif structural == 'unknown' or binding == 'unknown':
        overall = 'unknown'
    else:
        overall = 'pass'
    return {'overall_status': overall, 'binding_status': binding}
