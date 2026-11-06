import asyncio
import io
import json
import threading
import time
from uuid import uuid4
from zipfile import ZipFile

import pytest
import httpx
from fastapi.testclient import TestClient

from report_workbench import agent, engine
from report_workbench.app import create_app
from report_workbench.charts import render_charts
from report_workbench.store import Store


def payload(**changes):
    return {'title':'Test report', 'mode':'rules', 'sources':[{'name':'records.csv', 'kind':'csv', 'content':'label,minutes\nA,30\nB,60\nC,\nD,-10\n'}], **changes}


@pytest.fixture
def owner(): return {'X-Workspace-Id':str(uuid4())}


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path)) as session: yield session


def finished(client, owner, job_id):
    end=time.monotonic()+5
    while time.monotonic()<end:
        job=client.get('/api/jobs/'+job_id,headers=owner).json()
        if job['status'] in {'completed','failed','cancelled'}:return job
        time.sleep(.01)
    pytest.fail('job did not terminate')


def submit(client,owner,**changes):
    response=client.post('/api/jobs',headers=owner,json=payload(**changes))
    assert response.status_code==202,response.text
    return response.json()['id']


@pytest.mark.parametrize('template,section', [('study','本次记录概况'),('comparison','同名数值列对照'),('overview','覆盖范围')])
def test_templates_change_goal_and_report_sections(client,owner,template,section):
    job=finished(client,owner,submit(client,owner,template=template))
    assert job['status']=='completed' and job['template']==template
    assert section in job['markdown'] and engine.TEMPLATES[template]['goal'] in job['markdown']
    assert engine.TEMPLATES[template]['next'] in job['markdown']
    assert job['evidence'][0]['analysis']['columns'][1]['median']=='30'


def test_chart_values_missing_negative_and_export_match(client,owner):
    job_id=submit(client,owner)
    job=finished(client,owner,job_id)
    chart=job['evidence'][0]['analysis']['charts'][0]
    assert chart['points']==[{'row':1,'value':'30'},{'row':2,'value':'60'},{'row':3,'value':None},{'row':4,'value':'-10'}]
    svg=render_charts(job['evidence'])
    assert '行 4：-10' in svg and '<rect ' in svg and '<circle ' in svg
    # The missing third value breaks the line; only rows 1 -> 2 are connected.
    assert svg.count('stroke-width="2"')==1
    preview=client.get(f'/api/jobs/{job_id}/preview',params={'workspace':owner['X-Workspace-Id']}).text
    exported=client.get(f'/api/jobs/{job_id}/export/html',headers=owner).text
    assert preview==exported and svg in exported
    assert '| 3 | 缺失 |' in job['markdown']


def test_chart_labels_escape_markup_and_point_budget():
    source=engine.Source(name='<img src=x onerror=alert(1)>.csv',kind='csv',content='"<script>alert(1)</script>"\n'+'1\n'*40)
    evidence=[engine.analyze_source(source)]
    chart=evidence[0]['analysis']['charts'][0]
    assert len(chart['points'])==24 and chart['rows']==40
    svg=render_charts(evidence)
    assert '<script>' not in svg and '<img ' not in svg
    assert '&lt;script&gt;' in svg and '24 / 40' in svg


def test_batch_zip_safe_paths_and_content_and_isolation(client,owner):
    batch=client.post('/api/batches',headers=owner,json=payload(title='../../unsafe',templates=['study','comparison','overview'])).json()
    jobs=[finished(client,owner,j) for j in batch['job_ids']]
    assert all(job['status']=='completed' for job in jobs)
    detail=client.get('/api/batches/'+batch['id'],headers=owner).json()
    assert detail['terminal'] and detail['completed']==3
    response=client.get(f"/api/batches/{batch['id']}/export/zip",headers=owner)
    with ZipFile(io.BytesIO(response.content)) as archive:
        assert len(archive.namelist())==7
        assert all('/' not in name and '\\' not in name and '..' not in name for name in archive.namelist())
        manifest=json.loads(archive.read('manifest.json'))
        assert len(manifest['jobs'])==3
        for job in jobs:
            assert archive.read(job['id']+'.md').decode()==job['markdown']
            html=client.get(f"/api/jobs/{job['id']}/export/html",headers=owner).content
            assert archive.read(job['id']+'.html')==html
    stranger={'X-Workspace-Id':str(uuid4())}
    for path in [f"/api/batches/{batch['id']}",f"/api/batches/{batch['id']}/export/zip"]:
        assert client.get(path,headers=stranger).status_code==404
    assert client.get('/api/batches',headers=stranger).json()==[]


def test_queue_cancellation_does_not_start_queued_job_and_failed_job_isolated(client,owner,monkeypatch):
    entered=threading.Event()
    calls=[]
    original=engine.compose_report
    async def wait_model(*args):
        entered.set();await asyncio.Event().wait()
    monkeypatch.setenv('REPORT_MODEL_BASE_URL','http://local.invalid/v1');monkeypatch.setenv('REPORT_MODEL_NAME','test')
    monkeypatch.setattr(engine,'call_model',wait_model)
    running=submit(client,owner,mode='model')
    assert entered.wait(2)
    def compose(request,*args):
        calls.append(request.template)
        if request.template=='comparison':raise ValueError('comparison fixture failure')
        return original(request,*args)
    monkeypatch.setattr(engine,'compose_report',compose)
    batch=client.post('/api/batches',headers=owner,json=payload(templates=['study','comparison','overview'])).json()
    queued=batch['job_ids'][0]
    assert client.get('/api/jobs/'+queued,headers=owner).json()['status']=='queued'
    assert client.get(f"/api/batches/{batch['id']}/export/zip",headers=owner).status_code==409
    assert client.post('/api/jobs/'+queued+'/cancel',headers=owner).status_code==200
    assert client.post('/api/jobs/'+running+'/cancel',headers=owner).status_code==200
    jobs=[finished(client,owner,j) for j in batch['job_ids']]
    assert [j['status'] for j in jobs]==['cancelled','failed','completed']
    assert calls==['comparison','overview']
    zipped=client.get(f"/api/batches/{batch['id']}/export/zip",headers=owner)
    with ZipFile(io.BytesIO(zipped.content)) as archive:
        manifest=json.loads(archive.read('manifest.json'))
        assert [len(job['files']) for job in manifest['jobs']]==[0,0,2]


