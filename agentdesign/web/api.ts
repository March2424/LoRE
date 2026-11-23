import { useEffect, useState } from "react";
import { type Locale, getLocale, pick } from "./i18n";

export type Language = "en" | "zh";
export type Profile = {
  profile_id: string; name: string; model: string; base_url: string;
  api_key_env?: string | null; api_key_masked?: string | null;
  credential_source?: "direct" | "env" | "local"; timeout_seconds: number;
  mode?: "api" | "local"; api_format?: "chat_completions" | "responses";
  thinking?: "auto" | "disabled"; credential_configured?: boolean;
};
export type Workspace = {
  offline: boolean; naev: boolean; naev_snapshot: string;
  capabilities?: { direct_api_key?: boolean; partial_graph_build?: boolean };
  default_models?: Record<string, string>;
  default_model_config?: { model: string; base_url: string; api_key_env: string; credential_configured: boolean };
};
export type Project = {
  project_id: string; name: string; source_lang: Language; target_lang: Language;
  revision: number; graph_revision: number | null; graph_snapshot: string | null;
  build_job_id: string | null; models: Record<string, string>;
  graph_ready?: boolean; resources?: number; unextracted?: number; approved_memory?: number;
};
export type Location = { path?: string; line?: number; sheet?: string; row?: number; column?: string };
type ContextItem = { id: string; text: string; category: string; mandatory: boolean };
export type Evidence = {
  actual_context: { items: ContextItem[]; resolved_metadata?: Record<string, string> } | null;
  retrieved: ContextItem[];
  mandatory_terms: { source: string; target: string }[];
  actual_sources: { occurrence_id: string; source_version: number; quote: string; location?: Location | null }[];
  selection?: { decision?: { selected_ids?: string[]; rationale?: string } | null; fallback?: string | null; applied_ids?: string[] }[];
  drafts?: { target: string; generated?: string; injection?: string | null }[];
  hard_checks?: { target_hash: string; problems: string[] }[];
  repairs?: { target: string }[];
  source_lookups: { name: string; effective_query?: string; args: { query?: string }; excluded_hits: number;
    result: { status: string; evidence: Record<string, unknown>[] } }[];
  reviews: { verdict: string; reason: string }[];
};
export type TranslationStatus = "translated" | "awaiting_review" | "untranslated";
export type ResourceCounts = Record<"all" | TranslationStatus, number>;
export type Resource = {
  occurrence_id: string; label: string; key?: string; source_version: number;
  location?: Location; file?: string | null; metadata?: Record<string, string>;
  translation_status?: TranslationStatus; target?: string | null; target_version?: number;
  locked?: boolean; origin_job_id?: string | null; latest_job_id?: string | null; latest_step?: number | null;
  review_reason?: string | null;
};
export type ProjectNote = {
  note_id: string; project_id: string;
  category: "background" | "system_prompt" | "style_guide";
  title: string; content: string; enabled: boolean;
};
export type InspectData = {
  project_id: string;
  resource: Resource;
  translation: {
    target: string | null; head_version: number; locked: boolean;
    versions: { version: number; target: string; source_version: number; origin_job?: string; reviewer?: string }[];
  };
  active_item: (Item & { job_id: string; job_status: string; target_lang?: Language }) | null;
  evidence: Evidence | null;
  project_notes?: ProjectNote[];
  graph: {
    snapshot_id: string | null;
    nodes: { id: string; version: number; kind: string; valid: boolean; attributes: Record<string, unknown>;
      evidence: { occurrence_id: string; source_version: number; quote: string }[] }[];
    edges: { id: string; version: number; relation: string; source: string; target: string; valid: boolean;
      attributes: Record<string, unknown>; evidence: { occurrence_id: string; source_version: number; quote: string }[] }[];
  };
  memory: { record_id: string; version: number; kind: string; content: Record<string, unknown> }[];
};
export type AskResponse = {
  answer: string;
  citations: { kind: string; id: string; text: string }[];
  offline: boolean;
  model: string;
};
export type Selection = { ids: string[]; file?: string; upload?: string };
export type JobSummary = {
  job_id: string; title: string; source_kind: string; project_id: string | null; status: string;
  reason: string | null; intents: string[]; bulk_id?: string;
};
export type JobPage = {
  jobs: JobSummary[]; total: number;
  summary?: {
    kinds: Record<string, number>; intents: Record<string, number>;
    statuses: Record<string, number>; exact_statuses?: Record<string, number>;
  };
};
export type FileReviewScope = { projectId: string; filename: string };
export type FileReviewItem = { job_id: string; step: number; occurrence_id: string; source_version: number; source: string; location: Location };
export type FileReviewPage = { project_revision: number; total: number; items: FileReviewItem[] };
export type Item = {
  step: number; occurrence_id: string; source: string; target: string | null;
  file?: string; location?: Location; metadata: Record<string, string>;
  source_version: number; expected_version: number; head_version: number; version?: number; locked: boolean; version_conflict: boolean;
  status: string; reason: string | null; display_reason?: string; draft_hash: string | null;
  next_actions: string[]; evidence_url: string;
};
export type Job = Omit<JobSummary, "title" | "intents"> & {
  source_lang: Language | null; target_lang: Language | null;
  upload_id: string | null; blocked_by: string | null; pause_requested: boolean;
  items: Item[]; steps: { index: number; intent: string; status: string }[]; results: { intent: string; [key: string]: unknown }[];
  missing: Record<string, { reason: string; choices?: string[] }>;
  clarification_resources?: Resource[]; resolved_targets?: string[];
  next_actions: string[]; policy_release_id: string; snapshots: { graph?: string; source_sha256?: string };
  artifacts: { type: string; url: string }[];
  budget: { limit_calls: number; reserved_or_spent: number };
  counts: { items: number; passed: number; awaiting_review: number; exportable: number };
  graph_build: { batches: number; completed: { entities: number; relations: number; rejected: unknown[] }[] } | null;
};

export const terminal = new Set(["completed", "partial", "failed", "cancelled"]);
export const language = { en: "英语", zh: "简体中文" };
export const languageEn = { en: "English", zh: "Simplified Chinese" };
export const langLabel = (lang: string, locale: Locale = getLocale()) =>
  locale === "zh" ? (language[lang as Language] ?? lang) : (languageEn[lang as Language] ?? lang);

