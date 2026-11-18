"""Bounded source adapters. Reads never execute source code or emit raw host configs."""
from collections import Counter
from contextlib import closing
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
from datetime import datetime, timezone
import urllib.parse
import tomllib
from types import CodeType
from observer_read import connect_ro
from security_review import CREDENTIAL_RE, credential_review, execution_review, _reference_spans, _is_value_reference, _ENV_RE, _PLACEHOLDER_RE, _VALUE_RE
from credential_mask import CREDENTIAL_RULE_VERSION,mask_credentials,structured_credential_spans,URL_USERINFO_RE,XML_TAG_RE,XML_ATTRIBUTE_RE,XML_CREDENTIAL_NAME_RE

NATIVE_DB=Path(os.environ.get('APPDATA', Path.home()/'AppData/Roaming'))/'OpenBot/openbot.db'
NATIVE_AGENTS=Path.home()/'OpenBot/Agents'
PLUGINS=Path.home()/'.claude/plugins/installed_plugins.json'
GRAPH='skill-hub/state/graph-projection.json'
AUDIT='skill-hub/state/AUDIT.json'
EMPTY_BODY_SHA = 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'
HEALTH={'skill-hub':('127.0.0.1',8765,'/health')}
_TASK_STATE_UNSET = object()
ROUTE_TASK_TARGET=('127.0.0.1',8765,'/route_task')

_QUALITY_SCAN_SCHEMA_VERSION = 4
_EXEC_SCAN_RE = re.compile(r'(?i)curl[^\n]{0,200}\|\s*(?:sh|bash)\b|powershell[^\n]{0,100}(?<!\w)-enc(?:odedcommand)?\b|ignore (?:all |previous )?instructions')
_EXEC_SIGNAL_RES = (
    ('curl_pipe_shell', re.compile(r'(?i)curl[^\n]{0,200}\|\s*(?:sh|bash)\b')),
    ('powershell_encoded', re.compile(r'(?i)powershell[^\n]{0,100}(?<!\w)-enc(?:odedcommand)?\b')),
    ('prompt_injection_phrase', re.compile(r'(?i)ignore (?:all |previous )?instructions')),
)
_CRED_PREFIX_RES = (
    ('prefix_sk', re.compile(r'(?i)(?<![a-z0-9_-])sk-[a-z0-9_-]{12,}')),
    ('prefix_gh', re.compile(r'(?i)(?<![a-z0-9_-])gh[pousr]_[a-z0-9_]+')),
)
_LONG_TOKEN_RE = re.compile(r'[A-Za-z0-9+/=_-]{24,}')
_PRE_KV_RE = re.compile(r'''(?:api[_-]?key|password|secret|token)["']?\s*[:=]\s*\S''')
_PRE_CURL_RE = re.compile(r'curl[^\n]{0,200}\|\s*(?:sh|bash)\b')
_PRE_PS_RE = re.compile(r'powershell[^\n]{0,100}(?<!\w)-enc(?:odedcommand)?\b')
_PRE_BEARER_RE = re.compile(r'bearer\s+\S')
_KV_SCAN_HINTS = ('api_key', 'api-key', 'apikey', 'password', 'secret', 'token')

def _has_quality_hint(low):
    if ('://' in low and '@' in low) or ('<' in low and '>' in low):
        if structured_credential_spans(low): return True
    if '[redacted]' in low or 'ignore instructions' in low or 'ignore all instructions' in low or 'ignore previous instructions' in low:
        return True
    if 'curl' in low and _PRE_CURL_RE.search(low):
        return True
    if 'powershell' in low and _PRE_PS_RE.search(low):
        return True
    if 'sk-' in low or 'ghp_' in low or 'gho_' in low or 'ghu_' in low or 'ghs_' in low or 'ghr_' in low:
        return True
    if 'bearer' in low and _PRE_BEARER_RE.search(low):
        return True
    if (':' in low or '=' in low) and any(h in low for h in _KV_SCAN_HINTS):
        return bool(_PRE_KV_RE.search(low))
    return False

def _safe_evidence_snippet(line_text, clean_fn):
    cleaned = clean_fn(line_text.replace('\r', ''))
    masked = _LONG_TOKEN_RE.sub(lambda m: f'<TOKEN:{len(m.group(0))}>', cleaned)
    return masked.strip()[:160]

def _line_evidence(clean_body, match_pos, rule, signal, clean_fn):
    line_no = clean_body[:match_pos].count('\n') + 1
    line_start = clean_body.rfind('\n', 0, match_pos) + 1
    line_end = clean_body.find('\n', match_pos)
    if line_end == -1:
        line_end = len(clean_body)
    return {
        'rule': rule,
        'signal': signal,
        'line': line_no,
        'snippet': _safe_evidence_snippet(clean_body[line_start:line_end], clean_fn),
    }

def _sha256_text(text):
    return hashlib.sha256(text.encode('utf-8', 'replace')).hexdigest()

def _code_semantics(code):
    return {'bytecode':code.co_code.hex(),'names':code.co_names,
            'constants':[_code_semantics(value) if isinstance(value,CodeType) else value for value in code.co_consts]}

def _detector_fingerprint(clean_fn):
    clean_code=getattr(clean_fn,'__code__',None)
    clean_part=_code_semantics(clean_code) if clean_code is not None else repr(clean_fn)
    payload={
        'schema_version':_QUALITY_SCAN_SCHEMA_VERSION,
        'exec_scan_re':(_EXEC_SCAN_RE.pattern,_EXEC_SCAN_RE.flags),
        'exec_signals':[(s,rx.pattern,rx.flags) for s,rx in _EXEC_SIGNAL_RES],
        'cred_prefixes':[(s,rx.pattern,rx.flags) for s,rx in _CRED_PREFIX_RES],
        'credential_rules':[(rx.pattern,rx.flags) for rx in (CREDENTIAL_RE,_ENV_RE,_PLACEHOLDER_RE,_VALUE_RE)],
        'structured_credential_rules':[(rx.pattern,rx.flags) for rx in (URL_USERINFO_RE,XML_TAG_RE,XML_ATTRIBUTE_RE,XML_CREDENTIAL_NAME_RE)],
        'mask_rule_version':CREDENTIAL_RULE_VERSION,
        'mask_semantics':_code_semantics(mask_credentials.__code__),
        'structured_semantics':_code_semantics(structured_credential_spans.__code__),
        'hint_semantics':_code_semantics(_has_quality_hint.__code__),
        'triage_semantics':_code_semantics(credential_review.__code__),
        'reference_semantics':_code_semantics(_reference_spans.__code__),
        'reference_value_semantics':_code_semantics(_is_value_reference.__code__),
        'educational_semantics':_code_semantics(execution_review.__code__),
        'hint_rules':[(rx.pattern,rx.flags) for rx in (_PRE_KV_RE,_PRE_CURL_RE,_PRE_PS_RE,_PRE_BEARER_RE)],
        'hint_keywords':_KV_SCAN_HINTS,
        'evidence_semantics':_code_semantics(_safe_evidence_snippet.__code__),
        'token_mask':(_LONG_TOKEN_RE.pattern,_LONG_TOKEN_RE.flags),
        'clean_semantics':clean_part,
    }
    return hashlib.sha256(json.dumps(payload,sort_keys=True).encode('utf-8')).hexdigest()[:32]

def read_json(path,cap=16000000):
    if not path.is_file() or path.stat().st_size>cap: raise OSError('source unavailable or oversized')
    return json.loads(path.read_text(encoding='utf-8-sig'))

