import asyncio
import json
import threading
import time
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from report_workbench import engine
from report_workbench.app import create_app, export_html


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        yield client


@pytest.fixture
def owner():
    return {"X-Workspace-Id": str(uuid4())}


def payload(content="主题,分钟\nPython,30\n阅读,60\n", kind="csv", mode="rules"):
    return {"title": "学习记录", "mode": mode, "sources": [{"name": "记录." + kind, "kind": kind, "content": content}]}


def submit(client, owner, data=None):
    response = client.post("/api/jobs", headers=owner, json=data or payload())
    assert response.status_code == 202, response.text
    return response.json()["id"]


def finished(client, owner, job_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        item = client.get(f"/api/jobs/{job_id}", headers=owner).json()
        if item["status"] in {"completed", "failed", "cancelled"}:
            return item
        time.sleep(.01)
    pytest.fail("job did not finish")


def test_statistics_change_with_input_and_missing_cells():
    first = engine.analyze_source(engine.Source(**payload()["sources"][0]))
    second = engine.analyze_source(engine.Source(name="other.csv", kind="csv", content="主题,分钟\nPython,30\n阅读,90\n练习,\n"))
    one, two = first["analysis"]["columns"][1], second["analysis"]["columns"][1]
    assert (one["total"], one["mean"]) == ("90", "45")
    assert (two["total"], two["mean"], two["missing"]) == ("120", "60", 1)
    assert first["source"]["sha256"] != second["source"]["sha256"]


def test_decimal_statistics_do_not_introduce_binary_rounding():
    source = engine.Source(name="time.csv", kind="csv", content="hours\n0.1\n0.2\n")
    assert engine.analyze_csv(source)["columns"][0]["total"] == "0.3"


def test_document_mode_quotes_actual_input_and_discloses_limits(client, owner):
    text = "# 开源阅读\n\n本周研究了命令行参数解析。\n\n还未验证安装失败的情况。"
    job = finished(client, owner, submit(client, owner, payload(text, "md")))
    assert job["status"] == "completed"
    assert "本周研究了命令行参数解析" in job["markdown"]
    assert "规则摘录" in job["markdown"] and "不代表全文语义摘要" in job["markdown"]
    assert job["sources"][0]["sha256"] in job["markdown"]


def test_document_heading_without_blank_line_keeps_following_text():
    source = engine.Source(name="notes.md", kind="md", content="# 标题\n不能遗漏的正文。\n\n第二段。")
    result = engine.analyze_document(source)
    assert result['headings'] == ['标题']
    assert result['excerpts'] == ['不能遗漏的正文。', '第二段。']


def test_history_isolation_export_and_progress(client, owner):
    job_id = submit(client, owner)
    job = finished(client, owner, job_id)
    assert job["status"] == "completed", job
    assert len(client.get("/api/jobs", headers=owner).json()) == 1
    stranger = {"X-Workspace-Id": str(uuid4())}
    assert client.get("/api/jobs", headers=stranger).json() == []
    for path in [f"/api/jobs/{job_id}", f"/api/jobs/{job_id}/export/md"]:
        assert client.get(path, headers=stranger).status_code == 404
    assert client.post(f"/api/jobs/{job_id}/cancel", headers=stranger).status_code == 404
    assert client.get(f"/api/jobs/{job_id}/events", params={"workspace": stranger['X-Workspace-Id']}).status_code == 404
    assert client.get(f"/api/jobs/{job_id}/preview", params={"workspace": stranger['X-Workspace-Id']}).status_code == 404
    markdown = client.get(f"/api/jobs/{job_id}/export/md", headers=owner)
    assert markdown.text == job["markdown"]
    assert "attachment" in markdown.headers['content-disposition']
    exported = client.get(f"/api/jobs/{job_id}/export/html", headers=owner)
    assert exported.status_code == 200 and "<table>" in exported.text
    assert "Content-Security-Policy" in exported.text
    events = client.get(f"/api/jobs/{job_id}/events", params={"workspace": owner['X-Workspace-Id']})
    assert "event: progress" in events.text and '"status": "completed"' in events.text


def test_history_persists_without_original_documents(tmp_path, owner):
    with TestClient(create_app(tmp_path)) as client:
        job_id = submit(client, owner)
        finished(client, owner, job_id)
    with TestClient(create_app(tmp_path)) as client:
        saved = client.get(f"/api/jobs/{job_id}", headers=owner).json()
        assert saved['status'] == 'completed'
        assert 'content' not in saved['sources'][0]


@pytest.mark.parametrize("filename,data,status", [
    ("bad.exe", b"plain text", 415), ("empty.txt", b"", 422),
    ("binary.txt", b"a\x00b", 422), ("invalid.txt", b"\xff\xfe", 422),
    ("huge.txt", b"x" * (1024 * 1024 + 1), 413),
    ("bad.csv", b"a,b\n1,2,3\n", 422),
    ("duplicate.csv", b"a,a\n1,2\n", 422),
])
def test_upload_validation(client, owner, filename, data, status):
    response = client.post("/api/uploads", headers=owner, files={"file": (filename, data)})
    assert response.status_code == status


def test_utf8_bom_upload_and_filename_cleaning(client, owner):
    response = client.post("/api/uploads", headers=owner, files={"file": ("../study.csv", b"\xef\xbb\xbfminutes\n15\n")})
    assert response.status_code == 200
    assert response.json()['name'] == 'study.csv'
    assert not response.json()['content'].startswith('\ufeff')


def test_pasted_malformed_csv_fails_without_export(client, owner):
    job_id = submit(client, owner, payload("a,b\n1\n"))
    job = finished(client, owner, job_id)
    assert job['status'] == 'failed' and job['markdown'] == ''
    assert "列数" in job['error']
    assert client.get(f"/api/jobs/{job_id}/export/md", headers=owner).status_code == 409


def test_json_and_total_limits(client, owner):
    assert client.post('/api/jobs', headers=owner, json=payload("中"*400000, 'txt')).status_code == 422
    data=payload('a'*900000, 'txt');data['sources']*=5
    assert client.post('/api/jobs', headers=owner, json=data).status_code == 422
    response=client.post('/api/jobs', headers={**owner,'Content-Type':'application/json'},content=b'x'*(8*1024*1024+1))
    assert response.status_code == 413


def test_model_requires_configuration(client, owner, monkeypatch):
    monkeypatch.delenv('REPORT_MODEL_BASE_URL', raising=False)
    monkeypatch.delenv('REPORT_MODEL_NAME', raising=False)
    assert client.post('/api/jobs', headers=owner, json=payload(mode='model')).status_code == 400


def test_model_http_failure_never_becomes_success(client, owner, monkeypatch):
    monkeypatch.setenv('REPORT_MODEL_BASE_URL', 'https://model.invalid/v1')
    monkeypatch.setenv('REPORT_MODEL_NAME', 'test-model')
    original = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(503, text='private-provider-detail'))
    monkeypatch.setattr(engine.httpx, 'AsyncClient', lambda **kwargs: original(transport=transport, **kwargs))
    job_id = submit(client, owner, payload(mode='model'))
    job = finished(client, owner, job_id)
    assert job['status'] == 'failed' and job['markdown'] == ''
    assert '503' in job['error'] and 'private-provider-detail' not in job['error']
    assert client.get(f'/api/jobs/{job_id}/export/html', headers=owner).status_code == 409