export const intents: Record<string, string> = {
  translate: "翻译", review: "审校", explain: "解释依据", export: "导出", propose_update: "术语更新预演", build_graph: "建图",
};
export const intentsEn: Record<string, string> = {
  translate: "Translate", review: "Review", explain: "Explain", export: "Export", propose_update: "Glossary Preview", build_graph: "Build Graph",
};
export const intentLabel = (intent: string, locale: Locale = getLocale()) =>
  locale === "zh" ? (intents[intent] ?? intent) : (intentsEn[intent] ?? intent);

export const roles = {
  extractor: "图谱提取", selector: "证据选择", translator: "草译",
  reviewer: "审校", repairer: "修复", intent: "意图理解",
};
export const rolesEn: Record<keyof typeof roles, string> = {
  extractor: "Graph Extractor", selector: "Context Selector", translator: "Translator",
  reviewer: "Pi Reviewer", repairer: "Repairer", intent: "Intent Parser",
};
export const roleLabel = (role: keyof typeof roles, locale: Locale = getLocale()) =>
  locale === "zh" ? roles[role] : rolesEn[role];

const messages: Record<string, string> = {
  // 通用与请求错误
  invalid_request: "请求参数或字段格式不符合要求。请检查表单中的必填项、数值范围或文件格式后重试。",
  operation_failed: "当前操作未能完成。请检查模型配置或项目状态后重试。",
  network_error: "无法连接到 LoRE 本地服务。请确认后端服务仍在运行（或刷新页面）后重试。",
  idempotency_key_required: "请求缺少幂等校验标识。请刷新页面后重新提交该操作。",
  idempotency_key_reused_with_different_payload: "检测到重复提交但参数已变化。请稍等片刻或刷新页面后再执行一次。",
  build_web_with_npm_prefix_web_run_build: "前端静态资源尚未构建。请在项目目录执行 npm --prefix agentdesign/web run build 后刷新页面。",

  // 项目、导入与导出
  project_not_found: "找不到该项目（可能已被移除）。请从左上角重新选择已有项目或新建项目。",
  resource_not_found: "找不到对应的源文本条目。请刷新列表后再试。",
  import_not_found: "找不到该导入记录。请重新打开项目资源列表查看。",
  select_an_imported_project: "当前操作需要先选择一个已导入资源的项目。请新建或切换到已导入文件的项目。",
  import_project_sources_first: "项目内还没有导入任何源文件。请先点击「+ 导入」上传 CSV、XLSX 或 PO 文件，再执行建图或翻译。",
  supported_import_formats_csv_xlsx_po: "暂不支持该文件格式。请上传 UTF-8 编码的 CSV、XLSX 或 PO 文件。",
  project_languages_must_differ: "源语言和目标语言不能相同。请选择不同的源语言与目标语言后创建项目。",
  project_import_too_large: "导入文件超过了 16 MB 上限。请拆分文件或精简未使用的列后重新上传。",
  po_upload_too_large: "上传的 PO 文件超过了 16 MB 上限。请拆分文件后再试。",
  uncompressed_workbook_too_large: "XLSX 解压后的表格内容过大，超出安全限制。请拆分成较小的工作簿再上传。",
  invalid_xlsx_archive: "无法读取该 XLSX 文件，文件可能损坏或不是标准压缩工作簿。建议用 Excel 重新另存为 XLSX 或 CSV。",
  invalid_utf8_po: "PO 文件不是 UTF-8 编码。请先将文件转为 UTF-8（无 BOM）编码后再上传。",
  invalid_po: "PO 文件语法有误，无法解析。请检查 msgid / msgstr 引号与转义字符后重试。",
  invalid_po_content: "无法读取 PO 内容。请检查文件头信息与条目结构是否完整。",
  po_extension_required: "此处仅支持 .po 后缀文件。请选择合法的 PO 文件上传。",
  po_upload_not_found: "之前的 PO 上传记录已过期或不存在。请重新选取并上传该 PO 文件。",
  use_project_import_for_project_po: "当前所在项目需通过项目文件方式管理资源。请使用右上角或右侧栏的「+ 导入」上传文件。",
  missing_import_columns: "文件中找不到指定的原文列、ID 列或映射列。请核对表头名称并在导入设置中正确映射。",
  worksheet_not_found: "XLSX 文件中找不到指定的工作表名称。请检查工作表名是否与实际一致。",
  invalid_import_options: "导入映射配置不合法。请检查列映射字段后重新导入。",
  unknown_source_metadata_mapping: "元数据列映射配置无法识别。请检查字段映射设置并去掉无效列名。",
  no_source_rows: "文件中没有可导入的有效源文本。请确认工作表非空且原文列包含文本内容。",
  existing_po_translations_use_another_target_language: "PO 文件中已有译文的语言方向与当前项目设置冲突。请核对文件的目标语言声明或新建匹配语言的项目。",
  source_file_not_found: "该文件已不在项目当前的源文件集合中。请刷新工作区或检查是否已被新版导入替换。",
  empty_or_nontext_source: "选中条目的原文为空或非文本内容，无法翻译。请检查源文件该行数据。",
  empty_source: "原文为空，无法执行翻译或检索。请填写有效的源文本。",
  source_text_required: "请填写需要翻译的原文内容后再提交。",
  text_source_cannot_be_combined_with_resource_selection: "直接输入文本快译不能同时勾选项目资源。请取消条目勾选或直接对选中条目下达指令。",
  conflicting_intent_fields: "同时传入了多种冲突的任务指令参数。请仅用自然语言或明确动作之一提交。",
  intent_or_text_required: "请输入任务指令，或至少选择一个明确的操作动作（如翻译、审校、导出）。",
  export_not_ready: "导出文件尚未生成完毕或任务未达到导出条件。请等待任务完成或检查排除报告。",
  po_export_not_ready: "PO 交付文件尚未准备就绪。请确认已完成翻译并通过校验。",
  project_export_validation_failed: "交付检查未通过（部分条目缺失译文、含术语冲突或处于待复核）。请先处理报告中列出的条目后再导出。",
  resolve_po_export_issues_first: "PO 文件存在译文冲突或校验问题。请打开「检查与交付」解决冲突后再导出。",
  po_plural_translations_conflict: "共用同一复数槽的译文存在不一致，导出保留了原译文。请逐条核对并统一复数译文。",
  po_plural_translation_incomplete: "同一 PO 键的复数形式尚未全部批准。请批准所有复数形态后再交付。",
  export_source_version_mismatch: "当前已批准的译文对应旧版原文。请基于最新源文本重新翻译并批准。",

  // 知识图谱与记忆
  project_graph_not_current: "项目源文件已更新，当前知识图谱已过期。请前往「04 图谱与记忆」重建并发布最新图谱后再翻译。",
  project_graph_not_built: "项目尚未构建知识图谱，Agent 缺少全局术语和角色关系。请先前往「04 图谱与记忆」点击建图。",
  graph_snapshot_not_found: "找不到对应的图谱快照。请前往「04 图谱与记忆」检查或重新建图。",
  project_graph_build_active: "当前源版本已有正在进行的建图任务。请等待该建图任务完成，或在任务页暂停/取消后再重新发起。",
  graph_partial_budget_reached: "已到达本次设定的调用上限，已完成的批次已实时保存到「图谱与记忆」。可随时点击「构建图谱」继续增量构建剩余条目。",
  graph_build_superseded: "已有更新源版本的建图任务取代了本次建图。请打开最新建图任务查看进度。",
  graph_build_interrupted: "建图任务在完成前被中断（可能因模型超时或上下文受限）。请适当调小每批条数并在「模型设置」关闭思考后重试。",
  graph_resume_cannot_change_extraction_inputs: "恢复暂停的建图任务时不能修改提取配置。如需更换参数，请重新发起一次新的建图。",
  memory_quote_not_in_source: "所选引文必须原封不动地出现在该条目的原文中。请重新框选源文本中的准确片段。",
  memory_requires_current_source: "源条目已更新版本，无法为旧版原文登记记忆。请刷新后选取当前版本的条目。",
  memory_term_requires_source_quote_and_target: "请同时填写「规定译法」，并确保原文术语准确出现在所选引文中。",
  memory_note_requires_known_speaker: "登记角色口吻笔记时无法识别说话人。请为该条目补充说话人元数据或改用通用笔记。",
  memory_not_found: "找不到该条记忆记录（可能已被清理）。请刷新记忆列表。",
  memory_version_changed: "该记忆条目刚刚已被修改，版本已更新。请刷新后重新操作。",
  memory_action_does_not_match_status: "当前记忆状态不允许执行此操作（例如重复批准或无效化）。请刷新记忆列表确认最新状态。",

  // 模型与凭证配置
  offline_fixture_unavailable: "当前处于离线演示模式且该条无预设示例，因此保留了原文。可在右侧直接输入译文并批准，或改用在线模型运行。",
  model_probe_requires_live_workspace: "可用性测试需要在线工作区权限（当前为 --offline 离线演示模式）。如需测试真实 API/本地模型，请不要加 --offline 重启服务。",
  model_credential_endpoint_not_configured: "该模型配置未关联有效的 API 地址。请在「05 模型设置」补全 Base URL 并保存。",
  model_credential_unavailable: "未检测到可用 API 密钥。请前往「05 模型设置」点击对应连接的「编辑」，直接填入 API Key 并保存（或在服务端设置对应的环境变量）。",
  invalid_model_base_url: "Base URL 格式无效。请输入完整的 HTTP/HTTPS 地址（例如 https://api.openai.com/v1 或 http://127.0.0.1:11434/v1）。",
  model_requires_https_or_loopback: "云端模型地址必须使用安全的 https://（仅本机地址允许 http://）。请检查 Base URL 协议头。",
  model_local_requires_loopback: "本地模型模式仅允许本机回环地址（localhost、127.0.0.1 或 ::1）。若要连接远程服务器，请切换为「远程接口（OpenAI 格式）」。",
  model_api_requires_key_env: "使用远程模型接口需要提供凭据。请在配置表单中直接填写 API Key（或指定服务端环境变量名）。",
  model_api_requires_credential: "使用远程模型接口需要提供 API 凭据。请在配置界面直接填写 API Key，或切换为高级环境变量方式。",
  model_profile_in_use: "该模型配置正在被项目或工作区默认角色使用，无法直接删除。请先到角色绑定区将相关角色切换到其他配置后再删除。",
  model_profile_not_found: "找不到该模型配置（可能已被删除）。请刷新页面后重新选择。",
  unknown_model_role: "指定的 Agent 角色名称无效。请刷新页面并从合法角色（提取/选择/翻译/审校/修复/意图）中选择。",
  invalid_probe_reply: "模型返回了空内容或非预期的格式。请检查模型名称是否正确，并在「05 模型设置」中关闭模型思考后再测。",
  model_capability_not_confirmed: "模型未能按要求输出结构化 JSON（可能是接口格式选择不匹配或模型能力不足）。请尝试在 Chat Completions 与 Responses 间切换接口格式后再试。",
  pi_provider_error: "Pi 审校阶段调用模型服务失败。请到「05 模型设置」检查 reviewer 角色配置，确认连通后点击重试。",
  call_budget_exhausted: "本任务已达到设定的模型调用上限（max_calls）。请在任务卡片调高调用预算后恢复，或接受当前已完成部分。",
  empty_response: "模型返回了空白响应（常见原因：思考耗尽了输出长度）。请到「05 模型设置」关闭思考或调大输出上限后重试。",
  unknown_provider_result: "上一次模型请求状态不明确（可能网络中断但服务端已扣费/处理）。如需继续，请先勾选「确认重复调用」后再重试。",
  unknown_provider_result_requires_confirmation: "前次模型调用结果未明，直接重试可能产生重复调用。请先确认提示弹窗后再点击重试。",
  call_input_changed: "重试前条目的上下文输入已发生变化。请新建任务重新翻译该条目。",
  stale_tool_receipt: "工具检索回执已过期。请重试该条目以重新执行查证。",
  intent_structure: "Agent 没能将你的指令解析为可执行的任务结构。请用更明确的句子重试（例如「把选中的条目翻译成简体中文」）。",

  // 任务、审校与版本控制
  job_not_found: "找不到该任务（可能已被清理或 ID 有误）。请打开「03 任务」列表查看当前任务。",
  job_item_not_found: "找不到对应的任务条目。请刷新任务详情页确认。",
  resource_not_in_job: "该文本条目不在当前任务范围内。请从该条目所属的任务查看或发起新任务。",
  batch_targets_outside_project_scope: "所选条目中有部分不属于当前项目。请清空勾选后重新在当前项目中选择。",
  no_approved_translation: "该条目还没有已批准的译文，无法导出或依据解释。请先完成翻译并批准。",
  no_completed_items_to_accept: "目前还没有已完成的条目可以部分提交。请等待至少一条完成后再点部分接受，或直接取消任务。",
  job_not_retryable: "当前任务状态不支持重试（仅失败或部分完成可重试）。请检查任务是否仍在执行中。",
  job_is_not_running: "任务当前并非运行中状态，无需暂停。",
  job_is_not_paused: "任务当前不是已暂停状态，无需恢复。",
  unsupported_job_action: "不支持的任务操作。请刷新页面确认最新可用按钮。",
  occurrence_id_required: "请指定要重试的具体条目 ID。",
  item_not_retryable: "该条目当前状态不需要或不支持重试。请检查条目当前状态。",
  item_is_not_awaiting_approval: "该条目当前不处于「待人工复核」状态（可能已被他人或后台处理）。请刷新列表确认。",
  wait_for_item_outcome: "该条目正在处理中尚未产生结果。请等待当前步骤结束后再提交反馈。",
  items_need_attention: "有部分条目未通过自动审校（如术语疑似歧义或上下文不确信），需要人工确认。请点开标橙色条目修改并批准。",
  items_failed: "有条目在执行过程中出错（常见原因：模型超时、输出截断或 API 报错）。请点击条目查看具体原因并在修正模型/参数后重试。",
  paused_by_editor: "任务已按你的要求暂停。确认无误后点击「恢复执行」即可从断点继续。",
  cancelled_by_editor: "任务已取消，未完成的条目未写入正式译文。",
  translation_locked: "该译文已被锁定以防误改。如需修改或更新，请先点击「解锁」后再操作。",
  translation_locked_or_version_changed: "该译文已被锁定或已有更新版本。请刷新面板查看最新版本，解锁后再重新批准。",
  translation_version_changed: "该译文已产生新版本（版本冲突）。请刷新查看最新版本后再重新提交。",
  translation_version_changed_create_new_job: "该条目自任务发起以来已产生新版本，旧草稿已过期。请使用最新资源发起新任务。",
  version_or_knowledge_changed: "译文或项目知识图谱刚刚发生变更。请刷新页面核对最新版本。",
  knowledge_changed_create_new_job: "项目术语/记忆知识已更新，当前草稿依赖的旧上下文已失效。请重新发起一次新任务。",
  context_changed_create_new_job: "条目的上下文依据已变化。请重新发起任务以确保依据最新记忆翻译。",
  source_not_in_evidence: "该源文本版本并未在当前条目的上下文依据中引用。请刷新检视最新证据。",
  source_context_unavailable: "该条引用缺少定位的文件上下文信息，无法跳转到文件位置。",
  translation_validation_stale: "译文依赖的术语或源文已变更，原有验证已失效。请重新审校并确认该条目。",
  historical_translation_stale_or_missing: "选中的历史版本已与当前术语库或源文不兼容，无法直接回滚。请重新翻译或手动修订当前译文。",
  historical_version_required: "请选择早于当前版本的历史版本号再执行回滚。",
  unsupported_version_action: "不支持的版本操作，请刷新页面重试。",
  draft_changed: "在你编辑期间草稿内容发生了变化。请刷新面板获取最新草稿后再批准。",
  draft_required: "请先输入有效译文草稿后再点击批准。",
  stale_expected_version: "表单持有的译文版本号已过期。请刷新后再提交。",
  target_conflicts_with_active_glossary: "译文与当前生效的强制术语规则冲突。请修改译文以遵循规定术语译法。",

  // 意图澄清
  clarification_requires_new_information: "补充的信息没有变化，Agent 仍缺少关键执行条件。请根据下方提示填写新的参数或选区后继续。",
  choose_supported_intent: "无法识别需要执行的动作。请明确说明「翻译」「审校」「解释依据」「更新术语」或「导出」。",
  choose_other_supported_language: "不支持所请求的目标语言（当前支持简体中文与英语）。请指定简体中文或英语。",
  choose_supported_source_language: "不支持所指定的源语言。请选用简体中文或英语。",
  target_required: "尚不明确要处理哪些条目。请在右侧勾选条目，或在对话中点名原文句子/文件名。",
  ambiguous_target: "有多个条目与描述匹配，Agent 无法确定具体目标。请在下方候选中勾选要处理的那一条后继续。",
  target_not_in_scope: "指令中提到的条目不在当前项目或文件范围内。请切换文件或从候选中直接勾选条目。",
  source_and_target_terms_required: "术语更新需要明确原词和新译法。请说明「把 XXX 改成 YYY」。",
  project_source_language_mismatch: "指定的源语言与当前项目设定不符。请沿用项目设置中的源语言，或新建对应语言的项目。",
  project_target_language_mismatch: "指定的目标语言与当前项目设定不符。请沿用项目设置中的目标语言，或新建对应语言的项目。",
  naev_requires_chinese_target: "Naev 示范区仅支持翻译成简体中文。请将目标语言设为简体中文。",
  naev_source_language_is_english: "Naev 示范区源文本均为英语。请将源语言设为英语。",

  // 术语变更维护与进化中心
  maintenance_plan_not_found: "找不到该术语变更预演方案（可能已过期或被删除）。请重新发起术语变更预演。",
  overlapping_conflicting_term: "存在冲突的重叠强制术语规则。请先调整旧规则或缩小作用域后再预演。",
  glossary_rule_not_found: "找不到指定的术语规则，请刷新术语表后查看。",
  term_not_found_in_scope: "选定范围的原文中未出现该术语。请放宽文件/分支范围或核对原文拼写。",
  unknown_scope_occurrence: "指定作用域中包含未知条目 ID。请刷新资源列表重新选取。",
  maintenance_plan_already_used: "该变更方案已经应用过了。如需再次修改术语，请发起新的预演方案。",
  maintenance_plan_stale_preview_again: "预演后部分条目已被修改，方案已过期。请重新生成预演以确认最新影响范围。",
  feedback_or_policy_not_found: "找不到对应的反馈记录或策略版本。请刷新「进化中心」页面。",
  feedback_required: "请至少选择一条人工反馈后再创建策略候选。",
  conflicting_feedback_aliases: "多条反馈对同一个词给出了不同的别名映射。请分开或先统一别名后再创建候选。",
  no_policy_change: "当前候选没有实质性修改任何策略参数（别名、规则提示词等）。请至少修改一项配置。",
  memory_candidate_requires_preview: "涉及长期记忆变更的候选必须先执行预演检查。请先完成预演。",
  job_required_for_validation_cases: "请先选择用于验证对照的历史任务，再拉取验证案例。",
  historical_source_snapshot_unavailable: "历史任务对应的原文快照已不可用，无法用于回放。请更换最近的任务案例。",
  complete_review_trace_required: "该条目的审校追踪记录不完整，无法作为自动回放案例。请选择完整运行完审校的条目。",
  historical_review_draft_unavailable: "历史审校草稿缺失，无法用于回放比对。请选择包含完整草稿的案例。",
  live_strategy_requires_live_workspace: "线上模型策略验证需要处于在线工作区模式（非 --offline）。",
  feedback_case_must_be_linked: "所选回放案例必须来自真实关联的反馈或任务记录。",
  duplicate_replay_case: "回放列表中包含了重复的条目案例，请去重后再提交。",
  held_out_case_overlaps_feedback: "独立验证集案例不能与本轮训练所用的反馈条目或文件重叠。请换用另一份文件或条目的任务进行隔离回放。",
  candidate_already_decided: "该策略候选已经完成审批决定，无法重复审批。如需调整请基于现有版本发起新候选。",
  can_only_rollback_current_policy_release: "仅支持回滚当前正在生效的最新策略版本。",
};

