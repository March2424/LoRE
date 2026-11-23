import { useState } from "react";
import { type FileReviewScope, type JobPage, type Location, type Project, type Resource, type Selection,
  download, json, langLabel, locationText, params, post, reasonText, saveBlob, useQuery } from "./api";
import { type Locale, useI18n } from "./i18n";
import { type Composer } from "./jobs";
import { Button, Empty, ErrorNote, Field, Grid, Head, Modal, Pager, Section, Status, useAction } from "./ui";

export type Import = {
  import_id: string; filename: string; format: string; rows: number;
  skipped: { row?: number; line?: number; key?: string; reason: string }[];
  project_revision: number; changes: Record<"added" | "changed" | "unchanged" | "removed", number>;
};
export type Upload = { upload_id: string; filename: string; entries: number; mapped_occurrences: number };
type PoEntry = {
  id: string; source: string; source_plural?: string; context?: string;
  occurrences: { occurrence_id: string; source_form: string; location: Location }[];
};
type ExportReport = {
  translated: number; excluded: string[];
  exclusion_details: { occurrence_id: string; source: string; location: Location; reason: string }[];
  validation: { ok: boolean; level?: string; diagnostics?: string; reason?: string } | null;
};
export type FileProgress = {
  filename: string; import_id: string; total: number; untranslated: number; awaiting_review: number; deliverable: number;
};

const columns = "28px minmax(180px, 2fr) minmax(120px, 1fr) 86px 92px";
const metadata = {
  text_type: { en: "Text Type", zh: "文本类型" },
  native_context: { en: "Context", zh: "上下文" },
  document: { en: "Document", zh: "文档" },
  scene: { en: "Scene", zh: "场景" },
  block: { en: "Dialogue Block", zh: "对话块" },
  branch_id: { en: "Story Branch", zh: "剧情分支" },
  phase_id: { en: "Phase", zh: "阶段" },
  speaker_id: { en: "Speaker", zh: "说话人" },
  addressee_id: { en: "Addressee", zh: "听话人" },
  topic_id: { en: "Topic", zh: "话题" },
  requirements: { en: "Requirements", zh: "翻译要求" },
} as const;
const metaLabel = (key: string, locale: Locale) =>
  (metadata as Record<string, { en: string; zh: string }>)[key]?.[locale] ?? key;

function parseCsvHeaders(line: string): string[] {
  const cells: string[] = [];
  let current = "";
  let inQuotes = false;
  for (let i = 0; i < line.length; i++) {
    const ch = line[i];
    if (ch === '"') {
      if (inQuotes && line[i + 1] === '"') { current += '"'; i++; }
      else inQuotes = !inQuotes;
    } else if (ch === "," && !inQuotes) {
      cells.push(current.trim());
      current = "";
    } else if ((ch === "\n" || ch === "\r") && !inQuotes) {
      break;
    } else {
      current += ch;
    }
  }
  if (current.trim() || cells.length) cells.push(current.trim());
  return cells.filter(Boolean);
}

