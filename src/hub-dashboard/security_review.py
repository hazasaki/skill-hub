"""Credential masking and conservative syntax triage; never return matched values."""
import ast
import re
from credential_mask import CREDENTIAL_RE,structured_credential_spans

_VALUE_RE = re.compile(r'''(?is)^(?:bearer\s+|["']?(?:api[_-]?key|password|secret|token)["']?\s*[:=]\s*)(.*)$''')
_ENV_RE = re.compile(r'(?:\$[A-Za-z_]\w*|\$\{[A-Za-z_]\w*\}|%[A-Za-z_]\w*%)')
_PLACEHOLDER_RE = re.compile(r'(?i)(?:<(?:your[_-])?(?:api[_-]?key|access[_-]?token|token|password|secret|credential|key)(?:[_-](?:here|value))?>|\{\{[A-Za-z_][A-Za-z0-9_.]*\}\}|(?:your|example|dummy|placeholder)[_-](?:[a-z0-9_-]+)|(?:x{4,}|\*{4,}|\.{3}))')

def _is_value_reference(value):
    if isinstance(value,ast.Name):
        return True
    if isinstance(value,ast.Attribute):
        return _is_value_reference(value.value)
    if isinstance(value,ast.Subscript) and _is_value_reference(value.value):
        key=value.slice
    elif (isinstance(value,ast.Call) and isinstance(value.func,ast.Attribute)
          and _is_value_reference(value.func.value) and value.func.attr in ('get','getenv','pop') and len(value.args)==1 and not value.keywords):
        key=value.args[0]
    else:
        return False
    return isinstance(key,ast.Constant) and isinstance(key.value,str) and bool(re.fullmatch(r'(?:[A-Z][A-Z0-9_]*|api_key|apikey|password|secret|token)',key.value))


def _reference_spans(text):
    """Parse Python documentation fences; value references are not embedded secrets."""
    spans = []
    for fence in re.finditer(r'(?m)^```(?:python|python3|py)[ \t]*\n(.*?)^```[ \t]*$', text, re.S):
        code = fence.group(1)
        if len(code) > 128000:
            continue  # Oversized fences keep review status; detection and masking still cover them.
        try:
            tree = ast.parse(code)
        except (SyntaxError, ValueError, RecursionError):
            continue
        lines = code.splitlines(keepends=True)
        offsets = [0]
        for line in lines:
            offsets.append(offsets[-1] + len(line))

        def position(node, end=False):
            line = node.end_lineno if end else node.lineno
            column = node.end_col_offset if end else node.col_offset
            # Python AST columns count UTF-8 bytes, not Unicode characters.
            return fence.start(1) + offsets[line - 1] + len(lines[line - 1].encode()[:column].decode())

        for node in ast.walk(tree):
            value = getattr(node, 'value', None)
            safe = _is_value_reference(value)
            targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
            key = lambda target: target.id if isinstance(target, ast.Name) else target.attr if isinstance(target, ast.Attribute) else ''
            credential_target = all(isinstance(target,(ast.Name,ast.Attribute)) for target in targets) and any(re.fullmatch(r'(?i)api[_-]?key|password|secret|token', key(target)) for target in targets)
            credential_keyword = isinstance(node, ast.keyword) and node.arg in ('api_key', 'apikey', 'password', 'secret', 'token')
            if (credential_target or credential_keyword) and safe:
                spans.append((position(node), position(node, True)))
            if isinstance(node, ast.AnnAssign) and credential_target and node.value is None:
                spans.append((position(node), position(node, True)))
            if isinstance(node,ast.Dict):
                for field,reference in zip(node.keys,node.values):
                    if (isinstance(field,ast.Constant) and field.value in ('api_key','apikey','password','secret','token')
                            and _is_value_reference(reference)):
                        spans.append((position(field),position(reference,True)))
    return spans


def credential_review(text):
    """Only explicit placeholders/env substitutions are examples; other literals stay open."""
    reasons = []
    unresolved = False
    matches = list(CREDENTIAL_RE.finditer(text))
    references = _reference_spans(text) if matches else []
    for match in matches:
        value = _VALUE_RE.match(match.group())
        value = value.group(1).strip() if value else match.group()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if _ENV_RE.fullmatch(value):
            reasons.append('environment_reference')
        elif _PLACEHOLDER_RE.fullmatch(value):
            reasons.append('explicit_placeholder')
        elif any(start <= match.start() < end and
                 (match.end() <= end or re.fullmatch(r'[)\]}:.]*', text[end:match.end()]))
                 for start, end in references):
            reasons.append('python_value_reference')
        else:
            unresolved = True
            reasons.append('literal_or_expression_requires_review')
            raw_value = _VALUE_RE.match(match.group())
            raw_value = raw_value.group(1).strip() if raw_value else match.group()
            if re.search(r'(?i)(?<![a-z0-9_-])(?:sk-[a-z0-9_-]{12,}|gh[pousr]_[a-z0-9_]+)',raw_value):
                reasons.append('provider_format_literal')
            elif raw_value[:1] in ('"',"'"):
                reasons.append('quoted_literal_requires_review')
            elif re.match(r'(?i)(?:os\.|process\.|self\.|this\.|config\.|args\.|\$|<)',raw_value):
                reasons.append('dynamic_expression_unverified')
            else:
                reasons.append('unquoted_literal_or_type_unverified')
    if '[redacted]' in text.lower():
        unresolved = True
        reasons.append('original_redacted_marker_requires_review')
    structured=structured_credential_spans(text)
    if structured:
        unresolved=True
        reasons.extend(reason for _,_,reason in structured)
    return {'matches': len(matches)+len(structured), 'example_only': bool(matches) and not unresolved,
            'reasons': sorted(set(reasons))}


def execution_review(text, pattern):
    """Resolve only fenced, explicitly labelled attack examples with defensive context."""
    spans = []
    for fence in re.finditer(r'(?m)^```[^\n]*\n(.*?)^```[ \t]*$', text, re.S):
        before = text[max(0,fence.start()-1200):fence.start()]
        after = text[fence.end():fence.end()+1200]
        label = '\n'.join(before.splitlines()[-3:])
        context = before + '\n' + after
        if (re.search(r'(?i)example|anti.pattern|vulnerable|unsafe|attack',label)
                and re.search(r'(?i)prompt injection|security vulnerability|malicious|attack|vulnerable',context)
                and re.search(r"(?i)never|must not|do not|don't|avoid|saniti[sz]|mitigat|prevent|secure",context)):
            spans.append(fence.span(1))
    matches = list(pattern.finditer(text))
    confirmed = sum(any(start <= match.start() and match.end() <= end for start,end in spans) for match in matches)
    return {'matches':len(matches),'educational_matches':confirmed,
            'example_only':bool(matches) and confirmed == len(matches),
            'reasons':(['educational_attack_example'] if confirmed else []) +
                      (['unverified_execution_or_override'] if confirmed < len(matches) else [])}
