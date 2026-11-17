#!/usr/bin/env python3
"""Explicit quality-record validator for Skill Hub (skill-quality-round-01.check.json).

Validates one agent-reported quality record against the pilot contract: strict types,
unknown-field rejection at every level, and precedence-based overall_status. Pure stdlib;
the validator never opens or executes `evidence_ref`, never runs commands, and writes
nothing. Records are agent-reported evidence, not independent correctness certification.

Run: python skill_quality.py RECORD.json   (exit 0 pass, 1 fail/unknown, 2 invalid)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SCHEMA_VERSION = 1
HEX64 = re.compile(r'[0-9a-f]{64}')
REQUIRED_FIELDS = ('schema_version', 'skill', 'body_sha256', 'host', 'run_id', 'checks', 'feedback')
CHECK_FIELDS = ('id', 'status', 'evidence_ref')
FEEDBACK_FIELDS = ('decision', 'next_body_sha256')
STATUSES = ('pass', 'fail', 'unknown')
DECISIONS = ('none', 'propose_change')


def _err(message):
    raise ValueError(message)


def _nonempty_str(value, where):
    if not isinstance(value, str) or not value:
        _err(f'{where} must be a nonempty string')


def _hex_or_none(value, where):
    if value is None:
        return
    if not isinstance(value, str) or not HEX64.fullmatch(value):
        _err(f'{where} must be lowercase 64-hex or null')


def evaluate(record):
    """Precedence: any fail -> fail; else missing hash/empty checks/any unknown/pass
    without evidence_ref -> unknown; else pass."""
    if any(check['status'] == 'fail' for check in record['checks']):
        return 'fail'
    if (record['body_sha256'] is None or not record['checks']
            or any(check['status'] == 'unknown' for check in record['checks'])
            or any(check['evidence_ref'] is None for check in record['checks']
                   if check['status'] == 'pass')):
        return 'unknown'
    return 'pass'


def validate_record(record):
    """Validate one quality record; return a new dict with overall_status.

    Invalid input raises ValueError. The input record is never mutated and evidence_ref
    values are never read, opened or executed.
    """
    if not isinstance(record, dict):
        _err('record must be a JSON object')
    unknown = sorted(set(record) - set(REQUIRED_FIELDS))
    if unknown:
        _err(f'unknown field(s): {", ".join(unknown)}')
    missing = [field for field in REQUIRED_FIELDS if field not in record]
    if missing:
        _err(f'missing field(s): {", ".join(missing)}')

    schema_version = record['schema_version']
    if isinstance(schema_version, bool) or not isinstance(schema_version, int) \
            or schema_version != SCHEMA_VERSION:
        _err(f'schema_version must be integer {SCHEMA_VERSION}')
    for field in ('skill', 'host', 'run_id'):
        _nonempty_str(record[field], field)
    _hex_or_none(record['body_sha256'], 'body_sha256')

    checks = record['checks']
    if not isinstance(checks, list):
        _err('checks must be a list')
    seen_ids = set()
    for index, check in enumerate(checks):
        where = f'checks[{index}]'
        if not isinstance(check, dict):
            _err(f'{where} must be an object')
        unknown = sorted(set(check) - set(CHECK_FIELDS))
        if unknown:
            _err(f'{where}: unknown field(s): {", ".join(unknown)}')
        missing = [field for field in CHECK_FIELDS if field not in check]
        if missing:
            _err(f'{where}: missing field(s): {", ".join(missing)}')
        _nonempty_str(check['id'], f'{where}.id')
        if check['id'] in seen_ids:
            _err(f'{where}.id duplicates an earlier check id')
        seen_ids.add(check['id'])
        if check['status'] not in STATUSES or not isinstance(check['status'], str):
            _err(f'{where}.status must be one of: {", ".join(STATUSES)}')
        if check['evidence_ref'] is not None:
            _nonempty_str(check['evidence_ref'], f'{where}.evidence_ref')

    feedback = record['feedback']
    if not isinstance(feedback, dict):
        _err('feedback must be an object')
    unknown = sorted(set(feedback) - set(FEEDBACK_FIELDS))
    if unknown:
        _err(f'feedback: unknown field(s): {", ".join(unknown)}')
    missing = [field for field in FEEDBACK_FIELDS if field not in feedback]
    if missing:
        _err(f'feedback: missing field(s): {", ".join(missing)}')
    if feedback['decision'] not in DECISIONS or not isinstance(feedback['decision'], str):
        _err(f'feedback.decision must be one of: {", ".join(DECISIONS)}')
    _hex_or_none(feedback['next_body_sha256'], 'feedback.next_body_sha256')

    return {'overall_status': evaluate(record)}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Validate a Skill Hub quality record.')
    parser.add_argument('record', help='path to a quality-record JSON file')
    args = parser.parse_args(argv)
    try:
        record = json.loads(Path(args.record).read_text(encoding='utf-8'))
        result = validate_record(record)
    except (OSError, ValueError) as error:  # json.JSONDecodeError is a ValueError
        print(json.dumps({'error': str(error)}))
        return 2
    print(json.dumps(result))
    return 0 if result['overall_status'] == 'pass' else 1


if __name__ == '__main__':
    sys.exit(main())