def test_model_success_uses_computed_evidence(client, owner, monkeypatch):
    monkeypatch.setenv('REPORT_MODEL_BASE_URL', 'https://model.invalid/v1')
    monkeypatch.setenv('REPORT_MODEL_NAME', 'test-model')
    seen = []
    def handler(request):
        body=json.loads(request.content)
        seen.append(body)
        assert '"total": "90"' in body['messages'][1]['content']
        return httpx.Response(200,json={'choices':[{'message':{'content':'依据记录.csv，投入总计 90 分钟。'}}]})
    original=httpx.AsyncClient
    monkeypatch.setattr(engine.httpx,'AsyncClient',lambda **kwargs: original(transport=httpx.MockTransport(handler),**kwargs))
    job=finished(client,owner,submit(client,owner,payload(mode='model')))
    assert job['status']=='completed' and seen
    assert '模型辅助解读' in job['markdown'] and '投入总计 90 分钟' in job['markdown']


def test_cancel_running_model_is_terminal(client, owner, monkeypatch):
    monkeypatch.setenv('REPORT_MODEL_BASE_URL', 'https://model.invalid/v1')
    monkeypatch.setenv('REPORT_MODEL_NAME', 'test-model')
    started=threading.Event()
    async def pending_model(*args):
        started.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(engine, 'call_model', pending_model)
    job_id=submit(client,owner,payload(mode='model'))
    assert started.wait(2)
    response=client.post(f'/api/jobs/{job_id}/cancel',headers=owner)
    assert response.status_code==200
    job=finished(client,owner,job_id)
    assert job['status']=='cancelled' and job['markdown']==''
    assert client.get(f'/api/jobs/{job_id}/export/md',headers=owner).status_code==409
    assert client.post(f'/api/jobs/{job_id}/cancel',headers=owner).status_code==409


def test_export_escapes_html_and_script_in_markdown():
    document=export_html('# Report\n\n<script>alert(1)</script>\n\n[bad](javascript:alert(1))', '<unsafe>')
    assert '<script>' not in document
    assert '<title>&lt;unsafe&gt;</title>' in document
    assert 'href="javascript:' not in document


def test_missing_workspace_is_rejected(client):
    assert client.get('/api/jobs').status_code==400
    assert client.get('/').status_code==200
