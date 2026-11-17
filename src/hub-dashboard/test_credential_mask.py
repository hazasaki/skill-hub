"""Synthetic-only XML/URL masking, source evidence and admission parity checks."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import unittest

import test_backend
import batch_intake
from credential_mask import mask_credentials,structured_credential_spans
from security_review import credential_review
from server import public_text

XML_VALUE='fixture_xml_private_80731'
URI_VALUE='fixture_uri_private_80731'
XML=f'<string name="user_password">{XML_VALUE}</string>'
URI=f'postgresql://reader:{URI_VALUE}@db.example.invalid:5432/main'


class MaskingTests(unittest.TestCase):
    def test_xml_fields_attributes_entities_namespaces_and_cdata(self):
        for text in (XML,f'<password>{XML_VALUE}</password>',
                     f'<privatekey>{XML_VALUE}</privatekey>',
                     f'<auth:password>{XML_VALUE}</auth:password>',
                     f'<string name="auth_token">{XML_VALUE}</string>',
                     f'<add key="client_secret" value="{XML_VALUE}"/>',
                     f'<config user_password="{XML_VALUE}"/>',
                     f'<config user_password="{XML_VALUE}>suffix"/>',
                     f'<add value="{XML_VALUE}>suffix" name="password"/>',
                     f'<string name="pass&#119;ord">prefix{XML_VALUE}<![CDATA[{XML_VALUE}]]>suffix</string>',
                     f'<password>prefix{XML_VALUE}<b>{XML_VALUE}</b>{XML_VALUE}</password>',
                     f'<password>prefix<!-- </password> -->{XML_VALUE}</password>',
                     f'<password><![CDATA[{XML_VALUE}<xml-looking>]]></password>',
                     f'<password>\n{XML_VALUE}\n</password>'):
            with self.subTest(form=text.split('>')[0]):
                result=public_text(text)
                self.assertNotIn(XML_VALUE,result)
                self.assertIn('[REDACTED]',result)
                self.assertEqual(text.count('\n'),result.count('\n'))
                self.assertFalse(credential_review(text)['example_only'])
                self.assertIn('xml_credential_field_requires_review',credential_review(text)['reasons'])

    def test_url_userinfo_is_hidden_with_encoded_and_literal_at_signs(self):
        for text in (URI,URI.replace(URI_VALUE,URI_VALUE+'%40suffix'),URI.replace(URI_VALUE,URI_VALUE+'@suffix'),
                     URI.replace('reader:',' :').replace(' ','') ):
            result=public_text(text)
            self.assertNotIn(URI_VALUE,result)
            self.assertNotIn('reader:',result)
            self.assertIn('[REDACTED]@db.example.invalid',result)
            self.assertIn('url_userinfo_requires_review',credential_review(text)['reasons'])

    def test_public_urls_and_xml_near_misses_are_unchanged(self):
        for text in ('https://example.invalid:443/path','https://reader@example.invalid/doc',
                     'https://reader:@example.invalid/doc','reader@example.invalid',
                     '<string name="password_hint">public hint</string>',
                     '<string name="token_count">123</string>',
                     '<tokenize>public</tokenize>','<password></password>',
                     '<password> \n </password>','<config password_hint="public"/>'):
            self.assertEqual(public_text(text),text)
        for text in ('Bearer <ACCESS_TOKEN>','token=<TOKEN>'):
            self.assertEqual(public_text(text),'[REDACTED]')
            self.assertTrue(credential_review(text)['example_only'])

    def test_mixed_document_and_bounded_malformed_structures(self):
        body=URI+'\n<string name="pass&#119;ord">\n'+XML_VALUE+'<b>'+XML_VALUE+'</b>\n</string>\nhttps://public.example.invalid\n<string name="password_hint">public</string>'
        result=public_text(body)
        self.assertNotIn(XML_VALUE,result);self.assertNotIn(URI_VALUE,result)
        self.assertEqual(body.count('\n'),result.count('\n'))
        self.assertIn('https://public.example.invalid',result)
        self.assertIn('<string name="password_hint">public</string>',result)
        started=time.monotonic()
        malformed='<password '+'<node a="'*20000
        public_text(malformed)
        nested='<password>'+'<x>'*10000+XML_VALUE
        self.assertNotIn(XML_VALUE,public_text(nested))
        self.assertLess(time.monotonic()-started,2.0)

    def test_masks_are_identical_in_dashboard_and_native_runtime(self):
        app=Path(__file__).resolve().parents[2]/'deploy/skill-hub/app'
        native=app/'credential_mask.py'
        self.assertEqual(native.read_bytes(),Path(__file__).with_name('credential_mask.py').read_bytes())
        code='import json,publish_batch; print(json.dumps([publish_batch.credential_text(x) for x in json.loads(input())]))'
        samples=[XML,URI,'https://example.invalid/path','password="fixture_old_private"']
        result=subprocess.check_output([sys.executable,'-B','-c',code],input=json.dumps(samples),text=True,cwd=app)
        self.assertEqual(json.loads(result),[mask_credentials(text) for text in samples])

    def test_detail_scan_preview_and_intake_reject_or_mask(self):
        fixture=test_backend.BackendTests();fixture.setUp()
        self.addCleanup(fixture.tearDown);app=fixture.app
        encoded=f'<string name="pass&#119;ord">{XML_VALUE}<b>{XML_VALUE}</b></string>'
        for body in (XML,URI,encoded):
            with closing(sqlite3.connect(app.catalog)) as db:
                db.execute('UPDATE skills SET body=? WHERE name=?',(body,'alpha'));db.commit()
            detail=app.resources({'kind':'skill'},detail='alpha')
            serialized=json.dumps(detail)
            self.assertNotIn(XML_VALUE,serialized);self.assertNotIn(URI_VALUE,serialized)
            scanned=app.sources._scan_catalog_security()
            row=next(row for row in scanned['items'] if row['kind']=='skill' and row['id']=='alpha')
            self.assertIn('REDACTED_CREDENTIAL_REVIEW',row['checks'])
            self.assertEqual(row['default_disposition'],'needs_source_review')
            for operation in ('resource.import','resource.import_batch'):
                payload=({'operation':operation,'kind':'prompt','name':'new-prompt','content':body,'expected_sha256':hashlib.sha256(b'').hexdigest()}
                         if operation=='resource.import' else {'operation':operation,'items':[{'kind':'prompt','name':'new-prompt','content':body}]})
                with self.assertRaises(ValueError):app.operation('preview',payload)

    def test_native_normalize_and_boot_reject_structured_secrets(self):
        app=Path(__file__).resolve().parents[2]/'deploy/skill-hub/app'
        code='''import json,pathlib,tempfile,publish_batch as pub
from test_publish_batch import make_export
results=[]
for content in json.loads(input()):
    payload=make_export([dict(kind="prompt",name="synthetic",content=content)])
    for target in ("normalize","boot"):
        try:
            if target=="normalize":pub.normalize(payload)
            else:
                with tempfile.TemporaryDirectory() as folder:
                    pathlib.Path(folder,payload["batch_id"]+".json").write_text(json.dumps(payload))
                    pub.load_all(folder)
            results.append(False)
        except ValueError:results.append(True)
print(json.dumps(results))'''
        encoded=f'<string name="pass&#119;ord">{XML_VALUE}<b>{XML_VALUE}</b></string>'
        result=subprocess.check_output([sys.executable,'-B','-c',code],input=json.dumps([XML,URI,encoded]),text=True,cwd=app)
        self.assertEqual(json.loads(result),[True]*6)

    def test_preexisting_staged_unsafe_exports_are_rejected(self):
        fixture=test_backend.BackendTests();fixture.setUp()
        self.addCleanup(fixture.tearDown);app=fixture.app
        plan=app.operation('preview',{'operation':'resource.import_batch','items':[{'kind':'prompt','name':'legacy-export','content':'safe fixture'}]})
        app.operation('apply',{'plan_id':plan['plan_id'],'confirmed':True})
        encoded=f'<string name="pass&#119;ord">{XML_VALUE}<b>{XML_VALUE}</b></string>'
        for body in (XML,URI,encoded):
            with closing(sqlite3.connect(batch_intake.db_path(app))) as db:
                db.execute('UPDATE items SET content=?,version=?',(body,hashlib.sha256(body.encode()).hexdigest()));db.commit()
            with self.assertRaises(ValueError):batch_intake.export(app,'prompt','legacy-export',public_text)
            with self.assertRaises(ValueError):batch_intake.export_batch(app,plan['batch_id'],public_text)

    def test_detail_masks_complete_structures_before_output_truncation(self):
        fixture=test_backend.BackendTests();fixture.setUp()
        self.addCleanup(fixture.tearDown);app=fixture.app
        for secret,body in ((XML_VALUE,'x'*262110+XML),(URI_VALUE,'x'*262110+' '+URI)):
            with closing(sqlite3.connect(app.catalog)) as db:
                db.execute('UPDATE skills SET body=? WHERE name=?',(body,'alpha'));db.commit()
            detail=app.resources({'kind':'skill'},detail='alpha')['item']['body']
            self.assertNotIn(secret,detail)
            self.assertLessEqual(len(detail),262144)


if __name__=='__main__':unittest.main()
