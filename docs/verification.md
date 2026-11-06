# 验证记录

日期：2026-11-06。本地环境为 Windows、Python 3.12；项目使用独立 `.venv`。

| 检查 | 结果 |
|---|---|
| `python -m pytest -q` | 42 项通过；覆盖统计、工具协议、任务隔离、取消、失败和导出 |
| `node --test tests/ui-races.test.cjs` | 8 项通过；覆盖历史报告选择、工作区切换、SSE、取消与下载乱序响应 |
| Python、JavaScript、PowerShell 语法 | 通过 |
| 浏览器真实操作 | 加载两份合成样例并生成报告，展示 5 行学习记录、分钟合计 260、均值 52、练习数 15；报告进入历史，可查看并使用导出入口 |
| 模型工具调用测试 | HTTP MockTransport 驱动四轮工具链，工具实际计算两份 CSV 合计 90、150 和差值 60 |

前端交叉审查实际复现了“选 A 后选 B，A 的迟到响应覆盖 B 预览”的问题，现已修复并加入回归测试。显示内容、当前选择和下载文件保持一致。

截图见 `screenshots/report-workbench.png`。没有调用真实模型供应商，协议测试不等于联网模型效果验收；没有测量生产规模、吞吐、模型准确率或费用收益。

GitHub Actions 配置在 Python 3.11/3.12 上执行 Python 与 JavaScript 测试，实际远程状态以仓库 Actions 为准。
