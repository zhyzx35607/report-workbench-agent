"""Protocol tests use a fake HTTP transport, never a real model or account."""
import asyncio
import json
import threading
import time
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from report_workbench import agent, engine
from report_workbench.app import create_app


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv('REPORT_MODEL_BASE_URL', 'https://model.invalid/v1')
    monkeypatch.setenv('REPORT_MODEL_NAME', 'protocol-test-model')


@pytest.fixture
def client(tmp_path, configured):
    with TestClient(create_app(tmp_path)) as value:
        yield value


@pytest.fixture
def owner():
    return {'X-Workspace-Id': str(uuid4())}


def request_data():
    return {'title': '两周学习对比', 'mode': 'agent', 'instruction': '比较两份记录中的 minutes 合计，引用输入资料。',
            'sources': [{'name': 'week1.csv', 'kind': 'csv', 'content': 'topic,minutes\nPython,30\nReading,60\n'},
                        {'name': 'week2.csv', 'kind': 'csv', 'content': 'topic,minutes\nPython,60\nReading,90\n'}]}


def call(name, args, call_id='call-1'):
    return {'id': call_id, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}


def gateway(monkeypatch, messages):
    requests = []
    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        response = messages[len(requests) - 1]
        message = response(body) if callable(response) else response
        return httpx.Response(200, json={'choices': [{'message': message}]})
    original = httpx.AsyncClient
    monkeypatch.setattr(engine.httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    return requests


def run_job(client, owner):
    response=client.post('/api/jobs',headers=owner,json=request_data())
    assert response.status_code == 202, response.text
    return response.json()['id']


def finish(client, owner, job_id):
    deadline=time.monotonic()+5
    while time.monotonic()<deadline:
        job=client.get(f'/api/jobs/{job_id}',headers=owner).json()
        if job['status'] in {'completed','failed','cancelled'}:
            return job
        time.sleep(.01)
    pytest.fail('Agent did not terminate')


def test_multiround_agent_inspects_compares_and_finishes(client, owner, monkeypatch):
    def final(body):
        results=[json.loads(m['content']) for m in body['messages'] if m['role']=='tool']
        values=results[-1]['values']
        assert [v['value'] for v in values] == ['90','150']
        assert values[1]['delta_from_first']=='60'
        assert 'private-reasoning-marker' not in json.dumps(body)
        assert len(body['tools'])==3 and body['tool_choice']=='auto'
        return {'content':'week1.csv 共 90 分钟，week2.csv 共 150 分钟，相差 60 分钟。', 'reasoning_content':'private-reasoning-marker'}
    seen=gateway(monkeypatch,[
        {'tool_calls':[call('list_sources',{})], 'content':'private-reasoning-marker'},
        {'tool_calls':[call('inspect_source',{'source_id':1},'call-2'),call('inspect_source',{'source_id':2},'call-3')]},
        {'tool_calls':[call('compare_metrics',{'source_ids':[1,2],'metric':'total','column':'minutes'},'call-4')]},
        final,
    ])
    job_id=run_job(client,owner)
    job=finish(client,owner,job_id)
    assert job['status']=='completed',job
    assert len(seen)==4
    assert '相差 60 分钟' in job['markdown'] and 'Agent 执行记录' in job['markdown']
    assert 'private-reasoning-marker' not in json.dumps(job,ensure_ascii=False)
    completed=[step for step in job['trace'] if step['kind']=='tool' and step['status']=='completed']
    assert [step['tool'] for step in completed]==['list_sources','inspect_source','inspect_source','compare_metrics']
    exported=client.get(f'/api/jobs/{job_id}/export/md',headers=owner).text
    assert 'compare_metrics' in exported and '#2=150' in exported
    other={'X-Workspace-Id':str(uuid4())}
    assert client.get(f'/api/jobs/{job_id}',headers=other).status_code==404


@pytest.mark.parametrize('tool,args',[
    ('run_sql',{'sql':'SELECT 1'}),
    ('inspect_source',{'source_id':True}),
    ('inspect_source',{'source_id':'1'}),
    ('inspect_source',{'source_id':3}),
    ('inspect_source',{'source_id':1,'path':'/tmp/arbitrary'}),
    ('compare_metrics',{'source_ids':[1,1],'metric':'rows'}),
    ('compare_metrics',{'source_ids':[1,2],'metric':'total'}),
    ('compare_metrics',{'source_ids':[1,2],'metric':'total','column':'not-a-column'}),
])
def test_invalid_tool_or_arguments_fail_closed(client,owner,monkeypatch,tool,args):
    seen=gateway(monkeypatch,[{'tool_calls':[call(tool,args)]}])
    job_id=run_job(client,owner)
    job=finish(client,owner,job_id)
    assert job['status']=='failed' and not job['markdown']
    assert len(seen)==1
    assert any(step['status']=='failed' for step in job['trace'])
    assert client.get(f'/api/jobs/{job_id}/export/md',headers=owner).status_code==409


def test_malformed_argument_json_fails(client,owner,monkeypatch):
    invalid=call('inspect_source',{'source_id':1});invalid['function']['arguments']='{"source_id":'
    gateway(monkeypatch,[{'tool_calls':[invalid]}])
    job=finish(client,owner,run_job(client,owner))
    assert job['status']=='failed' and '参数无效' in job['error']


def test_model_round_limit_is_enforced(client,owner,monkeypatch):
    # Six distinct legal actions reach the round budget without triggering repeat detection.
    actions=[('list_sources',{}),('inspect_source',{'source_id':1}),('inspect_source',{'source_id':2})]
    actions += [('compare_metrics',{'source_ids':[1,2],'metric':metric,'column':'minutes'}) for metric in ('total','mean','maximum')]
    replies=[{'tool_calls':[call(name,args,f'call-{i}')]} for i,(name,args) in enumerate(actions)]
    seen=gateway(monkeypatch,replies)
    job=finish(client,owner,run_job(client,owner))
    assert job['status']=='failed' and '6 轮上限' in job['error']
    assert len(seen)==6 and job['markdown']==''


def test_per_round_tool_limit_is_enforced(client,owner,monkeypatch):
    gateway(monkeypatch,[{'tool_calls':[call('list_sources',{},f'call-{i}') for i in range(4)]}])
    job=finish(client,owner,run_job(client,owner))
    assert job['status']=='failed' and '最多允许 3' in job['error']
    assert not any(step['kind']=='tool' for step in job['trace'])


def test_reused_call_id_is_rejected(client,owner,monkeypatch):
    gateway(monkeypatch,[{'tool_calls':[call('inspect_source',{'source_id':1})]}, {'tool_calls':[call('inspect_source',{'source_id':2})]}])
    job=finish(client,owner,run_job(client,owner))
    assert job['status']=='failed' and '唯一 ID' in job['error']


def test_final_without_evidence_is_not_accepted(client,owner,monkeypatch):
    gateway(monkeypatch,[{'content':'一切良好。'}])
    job=finish(client,owner,run_job(client,owner))
    assert job['status']=='failed' and '未检查任何证据' in job['error']


def test_transport_failure_is_terminal_and_does_not_leak_provider_body(client,owner,monkeypatch):
    original=httpx.AsyncClient
    transport=httpx.MockTransport(lambda request: httpx.Response(500,text='sensitive-provider-detail'))
    monkeypatch.setattr(engine.httpx,'AsyncClient',lambda **kwargs: original(transport=transport,**kwargs))
    job=finish(client,owner,run_job(client,owner))
    assert job['status']=='failed' and '500' in job['error']
    assert 'sensitive-provider-detail' not in json.dumps(job)


@pytest.mark.parametrize('phase',['model','tool'])
def test_cancellation_stops_agent_model_and_tool_phases(client,owner,monkeypatch,phase):
    entered=threading.Event()
    calls=[]
    async def waiting_model(*args,**kwargs):
        calls.append('model')
        if phase=='tool':
            return {'tool_calls':[call('inspect_source',{'source_id':1})]}
        entered.set()
        await asyncio.Event().wait()
    async def waiting_tool(*args):
        calls.append('tool')
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(engine,'request_message',waiting_model)
    monkeypatch.setattr(agent,'execute_tool',waiting_tool)
    job_id=run_job(client,owner)
    assert entered.wait(2)
    assert client.post(f'/api/jobs/{job_id}/cancel',headers=owner).status_code==200
    job=finish(client,owner,job_id)
    assert job['status']=='cancelled' and job['markdown']==''
    assert job['trace'][-1]['kind']=='cancel'
    assert calls==(['model','tool'] if phase=='tool' else ['model'])
    assert client.get(f'/api/jobs/{job_id}/export/html',headers=owner).status_code==409


def test_context_budget_stops_before_model_request(client,owner,monkeypatch):
    monkeypatch.setattr(agent,'MAX_CONTEXT_CHARS',10)
    job=finish(client,owner,run_job(client,owner))
    assert job['status']=='failed' and '上下文' in job['error']


def test_tool_result_budget_rejects_oversized_evidence():
    items=[{'source':{'name':'big.txt','kind':'txt','sha256':'x','bytes':25000},'analysis':{'type':'document','characters':25000,'excerpts':['a'*25000]}}]
    with pytest.raises(ValueError,match='24000'):
        asyncio.run(agent.execute_tool('inspect_source','{"source_id":1}',items))


def test_agent_html_contains_three_well_formed_mode_options(client):
    page=client.get('/').text
    for mode in ('rules','model','agent'):
        assert f'<option value="{mode}"' in page
    assert 'id="agent-trace"' in page
