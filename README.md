# 纸页 · Report Workbench Agent

一个有界工具调用的个人报告 Agent：输入学习笔记、开源项目数据或个人记录，由模型选择检查资料、比较真实指标，再生成附带来源的结构化报告。后端采用 FastAPI 与 SQLite，前端是无需构建的原生浏览器界面。

[![Tests](https://github.com/zhyzx35607/report-workbench-agent/actions/workflows/test.yml/badge.svg)](https://github.com/zhyzx35607/report-workbench-agent/actions/workflows/test.yml)

![合成样例生成的实际本地报告](docs/screenshots/report-workbench.png)

默认规则模式不需要 API Key，也不访问外网。CSV 的行数、缺失值、数值合计/均值/极值与文本高频值来自实际输入。Markdown/TXT 提供字符统计、标题索引和原文摘录，明确标为**规则提要，不是全文语义摘要**。可选模型模式在确定性证据之后补充解释；接口失败会使任务失败，不生成虚构报告。

提供三种清晰区分的模式：

| 模式 | 行为 | 需要模型 |
|---|---|---|
| 规则模式 | 确定性统计、原文摘录、来源与导出 | 否 |
| 直接模型解读 | 一次请求解读已解析证据，不声称有工具规划 | 是 |
| 报告 Agent | 模型选择工具 → 严格校验 → 执行工具 → 回传结果 → 继续或完成 | 是，接口必须支持 function tools |

Agent 不是把一次摘要请求换个名字：它有标准 `tool_calls` 协议、3 个工具白名单、严格参数校验、有限轮次、取消传播及持久化动作轨迹。没有实际检查过证据的最终回答会被拒绝。

本项目独立实现，没有复制公司框架代码、业务数据、客户品牌、内部接口、私有 npm 包或历史凭据。

## 本机启动

需要 Python 3.11 或更高版本。

Windows PowerShell：

```powershell
./start.ps1
```

macOS / Linux：

```bash
sh start.sh
```

脚本首次创建 `.venv` 并安装项目依赖，然后在 `http://127.0.0.1:8767` 启动。首次安装依赖需要网络；之后规则模式可离线运行。也可手动操作：

```bash
python -m venv .venv
# 激活虚拟环境后
python -m pip install -e '.[test]'
python -m uvicorn report_workbench.app:app --host 127.0.0.1 --port 8767
```

在页面添加 UTF-8 `.csv`、`.md` 或 `.txt` 文件，或直接粘贴内容；两个按钮提供明确标注的合成样例。生成后可以查看进度、取消任务、打开历史报告，并导出 Markdown 或可打印 HTML。打印预览打开独立报告页面，再使用浏览器打印功能保存为 PDF。

`examples/` 还提供两份用于比较的学习记录和一份项目笔记。它们都是合成数据，不来自任何实际公司或个人记录。三分钟演示流程与简历表述见 [docs/resume.md](docs/resume.md)。

## 可选模型配置

支持提供 `/chat/completions` 端点的通用接口。配置由服务端环境变量读取，不在网页保存或返回密钥。例如 PowerShell：

```powershell
$env:REPORT_MODEL_BASE_URL = 'http://127.0.0.1:1234/v1'
$env:REPORT_MODEL_NAME = 'your-local-model'
# 如接口需要认证，在当前终端设置 REPORT_MODEL_API_KEY；不要提交该值。
./start.ps1
```

| 变量 | 作用 | 默认值 |
|---|---|---|
| `REPORT_MODEL_BASE_URL` | 模型接口根路径，通常以 `/v1` 结尾 | 未设置 |
| `REPORT_MODEL_NAME` | 接口实际支持的模型标识 | 未设置 |
| `REPORT_MODEL_API_KEY` | 可选 Bearer 密钥 | 未设置 |
| `REPORT_DATA_DIR` | SQLite 数据目录 | 当前目录的 `data` |

模型模式发送分析目标、文件名/哈希、CSV 统计和文档的有限摘录；不发送没有被摘录的文档全文。模型解释与确定性证据分开展示，解释仍需人工核验。没有模型服务时，规则模式保持可用。

## Agent 工具与执行约束

| 工具 | 参数 | 实际操作 |
|---|---|---|
| `list_sources` | 无参数 | 列出本次输入的 ID、文件名、类型、字节数与哈希 |
| `inspect_source` | `source_id` | 返回对应资料的确定性统计或有限文档摘录 |
| `compare_metrics` | `source_ids`、`metric`，列指标另需 `column` | 比较同一指标并计算相对第一份资料的差值 |

比较支持 CSV 行数/列数、文档字符数/行数，以及 CSV 指定列的合计、均值、极值、非空或缺失数量。工具只操作当前任务的已解析证据，不提供任意 Python、Shell、SQL、文件路径或数据库访问。来源 ID 必须存在，额外参数、错误类型、缺失列、重复 ID、非法工具名都会使任务明确失败。

每个 Agent 任务最多 **6 轮模型请求**，每轮最多 **3 次工具调用**；工具参数最多 4096 字符、单次结果最多 24000 字符、累计模型上下文最多 120000 字符。达到上限仍未 final 时任务失败，不能把中间产物当作成功报告。最终回答之前必须至少完成一次 `inspect_source` 或 `compare_metrics`。

模型响应中的 `reasoning_content` 等隐藏思维字段和工具调用前的自由文本不持久化、不展示。SQLite、SSE 与导出报告只保存应用生成的轮次、工具名称、状态和结果摘要。模型最终报告作为公开输出保存。

## 能力边界

- 单个输入最多 1 MiB，最多 8 份，总计最多 4 MiB；HTTP 请求体最多 8 MiB。仅接受非空 UTF-8 文本。
- CSV 使用逗号分隔，必须有唯一且非空的表头，至少一行数据，最多 50000 行、100 列。完全空白行忽略；列数不一致会明确失败。
- 数值列要求所有非空值均可解析为有限十进制数。缺失值不参与统计；均值最多显示 4 位小数。数值绝对值超过 `1e18` 或小数精度超过 20 位时，该列按文本处理。不会推断列的单位、金额意义、比例或因果。
- 文档模式依次摘录前 5 个非标题段落，每段最多 420 字符，展示前 12 个 Markdown 标题；长文后部可能未包含在摘录中。
- 直接模型解读模式最多发送 60000 字符的证据 JSON；Agent 模式使用上述分步预算。每次模型请求超时为 90 秒，连接超时为 10 秒。
- 进度是实际处理阶段与输入完成比例，不是假装逐字生成。模型调用期间显示等待状态。取消会关闭本地等待并阻止保存报告，远端供应商可能仍完成已接收的请求。
- 报告只保存最终 Markdown、来源元数据、任务状态与公开动作摘要；不另存上传原件。报告自身可能含原文摘录。SQLite 默认位于 `data/reports.sqlite3`，已有数据库会自动添加 trace 列。
- 来源 SHA-256 和字节数针对实际参与分析的 UTF-8 文本计算；上传时剔除 UTF-8 BOM，因此带 BOM 文件的原始字节哈希可能不同。
- 工作区 UUID 保存在浏览器 localStorage；不同工作区的历史、导出、预览和取消操作均隔离。它是单机使用的访问标识，不是多人账户认证。创建新工作区不会删除旧数据；清除浏览器存储会失去原工作区标识。默认仅监听本机。
- 单进程运行。服务重启后，未完成任务会标记取消，不自动重试付费请求。

## 架构

```mermaid
flowchart TD
    UI[浏览器：输入、进度、动作轨迹] --> API[FastAPI：校验、任务与工作区]
    API --> E[确定性解析与来源 SHA-256]
    E --> R[规则模式]
    E --> A[有界 Agent 循环]
    E --> M[一次模型解读]
    A --> L[模型选择工具或 final]
    L --> V[工具名、参数、轮次与上下文校验]
    V --> T[list_sources / inspect_source / compare_metrics]
    T --> L
    L --> F[最终解释]
    R --> D[合并确定性证据与报告]
    M --> D
    F --> D
    D --> DB[(SQLite：报告、来源、动作摘要)]
    DB --> X[Markdown / 安全 HTML]
    API --> SSE[SSE：真实阶段与动作]
    SSE --> UI
    API --> C[取消：终止等待及后续工具调用]
```

核心文件：`report_workbench/agent.py` 实现工具协议与有限循环；`engine.py` 负责输入、统计和模型网关；`store.py` 负责参数化 SQLite 存取；`app.py` 负责任务、隔离、SSE、上传与导出；`static/` 包含界面。不存在公司领域的债务、财务、人事或企业风险计算。

## 验证

```bash
python -m pip install -e '.[test]'
python -m pytest -q
node --test tests/ui-races.test.cjs
```

当前测试共 **42 项**，覆盖输入变化改变真实统计、十进制精度、来源与规则模式标注、历史持久化和工作区隔离、Markdown/HTML 导出、非法上传与体积限制、四轮工具协议成功、错误工具/参数、重复调用 ID、轮次/上下文/结果上限、模型失败、模型与工具阶段取消，以及 HTML 注入防护。模型测试使用 HTTP MockTransport，不访问真实供应商或消耗额度。GitHub Actions 在 Python 3.11 / 3.12 上运行测试。

采用 MIT 许可证。

另有 **8 项 JavaScript 竞态测试**：历史选择、工作区切换、SSE、取消与下载的旧响应不会覆盖当前选择。加载新报告时禁用导出，避免预览和下载错配。浏览器已验证合成样例生成、真实统计、历史及报告展示；详细范围见 [验证记录](docs/verification.md)。
