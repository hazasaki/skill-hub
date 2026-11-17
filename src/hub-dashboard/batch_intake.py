"""Atomic dashboard-owned batch staging; never activates catalog resources."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import secrets
import sqlite3
import time

from observer_read import connect_ro

_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z')
_RESERVED = re.compile(r'(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?\Z')


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def native_content(kind, name, content):
    if kind == 'prompt' and not re.match(r'\A---[^\S\n]*\r?\n', content):
        return f"---\nname: {name}\ndescription: Imported prompt\nprompt_type: agent_role\n---\n" + content
    return content


def instruction_digest(record):
    text = native_content(record['kind'], record['name'], record['content']).replace('\r\n', '\n').replace('\r', '\n')
    match = re.match(r'^---\s*\n(.*?)\n---\s*\n', text, re.S)
    return sha((text[match.end():] if match else text).encode('utf-8'))


def validate_item(item, public_text):
    if not isinstance(item, dict) or set(item) != {'kind', 'name', 'content'}:
        raise ValueError('item requires kind, name and content')
    kind, name, content = item['kind'], item['name'], item['content']
    if kind not in ('skill', 'prompt') or not isinstance(name, str) or not _ID.fullmatch(name) or name.endswith('.') or _RESERVED.fullmatch(name):
        raise ValueError('invalid cross-platform resource identity')
    if not isinstance(content, str) or not 1 <= len(content.encode('utf-8')) <= 65536 or '\x00' in content or public_text(content) != content:
        raise ValueError('invalid or credential-looking content')
    normalized = content.replace('\r\n', '\n').replace('\r', '\n')
    if kind == 'skill':
        match = re.match(r'\A---\s*\n(.*?)\n---\s*\n(.*)\Z', normalized, re.S)
        if not match or not match.group(2).strip():
            raise ValueError('skill needs frontmatter and nonempty body')
        meta = match.group(1)
        for field in ('name', 'description'):
            values = re.findall(r'(?m)^' + field + r':[^\S\n]*(.*)$', meta)
            if len(values) != 1 or not values[0].strip() or values[0].strip() in ("''", '""'):
                raise ValueError('skill frontmatter needs one nonempty name and description')
        declared = re.search(r'(?m)^name:[^\S\n]*(.*)$', meta).group(1).strip().strip("'\"")
        if declared != name: raise ValueError('frontmatter name must match identity')
    elif not normalized.strip():
        raise ValueError('prompt needs nonempty content')
    return {'kind': kind, 'name': name, 'content': content, 'version': sha(content.encode('utf-8'))}


def is_link(path):
    return path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction())


def db_path(app):
    path = app.state / 'intake' / 'batches.sqlite'
    if not path.resolve().is_relative_to(app.state.resolve()) or any(is_link(p) for p in [path, app.state, *path.parents]):
        raise ValueError('unsafe intake path')
    return path


def read_staged(app):
    path = db_path(app)
    if not path.exists(): return {}
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as con:
        return {(r[0], r[1].casefold()): (r[1], r[2]) for r in con.execute('SELECT kind,name,version FROM items')}


def validate(app, items, public_text):
    if not isinstance(items, list) or not 1 <= len(items) <= 20:
        raise ValueError('batch requires 1..20 items')
    records = []
    for index, item in enumerate(items):
        try: records.append(validate_item(item, public_text))
        except ValueError as error: raise ValueError(f'item {index + 1}: {error}') from error
    if sum(len(r['content'].encode('utf-8')) for r in records) > 250000:
        raise ValueError('batch too large')
    keys = [(r['kind'], r['name'].casefold()) for r in records]
    if len(keys) != len(set(keys)): raise ValueError('duplicate batch identity')
    bodies = {}
    for record in records:
        key = (record['kind'], instruction_digest(record))
        if key in bodies: raise ValueError(f"Trùng nội dung hướng dẫn trong lô: {record['kind']}:{record['name']} và {bodies[key]}")
        bodies[key] = record['name']
    staged = read_staged(app)
    with closing(connect_ro(app.catalog)) as con:
        catalog = {(kind, row[0].casefold()) for kind, table in [('skill','skills'), ('prompt','prompts')] for row in con.execute('SELECT name FROM ' + table)}
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, column in [('aliases', 'alias_name'), ('quarantine', 'name')]:
            if table in tables:
                catalog.update((kind, name.casefold()) for kind, name in con.execute(
                    f'SELECT kind, {column} FROM {table}') if name and kind in ('skill', 'prompt'))
        for record in records:
            kind = record['kind']; digest = instruction_digest(record)
            view = 'skill_resources' if kind == 'skill' else 'prompt_resources'
            try:
                match = con.execute('SELECT name FROM ' + view + ' WHERE body_sha256=? ORDER BY name LIMIT 1', (digest,)).fetchone()
                blocked = 'catalog_blocked_bodies' in tables and con.execute('SELECT 1 FROM catalog_blocked_bodies WHERE kind=? AND body_sha256=? LIMIT 1', (kind,digest)).fetchone()
            except sqlite3.Error as error:
                raise ValueError('Không thể kiểm tra nội dung trùng trong catalog snapshot') from error
            if match: raise ValueError(f"Trùng nội dung hướng dẫn catalog: {kind}:{record['name']} và {kind}:{public_text(str(match[0]))}")
            if blocked: raise ValueError(f"Nội dung hướng dẫn của {kind}:{record['name']} trùng nội dung đã bị chặn trong catalog")
    for record, key in zip(records, keys):
        if key in catalog: raise ValueError('identity conflicts with catalog snapshot')
        folder = app.state / 'quarantine' / record['kind']
        if folder.is_dir():
            if any(is_link(p) for p in [folder, *folder.parents]): raise ValueError('unsafe quarantine')
            if any(p.name.casefold() == key[1] for p in folder.iterdir()): raise ValueError('identity conflicts with quarantine')
        if key in staged and staged[key] != (record['name'], record['version']): raise ValueError('identity conflicts with staged content')
    return records


def preview(app, body, public_text):
    records = validate(app, body.get('items'), public_text)
    canonical = json.dumps(sorted(records, key=lambda r: (r['kind'], r['name'].casefold())), ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    batch_id = sha(canonical)
    staged = read_staged(app)
    duplicate_info = []
    with closing(connect_ro(app.catalog)) as con:
        for record in records:
            table = 'skills' if record['kind'] == 'skill' else 'prompts'
            cols = {row[1] for row in con.execute('PRAGMA table_info(' + table + ')')}
            normalized = record['content'].replace('\r\n', '\n').replace('\r', '\n')
            identical = []
            if 'content_sha256' in cols:
                identical = [row[0] for row in con.execute('SELECT name FROM ' + table + ' WHERE content_sha256=? LIMIT 20', (sha(normalized.encode('utf-8')),))]
            duplicate_info.append({'kind':record['kind'], 'name':record['name'],
                                   'same_staged_identity':staged.get((record['kind'],record['name'].casefold())) == (record['name'], record['version']),
                                   'same_catalog_document_ids':identical,
                                   'bundle_equivalence':'unknown'})
    token = secrets.token_urlsafe(24)
    app.plans = {k:v for k,v in app.plans.items() if v['expires'] > time.time()}
    if len(app.plans) >= 100: raise ValueError('too many pending plans')
    app.plans[token] = {'expires': time.time() + 300, 'batch': records, 'batch_id': batch_id, 'receipt': sha(token.encode())}
    return {'plan_id': token, 'batch_id': batch_id, 'expires_in': 300,
            'items': [{**{k:r[k] for k in ('kind','name','version')},
                       'native_sha256':sha(native_content(r['kind'],r['name'],r['content']).encode('utf-8')),
                       'prompt_frontmatter_added':native_content(r['kind'],r['name'],r['content']) != r['content']} for r in records],
            'duplicates': duplicate_info, 'status': 'preview', 'catalog_scope': 'snapshot; live publication must recheck',
            'validation': 'basic frontmatter only; publisher validates YAML, safety and live catalog; prompts without frontmatter receive a deterministic wrapper',
            'ownership': 'dashboard staging only; not active'}


def apply(app, plan, public_text):
    records = validate(app, [{k:r[k] for k in ('kind','name','content')} for r in plan['batch']], public_text)
    path = db_path(app)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as con:
        con.execute('CREATE TABLE IF NOT EXISTS batches (batch_id TEXT PRIMARY KEY, created_at REAL NOT NULL)')
        con.execute('CREATE TABLE IF NOT EXISTS items (kind TEXT NOT NULL, name TEXT NOT NULL, name_key TEXT NOT NULL, version TEXT NOT NULL, content TEXT NOT NULL, batch_id TEXT NOT NULL, PRIMARY KEY(kind,name_key))')
        con.execute('CREATE TABLE IF NOT EXISTS batch_items (batch_id TEXT NOT NULL, kind TEXT NOT NULL, name_key TEXT NOT NULL, PRIMARY KEY(batch_id,kind,name_key))')
        con.execute('CREATE TABLE IF NOT EXISTS receipts (token_hash TEXT PRIMARY KEY, batch_id TEXT NOT NULL)')
        con.execute('BEGIN IMMEDIATE')
        existing = con.execute('SELECT 1 FROM batches WHERE batch_id=?', (plan['batch_id'],)).fetchone()
        if existing:
            con.execute('INSERT OR IGNORE INTO receipts VALUES(?,?)', (plan['receipt'], plan['batch_id']))
            con.commit()
            return {'applied': True, 'replayed': True, 'batch_id': plan['batch_id'], 'status': 'staged', 'active': False}
        for record in records:
            prior = con.execute('SELECT name, version FROM items WHERE kind=? AND name_key=?', (record['kind'], record['name'].casefold())).fetchone()
            if prior and prior != (record['name'], record['version']): raise ValueError('staged identity or content changed')
            con.execute('INSERT OR IGNORE INTO items VALUES(?,?,?,?,?,?)', (record['kind'], record['name'], record['name'].casefold(), record['version'], record['content'], plan['batch_id']))
            con.execute('INSERT INTO batch_items VALUES(?,?,?)', (plan['batch_id'], record['kind'], record['name'].casefold()))
        con.execute('INSERT INTO batches VALUES(?,?)', (plan['batch_id'], time.time()))
        con.execute('INSERT INTO receipts VALUES(?,?)', (plan['receipt'], plan['batch_id']))
        con.commit()
    return {'applied': True, 'replayed': False, 'batch_id': plan['batch_id'], 'status': 'staged', 'active': False}


def listing(app, query):
    path = db_path(app)
    if not path.exists(): return {'items': [], 'total': 0, 'status': 'staged', 'active': False}
    limit = max(1, min(100, int(query.get('limit', 50))))
    offset = max(0, min(100000, int(query.get('offset', 0))))
    batch_id = query.get('batch_id')
    if batch_id is not None and not re.fullmatch(r'[a-f0-9]{64}', batch_id): raise ValueError('invalid batch id')
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as con:
        if batch_id:
            args = [batch_id]
            source = ' FROM items i JOIN batch_items b ON i.kind=b.kind AND i.name_key=b.name_key WHERE b.batch_id=?'
            total = con.execute('SELECT count(*)' + source, args).fetchone()[0]
            rows = con.execute('SELECT i.kind,i.name,i.version,b.batch_id' + source + ' ORDER BY i.kind,i.name LIMIT ? OFFSET ?', args + [limit, offset]).fetchall()
        else:
            total = con.execute('SELECT count(*) FROM items').fetchone()[0]
            rows = con.execute('SELECT kind,name,version,batch_id FROM items ORDER BY kind,name LIMIT ? OFFSET ?', [limit, offset]).fetchall()
    return {'items': [dict(zip(('kind','name','version','batch_id'), r), status='staged', active=False) for r in rows], 'total': total, 'offset': offset, 'limit': limit}


def export(app, kind, name, public_text):
    if kind not in ('skill','prompt') or not isinstance(name,str) or not _ID.fullmatch(name): raise ValueError('invalid identity')
    path=db_path(app)
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as con:
        row=con.execute('SELECT kind,name,version,content,batch_id FROM items WHERE kind=? AND name_key=?', (kind,name.casefold())).fetchone()
    if not row: raise KeyError(name)
    if public_text(row[3]) != row[3]: raise ValueError('unsafe staged content')
    return {'kind':row[0], 'name':row[1], 'version':row[2], 'content':row[3], 'batch_id':row[4], 'filename': 'SKILL.md' if kind=='skill' else row[1]+'.md', 'status':'staged', 'active':False}


def receipt(app, token):
    if not isinstance(token, str) or len(token) > 100: return None
    path = db_path(app)
    if not path.exists(): return None
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as con:
        row = con.execute('SELECT batch_id FROM receipts WHERE token_hash=?', (sha(token.encode()),)).fetchone()
    return {'applied':True, 'replayed':True, 'batch_id':row[0], 'status':'staged', 'active':False} if row else None


def export_batch(app, batch_id, public_text):
    if not isinstance(batch_id, str) or not re.fullmatch(r'[a-f0-9]{64}', batch_id): raise ValueError('invalid batch id')
    path = db_path(app)
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as con:
        if not con.execute('SELECT 1 FROM batches WHERE batch_id=?', (batch_id,)).fetchone(): raise KeyError(batch_id)
        rows = con.execute('SELECT i.kind,i.name,i.content,i.version FROM items i JOIN batch_items b ON i.kind=b.kind AND i.name_key=b.name_key WHERE b.batch_id=? ORDER BY i.kind,i.name', (batch_id,)).fetchall()
    if any(public_text(row[2]) != row[2] or sha(row[2].encode('utf-8')) != row[3] for row in rows): raise ValueError('unsafe or changed staged content')
    return {'batch_id':batch_id, 'items':[dict(zip(('kind','name','content','version'), row)) for row in rows]}
