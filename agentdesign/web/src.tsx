import { useEffect, useState, type ReactNode } from "react";
import { createRoot } from "react-dom/client";

type Location = { path?: string; line?: number; xpath?: string };
type Occurrence = { occurrence_id: string; label: string; source_form: string; location: Location };
type Entry = { id: string; context: string | null; source: string; source_plural: string | null; occurrences: Occurrence[] };
type Catalog = { entries: Entry[]; total: number };
type Upload = { upload_id: string; filename: string; entries: number; mapped_occurrences: number };
type Item = { occurrence_id: string; step: number; source: string; target: string | null; status: string;
  reason: string | null; display_reason?: string | null; expected_version: number;
  draft_hash: string | null; evidence_url: string };
type Job = { job_id: string; status: string; reason: string | null; items: Item[];
  source_kind: "text" | "naev"; source_lang: string | null; target_lang: string | null;
  steps: { intent: string; status: string }[]; next_actions: string[];
  counts: { items: number; passed: number; exportable: number; awaiting_review: number };
  missing: Record<string, { reason: string; choices?: string[] }>;
  artifacts: { type: string; url: string }[] };
type Evidence = { retrieved: { id: string; text: string }[];
  actual_context: { items: { id: string; text: string }[] } | null;
  actual_sources: { occurrence_id: string; source_version: number; quote: string; location: Location | null }[];
  source_lookups: { name: string; effective_query?: string | null; args: { query?: string }; result: { status: string } }[];
  reviews: { verdict: string; reason: string }[] };
type Release = { occurrence_id: string; version: number; target: string };
type Report = { validation: { ok: boolean; level: string; diagnostics?: string; reason?: string };
  download_ready: boolean; applied: number; changes: unknown[];
  conflicts: { entry_id: string; source: string; versions: Release[] }[];
  exclusions: { occurrence_id?: string; entry_id?: string; reason: string }[] };

const terminal = new Set(["completed", "partial", "failed", "cancelled"]);
const statuses: Record<string, string> = {
  queued: "排队中", running: "执行中", awaiting_input: "待补充", awaiting_review: "待复核",
  completed: "已完成", partial: "部分完成", failed: "失败", cancelled: "已取消",
  committed: "已提交", needs_human: "待人工", locked: "已锁定", conflict: "版本冲突",
  excluded: "已排除", no_change: "沿用已有版本",
};
const label = (status: string) => statuses[status] || status;

async function request(path: string, init?: RequestInit) {
  const response = await fetch(path, init);
  if (!response.ok) {
    const details = await response.json().catch(() => ({}));
    throw new Error(details.reason || `HTTP ${response.status}`);
  }
  return response;
}
async function json<T>(path: string, init?: RequestInit): Promise<T> {
  return (await request(path, init)).json();
}
function post(payload: unknown): RequestInit {
  return { method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify(payload) };
}

type GlossaryScope = { occurrence_ids?: string[]; file?: string; branch_id?: string };
type GlossaryRule = { rule_id: string; source: string; target: string; version: number; scope: GlossaryScope;
  matched_occurrences: number; memory_ref?: unknown };
type MaintenancePlan = { plan_id: string; status: string; rule: GlossaryRule; job_id: string | null; job_status?: string;
  items: { occurrence_id: string; source: string; before_version: number; before: string; proposed: string | null;
    status: string; reason: string; discovery: string[]; result?: string; result_reason?: string;
    after?: string; after_version?: number; model_calls?: number }[] };