const messagesEn: Record<string, string> = {
  invalid_request: "The request parameters or field format did not pass validation. Check required fields, ranges, or file formats and retry.",
  operation_failed: "The operation could not be completed. Check your model settings or project state and try again.",
  network_error: "Cannot reach the local LoRE server. Make sure run_api.py is still running (or refresh the page) and retry.",
  idempotency_key_required: "Missing idempotency header. Refresh the page and submit the request again.",
  idempotency_key_reused_with_different_payload: "Duplicate request key with changed payload. Wait a moment or refresh and retry.",
  build_web_with_npm_prefix_web_run_build: "Frontend assets are not built yet. Run `npm --prefix agentdesign/web run build` and refresh.",
  project_not_found: "Project not found (it may have been deleted). Select another project or create a new one.",
  resource_not_found: "Source entry not found. Refresh the resource list and retry.",
  import_not_found: "Import record not found. Reopen the project resources view.",
  select_an_imported_project: "This action requires an imported project. Create or select a project with imported files first.",
  import_project_sources_first: "No source files have been imported yet. Click '+ Import' to upload CSV, XLSX, or UTF-8 PO files first.",
  supported_import_formats_csv_xlsx_po: "Unsupported file format. Upload a UTF-8 CSV, XLSX, or PO file.",
  project_languages_must_differ: "Source and target languages cannot be the same.",
  project_import_too_large: "Imported file exceeds the 16 MB limit. Split the file or remove unused columns.",
  po_upload_too_large: "Uploaded PO file exceeds the 16 MB limit.",
  uncompressed_workbook_too_large: "Uncompressed XLSX content exceeds the safety limit. Split it into smaller workbooks.",
  invalid_xlsx_archive: "Cannot read this XLSX archive. Re-save as XLSX or CSV and re-upload.",
  invalid_utf8_po: "The PO file is not valid UTF-8. Convert to UTF-8 (without BOM) and retry.",
  invalid_po: "Invalid PO syntax. Check msgid/msgstr quotes and escape sequences.",
  invalid_po_content: "Unable to parse PO content. Verify headers and entries.",
  po_extension_required: "Only .po files are accepted here.",
  po_upload_not_found: "Previous PO upload expired or was not found. Re-upload the file.",
  use_project_import_for_project_po: "Please upload files via '+ Import' inside this project.",
  missing_import_columns: "Required ID or source columns were not found in the file. Check column headers and mappings.",
  worksheet_not_found: "Specified worksheet name was not found in the XLSX workbook.",
  invalid_import_options: "Invalid import mapping options. Check your column mappings.",
  unknown_source_metadata_mapping: "Unrecognized metadata column mapping. Remove invalid column names.",
  no_source_rows: "No valid source text rows found in the uploaded file.",
  existing_po_translations_use_another_target_language: "Existing PO translations conflict with this project's target language.",
  source_file_not_found: "File is no longer in the active source set. Refresh the workspace.",
  empty_or_nontext_source: "Selected entry has empty or non-text source content and cannot be translated.",
  empty_source: "Source text is empty. Provide valid source text.",
  source_text_required: "Source text is required before submitting.",
  text_source_cannot_be_combined_with_resource_selection: "Quick text translation cannot be combined with selected project entries.",
  conflicting_intent_fields: "Conflicting instruction parameters were sent. Submit either natural language or explicit intents.",
  intent_or_text_required: "Enter a natural-language instruction or choose at least one explicit action.",
  export_not_ready: "Export is not ready yet. Wait for the job to finish or inspect the exclusion report.",
  po_export_not_ready: "PO export is not ready yet. Ensure translations are approved and validated.",
  project_export_validation_failed: "Export validation failed (missing translations, glossary conflicts, or unreviewed items). Resolve flagged items first.",
  resolve_po_export_issues_first: "Resolve PO translation conflicts or validation issues before downloading.",
  po_plural_translations_conflict: "Plural translations sharing this slot conflict. Review and unify plural forms.",
  po_plural_translation_incomplete: "Not all plural forms for this PO key are approved yet.",
  export_source_version_mismatch: "Approved translation targets an older source revision. Retranslate and approve on the latest source.",
  project_graph_not_current: "Source files changed and the current graph is stale. Rebuild the graph in '04 Graph & Memory'.",
  project_graph_not_built: "Knowledge graph has not been built yet. Build the graph in '04 Graph & Memory' first.",
  graph_snapshot_not_found: "Graph snapshot not found. Check or rebuild in '04 Graph & Memory'.",
  project_graph_build_active: "A graph build job is already running for this revision. Wait for completion or pause/cancel it first.",
  graph_partial_budget_reached: "Reached max_calls limit. Completed batches are saved in 'Graph & Memory'; click 'Build Graph' anytime to continue.",
  graph_build_superseded: "A newer source revision superseded this graph build.",
  graph_build_interrupted: "Graph build was interrupted (timeout or context limit). Lower batch_size and disable thinking in 'Models', then retry.",
  graph_resume_cannot_change_extraction_inputs: "Extraction parameters cannot be changed when resuming a paused graph build.",
  memory_quote_not_in_source: "The selected quote must appear verbatim inside the source text.",
  memory_requires_current_source: "Source entry was updated. Refresh and select the current revision.",
  memory_term_requires_source_quote_and_target: "Provide the target translation and ensure the source term appears verbatim in the quote.",
  memory_note_requires_known_speaker: "Speaker metadata is missing on this entry. Add speaker metadata or use a project-level note.",
  memory_not_found: "Memory record not found. Refresh the memory list.",
  memory_version_changed: "Memory record was modified concurrently. Refresh and retry.",
  memory_action_does_not_match_status: "This memory status does not allow the requested action.",
  offline_fixture_unavailable: "Offline demo mode has no fixture for this row; original text was kept. Edit manually on the right or switch to a live model.",
  model_probe_requires_live_workspace: "Model probing requires live workspace mode (start run_api.py without --offline).",
  model_credential_endpoint_not_configured: "This model profile has no valid Base URL configured. Edit it in '05 Models'.",
  model_credential_unavailable: "No usable API key detected. Open '05 Models', edit this profile, and paste your API Key directly (or set the env var).",
  invalid_model_base_url: "Invalid Base URL. Enter a full http:// or https:// URL (e.g. https://api.openai.com/v1).",
  model_requires_https_or_loopback: "Remote model endpoints must use https:// (http:// is allowed only for localhost / 127.0.0.1).",
  model_local_requires_loopback: "Local model mode only permits loopback hosts (localhost, 127.0.0.1, ::1).",
  model_api_requires_key_env: "Remote API models require credentials. Enter an API Key or environment variable name.",
  model_api_requires_credential: "Remote API models require an API Key or environment variable name.",
  model_profile_in_use: "This model profile is currently assigned to a project or workspace role. Reassign those roles before deleting.",
  model_profile_not_found: "Model profile not found. Refresh the page and select another profile.",
  unknown_model_role: "Invalid agent role name.",
  invalid_probe_reply: "Model returned an empty or unexpected reply. Check the Model ID and disable thinking in '05 Models'.",
  model_capability_not_confirmed: "Model did not return valid structured JSON. Try switching between Chat Completions and Responses format.",
  pi_provider_error: "Pi Reviewer model call failed. Check the reviewer profile in '05 Models' and retry.",
  call_budget_exhausted: "Reached max_calls budget for this job. Increase budget or accept completed items.",
  empty_response: "Model returned an empty response (often caused by reasoning tokens exhausting output length). Disable thinking in '05 Models' and retry.",
  unknown_provider_result: "Previous model call status is uncertain. Confirm duplicate call permission before retrying.",
  unknown_provider_result_requires_confirmation: "Previous call outcome is uncertain. Check the confirmation box before retrying.",
  call_input_changed: "Entry context changed before retry. Start a new job for this entry.",
  stale_tool_receipt: "Tool lookup receipt is stale. Retry this item.",
  intent_structure: "Could not parse your request into a structured job. Try clearer instructions (e.g. 'Translate selected entries to Chinese').",
  job_not_found: "Job not found. Check '03 Jobs'.",
  job_item_not_found: "Job item not found. Refresh the job details page.",
  resource_not_in_job: "This entry is not part of the current job.",
  batch_targets_outside_project_scope: "Some selected entries do not belong to the active project. Clear selection and retry.",
  no_approved_translation: "No approved translation exists for this entry yet.",
  no_completed_items_to_accept: "No completed items are ready for partial acceptance yet.",
  job_not_retryable: "Current job status does not allow retrying.",
  job_is_not_running: "Job is not currently running.",
  job_is_not_paused: "Job is not paused.",
  unsupported_job_action: "Unsupported job action. Refresh to see available actions.",
  occurrence_id_required: "Specify an occurrence_id to retry.",
  item_not_retryable: "This item does not need or allow retrying.",
  item_is_not_awaiting_approval: "Item is no longer awaiting manual review.",
  wait_for_item_outcome: "Item is still running. Wait for completion before submitting.",
  items_need_attention: "Some items did not pass automated review and require human confirmation.",
  items_failed: "Some items failed during execution (timeout, truncated output, or API error). Inspect the item and retry.",
  paused_by_editor: "Job paused as requested. Click 'Resume' when ready.",
  cancelled_by_editor: "Job cancelled; unfinished items were not committed.",
  translation_locked: "Translation is locked. Unlock it before making edits.",
  translation_locked_or_version_changed: "Translation is locked or a newer version exists. Refresh before approving.",
  translation_version_changed: "A newer translation version exists. Refresh before submitting.",
  translation_version_changed_create_new_job: "Source or translation version changed since this job started. Create a new job.",
  version_or_knowledge_changed: "Translation or knowledge graph changed concurrently. Refresh and verify.",
  knowledge_changed_create_new_job: "Project glossary or memory changed; start a new job to use the latest context.",
  context_changed_create_new_job: "Context evidence changed. Create a new job.",
  source_not_in_evidence: "Source revision is not cited in this item's evidence.",
  source_context_unavailable: "Source file location context is unavailable for this citation.",
  translation_validation_stale: "Validation is stale because source text or glossary changed. Re-review this entry.",
  historical_translation_stale_or_missing: "Historical version is incompatible with current source or glossary and cannot be rolled back directly.",
  historical_version_required: "Select a version older than the active head version.",
  unsupported_version_action: "Unsupported version action.",
  draft_changed: "Draft changed while you were editing. Refresh to view the latest draft.",
  draft_required: "Enter a non-empty translation before approving.",
  stale_expected_version: "Form holds a stale translation version. Refresh and submit again.",
  target_conflicts_with_active_glossary: "Translation conflicts with an active mandatory glossary rule.",
  clarification_requires_new_information: "Additional clarification details did not change. Provide new parameters or targets.",
  choose_supported_intent: "Could not identify action. Specify translate, review, explain, glossary update, or export.",
  choose_other_supported_language: "Unsupported target language (supported: English and Simplified Chinese).",
  choose_supported_source_language: "Unsupported source language (supported: English and Simplified Chinese).",
  target_required: "Target entries are unclear. Select entries on the right or name the text/file in chat.",
  ambiguous_target: "Multiple entries matched. Check the exact target(s) below to continue.",
  target_not_in_scope: "Mentioned entry is outside the active project or file scope.",
  source_and_target_terms_required: "Provide both source term and new target translation.",
  project_source_language_mismatch: "Specified source language does not match the project setting.",
  project_target_language_mismatch: "Specified target language does not match the project setting.",
  naev_requires_chinese_target: "Naev demo workspace only supports Simplified Chinese as target language.",
  naev_source_language_is_english: "Naev demo source texts are in English.",
  maintenance_plan_not_found: "Glossary maintenance plan not found.",
  overlapping_conflicting_term: "Overlapping conflicting mandatory glossary rule exists.",
  glossary_rule_not_found: "Glossary rule not found.",
  term_not_found_in_scope: "Term was not found in source texts within the selected scope.",
  unknown_scope_occurrence: "Unknown occurrence ID in scope.",
  maintenance_plan_already_used: "This maintenance plan has already been applied.",
  maintenance_plan_stale_preview_again: "Entries changed since preview. Regenerate preview first.",
  feedback_or_policy_not_found: "Feedback or policy release not found. Refresh Evolution Center.",
  feedback_required: "Select at least one feedback record before creating a policy candidate.",
  conflicting_feedback_aliases: "Selected feedback rows define conflicting aliases for the same query.",
  no_policy_change: "No policy parameters were modified in this candidate.",
  memory_candidate_requires_preview: "Candidates modifying memory require a glossary preview first.",
  job_required_for_validation_cases: "Select a completed job to load validation cases.",
  historical_source_snapshot_unavailable: "Historical source snapshot is unavailable for replay.",
  complete_review_trace_required: "Complete review trace is required for replay cases.",
  historical_review_draft_unavailable: "Historical review draft is missing.",
  live_strategy_requires_live_workspace: "Live strategy replay requires live mode (without --offline).",
  feedback_case_must_be_linked: "Replay case must be linked to a real feedback or job record.",
  duplicate_replay_case: "Duplicate case in replay selection.",
  held_out_case_overlaps_feedback: "Validation cases cannot overlap with training feedback entries or files. Pick cases from another file.",
  candidate_already_decided: "This policy candidate has already been decided.",
  can_only_rollback_current_policy_release: "Only the currently active policy release can be rolled back.",
};

