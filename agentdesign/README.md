# Naev 本地化 Agent 工作台

命令行支持 P0 翻译闭环和 P1 主动取证，P2 提供本地任务 API，P3 提供最简浏览器工作台与原生 PO 往返，P4 增加术语维护，P5 提供反馈、策略回放和版本管理。Python 和 LangGraph 负责批次编排、检索、硬检查与版本提交。DeepSeek 负责选择、草译和修复，Pi Agent Core 通过同一 DeepSeek Responses 网关审校，可查本地源图、批准记忆和版本化证据。各阶段见 [路线图](ROADMAP.md)。

| 位置 | 内容 |
| --- | --- |
| `web/` | React 工作台与 TypeScript 构建；`web/design/` 提供样式真源与静态模板 |
| `run_api.py` | FastAPI、SQLite 检查点与单 worker |
| `run_p0.py` 与 `workbench/` | 命令行、Responses 适配、LangGraph 翻译闭环、只读源图查询 |
| `pi/` | Pi Reviewer、原生工具调用、流式回执及依赖锁文件 |
| `configs/` | 网关和预算、固定样本及证据范围、离线模拟回执 |
| `prompts/` | 选择、翻译、审校、修复的独立提示词 |
| `runs/` | 每次运行的本地产物，已从 Git 忽略 |

## 安装和运行

在本目录执行。已在 Python 3.14.7 和 Node 24.14.0 验证，Pi 依赖要求 Node ≥22.19.0。

```bash
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -r requirements.txt
npm --prefix pi ci
.venv/bin/python run_p0.py --offline
.venv/bin/python run_p0.py --p1 --offline
```

批次默认读取本地仓库的 `outputs/naev_full_2026-11-01/graph.sqlite`。该 SQLite 是完整的 Naev 源图，随研究提交包提供的是 [Naev 源侧 CSV 和溯源文件](../game-localization-agent/data/naev/snapshot-123fdc1/README.md)。用 `--db /path/to/graph.sqlite` 可以指定同一冻结快照的本地副本。批次按 [小样本清单](configs/naev-p0.json) 校验源库 SHA-256 和查询范围，再备份到独立运行库。检索和落库都在本机执行。

网关凭据通过进程环境变量 `DEEPSEEK_API_KEY` 注入。连通性探针使用短文本、结构化 JSON 和一个回显工具。真实批次使用开源 Naev 样本、指定模型和同一网关。每次批次最多 24 次调用，每条最多一次修复。

```bash
.venv/bin/python run_p0.py --probe
.venv/bin/python run_p0.py
.venv/bin/python run_p0.py --p1
```

`--output` 可指定一个新目录。退出码 1 表示实际结果与清单预期有差异，检查生成的 `summary.json` 和 `trace.jsonl`。批次另有 `run.sqlite`，其中新增的译例需要当前草稿通过程序检查和 Pi 审校。轨迹包含选择器输入、候选池、实际 ContextPack、用量、回退、修复及译文版本。两项故障注入用于固定验收，轨迹分别保留真实生成的草译和送审稿。

P1 使用[独立样本清单](configs/naev-p1.json)。Reviewer 的五个只读工具共享每条一次查询的额度，工具结果和换包分开记录。查无有效证据时生成带原因的人工复核项。[工具说明](docs/P1.md)

## 启动 P2 任务服务

```bash
uv pip install --python .venv/bin/python -r requirements-api.txt
.venv/bin/python run_api.py --offline
```

在本机打开 `http://127.0.0.1:8766/docs` 可调用 API。设置 `DEEPSEEK_API_KEY` 后省略 `--offline` 运行真实模型。离线和真实模式各有独立 SQLite 工作目录。[接口与验收](docs/P2.md)

## 启动 LoRE 工作台

没有 Naev 冻结快照时，可从空项目库启动。Windows PowerShell 在 `agentdesign` 目录运行：

```powershell
uv venv --python 3.14 .venv
uv pip install --python .venv\Scripts\python.exe -r requirements-api.txt
npm --prefix pi ci
npm --prefix web ci
npm --prefix web run build
.\.venv\Scripts\python.exe run_api.py --projects-only --offline
```

打开 [本地工作台](http://127.0.0.1:8766/)，创建项目并导入 CSV、XLSX 或 UTF-8 PO。发布图谱后可在资源库勾选条目发起任务，人工复核完成后从导入记录下载原格式文件和排除报告。离线模式使用固定回执，普通项目由编辑输入并批准译文。设置服务端模型凭据后，移除 `--offline` 启动在线工作区。

已有冻结 Naev 图谱时，可改用以下方式启动示范工作区：

```bash
npm --prefix web ci
npm --prefix web run build
.venv/bin/python run_api.py --offline
```

默认在 `http://127.0.0.1:8766/` 提供上传、任务、复核和下载。支持固定 Naev 源快照的简体中文 UTF-8 PO。[PO 接口与验收](docs/P3.md)

在「任务 → 文本翻译」粘贴原文并选择「中译英」或「英译中」，可复核译文并下载批准结果 JSON。每次文本任务使用独立的源和记忆快照。Naev PO 继续使用冻结的英文源和简体中文目录格式。[双向文本 API](docs/P2.md#双向文本任务)

术语维护区支持预览、确认应用和追加回滚。[P4 接口与验收](docs/P4.md)

复核区可在确认译文时附上修改原因。`/docs` 提供反馈归集、策略候选、回放、发布和回滚接口。[P5 接口与验收](docs/P5.md)

项目 API 支持 CSV、XLSX、PO 导入、后台建图、跨批批准记忆、按模块绑定模型和原格式交付。[项目接口与小样本验收](docs/PROJECTS.md)
