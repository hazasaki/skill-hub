"""Catalog Ingest and Validation Module for AdaptiveSkillReflex Skill Hub.

Implements B1-B7 and D4/D5 requirements with strict CI gate validation:
- B1/D4: Content-addressed canonical body & alias identification for IDENTICAL bundles (SKILL.md + scripts/resources).
- B2: Divergent same-name versions/provenance preservation (never collapsed by name).
- B3: Robust frontmatter risk validation & normalization.
- B4: Frontmatter missing/malformed validation. Explicit legacy recovery separation. Missing frontmatter is REJECTED/QUARANTINED unless explicitly allowed.
- B5: Bundle reference validation (references/**, resources/**, scripts/**). Symlinks/junctions, non-existent references, or references resolving outside bundle root are REJECTED/QUARANTINED.
- B6/D5: Empty body rejection on load without arbitrary minimum length constraints.
- B7: Idempotent ingest & quarantine accounting.
"""
from __future__ import annotations

from bisect import bisect_right
from datetime import date
import hashlib
import json
import os
import re
import yaml
try:
    import rust_refs
except ImportError:
    rust_refs = None
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

FRONTMATTER_PATTERN = re.compile(r'^---\s*\n(.*?)\n---\s*\n', re.DOTALL)
MALFORMED_OPENING_PATTERN = re.compile(r'^(?:---|\+\+\+|===)', re.MULTILINE)

VALID_RISK_LEVELS = frozenset({'safe', 'low', 'medium', 'high', 'critical', 'offensive', 'unknown'})

RISK_SYNONYMS = {
    'none': 'unknown',
    'null': 'unknown',
    'absent': 'unknown',
    '': 'unknown',
    'clean': 'safe',
    'harmless': 'safe',
    'benign': 'safe',
    'danger': 'critical',
    'dangerous': 'critical',
    'malicious': 'critical',
    'exploit': 'offensive',
    'attack': 'offensive',
}

REFERENCE_PATTERNS = [
    re.compile(r'(?<![\w.-])(?:references|resources|scripts)/[A-Za-z0-9_.\-/]+'),
    re.compile(r'\[[^\[\]]*\]\(((?:references|resources|scripts)/[^)]+)\)'),
]

BUNDLE_DIRECTORIES = ('scripts', 'references', 'resources')


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _unique_mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise ValueError('frontmatter contains a non-string or duplicate key')
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def normalize_risk(raw_risk: Any) -> str:
    """Normalize risk level string into a validated enum value.
    
    Strips quotes, normalizes casing, maps known synonyms, defaults to 'unknown'.
    """
    if raw_risk is None:
        return 'unknown'
    text = str(raw_risk).strip().strip('\'"').lower()
    if text in RISK_SYNONYMS:
        return RISK_SYNONYMS[text]
    if text in VALID_RISK_LEVELS:
        return text
    return 'unknown'


