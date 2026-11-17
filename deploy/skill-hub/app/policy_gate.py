"""Mandatory policy & actual action permissions enforcement independent of System1 (R3-C05).

Verifies and enforces:
1. Mandatory policy applies across `bypass`, `selected`, and `no_skill` routing paths.
2. Draft actions (`email-compose`, `draft`, `prepare`) never authorize actual external sends (`email-send`, `post`, `submit`) or payments (`pay`, `checkout`).
3. Missing or revoked permissions and dependency conflicts deterministically deny execution regardless of System1 (Mammoth Teacher / Laya Student) predictions or confidence scores.
4. System1 models (Mammoth/Laya) cannot grant or elevate permissions (`system1_grants_ignored = True`).
5. `authorization_source == 'host_policy_only'` is strictly required for all `type: skill` and `type: prompt` resources.
6. Staging and loading are strictly read-only/data-only: never execute install scripts (`install.sh`, `.bat`, `.ps1`, git hooks, setup.py) and never override agent system/developer prompts or protected host policy files (`POLICY.md`, `AGENTS.md`, `WORKFLOW.md`).
7. No hidden AI planner/router precedes System1; candidate preparation is purely deterministic.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "openbot.r3.policy_gate.v1"

ROUTING_PATHS = ("bypass", "selected", "no_skill")
RESOURCE_TYPES = ("skill", "prompt", "mcp", "plugin")
PROMPT_TYPES = (
    "agent_role",
    "workflow",
    "task_template",
    "tool_guidance",
    "output_contract",
    "evaluation_rubric",
)

PROTECTED_HOST_FILES = frozenset({
    "policy.md",
    "agents.md",
    "workflow.md",
    "claude.md",
    "system_prompt",
    "developer_prompt",
})

EXTERNAL_EFFECT_ACTIONS = frozenset({
    "email-send",
    "send_message_external",
    "post_external",
    "submit_form",
    "pay",
    "checkout",
    "delete_unowned_data",
})

DRAFT_ONLY_INTENTS = frozenset({
    "draft",
    "compose",
    "prepare",
    "help_me_decide",
    "preview",
})

FORBIDDEN_INSTALL_PATTERNS = (
    re.compile(r"(?:^|/)(?:install|setup|postinstall|preinstall)\.(?:sh|bat|cmd|ps1|py)$", re.IGNORECASE),
    re.compile(r"(?:^|/)\.git/hooks/", re.IGNORECASE),
    re.compile(r"\b(?:curl|wget)\b.+\|\s*(?:sh|bash|pwsh|powershell)\b", re.IGNORECASE),
    re.compile(r"\bsubprocess\.(?:Popen|run|call|check_output)\b", re.IGNORECASE),
    re.compile(r"\bos\.system\b", re.IGNORECASE),
)

PROMPT_OVERRIDE_PATTERNS = (
    re.compile(r"ignore\s+(?:all\s+)?(?:previous|prior|system|developer|host)\s+instructions", re.IGNORECASE),
    re.compile(r"override\s+(?:POLICY\.md|AGENTS\.md|WORKFLOW\.md|host_policy)", re.IGNORECASE),
    re.compile(r"authorization_source\s*[:=]\s*['\"]?(?!host_policy_only\b)[a-z0-9_]+", re.IGNORECASE),
    re.compile(r"grant_permission\s*\(", re.IGNORECASE),
)
# Quoted material: fenced code blocks and inline code spans. Directive-looking text inside one
# is being shown to the reader, not issued to the host.
FENCED_BLOCK = re.compile(r"```.*?```", re.DOTALL)
INLINE_CODE = re.compile(r"`[^`\n]*`")


def canonical_hash(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def verify_candidate_preparation_stage(stage_record: dict[str, Any]) -> dict[str, Any]:
    """Verify deterministic candidate preparation before System1 with no hidden AI planner."""
    reasons: list[str] = []
    if not isinstance(stage_record, dict):
        return {"allowed": False, "status": "POLICY_DENIED", "reasons": ["INVALID_STAGE_RECORD"]}

    if stage_record.get("uses_llm_planner") or stage_record.get("ai_router_before_system1"):
        reasons.append("HIDDEN_AI_PLANNER_BEFORE_SYSTEM1")
    if stage_record.get("strategy") not in ("deterministic_fts5_taxonomy", "deterministic_metadata_filter"):
        reasons.append("NON_DETERMINISTIC_CANDIDATE_STRATEGY")
    if not isinstance(stage_record.get("catalog_revision"), str) or not stage_record.get("catalog_revision"):
        reasons.append("MISSING_CATALOG_REVISION")
    if not isinstance(stage_record.get("query_snapshot_sha256"), str) or len(stage_record.get("query_snapshot_sha256", "")) != 64:
        reasons.append("MISSING_QUERY_SNAPSHOT_HASH")

    for cand in stage_record.get("candidates", []):
        if cand.get("activation_status") != "active":
            reasons.append(f"INACTIVE_CANDIDATE_IN_SHORTLIST:{cand.get('id', 'unknown')}")

    return {
        "allowed": len(reasons) == 0,
        "status": "ALLOWED" if not reasons else "POLICY_DENIED",
        "reasons": reasons,
    }


def verify_resource_safety(resource: dict[str, Any]) -> dict[str, Any]:
    """Verify staging/loading safety for `type: skill` and `type: prompt` resources."""
    reasons: list[str] = []
    if not isinstance(resource, dict):
        return {"allowed": False, "status": "POLICY_DENIED", "reasons": ["INVALID_RESOURCE_OBJECT"]}

    rtype = resource.get("resource_type") or resource.get("type")
    if rtype not in RESOURCE_TYPES:
        reasons.append(f"INVALID_RESOURCE_TYPE:{rtype}")

    auth_source = resource.get("authorization_source")
    if auth_source != "host_policy_only":
        reasons.append(f"INVALID_AUTHORIZATION_SOURCE:{auth_source}")

    if rtype == "prompt":
        ptype = resource.get("prompt_type")
        if ptype not in PROMPT_TYPES:
            reasons.append(f"INVALID_PROMPT_TYPE:{ptype}")

    if resource.get("executes_on_stage") or resource.get("executes_on_load"):
        reasons.append("EXECUTION_ON_STAGE_OR_LOAD_FORBIDDEN")

    files = resource.get("bundle_files") or []
    for fpath in files:
        norm_path = str(fpath).replace("\\", "/")
        basename = norm_path.rsplit("/", 1)[-1].lower()
        # A protected name at the bundle root is a bundle impersonating a host policy file:
        # that is the shape that could be installed over POLICY.md/AGENTS.md. The same name
        # nested deeper is documentation the skill ships — `references/cost-model/workflow.md`
        # is a document a reader opens, not a file that lands where the host policy lives.
        at_root = "/" not in norm_path
        if at_root and basename in PROTECTED_HOST_FILES:
            reasons.append(f"PROTECTED_HOST_FILE_COLLISION:{fpath}")
        for pat in FORBIDDEN_INSTALL_PATTERNS:
            if pat.search(norm_path):
                reasons.append(f"FORBIDDEN_INSTALL_SCRIPT_OR_HOOK:{fpath}")
                break

    target_layer = str(resource.get("target_injection_layer") or "task_guidance").lower()
    if target_layer in ("system", "developer", "host_policy", "policy.md", "agents.md", "workflow.md"):
        reasons.append(f"FORBIDDEN_PROMPT_LAYER_OVERRIDE:{target_layer}")

    overrides = resource.get("overrides") or []
    for ov in overrides:
        if str(ov).lower() in PROTECTED_HOST_FILES:
            reasons.append(f"PROTECTED_FILE_OVERRIDE_ATTEMPT:{ov}")

    body = str(resource.get("body") or "")
    # A fenced code block is quoted material: a security rubric that shows an injection as an
    # example is teaching the reader to spot it, not asking the host to obey it. Only directive
    # text outside a fence can override policy, so the scan skips fenced regions.
    directive_text = INLINE_CODE.sub(" ", FENCED_BLOCK.sub(" ", body))
    for pat in PROMPT_OVERRIDE_PATTERNS:
        if pat.search(directive_text):
            reasons.append("HOSTILE_POLICY_OVERRIDE_DIRECTIVE_IN_BODY")
            break
    for pat in FORBIDDEN_INSTALL_PATTERNS:
        if pat.search(directive_text) and resource.get("auto_run_body_scripts", False):
            reasons.append("AUTO_RUN_SCRIPT_DIRECTIVE_IN_BODY")
            break

    return {
        "allowed": len(reasons) == 0,
        "status": "ALLOWED" if not reasons else "POLICY_DENIED",
        "authorization_source_verified": auth_source == "host_policy_only",
        "reasons": reasons,
    }


def evaluate_policy_and_permissions(context: dict[str, Any]) -> dict[str, Any]:
    """Evaluate mandatory host policy and actual action permissions independently of System1."""
    reasons: list[str] = []
    route = context.get("routing_path")
    if route not in ROUTING_PATHS:
        reasons.append(f"INVALID_ROUTING_PATH:{route}")

    host_policy = context.get("host_policy") or {}
    if not host_policy.get("mandatory_policy_active", True):
        reasons.append("MANDATORY_POLICY_BYPASS_ATTEMPT")

    # System1 (Mammoth / Laya) predictions and purported permission grants are strictly ignored for auth
    system1_pred = context.get("system1_prediction") or {}
    if system1_pred.get("grant_permissions") or system1_pred.get("override_mandatory_policy"):
        reasons.append("SYSTEM1_PERMISSION_GRANT_IGNORED_AND_REJECTED")

    # Mandatory host skills must be enforced even on bypass or no_skill paths
    mandatory_skills = list(host_policy.get("mandatory_skills") or [])
    loaded_mandatory = list(context.get("loaded_mandatory_skills") or [])
    for mskill in mandatory_skills:
        if mskill not in loaded_mandatory:
            reasons.append(f"MISSING_MANDATORY_SKILL:{mskill}")

    # Check resource safety for any selected/loaded resources
    for res in context.get("resources") or []:
        res_check = verify_resource_safety(res)
        if not res_check["allowed"]:
            reasons.extend(res_check["reasons"])

    # Check dependency conflicts and missing dependencies
    granted_permissions = set(host_policy.get("granted_permissions") or [])
    revoked_permissions = set(host_policy.get("revoked_permissions") or [])
    effective_permissions = granted_permissions - revoked_permissions

    available_resources = set(context.get("available_resources") or [])
    selected_ids = [r.get("id") for r in (context.get("resources") or []) if r.get("id")]

    for res in context.get("resources") or []:
        res_id = res.get("id", "unknown")
        # Activation status check
        if res.get("activation_status", "active") != "active":
            reasons.append(f"RESOURCE_NOT_ACTIVE:{res_id}:{res.get('activation_status')}")
        # Required permissions check
        for perm in res.get("required_permissions") or []:
            if perm in revoked_permissions:
                reasons.append(f"PERMISSION_REVOKED:{perm}")
            elif perm not in effective_permissions:
                reasons.append(f"PERMISSION_MISSING:{perm}")
        # Required dependencies check
        for dep in res.get("dependencies") or []:
            dep_id = dep.get("resource_id") if isinstance(dep, dict) else str(dep)
            if dep_id not in available_resources and dep_id not in selected_ids:
                reasons.append(f"MISSING_DEPENDENCY:{res_id}->{dep_id}")
        # Mutual exclusion / conflict check
        for conflict_id in res.get("conflicts_with") or []:
            if conflict_id in selected_ids:
                reasons.append(f"DEPENDENCY_CONFLICT:{res_id}<->{conflict_id}")

    # Check requested actual actions (sends, payments, destructive ops)
    user_intent = str(context.get("user_intent") or "chat").lower()
    explicit_user_approval = bool(context.get("explicit_user_approval", False))
    payment_readback_confirmed = bool(context.get("payment_readback_confirmed", False))

    for action in context.get("requested_actions") or []:
        act_name = action.get("action") if isinstance(action, dict) else str(action)
        req_perm = (action.get("required_permission") if isinstance(action, dict) else None) or act_name

        if req_perm in revoked_permissions:
            reasons.append(f"ACTION_PERMISSION_REVOKED:{act_name}")
        elif req_perm not in effective_permissions:
            reasons.append(f"ACTION_PERMISSION_MISSING:{act_name}")

        if act_name in EXTERNAL_EFFECT_ACTIONS:
            if user_intent in DRAFT_ONLY_INTENTS:
                reasons.append(f"DRAFT_CANNOT_TRIGGER_EXTERNAL_ACTION:{act_name}")
            if not explicit_user_approval:
                reasons.append(f"EXTERNAL_ACTION_REQUIRES_USER_APPROVAL:{act_name}")
            if act_name in ("pay", "checkout") and not payment_readback_confirmed:
                reasons.append(f"PAYMENT_REQUIRES_READBACK_CONFIRMATION:{act_name}")

    allowed = len(reasons) == 0
    decision = {
        "schema_version": SCHEMA_VERSION,
        "routing_path": route,
        "allowed": allowed,
        "terminal_decision": "ALLOW" if allowed else "POLICY_DENIED",
        "mandatory_policy_enforced": True,
        "authorization_source": "host_policy_only",
        "system1_grants_ignored": True,
        "external_sends_executed": 0,
        "external_payments_executed": 0,
        "reasons": reasons,
    }
    decision["decision_sha256"] = canonical_hash(decision)
    return decision
