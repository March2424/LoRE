# 项目后端 · 导入、建图、记忆与交付

这组接口与原有 Naev 工作台共用 SQLite、任务服务和 Pi Reviewer。每个项目有自己的语言方向、源版本、图快照和批准记忆。本轮交付为 API，现有浏览器页面继续走原有入口。启动后可在本机 `/docs` 试用。大部分写操作需要 `Idempotency-Key`，同一操作重试时复用原键。

在新工作目录中运行 `.venv/bin/python run_api.py --projects-only` 可从空项目库启动。加 `--offline` 使用离线回执。默认启动方式仍加载 Naev 冻结快照，同时可创建其他项目。

## 文件与图谱

CSV 以 [用户提供的 Flare CSV](../flare_entity_input_v0.2.csv) 为默认输入契约。

```csv
id,source_text,text_type,context
example_01,Antlion,enemy_name,
example_02,Antlion Nest,location_name,
```

`id` 在同一项目文件内稳定且唯一。相同原文在不同位置用不同 ID。CSV 和 Excel 可以显式映射 `document`、`scene`、`block`、`branch_id`、`phase_id`、`speaker_id`、`addressee_id`、`topic_id`、`requirements` 等元数据。空上下文保留为空，物理行位置用于回写。Excel 默认使用第一张工作表，可指定表名和列，例如 `options={"sheet":"Dialog","id_column":"Key","source_column":"English","target_column":"Chinese","columns":{"branch_id":"Route"}}`。独立译文列只用于原文件保留和导出。PO 使用 `msgid` 和 `msgid_plural` 的原文，并保留 `msgctxt`、条目位置和引用。

| 接口 | 操作 |
| --- | --- |
| `POST /projects` | 提供 `name`、`source_lang` 和 `target_lang`，当前支持 `en` 与 `zh`。初始批准记忆为零。 |
| `POST /projects/{id}/imports?filename=...` | 请求体直接上传 CSV、XLSX 或 UTF-8 PO，`options` 接受列映射 JSON。传 `preview=true` 只解析并报告新增、修改、移除和跳过项。 |
| `GET /projects/{id}/resources` · `GET /projects/{id}/imports` | 查询当前来源和历史导入批次。重新上传同名文件会比较版本。同内容重复上传复用活动导入。 |
| `POST /projects/{id}/graph/build` | 建图入队。可设批量大小、调用数和单批输入/输出预算。例如 `{"batch_size":3,"max_calls":1,"max_input_tokens":3800,"max_output_tokens":1800}`。 |
| `GET /jobs/{job_id}` | 查看建图批次、失败结果和发布快照。恢复失败批次用 `/retry`，服务商结果未知时需明确确认。 |
| `GET /projects/{id}/graph` | 用 `snapshot_id`、`node_id`、`query` 或 `branch_id` 查看版本化局部图。节点与边含来源位置和当前有效性。 |

结构入库使用项目独立的 occurrence 节点。实体关系提取复用研究包的 AutoGraphBuilder，保存原文证据和程序校验状态。默认只提取尚未覆盖的源版本。新增、修改或移除源记录后，翻译等候新快照发布。建图中断时保留已成功批次。请求的建图调用上限低于预计批次数时直接返回所需批次数。可传 `extract:false` 发布仅含结构的快照。

## 模型、翻译和记忆

- `POST /model-profiles` 登记连接，字段为 `name`、`model`、`base_url`、可选的 `api_key_env` 和 `timeout_seconds`。当前适配 OpenAI Responses 兼容端点，包括实现该接口的本地服务。默认走现有 DeepSeek。远端自定义连接由服务环境设置 `WORKBENCH_MODEL_<NAME>_URL` 和对应的 `WORKBENCH_MODEL_<NAME>_KEY`。API 仅保存变量名。填写 `DEEPSEEK_API_KEY` 时，端点须与工作台默认网关一致。
- `PUT /projects/{id}/model-roles` 传 `{"roles":{"extractor":"<profile-id>","translator":"<profile-id>"}}`。可绑定建图、选择、翻译、Pi 审校、修复和意图。绑定在任务创建时冻结。可用 `POST /model-profiles/{profile_id}/probe` 加 `{"role":"reviewer"}` 验证连接与实际工具回执。真实探针会发请求。
- `POST /jobs` 传 `selection.project` 和目标资源 ID，可指定 `max_calls`。大文件使用 `POST /projects/{id}/translation-batches`，传 `file` 或 `targets`、`batch_size`、`max_calls_per_batch`。前批完成或由编辑确认部分交付后，下一批读取最新批准记忆；失败或取消的前批会阻塞后批。
- `/jobs/{id}/pause` 在当前条目或建图批次完成后暂停。用 `/resume` 继续。翻译任务可以附补充 `requirements`，供尚未开始的条目使用。
- `GET /projects/{id}/memory` 分别列出批准项和待审项。验证通过的译文追加为项目 TM。术语或角色笔记可通过 `/memory/drafts` 建议，再用 `/memory/{record_id}/{version}/approve` 批准，或对已批准知识用 `/retire` 退役。草稿和原文件已有译文留在批准库之外。人工词条须有原文引文，角色笔记须有已知说话人。
- `GET /jobs/{job_id}/items/{step}/{occurrence_id}/evidence` 返回候选、实际输入、主动补查和审校轨迹。下一批复用 TM 时保存依赖版本。源记录被改动或移除时，旧证据以及依赖它的记忆失效。

## 交付

`GET /projects/{id}/imports/{import_id}/export` 回写该文件的已批准有效译文。另有 `/export/report` 列出排除项。XLSX 保留其他工作表的数据和公式，CSV 保留其他列数据。项目 PO 保留条目、上下文和引用，并按项目目标语言输出复数槽位。有 Gettext 时执行 `msgfmt --check` 并读回编译结果。对共用同一中文复数槽却给出不同译法的条目，保留原译文并列入排除。Naev 原生 PO 入口继续使用专用写回策略。

## 验证

- 按原始 CSV 文件离线解析了 4,416 条，解析过程没有建图模型调用。
- [离线集成用例](../tests/test_projects.py) 覆盖 CSV、XLSX 和 PO、项目建图、原格式回写、前后批 TM、术语批准、源版本更新和增量建图。
- [小样本脚本](../smoke_project_graph.py) 选用 3 条 Flare 资源。真实 DeepSeek 共调用 1 次，产出 3 个实体、4 条关系，批准记忆为 0。逐条报告保存在本地 `runs/p6-deepseek-smoke/acceptance.json`。首次运行的 smoke 报告未采到 token 用量，后续脚本已修正账本查询。
