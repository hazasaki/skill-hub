"""Review pinned original source locally; persist metadata only, never source/value snippets."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--hub',type=Path,required=True)
    parser.add_argument('--catalog',type=Path,required=True,help='Finalized non-WAL catalog snapshot')
    parser.add_argument('--output',type=Path,required=True,help='Metadata-only security-review.json')
    parser.add_argument('--audit-output',type=Path,help='Optional local metadata report including source paths')
    args=parser.parse_args()
    from server import Dashboard,public_text
    from sources import _detector_fingerprint,_EXEC_SCAN_RE
    from security_review import CREDENTIAL_RE,credential_review,execution_review
    from credential_mask import structured_credential_spans
    hub=args.hub.resolve(strict=True)
    catalog=args.catalog.resolve(strict=True)
    for target in (args.output,args.audit_output):
        if target is not None and (target.resolve()==catalog or target.resolve().is_relative_to(hub)):
            raise SystemExit('Review outputs must be outside the source hub and catalog')
    if any(Path(str(catalog)+suffix).exists() for suffix in ('-wal','-journal')):
        raise SystemExit('A finalized catalog snapshot without journals is required')
    catalog_sha=hashlib.sha256(catalog.read_bytes()).hexdigest()
    con=sqlite3.connect(catalog.as_uri()+'?mode=ro',uri=True);con.row_factory=sqlite3.Row
    app=Dashboard(project=hub.parent);app.catalog=catalog
    scanned=app.sources._scan_catalog_security()
    targets={(x['kind'],x['id']):x['checks']==['EMPTY_BODY'] for x in scanned['items'] if x['checks']}
    records=[];local=[]
    norm=lambda text:text.replace('\r\n','\n').replace('\r','\n')
    for (kind,rid),empty in sorted(targets.items()):
        table='skills' if kind=='skill' else 'prompts'
        indexed=con.execute('SELECT * FROM '+table+' WHERE name=?',(rid,)).fetchone()
        path=hub/'skills'/rid/'SKILL.md' if kind=='skill' else Path(indexed['path'])
        if not path.is_absolute():path=hub/'prompts'/path
        record={'kind':kind,'id':rid,'source_scope':'original_document','source_status':'UNAVAILABLE',
                'source_content_sha256':None,'source_file_sha256':None,'catalog_body_sha256':indexed['body_sha256'],
                'catalog_content_sha256':indexed['content_sha256'],'content_matches_catalog':False,
                'indexed_active':bool(indexed['active']),'index_body_empty':empty,'checks':[],'reasons':[],
                'evidence_lines':[],'disposition':'source_unavailable'}
        try:
            ancestors=[path]+[parent for parent in path.parents if parent.is_relative_to(hub)]
            if any(part.is_symlink() or (hasattr(part,'is_junction') and part.is_junction()) for part in ancestors):raise ValueError('linked source')
            if not path.resolve(strict=True).is_relative_to(hub.resolve()):raise ValueError('outside hub')
            raw=path.read_bytes();text=raw.decode('utf-8',errors='replace')
            normalized=norm(text)
            source_sha=hashlib.sha256(normalized.encode()).hexdigest()
            same=source_sha==indexed['content_sha256']
            credential=credential_review(normalized)
            execution=execution_review(normalized,_EXEC_SCAN_RE)
            has_cred=bool(credential['matches'] or '[redacted]' in normalized.lower())
            has_exec=bool(execution['matches'])
            checks=(['REDACTED_CREDENTIAL_REVIEW'] if has_cred else [])+(['EXECUTION_OR_INJECTION_REVIEW'] if has_exec else [])
            example=bool(checks and (not has_cred or credential['example_only']) and (not has_exec or execution['example_only']))
            positions=[m.start() for m in CREDENTIAL_RE.finditer(normalized)]+[m.start() for m in _EXEC_SCAN_RE.finditer(normalized)]+[m.start() for m in re.finditer(r'(?i)\[redacted\]',normalized)]
            positions.extend(start for start,_,_ in structured_credential_spans(normalized))
            record.update(source_status='READ' if same else 'CHANGED',source_content_sha256=source_sha,
                          source_file_sha256=hashlib.sha256(raw).hexdigest(),content_matches_catalog=same,
                          checks=checks,reasons=sorted(set(credential['reasons']+execution['reasons'])),
                          evidence_lines=sorted(set(normalized[:p].count('\n')+1 for p in positions)),
                          disposition='source_changed' if not same else 'confirmed_example' if example else 'needs_source_review' if checks else 'no_rule_hit')
        except (OSError,ValueError):
            pass
        records.append(record)
        local.append({**record,'source_path':str(path)})
    con.close()
    receipt={'schema':'skill-hub.security-review.v1','reviewed_at':datetime.now(timezone.utc).isoformat(),
             'catalog_sha256':catalog_sha,'detector_fingerprint':_detector_fingerprint(public_text),'records':records}
    if (hashlib.sha256(catalog.read_bytes()).hexdigest()!=catalog_sha or
            any(Path(str(catalog)+suffix).exists() for suffix in ('-wal','-journal'))):
        raise SystemExit('Catalog changed during original-source review; receipt not published')
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(receipt,indent=2),encoding='utf-8')
    summary={'total':len(records),'source_status':dict(Counter(r['source_status'] for r in records)),
             'by_disposition':dict(Counter(r['disposition'] for r in records)),
             'indexed_empty_originals':sum(r['index_body_empty'] for r in records),
             'by_scope':{scope:dict(Counter(r['disposition'] for r in records if r['index_body_empty']==empty)) for scope,empty in (('normalized_findings',False),('quarantined_originals',True))},
             'reasons':dict(Counter(reason for r in records for reason in r['reasons']))}
    priority=('provider_format_literal','url_userinfo_requires_review','xml_credential_field_requires_review','original_redacted_marker_requires_review',
              'unverified_execution_or_override','quoted_literal_requires_review',
              'dynamic_expression_unverified','unquoted_literal_or_type_unverified')
    summary['unresolved_primary_causes']=dict(Counter(next((reason for reason in priority if reason in record['reasons']),'other')
        for record in records if record['disposition']=='needs_source_review'))
    if args.audit_output:
        args.audit_output.parent.mkdir(parents=True,exist_ok=True)
        args.audit_output.write_text(json.dumps({'summary':summary,'records':local},indent=2),encoding='utf-8')
    print(json.dumps(summary))


if __name__=='__main__':main()
