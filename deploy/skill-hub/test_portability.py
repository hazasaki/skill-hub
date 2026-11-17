import ast
import contextlib
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
BASE_SOURCE = Path(os.environ.get('SKILL_HUB_TEST_BASE_SOURCE', ROOT.parents[1] / 'agent-memory'))
sys.path.insert(0, str(BASE_SOURCE))
sys.path.insert(0, str(ROOT / 'app'))
os.environ.setdefault('SKILL_HUB_ROOT', str(ROOT / 'tmp/hub'))
os.environ.setdefault('SKILL_HUB_OLLAMA_ENDPOINT', 'http://host.docker.internal:11434')
import portable_paths as paths
import retrieval_fusion as fusion
import retrieval_semantic as semantic
import skill_index as si
import skill_mcp as server

class FakeMCP:
    def __init__(self, name):
        self.tools = {}
        self.settings = types.SimpleNamespace()
        self.transport = None
    def tool(self):
        def register(fn):
            self.tools[fn.__name__] = fn
            return fn
        return register
    def run(self, *, transport): self.transport = transport

class PortabilityTests(unittest.TestCase):
    def setUp(self):
        temp_root = ROOT / 'tmp'; temp_root.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=temp_root)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.hub = self.root / 'hub'; self.hub.mkdir()
        self.mapping = {'Z:/hub': str(self.hub), 'Z:/release': str(self.root/'release')}
        if os.name == 'nt': self.mapping[str(self.root)] = str(self.root)
        self.env = patch.dict(os.environ, {'SKILL_HUB_PATH_MAP':json.dumps(self.mapping)})
        self.env.start(); self.addCleanup(self.env.stop)
        for name in ['socket.create_connection','socket.socket.connect','urllib.request.urlopen']:
            blocker=patch(name,side_effect=AssertionError('Real network forbidden'))
            blocker.start();self.addCleanup(blocker.stop)
        for module in (paths,fusion,semantic,si,server):
            self.assertEqual(Path(module.__file__).resolve().parent,ROOT/'app')
    def test_slash_backslash_case_and_longest_prefix(self):
        self.assertEqual(paths.portable_path(r'z:\hub\prompts\a.md'), self.hub/'prompts/a.md')
        self.assertEqual(paths.portable_path('Z:/hub/prompts/a.md'), self.hub/'prompts/a.md')
        self.mapping['Z:/hub/special']=str(self.root/'special')
        with patch.dict(os.environ,{'SKILL_HUB_PATH_MAP':json.dumps(self.mapping)}):
            self.assertEqual(paths.portable_path('Z:/hub/special/a'),self.root/'special/a')
    def test_unknown_sibling_traversal_and_relative_drive_rejected(self):
        for value in [r'Z:\hub-other\a',r'Z:\hub\..\secret',r'Z:hub\a',r'Q:\other\a',r'\rooted\a']:
            with self.subTest(value=value),self.assertRaises(ValueError):paths.portable_path(value)
    def test_native_path_unchanged(self):
        self.assertEqual(paths.portable_path(self.hub),self.hub)
    def test_config_paths_remap_without_changing_bytes_or_pins(self):
        cfg=self.root/'config.json'
        data={'catalog':{'path':r'Z:\hub\state\skill-index.db','sha256':'a'*64},'semantic':{'path':r'Z:\release\semantic.db','sha256':'b'*64,'embedding_model_digest':'pinned'},'quota':{'skills':200,'prompts':50}}
        cfg.write_text(json.dumps(data),encoding='utf-8');before=cfg.read_bytes()
        got=fusion.load_config(cfg)
        self.assertEqual(got['catalog']['path'],str(self.hub/'state/skill-index.db'))
        self.assertEqual(got['semantic']['path'],str(self.root/'release/semantic.db'))
        self.assertEqual(got['semantic']['sha256'],data['semantic']['sha256'])
        self.assertEqual(got['semantic']['embedding_model_digest'],'pinned')
        self.assertEqual(got['quota'],data['quota']);self.assertEqual(cfg.read_bytes(),before)
    def test_artifact_hash_still_enforced(self):
        artifact=self.root/'release/vector.db';artifact.parent.mkdir();artifact.write_bytes(b'fixture')
        cfg=self.root/'config.json';cfg.write_text(json.dumps({'semantic':{'path':r'Z:\release\vector.db','sha256':'0'*64}}))
        got=fusion.load_config(cfg);bound=fusion.bind_artifacts(got)
        self.assertFalse(bound['artifacts']['semantic']['match'])
    def test_prompt_query_maps_persisted_path_and_leaves_db_unchanged(self):
        (self.hub/'state').mkdir();db=self.hub/'state/skill-index.db'
        con=sqlite3.connect(db)
        con.execute('CREATE TABLE prompts(name,description,tags,prompt_type,files,path,active)')
        con.execute('CREATE VIRTUAL TABLE fts_prompts USING fts5(name,description,tags,body)')
        con.execute('INSERT INTO prompts VALUES (?,?,?,?,?,?,?)',('demo','alpha','alpha','agent_role',1,r'Z:\hub\prompts\demo.md',1))
        con.execute("INSERT INTO fts_prompts VALUES ('demo','alpha','alpha','alpha')")
        con.commit();con.close();before=db.read_bytes()
        rows=si.search_prompts('alpha',hub=self.hub)
        self.assertEqual(rows[0]['path'],str(self.hub/'prompts/demo.md'));self.assertEqual(db.read_bytes(),before)
    def test_get_materializes_data_without_executing(self):
        skill=self.hub/'skills/demo';(skill/'references').mkdir(parents=True)
        (skill/'SKILL.md').write_text('---\nname: demo\ndescription: fixture skill\nrisk: safe\n---\nRead references/info.md\n')
        (skill/'references/info.md').write_text('fixture reference')
        (skill/'scripts').mkdir();(skill/'scripts/probe.py').write_text('raise RuntimeError("must never run")')
        before=si.bundle_digest(skill)
        result=si.get('demo',self.hub,materialize_root=self.root/'materialized')
        self.assertEqual(si.bundle_digest(Path(result['path'])),before)
        self.assertEqual(si.bundle_digest(skill),before)
        self.assertTrue((Path(result['path'])/'references/info.md').is_file())
    def test_gpu_denial_prevents_embedding(self):
        self.assertEqual(semantic.DEFAULT_ENDPOINT,'http://host.docker.internal:11434')
        with patch.object(semantic,'_residency',return_value={'size':100,'size_vram':0,'digest':semantic.DEFAULT_DIGEST}),patch.object(semantic,'load_on_gpu',side_effect=AssertionError('Loaded CPU model must not reload')):
            with self.assertRaises(semantic.SemanticRetrievalError): semantic.embed_bge_m3('fixture')
    def test_missing_index_and_rebuild_refused_before_start(self):
        with patch.object(server.si,'build',side_effect=AssertionError('No build permitted')),patch.object(server,'build_server',side_effect=AssertionError('No startup permitted')),contextlib.redirect_stderr(io.StringIO()):
            for extra in [[],['--rebuild']]:
                with self.assertRaises(SystemExit) as error:server.main(['--hub',str(self.hub),'--require-index']+extra)
                self.assertEqual(error.exception.code,2)
    def test_http_bind_explicit_and_broker_disabled(self):
        (self.hub/'state').mkdir();(self.hub/'state/skill-index.db').write_bytes(b'fixture')
        fake=FakeMCP('fixture')
        with patch.object(server,'FastMCP',return_value=fake),patch.dict(os.environ,{'SKILL_HUB_DISABLE_BROKER':'1'}),patch.object(server.broker,'call_tool',side_effect=AssertionError('No host broker execution')):
            self.assertEqual(server.main(['--hub',str(self.hub),'--http','--host','0.0.0.0','--port','8765','--require-index']),0)
            self.assertEqual(fake.settings.host,'0.0.0.0');self.assertEqual(fake.transport,'streamable-http')
            self.assertEqual(json.loads(fake.tools['call_hub_tool']('computer_use','probe'))['error'],'BROKER_UNAVAILABLE')
    def test_credential_pointer_override_never_reads_secret(self):
        import importlib.util
        target = self.root / 'unread-secret-pointer'
        spec = importlib.util.spec_from_file_location('server_pointer_test', ROOT/'app/skill_mcp.py')
        isolated = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {'SKILL_HUB_KEY_PATH':str(target)}):
            spec.loader.exec_module(isolated)
        self.assertEqual(isolated.LIVE_KEY_PATH,target)
        self.assertFalse(target.exists())
    def test_telemetry_writes_only_separate_root(self):
        target = self.root/'telemetry'
        with patch.dict(os.environ,{'SKILL_HUB_TELEMETRY_ROOT':str(target)}):
            receipt=server._record_route_telemetry({'outcome':'NO_SKILL'},self.hub,server.time.monotonic())
        self.assertEqual(receipt['status'],'RECORDED')
        self.assertTrue(target.exists())
        self.assertFalse((self.hub/'state/telemetry').exists())
    def test_gpu_guard_bodies_match_canonical_source(self):
        if not (BASE_SOURCE/'retrieval_semantic.py').is_file():
            self.skipTest('set SKILL_HUB_TEST_BASE_SOURCE to an independent baseline for parity comparison')
        def funcs(p):return {n.name:ast.dump(n) for n in ast.parse(p.read_text(encoding='utf-8')).body if isinstance(n,ast.FunctionDef)}
        before=funcs(BASE_SOURCE/'retrieval_semantic.py');after=funcs(ROOT/'app/retrieval_semantic.py')
        for name,body in before.items():self.assertEqual(body,after[name],name)

if __name__=='__main__':unittest.main()
