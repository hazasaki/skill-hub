"""Explicit native publication of dashboard batch exports, plus startup overlay."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile

import catalog_ingest
import policy_gate
import skill_index
from credential_mask import CREDENTIAL_RULE_VERSION,mask_credentials

ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z')
MAX_ITEMS = 1000
MAX_BYTES = 10000000
RESERVED = re.compile(r'(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?\Z')

def sha(data): return hashlib.sha256(data).hexdigest()

def is_link(path):
    return path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction())

def check_directory(path):
    path = Path(path)
    if any(is_link(p) for p in [path, *path.parents]): raise ValueError('publication path contains a link or junction')

def credential_text(content):
    return mask_credentials(content)

def native_content(kind, name, content):
    if kind == 'prompt' and not re.match(r'\A---[^\S\n]*\r?\n', content):
        return f'---\nname: {name}\ndescription: Imported prompt\nprompt_type: agent_role\n---\n' + content
    return content

def normalize(export):
    if not isinstance(export, dict) or set(export) != {'batch_id', 'items'}: raise ValueError('invalid export')
    items = export['items']
    if not isinstance(items, list) or not 1 <= len(items) <= 20: raise ValueError('invalid batch size')
    keys=set()
    for item in items:
        if not isinstance(item,dict) or set(item) != {'kind','name','content','version'}: raise ValueError('invalid item')
        kind,name,content,version = item['kind'],item['name'],item['content'],item['version']
        if kind not in ('skill','prompt') or not isinstance(name,str) or not ID.fullmatch(name) or name.endswith('.') or RESERVED.fullmatch(name): raise ValueError('invalid identity')
        if not isinstance(content,str) or not 1 <= len(content.encode()) <= 65536 or '\x00' in content: raise ValueError('invalid content')
        if credential_text(content) != content: raise ValueError('credential-looking content rejected')
        if version != sha(content.encode()): raise ValueError('source version mismatch')
        key=(kind,name.casefold())
        if key in keys: raise ValueError('batch duplicate identity')
        keys.add(key)
    if sum(len(x['content'].encode()) for x in items)>250000: raise ValueError('batch too large')
    canonical=json.dumps(sorted(items,key=lambda r:(r['kind'],r['name'].casefold())),ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
    if export['batch_id'] != sha(canonical): raise ValueError('batch id mismatch')
    return items

def write_tree(root, items):
    (root/'skills').mkdir(parents=True,exist_ok=True)
    (root/'prompts').mkdir(parents=True,exist_ok=True)
    for item in items:
        kind,name=item['kind'],item['name']
        text=native_content(kind,name,item['content'])
        if kind=='skill':
            folder=root/'skills'/name; folder.mkdir(); path=folder/'SKILL.md'
        else: path=root/'prompts'/(name+'.md')
        path.write_bytes(text.encode('utf-8'))
        rec=(catalog_ingest.validate_skill_entry(path.parent) if kind=='skill' else catalog_ingest.validate_prompt_entry(path))
        if not rec.is_valid: raise ValueError(f'{kind}:{name} failed native format validation')
        meta,body,_,error=catalog_ingest.parse_frontmatter(text,default_name=name)
        if error or meta.get('name',name)!=name: raise ValueError(f'{kind}:{name} metadata name mismatch')
        resource={**meta,'type':kind,'id':name,'body':body,'authorization_source':'host_policy_only',
                  'bundle_files':[], 'prompt_type':meta.get('prompt_type') or 'agent_role'}
        if not policy_gate.verify_resource_safety(resource).get('allowed'): raise ValueError(f'{kind}:{name} failed safety check')
        if meta.get('activation_status','active')!='active' or meta.get('disabled') is True: raise ValueError(f'{kind}:{name} is disabled')

def load_all(directory):
    directory=Path(directory)
    if not directory.exists(): raise ValueError('configured publication directory missing')
    check_directory(directory)
    all_items=[]; seen={}; total_bytes=0
    for path in sorted(directory.glob('*.json')):
        if path.is_symlink() or path.stat().st_size>400000: raise ValueError('unsafe publication')
        payload=json.loads(path.read_text(encoding='utf-8'))
        items=normalize(payload)
        if path.stem != payload['batch_id']: raise ValueError('publication filename mismatch')
        for item in items:
            key=(item['kind'],item['name'].casefold())
            if key in seen:
                if seen[key] != item: raise ValueError('published identity collision')
                continue
            seen[key]=item; all_items.append(item)
            total_bytes += len(item['content'].encode('utf-8'))
            if len(all_items) > MAX_ITEMS or total_bytes > MAX_BYTES:
                raise ValueError('publication storage limit exceeded')
    return all_items

def collisions(base_db, items, hub):
    with closing(sqlite3.connect(Path(base_db).resolve().as_uri()+'?mode=ro',uri=True)) as con:
        existing={(kind,row[0].casefold()) for kind,table in [('skill','skills'),('prompt','prompts')] for row in con.execute('SELECT name FROM '+table)}
        existing.update((row[1],row[0].casefold()) for row in con.execute('SELECT alias_name,kind FROM aliases') if row[1] in ('skill','prompt'))
        existing.update((row[1],row[0].casefold()) for row in con.execute('SELECT name,kind FROM quarantine') if row[1] in ('skill','prompt') and row[0])
    for kind, folder in [('skill',Path(hub)/'skills'),('prompt',Path(hub)/'prompts')]:
        if not folder.is_dir() or folder.is_symlink(): raise ValueError('native resource directory unavailable or unsafe')
        for entry in folder.iterdir():
            name = entry.name if kind=='skill' else entry.stem
            existing.add((kind,name.casefold()))
    return [(i['kind'],i['name']) for i in items if (i['kind'],i['name'].casefold()) in existing]

def check_instruction_bodies(base_db, items):
    with closing(sqlite3.connect(Path(base_db).resolve().as_uri()+'?mode=ro',uri=True)) as con:
        bodies = {(kind, row[0]) for kind, table in [('skill','skill_resources'),('prompt','prompt_resources')]
                  for row in con.execute('SELECT body_sha256 FROM '+table)}
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='catalog_blocked_bodies'").fetchone():
            bodies.update(con.execute('SELECT kind,body_sha256 FROM catalog_blocked_bodies'))
        for item in items:
            kind, name = item['kind'], item['name']
            text = native_content(kind, name, item['content']).replace('\r\n','\n').replace('\r','\n')
            _, body, _, _ = catalog_ingest.parse_frontmatter(text, default_name=name)
            key = (kind, catalog_ingest.content_digest(body))
            if key in bodies:
                raise ValueError(f'{kind}:{name} repeats an existing instruction body')
            bodies.add(key)

def publish(export, directory, base_db, hub):
    items=normalize(export); directory=Path(directory)
    check_directory(directory)
    directory.mkdir(parents=True,exist_ok=True)
    check_directory(directory)
    lock=directory/'.publish-lock.sqlite'
    if lock.is_symlink(): raise ValueError('unsafe publication lock')
    temp=None
    con=sqlite3.connect(lock,timeout=5)
    try:
        con.execute('BEGIN IMMEDIATE')
        existing=load_all(directory)
        target=directory/(export['batch_id']+'.json')
        if target.exists():
            if json.loads(target.read_text(encoding='utf-8')) != export: raise ValueError('batch id collision')
            return {'batch_id':export['batch_id'],'status':'published','replayed':True,'activation':'restart required'}
        if collisions(base_db,items,hub): raise ValueError('identity conflicts with live catalog, including disabled')
        old={(i['kind'],i['name'].casefold()):i for i in existing}
        if any((i['kind'],i['name'].casefold()) in old and old[(i['kind'],i['name'].casefold())] != i for i in items): raise ValueError('identity conflicts with prior publication')
        new_items=[i for i in items if (i['kind'],i['name'].casefold()) not in old]
        check_instruction_bodies(base_db,existing+new_items)
        if len(existing)+len(new_items)>MAX_ITEMS or sum(len(i['content'].encode('utf-8')) for i in existing+new_items)>MAX_BYTES:
            raise ValueError('publication storage limit exceeded')
        with tempfile.TemporaryDirectory() as tmp:
            write_tree(Path(tmp),items)
        raw=json.dumps(export,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
        fd,temp=tempfile.mkstemp(prefix='.batch-',suffix='.tmp',dir=directory)
        with os.fdopen(fd,'wb') as out:
            out.write(raw); out.flush(); os.fsync(out.fileno())
        os.replace(temp,target); temp=None
        return {'batch_id':export['batch_id'],'status':'published','replayed':False,'activation':'restart required'}
    finally:
        if temp and os.path.exists(temp): os.unlink(temp)
        con.rollback()
        con.close()

def materialize_overlay(directory, overlay, db, hub):
    items=load_all(directory)
    if not items: return {'published_skills':0,'published_prompts':0}
    if collisions(db,items,hub): raise ValueError('published resource conflicts with baseline catalog')
    check_instruction_bodies(db,items)
    overlay=Path(overlay)
    check_directory(overlay)
    overlay.mkdir(parents=True,exist_ok=False)
    created = overlay.resolve()
    try:
        write_tree(overlay,items)
        candidate=overlay/'additions.sqlite'
        skill_index.build(overlay,output_path=candidate)
        with closing(sqlite3.connect(db)) as con:
            con.execute('ATTACH DATABASE ? AS additions',(str(candidate),))
            con.execute('BEGIN IMMEDIATE')
            con.execute('INSERT OR IGNORE INTO resource_bodies SELECT * FROM additions.resource_bodies')
            for table in ('skill_resources','prompt_resources'):
                con.execute(f'INSERT INTO {table} SELECT * FROM additions.{table}')
            for table,source in [('fts','skills'),('fts_prompts','prompts')]:
                # Insert only the newly published rows; existing external-content postings remain intact.
                names=[i['name'] for i in items if i['kind']==('skill' if table=='fts' else 'prompt')]
                for name in names:
                    con.execute(f'INSERT INTO {table}(rowid,name,description,tags,body) SELECT rowid,name,description,tags,body FROM {source} WHERE name=?',(name,))
            con.commit()
    except Exception:
        # The directory was created by this invocation and contains only our validated tree.
        if is_link(overlay) or overlay.resolve() != created or created.parent != overlay.parent.resolve(): raise
        shutil.rmtree(overlay)
        raise
    return {'published_skills':sum(i['kind']=='skill' for i in items), 'published_prompts':sum(i['kind']=='prompt' for i in items)}

def main():
    p=argparse.ArgumentParser(); p.add_argument('export');p.add_argument('--directory',required=True);p.add_argument('--catalog',required=True);p.add_argument('--hub',required=True)
    args=p.parse_args()
    source=Path(args.export)
    check_directory(source.parent)
    if is_link(source) or not source.is_file(): raise ValueError('invalid export file')
    with source.open('rb') as stream:
        before=os.fstat(stream.fileno())
        if before.st_size>400000: raise ValueError('invalid export file')
        raw=stream.read(400001)
        after=os.fstat(stream.fileno())
    current=source.stat()
    if is_link(source) or len(raw)>400000 or (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns) or (after.st_dev,after.st_ino)!=(current.st_dev,current.st_ino): raise ValueError('export file changed')
    export=json.loads(raw.decode('utf-8-sig'))
    if set(export) == {'schema','observed_at','status','data','sources','limits'}: export=export['data']
    print(json.dumps(publish(export,args.directory,args.catalog,args.hub),ensure_ascii=False))

if __name__=='__main__': main()
