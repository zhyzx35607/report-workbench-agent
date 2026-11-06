"use strict";
const $ = id => document.getElementById(id);
let workspace = localStorage.getItem("report-workspace") || crypto.randomUUID();
localStorage.setItem("report-workspace", workspace);
let sources = [], activeId = null, stream = null, activeJob = null;
let selectionVersion = 0, historyVersion = 0;
const statusNames = {queued:"等待中", running:"生成中", completed:"已完成", failed:"失败", cancelled:"已取消"};
const modeNames = {rules:"规则模式", model:"直接模型解读", agent:"报告 Agent"};

function notice(message = "") { $("notice").textContent = message; $("notice").hidden = !message; }
async function api(path, options = {}) {
  const headers = {"X-Workspace-Id": workspace, ...options.headers};
  const response = await fetch(path, {...options, headers});
  if (!response.ok) {
    let detail;
    try { detail = (await response.json()).detail; } catch { detail = "请求失败"; }
    throw new Error(Array.isArray(detail) ? detail.map(v=>v.msg).join("；") : detail || `请求失败 (${response.status})`);
  }
  return response;
}
function renderSources() {
  $("sources").replaceChildren();
  sources.forEach((source, index) => {
    const chip = document.createElement("div"); chip.className = "source-chip";
    const name = document.createElement("span"); name.textContent = source.name;
    const size = document.createElement("small"); size.textContent = `${(new TextEncoder().encode(source.content).length/1024).toFixed(1)} KiB`;
    const remove = document.createElement("button"); remove.textContent = "×"; remove.ariaLabel = `移除 ${source.name}`;
    remove.onclick = () => { sources.splice(index, 1); renderSources(); };
    chip.append(name, size, remove); $("sources").append(chip);
  });
}
function addSource(source) {
  const bytes = text => new TextEncoder().encode(text).length;
  if (sources.length >= 8) throw new Error("最多添加 8 份资料");
  if (!source.content.trim()) throw new Error("资料内容不能为空");
  if (bytes(source.content)>1048576) throw new Error("单份资料不得超过 1 MiB");
  if (sources.reduce((n,s)=>n+bytes(s.content),0)+bytes(source.content)>4194304) throw new Error("总资料不得超过 4 MiB");
  sources.push(source); renderSources(); notice();
}
$("files").onchange = async event => {
  try {
    for (const file of event.target.files) {
      if(file.size>1048576) throw new Error(`${file.name} 超过 1 MiB`);
      const form = new FormData(); form.append("file", file);
      addSource(await (await api("/api/uploads", {method:"POST", body:form})).json());
    }
  } catch (error) { notice(error.message); } finally { event.target.value = ""; }
};
$("add-paste").onclick = () => { try {
  const kind = $("paste-kind").value;
  let name = $("paste-name").value.trim() || `粘贴资料-${sources.length+1}.${kind}`;
  if (!name.toLowerCase().endsWith(`.${kind}`)) name += `.${kind}`;
  addSource({name,kind,content:$("paste").value}); $("paste").value=""; $("paste-name").value="";
} catch(error) {notice(error.message);} };
$("sample-csv").onclick = () => { try { addSource({name:"合成样例-学习记录.csv",kind:"csv",content:"日期,主题,投入分钟,练习数\n2026-01-05,Python,45,3\n2026-01-06,数据分析,60,4\n2026-01-07,Python,30,2\n2026-01-08,开源阅读,50,1\n2026-01-09,数据分析,75,5\n"}); } catch(e){notice(e.message);} };
$("sample-notes").onclick = () => { try { addSource({name:"合成样例-开源项目笔记.md",kind:"md",content:"# 开源项目学习记录\n\n本周阅读了一个命令行工具的源码，重点追踪参数解析和错误处理。这里的内容是演示用合成笔记。\n\n## 已完成\n\n整理了三个使用示例，运行了项目自带的单元测试，并记录了安装步骤。\n\n## 待验证\n\n尚未验证 Windows 路径包含空格时的行为；下周计划增加一个最小复现场景。\n\n## 个人观察\n\n本周学习以阅读和小规模实验为主，尚不能据此评价项目整体质量。\n"}); } catch(e){notice(e.message);} };
$("mode").onchange = () => {$("mode-note").textContent = ({model:"模型模式会将统计与有限摘录发往已配置的接口；连接失败会明确报错。",agent:"Agent 自主选择检查资料或比较指标，最多 6 轮；动作可追溯，随时可取消。",rules:"规则模式生成真实统计和原文摘录，不推断全文含义。"})[$("mode").value];};

