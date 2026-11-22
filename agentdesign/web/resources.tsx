import { useState } from "react";
import { type FileReviewScope, type Project, type Resource, type Selection, type Location, json, params, post, useQuery, locationText, download, saveBlob, reasonText } from "./api";
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
type ExportReport = { translated: number; excluded: string[];
  exclusion_details: { occurrence_id: string; source: string; location: Location; reason: string }[];
  validation: { ok: boolean; level?: string; diagnostics?: string; reason?: string } | null };
type FileProgress = { filename: string; import_id: string; total: number; untranslated: number; awaiting_review: number; deliverable: number };
const columns = "25px minmax(180px, 2fr) minmax(120px, 1fr) 80px 62px";
const fileColumns = "minmax(130px, 1fr) 72px 72px 82px 72px";
const metadata = {
  text_type: "文本类型", native_context: "上下文", document: "文档", scene: "场景", block: "对话块",
  branch_id: "剧情分支", phase_id: "阶段", speaker_id: "说话人", addressee_id: "听话人", topic_id: "话题", requirements: "翻译要求",
};

function ImportDialog({ project, close, changed }: { project: Project; close: () => void; changed: () => void }) {
  const [file, setFile] = useState<File>();
  const [options, setOptions] = useState({ id_column: "id", source_column: "source_text", sheet: "", target_column: "" });
  const [mapping, setMapping] = useState<Record<string, string>>({});
  const [prepared, setPrepared] = useState<{ preview: Import; url: string; key: string }>();
  const action = useAction();
  const table = file && !file.name.toLowerCase().endsWith(".po");
  return <Modal title="导入源文件" close={close}>
    <ErrorNote error={action.error} />
    <form className="stack" onSubmit={event => {
      event.preventDefault();
      if (!file) return;
      void action.run(async () => {
        const spec = table ? { id_column: options.id_column, source_column: options.source_column,
          target_column: options.target_column.trim() || undefined, sheet: options.sheet.trim() || undefined,
          columns: Object.fromEntries(Object.entries(mapping).filter(([, name]) => !!name.trim())) } : {};
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
      <Field label="源文件（CSV、XLSX 或 UTF-8 PO，最多 16 MB）"><input className="input" type="file" required
        accept=".csv,.xlsx,.po" disabled={action.busy} onChange={event => { setPrepared(undefined); setFile(event.target.files?.[0]); }} /></Field>
      {table && <details open={!prepared}><summary>列映射</summary>
        <div className="form-grid">
          {Object.entries({ id_column: "ID 列", source_column: "原文列", target_column: "原文件译文列", sheet: "工作表" }).map(([key, title]) =>
            (key !== "sheet" || file.name.toLowerCase().endsWith(".xlsx")) && <Field key={key} label={title}>
              <input className="input" value={options[key as keyof typeof options]} disabled={action.busy}
                required={key === "id_column" || key === "source_column"} onChange={event => {
                  setPrepared(undefined); setOptions(previous => ({ ...previous, [key]: event.target.value }));
                }} />
            </Field>)}
        </div>
        <details><summary>游戏上下文列</summary><div className="form-grid">
          {Object.entries(metadata).map(([key, title]) => <Field key={key} label={title}>
            <input className="input" placeholder="使用默认映射" disabled={action.busy} value={mapping[key] ?? ""} onChange={event => {
              setPrepared(undefined); setMapping(previous => ({ ...previous, [key]: event.target.value }));
            }} />
          </Field>)}
        </div></details>
      </details>}
      {prepared && <div className="stack">
        <div className="note note--ok"><b>预检完成 · {prepared.preview.rows} 条源文</b>
          新增 {prepared.preview.changes.added} · 修改 {prepared.preview.changes.changed} · 沿用 {prepared.preview.changes.unchanged}
          {" · "}移除 {prepared.preview.changes.removed} · 跳过 {prepared.preview.skipped.length}
        </div>
        {!!prepared.preview.skipped.length && <details><summary>跳过的源行</summary><ul className="scroll-list">
          {prepared.preview.skipped.map((row, index) => <li key={index}>第 {row.row ?? row.line} 行 · {row.key && row.key + " · "}
            {reasonText(row.reason)} <code>{row.reason}</code></li>)}
        </ul></details>}
        {(prepared.preview.changes.changed > 0 || prepared.preview.changes.removed > 0) &&
          <div className="note note--warn">修改或移除原文会使对应的旧证据和依赖记忆失效。</div>}
      </div>}
      <div className="dialog__foot"><Button type="submit" primary disabled={!file || action.busy}>
        {action.busy ? "正在读取文件…" : prepared ? "确认导入" : "导入预检"}
      </Button></div>
    </form>
  </Modal>;
}

function BatchDialog({ project, selection, close, openJob }: {
  project: Project; selection: Selection; close: () => void; openJob: (id: string) => void;
}) {
  const action = useAction();
  return <Modal title="自动分批翻译" close={close}>
    <ErrorNote error={action.error} />
    <form className="stack" onSubmit={event => {
      event.preventDefault();
      const form = new FormData(event.currentTarget);
      void action.run(async () => {
        const result = await json<{ jobs: { job_id: string }[] }>(`/projects/${project.project_id}/translation-batches`, post({
          ...(form.get("scope") === "selected" ? { targets: selection.ids } : selection.file ? { file: selection.file } : {}),
          batch_size: Number(form.get("size")), max_calls_per_batch: Number(form.get("limit")),
          requirements: String(form.get("requirements")).trim() || undefined,
        }));
        openJob(result.jobs[0].job_id);
      });
    }}>
      <Field label="范围"><select name="scope" className="input" defaultValue={selection.ids.length ? "selected" : "file"}>
        <option value="file">{selection.file || "项目全部文件"}</option>
        <option value="selected" disabled={!selection.ids.length}>所选条目（{selection.ids.length}）</option>
      </select></Field>
      <div className="form-grid">
        <Field label="每批条数"><input className="input" type="number" name="size" min="1" max="64" defaultValue="12" required /></Field>
        <Field label="单批调用上限"><input className="input" type="number" name="limit" min="1" max="1000" defaultValue="72" required /></Field>
      </div>
      <Field label="补充要求"><textarea className="textarea" name="requirements" maxLength={5000} /></Field>
      <p>前一批完成后，下一批会读取最新的已批准记忆。</p>
      <div className="dialog__foot"><Button primary type="submit" disabled={action.busy}>创建批次</Button></div>
    </form>
  </Modal>;
}

function Deliver({ project, file, close }: { project: Project; file: Import; close: () => void }) {
  const path = `/projects/${project.project_id}/imports/${file.import_id}/export`;
  const report = useQuery<ExportReport>(path + "/report");
  const action = useAction();
  return <Modal title={`交付 · ${file.filename}`} close={close}>
    <ErrorNote error={report.error || action.error} />
    {!report.data && !report.error && <p>正在检查译文版本…</p>}
    {report.data && <>
      <div className="note"><b>将写回 {report.data.translated} 条译文</b>排除 {report.data.excluded.length} 条 · 源版本 {file.project_revision}</div>
      {report.data.validation && <><Status value={report.data.validation.ok ? "passed" : "failed"} />
        <ErrorNote reason={report.data.validation.reason} />
        {report.data.validation.diagnostics && <pre>{report.data.validation.diagnostics}</pre>}</>}
      {!!report.data.excluded.length && <details><summary>排除项</summary><ul className="scroll-list">
        {report.data.exclusion_details.map(item => <li key={item.occurrence_id}>
          <strong>{item.source}</strong><p>{reasonText(item.reason)} <code>{item.reason}</code></p>
          <span className="hint">{locationText(item.location)}</span>
        </li>)}
      </ul></details>}
      <div className="dialog__foot">
        <Button onClick={() => saveBlob(new Blob([JSON.stringify(report.data, null, 2)], { type: "application/json" }),
          `${file.filename}.report.json`)}>下载报告</Button>
        <Button primary disabled={action.busy || report.data.validation?.ok === false}
          onClick={() => void action.run(() => download(path, file.filename))}>下载原格式文件</Button>
      </div>
    </>}
  </Modal>;
}

export function Resources({ project, upload, setUpload, selection, setSelection, refresh, changed, openJob, compose, openGraph, openFileReview }: {
  project?: Project; upload: Upload | null; setUpload: (upload: Upload) => void; selection: Selection;
  setSelection: (selection: Selection) => void; refresh: number; changed: () => void;
  openJob: (id: string) => void; compose: () => void; openGraph: () => void; openFileReview: (scope: FileReviewScope) => void;
}) {
  const size = 25;
  const limit = project ? 64 : 12;
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState("");
  const [offset, setOffset] = useState(0);
  const [dialog, setDialog] = useState<"import" | "batches" | null>(null);
  const [delivery, setDelivery] = useState<Import>();
  const [detail, setDetail] = useState<Resource>();
  const action = useAction();
  const base = project ? `/projects/${project.project_id}` : null;
  const resources = useQuery<{ resources: Resource[]; total: number }>(
    base ? base + `/resources?${params({ query: search, offset, limit: size, file: selection.file })}` : upload ? null : "/resources", refresh);
  const po = useQuery<{ entries: PoEntry[]; total: number }>(!base && upload ?
    `/po-uploads/${upload.upload_id}/entries?${params({ query: search, offset, limit: size })}` : null, refresh);
  const imports = useQuery<{ imports: Import[] }>(base && base + "/imports", refresh);
  const progress = useQuery<{ project_revision: number; files: FileProgress[] }>(base && base + "/files/progress", refresh);
  const currentFiles = [...new Map((imports.data?.imports ?? []).slice().reverse().map(file => [file.filename, file])).values()];
  const all = resources.data?.resources ?? [];
  const native = !project && !upload ? all.filter(item => [item.label, item.file, item.occurrence_id]
    .some(value => value?.toLowerCase().includes(search.toLowerCase()))) : [];
  const total = project ? resources.data?.total : upload ? po.data?.total : native.length;
  const rows = project ? all : upload ? [] : native.slice(offset, offset + size);
  const select = (id: string) => {
    const ids = selection.ids.includes(id) ? selection.ids.filter(item => item !== id) : [...selection.ids, id];
    setSelection({ ...selection, ids });
  };
  const checkbox = (id: string) => <input type="checkbox" aria-label={`选择 ${id}`}
    checked={selection.ids.includes(id)} disabled={!selection.ids.includes(id) && selection.ids.length >= limit}
    onChange={() => select(id)} />;
  return <>
    <Head title="资源库" sub={project ? "CSV、XLSX 与 PO 源文件" : upload?.filename || "Naev 冻结快照 · 可上传简体中文 PO"}>
      {project ? <Button onClick={() => setDialog("import")} primary={!selection.ids.length}>导入文件</Button> :
        <label className="btn">上传 PO<input className="file-control" type="file" accept=".po" disabled={action.busy}
          onChange={event => {
            const file = event.target.files?.[0];
            if (file) void action.run(async () => {
              const next = await json<Upload>(`/po-uploads?${params({ filename: file.name })}`, { method: "POST", body: file });
              setUpload(next); setSelection({ ids: [], upload: next.upload_id }); setOffset(0);
            });
          }} /></label>}
    </Head>
    <ErrorNote error={action.error || resources.error || po.error || imports.error} />
    {project && currentFiles.length > 0 && !project.graph_ready && <div className="note note--warn">
      <div className="row-set"><span>源版本 {project.revision} 等待发布图谱。发布后可开始翻译。</span>
        <Button small ghost onClick={openGraph}>前往建图</Button></div>
    </div>}
    {project && <>
      <Section>文件进度{progress.data && ` · 源 v${progress.data.project_revision}`}</Section>
      <ErrorNote error={progress.error} />
      {!progress.data && !progress.error && <span className="hint" role="status">正在读取文件进度…</span>}
      {progress.data?.files.length === 0 && <Empty title="还没有导入文件" />}
      {!!progress.data?.files.length && <section aria-label="文件进度" className="table">
        <Grid columns={fileColumns} header><span>文件</span><span>源条目</span>
          <span title="尚无可交付译文或待复核任务">未翻译</span><span>待复核</span>
          <span title="符合当前源版本的写回要求。重新送审的条目也可以保留可交付译文。">可交付</span>
        </Grid>
        {progress.data.files.map(file => <Grid key={file.import_id} columns={fileColumns}>
          <span className="clip" title={file.filename}>{file.filename}</span>
          <span className="mono">{file.total}</span><span className="mono">{file.untranslated}</span>
          {file.awaiting_review ? <Button small ghost className="mono"
            aria-label={`${file.filename} 待复核 ${file.awaiting_review} 条`}
            onClick={() => openFileReview({ projectId: project.project_id, filename: file.filename })}>{file.awaiting_review} 条</Button>
            : <span className="mono muted">0</span>}
          <span className="mono">{file.deliverable}</span>
        </Grid>)}
      </section>}
    </>}
    {project && !!currentFiles.length && <details>
      <summary>导入记录（{imports.data?.imports.length}）与文件交付</summary>
      {(imports.data?.imports ?? []).map(file => <Grid key={file.import_id} columns="minmax(100px, 1fr) 90px 80px 95px">
        <span className="clip" title={file.filename}>{file.filename}</span><span className="mono">{file.rows} 条</span>
        <span className="mono">源 v{file.project_revision}</span><Button small ghost onClick={() => setDelivery(file)}>检查与交付</Button>
      </Grid>)}
    </details>}
    <Section>源条目</Section>
    <form className="row-set" onSubmit={event => { event.preventDefault(); setOffset(0); setSearch(query.trim()); }}>
      <input className="input" aria-label="搜索原文" placeholder="搜索原文" maxLength={250} value={query} onChange={event => setQuery(event.target.value)} />
      {project && <select className="input" aria-label="按文件筛选" value={selection.file ?? ""} onChange={event => {
        setOffset(0); setSelection({ ids: [], file: event.target.value || undefined });
      }}><option value="">所有文件</option>{currentFiles.map(file => <option key={file.import_id}>{file.filename}</option>)}</select>}
      <Button type="submit">搜索</Button>
      {!project && upload && <Button ghost onClick={() => { setSelection({ ids: [] }); setOffset(0); }}>查看示范条目</Button>}
      <span className="sp" />
      <span className="muted">已选 <span className="mono">{selection.ids.length}/{limit}</span></span>
      {!!selection.ids.length && <Button small ghost onClick={() => setSelection({ ...selection, ids: [] })}>清空选择</Button>}
      {project && <Button disabled={!project.graph_ready} onClick={() => setDialog("batches")}>自动分批</Button>}
      <Button primary={!!selection.ids.length} disabled={!selection.ids.length || (!!project && !project.graph_ready)} onClick={compose}>发起任务</Button>
    </form>
    {total === 0 && <Empty title={search ? "没有匹配的源条目" : "还没有导入源条目"} />}
    {(!!rows.length || !!po.data?.entries.length) && <div className="table">
      <Grid columns={columns} header><span /><span>原文</span><span>文件与位置</span><span>源版本</span><span /></Grid>
      {rows.map(item => <Grid key={item.occurrence_id} columns={columns}>
        {checkbox(item.occurrence_id)}<span className="clip" title={item.label}>{item.label}</span>
        <span className="mono clip" title={locationText(item.location, item.file)}>{locationText(item.location, item.file) || "—"}</span>
        <span className="mono">v{item.source_version}</span><Button small ghost onClick={() => setDetail(item)}>详情</Button>
      </Grid>)}
      {po.data?.entries.flatMap(entry => entry.occurrences.length ? entry.occurrences.map(item =>
        <Grid key={item.occurrence_id} columns={columns}>{checkbox(item.occurrence_id)}
          <span className="clip" title={entry.source}>{item.source_form === "plural" ? entry.source_plural || entry.source : entry.source}</span>
          <span className="mono clip">{locationText(item.location)}</span><span>{item.source_form === "plural" ? "复数" : "单数"}</span>
          <Button small ghost onClick={() => setDetail({ occurrence_id: item.occurrence_id, source_version: 0,
            label: [entry.source, entry.source_plural].filter(Boolean).join("\n"), location: item.location,
            metadata: entry.context ? { native_context: entry.context } : {} })}>详情</Button>
        </Grid>) : <Grid key={entry.id} columns={columns}><span /><span className="clip" title={entry.source}>{entry.source}</span>
          <span className="muted">暂无源快照映射</span><span /><span /></Grid>)}
    </div>}
    {total !== undefined && <Pager total={total} offset={offset} size={size} setOffset={setOffset} />}
    {project && dialog === "import" && <ImportDialog project={project} close={() => setDialog(null)} changed={() => {
      setSelection({ ids: [] }); setOffset(0); changed();
    }} />}
    {project && dialog === "batches" && <BatchDialog project={project} selection={selection} close={() => setDialog(null)} openJob={openJob} />}
    {project && delivery && <Deliver project={project} file={delivery} close={() => setDelivery(undefined)} />}
    {detail && <Modal title="源条目详情" close={() => setDetail(undefined)}>
      <div className="source text-block">{detail.label}</div>
      <span className="mono muted">{locationText(detail.location, detail.file)} · {detail.key ?? detail.occurrence_id}</span>
      <div className="ctxgrid">{Object.entries(detail.metadata ?? {}).map(([key, value]) =>
        <div className="ctxcell" key={key}><span>{metadata[key as keyof typeof metadata] ?? key}</span><b>{value}</b></div>)}</div>
    </Modal>}
  </>;
}