export function ImportDialog({ project, close, changed }: { project: Project; close: () => void; changed: () => void }) {
  const { locale, t } = useI18n();
  const [file, setFile] = useState<File>();
  const [headers, setHeaders] = useState<string[]>([]);
  const [options, setOptions] = useState({ id_column: "id", source_column: "source_text", sheet: "", target_column: "" });
  const [mapping, setMapping] = useState<Record<string, string>>({});
  const [prepared, setPrepared] = useState<{ preview: Import; url: string; key: string }>();
  const action = useAction();
  const table = file && !file.name.toLowerCase().endsWith(".po");

  const handleFile = async (nextFile?: File) => {
    setPrepared(undefined);
    setFile(nextFile);
    setHeaders([]);
    if (nextFile && nextFile.name.toLowerCase().endsWith(".csv")) {
      try {
        const text = await nextFile.slice(0, 4096).text();
        const firstLine = text.replace(/^\uFEFF/, "").split(/\r?\n/, 1)[0] ?? "";
        const cols = parseCsvHeaders(firstLine);
        setHeaders(cols);
        setOptions(old => ({
          ...old,
          id_column: cols.find(c => ["id", "key", "occurrence_id", "string_id"].includes(c.toLowerCase())) ?? old.id_column,
          source_column: cols.find(c => ["source_text", "source", "english", "en", "text"].includes(c.toLowerCase())) ?? old.source_column,
        }));
      } catch { /* Ignore client-side CSV header preview failure */ }
    }
  };

  return <Modal title={t("Import Source File", "导入源文件")} close={close}>
    <ErrorNote error={action.error} />
    <form className="stack" onSubmit={event => {
      event.preventDefault();
      if (!file) return;
      void action.run(async () => {
        const spec = table ? {
          id_column: options.id_column, source_column: options.source_column,
          target_column: options.target_column.trim() || undefined, sheet: options.sheet.trim() || undefined,
          columns: Object.fromEntries(Object.entries(mapping).filter(([, name]) => !!name.trim())),
        } : {};
        const url = `/projects/${project.project_id}/imports?${params({ filename: file.name, options: JSON.stringify(spec) })}`;
        if (!prepared) {
          const preview = await json<Import>(url + "&preview=true", { method: "POST", body: file });
          setPrepared({ preview, url, key: crypto.randomUUID() });
        } else {
          await json<Import>(prepared.url, { method: "POST", headers: { "Idempotency-Key": prepared.key }, body: file });
          changed();
          close();
        }
      });
    }}>
      <Field label={t("Source File (CSV, XLSX, or UTF-8 PO · max 16 MB)", "源文件（CSV、XLSX 或 UTF-8 PO，最多 16 MB）")}>
        <input className="input" type="file" required accept=".csv,.xlsx,.po" disabled={action.busy}
          onChange={event => void handleFile(event.target.files?.[0])} />
      </Field>
      {!!headers.length && <div className="note">
        <b>{t("Detected CSV headers (verify column mapping below)", "检测到 CSV 表头（点击可填入或核对下方映射）")}</b>
        <div className="row-set">
          {headers.slice(0, 14).map(col => <span key={col} className="pill pill--plain mono">{col}</span>)}
          {headers.length > 14 && <span className="hint">{t(`+${headers.length - 14} more (${headers.length} total)`, `等共 ${headers.length} 列`)}</span>}
        </div>
      </div>}
      {table && <details open={!prepared}><summary>{t("Column Mapping", "列映射")}</summary>
        <div className="form-grid">
          {Object.entries({
            id_column: t("ID Column", "ID 列"),
            source_column: t("Source Column", "原文列"),
            target_column: t("Existing Target Column", "原文件译文列"),
            sheet: t("Worksheet", "工作表"),
          }).map(([key, title]) =>
            (key !== "sheet" || file.name.toLowerCase().endsWith(".xlsx")) && <Field key={key} label={title}>
              <input className="input" value={options[key as keyof typeof options]} disabled={action.busy}
                required={key === "id_column" || key === "source_column"} onChange={event => {
                  setPrepared(undefined); setOptions(previous => ({ ...previous, [key]: event.target.value }));
                }} />
            </Field>)}
        </div>
        <details><summary>{t("Game Context Columns (Optional)", "游戏上下文列（可选映射）")}</summary><div className="form-grid">
          {Object.keys(metadata).map(key => <Field key={key} label={metaLabel(key, locale)}>
            <input className="input" placeholder={t("Default mapping", "使用默认映射")} disabled={action.busy} value={mapping[key] ?? ""} onChange={event => {
              setPrepared(undefined); setMapping(previous => ({ ...previous, [key]: event.target.value }));
            }} />
          </Field>)}
        </div></details>
      </details>}
      {prepared && <div className="stack">
        <div className="note note--ok"><b>{t(`Preview Ready · ${prepared.preview.rows} source rows`, `预检完成 · ${prepared.preview.rows} 条源文`)}</b>
          {t(
            `Added ${prepared.preview.changes.added} · Changed ${prepared.preview.changes.changed} · Unchanged ${prepared.preview.changes.unchanged} · Removed ${prepared.preview.changes.removed} · Skipped ${prepared.preview.skipped.length}`,
            `新增 ${prepared.preview.changes.added} · 修改 ${prepared.preview.changes.changed} · 沿用 ${prepared.preview.changes.unchanged} · 移除 ${prepared.preview.changes.removed} · 跳过 ${prepared.preview.skipped.length}`,
          )}
        </div>
        {!!prepared.preview.skipped.length && <details><summary>{t("Skipped rows", "跳过的源行")}</summary><ul className="scroll-list">
          {prepared.preview.skipped.map((row, index) => <li key={index}>{t(`Line ${row.row ?? row.line}`, `第 ${row.row ?? row.line} 行`)} · {row.key && row.key + " · "}
            {reasonText(row.reason, locale)} <code>{row.reason}</code></li>)}
        </ul></details>}
        {(prepared.preview.changes.changed > 0 || prepared.preview.changes.removed > 0) &&
          <div className="note note--warn">{t("Modified or removed source lines will invalidate prior evidence and dependent memory.", "修改或移除原文会使对应的旧证据和依赖记忆失效。")}</div>}
      </div>}
      <div className="dialog__foot"><Button type="submit" primary disabled={!file || action.busy}>
        {action.busy ? t("Reading file…", "正在读取文件…") : prepared ? t("Confirm Import", "确认导入") : t("Preview Import", "导入预检")}
      </Button></div>
    </form>
  </Modal>;
}