class Sources:
    def __init__(self,app,clean,native_db=NATIVE_DB,native_agents=NATIVE_AGENTS,plugins=PLUGINS):
        self.app,self.clean=app,clean
        self.catalog = getattr(app, 'catalog', None)
        self.native_db,self.native_agents,self.plugins_path=Path(native_db),Path(native_agents),Path(plugins)
        self.native_snapshot=None
        self._quality_lock=threading.Lock()
        self._quality_cache=None
        self.security_review_receipt=None
        self.original_decisions_receipt=None

    def _original_security_review(self,q):
        """Read operator-pinned, metadata-only original-source review, never source text."""
        path=self.security_review_receipt
        if path is None:
            return {'status':'unavailable','reason':'NO_ORIGINAL_SOURCE_RECEIPT','items':[]}
        try:
            manifest=self.app.input_manifest
            expected=manifest['sha256']['security-review.json']
            raw=Path(path).read_bytes()
            if len(raw)>8000000 or hashlib.sha256(raw).hexdigest()!=expected:
                raise ValueError('receipt mismatch')
            receipt=json.loads(raw)
            if receipt['schema']!='skill-hub.security-review.v1' or receipt['catalog_sha256']!=manifest['sha256']['skill-index.db']:
                raise ValueError('catalog mismatch')
            if receipt['detector_fingerprint']!=_detector_fingerprint(self.clean): raise ValueError('stale detector')
            reviewed_at=receipt['reviewed_at']
            if not isinstance(reviewed_at,str) or len(reviewed_at)>40 or datetime.fromisoformat(reviewed_at).tzinfo is None: raise ValueError('invalid review timestamp')
            allowed={'kind','id','source_scope','source_status','source_content_sha256','source_file_sha256',
                     'catalog_body_sha256','catalog_content_sha256','content_matches_catalog','indexed_active',
                     'index_body_empty','checks','reasons','evidence_lines','disposition'}
            dispositions={'confirmed_example','needs_source_review','no_rule_hit','source_unavailable','source_changed'}
            checks={'EXECUTION_OR_INJECTION_REVIEW','REDACTED_CREDENTIAL_REVIEW'}
            reasons={'environment_reference','explicit_placeholder','python_value_reference',
                     'literal_or_expression_requires_review','original_redacted_marker_requires_review',
                     'provider_format_literal','quoted_literal_requires_review','dynamic_expression_unverified','unquoted_literal_or_type_unverified',
                     'url_userinfo_requires_review','xml_credential_field_requires_review',
                     'educational_attack_example','unverified_execution_or_override'}
            rows=receipt['records']
            if not isinstance(rows,list) or len(rows)>20000: raise ValueError('invalid records')
            seen=set()
            for row in rows:
                if not isinstance(row,dict) or set(row)!=allowed: raise ValueError('invalid receipt fields')
                if row['kind'] not in ('skill','prompt') or row['disposition'] not in dispositions: raise ValueError('invalid identity')
                if not isinstance(row['id'],str) or not re.fullmatch(r'[\w.:/@+\-]{1,240}',row['id']) or self.clean(row['id'])!=row['id']: raise ValueError('invalid id')
                identity=(row['kind'],row['id'])
                if identity in seen: raise ValueError('duplicate id')
                seen.add(identity)
                if row['source_scope']!='original_document' or row['source_status'] not in ('READ','UNAVAILABLE','CHANGED'): raise ValueError('invalid scope')
                for key in ('source_content_sha256','source_file_sha256','catalog_body_sha256','catalog_content_sha256'):
                    if row.get(key) is not None and not re.fullmatch('[a-f0-9]{64}',row[key]): raise ValueError('invalid digest')
                for key in ('indexed_active','index_body_empty','content_matches_catalog'):
                    if type(row[key]) is not bool: raise ValueError('invalid flag')
                if not isinstance(row['checks'],list) or set(row['checks'])-checks: raise ValueError('invalid checks')
                if not isinstance(row['reasons'],list) or set(row['reasons'])-reasons: raise ValueError('invalid reasons')
                if not isinstance(row['evidence_lines'],list) or any(type(line) is not int or not 1<=line<=1000000 for line in row['evidence_lines']): raise ValueError('invalid evidence')
                if row['disposition'] in ('confirmed_example','needs_source_review','no_rule_hit') and (row['source_status']!='READ' or not row['content_matches_catalog']): raise ValueError('unverified disposition')
                if row['disposition']=='no_rule_hit' and row['checks']: raise ValueError('contradictory checks')
                if row['disposition'] in ('confirmed_example','needs_source_review') and not row['checks']: raise ValueError('missing checks')
            # Decisions are advisory and never modify the scanner verdict or activation state.
            decisions=self._load_original_decisions()
            hashes=('source_file_sha256','source_content_sha256','catalog_body_sha256','catalog_content_sha256')
            for row in rows:
                decision=decisions.get(f"{row['kind']}:{row['id']}")
                if isinstance(decision,dict):
                    valid=set(decision)=={'kind','id','outcome','reason','evidence_lines',*hashes}
                    valid=valid and decision.get('kind')==row['kind'] and decision.get('id')==row['id']
                    valid=valid and all(decision.get(k)==row.get(k) and row.get(k) for k in hashes)
                    valid=valid and row['source_status']=='READ' and row['content_matches_catalog']
                    valid=valid and decision.get('outcome') in ('approved','documented_example','prerequisite','restricted')
                    valid=valid and decision.get('reason') in row['reasons']
                    evidence=decision.get('evidence_lines')
                    valid=valid and isinstance(evidence,list) and bool(evidence) and all(type(x) is int for x in evidence) and set(evidence)<=set(row['evidence_lines'])
                    row['manual_decision']=decision if valid else {'status':'stale'}
            disposition=q.get('original_disposition','all')
            if disposition!='all' and disposition not in dispositions: raise ValueError('invalid filter')
            query=(q.get('original_q') or '').lower()[:100]
            filtered=[row for row in rows if (disposition=='all' or row['disposition']==disposition) and (not query or query in row['id'].lower())]
            offset=max(0,min(20000,int(q.get('original_offset',0))))
            limit=max(1,min(500,int(q.get('original_limit',100))))
            return {'status':'verified-receipt','catalog_sha256':receipt['catalog_sha256'],
                    'receipt_sha256':expected,'reviewed_at':reviewed_at,'detector_fingerprint':receipt['detector_fingerprint'],'items':filtered[offset:offset+limit],
                    'summary':dict(Counter(row['disposition'] for row in rows)),
                    'coverage':{'reviewed':len(rows),'filtered_total':len(filtered),'offset':offset,'limit':limit,
                                'returned':len(filtered[offset:offset+limit]),'truncated':len(filtered)>offset+limit},
                    'scope':'Locally reviewed original documents; hash-pinned metadata receipt. No raw source mounted; no execution or credential validity test; separate from normalized catalog body coverage.'}
        except (OSError,ValueError,TypeError,KeyError,AttributeError):
            return {'status':'unavailable','reason':'ORIGINAL_SOURCE_RECEIPT_INVALID','items':[]}
    def _load_original_decisions(self, include_receipt=True):
        entries={}
        if include_receipt and self.original_decisions_receipt is not None:
            path=Path(self.original_decisions_receipt)
            expected=self.app.input_manifest['sha256'].get('original-decisions.json')
            if expected is not None or path.exists():
                if path.is_symlink() or not path.is_file() or path.stat().st_size>8000000: raise ValueError('invalid decisions receipt')
                raw=path.read_bytes()
                if hashlib.sha256(raw).hexdigest()!=expected: raise ValueError('decisions receipt mismatch')
                receipt=json.loads(raw)
                if set(receipt)!={'schema','entries'} or receipt['schema']!='skill-hub.original-decisions.v1' or not isinstance(receipt['entries'],dict) or len(receipt['entries'])>20000: raise ValueError('invalid decisions receipt schema')
                entries.update(receipt['entries'])
        try:
            path=self.app.owned_target('original-dispositions')
            if not path.is_file() or path.is_symlink(): return entries
            payload=json.loads(json.loads(path.read_text('utf-8'))['content'])
            if payload['schema']=='skill-hub.original-decisions.v1' and isinstance(payload['entries'],dict): entries.update(payload['entries'])
            return entries
        except (OSError,ValueError,TypeError,KeyError):
            return entries
    def source(self,name,path,kind='json'):
        s=self.app.source(name,path); s['type']=kind
        if kind=='directory' and path.is_dir(): s['status']='readable'; s['freshness']='directory-snapshot'
        return s
    def snapshot_data(self):
        raw=read_json(self.native_snapshot,4000000)
        if raw.get('schema')!='skill-hub.dashboard.native.v1': raise ValueError('unsupported native snapshot')
        return raw
    def snapshot_source(self):
        source=self.source('sanitized-native-snapshot',self.native_snapshot)
        source['recorded_at']=self.snapshot_data().get('observed_at')
        return source
    def export(self):
        agents,ags=self.agents({'limit':'100'})
        if agents['total']>100:
            more,_=self.agents({'limit':'100','offset':'100'}); agents['items']+=more['items']; agents['limit']=200
        integrations,ms=self.integrations()
        try: plugins,ps=self.plugins()
        except (OSError,ValueError): plugins=None; ps={'status':'unavailable','name':'plugins'}
        return {'schema':'skill-hub.dashboard.native.v1','observed_at':datetime.now(timezone.utc).isoformat(),'agents':agents,'integrations':integrations,'plugins':plugins,'sources':ags+ms+[ps]}
    def snapshot_items(self,key,fields):
        raw=self.snapshot_data().get(key,{}); rows=raw.get('items',[]) if isinstance(raw,dict) else raw
        if not isinstance(rows,list): raise ValueError('invalid snapshot rows')
        def value(v):
            if v is None or type(v) in (bool,int): return v
            if isinstance(v,str): return self.clean(v[:10000])
            if isinstance(v,list): return [value(x) for x in v[:300] if isinstance(x,(str,bool,int))]
            return None
        return [{k:value(row.get(k)) for k in fields} for row in rows[:500] if isinstance(row,dict)]
    def plugins(self):
        if self.native_snapshot: return self.snapshot_items('plugins',('id','host','version','scope','status','updated_at')),self.snapshot_source()
        raw=read_json(self.plugins_path,2000000)
        items=[]
        for name,records in list(raw.get('plugins',{}).items())[:500]:
            if not isinstance(records,list): continue
            for record in records[:20]:
                items.append({'id':self.clean(name),'host':'claude','version':self.clean(record.get('version','')),'scope':self.clean(record.get('scope','')),'status':'installed-record','updated_at':record.get('lastUpdated')})
        return items,self.source('claude-installed-plugins',self.plugins_path)
    def integrations(self,probe=None):
        if self.native_snapshot:
            if probe and probe not in HEALTH: raise ValueError('health target not allowed')
            items=self.snapshot_items('integrations',('id','name','transport','enabled','updated_at','status','connection'))
            # Tool names alone are portable; never copy arbitrary tool config/schema.
            raw=self.snapshot_data().get('integrations',{}).get('items',[])
            for item,original in zip(items,raw):
                item['tools']=[{'name':self.clean(t['name'])} for t in original.get('tools',[])[:300] if isinstance(t,dict) and isinstance(t.get('name'),str)]
                item['health']=self.health(probe) if probe==item['name'] else None
            return {'items':items,'total':len(items),'offset':0,'limit':100,'configured_count':sum(i['status']=='configured' for i in items),'health_targets':list(HEALTH)},[self.snapshot_source()]
        items=[]; sources=[self.source('openbot-mcp-projection',self.native_db,'sqlite')]
        try:
            with closing(connect_ro(self.native_db)) as con:
                rows=con.execute('SELECT mcp_server_id,name,transport,enabled,updated_at FROM projection_mcp_servers ORDER BY name LIMIT 100').fetchall()
            for ident,name,transport,enabled,updated in rows:
                items.append({'id':ident,'name':self.clean(name),'transport':transport,'enabled':bool(enabled),'updated_at':updated,'status':'configured','connection':None,'tools':[],'health':self.health(name) if probe==name else None})
        except (OSError,sqlite3.Error): sources[0]['status']='unavailable'
        try:
            raw,source=self.app.json_source('mcp-tool-catalog.json'); sources.append(source)
            for name,tools in list(raw.items())[:100]:
                item=next((i for i in items if i['name']==name),None)
                if not item:
                    item={'id':'catalog:'+name,'name':self.clean(name),'status':'catalogued','enabled':None,'connection':None,'transport':None,'health':None}; items.append(item)
                item['tools']=[{'name':self.clean(t.get('name',''))} for t in tools[:300] if isinstance(t,dict)]
        except (OSError,ValueError): pass
        if probe and probe not in HEALTH: raise ValueError('health target not allowed')
        return {'items':items,'total':len(items),'offset':0,'limit':100,'configured_count':sum(i['status']=='configured' for i in items),'health_targets':list(HEALTH)},sources
    def health(self,name):
        if name not in HEALTH: return {'status':'unavailable','target':name,'observed_at':datetime.now(timezone.utc).isoformat()}
        host,port,path=HEALTH[name]
        observed_at=datetime.now(timezone.utc).isoformat()
        con=http.client.HTTPConnection(host,port,timeout=2)
        try:
            con.request('GET',path); response=con.getresponse(); raw=response.read(16385)
            if response.status not in (200, 503) or len(raw)>16384: return {'status':'unavailable','http_status':response.status,'observed_at':observed_at,'target':name}
            body=json.loads(raw)
            if not isinstance(body,dict): raise ValueError('health response must be object')
            fields={k:self.clean(body[k]) if isinstance(body[k],str) else body[k] for k in ('status','catalog_skills','catalog_sha256','effective_catalog_sha256','broker_execution','projection_scope') if k in body and isinstance(body[k],(str,int,bool))}
            capabilities = body.get('capabilities')
            if isinstance(capabilities, dict) and capabilities.get('model_routing') in ('configured', 'disabled'):
                fields['model_routing'] = capabilities['model_routing']
            publication=body.get('publication')
            if isinstance(publication,dict):
                fields['publication']={k:publication[k] for k in ('published_skills','published_prompts') if type(publication.get(k)) is int and publication[k]>=0}
            warmup=body.get('warmup')
            if isinstance(warmup,dict):
                clean_warmup={}
                for k in ('ok','catalog_candidates','tempr_configured','semantic_vectors','graph_loaded','temporal_loaded','gpu_resident'):
                    if k in warmup and isinstance(warmup[k],(bool,int)):
                        clean_warmup[k]=warmup[k]
                errs=warmup.get('errors')
                if isinstance(errs,list):
                    clean_warmup['errors']=[self.clean(str(e)[:200]) for e in errs[:10]]
                fields['warmup']=clean_warmup
            status='observed' if response.status==200 else 'degraded'
            return {'status':status,'http_status':response.status,'observed_at':observed_at,'fields':fields,'target':name}
        except (OSError,ValueError): return {'status':'unavailable','target':name,'observed_at':observed_at}
        finally: con.close()

    def semantic_search(self,query,limit=10):
        if 'skill-hub' not in HEALTH:
            return {'status':'unavailable','hits':[],'total':0,'reason':'Target skill-hub not configured'}
        host,port,_=HEALTH['skill-hub']
        observed_at=datetime.now(timezone.utc).isoformat()
        q_str=self.clean(str(query or '').strip()[:200])
        if not q_str:
            return {'status':'ok','hits':[],'total':0,'query':'','observed_at':observed_at,'search_mode':'semantic'}
        try: lim=max(1,min(int(limit),50))
        except (ValueError,TypeError): lim=10
        con=http.client.HTTPConnection(host,port,timeout=4)
        try:
            path=f"/semantic?q={urllib.parse.quote(q_str)}&limit={lim}"
            con.request('GET',path); response=con.getresponse(); raw=response.read(65537)
            if response.status not in (200, 500, 503, 504) or len(raw)>65536:
                return {'status':'degraded' if response.status==503 else 'unavailable','http_status':response.status,'hits':[],'total':0,'observed_at':observed_at}
            try:
                body=json.loads(raw)
            except Exception:
                return {'status':'degraded' if response.status==503 else 'unavailable','http_status':response.status,'hits':[],'total':0,'observed_at':observed_at}
            if not isinstance(body,dict): return {'status':'unavailable','hits':[],'total':0,'observed_at':observed_at}
            raw_hits=body.get('hits',[])
            hits=[]
            if isinstance(raw_hits,list):
                for h in raw_hits[:lim]:
                    if isinstance(h,dict):
                        hits.append({
                            'id':self.clean(str(h.get('id',''))[:100]),
                            'rank':int(h.get('rank',0)) if isinstance(h.get('rank'),(int,float)) else 0,
                            'raw_score':float(h.get('raw_score',0.0)) if isinstance(h.get('raw_score'),(int,float)) else 0.0,
                            'evidence':h.get('evidence') if isinstance(h.get('evidence'),dict) else {}
                        })
            backend_status = str(body.get('status', 'ok' if response.status==200 else 'degraded')).lower()
            if response.status != 200 and backend_status == 'ok':
                backend_status = 'degraded' if response.status==503 else 'error'
            return {
                'status':backend_status,
                'query':q_str,
                'hits':hits,
                'total':len(hits),
                'elapsed_ms':body.get('elapsed_ms'),
                'model':self.clean(str(body.get('model','bge-m3:latest'))),
                'gpu_resident':bool(body.get('gpu_resident',False)),
                'observed_at':observed_at,
                'search_mode':'semantic',
                'error':self.clean(str(body.get('error') or body.get('reason') or '')) if (body.get('error') or body.get('reason')) else None
            }
        except (OSError,ValueError):
            return {'status':'unavailable','hits':[],'total':0,'query':q_str,'observed_at':observed_at}
        finally: con.close()

    def _sanitize(self, val):
        if isinstance(val, str):
            return self.clean(val)
        elif isinstance(val, dict):
            return {k: self._sanitize(v) for k, v in val.items()}
        elif isinstance(val, list):
            return [self._sanitize(v) for v in val]
        return val

    def route_task(self, state, retrieval_mode='tempr', include_graph=False, task_id=None, task_state=_TASK_STATE_UNSET):
        if not isinstance(state, str) or not state.strip():
            raise ValueError('state is required')
        clean_state = state
        if task_state is not _TASK_STATE_UNSET and task_state is not None and not isinstance(task_state, dict):
            raise ValueError('INVALID_TASK_STATE: task_state must be an object or null')
        env_bin = os.environ.get('SKILL_HUB_HOST_BIN')
        if env_bin:
            if not os.path.isabs(env_bin) or os.path.islink(env_bin) or not os.path.isfile(env_bin):
                raise ValueError('RUST_HOST_BINARY_INVALID: SKILL_HUB_HOST_BIN must be an absolute non-symlink file')
            rust_bin = env_bin
        else:
            rust_bin = None
            for cand in (
                '/app/skill_hub_host',
                '/usr/local/bin/skill_hub_host',
                os.path.join(os.environ.get('LOCALAPPDATA', ''), 'SkillHub', 'bin', 'skill_hub_host.exe') if os.environ.get('LOCALAPPDATA') else None,
                os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'host-rust', 'target', 'release', 'skill_hub_host.exe')),
                os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'host-rust', 'target', 'release', 'skill_hub_host')),
            ):
                if cand and os.path.isabs(cand) and not os.path.islink(cand) and os.path.isfile(cand):
                    rust_bin = cand
                    break
        if task_state is not _TASK_STATE_UNSET and task_state is not None:
            if not rust_bin:
                if os.path.isfile('/app/container_entry.py') or os.environ.get('SKILL_HUB_REQUIRE_RUST_HOST') == '1':
                    raise ValueError('RUST_HOST_BINARY_MISSING: skill_hub_host binary not found')
            else:
                import subprocess
                proc = subprocess.run(
                    [rust_bin, 'normalize-task-state'],
                    input=json.dumps({'state': clean_state, 'task_state': task_state}, ensure_ascii=False),
                    text=True, encoding='utf-8', capture_output=True, timeout=5.0,
                )
                try:
                    norm_out = json.loads(proc.stdout) if proc.stdout else None
                except ValueError as exc:
                    raise ValueError('RUST_HOST_MALFORMED_OUTPUT: invalid JSON from skill_hub_host') from exc
                if not isinstance(norm_out, dict):
                    raise ValueError('RUST_HOST_MALFORMED_OUTPUT: missing JSON object from skill_hub_host')
                if proc.returncode == 0:
                    if not isinstance(norm_out.get('task_state_sha256'), str):
                        raise ValueError('INVALID_TASK_STATE: Rust normalizer missing digest')
                elif norm_out.get('error') != 'INVALID_TASK_STATE':
                    raise ValueError(f"RUST_HOST_NORMALIZE_FAILED: {norm_out.get('error') or proc.returncode}")
        if retrieval_mode not in ('tempr', 'metadata'):
            raise ValueError('retrieval_mode must be tempr or metadata')

        host, port, _ = HEALTH.get('skill-hub', ROUTE_TASK_TARGET)
        _, _, path = ROUTE_TASK_TARGET
        observed_at = datetime.now(timezone.utc).isoformat()
        query_dict = {
            'state': self.clean(clean_state),
            'retrieval_mode': retrieval_mode,
            'include_graph': bool(include_graph),
            'task_id': self.clean(str(task_id)) if task_id else None
        }

        cat_source = self.source('catalog', self.catalog, 'sqlite-snapshot') if self.catalog else None
        sources = [cat_source] if cat_source else []

        con = http.client.HTTPConnection(host, port, timeout=45.0)
        try:
            req_payload = {
                'state': clean_state,
                'response_mode': 'full',
                'retrieval_mode': retrieval_mode,
                'include_graph': bool(include_graph),
                'task_id': task_id
            }
            if task_state is not _TASK_STATE_UNSET:
                req_payload['task_state'] = task_state
            req_body = json.dumps(req_payload).encode('utf-8')
            con.request('POST', path, body=req_body, headers={'Content-Type': 'application/json'})
            response = con.getresponse()
            # Reject oversized full results instead of parsing a truncated prefix.
            raw = response.read(64 * 1024 * 1024 + 1)
            if len(raw) > 64 * 1024 * 1024:
                raise ValueError('ROUTE_RESPONSE_TOO_LARGE: full Dashboard response exceeds 64 MiB')

            try:
                raw_body = json.loads(raw)
            except Exception:
                raw_body = None

            if not isinstance(raw_body, dict):
                return {
                    'status': 'unavailable',
                    'schema_version': 'openbot.elephant.route_task.v2',
                    'query': query_dict,
                    'outcome': None,
                    'trigger': None,
                    'reason': f'HTTP {response.status}: unparseable response from router',
                    'fallback': None,
                    'fallback_discovery': None,
                    'elapsed_ms': None,
                    'observed_at': observed_at,
                    'gpu_resident': None,
                    'thresholds': None,
                    'coverage': None,
                    'retrieval': None,
                    'provider': None,
                    'selected': [],
                    'loads': {'skills': [], 'prompts': [], 'deferred': []},
                    'scores': [],
                    'scores_total': 0,
                    'scores_truncated': False,
                    'telemetry': None,
                    'limits': None,
                    'error': f'HTTP {response.status}: unparseable response from router'
                }, sources

            transport = raw_body.get('transport')
            if ((isinstance(transport, dict) and transport.get('complete') is False)
                    or any(isinstance(raw_body.get(key), dict)
                           and raw_body[key].get('delivery') == 'DEFERRED_FULL_FETCH'
                           for key in ('retrieval', 'loads', 'selected', 'coverage'))):
                raise ValueError('ROUTE_RESPONSE_REQUIRES_FULL_FETCH: Dashboard requested full mode')
            sanitized_body = self._sanitize(raw_body)
            backend_status = sanitized_body.get('status')
            if response.status == 200:
                calc_status = 'ok'
            elif response.status == 503 and backend_status == 'busy':
                calc_status = 'busy'
            elif response.status in (400, 500, 502, 503):
                calc_status = 'error' if response.status in (400, 500, 502) else 'unavailable'
            else:
                calc_status = 'unavailable'

            raw_scores = sanitized_body.get('scores')
            if raw_scores is None:
                raw_scores = []

            # Gap 2: loads normalization & partitioning from loads['loaded']
            loads = sanitized_body.get('loads')
            if not isinstance(loads, dict):
                loads = {'skills': [], 'prompts': [], 'deferred': []}
            else:
                loaded_items = loads.get('loaded')
                if isinstance(loaded_items, list):
                    if not loads.get('skills') and not loads.get('prompts'):
                        skills_part = []
                        prompts_part = []
                        for item in loaded_items:
                            if isinstance(item, dict):
                                k = item.get('kind') or item.get('type') or ('prompt' if str(item.get('id', '')).startswith('prompt:') else 'skill')
                                if k == 'prompt':
                                    prompts_part.append(item)
                                else:
                                    skills_part.append(item)
                        loads['skills'] = skills_part
                        loads['prompts'] = prompts_part

                for k in ('skills', 'prompts', 'deferred'):
                    if k not in loads or not isinstance(loads[k], list):
                        loads[k] = []

            def _lookup_score(res_id_str, clean_name, kind):
                if isinstance(raw_scores, dict):
                    val = raw_scores.get(res_id_str)
                    if val is None:
                        val = raw_scores.get(clean_name)
                    if val is None:
                        val = raw_scores.get(f"{kind}:{clean_name}")
                    if val is not None:
                        try:
                            return float(val)
                        except (ValueError, TypeError):
                            return None
                elif isinstance(raw_scores, list):
                    for s in raw_scores:
                        if isinstance(s, dict):
                            sid = str(s.get('id') or '')
                            sname = str(s.get('name') or '')
                            if sid in (res_id_str, clean_name, f"{kind}:{clean_name}") or sname in (res_id_str, clean_name):
                                try:
                                    return float(s.get('score'))
                                except (ValueError, TypeError):
                                    return None
                return None

            def _normalize_load_item(item):
                if not isinstance(item, dict):
                    return item
                res_id_str = str(item.get('id') or item.get('name') or '')
                clean_name = item.get('name') or item.get('canonical_name') or (res_id_str.split(':', 1)[-1] if res_id_str else '')
                kind = item.get('kind') or item.get('type') or ('prompt' if res_id_str.startswith('prompt:') else 'skill')
                sha256 = item.get('sha256') or item.get('body_sha256') or item.get('served_content_sha256') or ''
                score = item.get('score')
                if score is None:
                    score = _lookup_score(res_id_str, clean_name, kind)
                else:
                    try:
                        score = float(score)
                    except (ValueError, TypeError):
                        score = None

                item['id'] = res_id_str or (f"{kind}:{clean_name}" if clean_name else '')
                item['name'] = clean_name
                item['kind'] = kind
                item['sha256'] = sha256
                if score is not None:
                    item['score'] = score
                return item

            for k in ('skills', 'prompts', 'deferred'):
                loads[k] = [_normalize_load_item(x) for x in loads[k]]
            if isinstance(loads.get('loaded'), list):
                loads['loaded'] = [_normalize_load_item(x) for x in loads['loaded']]

            selected_list = sanitized_body.get('selected') or []
            selected_ids = set()
            for s in selected_list:
                if isinstance(s, str):
                    selected_ids.add(s)
                    selected_ids.add(s.split(':', 1)[-1])
                elif isinstance(s, dict):
                    if s.get('id'):
                        selected_ids.add(str(s['id']))
                        selected_ids.add(str(s['id']).split(':', 1)[-1])
                    if s.get('name'):
                        selected_ids.add(str(s['name']))

            loaded_ids = set()
            for item in (loads.get('skills', []) + loads.get('prompts', []) + (loads.get('loaded') or [])):
                if isinstance(item, dict):
                    if item.get('id'):
                        loaded_ids.add(str(item['id']))
                        loaded_ids.add(str(item['id']).split(':', 1)[-1])
                    if item.get('name'):
                        loaded_ids.add(str(item['name']))
                elif isinstance(item, str):
                    loaded_ids.add(item)
                    loaded_ids.add(item.split(':', 1)[-1])

            deferred_ids = set()
            for item in loads.get('deferred', []):
                if isinstance(item, dict):
                    if item.get('id'):
                        deferred_ids.add(str(item['id']))
                        deferred_ids.add(str(item['id']).split(':', 1)[-1])
                    if item.get('name'):
                        deferred_ids.add(str(item['name']))
                elif isinstance(item, str):
                    deferred_ids.add(item)
                    deferred_ids.add(item.split(':', 1)[-1])

            # Gap 1: scores Dict vs List Normalization
            normalized_scores = []
            if isinstance(raw_scores, dict):
                for res_id, score_val in raw_scores.items():
                    res_id_str = str(res_id)
                    kind = 'prompt' if res_id_str.startswith('prompt:') else 'skill'
                    clean_name = res_id_str.split(':', 1)[-1]
                    try:
                        score_num = float(score_val)
                    except (ValueError, TypeError):
                        score_num = 0.0

                    if res_id_str in loaded_ids or clean_name in loaded_ids:
                        status = 'loaded'
                    elif res_id_str in deferred_ids or clean_name in deferred_ids:
                        status = 'deferred'
                    elif res_id_str in selected_ids or clean_name in selected_ids:
                        status = 'selected'
                    else:
                        status = 'below_threshold'

                    normalized_scores.append({
                        'id': res_id_str,
                        'name': clean_name,
                        'kind': kind,
                        'score': score_num,
                        'status': status
                    })
            elif isinstance(raw_scores, list):
                for item in raw_scores:
                    if isinstance(item, dict):
                        res_id_str = str(item.get('id') or item.get('name') or '')
                        clean_name = item.get('name') or item.get('canonical_name') or (res_id_str.split(':', 1)[-1] if res_id_str else '')
                        kind = item.get('kind') or item.get('type') or ('prompt' if res_id_str.startswith('prompt:') else 'skill')
                        try:
                            score_num = float(item.get('score', 0.0))
                        except (ValueError, TypeError):
                            score_num = 0.0

                        status = item.get('status')
                        if not status:
                            if res_id_str in loaded_ids or clean_name in loaded_ids:
                                status = 'loaded'
                            elif res_id_str in deferred_ids or clean_name in deferred_ids:
                                status = 'deferred'
                            elif res_id_str in selected_ids or clean_name in selected_ids:
                                status = 'selected'
                            else:
                                status = 'below_threshold'

                        entry = dict(item)
                        entry['id'] = res_id_str or (f"{kind}:{clean_name}" if clean_name else '')
                        entry['name'] = clean_name
                        entry['kind'] = kind
                        entry['score'] = score_num
                        entry['status'] = status
                        normalized_scores.append(entry)

            scores_total = len(normalized_scores)
            sorted_scores = sorted(
                normalized_scores,
                key=lambda x: (-float(x.get('score', 0.0)), str(x.get('id', '')))
            )
            scores_truncated = scores_total > 100
            bounded_scores = sorted_scores[:100]
            for idx, s_item in enumerate(bounded_scores):
                s_item['rank'] = idx + 1

            # Gap 3: coverage field aliases
            coverage = sanitized_body.get('coverage')
            if isinstance(coverage, dict):
                scored_val = coverage.get('scored_candidates') if 'scored_candidates' in coverage else coverage.get('scored', 0)
                retrieved_val = coverage.get('retrieved_candidates') if 'retrieved_candidates' in coverage else coverage.get('retrieved', 0)
                coverage['scored'] = scored_val
                coverage['scored_candidates'] = scored_val
                coverage['retrieved'] = retrieved_val
                coverage['retrieved_candidates'] = retrieved_val

            # Gap 4: retrieval.engines aliases & metadata synthesis
            retrieval = sanitized_body.get('retrieval')
            if isinstance(retrieval, dict):
                if isinstance(retrieval.get('engines'), dict):
                    for eng_name, eng_info in retrieval['engines'].items():
                        if isinstance(eng_info, dict):
                            count_val = eng_info.get('count') if 'count' in eng_info else eng_info.get('hits', 0)
                            hits_val = eng_info.get('hits') if 'hits' in eng_info else count_val
                            try:
                                c_int = int(count_val or 0)
                            except (ValueError, TypeError):
                                c_int = 0
                            try:
                                h_int = int(hits_val or 0)
                            except (ValueError, TypeError):
                                h_int = 0
                            eng_info['count'] = c_int
                            eng_info['hits'] = h_int
                else:
                    try:
                        ret_count = int(retrieval.get('retrieved', 0) or 0)
                    except (ValueError, TypeError):
                        ret_count = 0
                    retrieval['engines'] = {
                        'keyword': {
                            'status': 'OK',
                            'hits': ret_count,
                            'count': ret_count,
                            'elapsed_ms': retrieval.get('fts_elapsed_ms', 0)
                        },
                        'semantic': {
                            'status': 'SKIPPED',
                            'hits': 0,
                            'count': 0,
                            'elapsed_ms': 0
                        },
                        'graph': {
                            'status': 'SKIPPED',
                            'hits': 0,
                            'count': 0,
                            'elapsed_ms': 0
                        },
                        'temporal': {
                            'status': 'SKIPPED',
                            'hits': 0,
                            'count': 0,
                            'elapsed_ms': 0
                        }
                    }
            elif retrieval is None and calc_status == 'ok' and retrieval_mode == 'metadata':
                retrieval = {
                    'retrieval_mode': 'metadata',
                    'engines': {
                        'keyword': {'status': 'OK', 'hits': 0, 'count': 0, 'elapsed_ms': 0},
                        'semantic': {'status': 'SKIPPED', 'hits': 0, 'count': 0, 'elapsed_ms': 0},
                        'graph': {'status': 'SKIPPED', 'hits': 0, 'count': 0, 'elapsed_ms': 0},
                        'temporal': {'status': 'SKIPPED', 'hits': 0, 'count': 0, 'elapsed_ms': 0}
                    }
                }

            # Gap 5: provider enrichment
            provider = sanitized_body.get('provider')
            if isinstance(provider, dict):
                if not provider.get('model'):
                    provider['model'] = 'convaiinnovations/laya-typed-decisions'
                if provider.get('latency_ms') is None:
                    reqs = provider.get('requests')
                    if isinstance(reqs, list):
                        try:
                            comp_lat = sum(
                                float(r.get('latency_ms') or 0.0)
                                for r in reqs
                                if isinstance(r, dict) and r.get('latency_ms') is not None
                            )
                            provider['latency_ms'] = round(comp_lat, 2)
                        except (ValueError, TypeError):
                            pass

            outcome_val = sanitized_body.get('outcome')
            action_val = sanitized_body.get('action')
            if not action_val and outcome_val:
                action_val = 'select' if str(outcome_val).upper() == 'SELECTED' else 'abstain'
            elif not outcome_val and action_val:
                outcome_val = 'SELECTED' if str(action_val).lower() == 'select' else 'ABSTAIN'

            data = {
                'status': calc_status,
                'schema_version': sanitized_body.get('schema_version', 'openbot.elephant.route_task.v2'),
                'query': query_dict,
                'task_state': sanitized_body.get('task_state'),
                'task_state_sha256': sanitized_body.get('task_state_sha256'),
                'outcome': outcome_val,
                'action': action_val,
                'trigger': sanitized_body.get('trigger'),
                'reason': sanitized_body.get('reason'),
                'fallback': sanitized_body.get('fallback'),
                'fallback_discovery': sanitized_body.get('fallback_discovery'),
                'elapsed_ms': sanitized_body.get('elapsed_ms'),
                'observed_at': observed_at,
                'gpu_resident': sanitized_body.get('gpu_resident'),
                'thresholds': sanitized_body.get('thresholds'),
                'coverage': coverage,
                'retrieval': retrieval,
                'provider': provider,
                'selected': selected_list,
                'loads': loads,
                'scores': bounded_scores,
                'scores_total': scores_total,
                'scores_truncated': scores_truncated,
                'telemetry': sanitized_body.get('telemetry'),
                'limits': sanitized_body.get('limits'),
                'error': sanitized_body.get('error'),
                'detail': sanitized_body.get('detail')
            }
            if 'choice_pool' in sanitized_body:
                data['choice_pool'] = sanitized_body['choice_pool']
            return data, sources
        except (OSError, ValueError) as exc:
            return {
                'status': 'unavailable',
                'schema_version': 'openbot.elephant.route_task.v2',
                'query': query_dict,
                'outcome': None,
                'trigger': None,
                'reason': str(exc) if isinstance(exc, ValueError) and str(exc).startswith('ROUTE_RESPONSE_')
                    else 'Skill Hub router backend unavailable on port 8765',
                'fallback': None,
                'fallback_discovery': None,
                'elapsed_ms': None,
                'observed_at': observed_at,
                'gpu_resident': None,
                'thresholds': None,
                'coverage': None,
                'retrieval': None,
                'provider': None,
                'selected': [],
                'loads': {'skills': [], 'prompts': [], 'deferred': []},
                'scores': [],
                'scores_total': 0,
                'scores_truncated': False,
                'telemetry': None,
                'limits': None,
                'error': f'{type(exc).__name__}: {exc}'
            }, sources
        finally:
            con.close()
    def load_resource_choice(self, resource_id, source_raw_sha256,
                             loaded_skill_ids_and_versions=None, bundle_sha256=None):
        if not isinstance(resource_id, str) or not resource_id.startswith(('skill:', 'prompt:')):
            raise ValueError('INVALID_CHOICE_IDENTITY: resource_id must be skill:<id> or prompt:<id>')
        if not isinstance(source_raw_sha256, str) or not re.fullmatch(r'[a-f0-9]{64}', source_raw_sha256):
            raise ValueError('INVALID_CHOICE_IDENTITY: source_raw_sha256 must be 64-hex SHA-256')
        if resource_id.startswith('skill:') and (not isinstance(bundle_sha256, str) or not re.fullmatch(r'[a-f0-9]{64}', bundle_sha256)):
            raise ValueError('INVALID_CHOICE_IDENTITY: bundle_sha256 required for skill')
        host, port, _ = HEALTH.get('skill-hub', ROUTE_TASK_TARGET)
        con = http.client.HTTPConnection(host, port, timeout=25.0)
        sid = None
        try:
            def mcp_post(msg, sid=None):
                hdrs = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream'}
                if sid:
                    hdrs['Mcp-Session-Id'] = sid
                con.request('POST', '/mcp', body=json.dumps(msg, ensure_ascii=False).encode('utf-8'), headers=hdrs)
                resp = con.getresponse()
                raw = resp.read(64 * 1024 * 1024 + 1)
                if len(raw) > 64 * 1024 * 1024:
                    raise ValueError('LOAD_RESPONSE_TOO_LARGE')
                new_sid = resp.getheader('Mcp-Session-Id') or sid
                text = raw.decode('utf-8', 'replace')
                ctype = (resp.getheader('Content-Type') or '').lower()
                if 'text/event-stream' in ctype:
                    last_data = None
                    for line in text.splitlines():
                        if line.startswith('data:'):
                            last_data = line[5:].strip()
                    text = last_data or '{}'
                return resp.status, new_sid, (json.loads(text) if text.strip() else {})

            st, sid, _ = mcp_post({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                                   'params': {'protocolVersion': '2025-11-25', 'capabilities': {},
                                              'clientInfo': {'name': 'hub-dashboard', 'version': '1.0'}}})
            if st != 200:
                raise ValueError(f'MCP_INIT_FAILED: HTTP {st}')
            mcp_post({'jsonrpc': '2.0', 'method': 'notifications/initialized'}, sid=sid)
            args = {'resource_id': resource_id, 'source_raw_sha256': source_raw_sha256}
            if loaded_skill_ids_and_versions is not None:
                args['loaded_skill_ids_and_versions'] = loaded_skill_ids_and_versions
            if bundle_sha256 is not None:
                args['bundle_sha256'] = bundle_sha256
            st, _, rpc = mcp_post({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                                   'params': {'name': 'load_resource_choice', 'arguments': args}}, sid=sid)
            if st != 200:
                raise ValueError(f'MCP_CALL_FAILED: HTTP {st}')
            content = ((rpc.get('result') or {}).get('content') or [])
            payload_text = next((c.get('text') for c in content if isinstance(c, dict) and isinstance(c.get('text'), str)), None)
            if not payload_text:
                raise ValueError('MCP_EMPTY_CONTENT')
            receipt = json.loads(payload_text)
            if not isinstance(receipt, dict) or receipt.get('load_verified') is not True:
                return self._sanitize(receipt)
            body = receipt.get('body')
            if not isinstance(body, str):
                raise ValueError('MCP_EMPTY_CONTENT')
            raw = body.encode('utf-8')
            actual_sha = hashlib.sha256(raw).hexdigest()
            if receipt.get('body_raw_sha256') != actual_sha or source_raw_sha256 != actual_sha:
                raise ValueError('BODY_HASH_MISMATCH')
            if receipt.get('body_bytes') != len(raw):
                raise ValueError('BODY_SIZE_MISMATCH')
            sanitized = self._sanitize(receipt)
            if sanitized['body'] != body:
                return {'error': 'BODY_REDACTED'}
            sanitized['verified_in_client'] = True
            return sanitized
        finally:
            if sid:
                con.close()
                try:
                    con.request('DELETE', '/mcp', headers={'Mcp-Session-Id': sid})
                    con.getresponse().close()
                except (OSError, http.client.HTTPException):
                    pass
            con.close()


    def agents(self,q):
        if self.native_snapshot:
            items=self.snapshot_items('agents',('id','name','role','model','runtime','status','skills','mcp','permissions','updated_at','source','loaded','mcp_binding'))
            query=q.get('q','')[:200].lower(); items=[i for i in items if query in ((i.get('name') or '')+' '+(i.get('role') or '')).lower()]
            offset=max(0,min(100000,int(q.get('offset',0)))); limit=max(1,min(100,int(q.get('limit',50))))
            return {'items':items[offset:offset+limit],'total':len(items),'offset':offset,'limit':limit},[self.snapshot_source()]
        items=[]; sources=[self.source('openbot-agents-projection',self.native_db,'sqlite')]
        integrations,_=self.integrations()
        enabled=[i['name'] for i in integrations['items'] if i.get('enabled')]
        try:
            with closing(connect_ro(self.native_db)) as con:
                rows=con.execute('SELECT agent_id,model,agent_json,updated_at FROM projection_agents ORDER BY sort_order LIMIT 200').fetchall()
            for ident,model,payload,updated in rows:
                if len(payload)>200000: continue
                try: a=json.loads(payload)
                except ValueError: continue
                if not re.fullmatch(r'agent-[a-zA-Z0-9-]{1,100}',ident): continue
                skills=[]; folder=self.native_agents/ident/'.agents/skills'
                if folder.is_dir() and not folder.is_symlink():
                    for entry in folder.iterdir():
                        if len(skills)>=200: break
                        if not entry.is_symlink() and (entry/'SKILL.md').is_file(): skills.append(self.clean(entry.name))
                items.append({'id':ident,'name':self.clean(a.get('name',ident)),'role':self.clean(a.get('title','')),'model':self.clean(model or ''),'runtime':self.clean(a.get('provider','openbot')),'status':'configured','skills':skills,'mcp':enabled,'mcp_binding':'global configured availability; not evidence of per-agent load','permissions':self.clean(a['access']) if isinstance(a.get('access'),str) else None,'updated_at':updated,'source':'openbot-projection','loaded':None})
        except (OSError,sqlite3.Error): sources[0]['status']='unavailable'
        folder=self.app.project/'skill-hub/agents'
        if folder.is_dir():
            sources.append(self.source('hub-agent-toml',folder,'directory'))
            for path in list(folder.glob('*.toml'))[:200]:
                if path.is_symlink() or path.stat().st_size>100000: continue
                try: a=tomllib.loads(path.read_text('utf-8'))
                except (ValueError,OSError): continue
                items.append({'id':'hub:'+path.stem,'name':self.clean(a.get('name',path.stem)),'role':self.clean(a.get('description','')),'model':self.clean(a.get('model','')) or None,'runtime':'codex-portable','status':'catalogued','skills':None,'mcp':None,'permissions':None,'source':'hub-agent-toml','loaded':None})
        query=q.get('q','')[:200].lower(); items=[i for i in items if query in (i['name']+' '+i['role']).lower()]
        offset=max(0,min(100000,int(q.get('offset',0)))); limit=max(1,min(100,int(q.get('limit',50))))
        return {'items':items[offset:offset+limit],'total':len(items),'offset':offset,'limit':limit},sources
    def documents(self,q,detail=None):
        items=[]
        # Only shared documentation roots; no home directories, policies, test cases or artifacts.
        for root in (self.app.project/'skill-hub/library',self.app.project/'skill-hub/instructions',self.app.project/'workflow'):
            if not root.is_dir() or root.is_symlink(): continue
            visited=0
            for folder,dirs,files in os.walk(root,followlinks=False):
                visited+=1
                dirs[:]=sorted(d for d in dirs if not (Path(folder)/d).is_symlink())[:30] if visited<100 and len(Path(folder).relative_to(root).parts)<3 else []
                for filename in sorted(files)[:100]:
                    path=Path(folder)/filename
                    if path.suffix.lower()!='.md' or path.is_symlink() or re.search(r'(?i)policy|gold|evaluat|benchmark|secret|credential',filename): continue
                    if not path.resolve().is_relative_to(root.resolve()) or path.stat().st_size>262144: continue
                    ident='document:'+path.relative_to(self.app.project).as_posix()
                    if detail and detail!=ident: continue
                    text=path.read_text('utf-8',errors='replace')
                    if q.get('q','').lower() not in (filename+' '+text).lower(): continue
                    items.append({'id':ident,'name':filename,'kind':'document','domain':'instructions','version':self.app_hash(path.read_bytes()),'description':'Shared source document','body':self.clean(text) if detail else None,'active':True,'canonical_name':None,'tags':None,'updated_at':self.app.source(filename,path)['modified_at'],'editable':False})
                    if len(items)>=200: break
                if len(items)>=200: break
        if detail: return {'item':items[0] if items else None,'dependencies':None}
        offset=max(0,min(200,int(q.get('offset',0)))); limit=max(1,min(100,int(q.get('limit',50))))
        return {'items':items[offset:offset+limit],'total':len(items),'offset':offset,'limit':limit,'search_mode':'bounded document full-text substring','truncated':len(items)>=200}
    @staticmethod
    def app_hash(raw):
        import hashlib
        return hashlib.sha256(raw).hexdigest()
    def graph(self,q):
        path=self.app.project/GRAPH
        try: raw=read_json(path,24000000)
        except OSError:
            page=self.app.resources({'limit':'100'})
            raw={'nodes':{r['kind']+':'+r['id']:{'t':r['kind'],'n':r['name'],'ch':r['version']} for r in page['items']},'edges':[{'s':r['kind']+':'+r['id'],'d':r['kind']+':'+str(r['canonical_name']),'r':'alias_of','src':'catalog canonical_name'} for r in page['items'] if r['id']!=r['canonical_name']]}

        all_nodes={k:{'id':k,'type':v['t'],'label':self.clean(v.get('n',k)),'status':'projected','metadata':{'version':v.get('ch'),'observed_at':v.get('obs')}} for k,v in raw.get('nodes',{}).items() if v.get('t') in ('skill','prompt','agent','mcp','project')}
        edges=[{'id':str(i),'source':e['s'],'target':e['d'],'type':e['r'],'evidence':{'source':e.get('src'),'class':e.get('class'),'projection_built_at':raw.get('built_at')}} for i,e in enumerate(raw.get('edges',[])[:100000]) if e.get('s') in all_nodes and e.get('d') in all_nodes]
        agents,agent_sources=self.agents({'limit':'100'}); integrations,mcp_sources=self.integrations()
        for a in agents['items']:
            ident='agent:'+a['id']; all_nodes[ident]={'id':ident,'type':'agent','label':a['name'],'status':a['status'],'metadata':{'model':a['model'],'runtime':a['runtime']}}
            for skill in a.get('skills') or []:
                target='skill:'+skill
                if target in all_nodes: edges.append({'id':ident+'->'+target,'source':ident,'target':target,'type':'installed_skill','evidence':'native agent .agents/skills directory'})
        for m in integrations['items']:
            ident='mcp:'+m['id']; all_nodes[ident]={'id':ident,'type':'mcp','label':m['name'],'status':m['status'],'metadata':{'enabled':m.get('enabled')}}
        selected=q.get('node_id'); types=q.get('type','all'); query=q.get('q','').lower()[:200]
        limit=max(1,min(200,int(q.get('limit',100))))
        if selected:
            chosen={selected} if selected in all_nodes else set()
            for _ in range(max(0,min(3,int(q.get('depth',1))))):
                found={e[k] for e in edges if e['source'] in chosen or e['target'] in chosen for k in ('source','target')}
                chosen.update(sorted(found)[:200])
            candidates=[k for k in sorted(chosen) if k in all_nodes]
        else:
            # Prefer evidenced dependencies so initial graph has meaningful connected nodes.
            candidates=list(dict.fromkeys([e[k] for e in edges for k in ('source','target')]+sorted(all_nodes)))
        candidates=[k for k in candidates if (types=='all' or all_nodes[k]['type']==types) and query in all_nodes[k]['label'].lower()]
        chosen=set(candidates[:limit]); visible=[e for e in edges if e['source'] in chosen and e['target'] in chosen][:500]
        return {'nodes':[all_nodes[k] for k in candidates[:limit]],'edges':visible,'truncated':len(candidates)>limit or len(visible)==500,'total':len(candidates)},[self.source('capability-graph-projection',path)]+agent_sources+mcp_sources
    def _load_quality_dispositions(self):
        try:
            path=self.app.owned_target('quality-dispositions')
            if not path.is_file() or path.is_symlink(): return {}
            record=json.loads(path.read_text('utf-8'))
            payload=json.loads(record['content'])
            entries=payload.get('entries',{})
            return entries if isinstance(entries,dict) else {}
        except (OSError,ValueError,TypeError,AttributeError,KeyError):
            return {}

    def _scan_catalog_security(self):
        with self._quality_lock:
            return self._scan_catalog_security_locked()

    def _scan_catalog_security_locked(self):
        catalog_path=self.app.catalog
        st=catalog_path.stat()
        det_fp=_detector_fingerprint(self.clean)
        use_fast_hint=getattr(self.clean,'__name__','')=='public_text'
        wal=Path(str(catalog_path)+'-wal')
        wal_stat=wal.stat() if wal.exists() else None
        cache_key=(str(catalog_path),st.st_mtime_ns,st.st_size,
                   (wal_stat.st_mtime_ns,wal_stat.st_size) if wal_stat else None,det_fp,id(self.clean))
        if self._quality_cache and self._quality_cache[0]==cache_key:
            return self._quality_cache[1]
        base_items=[]; skills_scanned=0; prompts_scanned=0
        with closing(connect_ro(catalog_path)) as con:
            con.execute('BEGIN')
            for kind,table in (('skill','skills'),('prompt','prompts')):
                rows=con.execute(f'SELECT name, body_sha256, active, body FROM {table} ORDER BY name')
                for rid,db_sha,active,raw in rows:
                    if kind=='skill': skills_scanned+=1
                    else: prompts_scanned+=1
                    raw_body=raw or ''
                    is_active=bool(active) if active is not None else True
                    if not raw_body.strip():
                        base_items.append({
                            'id':rid,'kind':kind,'version':db_sha or EMPTY_BODY_SHA,
                            'content_sha256':EMPTY_BODY_SHA,'active':is_active,
                            'checks':['EMPTY_BODY'],'rule_signature':'EMPTY_BODY',
                            'sub_checks':('empty_body',),'credential_signals':(),
                            'evidence':[],'default_bucket':'data_quality',
                            'default_disposition':'unscannable_empty_body','status':'review'
                        })
                        continue
                    low=raw_body.lower()
                    if use_fast_hint and not _has_quality_hint(low):
                        base_items.append({
                            'id':rid,'kind':kind,'version':db_sha or '',
                            'content_sha256':None,'active':is_active,
                            'checks':[],'rule_signature':'NONE',
                            'sub_checks':(),'credential_signals':(),
                            'evidence':[],'default_bucket':'clean',
                            'default_disposition':'no_rule_hit','status':'no-rule-hit'
                        })
                        continue
                    clean_body=self.clean(raw_body)
                    rules=[]; sub_checks=[]; cred_signals=[]; evidence=[]
                    exec_match=_EXEC_SCAN_RE.search(clean_body)
                    if exec_match:
                        rules.append('EXECUTION_OR_INJECTION_REVIEW')
                        for sig,rx in _EXEC_SIGNAL_RES:
                            m=rx.search(clean_body)
                            if m:
                                sub_checks.append(sig)
                                if len(evidence)<3:
                                    evidence.append(_line_evidence(clean_body,m.start(),'EXECUTION_OR_INJECTION_REVIEW',sig,self.clean))
                    if '[redacted]' in clean_body.lower():
                        rules.append('REDACTED_CREDENTIAL_REVIEW')
                        if '[redacted]' in raw_body.lower(): cred_signals.append('source_literal_redacted')
                        if clean_body!=raw_body: cred_signals.append('redacted_by_sanitizer')
                        for csig,crx in _CRED_PREFIX_RES:
                            if crx.search(raw_body): cred_signals.append(csig)
                        red_pos=clean_body.lower().find('[redacted]')
                        if red_pos!=-1 and len(evidence)<3:
                            evidence.append(_line_evidence(clean_body,red_pos,'REDACTED_CREDENTIAL_REVIEW',cred_signals[0] if cred_signals else 'redacted_marker',self.clean))
                    csha=_sha256_text(raw_body) if rules else None
                    triage=credential_review(raw_body)
                    execution=execution_review(clean_body,_EXEC_SCAN_RE)
                    example_only=bool(rules and
                        ('REDACTED_CREDENTIAL_REVIEW' not in rules or triage['example_only']) and
                        (not exec_match or execution['example_only']))
                    triage={**triage,'example_only':example_only,'execution':execution}
                    base_items.append({
                        'id':rid,'kind':kind,'version':db_sha or csha or '',
                        'content_sha256':csha,'active':is_active,
                        'checks':rules,'rule_signature':'|'.join(sorted(rules)) if rules else 'NONE',
                        'sub_checks':tuple(sub_checks),'credential_signals':tuple(cred_signals),
                        'evidence':evidence,
                        'default_bucket':'resolved_example' if example_only else 'security_review' if rules else 'clean',
                        'default_disposition':'confirmed_example' if example_only else 'needs_source_review' if rules else 'no_rule_hit',
                        'status':'confirmed-example' if example_only else 'review' if rules else 'no-rule-hit',
                        'automatic_triage':triage
                    })
        scanned={'detector_fingerprint':det_fp,'skills_total':skills_scanned,'prompts_total':prompts_scanned,'items':tuple(base_items)}
        self._quality_cache=(cache_key,scanned)
        return scanned

    @staticmethod
    def _remediation_meta(item):
        kind=item['kind']; rid=item['id']; checks=item['checks']; disp=item['disposition']
        rev_name='revision-'+hashlib.sha256((kind+':'+rid).encode()).hexdigest()[:32]
        if checks==['EMPTY_BODY']:
            return {'quarantine_revision_name':rev_name,'suggested_action':'Verify source extraction/quarantine record; empty bodies cannot receive security PASS.'}
        if disp=='confirmed_example':
            if item.get('automatic_triage',{}).get('example_only'):
                return {'quarantine_revision_name':rev_name,'suggested_action':'Static syntax/context confirms detected example spans; masking retained. Content or detector changes rerun triage; this is not security acceptance.'}
            return {'quarantine_revision_name':rev_name,'suggested_action':'Version+rule-pinned as confirmed_example; content or rule changes automatically re-open review.'}
        if disp=='confirmed_finding':
            return {'quarantine_revision_name':rev_name,'suggested_action':'Stage sanitized replacement via resource.revise in quarantine before reactivation.'}
        if 'EXECUTION_OR_INJECTION_REVIEW' in checks and 'REDACTED_CREDENTIAL_REVIEW' in checks:
            return {'quarantine_revision_name':rev_name,'suggested_action':'Inspect execution/injection syntax and sanitized [REDACTED] line; stage resource.revise or pin disposition.'}
        if 'EXECUTION_OR_INJECTION_REVIEW' in checks:
            return {'quarantine_revision_name':rev_name,'suggested_action':'Check whether line is defensive/DFIR doc or unverified installer; replace curl|bash or encoded PowerShell.'}
        if 'REDACTED_CREDENTIAL_REVIEW' in checks:
            return {'quarantine_revision_name':rev_name,'suggested_action':'Confirm whether [REDACTED] masked prose/placeholder or an embedded key; revise in quarantine if real.'}
        return None

    def _catalog_curation(self,q):
        try:
            offset=max(0,min(100000,int(q.get('curation_offset',0))))
            limit=max(1,min(100,int(q.get('curation_limit',20))))
            query=str(q.get('curation_q',''))[:100].replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
            state=q.get('curation_state','all')
            states=('active_reviewed','active_repaired','active_admitted','active_review_pending','active_policy_blocked','quarantine_security','quarantine_structure','deleted_duplicate')
            if state!='all' and state not in states: raise ValueError('invalid state')
            where="name LIKE ? ESCAPE '\\'"; args=['%'+query+'%']
            if state!='all': where+=' AND state=?'; args.append(state)
            with closing(connect_ro(self.app.catalog)) as con:
                if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='catalog_curation'").fetchone(): return {'status':'unavailable','reason':'CURATION_TABLE_ABSENT','items':[]}
                counts={state:con.execute('SELECT count(*) FROM catalog_curation WHERE state=?',(state,)).fetchone()[0] for state in states}
                total=con.execute('SELECT count(*) FROM catalog_curation WHERE '+where,args).fetchone()[0]
                records=con.execute('SELECT kind,name,state,reason FROM catalog_curation WHERE '+where+' ORDER BY name,kind LIMIT ? OFFSET ?',args+[limit,offset]).fetchall()
            items=[{'kind':self.clean(str(r[0]))[:240],'id':self.clean(str(r[1]))[:240],'state':self.clean(str(r[2]))[:80],'reason':self.clean(str(r[3]))[:200]} for r in records]
            return {'status':'available','counts':counts,'items':items,'total':total,'offset':offset,'limit':limit}
        except (sqlite3.Error,OSError,ValueError,TypeError):
            return {'status':'unavailable','reason':'CURATION_TABLE_INVALID','items':[]}

    def quality(self,q):
        sec_kind=q.get('sec_kind','all')
        if sec_kind not in ('all','skill','prompt'): raise ValueError('invalid sec_kind')
        sec_bucket=q.get('sec_bucket','all')
        if sec_bucket not in ('all','security_review','data_quality','clean','needs_source_review','confirmed_finding','confirmed_example','resolved_example','unscannable_empty_body'): raise ValueError('invalid sec_bucket')
        sec_rule=q.get('sec_rule','all')
        if sec_rule not in ('all','EXECUTION_OR_INJECTION_REVIEW','REDACTED_CREDENTIAL_REVIEW','EMPTY_BODY'): raise ValueError('invalid sec_rule')
        sec_q=(q.get('sec_q') or '').strip().lower()[:100]
        sec_offset=max(0,min(100000,int(q.get('sec_offset',0))))
        sec_limit=max(1,min(500,int(q.get('sec_limit',200))))
        dup_offset=max(0,min(100000,int(q.get('offset',0))))

        scanned=self._scan_catalog_security()
        det_fp=scanned['detector_fingerprint']
        dispositions=self._load_quality_dispositions()
        bucket_counts=Counter(); disp_counts=Counter(); rule_counts=Counter()
        kind_hit_counts=Counter(); redaction_counts=Counter(); stale_invalidated=0
        resolved_all=[]

        for base in scanned['items']:
            bucket=base['default_bucket']; disp=base['default_disposition']; status=base['status']
            note=None; pinned_at=None; stale=False
            checks=base['checks']
            entry=dispositions.get(f"{base['kind']}:{base['id']}")
            if isinstance(entry,dict) and checks:
                if (
                    checks!=['EMPTY_BODY']
                    and entry.get('kind')==base['kind']
                    and entry.get('id')==base['id']
                    and entry.get('version')==base['version']
                    and entry.get('content_sha256')==base['content_sha256']
                    and entry.get('rule_signature')==base['rule_signature']
                    and entry.get('detector_fingerprint')==det_fp
                    and entry.get('disposition') in ('confirmed_finding','confirmed_example','needs_source_review')
                ):
                    disp=entry['disposition']; note=entry.get('note'); pinned_at=entry.get('updated_at')
                    if disp=='confirmed_example':
                        bucket='resolved_example'; status='confirmed-example'
                    else:
                        bucket='security_review'; status='review'
                else:
                    stale=True; stale_invalidated+=1

            bucket_counts[bucket]+=1; disp_counts[disp]+=1
            for r in checks: rule_counts[r]+=1
            if bucket=='security_review': kind_hit_counts[base['kind']]+=1
            for cs in base['credential_signals']: redaction_counts[cs]+=1

            item={
                'id':base['id'],'kind':base['kind'],'version':base['version'],
                'content_sha256':base['content_sha256'],'active':base['active'],
                'checks':checks,'rule_signature':base['rule_signature'],
                'detector_fingerprint':det_fp,
                'sub_checks':list(base['sub_checks']),'credential_signals':list(base['credential_signals']),
                'evidence':base['evidence'],'bucket':bucket,'disposition':disp,
                'status':status,'stale_disposition':stale
            }
            if base.get('automatic_triage'):
                item['automatic_triage']=base['automatic_triage']
            if note is not None: item['disposition_note']=note
            if pinned_at is not None: item['disposition_pinned_at']=pinned_at
            resolved_all.append(item)

        filtered=[]
        for item in resolved_all:
            if sec_kind!='all' and item['kind']!=sec_kind: continue
            if sec_bucket!='all':
                if sec_bucket in ('security_review','data_quality','clean','resolved_example') and item['bucket']!=sec_bucket: continue
                if sec_bucket in ('needs_source_review','confirmed_finding','confirmed_example','unscannable_empty_body') and item['disposition']!=sec_bucket: continue
            if sec_rule!='all' and sec_rule not in item['checks']: continue
            if sec_q and sec_q not in item['id'].lower() and sec_q not in item['kind']: continue
            filtered.append(item)

        def sort_key(it):
            d=it['disposition']; ch=it['checks']
            if d=='confirmed_finding': rank=0
            elif d=='needs_source_review' and 'EXECUTION_OR_INJECTION_REVIEW' in ch: rank=1
            elif d=='needs_source_review': rank=2
            elif d=='confirmed_example': rank=3
            elif d=='unscannable_empty_body': rank=4
            else: rank=5
            return (rank, 0 if it['active'] else 1, it['kind'], it['id'])

        filtered.sort(key=sort_key)
        paged=[]
        for it in filtered[sec_offset:sec_offset+sec_limit]:
            row=dict(it)
            if row['checks']:
                row['remediation']=self._remediation_meta(row)
            paged.append(row)
        findings=paged
        sec_meta={
            'skills_total':scanned['skills_total'],
            'prompts_total':scanned['prompts_total'],
            'total_resources':len(resolved_all),
            'filtered_total':len(filtered),
            'returned':len(paged),
            'offset':sec_offset,
            'limit':sec_limit,
            'stale_dispositions_invalidated':stale_invalidated,
            'summary':{
                'by_bucket':dict(bucket_counts),
                'by_disposition':dict(disp_counts),
                'by_rule':dict(rule_counts),
                'security_review_by_kind':dict(kind_hit_counts),
                'redaction_causes':dict(redaction_counts),
            }
        }
        path=self.app.project/AUDIT; benchmark=None; sources=[self.app.source('catalog',self.app.catalog)]
        try:
            audit=read_json(path,1000000); sources.append(self.source('historical-regression-audit',path))
            benchmark={k:(self.clean(audit[k][:2000]) if isinstance(audit.get(k),str) else audit.get(k) if audit.get(k) is None or type(audit.get(k)) in (int,float,bool) else None) for k in ('verified_at_utc','mode','verdict','cases','selection_recall','loaded_recall','candidate_recall','broad_relevance_precision','provenance_limit','runtime_limit')}
            benchmark['timing_ms']={k:v for k,v in audit.get('timing_ms',{}).items() if isinstance(v,(int,float))}
            benchmark['historical']=True
        except (OSError,ValueError): pass
        with closing(connect_ro(self.app.catalog)) as con:
            # Total non-empty duplicate groups (without LIMIT)
            total_dup_groups_row = con.execute(
                'SELECT count(*) FROM ('
                '  SELECT 1 FROM skill_resources '
                '  WHERE body_sha256 != ? AND body_sha256 IS NOT NULL AND body_sha256 != "" '
                '  GROUP BY body_sha256 HAVING count(*) > 1'
                ')',
                (EMPTY_BODY_SHA,)
            ).fetchone()
            total_dup_groups = total_dup_groups_row[0] if total_dup_groups_row else 0

            # Capped duplicate groups
            dup_rows = con.execute(
                'SELECT body_sha256, count(*) '
                'FROM skill_resources '
                'WHERE body_sha256 != ? AND body_sha256 IS NOT NULL AND body_sha256 != "" '
                'GROUP BY body_sha256 HAVING count(*) > 1 '
                'ORDER BY count(*) DESC LIMIT 100',
                (EMPTY_BODY_SHA,)
            ).fetchall()

            # Query members via structured query, preserving comma-containing names and stable id/kind
            dup_items = []
            for h, count in dup_rows:
                member_rows = con.execute(
                    'SELECT "skill" AS kind, name AS id, name, canonical_name, active '
                    'FROM skill_resources WHERE body_sha256 = ? ORDER BY name',
                    (h,)
                ).fetchall()
                members = [{'id': r[1], 'name': r[2], 'kind': r[0], 'canonical_name': r[3], 'active': r[4]} for r in member_rows]
                member_ids = [r['id'] for r in members]
                dup_items.append({
                    'sha256': h,
                    'count': count,
                    'type': 'exact-body-duplicate',
                    'members': members,
                    'resources': member_ids,
                    'member_ids': member_ids,
                })

            # Separate known normalized empty body from unknown/missing hash
            known_empty_count = con.execute(
                'SELECT count(*) FROM skill_resources WHERE body_sha256 = ?',
                (EMPTY_BODY_SHA,)
            ).fetchone()[0]

            missing_hash_count = con.execute(
                'SELECT count(*) FROM skill_resources WHERE body_sha256 IS NULL OR body_sha256 = ""'
            ).fetchone()[0]

            # Count quarantine intersection strictly for matching empty resources (distinct names)
            has_quarantine_table = False
            try:
                con.execute('SELECT 1 FROM quarantine LIMIT 1')
                has_quarantine_table = True
            except (sqlite3.OperationalError, sqlite3.DatabaseError):
                has_quarantine_table = False

            quarantined_empty_intersection = 0
            quarantined_reasons = {}
            if has_quarantine_table:
                q_inter_row = con.execute(
                    'SELECT count(DISTINCT s.name) '
                    'FROM skill_resources s '
                    'JOIN quarantine q ON q.name = s.name AND q.kind = "skill" '
                    'WHERE s.body_sha256 = ?',
                    (EMPTY_BODY_SHA,)
                ).fetchone()
                quarantined_empty_intersection = q_inter_row[0] if q_inter_row else 0

                q_reasons = con.execute(
                    'SELECT q.reason, count(DISTINCT s.name) '
                    'FROM skill_resources s '
                    'JOIN quarantine q ON q.name = s.name AND q.kind = "skill" '
                    'WHERE s.body_sha256 = ? '
                    'GROUP BY q.reason ORDER BY count(DISTINCT s.name) DESC LIMIT 5',
                    (EMPTY_BODY_SHA,)
                ).fetchall()
                quarantined_reasons = {r[0]: r[1] for r in q_reasons}

            non_quarantined_empty_count = max(0, known_empty_count - quarantined_empty_intersection)

            if has_quarantine_table:
                sample_rows = con.execute(
                    'SELECT s.name, q.reason '
                    'FROM skill_resources s '
                    'LEFT JOIN quarantine q ON q.name = s.name AND q.kind = "skill" '
                    'WHERE s.body_sha256 = ? '
                    'GROUP BY s.name LIMIT 20',
                    (EMPTY_BODY_SHA,)
                ).fetchall()
                empty_samples = [{'id': r[0], 'name': r[0], 'kind': 'skill', 'quarantine_reason': r[1]} for r in sample_rows]
            else:
                sample_rows = con.execute(
                    'SELECT name FROM skill_resources WHERE body_sha256 = ? LIMIT 20',
                    (EMPTY_BODY_SHA,)
                ).fetchall()
                empty_samples = [{'id': r[0], 'name': r[0], 'kind': 'skill', 'quarantine_reason': None} for r in sample_rows]

            empty_sample_names = [s['id'] for s in empty_samples]

        desc_parts = [f'{known_empty_count} tài nguyên trong catalog chưa được trích xuất phần thân (catalog body unextracted, mã băm SHA256 {EMPTY_BODY_SHA[:8]}...).']
        if quarantined_empty_intersection:
            desc_parts.append(f'Trong đó có {quarantined_empty_intersection} tài nguyên đã xác minh có bản ghi cách ly (quarantine).')
        if non_quarantined_empty_count:
            desc_parts.append(f'Có {non_quarantined_empty_count} tài nguyên thân rỗng không có bản ghi cách ly.')
        if missing_hash_count:
            desc_parts.append(f'Phát hiện thêm {missing_hash_count} tài nguyên thiếu mã băm (NULL/rỗng).')
        desc_parts.append('Tất cả được phân loại riêng, không coi là trùng lặp mã nguồn và không tự ý suy diễn là file nguồn trên đĩa bị rỗng.')

        empty_info = {
            'count': known_empty_count,
            'sha256': EMPTY_BODY_SHA,
            'type': 'catalog-body-unextracted',
            'status': 'unextracted_empty_body',
            'quarantined_intersection_count': quarantined_empty_intersection,
            'non_quarantined_empty_count': non_quarantined_empty_count,
            'missing_or_null_hash_count': missing_hash_count,
            'top_verified_reasons': quarantined_reasons,
            'sample_resources': empty_sample_names,
            'samples': empty_samples,
            'description': ' '.join(desc_parts)
        }

        return {
            'items': dup_items,
            'total': total_dup_groups,
            'limit': 100,
            'truncated': total_dup_groups > len(dup_items),
            'offset': dup_offset,
            'empty_bodies': empty_info,
            'security_scan': {
                'catalog_curation':self._catalog_curation(q),
                'original_sources':self._original_security_review(q),
                'items': findings,
                'total': sec_meta['total_resources'],
                'scanned_in_page': sec_meta['total_resources'],
                'detector_fingerprint': det_fp,
                'coverage': {
                    'mode': 'full-catalog',
                    'skills_total': sec_meta['skills_total'],
                    'skills_scanned': sec_meta['skills_total'],
                    'prompts_total': sec_meta['prompts_total'],
                    'prompts_scanned': sec_meta['prompts_total'],
                    'resources_total': sec_meta['total_resources'],
                    'resources_scanned': sec_meta['total_resources'],
                    'filtered_total': sec_meta['filtered_total'],
                    'returned': sec_meta['returned'],
                    'offset': sec_meta['offset'],
                    'limit': sec_meta['limit'],
                    'truncated': sec_meta['filtered_total'] > (sec_meta['offset'] + sec_meta['returned']),
                    'stale_dispositions_invalidated': sec_meta['stale_dispositions_invalidated'],
                },
                'summary': sec_meta['summary'],
                'scope': 'static rules across full normalized catalog (skills + prompts); frontmatter not assessed; empty bodies separated into data_quality (no false security pass); redacted markers require source review; not security acceptance'
            },
            'benchmark': benchmark,
            'accuracy': None
        }, sources