def extract_fallback_metadata(text: str, default_name: str = '') -> Dict[str, Any]:
    """Extract name and description from markdown text for explicit legacy recovery."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    name = default_name
    description = ''
    for line in lines:
        if line.startswith('#'):
            heading = line.lstrip('#').strip()
            if not description:
                description = heading
        elif not description:
            description = line
        if description:
            break
    if not description:
        description = f'Skill package: {default_name}'
    return {
        'name': name,
        'description': description,
        'risk': 'unknown',
        'tags': '["general"]',
        '_synthetic_frontmatter': True,
        '_legacy_recovered': True,
    }


def _normalize_metadata_dates(fields):
    # Safe YAML produces date/datetime objects. Keep other values and alias
    # structure intact while making timestamp values usable in routing JSON.
    pending, visited = [fields], set()
    while pending:
        container = pending.pop()
        if id(container) in visited:
            continue
        visited.add(id(container))
        items = container.items() if isinstance(container, dict) else enumerate(container)
        for key, value in items:
            if isinstance(value, date):
                container[key] = value.isoformat()
            elif isinstance(value, (dict, list)):
                pending.append(value)


def parse_frontmatter(text: str, default_name: str = '') -> Tuple[Dict[str, Any], str, bool, Optional[str]]:
    """Parse safe YAML, rejecting malformed mappings and invalid risk declarations.
    
    Returns (metadata_dict, body_text, has_frontmatter, parse_error_or_none).
    """
    match = FRONTMATTER_PATTERN.match(text)
    if not match:
        # Check if text looks like it intended to have frontmatter but was malformed
        error = None
        if text.startswith('---'):
            error = 'malformed frontmatter: missing closing delimiter'
        elif text.lstrip().startswith('---'):
            error = 'malformed frontmatter: leading whitespace before opening delimiter'
        meta = extract_fallback_metadata(text, default_name)
        return meta, text, False, error

    raw_yaml = match.group(1)
    body = text[match.end():]
    try:
        fields = yaml.load(raw_yaml, Loader=_UniqueKeyLoader)
        if not isinstance(fields, dict):
            raise ValueError('frontmatter must be a mapping')
        for key in ('name', 'description', 'category', 'prompt_type'):
            if key in fields and not isinstance(fields[key], str):
                raise ValueError(f'{key} must be text')
        # `categories` is an authored list form of the same classification; it may be a
        # scalar or a list of non-empty strings, never a mapping.
        if 'categories' in fields:
            listed = fields['categories']
            if isinstance(listed, str):
                pass
            elif isinstance(listed, list):
                if any(not isinstance(item, str) for item in listed):
                    raise ValueError('categories must be text or a list of text')
            else:
                raise ValueError('categories must be text or a list of text')
        risk = fields.get('risk')
        if risk is not None:
            if not isinstance(risk, str):
                raise ValueError('risk must be a string enum')
            raw_risk = risk.strip().strip('\'"').lower()
            if raw_risk not in VALID_RISK_LEVELS and raw_risk not in RISK_SYNONYMS:
                raise ValueError('invalid risk enum')
        # Preserve the scalar database contract for tags while allowing YAML lists.
        for key in ('tags',):
            if isinstance(fields.get(key), (dict, list)):
                fields[key] = json.dumps(fields[key], ensure_ascii=False)
    except (yaml.YAMLError, ValueError, TypeError) as exc:
        return {}, body, True, 'malformed frontmatter: ' + str(exc)

    _normalize_metadata_dates(fields)

    # Normalize risk
    fields['risk'] = normalize_risk(fields.get('risk'))
    if 'name' not in fields and default_name:
        fields['name'] = default_name

    return fields, body, True, None


def content_digest(text: str) -> str:
    """Canonical content digest under normalized line endings."""
    normalized = text.replace('\r\n', '\n').replace('\r', '\n')
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()


def _bundle_files(tree: Path) -> List[Tuple[str, Path]]:
    """Every file in bundle as sorted relative posix paths.
    
    Rejects external symlinks and junctions.
    """
    tree = Path(tree)
    if tree.is_symlink() or (hasattr(tree, 'is_junction') and tree.is_junction()):
        raise ValueError('bundle root is a link')
    tree = tree.resolve()
    records: List[Tuple[str, Path]] = []

    def walk(directory: Path):
        for path in sorted(directory.iterdir()):
            try:
                relative = path.relative_to(tree).as_posix()
            except ValueError:
                raise ValueError(f'bundle file outside tree: {path}')
            if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
                raise ValueError(f'bundle contains a link: {relative}')
            if path.is_dir():
                walk(path)
            elif path.is_file():
                records.append((relative, path))
            else:
                raise ValueError(f'bundle contains a non-regular file: {relative}')

    walk(tree)
    return sorted(records)


def compute_bundle_digest(tree: Path) -> str:
    """Digest every served file; an unsafe/unreadable bundle has no valid digest."""
    files = _bundle_files(tree)
    rows = [{'path': rel, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
            for rel, p in files]
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


_URL_MARKER = re.compile(r'://|[wW][wW][wW]\.')
_URL_TAIL = re.compile(r"""[^\s<>"`]*""")
_URL_BRACKETS = re.compile(r'[()\[\]]')


