import os,sqlite3,statistics,time
import catalog_ingest as ci
import rust_refs

con=sqlite3.connect('file:'+os.environ.get('SKILL_HUB_TEST_DB','/tmp/source.db')+'?mode=ro',uri=True)
texts=[r[0] for r in con.execute('select body from skills where length(body)>0')]
texts += [r[0] for r in con.execute('select body from prompts where length(body)>0')]
hub = os.environ.get('SKILL_HUB_TEST_HUB')
if hub:
    from pathlib import Path
    for (name,) in con.execute("select name from quarantine where kind='skill'"):
        path = Path(hub) / 'skills' / name / 'SKILL.md'
        if path.is_file():
            texts.append(path.read_text(encoding='utf-8', errors='replace'))
native=rust_refs._scan
assert native is not None
results={};times={'python':[],'rust':[]}
for iteration in range(4):
    order=['python','rust'] if iteration%2==0 else ['rust','python']
    for mode in order:
        rust_refs._scan=native if mode=='rust' else None
        start=time.perf_counter(); value=[ci.find_referenced_paths(t) for t in texts]
        times[mode].append((time.perf_counter()-start)*1000)
        if not results:results['baseline']=value
        assert results['baseline']==value,mode
print('texts',len(texts),'full parity passed')
for mode,values in times.items():print(mode,'median_ms',round(statistics.median(values),2),'samples',*[round(v,2) for v in values])
