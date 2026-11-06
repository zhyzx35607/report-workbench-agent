from __future__ import annotations

import asyncio
import html
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from markdown_it import MarkdownIt
from pydantic import ValidationError

from . import agent, engine
from .store import Store

STATIC = Path(__file__).parent / "static"
TERMINAL = {"completed", "failed", "cancelled"}


class BodyLimitMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in {"POST", "PUT", "PATCH"}:
            return await self.app(scope, receive, send)
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > 8 * 1024 * 1024:
                await Response("请求超过 8 MiB 上限", status_code=413)(scope, receive, send)
                return
            if not message.get("more_body", False):
                break
        sent = False

        async def replay():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


def normalize_workspace(value: str | None) -> str:
    try:
        return str(UUID(value or ""))
    except (ValueError, AttributeError) as exc:
        raise HTTPException(400, "需要有效的工作区标识") from exc


def export_html(markdown: str, title: str) -> str:
    rendered = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable("table").render(markdown)
    return f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:">
<title>{html.escape(title)}</title><style>body{{font-family:system-ui,sans-serif;max-width:960px;margin:48px auto;padding:0 24px;color:#172a35;line-height:1.75}}table{{border-collapse:collapse;width:100%;font-size:.88rem;display:block;overflow:auto}}td,th{{padding:8px 12px;border:1px solid #ccd8dc;text-align:left}}th{{background:#eef5f5}}pre{{white-space:pre-wrap}}code{{overflow-wrap:anywhere}}blockquote{{border-left:3px solid #2e8b85;padding-left:18px;color:#475d67}}@media print{{body{{margin:0;max-width:none}}table{{font-size:9pt}}}}</style><article>{rendered}</article></html>"""


def create_app(data_dir: Path | None = None) -> FastAPI:
    tasks: dict[str, asyncio.Task] = {}

    @asynccontextmanager
    async def lifespan(app):
        app.state.store = Store(data_dir or Path(os.getenv("REPORT_DATA_DIR", "data")))
        yield
        for task in list(tasks.values()):
            task.cancel()
        if tasks:
            await asyncio.gather(*list(tasks.values()), return_exceptions=True)

    app = FastAPI(title="Report Workbench Agent", lifespan=lifespan)
    app.add_middleware(BodyLimitMiddleware)

    async def process(job_id: str, request: engine.ReportRequest):
        store = app.state.store
        trace = []

        async def record_step(entry):
            trace.append(entry)
            store.update(job_id, trace=json.dumps(trace, ensure_ascii=False), stage=entry['summary'],
                         progress=60 + min(entry['round'], agent.MAX_ROUNDS) * 4)
            await asyncio.sleep(0)
        try:
            store.update(job_id, status="running", stage="读取与校验输入", progress=5)
            evidence = []
            for i, source in enumerate(request.sources):
                store.update(job_id, stage=f"分析 {source.name}", progress=10 + int(i / len(request.sources) * 50))
                evidence.append(await asyncio.to_thread(engine.analyze_source, source))
                await asyncio.sleep(0)
            model_text = None
            if request.mode == "model":
                store.update(job_id, stage="等待模型分析统计与摘录", progress=65)
                model_text = await engine.call_model(request.instruction, evidence)
            elif request.mode == "agent":
                model_text = await agent.run_agent(request.instruction, evidence, record_step)
            store.update(job_id, stage="整理证据与报告", progress=90)
            await asyncio.sleep(0)
            report = engine.compose_report(request, evidence, model_text, trace)
            store.update(job_id, status="completed", stage="报告已保存", progress=100, markdown=report)
        except asyncio.CancelledError:
            if trace:
                trace.append({"round": trace[-1]['round'], "kind": "cancel", "status": "cancelled", "summary": "任务已取消，停止后续模型与工具调用"})
            store.update(job_id, status="cancelled", stage="已取消", error="", trace=json.dumps(trace, ensure_ascii=False))
            raise
        except ValueError as exc:
            if trace:
                trace.append({"round": trace[-1]['round'], "kind": "error", "status": "failed", "summary": str(exc)})
            store.update(job_id, status="failed", stage="处理失败", error=str(exc), trace=json.dumps(trace, ensure_ascii=False))
        except Exception:
            store.update(job_id, status="failed", stage="处理失败", error="内部处理错误，未生成报告")
        finally:
            tasks.pop(job_id, None)

    def owned(job_id, workspace):
        job = app.state.store.get(job_id, workspace)
        if job is None:
            raise HTTPException(404, "找不到该工作区的报告")
        return job

    @app.get("/api/config")
    def config():
        return {"model_available": engine.model_configured(), "max_source_bytes": engine.MAX_SOURCE_BYTES,
                "max_total_bytes": engine.MAX_TOTAL_BYTES, "max_sources": 8, "agent_max_rounds": agent.MAX_ROUNDS}

    @app.post("/api/uploads")
    async def upload(file: UploadFile = File(...), x_workspace_id: str | None = Header(default=None)):
        normalize_workspace(x_workspace_id)
        filename = (file.filename or "").replace("\\", "/").split("/")[-1]
        kind = Path(filename).suffix.lower().lstrip(".")
        if kind not in {"md", "txt", "csv"}:
            await file.close()
            raise HTTPException(415, "只支持 .md、.txt 和 .csv 文件")
        raw = await file.read(engine.MAX_SOURCE_BYTES + 1)
        await file.close()
        if len(raw) > engine.MAX_SOURCE_BYTES:
            raise HTTPException(413, "单个输入不得超过 1 MiB")
        try:
            text = raw.decode("utf-8-sig")
            source = engine.Source(name=filename, kind=kind, content=text)
            if kind == "csv":
                await asyncio.to_thread(engine.analyze_csv, source)
        except UnicodeDecodeError as exc:
            raise HTTPException(422, "文件必须使用 UTF-8 编码") from exc
        except (ValidationError, ValueError) as exc:
            message = "输入无效、为空或超过限制" if isinstance(exc, ValidationError) else str(exc)
            raise HTTPException(422, message) from exc
        return source.model_dump()

    @app.post("/api/jobs", status_code=202)
    async def create_job(request: engine.ReportRequest, x_workspace_id: str | None = Header(default=None)):
        workspace = normalize_workspace(x_workspace_id)
        if request.mode in {"model", "agent"} and not engine.model_configured():
            raise HTTPException(400, "模型模式尚未配置；请使用规则模式或配置模型环境变量")
        job_id = str(uuid4())
        app.state.store.create(job_id, workspace, request.title, request.mode, [engine.metadata(s) for s in request.sources])
        tasks[job_id] = asyncio.create_task(process(job_id, request))
        return {"id": job_id}

    @app.get("/api/jobs")
    def history(x_workspace_id: str | None = Header(default=None)):
        return app.state.store.history(normalize_workspace(x_workspace_id))

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str, x_workspace_id: str | None = Header(default=None)):
        return owned(job_id, normalize_workspace(x_workspace_id))

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel(job_id: str, x_workspace_id: str | None = Header(default=None)):
        workspace = normalize_workspace(x_workspace_id)
        job = owned(job_id, workspace)
        if job["status"] in TERMINAL:
            raise HTTPException(409, "任务已经结束")
        task = tasks.get(job_id)
        if task:
            task.cancel()
        trace = job.get('trace', [])
        if trace:
            trace.append({"round": trace[-1]['round'], "kind": "cancel", "status": "cancelled", "summary": "任务已取消，停止后续模型与工具调用"})
        app.state.store.update(job_id, status="cancelled", stage="已取消", trace=json.dumps(trace, ensure_ascii=False))
        return owned(job_id, workspace)

    @app.get("/api/jobs/{job_id}/events")
    async def events(job_id: str, workspace: str = Query(...)):
        owner = normalize_workspace(workspace)
        owned(job_id, owner)

        async def stream():
            previous = None
            while True:
                job = owned(job_id, owner)
                job.pop("markdown", None)
                text = json.dumps(job, ensure_ascii=False)
                if text != previous:
                    yield f"event: progress\ndata: {text}\n\n"
                    previous = text
                if job["status"] in TERMINAL:
                    break
                await asyncio.sleep(0.2)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/jobs/{job_id}/export/{format}")
    def export(job_id: str, format: str, x_workspace_id: str | None = Header(default=None)):
        job = owned(job_id, normalize_workspace(x_workspace_id))
        if job['status'] != 'completed':
            raise HTTPException(409, "只有完成的报告可以导出")
        if format not in {"md", "html"}:
            raise HTTPException(400, "只支持 md 或 html 导出")
        headers = {"Content-Disposition": f'attachment; filename="report-{job_id[:8]}.{format}"', "X-Content-Type-Options": "nosniff"}
        if format == "md":
            return Response(job['markdown'], media_type="text/markdown; charset=utf-8", headers=headers)
        return HTMLResponse(export_html(job['markdown'], job['title']), headers=headers)

    @app.get("/api/jobs/{job_id}/preview")
    def preview(job_id: str, workspace: str = Query(...)):
        job = owned(job_id, normalize_workspace(workspace))
        if job['status'] != 'completed':
            raise HTTPException(409, "报告尚未完成")
        return HTMLResponse(export_html(job['markdown'], job['title']))

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


app = create_app()