async function refreshHistory() {
  const version = ++historyVersion, requestedWorkspace = workspace;
  const current = () => version === historyVersion && requestedWorkspace === workspace;
  let jobs;
  try { jobs = await (await api("/api/jobs")).json(); }
  catch (error) { if (current()) throw error; return; }
  if (!current()) return;
  $("history").replaceChildren();
  if(!jobs.length) { const empty=document.createElement("div");empty.className="history-empty";empty.textContent="这里会保存你的报告。";$("history").append(empty); }
  jobs.forEach(job=>{
    const button=document.createElement("button");button.className="history-item"+(activeId===job.id?" active":"");
    const title=document.createElement("strong");title.textContent=job.title;
    const meta=document.createElement("small");meta.textContent=`${statusNames[job.status]} · ${new Date(job.created_at).toLocaleString("zh-CN",{month:"numeric",day:"numeric",hour:"2-digit",minute:"2-digit"})}`;
    button.append(title,meta);button.onclick=()=>selectJob(job.id).catch(e=>notice(e.message));$("history").append(button);
  });
}
function renderJob(job) {
  activeJob=job;
  const running=["queued","running"].includes(job.status), done=job.status==="completed";
  $("progress-panel").hidden=false;$("progress").value=job.progress;$("stage").textContent=job.stage;$("cancel").hidden=!running;
  $("generate").disabled=running;$("result-empty").hidden=true;
  $("result-meta").hidden=!done;
  $("result-meta").textContent=`${job.title} · ${modeNames[job.mode]} · ${job.sources.length} 份输入 · ${new Date(job.created_at).toLocaleString("zh-CN")}`;
  $("agent-trace").hidden=job.mode!=="agent";$("trace-list").replaceChildren();
  (job.trace||[]).forEach(entry=>{const li=document.createElement("li");li.dataset.status=entry.status;const round=document.createElement("span");round.textContent=`第 ${entry.round} 轮`;const summary=document.createElement("span");summary.textContent=entry.summary;li.append(round,summary);$("trace-list").append(li);});
  $("failure").hidden=done||running;$("failure").textContent=job.status==="cancelled"?"任务已取消，没有保存不完整报告。":job.error||"";
  ["export-md","export-html","print"].forEach(id=>$(id).disabled=!done);
  $("preview").hidden=!done;
  if(done) { const url=`/api/jobs/${job.id}/preview?workspace=${encodeURIComponent(workspace)}`;if($("preview").getAttribute("src")!==url) $("preview").src=url; }
}
function watchJob(id) {
  if(stream) stream.close();
  const watchedWorkspace = workspace;
  const connection = new EventSource(`/api/jobs/${id}/events?workspace=${encodeURIComponent(workspace)}`);
  stream = connection;
  const current = () => stream === connection && activeId === id && workspace === watchedWorkspace;
  connection.addEventListener("progress",event=>{
    if(!current())return;
    const job=JSON.parse(event.data);renderJob(job);
    if(["completed","failed","cancelled"].includes(job.status)) {connection.close();stream=null;refreshHistory().catch(e=>notice(e.message));}
  });
  connection.onerror=()=>{if(!current())return;connection.close();stream=null;notice("进度连接已断开。可以刷新历史查看任务状态。");};
}
async function selectJob(id) {
  const version = ++selectionVersion, requestedWorkspace = workspace;
  const current = () => version === selectionVersion && activeId === id && requestedWorkspace === workspace;
  activeId=id;notice();if(stream){stream.close();stream=null;}
  activeJob=null;["preview","result-meta","cancel","progress-panel","agent-trace","failure"].forEach(panel=>$(panel).hidden=true);
  ["export-md","export-html","print"].forEach(button=>$(button).disabled=true);
  try {
    const job=await(await api(`/api/jobs/${id}`)).json();
    if(!current())return;
    renderJob(job);
    if(["queued","running"].includes(job.status))watchJob(id);
    await refreshHistory();
  } catch(error) { if(current())throw error; }
}
$("generate").onclick=async()=>{
  const requestedWorkspace = workspace, version = selectionVersion;
  const current = () => workspace === requestedWorkspace && selectionVersion === version;
  try {
    notice();if(!sources.length)throw new Error("请先添加至少一份资料");
    if(!$("title").value.trim())throw new Error("请填写报告标题");
    $("generate").disabled=true;
    const data=await(await api("/api/jobs",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({title:$("title").value.trim(),mode:$("mode").value,instruction:$("instruction").value,sources})})).json();
    if(!current())return;
    await selectJob(data.id).catch(error=>{notice(error.message);$("generate").disabled=false;});
  } catch(e){if(current()){notice(e.message);$("generate").disabled=false;}}
};
$("cancel").onclick=async()=>{
  const id=activeId, requestedWorkspace=workspace, version=selectionVersion;
  const current=()=>activeId===id && workspace===requestedWorkspace && selectionVersion===version;
  try {
    if(id){const job=await(await api(`/api/jobs/${id}/cancel`,{method:"POST"})).json();if(current())renderJob(job);}
    if(workspace===requestedWorkspace)await refreshHistory();
  } catch(e){if(current())notice(e.message);}
};
async function download(format) {
  if(activeJob?.id!==activeId || activeJob?.status!=="completed")return;
  const id=activeId, requestedWorkspace=workspace;
  try {
    const response=await api(`/api/jobs/${id}/export/${format}`), blob=await response.blob();
    if(workspace!==requestedWorkspace)return;
    const url=URL.createObjectURL(blob), link=document.createElement("a");link.href=url;link.download=`报告-${id.slice(0,8)}.${format}`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  }catch(e){if(workspace===requestedWorkspace)notice(e.message);}
}
$("export-md").onclick=()=>download("md");$("export-html").onclick=()=>download("html");
$("print").onclick=()=>{if(activeJob?.id===activeId && activeJob?.status==="completed")window.open(`/api/jobs/${activeId}/preview?workspace=${encodeURIComponent(workspace)}`,"_blank","noopener,noreferrer");};
function resetEditor() {
  selectionVersion++;
  if(stream){stream.close();stream=null;}activeId=null;activeJob=null;sources=[];renderSources();notice();
  $("generate").disabled=false;$("progress-panel").hidden=true;$("result-empty").hidden=false;$("preview").hidden=true;$("failure").hidden=true;$("result-meta").hidden=true;$("agent-trace").hidden=true;
  ["export-md","export-html","print"].forEach(id=>$(id).disabled=true);$("title").focus();
}
$("new-report").onclick=()=>{resetEditor();refreshHistory().catch(e=>notice(e.message));};
$("refresh").onclick=()=>{if(activeId)selectJob(activeId).catch(e=>notice(e.message));else refreshHistory().catch(e=>notice(e.message));};
$("switch-workspace").onclick=()=>{workspace=crypto.randomUUID();localStorage.setItem("report-workspace",workspace);$("workspace-id").textContent=workspace.slice(0,8);resetEditor();$("history").replaceChildren();refreshHistory().catch(e=>notice(e.message));};
(async()=>{try{$("workspace-id").textContent=workspace.slice(0,8);const config=await(await api("/api/config")).json();if(config.model_available){for(const mode of ["agent","model"]){const option=$("mode").querySelector(`[value="${mode}"]`);option.disabled=false;option.textContent=modeNames[mode]+" · 已配置接口";}}await refreshHistory();}catch(e){notice(e.message);}})();
