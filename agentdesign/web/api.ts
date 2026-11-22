import { useEffect, useState } from "react";

export type Language = "en" | "zh";
export type Workspace = { offline: boolean; naev: boolean; naev_snapshot: string };
export type Project = {
  project_id: string; name: string; source_lang: Language; target_lang: Language;
  revision: number; graph_revision: number | null; graph_snapshot: string | null;
  build_job_id: string | null; models: Record<string, string>;
  graph_ready?: boolean; resources?: number; approved_memory?: number;
};
export type Location = { path?: string; line?: number; sheet?: string; row?: number; column?: string };
type ContextItem = { id: string; text: string; category: string; mandatory: boolean };
export type Evidence = {
  actual_context: { items: ContextItem[]; resolved_metadata?: Record<string, string> } | null;
  retrieved: ContextItem[];
  mandatory_terms: { source: string; target: string }[];
  actual_sources: { occurrence_id: string; source_version: number; quote: string; location?: Location | null }[];
  source_lookups: { name: string; effective_query?: string; args: { query?: string }; excluded_hits: number;
    result: { status: string; evidence: Record<string, unknown>[] } }[];
  reviews: { verdict: string; reason: string }[];
};
export type Resource = {
  occurrence_id: string; label: string; key?: string; source_version: number;
  location?: Location; file?: string | null; metadata?: Record<string, string>;
};
export type Selection = { ids: string[]; file?: string; upload?: string };
export type JobSummary = {
  job_id: string; title: string; source_kind: string; project_id: string | null; status: string;
  reason: string | null; intents: string[]; bulk_id?: string;
};
export type JobPage = { jobs: JobSummary[]; total: number };
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
export const intents: Record<string, string> = {
  translate: "翻译", review: "审校", explain: "解释依据", export: "导出", propose_update: "术语更新预演", build_graph: "建图",
};
export const roles = {
  extractor: "图谱提取", selector: "证据选择", translator: "草译",
  reviewer: "审校", repairer: "修复", intent: "意图理解",
};

const messages: Record<string, string> = {
  invalid_request: "请检查表单中的字段与取值范围。",
  network_error: "连接工作台失败。请检查本地服务后重试。",
  project_graph_not_current: "源文件已有变更，请先发布当前版本的图谱。",
  project_graph_not_built: "项目还没有发布图谱。",
  project_graph_build_active: "建图任务正在处理当前源版本，请先处理该任务。",
  project_import_too_large: "文件超过 16 MB 的导入上限。",
  po_upload_too_large: "PO 文件超过 16 MB 的导入上限。",
  invalid_utf8_po: "PO 文件必须使用 UTF-8 编码。",
  invalid_po: "无法解析 PO 文件，请检查其格式。",
  invalid_po_content: "无法读取 PO 内容，请检查文件头和条目格式。",
  empty_or_nontext_source: "原文为空或不是文本。",
  empty_source: "原文为空。",
  missing_import_columns: "找不到原文列、ID 列或映射列，请检查表头。",
  worksheet_not_found: "找不到填写的工作表，请检查表名。",
  offline_fixture_unavailable: "离线模式保留了源文，请输入译文并批准。",
  no_approved_translation: "该条目还没有已批准的译文。",
  source_file_not_found: "这个文件已不在项目的当前源文件中。",
  source_not_in_evidence: "该原文版本没有被当前条目引用。",
  source_context_unavailable: "这条引文没有可定位的文件上下文。",
  export_source_version_mismatch: "当前批准译文对应另一个源版本。",
  po_plural_translations_conflict: "共用同一复数槽的译文存在冲突，保留了文件中的原译文。",
  po_plural_translation_incomplete: "同一 PO 键的复数译文尚未全部批准。",
  translation_validation_stale: "译文依据已变更，需要重新验证。",
  translation_locked: "译文处于锁定状态。",
  version_or_knowledge_changed: "译文或项目知识已更新，请检查当前版本。",
  invalid_import_options: "请检查导入字段映射。",
  invalid_xlsx_archive: "无法读取 XLSX 文件。",
  no_source_rows: "文件中没有可导入的源条目，请检查工作表和列名。",
  job_not_found: "工作台中找不到该任务。",
  project_not_found: "工作台中找不到该项目。",
  po_upload_not_found: "上传记录已不可用，请重新选取 PO 文件。",
  translation_version_changed: "译文已产生新版本，请检查当前版本后重试。",
  translation_locked_or_version_changed: "译文已锁定或版本已变化，请刷新后检查。",
  translation_version_changed_create_new_job: "译文已有新版本，请用最新资源重新发起任务。",
  historical_translation_stale_or_missing: "该历史版本已失效或无法读取。",
  hard_check_failed: "译文未通过格式或强制术语检查。",
  unknown_provider_result_requires_confirmation: "前次模型调用结果未明，重试可能产生重复调用。",
  items_need_attention: "有条目需要人工复核。",
  items_failed: "条目执行失败，请检查任务详情。",
  paused_by_editor: "任务已暂停。",
  cancelled_by_editor: "任务已取消。",
  clarification_requires_new_information: "请补充缺失信息后继续。",
  choose_supported_intent: "请选择任务动作。",
  choose_other_supported_language: "请选择另一种目标语言。",
  choose_supported_source_language: "请选择源语言。",
  target_required: "请选择要处理的源条目。",
  ambiguous_target: "有多个条目与指令匹配，请选择具体位置。",
  target_not_in_scope: "指令里的条目未匹配，请从候选中选择。",
  source_and_target_terms_required: "请补充原文术语和新译法。",
  project_source_language_mismatch: "请选择项目设置中的源语言。",
  project_target_language_mismatch: "请选择项目设置中的目标语言。",
  naev_requires_chinese_target: "Naev 示范区的目标语言为简体中文。",
  naev_source_language_is_english: "Naev 示范区的源语言为英语。",
  model_probe_requires_live_workspace: "可用性测试需要在线工作区。",
  model_credential_endpoint_not_configured: "服务地址需要与后端登记的环境变量一致。",
  project_export_validation_failed: "导出文件未通过校验，请查看排除报告。",
  held_out_case_overlaps_feedback: "独立验证案例涉及已用于反馈的源条目或文件。",
  memory_quote_not_in_source: "引文必须出现在所选条目的原文中。",
  memory_requires_current_source: "源条目版本已更新，请重新选取依据。",
  memory_term_requires_source_quote_and_target: "请填写规定译法，并确认原文术语出现在所选引文中。",
  resolve_po_export_issues_first: "请先检查 PO 校验结果并解决译文冲突。",
};

export function reasonText(reason?: string | null) {
  return reason ? messages[reason.split(":")[0]] ||
    (reason.startsWith("graph_calls_required:") ? `建图至少需要 ${reason.match(/\d+/)?.[0]} 次模型调用。` :
      "工作台返回了一条状态说明，详情见下方代码。") : "";
}

export class ApiError extends Error {
  constructor(public status: number, public reason: string, public fields?: string[][]) {
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
    throw new ApiError(response.status, error.reason || `http_${response.status}`, error.fields);
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
