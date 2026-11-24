# LoRE · 游戏本地化 Agent

LoRE 面向游戏台词、任务描述、物品名称和界面文本，支持中英双向翻译。它结合源文本知识图谱、术语和已批准译例，帮助译者在不同角色、场景和剧情分支中保持译名与语气一致。

**工作流**　导入 CSV、Excel 或 PO → 构建源图 → 检索和选择证据 → 草译与审校 → 人工复核 → 导出。

- **有据可查**　Pi Reviewer 可以主动查询源图、术语、角色笔记和译例。译者可以查看模型实际用了哪些证据，以及它们在原文件中的位置。
- **持续积累**　项目从空批准记忆开始，后续批次可以复用已验证的译文。源文变更后，受影响的记忆及依赖会失效。
- **反馈改进**　人工复核中的反馈可用于提出策略候选。候选经过历史回放和独立用例验证后，由编辑决定是否发布，已发布策略支持回滚。

## 快速启动

想直接体验有数据的工作台，可启动 [《破碎的像素地牢》免安装演示数据库](agentdesign/demos/shattered/README.md)。

需要 uv、Python 3.14 和 Node.js 22.19 或更新版本。以下命令从仓库根目录执行。[uv 安装文档](https://docs.astral.sh/uv/getting-started/installation/)

**macOS / Linux**

```bash
cd agentdesign
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -r requirements-api.txt
npm --prefix pi ci
npm --prefix web ci
npm --prefix web run build
.venv/bin/python run_api.py --projects-only --offline
```

**Windows PowerShell**

```powershell
cd agentdesign
uv venv --python 3.14 .venv
uv pip install --python .venv\Scripts\python.exe -r requirements-api.txt
npm --prefix pi ci
npm --prefix web ci
npm --prefix web run build
.\.venv\Scripts\python.exe run_api.py --projects-only --offline
```

首次使用 `--projects-only` 会创建空的本地项目库。创建项目后即可导入 CSV、XLSX 或 PO。`--offline` 使用固定回执，普通项目的译文由编辑输入并批准。[网页工作台](http://127.0.0.1:8766/) · [API 文档](http://127.0.0.1:8766/docs)

网页默认使用英文，可在右上角用「EN / 中文」切换界面语言。使用真实模型时，移除 `--offline`，然后在「Models / 模型设置」中配置连接和角色绑定。也可以在启动前设置 `DEEPSEEK_API_KEY`，由默认网关执行任务。离线和在线模式各有独立工作目录。

使用 CLI 时，可在另一个终端从 `agentdesign` 运行 `.venv/bin/python run_cli.py --url http://127.0.0.1:8766`，Windows 下将 Python 路径换为 `.\.venv\Scripts\python.exe`。

## 目录

| 目录 | 内容 |
| --- | --- |
| [agentdesign](agentdesign/README.md) | 网页工作台、交互式 CLI 和任务 API。项目导入、建图和导出见 [项目接口](agentdesign/docs/PROJECTS.md)。 |
| [game-localization-agent](game-localization-agent/README.md) | 研究复现包，包含游戏数据、图谱与记忆组件、选择器训练、推理、评测和结果。 |
