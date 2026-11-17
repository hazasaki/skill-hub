"""Local management console: read-only live sources, isolated versioned intake."""
import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from urllib.parse import parse_qs, unquote, urlsplit
import batch_intake
from sources import Sources
from catalog_quality_read import read_receipt as read_catalog_quality
from observer_read import connect_ro, read_events, group_routes
from credential_mask import mask_credentials

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent.parent

def now():
    return datetime.now(timezone.utc).isoformat()

def digest(value):
    return hashlib.sha256(value).hexdigest()

def public_text(value):
    # Resource text is untrusted; credential-looking material never leaves this API.
    return mask_credentials(value)

class Dashboard:
    def __init__(self, project=PROJECT, state=None):
        self.project = Path(project)
        self.catalog = self.project/'skill-hub/state/skill-index.db'
        self.telemetry = {'native': self.project/'skill-hub/state/telemetry/hub-telemetry.db', 'docker': self.project/'state/docker-skill-hub/telemetry/hub-telemetry.db'}
        self.state = Path(state or self.project/'state/hub-dashboard')
        self.csrf = secrets.token_urlsafe(32)
        self.plans = {}
        self.lock = threading.Lock()
        self.check_lock = threading.Lock()
        self.sources = Sources(self,public_text)

    def source(self, name, path):
        exists = path.is_file()
        return {'type': 'sqlite', 'name': name, 'observed_at': now(), 'status': 'readable' if exists else 'unavailable', 'modified_at': datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat() if exists else None, 'freshness': ('stale' if time.time()-path.stat().st_mtime>300 else 'recent') if exists else 'unknown'}

    def resources(self, q, detail=None):
        kind = q.get('kind', 'all')
        if kind=='document': return self.sources.documents(q,detail)
        if q.get('source')=='quarantine':
            if not detail: raise ValueError('quarantine detail needs identity')
            path=self.target(kind,detail)
            record=json.loads(path.read_text('utf-8')) if path.exists() else None
            return {'item':{'id':detail,'name':detail,'kind':kind,'body':public_text(record['content']),'version':public_text(record.get('version','')),'sha256':digest(path.read_bytes()),'status':'quarantined'} if record else None}

        if kind not in ('all', 'skill', 'prompt'): raise ValueError('invalid kind')
        offset = max(0, min(100000, int(q.get('offset', 0))))
        limit = max(1, min(100, int(q.get('limit', 50))))
        query = q.get('q', '')[:200]
        domain = q.get('domain', '')[:100]
        pieces, args = [], []
        for k, table in [('skill', 'skills'), ('prompt', 'prompts')]:
            if kind not in ('all', k): continue
            where, params = [], []
            if detail:
                where.append('name=?'); params.append(detail)
            if domain:
                where.append('category_canonical=?'); params.append(domain)
            if query:
                # Literal bounded full-text substring search, including stored body, no FTS operators.
                where.append('rowid IN (SELECT rowid FROM '+('fts' if k=='skill' else 'fts_prompts')+' WHERE '+('fts' if k=='skill' else 'fts_prompts')+' MATCH ?)'); params.append(chr(34)+query.replace(chr(34),chr(34)*2)+chr(34))
            columns = 'body' if detail else 'NULL AS body'
            pieces.append(f"SELECT '{k}' AS kind,name AS id,name,description,tags,category_canonical AS domain,active,body_sha256 AS version,canonical_name,{columns} FROM {table}" + (' WHERE '+' AND '.join(where) if where else ''))
            args.extend(params)
        sql = ' UNION ALL '.join(pieces)
        with closing(connect_ro(self.catalog)) as con:
            con.row_factory = sqlite3.Row
            con.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10000)
            deadline = time.monotonic()+2
            total = con.execute('SELECT count(*) FROM ('+sql+')', args).fetchone()[0]
            rows = con.execute(sql+' ORDER BY name,kind LIMIT ? OFFSET ?', args+[limit,offset]).fetchall()
        items = [{k: public_text(v)[:262144] if isinstance(v,str) else v for k,v in dict(r).items()} for r in rows]
        if detail:
            item=items[0] if items else None
            if item:
                name='revision-'+digest((item['kind']+':'+item['id']).encode())[:32]
                target=self.target(item['kind'],name)
                item['revision']={'operation':'resource.revise','name':name,'expected_sha256':digest(target.read_bytes() if target.exists() else b''),'source':{'kind':item['kind'],'id':item['id'],'version':item['version']}}
            return {'item':item,'body_limit':262144,'dependencies':None}

        return {'items': items, 'total': total, 'offset': offset, 'limit': limit, 'search_mode': 'full-text FTS literal phrase' if query else 'catalog'}

    def events(self):
        items, sources = [], []
        for name,path in self.telemetry.items():
            source = self.source(name,path)
            try:
                events,rejected = read_events(path)
                source['rejected_events'] = rejected
                items.extend(dict(e, source=name) for e in events)
            except (OSError, sqlite3.Error, ValueError): source['status']='unavailable'
            sources.append(source)
        return sorted(items,key=lambda e:e['emitted_at'],reverse=True)[:1200],sources

    def json_source(self, name):
        path = self.project/'skill-hub/state'/name
        source = self.source(name,path); source['type']='json'
        if not path.is_file() or path.stat().st_size>4000000: raise OSError('source unavailable')
        return json.loads(path.read_text(encoding='utf-8-sig')),source

    def quarantine(self):
        items=[]
        base=self.state/'quarantine'
        if not base.exists(): return items
        for kind in ('skill','prompt'):
            folder=base/kind
            if not folder.is_dir() or folder.is_symlink(): continue
            for entry in folder.iterdir():
                if len(items)>=100: return items
                if not entry.is_dir() or entry.is_symlink(): continue
                path=self.target(kind,entry.name)
                if not path.exists() or path.is_symlink(): continue
                raw=path.read_bytes()
                record=json.loads(raw)
                versions=entry/'versions'
                backups=[] if versions.is_symlink() or not versions.exists() else [p.stem for p in versions.iterdir() if re.fullmatch('[a-f0-9]{64}\\.json',p.name) and not p.is_symlink()][:100]
                items.append({'kind':kind,'name':entry.name,'sha256':digest(raw),'version':record['version'],'backups':backups,'status':'quarantined'})
        return items

    def owned_target(self,name):
        if name not in ('settings','source-snapshot','quality-dispositions','original-dispositions'): raise ValueError('unknown dashboard-owned config')
        path=self.state/'managed'/name/'current.json'
        if path.is_symlink() or not path.resolve().is_relative_to(self.state.resolve()) or any(p.is_symlink() for p in path.parents if p.exists()): raise ValueError('unsafe state path')
        return path

    def settings(self):
        path=self.owned_target('settings')
        default={'resource_limit':50,'graph_limit':100,'routing_source':'all'}
        if path.exists(): default.update(json.loads(json.loads(path.read_text('utf-8'))['content']))
        return self.validate_settings(default)

    @staticmethod
    def validate_settings(value):
        if not isinstance(value,dict) or set(value)!={'resource_limit','graph_limit','routing_source'}: raise ValueError('settings keys not allowed')
        for key,maximum in (('resource_limit',100),('graph_limit',200)):
            if type(value[key]) is not int or not 1<=value[key]<=maximum: raise ValueError('setting out of bounds')
        if value['routing_source'] not in ('all','native','docker'): raise ValueError('invalid routing source')
        return value

    def owned_preview(self,body):
        op=body['operation']
        if op=='quality.disposition': name='quality-dispositions'
        elif op=='original.disposition_batch': name='original-dispositions'
        elif op=='config.snapshot': name='source-snapshot'
        else: name='settings'
        path=self.owned_target(name); before=path.read_bytes() if path.exists() else b''
        if body.get('expected_sha256')!=digest(before): raise ValueError('source hash precondition failed')
        plan_source=None
        if op=='config.apply': content=self.validate_settings(body.get('settings'))
        elif op=='config.rollback':
            version=body.get('version','')
            if not isinstance(version,str) or not re.fullmatch('[a-f0-9]{64}',version): raise ValueError('invalid version')
            backup=path.parent/'versions'/(version+'.json')
            if backup.is_symlink() or backup.parent.is_symlink(): raise ValueError('unsafe backup')
            raw=self.backup_bytes(backup,version)
            content=self.validate_settings(json.loads(json.loads(raw)['content']))
        elif op=='config.snapshot':
            agents,ags=self.sources.agents({'limit':'100'}); integrations,ms=self.sources.integrations()
            content={'observed_at':now(),'agents':agents,'integrations':integrations,'sources':ags+ms,'limits':['Read-only configured source snapshot, not host sync or runtime activation']}
        elif op=='original.disposition_batch':
            requested=body.get('decisions')
            if not isinstance(requested,list) or not 1<=len(requested)<=100: raise ValueError('decisions must contain 1..100 entries')
            rows=[]
            for offset in range(0,20000,500):
                page=self.sources._original_security_review({'original_offset':offset,'original_limit':500})
                if page.get('status')!='verified-receipt': raise ValueError('verified original receipt required')
                rows.extend(page['items'])
                if not page['coverage']['truncated']: break
            lookup={(r['kind'],r['id']):r for r in rows}
            hashes=('source_file_sha256','source_content_sha256','catalog_body_sha256','catalog_content_sha256')
            entries=dict(self.sources._load_original_decisions(include_receipt=False))
            seen=set()
            for decision in requested:
                fields={'kind','id','outcome','reason','evidence_lines',*hashes}
                if not isinstance(decision,dict) or set(decision)!=fields: raise ValueError('invalid decision fields')
                if not isinstance(decision['kind'],str) or not isinstance(decision['id'],str): raise ValueError('invalid identity')
                identity=(decision['kind'],decision['id'])
                if identity in seen or identity not in lookup: raise ValueError('duplicate or unknown decision')
                seen.add(identity); row=lookup[identity]
                if row['source_status']!='READ' or not row['content_matches_catalog'] or not all(row.get(k) and decision[k]==row[k] for k in hashes): raise ValueError('stale source or body hash')
                if decision['outcome'] not in ('approved','documented_example','prerequisite','restricted') or decision['reason'] not in row['reasons']: raise ValueError('invalid decision or reason')
                evidence=decision['evidence_lines']
                if not isinstance(evidence,list) or not evidence or any(type(x) is not int for x in evidence) or not set(evidence)<=set(row['evidence_lines']): raise ValueError('invalid evidence lines')
                entries[f"{identity[0]}:{identity[1]}"]=decision
            content={'schema':'skill-hub.original-decisions.v1','entries':entries}
        elif op=='quality.disposition':
            source=body.get('source')
            if not isinstance(source,dict): raise ValueError('catalog source required')
            self.check_catalog_source(source)
            disp=body.get('disposition')
            if disp not in ('confirmed_finding','confirmed_example','needs_source_review'): raise ValueError('invalid disposition')
            rule_sig=body.get('rule_signature')
            if not isinstance(rule_sig,str) or not rule_sig or rule_sig in ('NONE','EMPTY_BODY'): raise ValueError('invalid rule_signature')
            note=body.get('note')
            if note is not None:
                if not isinstance(note,str) or len(note)>300 or '\x00' in note or public_text(note)!=note: raise ValueError('invalid or credential-looking note')
            scanned=self.sources._scan_catalog_security()
            det_fp=scanned['detector_fingerprint']
            target_item=next((x for x in scanned['items'] if x['kind']==source['kind'] and x['id']==source['id']),None)
            if not target_item or not target_item['checks'] or target_item['checks']==['EMPTY_BODY']: raise ValueError('item not eligible for security disposition')
            if target_item['rule_signature']!=rule_sig or target_item['version']!=source.get('version'): raise ValueError('stale rule signature or catalog version')
            if body.get('detector_fingerprint') is not None and body.get('detector_fingerprint')!=det_fp: raise ValueError('stale detector fingerprint')
            existing={}
            if before:
                existing=json.loads(json.loads(before.decode('utf-8'))['content']).get('entries',{})
                if not isinstance(existing,dict): existing={}
            entries=dict(existing)
            entries[f"{target_item['kind']}:{target_item['id']}"]={
                'kind':target_item['kind'],
                'id':target_item['id'],
                'version':target_item['version'],
                'content_sha256':target_item['content_sha256'],
                'rule_signature':target_item['rule_signature'],
                'detector_fingerprint':det_fp,
                'disposition':disp,
                'note':note,
                'updated_at':now()
            }
            content={'schema':'skill-hub.dashboard.dispositions.v1','entries':entries}
            plan_source=source
        else: raise ValueError('unsupported config operation')
        record={'name':name,'kind':'config','content':json.dumps(content,ensure_ascii=False),'version':digest(json.dumps(content,sort_keys=True).encode()),'status':'dashboard-owned'}
        token=secrets.token_urlsafe(24)
        self.plans={k:v for k,v in self.plans.items() if v['expires']>time.time()}
        if len(self.plans)>=100: raise ValueError('too many plans')
        self.plans[token]={'expires':time.time()+300,'path':path,'before':before,'source':plan_source,'owner':'dashboard','after':json.dumps(record,ensure_ascii=False).encode()}
        if op=='original.disposition_batch': self.plans[token]['original_receipt_sha256']=page['receipt_sha256']
        rollback_msg=('Dispositions automatically invalidate when resource version/content or rule signature changes' if op=='quality.disposition' else 'config.rollback restores prior settings by backup hash; snapshot is read-only source export')
        return {'plan_id':token,'expires_in':300,'changes':content,'expected_sha256':digest(before),'backup':digest(before) if before else None,'ownership':'dashboard-owned '+name,'rollback':rollback_msg}

    def owned_config(self):
        result={'settings':self.settings()}
        for name in ('settings','source-snapshot'):
            path=self.owned_target(name); raw=path.read_bytes() if path.exists() else b''
            versions=path.parent/'versions'
            backups=[] if not versions.is_dir() or versions.is_symlink() else [p.stem for p in versions.iterdir() if re.fullmatch('[a-f0-9]{64}\\.json',p.name) and not p.is_symlink()][:100]
            result[name+'_state']={'sha256':digest(raw),'backups':backups,'present':bool(raw)}
        return result

    def capabilities(self):
        supported = ['resource.import_batch', 'resource.import', 'resource.update', 'resource.rollback', 'resource.revise', 'config.apply', 'config.rollback', 'config.snapshot', 'quality.disposition', 'mcp.health', 'resource.search', 'resource.detail']
        unsupported = {
            'agent.edit': ('Native owner adapter not implemented', 'native-write'),
            'mcp.edit': ('Native owner adapter not implemented; protected OpenBot settings require native host API', 'native-write'),
            'sync.apply': ('Runtime sync adapter not implemented', 'native-write'),
            'quality.run': ('Static scan and historical readback supported; model benchmark reruns require separate authorization', 'native-write'),
            'resource.promote': ('Quarantine review/promotion adapter not implemented', 'native-write'),
        }
        ops = [{'operation':x,'supported':True,'reason':('Dashboard-owned version-pinned quality disposition only' if x=='quality.disposition' else 'Dashboard-owned settings/snapshot only' if x.startswith('config.') else 'Fixed allowlisted read' if x in ('mcp.health','resource.search','resource.detail') else 'Versioned quarantine only; no runtime promotion'),'risk':'read-only' if x in ('mcp.health','resource.search','resource.detail') else 'isolated-write'} for x in supported]

        sem_status = False
        sem_reason = 'Semantic query adapter not connected; prioritize dGPU offload to reduce RAM, CPU/RAM coordination allowed'
        try:
            h = self.sources.health('skill-hub')
            if h.get('status') == 'observed':
                fields = h.get('fields', {})
                warmup = fields.get('warmup', {})
                if warmup.get('ok'):
                    sem_status = True
                    if warmup.get('gpu_resident'):
                        sem_reason = 'Live dGPU semantic retrieval connected; BGE-M3 offloaded to VRAM with bounded pipeline'
                    else:
                        sem_reason = 'Live semantic retrieval connected; shared backend query ready (CPU/RAM coordinated pipeline, dGPU preferred)'
                else:
                    sem_reason = 'Backend semantic engine warmup incomplete or index unavailable; shared retrieval adapter required'
            elif h.get('status') == 'degraded':
                sem_reason = 'Backend semantic service degraded; shared retrieval adapter required'
            else:
                sem_reason = 'Backend skill-hub server offline or unreachable; shared retrieval adapter required'
        except Exception:
            sem_reason = 'Semantic query adapter error checking backend health'

        ops.append({
            'operation': 'semantic.search',
            'supported': sem_status,
            'reason': sem_reason,
            'risk': 'read-only'
        })

        route_status = False
        route_reason = 'Skill Hub router backend unavailable or warming up on port 8765'
        try:
            if h.get('status') == 'observed' and h.get('http_status') == 200:
                fields = h.get('fields', {})
                warmup = fields.get('warmup', {})
                if warmup.get('ok'):
                    route_status = True
                    route_reason = 'Interactive route_task tester connected to live Skill Hub router (TEMPR + Elephant decision model, explicit operator trigger)'
        except Exception:
            route_reason = 'Skill Hub router backend unavailable or warming up on port 8765'

        ops.append({
            'operation': 'route.task',
            'supported': route_status,
            'reason': route_reason,
            'risk': 'interactive-model'
        })

        for k, (reason, risk) in unsupported.items():
            ops.append({'operation': k, 'supported': False, 'reason': reason, 'risk': risk})

        return ops

    def get(self, module, q):
        q=dict(q)
        settings=self.settings()
        if 'limit' not in q: q['limit']=str(settings['graph_limit'] if module=='graph' else settings['resource_limit'])
        sources=[self.source('catalog',self.catalog)]
        limits=['Unknown host bindings, tokens and cost remain null.', 'Catalog bodies capped at 262144 characters; page 100; query deadline 2 seconds.']
        status='ok'
        try:
            if module == 'intake':
                data = batch_intake.listing(self, q)
            elif module == 'intake/export':
                data = (batch_intake.export_batch(self, q['batch_id'], public_text) if 'batch_id' in q
                        else batch_intake.export(self, q.get('kind'), q.get('name'), public_text))
            elif module.startswith('resources/'):
                data=self.resources(q,unquote(module.split('/',1)[1]))
            elif module in ('resources','library'):
                data=self.resources(dict(q,kind=q.get('kind','prompt')) if module=='library' else q)
            elif module in ('routing','telemetry'):
                events,sources=self.events()
                source_filter=q.get('source',settings['routing_source'])
                if source_filter not in ('all','native','docker'): raise ValueError('invalid source filter')
                if source_filter!='all': events=[e for e in events if e['source']==source_filter]
                data={'items':[dict(r,source=source) for source in self.telemetry for r in group_routes([e for e in reversed(events) if e['source']==source])] if module=='routing' else events, 'total':len(events),'offset':0,'limit':1200,'tokens':None,'cost_usd':None,'window':'latest 1200 events per source before merged cap'}
                limits.append('Producer does not record confidence, candidates or should_use_skill; values are unknown. Attestation is self-reported.')
            elif module=='overview':
                with closing(connect_ro(self.catalog)) as con:
                    counts={k:con.execute('SELECT count(*) FROM '+t).fetchone()[0] for k,t in [('skills','skill_resources'),('prompts','prompt_resources')]}
                events,ts=self.events(); sources+=ts
                agents,ags=self.sources.agents({'limit':'1'}); integrations,ms=self.sources.integrations(); sources+=ags+ms
                try: plugins,ps=self.sources.plugins(); sources.append(ps)
                except (OSError,ValueError): plugins=None
                counts.update(agents=agents['total'] if any(x['status']=='readable' for x in ags) else None,mcp=integrations['configured_count'] if ms[0]['status']=='readable' else None,plugins=len(plugins) if plugins is not None else None)

                usage=Counter((e['source'],e['emitted_at'][:10]) for e in events)
                data={'counts':counts,'recent_activity':events[:20],'usage':[{'source':s,'date':d,'events':v} for (s,d),v in sorted(usage.items())]}
            elif module=='graph':
                data,sources=self.sources.graph(q)
                status='partial'
                limits.append('Graph projection and native installed-skill evidence only. External concept/tool nodes excluded; no fabricated project or runtime grants.')
            elif module=='catalog-quality':
                receipt = getattr(self, 'catalog_quality_receipt', self.catalog.parent/'catalog-quality.json')
                data = read_catalog_quality(self.catalog, receipt, q, public_text)
                status = 'partial'
                limits.append('Source review is a pinned historical observation; no activation or permission change.')
            elif module=='quality':
                data,sources=self.sources.quality(q); status='partial'
                limits.append('Static full-catalog scan (skills + prompts) and historical aggregates only; no paid rerun or security acceptance.')
            elif module=='config':
                live_cfg=None
                try:
                    h=self.sources.health('skill-hub')
                    if h.get('status') in ('observed','degraded'):
                        f=h.get('fields',{})
                        live_cfg={
                            'target':'skill-hub',
                            'status':h.get('status'),
                            'http_status':h.get('http_status'),
                            'observed_at':h.get('observed_at'),
                            'catalog_skills':f.get('catalog_skills'),
                            'catalog_sha256':f.get('catalog_sha256'),
                            'effective_catalog_sha256':f.get('effective_catalog_sha256'),
                            'publication':f.get('publication'),
                            'projection_scope':f.get('projection_scope'),
                            'warmup':f.get('warmup'),
                            'freshness':'live'
                        }
                    else:
                        live_cfg={
                            'target':'skill-hub',
                            'status':'unavailable',
                            'observed_at':h.get('observed_at'),
                            'freshness':'stale'
                        }
                except Exception:
                    live_cfg={'target':'skill-hub','status':'unavailable','freshness':'stale'}
                data={'capabilities':self.capabilities(),'csrf_token':self.csrf,'port':8769,'ownership':'dashboard quarantine only','live_configuration':live_cfg,'quarantine':self.quarantine(),'sync':[],'owned':self.owned_config()}
                for name in ('sync-status.json','hook-sync-status.json','autosync.json'):
                    try:
                        raw,source=self.json_source(name); sources.append(source)
                        data['sync'].append({k:raw.get(k) for k in ('schema','ok','dry_run','generated_at','counts','runtime_verified','enabled','running','cycles')})
                    except (OSError,ValueError): limits.append(name+' unavailable')
                status='partial'
                limits.append('Sync snapshots are recorded reconciliation results, not current loaded host configuration.')
            elif module=='semantic':
                data=self.sources.semantic_search(q.get('q',''),q.get('limit',10))
                if data.get('status')!='ok': status='partial'
                limits.append('Semantic search delegates to live local dGPU BGE-M3 model on loopback 8765; no paid model calls.')
            elif module=='integrations':
                data,sources=self.sources.integrations(q.get('probe'))
                status='partial'; limits.append('Projection is configured state; tool catalogs are snapshots. Explicit probe=skill-hub checks only the fixed loopback health endpoint.')
            elif module=='agents':
                data,sources=self.sources.agents(q)
                status='partial' if any(x['status']=='readable' for x in sources) else 'unavailable'
                limits.append('Configured profiles and installed skill directories; global enabled MCP availability is not per-agent runtime loading. No fabricated heartbeat.')
            else: raise KeyError(module)
        except (OSError,sqlite3.Error,json.JSONDecodeError):
            data={'items':[],'total':None}; status='unavailable'; limits.append('Source absent, unreadable, incompatible or query deadline exceeded.')
        if any(s['status']=='unavailable' for s in sources) and status=='ok': status='partial'
        return {'schema':'skill-hub.dashboard.v1','observed_at':now(),'status':status,'data':data,'sources':sources,'limits':limits}

    def retrieval_check(self):
        """Exercise the current local semantic service on fixed, inspectable cases."""
        cases = (
            ('Review a small code change', ('review-checklist',)),
        )
        if not self.check_lock.acquire(blocking=False):
            return {'status': 'busy', 'cases': []}
        try:
            results = []
            for query, expected in cases:
                started = time.monotonic()
                payload = self.sources.semantic_search(query, 5)
                hits = [item['id'] for item in payload.get('hits', [])]
                families = {name.split(':', 1)[1].split('--', 1)[0] for name in hits if name.startswith('skill:')}
                matched = any(name in families for name in expected)
                status = ('matched' if matched else 'miss') if payload.get('status') == 'ok' else 'unavailable'
                results.append({'query': query, 'expected_skill_families': expected, 'hits': hits,
                                'status': status, 'elapsed_ms': round((time.monotonic()-started)*1000, 1)})
            return {'status': 'ok' if all(r['status']=='matched' for r in results) else 'partial',
                    'observed_at': now(), 'scope': 'local_semantic_top5', 'cases': results}
        finally:
            self.check_lock.release()

    def route_task(self, payload):
        if not isinstance(payload, dict):
            raise ValueError('object required')
        state = payload.get('state')
        retrieval_mode = payload.get('retrieval_mode', 'tempr')
        include_graph = bool(payload.get('include_graph', False))
        task_id = payload.get('task_id')
        data, sources = self.sources.route_task(
            state=state,
            retrieval_mode=retrieval_mode,
            include_graph=include_graph,
            task_id=task_id
        )
        status = 'ok' if data.get('status') == 'ok' else 'unavailable'
        limits = [
            'Interactive route_task calls the configured model provider and can incur charges.'
        ]
        return {
            'schema': 'skill-hub.dashboard.v1',
            'observed_at': now(),
            'status': status,
            'data': data,
            'sources': sources,
            'limits': limits
        }

    def target(self, kind, name):
        if kind not in ('skill','prompt') or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}',name): raise ValueError('invalid resource identity')
        path=self.state/'quarantine'/kind/name/'current.json'
        if path.is_symlink() or not path.resolve().is_relative_to(self.state.resolve()) or any(p.is_symlink() for p in [self.state,*path.parents] if p.exists()): raise ValueError('unsafe state path')
        return path

    @staticmethod
    def backup_bytes(path,version):
        if path.is_symlink() or path.parent.is_symlink() or path.stat().st_size>4000000: raise ValueError('unsafe backup')
        raw=path.read_bytes()
        if digest(raw)!=version: raise ValueError('backup hash mismatch')
        return raw

    def check_catalog_source(self, source):
        if source.get('kind') not in ('skill','prompt') or not isinstance(source.get('id'),str): raise ValueError('invalid catalog source')
        table='skill_resources' if source['kind']=='skill' else 'prompt_resources'
        with closing(connect_ro(self.catalog)) as con:
            row=con.execute('SELECT body_sha256 FROM '+table+' WHERE name=?',(source['id'],)).fetchone()
        if not row or row[0]!=source.get('version'): raise ValueError('catalog source changed')

    def operation(self, stage, body):
        with self.lock:
            if stage=='preview':
                if body.get('operation') == 'resource.import_batch': return batch_intake.preview(self, body, public_text)
                if body.get('operation') in ('config.apply','config.rollback','config.snapshot','quality.disposition','original.disposition_batch'): return self.owned_preview(body)
                op=body.get('operation'); kind=body.get('kind'); name=body.get('name'); path=self.target(kind,name)
                before=path.read_bytes() if path.exists() else b''
                expected=body.get('expected_sha256')
                if expected!=digest(before): raise ValueError('source hash precondition failed')
                if op not in ('resource.import','resource.update','resource.rollback','resource.revise'): raise ValueError('unsupported operation')
                if op=='resource.import' and before: raise ValueError('already exists')
                if op in ('resource.update','resource.rollback') and not before: raise ValueError('resource missing')
                source=body.get('source') if op=='resource.revise' else None
                if source is not None:
                    if not isinstance(source,dict) or source.get('kind')!=kind: raise ValueError('invalid source')
                    self.check_catalog_source(source)
                    if name!='revision-'+digest((kind+':'+source['id']).encode())[:32]: raise ValueError('invalid revision identity')
                elif op=='resource.revise': raise ValueError('catalog source required')
                if op=='resource.rollback':
                    version=body.get('version','')
                    if not re.fullmatch('[a-f0-9]{64}',version): raise ValueError('invalid backup version')
                    backup=path.parent/'versions'/f'{version}.json'
                    if backup.is_symlink() or backup.parent.is_symlink(): raise ValueError('unsafe backup')
                    content=json.loads(self.backup_bytes(backup,version))['content']
                else: content=body.get('content')
                if not isinstance(content,str) or not 1<=len(content.encode())<=262144: raise ValueError('content must be 1..262144 bytes')
                if '\x00' in content or public_text(content)!=content: raise ValueError('credential-looking content rejected')
                record={'source':source,'name':name,'kind':kind,'content':content,'status':'quarantined','version':digest(content.encode())}
                token=secrets.token_urlsafe(24)
                self.plans={k:v for k,v in self.plans.items() if v['expires']>time.time()}
                if len(self.plans)>=100: raise ValueError('too many pending plans')
                self.plans[token]={'expires':time.time()+300,'path':path,'before':before,'source':source,'after':json.dumps(record,ensure_ascii=False).encode()}
                return {'plan_id':token,'expires_in':300,'changes':record,'expected_sha256':expected,'ownership':'quarantine only','backup':digest(before) if before else None,'rollback':'Preview resource.rollback with backup hash; new imports remain quarantined'}
            if stage!='apply' or body.get('confirmed') is not True: raise ValueError('explicit confirmation required')
            plan_id=body.get('plan_id','')
            plan=self.plans.get(plan_id)
            if not plan:
                replay = batch_intake.receipt(self, body.get('plan_id'))
                if replay: return replay
                raise ValueError('expired or consumed plan')
            if plan['expires']<time.time():
                self.plans.pop(plan_id,None)
                raise ValueError('expired or consumed plan')
            if 'batch' in plan:
                result=batch_intake.apply(self, plan, public_text)
                self.plans.pop(plan_id,None)
                return result
            self.plans.pop(plan_id,None)
            if plan.get('source'): self.check_catalog_source(plan['source'])
            if plan.get('original_receipt_sha256'):
                current=self.sources._original_security_review({'original_limit':1})
                if current.get('status')!='verified-receipt' or current.get('receipt_sha256')!=plan['original_receipt_sha256']: raise ValueError('original receipt changed since preview')
            path=plan['path']
            if plan.get('owner')=='dashboard': self.owned_target(path.parent.name)
            else: self.target(path.parent.parent.name,path.parent.name)
            before=path.read_bytes() if path.exists() else b''
            if before!=plan['before']: raise ValueError('source changed since preview')
            path.parent.mkdir(parents=True,exist_ok=True)
            versions=path.parent/'versions'; versions.mkdir(exist_ok=True)
            if versions.is_symlink(): raise ValueError('unsafe versions path')
            if before:
                backup=versions/(digest(before)+'.json')
                if backup.is_symlink(): raise ValueError('unsafe backup')
                if backup.exists() and backup.read_bytes()!=before: raise ValueError('backup collision')
                if not backup.exists():
                    with backup.open('xb') as f: f.write(before)
            temp=path.with_name(secrets.token_hex(16)+'.tmp')
            with temp.open('xb') as f: f.write(plan['after'])
            temp.replace(path)
            return {'applied':True,'status':'dashboard-owned' if plan.get('owner')=='dashboard' else 'quarantined','sha256':digest(plan['after']),'backup':digest(before) if before else None}