def test_batch_validates_templates_and_queue_capacity(client,owner,monkeypatch):
    assert client.post('/api/batches',headers=owner,json=payload(templates=['study','study'])).status_code==422
    assert client.post('/api/batches',headers=owner,json=payload(templates=['unknown'])).status_code==422
    async def waiting(*args):await asyncio.Event().wait()
    monkeypatch.setenv('REPORT_MODEL_BASE_URL','http://local.invalid/v1');monkeypatch.setenv('REPORT_MODEL_NAME','test')
    monkeypatch.setattr(engine,'call_model',waiting)
    for _ in range(3):
        assert client.post('/api/batches',headers=owner,json=payload(mode='model',templates=['study','comparison','overview'])).status_code==202
    assert client.post('/api/jobs',headers=owner,json=payload()).status_code==429


def test_restart_cancels_unfinished_batches_and_preserves_evidence(tmp_path,owner):
    with TestClient(create_app(tmp_path)) as client:
        completed=finished(client,owner,submit(client,owner))
    store=Store(tmp_path)
    pending_id=str(uuid4());batch_id=str(uuid4())
    store.create_batch(batch_id,owner['X-Workspace-Id'],'waiting',[(pending_id,engine.ReportRequest(**payload()))])
    with TestClient(create_app(tmp_path)) as client:
        assert client.get('/api/jobs/'+pending_id,headers=owner).json()['status']=='cancelled'
        assert client.get('/api/jobs/'+completed['id'],headers=owner).json()['evidence']==completed['evidence']
        assert client.get('/api/batches/'+batch_id,headers=owner).json()['terminal']


def test_comparison_uses_persisted_facts_and_enforces_scope(client,owner):
    left=finished(client,owner,submit(client,owner))
    right=finished(client,owner,submit(client,owner,sources=[{'name':'records.csv','kind':'csv','content':'label,minutes\nA,100\nB,20\n'}]))
    result=client.get('/api/compare',headers=owner,params={'left':left['id'],'right':right['id']}).json()
    total=next(row for row in result['metrics'] if row['metric']=='total')
    assert (total['left'],total['right'],total['delta'])==('80','120','40')
    assert result['sources'][0]['status']=='changed'
    assert client.get('/api/compare',headers={'X-Workspace-Id':str(uuid4())},params={'left':left['id'],'right':right['id']}).status_code==404


def test_no_progress_guard_and_trace_observability(client,owner,monkeypatch):
    monkeypatch.setenv('REPORT_MODEL_BASE_URL','http://local.invalid/v1');monkeypatch.setenv('REPORT_MODEL_NAME','test')
    count=0
    async def repeated(*args,**kwargs):
        nonlocal count
        count+=1
        return {'tool_calls':[{'id':str(count),'type':'function','function':{'name':'inspect_source','arguments':'{ "source_id" : 1 }' if count==1 else '{"source_id":1}'}}], '_usage':None}
    monkeypatch.setattr(engine,'request_message',repeated)
    job=finished(client,owner,submit(client,owner,mode='agent'))
    assert job['status']=='failed' and count==2
    assert len([step for step in job['trace'] if step['kind']=='tool' and step['status']=='completed'])==1
    guard=next(step for step in job['trace'] if step['kind']=='loop_guard')
    assert guard['failure_type']=='no_progress' and len(guard['evidence_fingerprint'])==64
    exported=client.get('/api/jobs/'+job['id']+'/trace',headers=owner).json()
    assert exported['trace']==job['trace']
    models=[step for step in job['trace'] if step['kind']=='model' and step['status']=='completed']
    assert all(step['duration_ms']>=0 and step['usage'] is None for step in models)
    assert client.get('/api/jobs/'+job['id']+'/trace',headers={'X-Workspace-Id':str(uuid4())}).status_code==404


@pytest.mark.parametrize('usage,expected', [(None,None),({'prompt_tokens':12,'completion_tokens':4,'total_tokens':16},{'prompt_tokens':12,'completion_tokens':4,'total_tokens':16}),({'total_tokens':'16','prompt_tokens':-1,'completion_tokens':True},None)])
def test_usage_only_accepts_provider_numeric_counts(monkeypatch,usage,expected):
    monkeypatch.setenv('REPORT_MODEL_BASE_URL','http://model.invalid/v1');monkeypatch.setenv('REPORT_MODEL_NAME','fixture')
    original=httpx.AsyncClient
    transport=httpx.MockTransport(lambda request:httpx.Response(200,json={'choices':[{'message':{'content':'public answer','reasoning_content':'do-not-persist'}}],'usage':usage}))
    monkeypatch.setattr(engine.httpx,'AsyncClient',lambda **kwargs:original(transport=transport,**kwargs))
    assert asyncio.run(engine.request_message([]))['_usage']==expected