def _remote_spans(text):
    """Stop a URI at an unmatched closer, retaining balanced URL paths and IPv6 hosts."""
    starts, ends = [], []
    pos = 0
    while match := _URL_MARKER.search(text, pos):
        pos = match.end()
        scheme = match.start()
        if match.group() == '://':
            while scheme > 0 and text[scheme - 1].isascii() and (text[scheme - 1].isalnum() or text[scheme - 1] in '+.-'):
                scheme -= 1
            if not any(ch.isalpha() for ch in text[scheme:match.start()]):
                continue
        end = _URL_TAIL.match(text, pos).end()
        if scheme > 0 and text[scheme - 1] == "'":
            quote = text.find("'", pos, end)
            if quote >= 0:
                end = quote
        brackets = []
        for mark in _URL_BRACKETS.finditer(text, match.start(), end):
            char = mark.group()
            if char in '([':
                brackets.append(char)
            elif not brackets or brackets.pop() != {')': '(', ']': '['}[char]:
                end = mark.start()
                break
        starts.append(match.start())
        ends.append(end)
        pos = end
    return starts, ends


def find_referenced_paths(text: str) -> List[str]:
    """Extract local references, including Unicode and whitespace in Markdown destinations."""
    native = rust_refs.offsets(text) if rust_refs is not None else None
    if native is not None:
        data, ranges = native
        source = data
    else:
        ranges = [match.span(match.lastindex or 0) for pattern in REFERENCE_PATTERNS
                  for match in pattern.finditer(text)]
        if not ranges:
            return []
        starts, ends = _remote_spans(text)
        def local(start):
            i = bisect_right(starts, start) - 1
            return i < 0 or start >= ends[i]
        ranges = [(start, end) for start, end in ranges if local(start)]
        source = text
    # A complete Markdown destination takes precedence over a shorter plain-text match.
    ends_by_start = {}
    for start, end in ranges:
        ends_by_start[start] = max(ends_by_start.get(start, end), end)
    found = set()
    for start, end in ends_by_start.items():
        path = source[start:end]
        if isinstance(path, bytes):
            path = path.decode('utf-8')
        if path.endswith('.') and not path.endswith(('..', '/.')):
            following = source[end:end + 4]
            if isinstance(following, bytes):
                following = following.decode('utf-8', errors='ignore')
            if not following or following[0].isspace():
                path = path[:-1]
        clean = path.strip().split('#')[0].split('?')[0].strip("'\"()[]")
        # A root-level ellipsis denotes an example command, not a portable filename.
        if clean and clean not in ('scripts/...', 'resources/...', 'references/...'):
            found.add(clean)
    return sorted(found)


def check_bundle_references(tree: Path, body_text: str) -> Tuple[List[str], List[str], List[str]]:
    """Verify whether referenced relative files exist inside the bundle and do not escape.
    
    Returns (existing_references, missing_references, escaping_references).
    """
    refs = find_referenced_paths(body_text)
    tree_resolved = tree.resolve()
    existing, missing, escaping = [], [], []
    for ref in refs:
        target = tree / ref
        try:
            resolved_target = target.resolve()
            if not resolved_target.is_relative_to(tree_resolved):
                escaping.append(ref)
                continue
            if target.is_symlink() or (hasattr(target, 'is_junction') and target.is_junction()):
                # Check target of link stays inside tree
                if not resolved_target.is_relative_to(tree_resolved):
                    escaping.append(ref)
                    continue
            if target.exists():
                existing.append(ref)
            else:
                missing.append(ref)
        except (ValueError, OSError):
            missing.append(ref)
    return existing, missing, escaping


@dataclass
class ValidationRecord:
    name: str
    kind: str  # 'skill' or 'prompt'
    path: str
    content_sha256: str
    bundle_sha256: str
    body_length: int
    is_valid: bool
    is_empty: bool
    has_frontmatter: bool
    risk: str
    missing_references: List[str] = field(default_factory=list)
    escaping_references: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


