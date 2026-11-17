"""GitHub Skill & Prompt Intake Module for AdaptiveSkillReflex Skill Hub.

Implements repeatable automated intake, quality evaluation, provenance pinning,
and candidate staging for weekly 7-day routine runs.

Quality Rubric Dimensions:
1. Genuine Task Utility: Real problem domain, structured actionable workflow.
2. Maintained Content: Upstream commit freshness, non-deprecated formats.
3. Author/Source Provenance: Repo URL, full 40-hex commit SHA, file path, author, commit date.
4. License Audit: Permissive OSI, Attribution, Copyleft, or Unclear license classification.
5. References & Bundle Completeness: Validates all referenced relative files inside bundle.
6. Duplicate & Near-Duplicate Overlap: Exact SHA+bundle match skips; updated versions preserved separately.
7. Prompt Injection & Dangerous Side Effects: Scans for injection vectors and offensive exploit capabilities.
8. Runtime Compatibility & Auditability: Strict frontmatter validation and content/bundle digests.

Classification Categories:
- NEW_READY: High quality, safe, fully validated, complete bundle without unreviewed scripts, staged for reviewed integration.
- REVIEW_REQUIRED: Genuine utility but contains unreviewed scripts, platform constraints, or attribution requirements.
- NEW_QUARANTINED: Incomplete metadata, missing frontmatter, broken references, incomplete bundle, or unclear license.
- UPDATED: Existing skill with new upstream revision; pinned and held without live overwrite.
- EXACT_DUPLICATE: Matches existing catalog content_sha256 AND bundle_sha256; skipped.
- REJECTED: Prompt injection, offensive exploit/attack capabilities, malicious execution, empty body, path escape.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import sys
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# Ensure current and parent dirs are on sys.path for direct or module execution
_curr_dir = Path(__file__).resolve().parent
if str(_curr_dir) not in sys.path:
    sys.path.insert(0, str(_curr_dir))
_parent_dir = _curr_dir.parent
if str(_parent_dir) not in sys.path:
    sys.path.insert(0, str(_parent_dir))

try:
    import catalog_ingest
except ImportError:
    from agent_memory import catalog_ingest


from shared_resources import DEFAULT_HUB

DEFAULT_DB_PATH = DEFAULT_HUB / "state/skill-index.db"
DEFAULT_STAGING_DIR = DEFAULT_HUB / "state/intake/staging"

PERMISSIVE_LICENSES = frozenset({
    "mit", "apache-2.0", "bsd-2-clause", "bsd-3-clause", "isc",
    "mit-0", "unlicense", "0bsd", "cc0-1.0"
})

ATTRIBUTION_LICENSES = frozenset({
    "cc-by-4.0", "cc-by-3.0", "mpl-2.0"
})

COPYLEFT_LICENSES = frozenset({
    "gpl-3.0", "gpl-2.0", "agpl-3.0", "lgpl-3.0", "lgpl-2.1", "cc-by-nc-4.0", "cc-by-nd-4.0"
})

OFFENSIVE_OR_MALICIOUS_PATTERNS = [
    re.compile(r'\b(dnscat2|icmpsh|ptunnel|steghide)\b', re.IGNORECASE),
    re.compile(r'\b(reverse[\s_-]shell|bind[\s_-]shell|shellcode|mimikatz|pass-the-hash)\b', re.IGNORECASE),
    re.compile(r'(?:nc|ncat|netcat)\s+-[ecl]', re.IGNORECASE),
    re.compile(r'/etc/shadow|/etc/passwd', re.IGNORECASE),
    re.compile(r'\bcovert\s+data\s+exfiltration\b', re.IGNORECASE),
    re.compile(r'\bcredential[\s_-]dump\b', re.IGNORECASE),
]

PROMPT_INJECTION_PATTERNS = [
    re.compile(r'ignore\s+(?:all\s+)?previous\s+instructions', re.IGNORECASE),
    re.compile(r'disregard\s+(?:all\s+)?prior\s+instructions', re.IGNORECASE),
    re.compile(r'you\s+are\s+now\s+in\s+developer\s+mode', re.IGNORECASE),
    re.compile(r'system\s+prompt\s+override', re.IGNORECASE),
    re.compile(r'\bjailbreak\b', re.IGNORECASE),
]

SCRIPT_EXTENSIONS = frozenset({
    ".py", ".sh", ".bash", ".ps1", ".js", ".ts", ".psjs", ".rb", ".pl"
})

EXTERNAL_MUTABLE_DOC_PATTERNS = [
    re.compile(r'https?://(?:raw\.githubusercontent\.com/wiki/|github\.com/[^/]+/[^/]+/wiki)', re.IGNORECASE),
    re.compile(r'fetch the latest version from (?:the )?raw URLs', re.IGNORECASE),
    re.compile(r'(?:WebFetch|curl|browser|built-in web tools)\s+to\s+(?:fetch|reference)\s+docs', re.IGNORECASE),
    re.compile(r'https?://raw\.githubusercontent\.com/[^/\s]+/[^/\s]+/wiki', re.IGNORECASE),
]

PLATFORM_DEPENDENCY_PATTERNS = [
    re.compile(r'(?:requires|prerequisites?[:\s]+.*)\b(?:macOS|Xcode|xcframework|iOS SDK|Android SDK|Visual Studio|CUDA)\b', re.IGNORECASE),
    re.compile(r'\b(?:SwiftBindings\.Sdk|\.NET MAUI|dotnet new swift-binding)\b', re.IGNORECASE),
    re.compile(r'Apple platform binding generation requires macOS \+ Xcode', re.IGNORECASE),
]

RESERVED_DEVICE_NAMES = frozenset({
    "con", "prn", "aux", "nul",
    "com1", "com2", "com3", "com4", "com5", "com6", "com7", "com8", "com9",
    "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9"
})

HEX_40_PATTERN = re.compile(r'^[0-9a-fA-F]{40}$')


def is_valid_40hex_commit(sha: Optional[str]) -> bool:
    """Validate that sha is a genuine 40-character hexadecimal commit hash."""
    return bool(sha and HEX_40_PATTERN.match(sha.strip()))


def write_staged_file_immutable(dest_file: Path, content_bytes: bytes) -> Path:
    """Write content_bytes to dest_file enforcing strict immutability.
    
    If dest_file already exists:
    - Identical bytes: reuse existing file without mutation.
    - Divergent bytes: raise ValueError (immutable staging violation).
    """
    if dest_file.exists():
        existing = dest_file.read_bytes()
        if existing == content_bytes:
            return dest_file
        raise ValueError(
            f"Immutable staging violation: file already exists with different bytes at '{dest_file}'. "
            f"Existing length {len(existing)}, new length {len(content_bytes)}."
        )
    dest_file.parent.mkdir(parents=True, exist_ok=True)
    dest_file.write_bytes(content_bytes)
    return dest_file


def assert_safe_path(base_dir: Path, rel_path: str) -> Path:
    r"""Validate that rel_path is strictly contained within base_dir.
    
    Rejects:
    - Drive-qualified paths ('C:\...', 'D:...')
    - Alternate Data Streams (ADS, containing ':')
    - Parent directory traversal ('..' or resolving outside base_dir)
    - Windows reserved device names (CON, NUL, AUX, PRN, COM*, LPT*)
    - Symlinks / junctions / reparse points
    """
    if not rel_path or rel_path.strip() == "":
        return base_dir.resolve()

    # Reject Alternate Data Streams and drive letters (colon anywhere in rel_path)
    if ":" in rel_path:
        raise ValueError(f"Path traversal / ADS rejected: '{rel_path}' contains ':'")

    # Check for reserved device names and traversal tokens in components
    parts = Path(rel_path).parts
    for part in parts:
        stem = part.split(".")[0].lower()
        if stem in RESERVED_DEVICE_NAMES:
            raise ValueError(f"Path contains reserved DOS device name: '{part}'")
        if part == "..":
            raise ValueError(f"Path traversal '..' rejected: '{rel_path}'")

    resolved_base = base_dir.resolve()
    target = (resolved_base / rel_path).resolve()

    # Must be strictly contained in resolved_base
    if resolved_base not in target.parents and target != resolved_base:
        raise ValueError(f"Path '{rel_path}' resolves outside base directory '{resolved_base}'")

    # Check if any parent or target itself is a symlink / reparse point
    curr = target
    while curr != resolved_base and curr != curr.parent:
        if curr.is_symlink() or os.path.islink(curr):
            raise ValueError(f"Symlink / reparse point detected at '{curr}'")
        curr = curr.parent

    return target


def verify_git_blob_sha(content_bytes: bytes, expected_blob_sha: Optional[str]) -> Optional[bool]:
    """Compute Git blob SHA (sha1(f"blob {len}\0" + bytes)) and verify match.
    
    Returns:
      True: expected SHA provided and matches computed blob SHA.
      False: expected SHA provided and DOES NOT match computed blob SHA.
      None: missing expected blob SHA header (explicitly unverified, NOT True).
    """
    if not expected_blob_sha or not expected_blob_sha.strip():
        return None
    header = f"blob {len(content_bytes)}\0".encode("utf-8")
    computed = hashlib.sha1(header + content_bytes).hexdigest()
    return computed.lower() == expected_blob_sha.strip().lower()


@dataclass
class QualityRubricResult:
    utility_score: float  # 0.0 - 1.0
    maintenance_score: float  # 0.0 - 1.0
    provenance_score: float  # 0.0 - 1.0
    license_score: float  # 0.0 - 1.0
    bundle_score: float  # 0.0 - 1.0
    duplicate_score: float  # 0.0 - 1.0 (1.0 = distinct, 0.0 = exact dupe)
    safety_score: float  # 0.0 - 1.0 (1.0 = completely safe, 0.0 = malicious)
    compatibility_score: float  # 0.0 - 1.0
    overall_score: float  # 0.0 - 10.0
    passed_all_gates: bool
    has_scripts: bool = False
    has_external_dependencies: bool = False
    reasons: List[str] = field(default_factory=list)


@dataclass
class CandidateProvenance:
    repo_url: str
    commit_sha: str
    relative_path: str
    author: str
    commit_date: str
    license_spdx: str
    blob_sha: Optional[str] = None
    blob_sha_verified: Optional[bool] = None  # True = match, False = mismatch, None = unverified
    license_commit: Optional[str] = None
    license_status: str = "VERIFIED"  # "VERIFIED", "STALE_COMMIT", "UNCERTAIN_MIXED", "UNKNOWN"


@dataclass
class IntakeCandidateRecord:
    candidate_id: str
    name: str
    kind: str  # 'skill' or 'prompt'
    provenance: CandidateProvenance
    content_sha256: str
    bundle_sha256: Optional[str]
    body_length: int
    classification: str  # 'NEW_READY', 'REVIEW_REQUIRED', 'NEW_QUARANTINED', 'UPDATED', 'EXACT_DUPLICATE', 'REJECTED'
    risk_level: str
    rubric: QualityRubricResult
    staging_path: str
    catalog_match: Optional[Dict[str, Any]] = None
    quarantine_reasons: List[str] = field(default_factory=list)
    active: bool = False  # NEVER active on intake


class GitHubIntakeScanner:
    """Intake scanner and evaluator for GitHub skill and prompt candidates."""

    def __init__(self, db_path: Optional[Path] = None, staging_dir: Optional[Path] = None):
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self.staging_dir = Path(staging_dir) if staging_dir else DEFAULT_STAGING_DIR
        self.catalog_pin_hash = None
        self.catalog_cache: Dict[str, Any] = {
            "skills": {},
            "prompts": {},
            "content_hashes": set(),
            "bundle_hashes": set(),
            "exact_bundles": set(),  # Set of (content_sha256, bundle_sha256)
        }
        self._load_catalog_cache()

    def _load_catalog_cache(self):
        """Read-only catalog cache load for fast duplicate and update checking."""
        if not self.db_path.is_file():
            return

        try:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            cur = conn.cursor()

            # Load skills
            for row in cur.execute("SELECT name, content_sha256, bundle_sha256, active, risk FROM skill_resources;"):
                name, c_sha, b_sha, active, risk = row
                self.catalog_cache["skills"][name] = {
                    "content_sha256": c_sha,
                    "bundle_sha256": b_sha,
                    "active": active,
                    "risk": risk
                }
                if c_sha:
                    self.catalog_cache["content_hashes"].add(c_sha)
                if b_sha:
                    self.catalog_cache["bundle_hashes"].add(b_sha)
                if c_sha and b_sha:
                    self.catalog_cache["exact_bundles"].add((c_sha, b_sha))

            # Load prompts
            for row in cur.execute("SELECT name, content_sha256, body_sha256, active, risk FROM prompt_resources;"):
                name, c_sha, b_sha, active, risk = row
                self.catalog_cache["prompts"][name] = {
                    "content_sha256": c_sha,
                    "bundle_sha256": b_sha,
                    "active": active,
                    "risk": risk
                }
                if c_sha:
                    self.catalog_cache["content_hashes"].add(c_sha)
                if b_sha:
                    self.catalog_cache["bundle_hashes"].add(b_sha)

            conn.close()
            with self.db_path.open('rb') as source:
                self.catalog_pin_hash = hashlib.file_digest(source, 'sha256').hexdigest()
        except sqlite3.Error as exc:
            print(f"Warning: could not load read-only catalog cache: {exc}", file=sys.stderr)

    def evaluate_quality_rubric(
        self,
        name: str,
        kind: str,
        text: str,
        val_rec: catalog_ingest.ValidationRecord,
        prov: CandidateProvenance,
        tree_path: Optional[Path] = None
    ) -> QualityRubricResult:
        """Run the 8-dimension quality rubric evaluation."""
        reasons = []

        # 1. Genuine Task Utility
        body_len = val_rec.body_length
        has_headings = bool(re.search(r'^#{1,3}\s+', text, re.MULTILINE))
        has_workflow = bool(re.search(r'\b(workflow|steps?|procedure|instructions?|usage|operating|quickstart)\b', text, re.IGNORECASE))
        if body_len < 100:
            utility_score = 0.2
            reasons.append("utility: body too short or placeholder (<100 chars)")
        elif has_headings and has_workflow and body_len >= 300:
            utility_score = 1.0
        elif has_headings or has_workflow:
            utility_score = 0.8
        else:
            utility_score = 0.5
            reasons.append("utility: missing clear workflow or markdown structure")

        # 2. Maintained Content
        maintenance_score = 1.0
        if prov.commit_date:
            try:
                c_year = int(prov.commit_date[:4])
                if c_year < 2024:
                    maintenance_score = 0.4
                    reasons.append(f"maintenance: commit date older than 2024 ({prov.commit_date[:10]})")
            except Exception:
                maintenance_score = 0.5
        else:
            maintenance_score = 0.5
            reasons.append("maintenance: missing commit timestamp")

        # 3. Provenance
        provenance_score = 1.0
        if not prov.repo_url or not prov.repo_url.startswith("https://github.com/"):
            provenance_score = 0.0
            reasons.append("provenance: invalid or missing GitHub repo URL")
        if not prov.commit_sha or not is_valid_40hex_commit(prov.commit_sha):
            provenance_score = 0.0
            reasons.append("provenance: missing or invalid 40-hex commit SHA")
        if prov.blob_sha_verified is False:
            provenance_score = 0.0
            reasons.append("provenance: Git blob SHA mismatch against GitHub MCP header")
        elif prov.blob_sha_verified is None:
            # Explicitly unverified Git blob header (missing header from evidence)
            reasons.append("provenance: Git blob SHA unverified (missing header from evidence)")

        # 4. License Audit (Never fabricate permissive license, pin to same commit)
        license_clean = (prov.license_spdx or "").strip().lower()
        if prov.license_status == "STALE_COMMIT":
            license_score = 0.3
            reasons.append(f"license: stale license commit (resource commit {prov.commit_sha[:8]} != license commit {str(prov.license_commit)[:8]}) (quarantine requirement)")
        elif prov.license_status == "UNCERTAIN_MIXED":
            license_score = 0.2
            reasons.append("license: uncertain or conflicting mixed license terms in repository (quarantine requirement)")
        elif license_clean in PERMISSIVE_LICENSES:
            license_score = 1.0
        elif license_clean in ATTRIBUTION_LICENSES:
            license_score = 0.8
            reasons.append(f"license: attribution license ({prov.license_spdx})")
        elif license_clean in COPYLEFT_LICENSES:
            license_score = 0.6
            reasons.append(f"license: copyleft or non-commercial license ({prov.license_spdx})")
        elif not license_clean or license_clean in {"null", "none", "noassertion", "unknown", "uncertain"}:
            license_score = 0.2
            reasons.append(f"license: missing, undeclared, or uncertain license ({prov.license_spdx or 'none'}) (quarantine requirement)")
        else:
            license_score = 0.4
            reasons.append(f"license: unrecognized license: {prov.license_spdx}")

        # 5. References & Bundle Completeness
        if val_rec.escaping_references:
            bundle_score = 0.0
            reasons.append(f"bundle: escaping references detected: {val_rec.escaping_references}")
        elif val_rec.missing_references:
            bundle_score = 0.2
            reasons.append(f"bundle: missing referenced files: {val_rec.missing_references}")
        else:
            bundle_score = 1.0

        # Check for scripts in bundle
        has_scripts = False
        script_files = []
        if tree_path and tree_path.is_dir():
            for p in tree_path.rglob("*"):
                if p.is_file() and p.suffix.lower() in SCRIPT_EXTENSIONS:
                    has_scripts = True
                    script_files.append(p.name)
        if has_scripts:
            reasons.append(f"scripts: bundle contains scripts: {', '.join(script_files[:3])}")

        # 6. Duplicate & Overlap Check (Whole-Bundle Semantics)
        exact_dupe = False

        if kind == "skill":
            # Exact duplicate ONLY if both content AND bundle hash match
            if val_rec.bundle_sha256 and (val_rec.content_sha256, val_rec.bundle_sha256) in self.catalog_cache["exact_bundles"]:
                exact_dupe = True
                duplicate_score = 0.0
                reasons.append("duplicate: exact content_sha256 AND bundle_sha256 match catalog")
            elif val_rec.content_sha256 in self.catalog_cache["content_hashes"]:
                duplicate_score = 0.8
                reasons.append("duplicate: same SKILL.md body, but bundle scripts/resources differ or incomplete (bundle variant)")
            else:
                duplicate_score = 1.0
        else:
            # Prompt is single file
            if val_rec.content_sha256 in self.catalog_cache["content_hashes"]:
                exact_dupe = True
                duplicate_score = 0.0
                reasons.append("duplicate: exact prompt content_sha256 exists in catalog")
            else:
                duplicate_score = 1.0

        # 7. Prompt Injection & Dangerous Side Effects
        safety_score = 1.0
        for pat in PROMPT_INJECTION_PATTERNS:
            if pat.search(text):
                safety_score = 0.0
                reasons.append(f"safety: prompt injection pattern detected: {pat.pattern}")
                break

        for pat in OFFENSIVE_OR_MALICIOUS_PATTERNS:
            if pat.search(text):
                safety_score = 0.0
                reasons.append(f"safety: offensive exploit/malicious payload pattern detected: {pat.pattern}")
                break

        # 8. Runtime Compatibility & Auditability (Real bundle closure & platform dependencies)
        external_docs = any(p.search(text) for p in EXTERNAL_MUTABLE_DOC_PATTERNS)
        platform_deps = any(p.search(text) for p in PLATFORM_DEPENDENCY_PATTERNS)
        has_external_dependencies = bool(external_docs or platform_deps)

        if not val_rec.has_frontmatter:
            compatibility_score = 0.3
            reasons.append("compatibility: missing standard YAML frontmatter")
        elif val_rec.errors:
            compatibility_score = 0.0
            reasons.append(f"compatibility: validation errors: {val_rec.errors}")
        elif has_external_dependencies:
            # External dependencies preclude claiming 1.0 compatibility or complete bundle closure from SKILL alone
            compatibility_score = 0.6
            if external_docs:
                reasons.append("compatibility: external mutable documentation/wiki dependencies detected")
            if platform_deps:
                reasons.append("compatibility: external runtime platform dependencies detected (macOS/Xcode/.NET)")
        else:
            compatibility_score = 1.0

        # Overall weighted score out of 10.0
        passed_all_gates = (
            safety_score > 0.0 and
            bundle_score > 0.5 and  # Requires complete bundle (no missing references)
            provenance_score > 0.5 and
            val_rec.is_valid and
            not exact_dupe and
            license_score >= 0.5 and
            not val_rec.missing_references and  # Incomplete bundle fails gate
            not has_external_dependencies and  # Real bundle closure required for NEW_READY
            prov.blob_sha_verified is not False  # Mismatch fails gate
        )

        overall = (
            utility_score * 2.0 +
            maintenance_score * 1.0 +
            provenance_score * 1.5 +
            license_score * 1.5 +
            bundle_score * 1.0 +
            duplicate_score * 1.0 +
            safety_score * 1.0 +
            compatibility_score * 1.0
        )

        return QualityRubricResult(
            utility_score=round(utility_score, 2),
            maintenance_score=round(maintenance_score, 2),
            provenance_score=round(provenance_score, 2),
            license_score=round(license_score, 2),
            bundle_score=round(bundle_score, 2),
            duplicate_score=round(duplicate_score, 2),
            safety_score=round(safety_score, 2),
            compatibility_score=round(compatibility_score, 2),
            overall_score=round(overall, 2),
            passed_all_gates=passed_all_gates,
            has_scripts=has_scripts,
            has_external_dependencies=has_external_dependencies,
            reasons=reasons
        )

    def classify_candidate(
        self,
        name: str,
        kind: str,
        val_rec: catalog_ingest.ValidationRecord,
        rubric: QualityRubricResult,
        prov: CandidateProvenance
    ) -> Tuple[str, List[str]]:
        """Classify candidate into NEW_READY, REVIEW_REQUIRED, NEW_QUARANTINED, UPDATED, EXACT_DUPLICATE, or REJECTED."""
        # 1. Critical Rejection: Security / Malicious / Escaping references / Empty body / Blob mismatch
        rejection_reasons = []
        if rubric.safety_score == 0.0:
            rejection_reasons.extend([r for r in rubric.reasons if "safety:" in r])
        if val_rec.is_empty:
            rejection_reasons.append("empty body")
        if val_rec.escaping_references:
            rejection_reasons.append(f"escaping references: {val_rec.escaping_references}")
        if any("Git blob SHA mismatch" in err for err in val_rec.errors) or prov.blob_sha_verified is False:
            rejection_reasons.append("Git blob SHA mismatch against GitHub MCP header")
        if rejection_reasons:
            return "REJECTED", rejection_reasons

        # 2. Incomplete bundle check: an unverified/missing bundle can NEVER be NEW_READY or clean UPDATED
        if val_rec.missing_references:
            return "NEW_QUARANTINED", [
                f"incomplete bundle: missing referenced files: {val_rec.missing_references}"
            ]

        # 3. Check for exact duplicate (both content and bundle SHA match catalog)
        existing_table = self.catalog_cache["skills"] if kind == "skill" else self.catalog_cache["prompts"]
        if name in existing_table:
            old_c_sha = existing_table[name]["content_sha256"]
            old_b_sha = existing_table[name].get("bundle_sha256")

            # Check if bundle is incomplete/unverified
            if kind == "skill" and val_rec.bundle_sha256 is None:
                return "NEW_QUARANTINED", [
                    f"content matches catalog ({old_c_sha[:8]}), but candidate bundle is unverified/null; held inactive for review"
                ]

            if old_c_sha == val_rec.content_sha256 and old_b_sha == val_rec.bundle_sha256:
                return "EXACT_DUPLICATE", [f"identical content and bundle SHA match catalog for {name}"]
            elif old_c_sha == val_rec.content_sha256 and old_b_sha != val_rec.bundle_sha256:
                return "UPDATED", [
                    f"content SHA matches catalog ({old_c_sha[:8]}), but bundle SHA differs (catalog {old_b_sha[:8] if old_b_sha else 'none'} != upstream {val_rec.bundle_sha256[:8] if val_rec.bundle_sha256 else 'none'})"
                ]
            elif old_c_sha != val_rec.content_sha256:
                return "UPDATED", [
                    f"new upstream revision for {name} (catalog {old_c_sha[:8]} != upstream {val_rec.content_sha256[:8]})"
                ]

        if rubric.duplicate_score == 0.0:
            return "EXACT_DUPLICATE", ["identical content_sha256 AND bundle_sha256 match catalog"]

        # 4. Quarantine conditions (missing frontmatter, missing/unclear license, stale license commit)
        quarantine_reasons = []
        if not val_rec.has_frontmatter:
            quarantine_reasons.append("missing standard YAML frontmatter")
        if prov.license_status == "STALE_COMMIT":
            quarantine_reasons.append(f"stale license commit (resource commit {prov.commit_sha[:8]} != license commit {str(prov.license_commit)[:8]})")
        elif prov.license_status == "UNCERTAIN_MIXED":
            quarantine_reasons.append("uncertain or conflicting mixed license terms in repository")
        elif rubric.license_score < 0.5:
            quarantine_reasons.append(f"unclear or missing license ({prov.license_spdx or 'none'})")

        if quarantine_reasons:
            return "NEW_QUARANTINED", quarantine_reasons

        # 5. Review Required conditions:
        # - Bundle contains scripts (held inactive pending review)
        # - External mutable wiki or platform dependencies (real bundle closure required)
        # - Attribution license (CC-BY-4.0)
        # - Copyleft license (GPL, LGPL)
        review_reasons = []
        if rubric.has_scripts:
            review_reasons.append("bundle contains scripts; held inactive pending manual security review")
        if rubric.has_external_dependencies:
            review_reasons.append("bundle relies on external mutable wiki/runtime platform dependencies; requires review before promotion")
        license_clean = (prov.license_spdx or "").strip().lower()
        if license_clean in ATTRIBUTION_LICENSES:
            review_reasons.append(f"requires attribution compliance review ({prov.license_spdx})")
        elif license_clean in COPYLEFT_LICENSES:
            review_reasons.append(f"requires copyleft licensing review ({prov.license_spdx})")

        if review_reasons:
            return "REVIEW_REQUIRED", review_reasons

        # 6. Clean, high quality new candidate
        if rubric.passed_all_gates:
            return "NEW_READY", []

        return "NEW_QUARANTINED", rubric.reasons

    def evaluate_skill_candidate(
        self,
        tree_path: Path,
        prov: CandidateProvenance
    ) -> IntakeCandidateRecord:
        """Evaluate a staged skill bundle directory."""
        val_rec = catalog_ingest.validate_skill_entry(tree_path)
        skill_md = tree_path / "SKILL.md"
        text = skill_md.read_text(encoding="utf-8", errors="replace") if skill_md.is_file() else ""
        name = val_rec.name

        # If references are missing, bundle is incomplete -> bundle_sha256 cannot be asserted as complete pin
        bundle_sha = val_rec.bundle_sha256 if not val_rec.missing_references else None

        rubric = self.evaluate_quality_rubric(name, "skill", text, val_rec, prov, tree_path=tree_path)
        classification, q_reasons = self.classify_candidate(name, "skill", val_rec, rubric, prov)

        catalog_match = None
        if name in self.catalog_cache["skills"]:
            catalog_match = self.catalog_cache["skills"][name]

        return IntakeCandidateRecord(
            candidate_id=f"skill:{name}",
            name=name,
            kind="skill",
            provenance=prov,
            content_sha256=val_rec.content_sha256,
            bundle_sha256=bundle_sha,
            body_length=val_rec.body_length,
            classification=classification,
            risk_level=val_rec.risk,
            rubric=rubric,
            staging_path=str(tree_path),
            catalog_match=catalog_match,
            quarantine_reasons=q_reasons,
            active=False
        )

    def evaluate_prompt_candidate(
        self,
        file_path: Path,
        prov: CandidateProvenance,
        prompt_name: Optional[str] = None
    ) -> IntakeCandidateRecord:
        """Evaluate a staged prompt markdown file."""
        val_rec = catalog_ingest.validate_prompt_entry(file_path)
        name = prompt_name or file_path.stem
        text = file_path.read_text(encoding="utf-8", errors="replace") if file_path.is_file() else ""

        rubric = self.evaluate_quality_rubric(name, "prompt", text, val_rec, prov)
        classification, q_reasons = self.classify_candidate(name, "prompt", val_rec, rubric, prov)

        catalog_match = None
        if name in self.catalog_cache["prompts"]:
            catalog_match = self.catalog_cache["prompts"][name]

        return IntakeCandidateRecord(
            candidate_id=f"prompt:{name}",
            name=name,
            kind="prompt",
            provenance=prov,
            content_sha256=val_rec.content_sha256,
            bundle_sha256=val_rec.bundle_sha256,
            body_length=val_rec.body_length,
            classification=classification,
            risk_level=val_rec.risk,
            rubric=rubric,
            staging_path=str(file_path),
            catalog_match=catalog_match,
            quarantine_reasons=q_reasons,
            active=False
        )

    def process_evidence_file(
        self,
        evidence_path: Path
    ) -> Tuple[List[IntakeCandidateRecord], Dict[str, Any]]:
        """Parse GitHub MCP evidence JSON, stage candidates without synthesis, and evaluate them."""
        if not evidence_path.is_file():
            raise FileNotFoundError(f"Evidence file not found: {evidence_path}")

        with open(evidence_path, "r", encoding="utf-8") as f:
            evidence_data = json.load(f)

        events = evidence_data.get("evidence", [])
        searches_meta: List[Dict[str, Any]] = []

        commits_by_repo: Dict[str, List[Dict[str, Any]]] = {}
        licenses_by_repo: Dict[str, List[Dict[str, Any]]] = {}
        files_by_repo: List[Dict[str, Any]] = []
        repo_pinned_commits: Dict[str, str] = {}
        mixed_commits_repos: Set[str] = set()

        search_queries: Dict[str, Dict[str, Any]] = {}
        api_failures: List[str] = []

        for e in events:
            etype = e.get("type")
            if etype == "search":
                q = (e.get("query") or "").strip()
                if not q:
                    api_failures.append("Search event with empty/undeclared query scope")
                    continue

                res = e.get("result")
                if res is None or res == {} or not isinstance(res, dict):
                    api_failures.append(f"Search query '{q}' returned empty/invalid result object")
                    search_queries.setdefault(q, {
                        "query": q, "total_count": -1, "unique_items": {}, "pages_seen": set(),
                        "has_error": True, "incomplete": True, "duplicate_page_detected": False
                    })["has_error"] = True
                    continue

                if res.get("isError") or "error" in res:
                    err_msg = str(res.get("error") or "API error flag")
                    api_failures.append(f"Search query '{q}' API error: {err_msg}")
                    search_queries.setdefault(q, {
                        "query": q, "total_count": -1, "unique_items": {}, "pages_seen": set(),
                        "has_error": True, "incomplete": True, "duplicate_page_detected": False
                    })["has_error"] = True
                    continue

                content_text = ""
                for c in res.get("content", []):
                    if isinstance(c, dict) and c.get("type") == "text":
                        content_text = c.get("text", "")
                        break

                if not content_text or not content_text.strip():
                    api_failures.append(f"Search query '{q}' returned empty content")
                    search_queries.setdefault(q, {
                        "query": q, "total_count": -1, "unique_items": {}, "pages_seen": set(),
                        "has_error": True, "incomplete": True, "duplicate_page_detected": False
                    })["has_error"] = True
                    continue

                try:
                    s_data = json.loads(content_text)
                except Exception as ex:
                    api_failures.append(f"Search query '{q}' content JSON decode failure: {ex}")
                    search_queries.setdefault(q, {
                        "query": q, "total_count": -1, "unique_items": {}, "pages_seen": set(),
                        "has_error": True, "incomplete": True, "duplicate_page_detected": False
                    })["has_error"] = True
                    continue

                # Point 3: Error result {} lacking total_count/items must never become healthy zero
                if not isinstance(s_data, dict) or "total_count" not in s_data or "items" not in s_data:
                    err_detail = s_data.get("message") if isinstance(s_data, dict) else "Result missing total_count/items"
                    api_failures.append(f"Search query '{q}' missing total_count/items: {err_detail}")
                    search_queries.setdefault(q, {
                        "query": q, "total_count": -1, "unique_items": {}, "pages_seen": set(),
                        "has_error": True, "incomplete": True, "duplicate_page_detected": False
                    })["has_error"] = True
                    continue

                total_count = s_data.get("total_count", 0)
                items = s_data.get("items", [])
                incomplete = s_data.get("incomplete_results", False)

                if q not in search_queries:
                    search_queries[q] = {
                        "query": q,
                        "total_count": total_count,
                        "unique_items": {},
                        "pages_seen": set(),
                        "has_error": False,
                        "incomplete": incomplete,
                        "duplicate_page_detected": False
                    }

                page_num = e.get("page") or e.get("page_number") or (len(search_queries[q]["pages_seen"]) + 1)
                if page_num in search_queries[q]["pages_seen"]:
                    search_queries[q]["duplicate_page_detected"] = True
                search_queries[q]["pages_seen"].add(page_num)

                if incomplete:
                    search_queries[q]["incomplete"] = True

                # Point 2: Aggregate by stable repo id or fullname (no raw sum inflation)
                for item in items:
                    if isinstance(item, dict):
                        stable_id = str(item.get("id") or item.get("full_name") or item.get("html_url") or item.get("name"))
                        search_queries[q]["unique_items"][stable_id] = item

            elif etype == "commits":
                repo = e.get("repo")
                for c in e.get("result", {}).get("content", []):
                    if isinstance(c, dict) and c.get("type") == "text":
                        try:
                            commits = json.loads(c.get("text", "[]"))
                            if commits and isinstance(commits, list):
                                for item in commits:
                                    commits_by_repo.setdefault(repo, []).append({
                                        "sha": item.get("sha"),
                                        "author": item.get("commit", {}).get("author", {}).get("name", ""),
                                        "date": item.get("commit", {}).get("author", {}).get("date", "")
                                    })
                        except Exception:
                            pass

            elif etype == "license":
                repo = e.get("repo")
                lic_sha = e.get("sha")
                for item in e.get("result", {}).get("content", []):
                    if isinstance(item, dict) and item.get("type") == "resource":
                        txt = item.get("resource", {}).get("text", "")
                        spdx = "UNKNOWN"
                        if "MIT No Attribution" in txt or "MIT-0" in txt:
                            spdx = "MIT-0"
                        elif "Creative Commons" in txt or "CC-BY" in txt:
                            if "-NC" in txt or "NonCommercial" in txt:
                                spdx = "CC-BY-NC-4.0"
                            elif "-ND" in txt or "NoDerivatives" in txt:
                                spdx = "CC-BY-ND-4.0"
                            else:
                                spdx = "CC-BY-4.0"
                        elif "Apache License" in txt and "Version 2.0" in txt:
                            spdx = "Apache-2.0"
                        elif "BSD 3-Clause" in txt:
                            spdx = "BSD-3-Clause"
                        elif "BSD 2-Clause" in txt:
                            spdx = "BSD-2-Clause"
                        elif "ISC License" in txt:
                            spdx = "ISC"
                        elif "Permission is hereby granted, free of charge" in txt and "MIT" in txt:
                            spdx = "MIT"
                        licenses_by_repo.setdefault(repo, []).append({
                            "spdx": spdx,
                            "sha": lic_sha,
                            "path": e.get("path", "LICENSE")
                        })

            elif etype == "file":
                files_by_repo.append(e)

        # Discovery completeness verification:
        # Requires:
        # 1. At least one declared search query with non-empty scope.
        # 2. No API failures or error flags.
        # 3. For every query: no incomplete_results, no duplicate page anomaly, and unique items fetched >= total_count.
        discovery_complete = False
        if search_queries and not api_failures:
            all_queries_ok = True
            for q, s_stat in search_queries.items():
                fetched_unique = len(s_stat["unique_items"])
                tot = s_stat["total_count"]
                has_err = s_stat["has_error"]
                incomp = s_stat["incomplete"]
                dup_page = s_stat.get("duplicate_page_detected", False)

                searches_meta.append({
                    "query": q,
                    "total_count": tot,
                    "fetched_unique_items": fetched_unique,
                    "pages_count": len(s_stat["pages_seen"]),
                    "incomplete_results": incomp,
                    "has_error": has_err,
                    "duplicate_page_detected": dup_page
                })

                if has_err or incomp or dup_page or tot < 0 or fetched_unique < tot:
                    all_queries_ok = False

            if all_queries_ok and len(files_by_repo) > 0:
                discovery_complete = True
        elif search_queries:
            for q, s_stat in search_queries.items():
                searches_meta.append({
                    "query": q,
                    "total_count": s_stat["total_count"],
                    "fetched_unique_items": len(s_stat["unique_items"]),
                    "pages_count": len(s_stat["pages_seen"]),
                    "incomplete_results": s_stat["incomplete"],
                    "has_error": s_stat["has_error"],
                    "duplicate_page_detected": s_stat.get("duplicate_page_detected", False)
                })

        is_partial_scan = not discovery_complete

        self.staging_dir.mkdir(parents=True, exist_ok=True)
        candidates: List[IntakeCandidateRecord] = []

        for fe in files_by_repo:
            repo = fe.get("repo", "")
            fpath = fe.get("path", "")
            fsha = fe.get("sha", "")
            retrieved_at = fe.get("time", "")

            # Extract file content and Git blob header
            ftext = ""
            header_text = ""
            for item in fe.get("result", {}).get("content", []):
                if isinstance(item, dict) and item.get("type") == "text":
                    header_text = item.get("text", "")
                elif isinstance(item, dict) and item.get("type") == "resource":
                    ftext = item.get("resource", {}).get("text", "")

            raw_bytes = ftext.encode("utf-8")

            # Determine kind and name first
            p = Path(fpath)
            if p.name == "SKILL.md" or "skills/" in fpath:
                kind = "skill"
                if p.name == "SKILL.md":
                    name = p.parent.name
                    if name in {"skills", ".", ""}:
                        name = repo.split("/")[-1]
                else:
                    name = p.stem
            else:
                kind = "prompt"
                name = p.stem

            # Check Git blob SHA binding
            blob_match = re.search(r'successfully downloaded text file \(SHA:\s*([0-9a-fA-F]{40})\)', header_text)
            expected_blob = blob_match.group(1) if blob_match else None
            blob_status = verify_git_blob_sha(raw_bytes, expected_blob)  # True, False, or None

            # Check commit binding and mixed commits within repo
            if repo in repo_pinned_commits and repo_pinned_commits[repo] != fsha:
                mixed_commits_repos.add(repo)
            else:
                repo_pinned_commits[repo] = fsha

            # Find author and date from matching commit
            commit_author = repo.split("/")[0] if "/" in repo else "unknown"
            commit_date = retrieved_at
            for c in commits_by_repo.get(repo, []):
                if c.get("sha") == fsha:
                    commit_author = c.get("author") or commit_author
                    commit_date = c.get("date") or commit_date
                    break
                elif not commit_date and c.get("date"):
                    commit_date = c.get("date")

            # Point 6: License commit pinning & mixed terms preservation
            repo_lics = licenses_by_repo.get(repo, [])
            if not repo_lics:
                license_spdx = "UNKNOWN"
                license_status = "UNKNOWN"
                lic_commit = None
            else:
                same_commit_lics = [l for l in repo_lics if l.get("sha") == fsha]
                if same_commit_lics:
                    distinct_spdx = set(l["spdx"] for l in same_commit_lics)
                    if len(distinct_spdx) > 1:
                        license_spdx = "UNCERTAIN"
                        license_status = "UNCERTAIN_MIXED"
                        lic_commit = fsha
                    else:
                        license_spdx = same_commit_lics[0]["spdx"]
                        license_status = "VERIFIED"
                        lic_commit = fsha
                else:
                    license_spdx = "UNCERTAIN"
                    license_status = "STALE_COMMIT"
                    lic_commit = repo_lics[0].get("sha")

            prov = CandidateProvenance(
                repo_url=f"https://github.com/{repo}",
                commit_sha=fsha,
                relative_path=fpath,
                author=commit_author,
                commit_date=commit_date,
                license_spdx=license_spdx,
                blob_sha=expected_blob,
                blob_sha_verified=blob_status,
                license_commit=lic_commit,
                license_status=license_status
            )

            # Point 5: Validate 40-hex commit hash before any staging write
            if not is_valid_40hex_commit(fsha):
                cand = IntakeCandidateRecord(
                    candidate_id=f"{kind}:{name}",
                    name=name,
                    kind=kind,
                    provenance=prov,
                    content_sha256=hashlib.sha256(raw_bytes).hexdigest(),
                    bundle_sha256=None,
                    body_length=len(raw_bytes),
                    classification="REJECTED",
                    risk_level="high",
                    rubric=QualityRubricResult(0, 0, 0, 0, 0, 0, 0, 0, 0, False, False, False, ["invalid commit SHA: must be 40-hex characters"]),
                    staging_path="",
                    catalog_match=None,
                    quarantine_reasons=[f"invalid 40-hex commit SHA '{fsha}': staging rejected before write"],
                    active=False
                )
                candidates.append(cand)
                continue

            # Content-addressed, collision-free, immutable staging layout using full 40-hex commit
            parts = repo.split("/")
            safe_owner = re.sub(r'[^A-Za-z0-9_-]', '_', parts[0] if len(parts) > 0 else "unknown")
            safe_repo = re.sub(r'[^A-Za-z0-9_-]', '_', parts[1] if len(parts) > 1 else repo)
            full_commit = fsha.strip().lower()

            repo_stage_dir = assert_safe_path(self.staging_dir, f"{safe_owner}/{safe_repo}/{full_commit}")
            repo_stage_dir.mkdir(parents=True, exist_ok=True)

            if kind == "skill":
                skill_rel_dir = str(p.parent)
                candidate_dir = assert_safe_path(repo_stage_dir, skill_rel_dir)
                candidate_dir.mkdir(parents=True, exist_ok=True)

                staged_file = assert_safe_path(candidate_dir, "SKILL.md")
                try:
                    write_staged_file_immutable(staged_file, raw_bytes)
                except ValueError as exc:
                    cand = IntakeCandidateRecord(
                        candidate_id=f"skill:{name}",
                        name=name,
                        kind="skill",
                        provenance=prov,
                        content_sha256=hashlib.sha256(raw_bytes).hexdigest(),
                        bundle_sha256=None,
                        body_length=len(raw_bytes),
                        classification="REJECTED",
                        risk_level="high",
                        rubric=QualityRubricResult(0, 0, 0, 0, 0, 0, 0, 0, 0, False, False, False, [str(exc)]),
                        staging_path=str(candidate_dir),
                        catalog_match=None,
                        quarantine_reasons=[str(exc)],
                        active=False
                    )
                    candidates.append(cand)
                    continue

                cand = self.evaluate_skill_candidate(candidate_dir, prov)

                if blob_status is False:
                    cand.classification = "REJECTED"
                    cand.quarantine_reasons.append(f"Git blob SHA mismatch: expected {expected_blob}")
                if repo in mixed_commits_repos:
                    cand.classification = "NEW_QUARANTINED"
                    cand.quarantine_reasons.append(f"mixed commits detected within repo {repo}")

                candidates.append(cand)
            else:
                prompt_rel_path = fpath
                staged_prompt = assert_safe_path(repo_stage_dir, prompt_rel_path)
                staged_prompt.parent.mkdir(parents=True, exist_ok=True)
                try:
                    write_staged_file_immutable(staged_prompt, raw_bytes)
                except ValueError as exc:
                    cand = IntakeCandidateRecord(
                        candidate_id=f"prompt:{name}",
                        name=name,
                        kind="prompt",
                        provenance=prov,
                        content_sha256=hashlib.sha256(raw_bytes).hexdigest(),
                        bundle_sha256=None,
                        body_length=len(raw_bytes),
                        classification="REJECTED",
                        risk_level="high",
                        rubric=QualityRubricResult(0, 0, 0, 0, 0, 0, 0, 0, 0, False, False, False, [str(exc)]),
                        staging_path=str(staged_prompt),
                        catalog_match=None,
                        quarantine_reasons=[str(exc)],
                        active=False
                    )
                    candidates.append(cand)
                    continue

                cand = self.evaluate_prompt_candidate(staged_prompt, prov, prompt_name=name)

                if blob_status is False:
                    cand.classification = "REJECTED"
                    cand.quarantine_reasons.append(f"Git blob SHA mismatch: expected {expected_blob}")
                if repo in mixed_commits_repos:
                    cand.classification = "NEW_QUARANTINED"
                    cand.quarantine_reasons.append(f"mixed commits detected within repo {repo}")

                candidates.append(cand)

        scan_meta = {
            "is_partial_scan": is_partial_scan,
            "discovery_complete": discovery_complete,
            "searches": searches_meta,
            "api_failures": api_failures,
            "deep_inspected_repos": len(set(f.get("repo") for f in files_by_repo)),
        }
        return candidates, scan_meta

    def build_intake_manifest(
        self,
        candidates: List[IntakeCandidateRecord],
        target_description: str,
        is_partial_scan: bool = True,
        discovery_complete: bool = False,
        api_failures: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """Assemble the complete intake manifest and integration seam."""
        counts = {
            "total_scanned": len(candidates),
            "new_ready": sum(1 for c in candidates if c.classification == "NEW_READY"),
            "review_required": sum(1 for c in candidates if c.classification == "REVIEW_REQUIRED"),
            "new_quarantined": sum(1 for c in candidates if c.classification == "NEW_QUARANTINED"),
            "updated": sum(1 for c in candidates if c.classification == "UPDATED"),
            "exact_duplicate": sum(1 for c in candidates if c.classification == "EXACT_DUPLICATE"),
            "rejected": sum(1 for c in candidates if c.classification == "REJECTED"),
        }

        last_advanced = bool(discovery_complete and not is_partial_scan and not api_failures)
        status = "COMPLETE_BOUNDED_SCAN" if last_advanced else "PARTIAL_SCAN_STAGING_ONLY"

        manifest = {
            "schema_version": 1,
            "task_id": str(uuid.uuid4()),
            "scan_timestamp_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "status": status,
            "is_partial_scan": is_partial_scan,
            "discovery_complete": discovery_complete,
            "last_successful_full_scan_advanced": last_advanced,
            "watermark_advancement_note": (
                "Complete bounded scan successfully verified with full discovery scope and no API errors; watermark advanced."
                if last_advanced else
                "Scan is partial or incomplete (truncated search pagination, unverified discovery scope, or API failures). Watermark advancement deferred."
            ),
            "completeness": {
                "discovery_complete": discovery_complete,
                "is_partial_scan": is_partial_scan,
                "api_failures": api_failures or [],
                "resource_completeness": {
                    "total_resources": len(candidates),
                    "complete_bundles": sum(1 for c in candidates if c.bundle_sha256 is not None),
                    "incomplete_bundles": sum(1 for c in candidates if c.kind == "skill" and c.bundle_sha256 is None)
                }
            },
            "target_description": target_description,
            "catalog_baseline": {
                "active_skills": len([s for s in self.catalog_cache["skills"].values() if s["active"]]),
                "quarantined_skills": len([s for s in self.catalog_cache["skills"].values() if not s["active"]]),
                "active_prompts": len([p for p in self.catalog_cache["prompts"].values() if p["active"]]),
                "catalog_pin_hash": self.catalog_pin_hash,
            },
            "counts": counts,
            "integration_seam": {
                "contract": "Read-only intake staging. Live catalog, pins, and embeddings remain untouched until reviewed acceptance.",
                "reviewers": {
                    "security_reviewer": "Audits NEW_READY and REVIEW_REQUIRED candidates; verifies REJECTED items are correctly dropped.",
                    "integration_reviewer": "Reviews accepted candidates, runs tests, and updates catalog pins when authorized."
                },
                "activation_rules": [
                    "Only candidates marked NEW_READY with complete bundle, valid license and passing rubric may be promoted.",
                    "REVIEW_REQUIRED candidates (such as those containing scripts or specific platform constraints) require manual code review before promotion.",
                    "UPDATED resources require diff inspection before overwriting live active versions.",
                    "NEW_QUARANTINED candidates (including incomplete bundles) remain inactive until complete files and clear license are verified.",
                    "Catalog pin and embedding updates require a separately configured, authorized integration workflow."
                ]
            },
            "candidates": [asdict(c) for c in candidates]
        }
        return manifest


def format_scan_report(manifest: Dict[str, Any]) -> str:
    """Format markdown report of scan findings with primary GitHub links and timestamps."""
    lines = []
    lines.append("# GitHub Resource Discovery & Intake Scan Report")
    lines.append(f"**Scan Timestamp**: `{manifest['scan_timestamp_utc']}`")
    lines.append(f"**Target**: {manifest['target_description']}")
    lines.append(f"**Status**: `{manifest['status']}` (Watermark advanced: `{manifest['last_successful_full_scan_advanced']}`)")
    lines.append(f"**Watermark Note**: {manifest['watermark_advancement_note']}")
    lines.append(f"**Discovery Complete**: `{manifest.get('discovery_complete', False)}` | **Partial Scan**: `{manifest['is_partial_scan']}`")
    pin = manifest['catalog_baseline']['catalog_pin_hash'] or 'unavailable'
    lines.append(f"**Catalog Baseline**: {manifest['catalog_baseline']['active_skills']} active skills, {manifest['catalog_baseline']['active_prompts']} active prompts (Pin: `{pin}`)")
    lines.append("")
    lines.append("## Summary Counts")
    c = manifest["counts"]
    lines.append(f"- **Total Scanned**: {c['total_scanned']}")
    lines.append(f"- **NEW Ready for Review**: {c['new_ready']}")
    lines.append(f"- **REVIEW_REQUIRED (Scripts / Attribution / Constraints)**: {c['review_required']}")
    lines.append(f"- **NEW Quarantined (Missing frontmatter / Incomplete bundle / License)**: {c['new_quarantined']}")
    lines.append(f"- **UPDATED (Upstream Revision)**: {c['updated']}")
    lines.append(f"- **EXACT DUPLICATE (Skipped)**: {c['exact_duplicate']}")
    lines.append(f"- **REJECTED (Security / Defect)**: {c['rejected']}")
    lines.append("")
    lines.append("## Candidate Details")
    lines.append("| Name | Kind | Classification | License | Score | Primary GitHub Source | Upstream Commit | Reasons / Notes |")
    lines.append("|---|---|---|---|---|---|---|---|")

    for cand in manifest["candidates"]:
        name = cand["name"]
        kind = cand["kind"]
        cls = cand["classification"]
        prov = cand["provenance"]
        repo = prov["repo_url"]
        commit = prov["commit_sha"][:7] if prov["commit_sha"] else "unpinned"
        c_date = prov["commit_date"] or "unknown"
        lic = prov["license_spdx"] or "UNKNOWN"
        score = cand["rubric"]["overall_score"]
        reasons = "; ".join(cand["quarantine_reasons"] or [r for r in cand["rubric"]["reasons"] if "duplicate" in r or "safety" in r or "scripts" in r] or ["All gates passed"])

        link = f"[{repo.replace('https://github.com/', '')}]({repo})"
        lines.append(f"| `{name}` | {kind} | **{cls}** | {lic} | {score}/10 | {link} | `{commit}` ({c_date[:10]}) | {reasons} |")

    lines.append("")
    lines.append("## Integration Seam & Next Steps")
    lines.append("- **Safety Isolation**: All staged candidates remain `active: false` without execution.")
    lines.append("- **Security Review**: Security Reviewer audits the manifest candidates.")
    lines.append("- **Integration**: Authorized maintainers review and promote accepted candidates through their own catalog and embedding workflow.")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="GitHub Skill & Prompt Intake Scanner")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH, help="Path to live skill-index.db (read-only)")
    parser.add_argument("--evidence-input", type=Path, default=None, help="Path to GitHub MCP evidence JSON")
    parser.add_argument("--staging-dir", type=Path, default=DEFAULT_STAGING_DIR, help="Path to candidate staging directory")
    parser.add_argument("--manifest-out", type=Path, default=None, help="Output path for INTAKE_MANIFEST.json")
    parser.add_argument("--report-out", type=Path, default=None, help="Output path for markdown report")
    parser.add_argument("--watermark-in", type=Path, default=None, help="Input watermark JSON")
    parser.add_argument("--watermark-out", type=Path, default=None, help="Output watermark JSON")
    parser.add_argument("--weekly-routine", action="store_true", help="Run in automated weekly routine mode")
    # Note: --force-full-scan bypass removed permanently per review. Completion is strictly evidence-backed.
    args = parser.parse_args()

    scanner = GitHubIntakeScanner(db_path=args.db_path, staging_dir=args.staging_dir)
    print(f"Loaded intake scanner against catalog: {scanner.db_path}")

    candidates: List[IntakeCandidateRecord] = []
    is_partial_scan = True
    discovery_complete = False
    api_failures: List[str] = []

    if args.evidence_input and args.evidence_input.is_file():
        print(f"Processing evidence from: {args.evidence_input}")
        candidates, scan_meta = scanner.process_evidence_file(args.evidence_input)
        is_partial_scan = scan_meta.get("is_partial_scan", True)
        discovery_complete = scan_meta.get("discovery_complete", False)
        api_failures = scan_meta.get("api_failures", [])
        print(f"Processed {len(candidates)} candidates from evidence (Partial scan: {is_partial_scan}, Discovery complete: {discovery_complete})")
    elif args.staging_dir and args.staging_dir.is_dir():
        print(f"Scanning existing staging directory: {args.staging_dir}")
        for child in args.staging_dir.iterdir():
            if child.is_dir() and (child / "SKILL.md").is_file():
                prov = CandidateProvenance(
                    repo_url="https://github.com/staged/repo",
                    commit_sha="0" * 40,
                    relative_path=f"skills/{child.name}/SKILL.md",
                    author="unknown",
                    commit_date=dt.datetime.now(dt.timezone.utc).isoformat(),
                    license_spdx="UNKNOWN"  # Never default to MIT
                )
                candidates.append(scanner.evaluate_skill_candidate(child, prov))
            elif child.is_file() and child.suffix == ".md":
                prov = CandidateProvenance(
                    repo_url="https://github.com/staged/repo",
                    commit_sha="0" * 40,
                    relative_path=f"prompts/{child.name}",
                    author="unknown",
                    commit_date=dt.datetime.now(dt.timezone.utc).isoformat(),
                    license_spdx="UNKNOWN"  # Never default to MIT
                )
                candidates.append(scanner.evaluate_prompt_candidate(child, prov))

    manifest = scanner.build_intake_manifest(
        candidates,
        target_description=f"GitHub Routine Intake ({'Evidence: ' + str(args.evidence_input) if args.evidence_input else 'Staged'})",
        is_partial_scan=is_partial_scan,
        discovery_complete=discovery_complete,
        api_failures=api_failures
    )

    report_text = format_scan_report(manifest)

    if args.manifest_out:
        args.manifest_out.parent.mkdir(parents=True, exist_ok=True)
        with open(args.manifest_out, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)
        print(f"Wrote intake manifest to: {args.manifest_out}")

    if args.report_out:
        args.report_out.parent.mkdir(parents=True, exist_ok=True)
        with open(args.report_out, "w", encoding="utf-8") as f:
            f.write(report_text)
        print(f"Wrote scan report to: {args.report_out}")

    if args.watermark_out:
        args.watermark_out.parent.mkdir(parents=True, exist_ok=True)
        wm = {
            "last_scan_utc": manifest["scan_timestamp_utc"],
            "is_partial_scan": is_partial_scan,
            "discovery_complete": discovery_complete,
            "advanced": manifest["last_successful_full_scan_advanced"],
            "total_scanned": len(candidates)
        }
        with open(args.watermark_out, "w", encoding="utf-8") as f:
            json.dump(wm, f, indent=2)
        print(f"Wrote watermark to: {args.watermark_out}")

    print("Intake scan completed successfully.")
    print(f"Summary: {manifest['counts']}")


if __name__ == "__main__":
    main()