function Glossary({ selected, openJob }: { selected: Occurrence[]; openJob: (job: Job) => void }) {
  const [rules, setRules] = useState<GlossaryRule[]>([]);
  const [ruleId, setRuleId] = useState("");
  const [source, setSource] = useState("");
  const [target, setTarget] = useState("");
  const [previous, setPrevious] = useState("");
  const [scopeMode, setScopeMode] = useState("selected");
  const [file, setFile] = useState("");
  const [branch, setBranch] = useState("");
  const [plan, setPlan] = useState<MaintenancePlan | null>(null);
  const [activeId, setActiveId] = useState(() => localStorage.getItem("naev-maintenance") || "");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function refreshRules() {
    setRules((await json<{rules: GlossaryRule[]}>("/glossary")).rules);
  }
  useEffect(() => { void refreshRules().catch(err => setError(err.message)); }, []);
  useEffect(() => {
    if (!activeId) return;
    let stopped = false;
    async function refresh() {
      try {
        const current = await json<MaintenancePlan>(`/maintenance/${activeId}`);
        if (!stopped) setPlan(current);
      } catch (err) {
        if (!stopped) setError((err as Error).message);
      }
    }
    void refresh();
    const timer = setInterval(() => void refresh(), 1800);
    return () => { stopped = true; clearInterval(timer); };
  }, [activeId]);

  async function act(action: () => Promise<void>) {
    setError(""); setBusy(true);
    try { await action(); } catch (err) { setError((err as Error).message); } finally { setBusy(false); }
  }
  const existing = rules.find(rule => rule.rule_id === ruleId);

  return <section aria-labelledby="glossary">
    <h2 id="glossary">术语维护</h2>
    {error && <p className="error" role="alert">{error}</p>}
    <form onSubmit={event => {
      event.preventDefault();
      void act(async () => {
        const scope = scopeMode === "existing" ? existing?.scope :
          { ...(scopeMode === "selected" ? { occurrence_ids: selected.map(item => item.occurrence_id) } : {}),
            ...(file.trim() ? { file: file.trim() } : {}), ...(branch.trim() ? { branch_id: branch.trim() } : {}) };
        const next = await json<MaintenancePlan>("/maintenance/preview", post({
          ...(ruleId ? { rule_id: ruleId } : {}), source, target, scope, ...(previous ? { previous_target: previous } : {}),
        }));
        setPlan(next); setActiveId(next.plan_id); localStorage.setItem("naev-maintenance", next.plan_id);
      });
    }}>
      <label>已登记规则 <select value={ruleId} onChange={event => {
        const id = event.target.value;
        const rule = rules.find(entry => entry.rule_id === id);
        setRuleId(id); setSource(rule?.source || ""); setPrevious(rule?.target || "");
        setTarget(rule?.target || ""); setScopeMode(rule ? "existing" : "selected");
      }}><option value="">新规则</option>
        {rules.map(rule => <option key={rule.rule_id} value={rule.rule_id}>
          {rule.source} → {rule.target} · v{rule.version}{rule.memory_ref ? "" : "（已退役）"}
        </option>)}
      </select></label>
      <div className="row">
        <label>原文术语 <input required value={source} onChange={event => setSource(event.target.value)} /></label>
        <label>新译法 <input required value={target} onChange={event => setTarget(event.target.value)} /></label>
        <label>旧译法（可选） <input value={previous} onChange={event => setPrevious(event.target.value)} /></label>
      </div>
      <div className="row">
        <label>作用域 <select value={scopeMode} onChange={event => setScopeMode(event.target.value)}>
          <option value="selected">所选出现位置（{selected.length}）</option>
          <option value="project">项目或文件范围</option>
          {existing && <option value="existing">沿用已有作用域</option>}
        </select></label>
        {scopeMode !== "existing" && <><label>文件（可选） <input value={file} onChange={event => setFile(event.target.value)} /></label>
          <label>分支（可选） <input value={branch} onChange={event => setBranch(event.target.value)} /></label></>}
        <button disabled={busy || (scopeMode === "selected" && selected.length === 0)}>预览术语变更</button>
      </div>
    </form>
    {plan && <>
      <p>{plan.rule.source} → {plan.rule.target} · 规则 v{plan.rule.version} · {plan.rule.matched_occurrences} 个源位置
        · {plan.status} {plan.job_status && "· " + label(plan.job_status)}</p>
      <div className="row">
        {plan.status === "preview" && <button disabled={busy} onClick={() => void act(async () => {
          const applied = await json<{job_id: string | null}>(`/maintenance/${plan.plan_id}/apply`, post({}));
          setPlan(await json<MaintenancePlan>(`/maintenance/${plan.plan_id}`));
          await refreshRules();
          if (applied.job_id) openJob(await json<Job>(`/jobs/${applied.job_id}`));
        })}>确认应用</button>}
        {plan.job_id && <button disabled={busy} onClick={() => void act(async () => {
          openJob(await json<Job>(`/jobs/${plan.job_id}`));
        })}>打开维护任务</button>}
        {plan.status === "applied" && (!plan.job_status || terminal.has(plan.job_status)) &&
          <button disabled={busy} onClick={() => void act(async () => {
            await request(`/maintenance/${plan.plan_id}/rollback`, post({}));
            setPlan(await json<MaintenancePlan>(`/maintenance/${plan.plan_id}`));
            await refreshRules();
          })}>回滚这次变更</button>}
      </div>
      {plan.items.map(item => <article key={item.occurrence_id}>
        <pre>{item.source}</pre>
        <p>{item.result || item.status} · {item.result_reason || item.reason} · {item.discovery.join(" / ")}</p>
        <p>v{item.before_version} {item.before} → {plan.status === "preview" ?
          item.proposed || "保持原样或等待审校" :
          `v${item.after_version || item.before_version} ${item.after || ""}`} · 调用 {item.model_calls || 0}</p>
      </article>)}
    </>}
  </section>;
}