export function BatchDialog({ project, selection, initialFile, close, openJob }: {
  project: Project; selection: Selection; initialFile?: string; close: () => void; openJob: (id: string) => void;
}) {
  const { t } = useI18n();
  const action = useAction();
  const targetFile = initialFile ?? selection.file;
  const [scopeMode, setScopeMode] = useState(
    !initialFile && selection.ids.length ? "selected" : "file",
  );
  return <Modal title={t("Auto-Batch Translation", "自动分批翻译")} close={close}>
    <ErrorNote error={action.error} />
    <form className="stack" onSubmit={event => {
      event.preventDefault();
      const form = new FormData(event.currentTarget);
      void action.run(async () => {
        const result = await json<{ jobs: { job_id: string }[] }>(`/projects/${project.project_id}/translation-batches`, post({
          ...(scopeMode === "selected" ? { targets: selection.ids } : targetFile ? { file: targetFile } : {}),
          batch_size: Number(form.get("size")), max_calls_per_batch: Number(form.get("limit")),
          requirements: String(form.get("requirements")).trim() || undefined,
        }));
        openJob(result.jobs[0].job_id);
      });
    }}>
      <Field label={t("Scope", "翻译范围")}><select className="input" value={scopeMode} onChange={e => setScopeMode(e.target.value)}>
        <option value="file">{targetFile ? t(`Full file: ${targetFile}`, `整份文件：${targetFile}`) : t("All project files", "项目全部文件")}</option>
        <option value="selected" disabled={!selection.ids.length}>{t(`Selected entries (${selection.ids.length})`, `当前已选条目（${selection.ids.length} 条）`)}</option>
      </select></Field>
      <div className="form-grid">
        <Field label={t("Batch Size (1–64)", "每批条数 (1–64)")}><input className="input" type="number" name="size" min="1" max="64" defaultValue="12" required /></Field>
        <Field label={t("Max Calls / Batch", "单批调用上限")}><input className="input" type="number" name="limit" min="1" max="1000" defaultValue="72" required /></Field>
      </div>
      <Field label={t("Extra Requirements (Optional)", "补充要求（可选）")}><textarea className="textarea" name="requirements" maxLength={5000} /></Field>
      <p className="muted">{t("Subsequent batches automatically inherit newly approved memory from earlier batches for consistent terms and tone.", "前一批完成或被确认部分交付后，下一批会自动读取最新已批准记忆，保持译名与语气一致。")}</p>
      <div className="dialog__foot"><Button primary type="submit" disabled={action.busy}>{t("Create Batch Jobs", "创建分批任务")}</Button></div>
    </form>
  </Modal>;
}

export function Deliver({ project, file, close }: { project: Project; file: Import; close: () => void }) {
  const { locale, t } = useI18n();
  const path = `/projects/${project.project_id}/imports/${file.import_id}/export`;
  const report = useQuery<ExportReport>(path + "/report");
  const action = useAction();
  const autoOpenExclusions = !!report.data && (report.data.translated === 0 || report.data.validation?.ok === false);
  return <Modal title={t(`Validate & Export · ${file.filename}`, `检查与导出 · ${file.filename}`)} close={close}>
    <ErrorNote error={report.error || action.error} />
    {!report.data && !report.error && <p role="status">{t("Checking writeback readiness and format compliance…", "正在检查可写回译文与格式合规性…")}</p>}
    {report.data && <>
      <div className={`note ${report.data.translated > 0 && report.data.validation?.ok !== false ? "note--ok" : "note--warn"}`}>
        <b>{t(`Ready to write ${report.data.translated} approved · ${report.data.excluded.length} excluded`, `将写回 ${report.data.translated} 条已批准译文 · 排除 ${report.data.excluded.length} 条`)}</b>
        {t(`Source revision v${file.project_revision}`, `对应项目源版本 v${file.project_revision}`)}
      </div>
      {report.data.validation && <div className="panel">
        <div className="row-set">
          <b>{t("Format & Compile Check", "格式与编译校验")}</b>
          <Status value={report.data.validation.ok ? "passed" : "failed"} pill />
          {report.data.validation.level && <span className="mono hint">{report.data.validation.level}</span>}
        </div>
        <ErrorNote reason={report.data.validation.reason} />
        {report.data.validation.diagnostics && <pre className="proof-cmd">{report.data.validation.diagnostics}</pre>}
      </div>}
      {!!report.data.excluded.length && <details open={autoOpenExclusions}><summary>{t(`Excluded Entries (${report.data.excluded.length})`, `未导出条目明细（${report.data.excluded.length} 条）`)}</summary>
        <ul className="scroll-list">{report.data.exclusion_details.map(item => <li key={item.occurrence_id} className="block">
          <div className="text-block"><strong>{item.source}</strong></div>
          <p>{reasonText(item.reason, locale)} <code>{item.reason}</code></p>
          <span className="hint">{locationText(item.location)}</span>
        </li>)}</ul>
      </details>}
      <div className="dialog__foot">
        <Button onClick={() => saveBlob(new Blob([JSON.stringify(report.data, null, 2)], { type: "application/json" }),
          `${file.filename}.report.json`)}>{t("Download Report", "下载检查报告")}</Button>
        <Button primary disabled={action.busy || report.data.validation?.ok === false || report.data.translated === 0}
          onClick={() => void action.run(() => download(path, file.filename))}>{t("Download Exported File", "下载原格式文件")}</Button>
      </div>
    </>}
  </Modal>;
}