export function reasonText(reason?: string | null, locale: Locale = getLocale()) {
  if (!reason) return "";
  const raw = String(reason).trim();
  const table = locale === "zh" ? messages : messagesEn;
  if (table[raw]) return table[raw];
  const base = raw.split(":")[0];
  if (table[base]) {
    const rest = raw.slice(base.length + 1).trim();
    return rest ? `${table[base]} (${rest})` : table[base];
  }
  if (raw.startsWith("graph_calls_required:")) {
    const need = raw.match(/\d+/)?.[0] ?? (locale === "zh" ? "更多" : "more");
    return pick(
      `This graph build requires at least ${need} calls. Increase max_calls above ${need} or raise batch_size.`,
      `本次建图至少需要 ${need} 次模型调用，当前调用上限不足。请将「最大调用数 max_calls」调高至 ${need} 以上，或适当增大「每批条数」后重试。`,
      locale,
    );
  }
  if (raw.startsWith("graph_input_token_limit:")) {
    const tokens = raw.split(":")[1] ?? "limit";
    return pick(
      `Batch input (~${tokens} tokens) exceeded context budget. Raise max_input_tokens or lower batch_size and disable thinking.`,
      `单批输入长度（约 ${tokens} tokens）超出上下文预算。请调高「单批输入 token」，或调小「每批条数」并确认「模型设置」中已关闭思考后再试。`,
      locale,
    );
  }
  if (raw.startsWith("response_incomplete:") || raw.startsWith("response_failed:")) {
    const why = raw.split(":").slice(1).join(":") || "output_limit";
    return pick(
      `Model response stopped before completion (${why}). Disable thinking in Models or increase output limit.`,
      `模型响应在完成前被中断或达到输出长度上限（${why}）。请到「模型设置」中关闭模型思考（thinking=disabled），或调高最大输出 token 上限后重试。`,
      locale,
    );
  }
  if (raw.startsWith("pi_response_incomplete:")) {
    const why = raw.split(":").slice(1).join(":") || "interrupted";
    return pick(
      `Pi Reviewer tool loop interrupted (${why}). Check reviewer timeout/model settings and retry.`,
      `Pi 审校工具调用被中断（${why}）。请检查「模型设置」中审校模型的超时时间与可用性，确认后重试该条目。`,
      locale,
    );
  }
  if (raw.startsWith("hard_check_failed:")) {
    const problems = raw.slice("hard_check_failed:".length).trim();
    return pick(
      `Translation failed hard checks (${problems}). Verify mandatory terms, placeholders, and formatting.`,
      `译文未通过术语或格式硬校验（${problems}）。请对照检查强制术语、占位符与格式标签后重新批准。`,
      locale,
    );
  }
  if (/AuthenticationError|401/.test(raw)) {
    return pick(
      "API authentication failed (401). Check your API Key in Models and retry.",
      "模型 API 密钥鉴权失败（401）。请检查启动服务的环境变量中对应的 API Key 是否填写正确或已失效，修改环境变量并重启服务后重试。",
      locale,
    );
  }
  if (/PermissionDeniedError|403/.test(raw)) {
    return pick(
      "Access denied for this model endpoint (403). Verify API Key permissions or model ID.",
      "没有权限访问所配置的模型接口（403）。请核对 API Key 权限或更换可用模型名称。",
      locale,
    );
  }
  if (/RateLimitError|429/.test(raw)) {
    return pick(
      "Rate limit or quota reached (429). Wait a moment and retry, or reduce batch concurrency.",
      "模型服务请求频率或账户额度达到上限（429）。请稍等片刻后点击重试，或适当调小分批并发速度。",
      locale,
    );
  }
  if (/APITimeoutError|TimeoutError|timed out/i.test(raw)) {
    return pick(
      "Model request timed out. Check network/local model load and increase Timeout (e.g. 120-300s) in Models.",
      "调用模型接口超时未返回结果。请检查网络连接或本地模型负载，并在「模型设置」中增大 Timeout（建议 120–300 秒）后重试。",
      locale,
    );
  }
  if (/APIConnectionError|ConnectionError|ECONNREFUSED/i.test(raw)) {
    return pick(
      "Could not connect to model endpoint. Verify Base URL in Models and ensure local Ollama/vLLM is running.",
      "无法连接到模型服务端点。请到「模型设置」检查 Base URL 是否正确；若使用本地 Ollama/vLLM，请先确认本地模型服务已启动。",
      locale,
    );
  }
  if (/NotFoundError|404/.test(raw)) {
    return pick(
      "Model or API endpoint not found (404). Verify Model ID and Base URL (/v1 path) in Models.",
      "找不到指定的模型或 API 路径（404）。请到「模型设置」核对 Model 名称（如 gpt-4o-mini / qwen3:8b）以及 Base URL 是否包含正确的 /v1 路径。",
      locale,
    );
  }
  if (/BadRequestError|UnprocessableEntityError|400|422/.test(raw)) {
    return pick(
      `Model rejected request parameters (${raw}). Try switching API format (Chat / Responses) in Models and probe.`,
      `模型服务拒绝了当前请求参数（${raw}）。常见原因是 API 接口格式或思考参数不兼容，请到「模型设置」切换 Chat Completions / Responses 并点击「测试可用性」。`,
      locale,
    );
  }
  if (/InternalServerError|500|502|503|504/.test(raw)) {
    return pick(
      `Model provider returned server error (${raw}). Wait a few seconds and retry.`,
      `模型或服务端临时出现内部错误（${raw}）。请稍等几秒后重试当前操作；如持续出现请检查服务终端日志。`,
      locale,
    );
  }
  if (/JSONDecodeError|json/i.test(raw)) {
    return pick(
      "Model output was not valid JSON. Disable thinking in Models or raise max output tokens and retry.",
      "模型输出未能解析为预期的 JSON 结构。建议在「模型设置」关闭思考（thinking=disabled）或调高输出长度上限后重试。",
      locale,
    );
  }
  if (raw.startsWith("http_")) {
    return pick(
      `Server returned HTTP error (${raw.replace("http_", "")}). Check inputs or refresh the page.`,
      `服务接口返回 HTTP 错误（${raw.replace("http_", "")}）。请检查表单输入与项目状态，或刷新页面后重试。`,
      locale,
    );
  }
  return pick(
    `Operation failed (${raw}). Check model configuration, project resources, or inputs and retry.`,
    `操作未能完成（${raw}）。请检查当前模型配置、项目资源或填写参数是否正确，修正后重试。`,
    locale,
  );
}