def validate_skill_entry(tree: Path, allow_legacy_recovery: bool = False) -> ValidationRecord:
    """Validate one skill tree directory according to strict Ingest CI Gate rules (B3, B4, B5, B6, D5).
    
    Rejections / Quarantine:
    - Missing SKILL.md -> is_valid = False
    - Empty body -> is_valid = False
    - Malformed or missing frontmatter -> is_valid = False (unless allow_legacy_recovery explicitly True)
    - Missing or escaping referenced files -> is_valid = False
    """
    name = tree.name
    doc = tree / 'SKILL.md'
    if (tree.is_symlink() or doc.is_symlink()
            or (hasattr(tree, 'is_junction') and tree.is_junction())):
        return ValidationRecord(
            name=name, kind='skill', path=str(tree), content_sha256='', bundle_sha256='',
            body_length=0, is_valid=False, is_empty=False, has_frontmatter=False,
            risk='unknown', errors=['bundle root or document is a link'])
    if not doc.is_file():
        return ValidationRecord(
            name=name, kind='skill', path=str(tree), content_sha256='', bundle_sha256='',
            body_length=0, is_valid=False, is_empty=True, has_frontmatter=False,
            risk='unknown', errors=['missing SKILL.md']
        )
    text = doc.read_text(encoding='utf-8', errors='replace')
    meta, body, has_frontmatter, parse_error = parse_frontmatter(text, default_name=name)
    body_stripped = body.strip()
    is_empty = len(body_stripped) == 0
    content_sha = content_digest(text)
    errors, warnings = [], []
    try:
        b_sha = compute_bundle_digest(tree)
    except (ValueError, OSError) as exc:
        b_sha = ''
        errors.append('invalid bundle: ' + str(exc))
    if is_empty:
        errors.append('empty body')
    
    if parse_error:
        errors.append(parse_error)
    elif not has_frontmatter:
        if allow_legacy_recovery:
            warnings.append('missing frontmatter (legacy recovered)')
        else:
            errors.append('missing frontmatter')
    
    _existing_refs, missing_refs, escaping_refs = check_bundle_references(tree, body)
    if escaping_refs:
        errors.append(f'{len(escaping_refs)} reference(s) escape bundle root: {escaping_refs}')
    if missing_refs:
        errors.append(f'{len(missing_refs)} missing referenced file(s): {missing_refs}')

    meta['missing_references'] = missing_refs
    meta['escaping_references'] = escaping_refs
    is_valid = len(errors) == 0

    return ValidationRecord(
        name=name, kind='skill', path=str(tree), content_sha256=content_sha,
        bundle_sha256=b_sha, body_length=len(body_stripped), is_valid=is_valid,
        is_empty=is_empty, has_frontmatter=has_frontmatter, risk=meta.get('risk', 'unknown'),
        missing_references=missing_refs, escaping_references=escaping_refs,
        errors=errors, warnings=warnings, metadata=meta
    )


def validate_prompt_entry(path: Path, allow_legacy_recovery: bool = False) -> ValidationRecord:
    """Validate one prompt file (B3, B4, B6, D5)."""
    name = path.stem
    if path.is_symlink():
        return ValidationRecord(
            name=name, kind='prompt', path=str(path), content_sha256='', bundle_sha256='',
            body_length=0, is_valid=False, is_empty=False, has_frontmatter=False,
            risk='unknown', errors=['prompt is a link'])
    if not path.is_file():
        return ValidationRecord(
            name=name, kind='prompt', path=str(path), content_sha256='', bundle_sha256='',
            body_length=0, is_valid=False, is_empty=True, has_frontmatter=False,
            risk='unknown', errors=['missing file']
        )
    text = path.read_text(encoding='utf-8', errors='replace')
    meta, body, has_frontmatter, parse_error = parse_frontmatter(text, default_name=name)
    body_stripped = body.strip()
    is_empty = len(body_stripped) == 0
    sha = content_digest(text)
    
    errors, warnings = [], []
    if is_empty:
        errors.append('empty body')
    if parse_error:
        errors.append(parse_error)
    elif not has_frontmatter:
        if allow_legacy_recovery:
            warnings.append('missing frontmatter (legacy recovered)')
        else:
            errors.append('missing frontmatter')

    is_valid = len(errors) == 0
    return ValidationRecord(
        name=name, kind='prompt', path=str(path), content_sha256=sha, bundle_sha256=sha,
        body_length=len(body_stripped), is_valid=is_valid, is_empty=is_empty,
        has_frontmatter=has_frontmatter, risk=meta.get('risk', 'unknown'),
        errors=errors, warnings=warnings, metadata=meta
    )


def select_canonical_name(names: List[str]) -> str:
    """Choose the best canonical name from a set of names sharing identical content.
    
    Prefers clean base name without '--<source>' suffix; falls back to shortest/alphabetical.
    """
    clean_names = [n for n in names if '--' not in n]
    if clean_names:
        return sorted(clean_names, key=lambda n: (len(n), n))[0]
    return sorted(names, key=lambda n: (len(n), n))[0]


