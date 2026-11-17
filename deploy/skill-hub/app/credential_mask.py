"""Presentation/admission masking; never changes source files or loading permissions."""
import re
from html import unescape

CREDENTIAL_RULE_VERSION = 'dashboard-public-text-v2'
CREDENTIAL_RE = re.compile(r'''(?i)(?:(?<![a-z0-9_-])(?:sk-[a-z0-9_-]{12,}|gh[pousr]_[a-z0-9_]+|bearer\s+\S+)|(?<![a-z0-9_])["']?(?:api[_-]?key|password|secret|token)["']?\s*[:=]\s*(?:"(?:\\.|[^"\\\r\n])*"|'(?:\\.|[^'\\\r\n])*'|[^\s,;"']+))''')
URL_USERINFO_RE = re.compile(r'''(?i)\b[a-z][a-z0-9+.-]*://(?P<userinfo>[^\s/@:<>"'`?#]*:[^\s/<>"'`?#]+)@''')
XML_TAG_RE = re.compile(r'''(?is)<(?P<closing>/)?(?P<tag>[a-z_][\w:.-]*)(?P<attrs>(?:[^<>"']|"[^"<]*"|'[^'<]*')*)>''')
XML_ATTRIBUTE_RE = re.compile(r'''(?is)(?P<name>[\w:.-]+)\s*=\s*(?P<quote>["'])(?P<value>.*?)(?P=quote)''')
XML_CREDENTIAL_NAME_RE = re.compile(r'(?i)(?:api[_-]?key|(?:user[_-])?password|(?:auth[_-]|access[_-])?token|(?:client[_-])?secret|private[_-]?key)\Z')


def structured_credential_spans(text):
    spans = [(match.start('userinfo'),match.end('userinfo'),'url_userinfo_requires_review') for match in URL_USERINFO_RE.finditer(text)]
    stack=[];cursor=0;last_close={}
    while True:
        start=text.find('<',cursor)
        if start<0: break
        if stack and (text.startswith('<![CDATA[',start) or text.startswith('<!--',start)):
            end_marker=']]>' if text.startswith('<![CDATA[',start) else '-->'
            end=text.find(end_marker,start+4)
            if end<0:break
            cursor=end+len(end_marker);continue
        match=XML_TAG_RE.match(text,start)
        if match is None:
            cursor=start+1;continue
        cursor=match.end();tag=match.group('tag').lower()
        if match.group('closing'):
            found=next((i for i in range(len(stack)-1,-1,-1) if stack[i][0]==tag),None)
            if found is not None:
                for _,value_start,sensitive in stack[found:]:
                    if sensitive and text[value_start:start].strip():spans.append((value_start,start,'xml_credential_field_requires_review'))
                del stack[found:]
            continue
        attributes=list(XML_ATTRIBUTE_RE.finditer(match.group('attrs')))
        named=any(attr.group('name').lower() in ('name','key') and XML_CREDENTIAL_NAME_RE.fullmatch(unescape(attr.group('value'))) for attr in attributes)
        for attr in attributes:
            if (XML_CREDENTIAL_NAME_RE.fullmatch(attr.group('name').rsplit(':',1)[-1]) or
                    (named and attr.group('name').lower()=='value')) and attr.group('value').strip():
                spans.append((match.start('attrs')+attr.start('value'),match.start('attrs')+attr.end('value'),'xml_credential_field_requires_review'))
        sensitive=bool(named or XML_CREDENTIAL_NAME_RE.fullmatch(tag.rsplit(':',1)[-1]))
        inline_placeholder=(not match.group('attrs') and start>0 and text[start-1]=='`' and text[cursor:cursor+1]=='`')
        if (not match.group('attrs') and ':' not in tag and XML_CREDENTIAL_NAME_RE.fullmatch(tag)
                and re.search(r'''(?i)(?:bearer\s+|(?:api[_-]?key|password|token|secret)["']?\s*[:=]\s*["']?)$''',text[max(0,start-80):start])):
            if tag not in last_close:last_close[tag]=text.lower().rfind('</'+tag)
            inline_placeholder=last_close[tag]<cursor
        if match.group('attrs').rstrip().endswith('/') or inline_placeholder:continue
        if sensitive or stack:
            if len(stack)>=256:
                # Unresolved excessive nesting never exposes the remaining sensitive body.
                first=min(value_start for _,value_start,is_sensitive in stack if is_sensitive)
                spans.append((first,len(text),'xml_credential_field_requires_review'));return spans
            stack.append((tag,cursor,sensitive))
    for _,start,sensitive in stack:
        if sensitive and text[start:].strip():spans.append((start,len(text),'xml_credential_field_requires_review'))
    return spans


def mask_credentials(value):
    text=str(value)
    spans=[match.span() for match in CREDENTIAL_RE.finditer(text)]
    spans.extend((start,end) for start,end,_ in structured_credential_spans(text))
    merged=[]
    for start,end in sorted(spans):
        if merged and start < merged[-1][1]:
            merged[-1]=(merged[-1][0],max(merged[-1][1],end))
        else:
            merged.append((start,end))
    parts=[]; previous=0
    for start,end in merged:
        parts.extend((text[previous:start],'[REDACTED]'+'\n'*text[start:end].count('\n')));previous=end
    parts.append(text[previous:])
    return ''.join(parts)