export type ValidationDetail = { field: string; type: string; msg: string };

export class ApiError extends Error {
  constructor(public status: number, public reason: string, public fields?: string[][], public details?: ValidationDetail[]) {
    super(reasonText(reason));
  }
}

export async function request(path: string, init?: RequestInit) {
  let response: Response;
  try { response = await fetch(path, init); }
  catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiError(0, "network_error");
  }
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    const rawDetail = error.reason || (typeof error.detail === "string" ? error.detail : "") || error.message || `http_${response.status}`;
    throw new ApiError(response.status, rawDetail, error.fields, error.details);
  }
  return response;
}

export async function json<T>(path: string, init?: RequestInit): Promise<T> {
  return (await request(path, init)).json();
}

export function post(payload: unknown, method = "POST"): RequestInit {
  return { method, headers: { "Content-Type": "application/json", "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify(payload) };
}

export function params(values: Record<string, string | number | boolean | null | undefined>) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) if (value != null && value !== "") query.set(key, String(value));
  return query.toString();
}

export function locationText(location?: Location | null, filename?: string | null) {
  return [location?.path || filename, location?.sheet, location?.line ?? location?.row, location?.column]
    .filter(value => value !== undefined && value !== null && value !== "").join(" · ");
}

export function useQuery<T>(path: string | null, refresh = 0) {
  const [result, setResult] = useState<{ path: string; data?: T; error?: unknown }>({ path: "" });
  useEffect(() => {
    if (!path) return;
    const abort = new AbortController();
    json<T>(path, { signal: abort.signal }).then(data => {
      if (!abort.signal.aborted) setResult({ path, data });
    }).catch(error => {
      if (!abort.signal.aborted) setResult(previous => ({ path, data: previous.path === path ? previous.data : undefined, error }));
    });
    return () => abort.abort();
  }, [path, refresh]);
  return path && result.path === path ? result : { data: undefined, error: undefined };
}

export async function download(path: string, filename: string, init?: RequestInit) {
  const blob = await (await request(path, init)).blob();
  saveBlob(blob, filename);
}

export function saveBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function stored<T>(key: string, fallback: T): T {
  try { return JSON.parse(localStorage.getItem(key) ?? "null") ?? fallback; }
  catch { return fallback; }
}
