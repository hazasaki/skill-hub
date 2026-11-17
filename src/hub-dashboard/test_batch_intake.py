from contextlib import closing
import os
import subprocess
import http.client
import json
import hashlib
import threading
import sqlite3
import tempfile
import unittest
from pathlib import Path
from server import Dashboard, Handler, ThreadingHTTPServer

SKILL = '---\nname: sample\ndescription: a test skill\n---\n\n# Instructions\n'

class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = Dashboard(self.root)
        self.app.catalog.parent.mkdir(parents=True)
        with closing(sqlite3.connect(self.app.catalog)) as db:
            for table in ('skills','prompts'):
                db.execute(f'CREATE TABLE {table}(name TEXT,active INTEGER,body_sha256 TEXT)')
            db.executescript('CREATE VIEW skill_resources AS SELECT * FROM skills; CREATE VIEW prompt_resources AS SELECT * FROM prompts;')
            db.execute("INSERT INTO skills(name,active) VALUES('disabled-skill',0)"); db.commit()
    def tearDown(self): self.temp.cleanup()
    def plan(self, items): return self.app.operation('preview', {'operation':'resource.import_batch','items':items})
    def apply(self, plan): return self.app.operation('apply', {'plan_id':plan['plan_id'],'confirmed':True})
    def test_instruction_body_duplicates_fail_before_staging_and_recheck_on_apply(self):
        one={'kind':'skill','name':'one','content':'---\nname: one\ndescription: first\n---\n\n# Same\n'}
        two={'kind':'skill','name':'two','content':'---\r\nname: two\r\ndescription: second\r\n---\r\n\r\n# Same\r\n'}
        with self.assertRaisesRegex(ValueError, 'trong lô.*two.*one'): self.plan([one,two])
        with self.assertRaisesRegex(ValueError, 'trong lô'):
            self.plan([{'kind':'prompt','name':'one','content':'# Same\n'}, {'kind':'prompt','name':'two','content':'---\nname: two\n---\n# Same\n'}])
        plan=self.plan([two])
        digest=hashlib.sha256(b'# Same\n').hexdigest()
        with closing(sqlite3.connect(self.app.catalog)) as db:
            db.execute('INSERT INTO skills VALUES(?,?,?)',('canonical',1,digest)); db.commit()
        with self.assertRaisesRegex(ValueError, 'catalog.*two.*canonical'): self.plan([two])
        with self.assertRaisesRegex(ValueError, 'catalog.*canonical'): self.apply(plan)
        self.assertEqual(self.app.get('intake',{})['data']['total'],0)
        self.assertTrue(self.plan([{'kind':'prompt','name':'distinct-kind','content':'# Same\n'}])['plan_id'])

    def test_blocked_body_and_missing_index_are_not_silently_accepted(self):
        with closing(sqlite3.connect(self.app.catalog)) as db:
            db.execute('CREATE TABLE catalog_blocked_bodies(kind,body_sha256)')
            db.execute('INSERT INTO catalog_blocked_bodies VALUES(?,?)',('prompt',hashlib.sha256(b'blocked').hexdigest())); db.commit()
        with self.assertRaisesRegex(ValueError, 'đã bị chặn'):
            self.plan([{'kind':'prompt','name':'other','content':'blocked'}])
        with closing(sqlite3.connect(self.app.catalog)) as db:
            db.execute('DROP VIEW prompt_resources'); db.commit()
        with self.assertRaisesRegex(ValueError, 'Không thể kiểm tra'):
            self.plan([{'kind':'prompt','name':'other','content':'new text'}])
    def test_staging_reserves_native_aliases_and_quarantine_by_kind(self):
        with closing(sqlite3.connect(self.app.catalog)) as db:
            db.execute('CREATE TABLE aliases (kind, alias_name)')
            db.execute('CREATE TABLE quarantine (kind, name)')
            db.execute("INSERT INTO aliases VALUES ('skill', 'SAMPLE')")
            db.execute("INSERT INTO quarantine VALUES ('prompt', 'archived')")
            db.commit()
        with self.assertRaisesRegex(ValueError, 'catalog snapshot'):
            self.plan([{'kind': 'skill', 'name': 'sample', 'content': SKILL}])
        with self.assertRaisesRegex(ValueError, 'catalog snapshot'):
            self.plan([{'kind': 'prompt', 'name': 'ARCHIVED', 'content': 'text'}])
        self.assertTrue(self.plan([{'kind': 'prompt', 'name': 'sample', 'content': 'text'}])['plan_id'])
    def test_mixed_batch_replay_export_and_persistence(self):
        items = [{'kind':'skill','name':'sample','content':SKILL}, {'kind':'prompt','name':'example','content':'# Prompt\nHello'}]
        p = self.plan(items)
        prompt_preview=next(item for item in p['items'] if item['kind']=='prompt')
        self.assertTrue(prompt_preview['prompt_frontmatter_added'])
        self.assertNotEqual(prompt_preview['native_sha256'],prompt_preview['version'])
        r = self.apply(p)
        self.assertEqual(r['status'],'staged'); self.assertFalse(r['active'])
        self.assertTrue(self.apply(p)['replayed'])
        self.assertTrue(Dashboard(self.root).operation('apply', {'plan_id':p['plan_id'], 'confirmed':True})['replayed'])
        self.assertEqual(self.app.get('intake',{})['data']['total'],2)
        self.assertEqual(self.app.get('intake/export',{'kind':'skill','name':'sample'})['data']['content'],SKILL)
        self.assertTrue(self.apply(self.plan(items))['replayed'])
        other = self.plan([items[0], {'kind':'prompt','name':'another','content':'hello'}]); self.apply(other)
        self.assertEqual(self.app.get('intake',{'batch_id':other['batch_id']})['data']['total'],2)
        exported=self.app.get('intake/export',{'batch_id':other['batch_id']})['data']
        self.assertEqual(exported['batch_id'],other['batch_id'])
        self.assertEqual(len(exported['items']),2)
        self.assertEqual(set(exported['items'][0]),{'kind','name','content','version'})
        self.assertEqual(Dashboard(self.root).get('intake',{})['data']['total'],3)
    def test_rejects_invalid_conflicts_and_no_partial_write(self):
        good={'kind':'skill','name':'sample','content':SKILL}
        for bad in [{'kind':'skill','name':'DISABLED-SKILL','content':SKILL.replace('name: sample','name: DISABLED-SKILL')}, {'kind':'skill','name':'CON.txt','content':SKILL.replace('name: sample','name: CON.txt')}, {'kind':'skill','name':'bad','content':'no frontmatter'}, {'kind':'prompt','name':'x','content':'password=secretvalue'}]:
            with self.assertRaises(ValueError): self.plan([good,bad])
        self.assertEqual(self.app.get('intake',{})['data']['total'],0)
        with self.assertRaises(ValueError): self.plan([good,dict(good,name='SAMPLE',content=SKILL.replace('name: sample','name: SAMPLE'))])
        p=self.plan([good])
        with closing(sqlite3.connect(self.app.catalog)) as db:
            db.execute("INSERT INTO skills(name,active) VALUES('sample',0)"); db.commit()
        with self.assertRaises(ValueError): self.apply(p)
        self.assertEqual(self.app.get('intake',{})['data']['total'],0)
    def test_failure_inside_transaction_leaves_no_partial_items(self):
        self.apply(self.plan([{'kind':'prompt','name':'seed','content':'hello'}]))
        path=self.app.state/'intake'/'batches.sqlite'
        with closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TRIGGER reject_fail BEFORE INSERT ON items WHEN NEW.name='fail' BEGIN SELECT RAISE(ABORT,'injected'); END")
            db.commit()
        p=self.plan([{'kind':'prompt','name':'ok','content':'other text'}, {'kind':'prompt','name':'fail','content':'different text'}])
        with self.assertRaises(sqlite3.IntegrityError): self.apply(p)
        self.assertEqual(self.app.get('intake',{})['data']['total'],1)
        with closing(sqlite3.connect(path)) as db:
            db.execute('DROP TRIGGER reject_fail'); db.commit()
        self.assertTrue(self.apply(p)['applied'])
        self.assertEqual(self.app.get('intake',{})['data']['total'],3)

    def test_http_csrf_preview_apply_and_export(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler); server.app=self.app
        thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        try:
            conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
            payload={'operation':'resource.import_batch','items':[{'kind':'skill','name':'sample','content':SKILL}]}
            headers={'Content-Type':'application/json'}
            conn.request('POST','/api/v1/operations/preview',json.dumps(payload),headers)
            resp=conn.getresponse(); self.assertEqual(resp.status,403); resp.read()
            headers['X-CSRF-Token']=self.app.csrf
            duplicate={'operation':'resource.import_batch','items':[{'kind':'prompt','name':'first','content':'same'}, {'kind':'prompt','name':'second','content':'same'}]}
            conn.request('POST','/api/v1/operations/preview',json.dumps(duplicate),headers)
            resp=conn.getresponse(); self.assertEqual(resp.status,409)
            self.assertIn('Trùng nội dung hướng dẫn trong lô',json.loads(resp.read())['error'])
            conn.request('POST','/api/v1/operations/preview',json.dumps(payload),headers)
            resp=conn.getresponse(); self.assertEqual(resp.status,200); plan=json.loads(resp.read())['data']['plan_id']
            conn.request('POST','/api/v1/operations/apply',json.dumps({'plan_id':plan,'confirmed':True}),headers)
            resp=conn.getresponse(); self.assertEqual(resp.status,200); self.assertFalse(json.loads(resp.read())['data']['active'])
            conn.request('GET','/api/v1/intake/export?kind=skill&name=sample')
            resp=conn.getresponse(); self.assertEqual(resp.status,200); self.assertEqual(json.loads(resp.read())['data']['content'],SKILL)
            conn.close()
        finally:
            server.shutdown(); server.server_close(); thread.join()

    @unittest.skipUnless(os.name == 'nt', 'Windows junction test')
    def test_staging_rejects_real_junction(self):
        outside=self.root/'outside'; outside.mkdir()
        link=self.root/'junction'
        subprocess.run(['cmd','/c','mklink','/J',str(link),str(outside)],check=True,capture_output=True)
        try:
            self.app.state=link/'state'
            with self.assertRaises(ValueError): self.plan([{'kind':'prompt','name':'example','content':'hello'}])
            self.assertEqual(list(outside.iterdir()),[])
        finally: os.rmdir(link)

    def test_quarantine_conflicts_and_changed_content(self):
        folder=self.app.state/'quarantine'/'skill'/'Sample';folder.mkdir(parents=True)
        with self.assertRaises(ValueError): self.plan([{'kind':'skill','name':'sample','content':SKILL}])
        folder.rmdir()
        self.apply(self.plan([{'kind':'skill','name':'sample','content':SKILL}]))
        with self.assertRaises(ValueError): self.plan([{'kind':'skill','name':'sample','content':SKILL+'changed'}])

if __name__ == '__main__': unittest.main()