function App() {
  const [upload, setUpload] = useState<Upload | null>(() => JSON.parse(localStorage.getItem("naev-upload") || "null"));
  const [jobId, setJobId] = useState(() => localStorage.getItem("naev-job") || "");
  const [job, setJob] = useState<Job | null>(null);
  const [page, setPage] = useState<Catalog>({ entries: [], total: 0 });
  const [offset, setOffset] = useState(0);
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<Occurrence[]>([]);
  const [text, setText] = useState("");
  const [sourceText, setSourceText] = useState("");
  const [targetLang, setTargetLang] = useState("en");
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [reviewReasons, setReviewReasons] = useState<Record<string, string>>({});
  const [itemEvidence, setItemEvidence] = useState<Record<string, Evidence>>({});
  const [report, setReport] = useState<Report | null>(null);
  const [validated, setValidated] = useState(false);
  const [choices, setChoices] = useState<Record<string, string>>({});
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [latest, setLatest] = useState("");

  async function act(action: () => Promise<void>) {
    setError("");
    setBusy(true);
    try { await action(); }
    catch (failure) { setError(String((failure as Error).message)); }
    finally { setBusy(false); }
  }

  useEffect(() => {
    if (!upload) return;
    const controller = new AbortController();
    const parameters = new URLSearchParams({ query: search, offset: String(offset), limit: "20" });
    json<Catalog>(`/po-uploads/${upload.upload_id}/entries?${parameters}`, { signal: controller.signal })
      .then(setPage).catch((failure: Error) => { if (failure.name !== "AbortError") setError(failure.message); });
    return () => controller.abort();
  }, [upload, offset, search]);

  useEffect(() => {
    if (!jobId) return;
    let cancelled = false;
    const cursorKey = "naev-events-" + jobId;
    const stream = new EventSource(`/jobs/${jobId}/events?after=${localStorage.getItem(cursorKey) || "0"}`);
    async function refresh() {
      try {
        const current = await json<Job>(`/jobs/${jobId}`);
        if (cancelled) return;
        setJob(current);
        if (terminal.has(current.status)) stream.close();
      } catch (failure) {
        if (!cancelled) setError((failure as Error).message);
        stream.close();
      }
    }
    for (const kind of ["job", "planned", "step", "progress", "item", "approved", "version_action"]) {
      stream.addEventListener(kind, ((event: MessageEvent) => {
        localStorage.setItem(cursorKey, event.lastEventId);
        const data = JSON.parse(event.data);
        setLatest([data.stage || kind, data.item_status || data.status || ""].join(" "));
        void refresh();
      }) as EventListener);
    }
    stream.onerror = () => { void refresh(); };
    void refresh();
    return () => { cancelled = true; stream.close(); };
  }, [jobId]);

  async function uploadFile(file: File) {
    const data = await json<Upload>(`/po-uploads?filename=${encodeURIComponent(file.name)}`, {
      method: "POST", headers: { "Content-Type": "text/x-gettext-translation" }, body: file,
    });
    localStorage.setItem("naev-upload", JSON.stringify(data));
    setUpload(data); setSelected([]); setOffset(0); setSearch(""); setQuery("");
  }

  async function start() {
    const ids = selected.map(item => item.occurrence_id);
    const intent = text.trim() ? { text } : { intents: ["translate", "export"] };
    const next = await json<Job>("/jobs", post({
      ...intent, upload_id: upload?.upload_id, selection: { source_ids: ids, target_lang: "zh" },
    }));
    openJob(next);
  }

  async function translateText() {
    const next = await json<Job>("/jobs", post({ source_text: sourceText, target_lang: targetLang }));
    openJob(next);
  }

  function openJob(next: Job) {
    localStorage.setItem("naev-job", next.job_id);
    setJob(next); setJobId(next.job_id); setLatest(""); setDrafts({}); setItemEvidence({});
    setReviewReasons({});
    setReport(null); setValidated(false); setChoices({});
  }

  async function download(endpoint: string, filename: string) {
    const data = await (await request(`/jobs/${jobId}/po/${endpoint}`, post({ choices }))).blob();
    const url = URL.createObjectURL(data);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function details(item: Item): ReactNode {
    const evidence = itemEvidence[item.evidence_url];
    if (!evidence) return null;
    return <div>
      <p>主动查询</p>
      <ul>{evidence.source_lookups.map((entry, i) => <li key={i}>{entry.name} {entry.args.query ?? entry.effective_query} → {entry.result.status}</li>)}</ul>
      <details open><summary>实际送入模型的证据</summary>
        {evidence.actual_context?.items.map(entry => <p key={entry.id}><code>{entry.id}</code><br />{entry.text}</p>)}
        {evidence.actual_sources.map((entry, i) => <p key={i}><code>{entry.occurrence_id}@{entry.source_version}
          {entry.location?.path && ` · ${entry.location.path}:${entry.location.line || ""}`}</code><br />{entry.quote}</p>)}
      </details>
      <details><summary>检索召回（{evidence.retrieved.length}）</summary>
        {evidence.retrieved.map(entry => <p key={entry.id}><code>{entry.id}</code> {entry.text}</p>)}
      </details>
      {evidence.reviews.map((review, i) => <p key={i}>{review.verdict}：{review.reason}</p>)}
    </div>;
  }

  return <main>
    <h1>Naev 本地化工作台</h1>
    {error && <p className="error" role="alert">{error}</p>}
    <section aria-labelledby="resources">
      <h2 id="resources">资源</h2>
      <label>上传简体中文 PO <input type="file" accept=".po" disabled={busy}
        onChange={e => { const file = e.target.files?.[0]; if (file) void act(() => uploadFile(file)); }} /></label>
      {upload && <>
        <p className="muted">{upload.filename} · {upload.entries} 个 PO 键 · {upload.mapped_occurrences} 个可绑定出现位置</p>
        <form className="row" onSubmit={e => { e.preventDefault(); setSearch(query); setOffset(0); }}>
          <input aria-label="搜索原文" placeholder="搜索原文或上下文" value={query} onChange={e => setQuery(e.target.value)} />
          <button>搜索</button>
          <button type="button" disabled={offset === 0} onClick={() => setOffset(offset - 20)}>上一页</button>
          <button type="button" disabled={offset + 20 >= page.total} onClick={() => setOffset(offset + 20)}>下一页</button>
          <span>{page.total} 项 · 已选 {selected.length}/12</span>
        </form>
        {page.entries.map(entry => <article key={entry.id}>
          <pre>{entry.source}{entry.source_plural ? "\n" + entry.source_plural : ""}</pre>
          {entry.context && <code>{entry.context}</code>}
          {entry.occurrences.length === 0 && <p className="muted">当前源快照暂无映射</p>}
          {entry.occurrences.map(item => {
            const checked = selected.some(current => current.occurrence_id === item.occurrence_id);
            return <label key={item.occurrence_id}><input type="checkbox" aria-label={item.occurrence_id}
              checked={checked} disabled={!checked && selected.length >= 12}
              onChange={() => setSelected(items => checked ? items.filter(x => x.occurrence_id !== item.occurrence_id) : [...items, item])} />
              {item.location.path || item.occurrence_id}{item.location.line ? ":" + item.location.line : ""} · {item.source_form}</label>;
          })}
        </article>)}
      </>}
    </section>
    <section aria-labelledby="work">
      <h2 id="work">任务</h2>
      <form onSubmit={e => { e.preventDefault(); void act(translateText); }}>
        <h3>文本翻译</h3>
        <label>方向 <select value={targetLang} onChange={e => setTargetLang(e.target.value)}>
          <option value="en">中译英</option><option value="zh">英译中</option>
        </select></label>
        <label>原文<textarea maxLength={5000} value={sourceText} onChange={e => setSourceText(e.target.value)} /></label>
        <button disabled={!sourceText.trim() || busy}>翻译文本</button>
      </form>
      <h3>Naev 资源任务</h3>
      <textarea aria-label="任务要求" placeholder="可输入自然语言任务，留空时翻译并导出" value={text} onChange={e => setText(e.target.value)} />
      <button disabled={!selected.length || busy} onClick={() => void act(start)}>运行所选条目</button>
      {job && <>
        {job.source_lang && job.target_lang && <p>翻译方向 {job.target_lang === "en" ? "中译英" : "英译中"}</p>}
        <p><strong>{label(job.status)}</strong> · 通过 {job.counts.passed} · 可导出 {job.counts.exportable} · 待复核 {job.counts.awaiting_review} · 锁定 {job.items.filter(i => i.status === "locked").length} · 失败 {job.items.filter(i => i.status === "failed").length}</p>
        <p>{job.steps.map(step => `${step.intent} (${label(step.status)})`).join(" → ")}</p>
        {latest && <p className="muted">{latest}</p>}
        {job.status === "awaiting_input" && <>
          <pre>{JSON.stringify(job.missing, null, 2)}</pre>
          <button disabled={busy || !selected.length} onClick={() => void act(async () => {
            await request(`/jobs/${jobId}/continue`, post({
              targets: selected.map(i => i.occurrence_id), target_lang: "zh",
              ...(job.missing.intent ? { intents: ["translate", "export"] } : {}),
            }));
          })}>按所选资源继续</button>
        </>}
        {job.next_actions.includes("cancel") && <button disabled={busy} onClick={() => void act(async () => {
          await request(`/jobs/${jobId}/cancel`, post({}));
        })}>取消任务</button>}
        {job.next_actions.includes("accept_partial") && <button disabled={busy} onClick={() => void act(async () => {
          await request(`/jobs/${jobId}/accept-partial`, post({}));
        })}>接受已完成部分</button>}
      </>}
    </section>
    <section aria-labelledby="review">
      <h2 id="review">复核</h2>
      {job?.items.map(item => {
        const key = `${item.step}:${item.occurrence_id}`;
        const target = drafts[key] ?? item.target ?? "";
        return <article key={key}>
          <p>{label(item.status)} {(item.display_reason ?? item.reason) && "· " + (item.display_reason ?? item.reason)}</p>
          <div className="compare">
            <div><strong>原文</strong><pre>{item.source}</pre></div>
            <label>译文<textarea aria-label={`译文 ${item.occurrence_id}`} value={target}
              readOnly={item.status !== "needs_human"}
              onChange={e => setDrafts(previous => ({ ...previous, [key]: e.target.value }))} /></label>
          </div>
          {job.status === "awaiting_review" && item.status === "needs_human" &&
            <label>修改原因（可选） <input value={reviewReasons[key] || ""} maxLength={1000}
              onChange={e => setReviewReasons(previous => ({ ...previous, [key]: e.target.value }))} /></label>}
          <div className="row">
            <button disabled={busy} onClick={() => void act(async () => {
              const evidence = await json<Evidence>(item.evidence_url);
              setItemEvidence(previous => ({ ...previous, [item.evidence_url]: evidence }));
            })}>查看证据 {item.occurrence_id}</button>
            {job.status === "awaiting_review" && item.status === "needs_human" &&
              <button disabled={!target.trim() || busy} onClick={() => void act(async () => {
                await request(`/jobs/${jobId}/items/${item.step}/${item.occurrence_id}/approve`, post({
                  target, expected_version: item.expected_version, expected_draft_hash: item.draft_hash,
                  ...(reviewReasons[key]?.trim() ? { reason: reviewReasons[key].trim() } : {}),
                }));
              })}>批准译文</button>}
          </div>
          {details(item)}
        </article>;
      })}
    </section>
    <section aria-labelledby="delivery">
      <h2 id="delivery">交付</h2>
      {job?.source_kind === "text" && job.artifacts.filter(a => a.type === "translation_export").map(a =>
        <a key={a.url} href={a.url}>下载译文 JSON</a>)}
      {job?.artifacts.some(item => item.type === "po_preview") &&
        <button disabled={busy} onClick={() => void act(async () => {
          setValidated(false);
          const result = await json<Report>(`/jobs/${jobId}/po/preview`, post({ choices }));
          setReport(result); setValidated(true);
        })}>检查 PO 导出</button>}
      {report && <>
        <p>{report.validation.ok ? "校验通过" : "校验失败"} · {report.validation.level} · 写回 {report.applied} 个键</p>
        {report.validation.diagnostics && <pre>{report.validation.diagnostics}</pre>}
        {report.validation.reason && <p>{report.validation.reason}</p>}
        {report.conflicts.map(entry => <label key={entry.entry_id}>
          同一 PO 键存在译文冲突：{entry.source}
          <select value={choices[entry.entry_id] || ""} onChange={e => {
            setValidated(false);
            const value = e.target.value;
            setChoices(previous => {
              const result = { ...previous };
              if (value) result[entry.entry_id] = value; else delete result[entry.entry_id];
              return result;
            });
          }}><option value="">选择已批准的译文</option>
            {entry.versions.map(version => <option value={version.occurrence_id} key={version.occurrence_id}>
              {version.target} · {version.occurrence_id} v{version.version}</option>)}
          </select>
        </label>)}
        {report.exclusions.length > 0 && <ul>{report.exclusions.map((entry, i) =>
          <li key={i}>{entry.occurrence_id || entry.entry_id} · {entry.reason}</li>)}</ul>}
        <div className="row">
          <button disabled={!validated || !report.download_ready || busy}
            onClick={() => void act(() => download("download", "naev-zh_Hans.po"))}>下载 PO</button>
          <button disabled={!validated || busy}
            onClick={() => void act(() => download("report", "naev-po-changes.json"))}>下载变更报告</button>
        </div>
      </>}
    </section>
    <Glossary selected={selected} openJob={openJob} />
  </main>;
}

createRoot(document.getElementById("root")!).render(<App />);