def group_canonical_and_aliases(records: List[ValidationRecord]) -> Tuple[Dict[str, str], Dict[str, List[str]], List[str]]:
    """Group records by full bundle identity: (bundle_sha256, content_sha256, risk).
    
    CRITICAL SAFETY RULES:
    1. Identical content + identical scripts/resources + identical risk/metadata:
       -> Safe to alias. Selects 1 canonical, others are aliases (D4).
    2. Same SKILL.md body BUT different scripts/resources (different bundle_sha256):
       -> DISTINCT BUNDLES! Never collapsed into aliases. Both remain distinct canonicals.
    3. Same body BUT different risk/permissions:
       -> DISTINCT PERMISSION IDENTITIES! Never collapsed into aliases. Both remain distinct.
    4. Divergent content:
       -> Distinct records each become their own canonical (B2).
    
    Returns:
    - alias_to_canonical: map from alias_name -> canonical_name
    - canonical_to_aliases: map from canonical_name -> list of alias names
    - canonical_names: set/list of all canonical names
    """
    by_identity: Dict[Tuple[str, str, str, str], List[ValidationRecord]] = {}
    for r in records:
        if not r.is_valid or not r.bundle_sha256:
            continue
        key = (r.kind, r.bundle_sha256, r.content_sha256, r.risk)
        by_identity.setdefault(key, []).append(r)

    alias_to_canonical: Dict[str, str] = {}
    canonical_to_aliases: Dict[str, List[str]] = {}
    canonical_names: List[str] = []

    for _identity_key, group in by_identity.items():
        if len(group) == 1:
            canonical = group[0].name
            canonical_names.append(canonical)
            canonical_to_aliases[canonical] = []
        else:
            names = [r.name for r in group]
            canonical = select_canonical_name(names)
            canonical_names.append(canonical)
            aliases = [n for n in names if n != canonical]
            canonical_to_aliases[canonical] = aliases
            for alias in aliases:
                alias_to_canonical[alias] = canonical

    return alias_to_canonical, canonical_to_aliases, sorted(canonical_names)


def quarantine_accounting(skills_root: Path, prompts_root: Optional[Path] = None, allow_legacy_recovery: bool = False) -> Dict[str, Any]:
    """Perform non-destructive accounting of catalog health and quarantine status (B7)."""
    skills_root = Path(skills_root)
    records: List[ValidationRecord] = []
    
    if skills_root.is_dir():
        for tree in sorted(p for p in skills_root.iterdir() if p.is_dir()):
            records.append(validate_skill_entry(tree, allow_legacy_recovery=allow_legacy_recovery))
            
    if prompts_root and prompts_root.is_dir():
        for file in sorted(p for p in prompts_root.iterdir() if p.is_file()):
            records.append(validate_prompt_entry(file, allow_legacy_recovery=allow_legacy_recovery))

    total = len(records)
    empty_bodies = [r for r in records if r.is_empty]
    missing_frontmatters = [r for r in records if not r.has_frontmatter and not r.is_empty]
    broken_refs = [r for r in records if r.missing_references or r.escaping_references]
    invalid_records = [r for r in records if not r.is_valid]
    valid = [r for r in records if r.is_valid]

    alias_to_canon, canon_to_aliases, canon_names = group_canonical_and_aliases(valid)

    # Count divergent groups by base name
    base_name_groups: Dict[str, Set[str]] = {}
    for r in valid:
        base = r.name.split('--')[0]
        base_name_groups.setdefault(base, set()).add(r.bundle_sha256 or r.content_sha256)
    divergent_groups = {base: hashes for base, hashes in base_name_groups.items() if len(hashes) > 1}

    return {
        'total_scanned': total,
        'valid_count': len(valid),
        'invalid_count': len(invalid_records),
        'empty_body_count': len(empty_bodies),
        'empty_body_names': [r.name for r in empty_bodies],
        'missing_frontmatter_count': len(missing_frontmatters),
        'broken_reference_count': len(broken_refs),
        'canonical_count': len(canon_names),
        'identical_alias_count': len(alias_to_canon),
        'divergent_groups_count': len(divergent_groups),
        'quarantine_candidates': [
            {'name': r.name, 'kind': r.kind, 'reason': '; '.join(r.errors)}
            for r in invalid_records
        ],
    }