export function Resources({
  project, scope, projects, jobs, offline, naev, upload, setUpload, selection, setSelection,
  refresh, changed, choose, createProject, navigate, openJob, compose, openGraph, openFileReview,
}: {
  project?: Project; scope: string; projects: Project[]; jobs?: JobPage; offline: boolean; naev: boolean;
  upload: Upload | null; setUpload: (upload: Upload) => void; selection: Selection;
  setSelection: (selection: Selection) => void; refresh: number; changed: () => void;
  choose: (id: string) => void; createProject: () => void;
  navigate: (page: "jobs" | "knowledge" | "models" | "glossary" | "evolution") => void;
  openJob: (id: string) => void; compose: (mode: Composer, operation?: string) => void;
  openGraph: () => void; openFileReview: (scope: FileReviewScope) => void;
}) {
  const { locale, t } = useI18n();
  const size = 25;
  const limit = project ? 64 : 12;
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState("");
  const [offset, setOffset] = useState(0);
  const [dialog, setDialog] = useState<"import" | "batches" | null>(null);
  const [batchFile, setBatchFile] = useState<string>();
  const [delivery, setDelivery] = useState<Import>();
  const [detail, setDetail] = useState<Resource>();
  const action = useAction();
  const base = project ? `/projects/${project.project_id}` : null;
  const resources = useQuery<{ resources: Resource[]; total: number }>(
    base ? base + `/resources?${params({ query: search, offset, limit: size, file: selection.file })}` : (scope === "naev" && !upload) ? "/resources" : null, refresh);
  const po = useQuery<{ entries: PoEntry[]; total: number }>(!base && scope === "naev" && upload ?
    `/po-uploads/${upload.upload_id}/entries?${params({ query: search, offset, limit: size })}` : null, refresh);
  const imports = useQuery<{ imports: Import[] }>(base && base + "/imports", refresh);
  const progress = useQuery<{ project_revision: number; files: FileProgress[] }>(base && base + "/files/progress", refresh);
  const importById = new Map((imports.data?.imports ?? []).map(item => [item.import_id, item]));
  const currentFiles = [...new Map((imports.data?.imports ?? []).slice().reverse().map(file => [file.filename, file])).values()];
  const all = resources.data?.resources ?? [];
  const native = !project && scope === "naev" && !upload ? all.filter(item => [item.label, item.file, item.occurrence_id]
    .some(value => value?.toLowerCase().includes(search.toLowerCase()))) : [];
  const total = project ? resources.data?.total : upload ? po.data?.total : native.length;
  const rows = project ? all : upload ? [] : native.slice(offset, offset + size);

  const visibleIds = project
    ? rows.map(r => r.occurrence_id)
    : upload
      ? (po.data?.entries ?? []).flatMap(e => e.occurrences.map(o => o.occurrence_id))
      : rows.map(r => r.occurrence_id);
  const allVisibleSelected = visibleIds.length > 0 && visibleIds.every(id => selection.ids.includes(id));

  const toggleAllVisible = () => {
    if (allVisibleSelected) {
      const visibleSet = new Set(visibleIds);
      setSelection({ ...selection, ids: selection.ids.filter(id => !visibleSet.has(id)) });
    } else {
      const merged = [...selection.ids];
      for (const id of visibleIds) {
        if (merged.length >= limit) break;
        if (!merged.includes(id)) merged.push(id);
      }
      setSelection({ ...selection, ids: merged });
    }
  };

  const select = (id: string) => {
    const ids = selection.ids.includes(id) ? selection.ids.filter(item => item !== id) : [...selection.ids, id];
    setSelection({ ...selection, ids });
  };
  const checkbox = (id: string) => <input type="checkbox" aria-label={t(`Select ${id}`, `选择 ${id}`)}
    checked={selection.ids.includes(id)} disabled={!selection.ids.includes(id) && selection.ids.length >= limit}
    onChange={() => select(id)} />;

  const fileRows = progress.data?.files ?? [];
  const totalDeliverable = fileRows.reduce((sum, f) => sum + f.deliverable, 0);
  const totalReview = fileRows.reduce((sum, f) => sum + f.awaiting_review, 0);

  return <>
    <Head title={project ? project.name : scope === "naev" ? t("Naev Demo Workbench", "Naev 示范工作台") : t("LoRE Localization Workbench", "LoRE 本地化工作台")}
      sub={project ? `${langLabel(project.source_lang, locale)} → ${langLabel(project.target_lang, locale)} · v${project.revision} · ${project.graph_ready ? t("Graph Ready", "图谱就绪") : t("Needs Graph", "待建图")}`
        : scope === "naev" ? (upload ? t(`Loaded PO: ${upload.filename} (${upload.mapped_occurrences} mapped)`, `已加载 PO：${upload.filename}（${upload.mapped_occurrences} 处映射）`) : t("English → Simplified Chinese · Naev Frozen Snapshot", "英语 → 简体中文 · Naev 冻结快照"))
          : t("Create or select a localization project to begin", "请先新建或选择一个本地化项目")}>
      {scope && <Button onClick={() => compose("command")}>{t("Run Command", "下达任务")}</Button>}
      {project ? <Button primary={!project.resources} onClick={() => setDialog("import")}>{t("+ Import Source", "+ 导入源文件")}</Button>
        : scope === "naev" ? <label className="btn">{t("Upload ZH PO", "上传简体中文 PO")}<input className="file-control" type="file" accept=".po" disabled={action.busy}
          onChange={event => {
            const file = event.target.files?.[0];
            if (file) void action.run(async () => {
              const next = await json<Upload>(`/po-uploads?${params({ filename: file.name })}`, { method: "POST", body: file });
              setUpload(next); setSelection({ ids: [], upload: next.upload_id }); setOffset(0);
            });
          }} /></label> : null}
      <Button primary={!scope} onClick={createProject}>{t("+ New Project", "+ 新建项目")}</Button>
    </Head>

    {offline && <div className="note">{t("Offline demo mode: fixed provider fixtures enabled. Supports import, graph build, review edits, export checks & policy replay.", "离线演示模式 · 使用固定回执运行 Agent。支持导入文件、建图、人工改译、原格式检查交付与反馈回放。")}</div>}
    <ErrorNote error={action.error || resources.error || po.error || imports.error || progress.error} />

    {!scope ? <div className="split">
      <div className="split__main">
        <Empty title={t("No project open", "还没有打开任何项目")}>{t("Create a localization project and import CSV, XLSX, or UTF-8 PO files to start.", "创建一个本地化项目，导入 CSV、XLSX 或 UTF-8 PO 源文件后即可开始构建图谱与翻译导出。")}</Empty>
      </div>
      <aside className="split__side">
        <div className="panel">
          <h4>{t("Quick Start", "快速开始")}</h4>
          <p className="muted">{t("1. Create a project and pick source/target languages", "1. 新建项目并选择中英翻译方向")}</p>
          <p className="muted">{t("2. Configure OpenAI-compatible APIs or local Ollama/vLLM in Models", "2. 在「模型设置」选择云端 OpenAI API 或本地 Ollama / vLLM")}</p>
          <p className="muted">{t("3. Import files → Build graph → Translate & Review → Validate & Export", "3. 导入文件 → 构建知识图谱 → 发起翻译与人工复核 → 检查并导出原格式文件")}</p>
          <Button primary onClick={createProject}>{t("Create Project Now", "立即新建项目")}</Button>
        </div>
      </aside>
    </div> : <div className="split">
      <div className="split__main">
        {project && <>
          <div className="stage-strip" role="navigation" aria-label={t("Localization pipeline", "本地化流程")}>
            <button type="button" className={`stage-step${project.resources ? " stage-step--done" : " stage-step--todo"}`}
              onClick={() => setDialog("import")}>
              <span className="stage-step__num">01</span>
              <b>{t("Import Source", "导入源文件")}</b>
              <span className="hint">{fileRows.length ? t(`${fileRows.length} files · ${project.resources ?? 0} rows`, `${fileRows.length} 份文件 · ${project.resources ?? 0} 条`) : t("Click to upload CSV/XLSX/PO", "点击上传 CSV/XLSX/PO")}</span>
            </button>
            <button type="button" className={`stage-step${project.graph_ready ? " stage-step--done" : project.resources ? " stage-step--todo" : ""}`}
              onClick={openGraph}>
              <span className="stage-step__num">02</span>
              <b>{t("Build Source Graph", "构建源图")}</b>
              <span className="hint">{project.graph_ready ? t("Ready · Click to browse", "图谱已就绪 · 点击浏览") : project.resources ? t("Click to build graph", "点击前往构建图谱") : t("Awaiting import", "等待导入")}</span>
            </button>
            <button type="button" className={`stage-step${totalReview > 0 ? " stage-step--warn" : totalDeliverable > 0 ? " stage-step--done" : ""}`}
              onClick={() => navigate("jobs")}>
              <span className="stage-step__num">03</span>
              <b>{t("Translate & Review", "翻译与复核")}</b>
              <span className="hint">{totalReview > 0 ? t(`${totalReview} awaiting review`, `${totalReview} 条待人工复核`) : t(`${jobs?.total ?? 0} jobs · View`, `${jobs?.total ?? 0} 个任务 · 点击查看`)}</span>
            </button>
            <button type="button" className={`stage-step${totalDeliverable > 0 ? " stage-step--done" : ""}`}
              onClick={() => {
                const first = currentFiles[0];
                if (first) setDelivery(first);
                else setDialog("import");
              }}>
              <span className="stage-step__num">04</span>
              <b>{t("Validate & Export", "检查与导出")}</b>
              <span className="hint">{totalDeliverable > 0 ? t(`${totalDeliverable} exportable`, `${totalDeliverable} 条可导出`) : t("Inspect per file below", "在下方文件行点击检查")}</span>
            </button>
          </div>

          <div className="stats">
            {[[project.resources ?? 0, t("Source Rows", "源条目"), project.unextracted ? t(`${project.unextracted} unextracted`, `待抽图 ${project.unextracted}`) : t("All covered", "已全部覆盖")],
              [totalDeliverable, t("Exportable", "可导出"), t("Validated", "通过校验版本")],
              [totalReview, t("Needs Review", "待复核"), t("Human check", "需人工确认")],
              [project.approved_memory ?? 0, t("Approved Memory", "已批准记忆"), t("Terms · Notes · TM", "术语 · 笔记 · 译例")]].map(([count, title, desc]) =>
              <div className="stat" key={String(title)}><b>{count}</b><span>{title}</span><em>{desc}</em></div>)}
          </div>

          <Section>{t("File Progress & Export", "文件进度与检查导出")}</Section>
          {!progress.data && !progress.error && <span className="hint" role="status">{t("Loading file progress…", "正在读取文件进度…")}</span>}
          {progress.data?.files.length === 0 && <Empty title={t("No files imported yet", "还没有导入文件")}>{t("Click '+ Import Source' above (supports CSV, XLSX, UTF-8 PO).", "点击右上角「+ 导入源文件」，支持 CSV、XLSX 或 UTF-8 PO。")}</Empty>}
          {!!fileRows.length && <div className="stack">
            {fileRows.map(file => {
              const imp = importById.get(file.import_id);
              const pct = file.total ? Math.round(100 * file.deliverable / file.total) : 0;
              const isFiltered = selection.file === file.filename;
              return <div className={`panel file-card${isFiltered ? " file-card--active" : ""}`} key={file.import_id}>
                <div className="row-set">
                  <b className="clip">{file.filename}</b>
                  {imp && <span className="pill pill--plain mono">{imp.format.toUpperCase()}</span>}
                  <span className="mono hint">{pct}% {t("ready", "可交付")}</span>
                  <span className="sp" />
                  <Button small ghost aria-pressed={isFiltered} onClick={() => {
                    setOffset(0);
                    setSelection({ ids: [], file: isFiltered ? undefined : file.filename });
                  }}>{isFiltered ? t("All Files", "显示全部文件") : t("Filter File", "筛选此文件")}</Button>
                  <Button small ghost disabled={!project.graph_ready} onClick={() => {
                    setBatchFile(file.filename);
                    setDialog("batches");
                  }}>{t("Batch Translate", "批量翻译")}</Button>
                  {imp && <Button small primary={file.deliverable > 0} onClick={() => setDelivery(imp)}>{t("Validate & Export", "检查与导出")}</Button>}
                </div>
                <div className="meter"><i style={{ width: `${pct}%` }} /></div>
                <div className="row-set file-card__counts">
                  <span>{t("Source", "源条目")} <b className="mono">{file.total}</b></span>
                  <span>{t("Untranslated", "未翻译")} <b className="mono">{file.untranslated}</b></span>
                  <span>{t("Needs Review", "待复核")} {file.awaiting_review > 0
                    ? <Button small ghost className="mono" onClick={() => openFileReview({ projectId: project.project_id, filename: file.filename })}>
                        {t(`${file.awaiting_review} (Review)`, `${file.awaiting_review} 条（去复核）`)}</Button>
                    : <b className="mono muted">0</b>}</span>
                  <span>{t("Ready", "可交付")} <b className="mono">{file.deliverable}</b></span>
                </div>
              </div>;
            })}
          </div>}
        </>}

        {!project && scope === "naev" && <>
          <div className="row-set">
            <Section>{t("Naev Data Source", "Naev 数据源")}</Section>
          </div>
          <div className="row-set">
            <Button small ghost aria-pressed={!upload} onClick={() => { setSelection({ ids: [] }); setOffset(0); }}>{t("Built-in Demo Rows", "内置示范条目")}</Button>
            {upload && <Button small ghost aria-pressed={!!selection.upload} onClick={() => {
              setSelection({ ids: [], upload: upload.upload_id }); setOffset(0);
            }}>{t(`Uploaded PO · ${upload.filename} (${upload.mapped_occurrences})`, `已上传 PO · ${upload.filename}（${upload.mapped_occurrences} 处）`)}</Button>}
            <span className="hint">{t("Select entries and run Translate/Export or Review, then preview & download PO in Job Details.", "选中条目发起「翻译并导出」或「审校」后，可在任务详情中进行 PO 写回检查与下载。")}</span>
          </div>
        </>}

        <Section>{t("Source Entries", "源条目列表")}{selection.file ? ` · ${selection.file}` : ""}</Section>
        <form className="row-set" onSubmit={event => { event.preventDefault(); setOffset(0); setSearch(query.trim()); }}>
          <input className="input" aria-label={t("Search source", "搜索原文")} placeholder={t("Search source or context…", "搜索原文或上下文…")} maxLength={250} value={query} onChange={event => setQuery(event.target.value)} />
          {project && <select className="input" aria-label={t("Filter by file", "按文件筛选")} value={selection.file ?? ""} onChange={event => {
            setOffset(0); setSelection({ ids: [], file: event.target.value || undefined });
          }}><option value="">{t("All Files", "所有文件")}</option>{currentFiles.map(file => <option key={file.import_id}>{file.filename}</option>)}</select>}
          <Button type="submit">{t("Search", "搜索")}</Button>
          {search && <Button ghost onClick={() => { setQuery(""); setSearch(""); setOffset(0); }}>{t("Clear", "清除")}</Button>}
          <span className="sp" />
          <span className="muted">{t("Selected", "已选")} <span className="mono">{selection.ids.length}/{limit}</span></span>
        </form>

        {selection.ids.length > 0 && <div className="selection-bar" role="region" aria-label={t("Selected entry actions", "已选条目操作")}>
          <div className="row-set">
            <b>{t(`${selection.ids.length} selected`, `已选 ${selection.ids.length} 条`)}</b>
            <Button small ghost onClick={() => setSelection({ ...selection, ids: [] })}>{t("Clear", "清空")}</Button>
            <span className="sp" />
            <Button small primary disabled={!!project && !project.graph_ready}
              onClick={() => compose("selection", "translate,export")}>{t("Translate & Export", "翻译并导出")}</Button>
            <Button small disabled={!!project && !project.graph_ready}
              onClick={() => compose("selection", "review")}>{t("Review Only", "仅审校")}</Button>
            <Button small disabled={!!project && !project.graph_ready}
              onClick={() => compose("selection", "explain")}>{t("Explain", "解释依据")}</Button>
            {project && <Button small disabled={!project.graph_ready} onClick={() => {
              setBatchFile(undefined); setDialog("batches");
            }}>{t("Auto-Batch", "自动分批")}</Button>}
            <Button small ghost onClick={() => compose("command")}>{t("Command", "下达任务")}</Button>
          </div>
          {project && !project.graph_ready && <span className="hint">{t("Note: Build the source graph in step 02 above before starting translation jobs.", "提示：当前源版本尚未构建图谱，请先点击上方「02 构建源图」再发起翻译任务。")}</span>}
        </div>}

        {total === 0 && <Empty title={search ? t("No matching entries", "没有匹配的源条目") : t("No imported entries yet", "还没有导入源条目")} />}
        {(!!rows.length || !!po.data?.entries.length) && <div className="table">
          <Grid columns={columns} header>
            <input type="checkbox" aria-label={t("Select all on page", "全选本页条目")} checked={allVisibleSelected} onChange={toggleAllVisible} />
            <span>{t("Source", "原文")}</span><span>{t("File & Location", "文件与位置")}</span><span>{t("Rev", "源版本")}</span><span />
          </Grid>
          {rows.map(item => <Grid key={item.occurrence_id} columns={columns}>
            {checkbox(item.occurrence_id)}<span className="clip" title={item.label}>{item.label}</span>
            <span className="mono clip" title={locationText(item.location, item.file)}>{locationText(item.location, item.file) || "—"}</span>
            <span className="mono">v{item.source_version}</span><Button small ghost onClick={() => setDetail(item)}>{t("Details", "详情")}</Button>
          </Grid>)}
          {po.data?.entries.flatMap(entry => entry.occurrences.length ? entry.occurrences.map(item =>
            <Grid key={item.occurrence_id} columns={columns}>{checkbox(item.occurrence_id)}
              <span className="clip" title={entry.source}>{item.source_form === "plural" ? entry.source_plural || entry.source : entry.source}</span>
              <span className="mono clip">{locationText(item.location)}</span><span>{item.source_form === "plural" ? t("Plural", "复数") : t("Singular", "单数")}</span>
              <Button small ghost onClick={() => setDetail({ occurrence_id: item.occurrence_id, source_version: 0,
                label: [entry.source, entry.source_plural].filter(Boolean).join("\n"), location: item.location,
                metadata: entry.context ? { native_context: entry.context } : {} })}>{t("Details", "详情")}</Button>
            </Grid>) : <Grid key={entry.id} columns={columns}><span /><span className="clip" title={entry.source}>{entry.source}</span>
              <span className="muted">{t("No snapshot match", "暂无源快照映射")}</span><span /><span /></Grid>)}
        </div>}
        {total !== undefined && <Pager total={total} offset={offset} size={size} setOffset={setOffset} />}
      </div>

      <aside className="split__side">
        <div className="panel">
          <div className="row-set">
            <h4>{t("Projects & Workspace", "项目与工作区")}</h4>
            <span className="sp" />
            <Button small ghost onClick={createProject}>{t("+ New", "+ 新建")}</Button>
          </div>
          {naev && <div className="row-set">
            <b className="clip">{t("Naev Demo", "Naev 示范区")}</b><span className="hint">EN → ZH</span><span className="sp" />
            {scope === "naev" ? <Status value="active" /> : <Button small ghost onClick={() => choose("naev")}>{t("Switch", "切换")}</Button>}
          </div>}
          {projects.map(item => <div className="row-set" key={item.project_id}>
            <b className="clip" title={item.name}>{item.name}</b>
            <Status value={item.graph_snapshot && item.graph_revision === item.revision ? "valid" : "pending"} />
            <span className="sp" />
            {scope === item.project_id ? <Status value="active" /> : <Button small ghost onClick={() => choose(item.project_id)}>{t("Switch", "切换")}</Button>}
          </div>)}
          {!projects.length && !naev && <p className="muted">{t("No other projects", "暂无其他项目")}</p>}
        </div>

        {project && !!imports.data?.imports.length && <details className="panel">
          <summary>{t(`Import History (${imports.data.imports.length})`, `历史导入批次（${imports.data.imports.length}）`)}</summary>
          <div className="stack">
            {imports.data.imports.map(file => <div className="row-set" key={file.import_id}>
              <span className="clip" style={{ flex: 1 }} title={file.filename}>{file.filename}</span>
              <span className="mono hint">{file.rows} · v{file.project_revision}</span>
              <Button small ghost onClick={() => setDelivery(file)}>{t("Check", "检查")}</Button>
            </div>)}
          </div>
        </details>}
      </aside>
    </div>}

    {project && dialog === "import" && <ImportDialog project={project} close={() => setDialog(null)} changed={() => {
      setSelection({ ids: [] }); setOffset(0); changed();
    }} />}
    {project && dialog === "batches" && <BatchDialog project={project} selection={selection} initialFile={batchFile}
      close={() => { setDialog(null); setBatchFile(undefined); }} openJob={openJob} />}
    {project && delivery && <Deliver project={project} file={delivery} close={() => setDelivery(undefined)} />}
    {detail && <Modal title={t("Source Entry Details", "源条目详情")} close={() => setDetail(undefined)}>
      <div className="source text-block">{detail.label}</div>
      <span className="mono muted">{locationText(detail.location, detail.file)} · {detail.key ?? detail.occurrence_id}</span>
      <div className="ctxgrid">{Object.entries(detail.metadata ?? {}).map(([key, value]) =>
        <div className="ctxcell" key={key}><span>{metaLabel(key, locale)}</span><b>{value}</b></div>)}</div>
      <div className="dialog__foot">
        <Button onClick={() => {
          if (!selection.ids.includes(detail.occurrence_id) && selection.ids.length < limit) select(detail.occurrence_id);
          setDetail(undefined);
        }}>{selection.ids.includes(detail.occurrence_id) ? t("Already Selected", "已在选中列表") : t("Select Entry", "勾选此条")}</Button>
      </div>
    </Modal>}
  </>;
}
