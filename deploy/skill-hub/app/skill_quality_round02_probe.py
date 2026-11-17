#!/usr/bin/env python3
"""Round-02 probe: real stdio MCP -> identity -> quality-record CLI.

Drives a freshly started `skill_mcp.py` over a real stdio ClientSession, recomputes every
returned body hash locally, then feeds real records to `skill_quality.py` and records the
actual exit codes. Also shows the validator's blind spot: it is structural only, so a
tampered hash still exits 0 and this probe is what catches the mismatch.

Writes nothing but the report path you give it; fixtures live in disposable temp dirs.

Run: python skill_quality_round02_probe.py [--report PATH]
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = Path(__file__).resolve().parent
QUALITY = HERE / 'skill_quality.py'
CAP = 200_000
SHARED_TEXT = '---\nname: probe-body\ndescription: Plain fixture.\n---\n# Steps\ndo it\n'
FIXTURES = {
    # Identical text, LF endings.
    'probe-plain': (SHARED_TEXT, 'newline: plain LF'),
    # Same text with CRLF endings: it must hash like probe-plain.
    'probe-crlf': (SHARED_TEXT.replace('\n', '\r\n'), 'newline: CRLF'),
    'probe-long': ('x' * (CAP + 5000), 'body over the response cap'),
}


def normalize(text):
    return text.replace('\r\n', '\n').replace('\r', '\n')


def body_hash(text):
    return hashlib.sha256(normalize(text).encode('utf-8')).hexdigest()


def make_hub(root):
    hub = Path(root) / 'hub'
    for name, (body, _) in FIXTURES.items():
        tree = hub / 'skills' / name
        tree.mkdir(parents=True)
        (tree / 'SKILL.md').write_bytes(body.encode('utf-8'))
    return hub


@asynccontextmanager
async def server(hub):
    params = StdioServerParameters(command=sys.executable,
                                   args=[str(HERE / 'skill_mcp.py'), '--hub', str(hub)],
                                   cwd=str(HERE))
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            yield session


async def call(session, name):
    result = await session.call_tool('get_skill', {'name': name})
    return json.loads(result.content[0].text)


def record(skill, digest, checks):
    return {'schema_version': 1, 'skill': skill, 'body_sha256': digest,
            'host': 'probe-stdio', 'run_id': f'probe-{skill}',
            'checks': checks, 'feedback': {'decision': 'none', 'next_body_sha256': None}}


def run_cli(path):
    done = subprocess.run([sys.executable, str(QUALITY), str(path)],
                          capture_output=True, text=True, timeout=60)
    try:
        stdout = json.loads(done.stdout)
    except json.JSONDecodeError:
        stdout = {'unparsed': done.stdout[:200]}
    return {'exit_code': done.returncode, 'stdout': stdout,
            'stderr_head': done.stderr[:200]}


async def probe_isolated(root):
    """Fixture hub: identity, truncation, old fields, unknown skill, missing identity."""
    hub = make_hub(root)
    findings = {}
    async with server(hub) as session:
        names = sorted(tool.name for tool in (await session.list_tools()).tools)
        findings['tools'] = names
        for name in ('probe-plain', 'probe-crlf', 'probe-long'):
            payload = await call(session, name)
            identity = payload.get('identity') or {}
            returned = payload.get('body', '')
            expected = body_hash(returned)
            findings[name] = {
                'fields': sorted(payload),
                'identity_present': 'identity' in payload,
                'algorithm': identity.get('algorithm'),
                'declared_sha256': identity.get('returned_body_sha256'),
                'recomputed_sha256': expected,
                'hash_matches': identity.get('returned_body_sha256') == expected,
                'body_truncated': identity.get('body_truncated'),
                'body_len': len(returned),
                'body_within_cap': len(returned) <= CAP,
                'old_fields_present': all(k in payload for k in
                                          ('name', 'description', 'body', 'path', 'files')),
            }
        findings['unknown_skill'] = await call(session, 'probe-missing')
    return findings


async def probe_real():
    """The installed hub, read through a server this probe starts itself."""
    async with server(Path.home() / '.agent-hub') as session:
        payload = await call(session, 'skill-check')
    identity = payload.get('identity') or {}
    returned = payload.get('body', '')
    return {'skill': 'skill-check',
            'fields': sorted(payload),
            'identity_present': 'identity' in payload,
            'declared_sha256': identity.get('returned_body_sha256'),
            'recomputed_sha256': body_hash(returned),
            'hash_matches': identity.get('returned_body_sha256') == body_hash(returned),
            'body_truncated': identity.get('body_truncated'),
            'body_len': len(returned),
            'old_fields_present': all(k in payload for k in
                                      ('name', 'description', 'body', 'path', 'files'))}


def probe_quality(root, real):
    """Feed real records to the existing CLI; also show its structural-only blind spot."""
    out = Path(root) / 'records'
    out.mkdir()
    good = record('skill-check', real['declared_sha256'], [
        {'id': 'identity-recomputed', 'status': 'pass', 'evidence_ref': 'probe:real'},
        {'id': 'output-checked', 'status': 'pass', 'evidence_ref': 'probe:artifact'},
    ])
    failing = record('skill-check', real['declared_sha256'], [
        {'id': 'output-checked', 'status': 'fail', 'evidence_ref': None}])
    unknown = record('skill-check', real['declared_sha256'], [
        {'id': 'output-checked', 'status': 'pass', 'evidence_ref': None}])
    tampered = record('skill-check',
                      ('0' if real['declared_sha256'][0] != '0' else '1')
                      + real['declared_sha256'][1:],
                      [{'id': 'identity-recomputed', 'status': 'pass',
                        'evidence_ref': 'probe:real'}])
    invalid = dict(good, unexpected_payload='x')
    results = {}
    for name, value in (('pass', good), ('fail', failing), ('unknown', unknown),
                        ('tampered', tampered), ('invalid', invalid)):
        path = out / f'{name}.json'
        path.write_text(json.dumps(value), encoding='utf-8')
        results[name] = run_cli(path)
        results[name]['record_sha256'] = value['body_sha256']
    # The validator passes a tampered hash: it never sees the body. The probe is the check.
    results['tampered']['declared_matches_body'] = (
        results['tampered']['record_sha256'] == real['recomputed_sha256'])
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path,
                        default=HERE / 'skill-quality-round-02.probe.json')
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory() as temp:
        isolated = asyncio.run(probe_isolated(temp))
        real = asyncio.run(probe_real())
        quality = probe_quality(temp, real)

    checks = {
        'identity_present': isolated['probe-plain']['identity_present']
                            and real['identity_present'],
        'hash_matches_returned_body': all(
            isolated[n]['hash_matches'] for n in FIXTURES) and real['hash_matches'],
        'newline_normalization_stable': isolated['probe-plain']['declared_sha256']
                                        == isolated['probe-crlf']['declared_sha256'],
        'truncation_explicit': isolated['probe-long']['body_truncated'] is True
                               and isolated['probe-long']['body_within_cap']
                               and isolated['probe-plain']['body_truncated'] is False,
        'old_fields_preserved': all(isolated[n]['old_fields_present'] for n in FIXTURES)
                                and real['old_fields_present'],
        'unknown_skill_error_unchanged': isolated['unknown_skill'] == {
            'error': "'unknown skill: probe-missing'"},
        'cli_pass_exit0': quality['pass']['exit_code'] == 0
                          and quality['pass']['stdout'] == {'overall_status': 'pass'},
        'cli_fail_exit1': quality['fail']['exit_code'] == 1
                          and quality['fail']['stdout'] == {'overall_status': 'fail'},
        'cli_unknown_exit1': quality['unknown']['exit_code'] == 1
                             and quality['unknown']['stdout'] == {'overall_status': 'unknown'},
        'cli_invalid_exit2': quality['invalid']['exit_code'] == 2,
        'tampered_hash_detected_by_probe_only': quality['tampered']['exit_code'] == 0
                                                and not quality['tampered']['declared_matches_body'],
    }
    report = {
        'schema_version': 1,
        'source': 'fresh stdio ClientSession -> skill_mcp.get_skill; skill_quality.py CLI',
        'isolated_hub': isolated,
        'real_hub': real,
        'quality_cli': quality,
        'checks': checks,
        'failed': sorted(k for k, ok in checks.items() if not ok),
        'limits': [
            'Proves the response bytes and the CLI exit codes, not that any agent applied a skill.',
            'Identity covers the returned decoded body only: not disk bytes, not support files.',
            'Validator is structural; tampered hashes are detected here, not by the CLI.',
            'This server was started by the probe; it says nothing about the server the host loaded.',
        ],
    }
    args.report.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({'report': str(args.report), 'failed': report['failed'],
                      'pass': not report['failed']}))
    return 0 if not report['failed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