class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(5)
    def log_message(self,*args): pass
    def reply(self,status,data,ctype='application/json'):
        raw=json.dumps(data,ensure_ascii=False,allow_nan=False).encode() if ctype=='application/json' else data
        self.send_response(status); self.send_header('Content-Type',ctype); self.send_header('Content-Length',str(len(raw))); self.send_header('Cache-Control','no-store'); self.send_header('X-Content-Type-Options','nosniff'); self.send_header('Content-Security-Policy',"default-src 'self'; object-src 'none'; frame-ancestors 'none'"); self.end_headers(); self.wfile.write(raw)
    def allowed(self,write=False):
        host=self.headers.get('Host','')
        port=getattr(self.server.app,'public_port',self.server.server_port)
        valid={f'127.0.0.1:{port}',f'localhost:{port}'}
        if host not in valid: return False
        valid_origins={'http://'+h for h in valid}
        origin=self.headers.get('Origin')
        if origin and origin not in valid_origins: return False
        if self.headers.get('Sec-Fetch-Site')=='cross-site': return False
        origin_ok = (origin in valid_origins) if origin else True
        csrf_ok = secrets.compare_digest(self.headers.get('X-CSRF-Token',''),self.server.app.csrf)
        return not write or (origin_ok and csrf_ok)
    def do_GET(self):
        if not self.allowed(): return self.reply(403,{'error':'origin/host rejected'})
        url=urlsplit(self.path)
        try:
            if url.path.startswith('/api/v1/'):
                q={k:v[0] for k,v in parse_qs(url.query).items()}
                return self.reply(200,self.server.app.get(url.path[8:],q))
            relative=unquote(url.path).lstrip('/') or 'index.html'
            path=(ROOT/'static'/relative).resolve()
            if not path.is_relative_to((ROOT/'static').resolve()) or not path.is_file(): return self.reply(404,{'error':'not found'})
            return self.reply(200,path.read_bytes(),mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
        except KeyError: self.reply(404,{'error':'not found'})
        except (ValueError, OSError): self.reply(400,{'error':'invalid request'})
    def do_POST(self):
        if not self.allowed(True): return self.reply(403,{'error':'CSRF/origin rejected'})
        try:
            if self.headers.get('Content-Type')!='application/json': raise ValueError('JSON required')
            length=int(self.headers.get('Content-Length',0))
            if not 0<length<=300000: raise ValueError('invalid body size')
            if self.path == '/api/v1/retrieval-check':
                if json.loads(self.rfile.read(length)) != {}: raise ValueError('empty object required')
                data = self.server.app.retrieval_check()
                return self.reply(429 if data['status'] == 'busy' else 200, {'data': data, 'status': data['status']})
            if self.path == '/api/v1/route_task':
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict): raise ValueError('object required')
                try:
                    envelope = self.server.app.route_task(body)
                except ValueError as ve:
                    return self.reply(400, {'error': str(ve)})
                status_code = 200 if envelope.get('status') == 'ok' else 503
                return self.reply(status_code, envelope)
            prefix='/api/v1/operations/'
            if not self.path.startswith(prefix): return self.reply(404,{'error':'not found'})
            body=json.loads(self.rfile.read(length))
            if not isinstance(body,dict): raise ValueError('object required')
            try:
                result = self.server.app.operation(self.path[len(prefix):],body)
            except ValueError as error:
                if self.path == prefix+'preview' and body.get('operation') == 'resource.import_batch':
                    return self.reply(409, {'error': public_text(str(error))})
                raise
            self.reply(200,{'schema':'skill-hub.dashboard.v1','observed_at':now(),'status':'ok','data':result,'sources':[],'limits':['Only dashboard-owned state or quarantine; no live host activation']})
        except (ValueError,TypeError,OSError,sqlite3.Error): self.reply(409,{'error':'invalid operation, stale source or plan'})

def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--port',type=int,default=8769); parser.add_argument('--project',type=Path,default=PROJECT); parser.add_argument('--state',type=Path)
    parser.add_argument('--native-db',type=Path)
    parser.add_argument('--native-agents',type=Path)
    parser.add_argument('--plugins',type=Path)
    parser.add_argument('--native-snapshot',type=Path)
    parser.add_argument('--export-native-snapshot',type=Path)
    args=parser.parse_args()
    app=Dashboard(args.project,args.state)
    for option,attribute in (('native_db','native_db'),('native_agents','native_agents'),('plugins','plugins_path'),('native_snapshot','native_snapshot')):
        if getattr(args,option) is not None: setattr(app.sources,attribute,getattr(args,option))
    if args.export_native_snapshot:
        # Explicit operator destination only; browser has no file/path API.
        with args.export_native_snapshot.open('x',encoding='utf-8') as output:
            json.dump(app.sources.export(),output,ensure_ascii=False,allow_nan=False)
        return

    server=ThreadingHTTPServer(('127.0.0.1',args.port),Handler); server.app=app; server.serve_forever()
if __name__=='__main__': main()
