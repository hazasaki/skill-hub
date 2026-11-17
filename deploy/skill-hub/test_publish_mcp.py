"""Isolated network-none smoke of real serve.py and MCP search/load APIs."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.request

import publish_batch as pub
import skill_index as si
from test_publish_batch import make_export
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

async def exercise():
    async with streamable_http_client('http://127.0.0.1:8765/mcp') as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            for tool,args,expected in [('search_skills',{'query':'quasar'},'new-skill'),
                                       ('search_prompts',{'query':'quasar'},'new-prompt'),
                                       ('get_skill',{'name':'new-skill'},'new-skill'),
                                       ('get_prompt',{'name':'new-prompt'},'new-prompt')]:
                result=await session.call_tool(tool,args)
                assert not result.isError, (tool,result)
                payload=json.loads(result.content[0].text)
                assert 'error' not in payload and expected in json.dumps(payload), (tool,payload)
                print(tool,'OK', flush=True)

def main():
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp); hub=root/'hub'; (hub/'skills'/'base').mkdir(parents=True); (hub/'prompts').mkdir(); (hub/'state').mkdir()
        (hub/'skills'/'base'/'SKILL.md').write_text('---\nname: base\ndescription: base skill\n---\n# Base\n')
        si.build(hub)
        export=make_export([{'kind':'skill','name':'new-skill','content':'---\nname: new-skill\ndescription: unique quasar skill\n---\n# Quasar\n'}, {'kind':'prompt','name':'new-prompt','content':'# Quasar prompt'}])
        directory=root/'published';pub.publish(export,directory,hub/'state'/'skill-index.db',hub)
        env=dict(os.environ,SKILL_HUB_ROOT=str(hub),SKILL_HUB_INDEX_PATH=str(root/'live.sqlite'),
                 SKILL_HUB_MATERIALIZE_ROOT=str(root/'materialized'),SKILL_HUB_PUBLICATIONS_DIR=str(directory),
                 SKILL_HUB_DISABLE_BROKER='1',SKILL_HUB_PATH_MAP='{}')
        proc=subprocess.Popen([sys.executable,'-B',str(Path(__file__).resolve().with_name('serve.py'))],env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
        try:
            for _ in range(150):
                if proc.poll() is not None: raise RuntimeError(proc.stdout.read().decode(errors='replace'))
                try:
                    urllib.request.urlopen('http://127.0.0.1:8765/health',timeout=0.2)
                except urllib.error.HTTPError: break
                except OSError: time.sleep(.1); continue
                break
            else: raise RuntimeError('server failed to start')
            try: urllib.request.urlopen('http://127.0.0.1:8765/health')
            except urllib.error.HTTPError as error: health=json.loads(error.read())
            else: raise AssertionError('expected no semantic configuration')
            assert health['publication']=={'published_skills':1,'published_prompts':1},health
            assert health['effective_catalog_sha256'] != health['catalog_sha256'],health
            assert 'baseline snapshot' in health['projection_scope'],health
            print('startup overlay and health pin OK',flush=True)
            asyncio.run(exercise())
        finally:
            proc.terminate()
            try: proc.wait(timeout=5)
            except subprocess.TimeoutExpired: proc.kill(); proc.wait()

if __name__=='__main__': main()
