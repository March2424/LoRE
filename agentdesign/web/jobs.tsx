import { useEffect, useRef, useState } from "react";
import { ApiError, type Evidence, type FileReviewItem, type FileReviewPage, type FileReviewScope, type Item, type Job, type JobPage, type Language, type Project, type Selection,
  download, intentLabel, json, langLabel, locationText, params, post, reasonText, stored, terminal, useQuery } from "./api";
import { type Locale, pick, useI18n } from "./i18n";
import { Button, Empty, ErrorNote, Field, Grid, Head, Modal, Pager, Section, Status, label, tone, useAction } from "./ui";
import { ContextGraph, evidenceCitation, type SourcePreview } from "./context-graph";

export type Composer = "command" | "selection" | "text";
export function Compose({ mode, scope, project, selection, initialOperation, close, openJob }: {
  mode: Composer; scope: string; project?: Project; selection: Selection; initialOperation?: string;
  close: () => void; openJob: (id: string) => void;
}) {
  const { t } = useI18n();
  const [requirements, setRequirements] = useState("");
  const [operation, setOperation] = useState(initialOperation || "translate,export");
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const action = useAction();
  return <Modal title={{ command: t("Run Command", "下达任务"), selection: t("Run Selection Job", "发起资源任务"), text: t("Quick Translate", "文本快译") }[mode]} close={close}>
    <ErrorNote error={action.error} />
    {mode !== "text" && <p>{t(`${selection.ids.length} selected`, `已选 ${selection.ids.length} 条`)}{selection.file ? ` · ${selection.file}` : ""} · {project?.name ?? t("Naev Demo", "Naev 示范区")}</p>}
    <form className="stack" onSubmit={event => {
      event.preventDefault(); const form = new FormData(event.currentTarget);
      const payload = mode === "text" ? { source_text: form.get("source"), target_lang: form.get("language") } : {
        selection: { project: scope, source_ids: selection.ids, file: selection.file,
          ...(mode === "selection" ? { target_lang: project?.target_lang ?? "zh" } : {}) },
        upload_id: selection.upload,
        ...(mode === "command" ? { text: form.get("command") } : { intents: operation.split(",") }),
        drafts: operation === "review" ? Object.fromEntries(Object.entries(drafts).filter(([, text]) => text.trim())) : undefined,
      };
      void action.run(async () => {
        const job = await json<Job>("/jobs", post({
          ...payload, requirements: requirements.trim() || undefined,
          max_calls: Number(form.get("budget")) || undefined,
        }));
        openJob(job.job_id);
      });
    }}>
      {mode === "command" && <Field label={t("Instruction", "任务指令")}><textarea className="textarea" name="command" autoFocus
        placeholder={t("e.g. Translate selected entries to Simplified Chinese and export", "例如：把所选条目翻译成简体中文并导出")} required maxLength={5000} /></Field>}
      {mode === "selection" && <Field label={t("Action", "动作")}><select className="input" value={operation} onChange={e => setOperation(e.target.value)}>
        <option value="translate,export">{t("Translate & Export", "翻译并导出")}</option><option value="translate">{t("Translate Only", "仅翻译")}</option>
        <option value="review">{t("Review", "审校译文")}</option><option value="explain">{t("Explain Evidence", "解释依据")}</option><option value="export">{t("Export Approved", "导出已批准译文")}</option>
      </select></Field>}
      {mode === "selection" && operation === "review" && <details><summary>{t("Attach draft translations (optional)", "手动附上送审译文")}</summary>
        <div className="stack">{selection.ids.map((id, index) => <Field key={id} label={t(`Item ${index + 1} · ${id.slice(0, 18)}`, `第 ${index + 1} 条 · ${id.slice(0, 18)}`)}>
          <input className="input" maxLength={5000} value={drafts[id] ?? ""} onChange={event => setDrafts(old => ({ ...old, [id]: event.target.value }))} />
        </Field>)}</div>
      </details>}
      {mode === "text" && <>
        <Field label={t("Direction", "翻译方向")}><select className="input" name="language" defaultValue="en">
          <option value="en">{t("ZH → EN", "中译英")}</option><option value="zh">{t("EN → ZH", "英译中")}</option>
        </select></Field>
        <Field label={t("Source Text", "原文")}><textarea name="source" className="textarea" required maxLength={5000} autoFocus /></Field>
      </>}
      {mode !== "command" && <Field label={t("Extra Requirements (Optional)", "补充要求（可选）")}><textarea className="textarea" maxLength={5000}
        value={requirements} onChange={e => setRequirements(e.target.value)} /></Field>}
      <Field label={t("Call Budget (Optional)", "调用上限（可选）")}><input className="input" type="number" name="budget" min="1" max="1000" /></Field>
      <div className="dialog__foot"><Button primary type="submit" disabled={action.busy || (mode === "selection" && !selection.ids.length)}>
        {mode === "text" ? t("Translate", "翻译文本") : t("Create Job", "创建任务")}
      </Button></div>
    </form>
  </Modal>;
}

const kindLabel = (kind: string, locale: Locale) => ({
  project: pick("Project", "项目资源任务", locale),
  graph_build: pick("Graph Build", "知识图谱构建", locale),
  text: pick("Quick Text", "文本快译", locale),
  naev: pick("Naev Demo", "Naev 示范区", locale),
}[kind] ?? kind);
const statusGroupMap: Record<string, Set<string>> = {
  active: new Set(["queued", "running", "awaiting_input", "awaiting_review", "paused"]),
  completed: new Set(["completed", "partial"]),
  failed: new Set(["failed", "cancelled"]),
};

export function JobList({ scope, refresh, openJob, onBack, openChat }: {
  scope?: string; refresh: number; openJob: (id: string) => void; onBack?: () => void; openChat?: () => void;
}) {
  const { locale, t } = useI18n();
  const [scopeTab, setScopeTab] = useState<"project" | "all">("project");
  const [kindFilter, setKindFilter] = useState("all");
  const [intentFilter, setIntentFilter] = useState("all");
  const [statusFilter, setStatusFilter] = useState("all");
  const [offset, setOffset] = useState(0);
  const size = 30;
  const query = useQuery<JobPage>(`/jobs?${params({
    project: scopeTab === "all" ? undefined : scope,
    kind: kindFilter === "all" ? undefined : kindFilter,
    intent: intentFilter === "all" ? undefined : intentFilter,
    status: statusFilter === "all" ? undefined : statusFilter,
    offset, limit: size,
  })}`, refresh);

  const rawJobs = query.data?.jobs ?? [];
  const jobIntents = (j: JobPage["jobs"][number]) =>
    j.source_kind === "graph_build" ? ["build_graph"] : (j.intents.length ? j.intents : j.source_kind === "text" ? ["translate"] : []);

  const visibleJobs = query.data?.summary ? rawJobs : rawJobs.filter(j => {
    if (kindFilter !== "all" && j.source_kind !== kindFilter) return false;
    if (intentFilter !== "all" && !jobIntents(j).includes(intentFilter)) return false;
    if (statusFilter !== "all") {
      const group = statusGroupMap[statusFilter];
      if (group ? !group.has(j.status) : j.status !== statusFilter) return false;
    }
    return true;
  });

  const computedSummary = query.data?.summary ?? (() => {
    const kinds: Record<string, number> = {};
    const intentCounts: Record<string, number> = {};
    const statuses: Record<string, number> = { active: 0, completed: 0, failed: 0 };
    const exact_statuses: Record<string, number> = {};
    for (const j of rawJobs) {
      kinds[j.source_kind] = (kinds[j.source_kind] ?? 0) + 1;
      for (const act of jobIntents(j)) intentCounts[act] = (intentCounts[act] ?? 0) + 1;
      exact_statuses[j.status] = (exact_statuses[j.status] ?? 0) + 1;
      for (const [g, set] of Object.entries(statusGroupMap)) if (set.has(j.status)) statuses[g] += 1;
    }
    return { kinds, intents: intentCounts, statuses, exact_statuses };
  })();

  const totalKinds = Object.values(computedSummary.kinds).reduce((a, b) => a + b, 0);
  const totalDisplay = query.data?.summary ? (query.data.total ?? 0) : visibleJobs.length;
  const hasFilters = kindFilter !== "all" || intentFilter !== "all" || statusFilter !== "all";
  return <>
    <Head title={t("Jobs", "任务中心")} sub={t("Filter and manage jobs by kind, action intent, and execution status", "按任务种类、意图与执行状态分类筛选与管理全部任务")}>
      {onBack && <Button ghost onClick={onBack}>{t("← Back", "← 返回上一页")}</Button>}
      {openChat && <Button primary onClick={openChat}>{t("Go to Agent Chat", "前往 Agent 对话")}</Button>}
    </Head>

    <div className="panel stack">
      <div className="row-set">
        <span className="micro">{t("Scope", "范围")}</span>
        <Button small ghost={scopeTab !== "project"} primary={scopeTab === "project"}
          onClick={() => { setScopeTab("project"); setOffset(0); }}>{scope ? t("Current Project", "当前项目") : t("Default Scope", "默认范围")}</Button>
        <Button small ghost={scopeTab !== "all"} primary={scopeTab === "all"}
          onClick={() => { setScopeTab("all"); setOffset(0); }}>{t("All Workspace Jobs", "全部工作区任务")}</Button>
        {hasFilters && <><span className="sp" /><Button small ghost onClick={() => {
          setKindFilter("all"); setIntentFilter("all"); setStatusFilter("all"); setOffset(0);
        }}>{t("Reset Filters", "重置筛选")}</Button></>}
      </div>

      <div className="row-set">
        <span className="micro">{t("Kind", "任务种类")}</span>
        <Button small ghost={kindFilter !== "all"} primary={kindFilter === "all"}
          onClick={() => { setKindFilter("all"); setOffset(0); }}>{t("All", "全部种类")} ({totalKinds})</Button>
        {(["project", "graph_build", "text", "naev"] as const).map(k => {
          const n = computedSummary.kinds[k] ?? 0;
          if (!n && kindFilter !== k) return null;
          return <Button key={k} small ghost={kindFilter !== k} primary={kindFilter === k}
            onClick={() => { setKindFilter(k); setOffset(0); }}>{kindLabel(k, locale)} ({n})</Button>;
        })}
      </div>

      <div className="row-set">
        <span className="micro">{t("Action", "意图动作")}</span>
        <Button small ghost={intentFilter !== "all"} primary={intentFilter === "all"}
          onClick={() => { setIntentFilter("all"); setOffset(0); }}>{t("All Actions", "全部动作")}</Button>
        {(["translate", "review", "export", "explain", "propose_update", "build_graph"] as const).map(act => {
          const n = computedSummary.intents[act] ?? 0;
          return <Button key={act} small ghost={intentFilter !== act} primary={intentFilter === act}
            onClick={() => { setIntentFilter(act); setOffset(0); }}>{intentLabel(act, locale)} ({n})</Button>;
        })}
      </div>

      <div className="row-set">
        <span className="micro">{t("Status", "执行状态")}</span>
        {([
          ["all", t("All Statuses", "全部状态")],
          ["active", t(`In Progress (${computedSummary.statuses.active ?? 0})`, `进行中 (${computedSummary.statuses.active ?? 0})`)],
          ["awaiting_review", t(`Needs Review (${computedSummary.exact_statuses?.awaiting_review ?? 0})`, `待复核 (${computedSummary.exact_statuses?.awaiting_review ?? 0})`)],
          ["awaiting_input", t(`Needs Input (${computedSummary.exact_statuses?.awaiting_input ?? 0})`, `待补充 (${computedSummary.exact_statuses?.awaiting_input ?? 0})`)],
          ["completed", t(`Done / Partial (${computedSummary.statuses.completed ?? 0})`, `已完成/部分 (${computedSummary.statuses.completed ?? 0})`)],
          ["failed", t(`Failed / Cancelled (${computedSummary.statuses.failed ?? 0})`, `失败/取消 (${computedSummary.statuses.failed ?? 0})`)],
        ] as const).map(([st, title]) => (
          <Button key={st} small ghost={statusFilter !== st} primary={statusFilter === st}
            onClick={() => { setStatusFilter(st); setOffset(0); }}>{title}</Button>
        ))}
      </div>
    </div>

    <ErrorNote error={query.error} />
    {query.data && visibleJobs.length === 0 && <Empty title={hasFilters ? t("No jobs match these filters", "当前分类下暂无任务") : t("No jobs yet", "还没有任务")}>
      {hasFilters ? t("Click 'Reset Filters' above to view other jobs.", "可点击上方「重置筛选」查看其他任务。") : t("Start a translation or graph build from '02 Agent Chat' or '01 Workspace'.", "请在「02 Agent 对话」或「01 工作台」发起翻译或建图任务。")}
    </Empty>}
    {!!visibleJobs.length && <div className="table">
      <Grid columns="100px minmax(110px, 140px) minmax(150px, 1fr) minmax(120px, 175px) minmax(115px, 130px) 92px" header>
        <span>{t("Job ID", "任务编号")}</span><span>{t("Kind", "种类")}</span><span>{t("Summary", "内容")}</span><span>{t("Intent / Action", "意图 / 动作")}</span><span>{t("Status", "状态")}</span><span />
      </Grid>
      {visibleJobs.map(job => <Grid key={job.job_id} columns="100px minmax(110px, 140px) minmax(150px, 1fr) minmax(120px, 175px) minmax(115px, 130px) 92px">
        <span className="mono">J-{job.job_id.slice(0, 6)}</span>
        <span className="pill pill--plain clip">{kindLabel(job.source_kind, locale)}</span>
        <span className="clip" title={job.title}>{job.title || (job.source_kind === "graph_build" ? t("Build Knowledge Graph", "构建知识图谱") :
          job.intents.map(i => intentLabel(i, locale)).join(" → ") || t("Agent Command", "Agent 指令"))}
          {job.bulk_id && <span className="mono hint"> · {t("Batch", "批次")} {job.bulk_id.slice(0, 6)}</span>}</span>
        <span className="hint clip">{jobIntents(job).map(i => intentLabel(i, locale)).join(" · ") || t("Parsing", "待解析")}</span>
        <Status value={job.status} /><Button small ghost onClick={() => openJob(job.job_id)}>{t("Open", "打开任务")}</Button>
      </Grid>)}
    </div>}
    {!!totalDisplay && <Pager total={totalDisplay} offset={offset} size={size} setOffset={setOffset} />}
  </>;
}

export function FileReviewQueue({ file, refresh, back, openItem }: {
  file: FileReviewScope; refresh: number; back: () => void; openItem: (item: FileReviewItem) => void;
}) {
  const { t } = useI18n();
  const [offset, setOffset] = useState(0);
  const size = 30;
  const queue = useQuery<FileReviewPage>(`/projects/${file.projectId}/files/review?${params({ file: file.filename, offset, limit: size })}`, refresh);
  const columns = "minmax(130px, 2fr) minmax(110px, 1fr) 96px 86px";
  useEffect(() => {
    if (queue.data && offset > 0 && offset >= queue.data.total) setOffset(Math.max(0, Math.floor((queue.data.total - 1) / size) * size));
  }, [queue.data, offset]);
  return <>
    <Head title={t("File Review Queue", "文件复核队列")} sub={<>{file.filename}{queue.data && t(` · v${queue.data.project_revision} · ${queue.data.total} awaiting review`, ` · 源 v${queue.data.project_revision} · ${queue.data.total} 条待复核`)}</>}>
      <Button ghost onClick={back}>{t("← Back / Exit Queue", "← 返回上一页 / 退出队列")}</Button>
    </Head>
    <ErrorNote error={queue.error} />
    {!queue.data && !queue.error && <Empty title={t("Loading review queue…", "正在读取文件复核队列…")} />}
    {queue.data?.total === 0 && <Empty title={t("No items awaiting review in this revision", "当前源版本暂无待复核条目")} />}
    {!!queue.data?.items.length && <section className="table" aria-label={t("File review queue items", "文件待复核条目")}>
      <Grid columns={columns} header><span>{t("Source", "原文")}</span><span>{t("Location", "源文件位置")}</span><span>{t("Job", "任务")}</span><span /></Grid>
      {queue.data.items.map(item => <Grid columns={columns} key={item.occurrence_id}>
        <span className="clip" title={item.source}>{item.source}</span>
        <span className="mono clip" title={locationText(item.location)}>{locationText(item.location)}</span>
        <span className="mono">J-{item.job_id.slice(0, 6)}</span>
        <Button small ghost aria-label={t(`Review ${locationText(item.location) || item.source}`, `复核 ${locationText(item.location) || item.source}`)} onClick={() => openItem(item)}>{t("Review", "复核")}</Button>
      </Grid>)}
      <Pager total={queue.data.total} offset={offset} size={size} setOffset={setOffset} />
    </section>}
  </>;
}

export function Clarify({ job, changed }: { job: Job; changed: () => void }) {
  const { locale, t } = useI18n();
  const missing = job.missing;
  const [targets, setTargets] = useState(job.resolved_targets ?? []);
  const [actions, setActions] = useState<string[]>([]);
  const action = useAction(changed);
  const choices = Object.entries(missing).filter(([key]) => !["intent", "term", "source_lang", "target_lang"].includes(key));
  const candidateIds = [...new Set([...choices.flatMap(([, finding]) => finding.choices ?? []), ...targets])];
  const limit = job.project_id ? 64 : 12;
  return <form className="panel" onSubmit={event => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    void action.run(() => json(`/jobs/${job.job_id}/continue`, post({
      intents: missing.intent ? actions : undefined, targets: choices.length ? targets : undefined,
      target_lang: form.get("target_lang") || undefined, source_lang: form.get("source_lang") || undefined,
      ...(missing.term ? { term: { source: form.get("source"), target: form.get("target"),
        previous_target: String(form.get("previous")).trim() || undefined } } : {}),
    })));
  }}>
    <Section>{t("Additional Info Needed", "补充任务信息")}</Section><ErrorNote error={action.error} />
    {missing.intent && <div className="stack"><span className="label">{t("Select actions in execution order", "按执行顺序选择动作")}</span>
      <div className="row-set">{missing.intent.choices?.map(intent => <label key={intent} className="check">
        <input type="checkbox" checked={actions.includes(intent)} onChange={e => setActions(old => e.target.checked ?
          [...old, intent] : old.filter(value => value !== intent))} />{intentLabel(intent, locale)}
      </label>)}</div>{actions.length > 0 && <span className="hint">{actions.map(intent => intentLabel(intent, locale)).join(" → ")}</span>}
    </div>}
    {(["source_lang", "target_lang"] as const).map(key => missing[key] && <Field key={key} label={key === "source_lang" ? t("Source Language", "源语言") : t("Target Language", "目标语言")}>
      <select className="input" name={key} required>{missing[key].choices?.map(value =>
        <option key={value} value={value}>{langLabel(value as Language, locale)}</option>)}</select>
    </Field>)}
    {!!choices.length && <div className="stack">
      {choices.map(([key, finding]) => <div className="note" key={key}>{reasonText(finding.reason, locale)} <code>{finding.reason}</code>
        {key !== "targets" && ` · ${key}`}</div>)}
      <span className="label">{t(`Select targets (${targets.length}/${limit})`, `选择目标（${targets.length}/${limit}）`)}</span>
      <div className="scroll-list">{candidateIds.map(id => {
        const resource = job.clarification_resources?.find(item => item.occurrence_id === id);
        return <label className="check" key={id}><input type="checkbox" checked={targets.includes(id)} disabled={!targets.includes(id) && targets.length >= limit}
          onChange={e => setTargets(old => e.target.checked ? [...old, id] : old.filter(value => value !== id))} />
          <span>{resource?.label ?? id} <small className="mono">{locationText(resource?.location, resource?.file)}</small></span></label>;
      })}</div>
    </div>}
    {missing.term && <div className="form-grid">
      <Field label={t("Source Term", "原文术语")}><input className="input" name="source" required /></Field>
      <Field label={t("New Target", "新译法")}><input className="input" name="target" required /></Field>
      <Field label={t("Previous Target (Optional)", "旧译法（可选）")}><input className="input" name="previous" /></Field>
    </div>}
    <div><Button primary type="submit" disabled={action.busy || (!!missing.intent && !actions.length) || (!!choices.length && !targets.length)}>{t("Continue Job", "继续任务")}</Button></div>
  </form>;
}

export const reviewLabels: Record<string, string> = {
  pass: "通过", repair: "需修复", needs_human: "需人工接手", found: "找到证据", no_match: "未匹配",
  out_of_scope: "范围外", no_new_evidence: "无新证据", evidence_budget_exhausted: "证据预算不足",
};
export const reviewLabel = (key: string, locale: Locale) => ({
  pass: pick("Pass", "通过", locale),
  repair: pick("Needs Repair", "需修复", locale),
  needs_human: pick("Needs Human", "需人工接手", locale),
  found: pick("Found", "找到证据", locale),
  no_match: pick("No Match", "未匹配", locale),
  out_of_scope: pick("Out of Scope", "范围外", locale),
  no_new_evidence: pick("No New Evidence", "无新证据", locale),
  evidence_budget_exhausted: pick("Budget Exhausted", "证据预算不足", locale),
}[key] ?? key);
export const toolLabels: Record<string, string> = {
  search_source_graph: "查源图", get_evidence: "核对证据", search_tm: "查译例", lookup_glossary: "查术语", get_character_note: "查角色笔记",
};
export const toolLabel = (key: string, locale: Locale) => ({
  search_source_graph: pick("Search Graph", "查源图", locale),
  get_evidence: pick("Get Evidence", "核对证据", locale),
  search_tm: pick("Search TM", "查译例", locale),
  lookup_glossary: pick("Lookup Glossary", "查术语", locale),
  get_character_note: pick("Character Note", "查角色笔记", locale),
}[key] ?? key);

function EvidenceDrawer({ evidence, error, openSource }: { evidence?: Evidence; error?: unknown; openSource: (source: SourcePreview) => void }) {
  const { locale, t } = useI18n();
  const [tab, setTab] = useState("actual");
  const actual = evidence?.actual_context?.items ?? [];
  const shadowed = evidence?.source_lookups.reduce((total, entry) => total + (entry.excluded_hits || 0), 0) ?? 0;
  const supplied = new Set(actual.map(item => item.id));
  return <aside className="pane--side" aria-label={t("Evidence drawer", "证据抽屉")}>
    <div className="drawer__head"><b>{t("Evidence", "依据")}</b><div className="segs">
      {Object.entries({ actual: t("Inputs", "实际输入"), lookups: t("Lookups", "主动查证"), reviews: t("Reviews", "审校结论") }).map(([key, title]) =>
        <button type="button" className={`seg${tab === key ? " seg--on" : ""}`} key={key} aria-pressed={tab === key}
          onClick={() => setTab(key)}>{title}{key === "lookups" && evidence?.source_lookups.length ? ` ${evidence.source_lookups.length}` : ""}</button>)}
    </div></div>
    <div className="drawer__body"><ErrorNote error={error} />
      {!evidence && !error && <p className="hint">{t("Loading evidence…", "正在读取条目依据…")}</p>}
      {evidence && tab === "actual" && <>
        {!!evidence.mandatory_terms?.length && <div><span className="micro">{t("Mandatory Terms", "硬性术语")}</span>
          {evidence.mandatory_terms.map((term, i) => <div className="term" key={i}><b>{term.target}</b>← <code>{term.source}</code><em>{t("REQ", "强制")}</em></div>)}</div>}
        <div><div className="row-set"><span className="micro">{t("Candidates vs Selected", "候选与实际输入")}</span><span className="sp" />
          <span className="micro">{evidence.retrieved.length} → {actual.length}</span></div>
          <div className="meter"><i style={{ width: `${Math.min(100, 100 * actual.length / (evidence.retrieved.length || 1))}%` }} /></div></div>
        {actual.map(item => <div className="block text-block" key={item.id}>{item.text}<p className="hint">{item.mandatory ? t("Mandatory · ", "强制 · ") : ""}{item.id}</p></div>)}
        {evidence.actual_sources.map((ref, index) => {
          const { id, location, preview } = evidenceCitation(ref, index);
          return <div className="block text-block" key={id}>{ref.quote}
            <p className="hint">{preview ? <button type="button" className="hint context-graph__location" aria-haspopup="dialog"
              aria-label={t(`Inspect context at ${location}`, `查看 ${location} 的前后文`)} onClick={() => openSource(preview)}>{location}</button> : location}</p>
          </div>;
        })}
        <details><summary>{t("Unused Candidates", "未采用的候选依据")}</summary>{evidence.retrieved.filter(item => !supplied.has(item.id)).map(item =>
          <p className="text-block" key={item.id}>{item.text}</p>)}</details>
      </>}
      {evidence && tab === "lookups" && <>
        {shadowed > 0 && <div className="note note--warn">{t(`Masked ${shadowed} out-of-scope hits.`, `已自动屏蔽 ${shadowed} 条范围外命中。`)}</div>}
        {!evidence.source_lookups.length && <Empty title={t("No active tool lookups recorded", "该条目没有主动查证记录")} />}
        {evidence.source_lookups.map((entry, index) => <div className="block stack" key={index}>
          <h4>{toolLabel(entry.name, locale)} · {reviewLabel(entry.result.status, locale)}</h4>
          <p className="text-block">{entry.effective_query ?? entry.args.query}</p>
          {entry.result.evidence?.map((e, i) => <pre className="text-block" key={i}>{JSON.stringify(e, null, 2)}</pre>)}
        </div>)}
      </>}
      {evidence && tab === "reviews" && <>
        {!evidence.reviews.length && <Empty title={t("No review verdicts yet", "该条目还没有审校回执")} />}
        {evidence.reviews.map((review, index) => <div className="block" key={index}><h4>{index + 1} · {reviewLabel(review.verdict, locale)}</h4><p>{review.reason}</p></div>)}
      </>}
    </div>
  </aside>;
}

export function FeedbackDialog({ job, item, close }: { job: Job; item: Item; close: () => void }) {
  const { locale, t } = useI18n();
  const action = useAction();
  const catOpts = [
    ["correction", t("Translation Fix", "译文纠偏")],
    ["retrieval", t("Retrieval Gap", "检索遗漏")],
    ["prompt", t("Prompt Guidance", "提示指令")],
    ["memory", t("Memory Issue", "记忆问题")],
    ["skill", t("Verification Flow", "查证流程")],
    ["harness", t("Tool Usage", "工具调用")],
  ];
  return <Modal title={t("Record Feedback", "登记反馈")} close={close}><ErrorNote error={action.error} />
    <form className="stack" onSubmit={event => {
      event.preventDefault(); const fields = new FormData(event.currentTarget);
      void action.run(async () => {
        await json("/feedback", post({
          job_id: job.job_id, step: item.step, occurrence_id: item.occurrence_id, category: fields.get("category"),
          reason: fields.get("reason"), after: String(fields.get("after")).trim() || undefined,
          alias: fields.get("query") || fields.get("canonical") ? { query: fields.get("query"), canonical: fields.get("canonical") } : undefined,
        }));
        close();
      });
    }}>
      <Field label={t("Category", "问题类型")}><select name="category" className="input">{catOpts.map(([value, title]) =>
        <option key={value} value={value}>{title}</option>)}</select></Field>
      <Field label={t("Feedback Reason", "反馈原因")}><textarea name="reason" className="textarea" required maxLength={5000} /></Field>
      <Field label={t("Suggested Target (Optional)", "建议译法（可选）")}><textarea name="after" className="textarea" maxLength={5000} /></Field>
      <details><summary>{t("Entity Alias (Optional)", "实体别名（可选）")}</summary><div className="form-grid">
        <Field label={t("Query Term", "查询词")}><input className="input" name="query" maxLength={120} /></Field>
        <Field label={t("Canonical Name", "标准实体名")}><input className="input" name="canonical" maxLength={120} /></Field>
      </div></details>
      <div className="dialog__foot"><Button primary type="submit" disabled={action.busy}>{t("Submit Feedback", "提交反馈")}</Button></div>
    </form>
  </Modal>;
}

type History = { head_version: number; locked: boolean; versions: { version: number; source_version: number; target: string; rollback_available: boolean }[] };
const itemKey = (item: Pick<Item, "step" | "occurrence_id">) => `${item.step}:${item.occurrence_id}`;
const pending = new Set(["needs_human", "locked", "conflict", "failed"]);
export const itemStatus = (item: Item) => item.version_conflict ? "conflict" : item.locked ? "locked" : item.status;
const metadataLabels: Record<string, string> = {
  branch_id: "剧情分支", speaker_id: "说话人", addressee_id: "听话人", phase_id: "阶段", native_context: "上下文",
};
const placeholders = (value: string) => [...value.matchAll(/\$?\{[^{}\n]+\}|%%|%(?:\d+\$)?[-+#0 ]*(?:\d+|\*)?(?:\.\d+)?[sdif]|<[^>\n]+>/g)]
  .filter(match => !(match[0].startsWith("%") && match[0].includes(" ") && /[a-z]/i.test(value[match.index + match[0].length] ?? "")))
  .map(match => match[0]);
const whitespace = (value: string) => [value.match(/^\s*/)?.[0], value.match(/\s*$/)?.[0]].join("\u0000");

function unmatchedPlaceholders(expected: string[], actual: string[]) {
  const remaining = [...actual];
  return expected.filter(token => {
    const match = remaining.indexOf(token);
    if (match === -1) return true;
    remaining.splice(match, 1);
    return false;
  });
}

export function TranslationChecks({ source, target, terms }: { source: string; target: string; terms?: Evidence["mandatory_terms"] }) {
  const { t } = useI18n();
  const originalTokens = placeholders(source);
  const translatedTokens = placeholders(target);
  const missing = unmatchedPlaceholders(originalTokens, translatedTokens);
  const extra = unmatchedPlaceholders(translatedTokens, originalTokens);
  const placeholderWarnings = [missing.length && t(`Missing placeholders: ${missing.join(", ")}`, `缺少占位符 ${missing.join("、")}`), extra.length && t(`Extra placeholders: ${extra.join(", ")}`, `多出占位符 ${extra.join("、")}`)];
  const missingTerms = terms?.filter(term => !target.includes(term.target)).map(term => term.target) ?? [];
  const checks = [
    [t("Placeholders", "占位符一致"), placeholderWarnings.filter(Boolean).join(" · ")],
    [t("Line breaks", "换行一致"), source.split("\n").length === target.split("\n").length ? "" : t("Line breaks differ from source", "换行与原文不同")],
    [t("Edge spaces", "首尾空格一致"), whitespace(source) === whitespace(target) ? "" : t("Leading/trailing spaces differ", "首尾空格与原文不同")],
    ...(terms?.length ? [[t("Mandatory terms", "强制术语齐全"), missingTerms.length ? t(`Missing terms: ${missingTerms.join(", ")}`, `缺少强制术语 ${missingTerms.join("、")}`) : ""]] : []),
  ];
  return <div className="check-strip" role="group" aria-label={t("Translation checks", "译文检查")}>{checks.map(([title, warning]) =>
    <span className={`chk${warning ? " chk--warn" : ""}`} key={title}><span>{warning || title}</span></span>)}</div>;
}

function Review({ job, refresh, refreshError, changed, openJob, retry, fileQueue }: {
  job: Job; refresh: number; refreshError?: unknown; changed: () => void; openJob: (id: string) => void; retry: (id: string) => void;
  fileQueue?: { items: FileReviewItem[]; selected?: string; back: () => void };
}) {
  const { locale, t } = useI18n();
  const [filter, setFilter] = useState("all");
  const [selected, setSelected] = useState(fileQueue?.selected ?? "");
  const [drafts, setDrafts] = useState<Record<string, { hash: string | null; text: string }>>(() => stored(`lore-draft-${job.job_id}`, {}));
  const [reasons, setReasons] = useState<Record<string, string>>({});
  const [dialog, setDialog] = useState<"history" | "feedback" | "context">();
  const [contextSource, setContextSource] = useState<SourcePreview>();
  const [contextApproval, setContextApproval] = useState<{ approved: string } | "complete">();
  const [saved, setSaved] = useState("");
  const action = useAction(changed);
  const fileVersions = fileQueue && new Map(fileQueue.items.map(item => [itemKey(item), item.source_version]));
  const reviewItems = fileVersions ? job.items.filter(item => fileVersions.get(itemKey(item)) === item.source_version &&
    item.next_actions.includes("review_item")) : job.items;
  const rows = reviewItems.filter(item => filter === "all" || (filter === "pending" && pending.has(itemStatus(item))) || filter === itemStatus(item));
  const updatingContext = dialog === "context" && contextApproval !== "complete" ? contextApproval : undefined;
  const approvedItem = updatingContext && job.items.find(row => itemKey(row) === updatingContext.approved);
  const item = approvedItem ??
    rows.find(row => itemKey(row) === selected) ?? rows.find(row => pending.has(itemStatus(row))) ?? rows[0];
  const key = item ? itemKey(item) : "";
  const edit = !!item?.next_actions.includes("review_item");
  const draft = drafts[key];
  const target = edit && draft && draft.hash === item?.draft_hash ? draft.text : item?.target ?? "";
  const historyUrl = item ? `/jobs/${job.job_id}/versions/${encodeURIComponent(item.occurrence_id)}` : "";
  const evidence = useQuery<Evidence>(item?.evidence_url ?? null, refresh);
  const history = useQuery<History>(dialog === "history" ? historyUrl : null, refresh);
  const changeDraft = (text: string) => {
    if (!item || !edit) return;
    setDrafts(old => ({ ...old, [key]: { text, hash: item.draft_hash } }));
    setSaved("");
  };
  const saveDraft = () => {
    if (!item || !edit) return;
    const storageKey = `lore-draft-${job.job_id}`;
    localStorage.setItem(storageKey, JSON.stringify({ ...stored(storageKey, {}), [key]: { hash: item.draft_hash, text: target } }));
    setSaved(key);
  };
  const approve = (advance = false) => {
    if (!item || !edit || !target.trim()) return;
    void action.run(async () => {
      await json(item.evidence_url.replace(/\/evidence$/, "/approve"), post({
        target, expected_version: item.expected_version, expected_draft_hash: item.draft_hash,
        reason: reasons[key]?.trim() || undefined,
      }));
      const storageKey = `lore-draft-${job.job_id}`;
      const remaining = stored<Record<string, unknown>>(storageKey, {});
      delete remaining[key];
      try { localStorage.setItem(storageKey, JSON.stringify(remaining)); }
      catch { /* A stale local draft is ignored once the server accepts the translation. */ }
      if (advance) setContextApproval({ approved: key });
      else setSelected("");
      setSaved("");
    });
  };
  useEffect(() => {
    if (!updatingContext) return;
    const index = job.items.findIndex(row => itemKey(row) === updatingContext.approved);
    // Select the next item after the job refresh confirms the approved item has left the queue.
    if (job.items[index]?.next_actions.includes("review_item")) return;
    const after = index + 1;
    const next = [...job.items.slice(after), ...job.items.slice(0, after)]
      .find(row => reviewItems.includes(row) && row.next_actions.includes("review_item"));
    setSelected(next ? itemKey(next) : updatingContext.approved);
    setContextSource(undefined);
    setContextApproval(next ? undefined : "complete");
  }, [job.items, reviewItems, updatingContext]);
  const shortcutHandler = useRef<(event: KeyboardEvent) => void>(undefined);
  shortcutHandler.current = (event: KeyboardEvent) => {
    if (dialog || document.querySelector("dialog[open]")) return;
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter" && edit) {
      event.preventDefault(); approve(); return;
    }
    if (!item || (event.target instanceof HTMLElement && event.target.closest("input,textarea,select,button"))) return;
    if (event.key === "j" || event.key === "k") {
      const next = rows[rows.indexOf(item) + (event.key === "j" ? 1 : -1)];
      if (next) setSelected(itemKey(next));
    }
  };
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => shortcutHandler.current?.(event);
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);
  const checks = item && target ? <TranslationChecks source={item.source} target={target} terms={evidence.data?.mandatory_terms} /> : null;
  const hasNextReview = reviewItems.some(row => itemKey(row) !== key && row.next_actions.includes("review_item"));
  const toggleContext = (open: boolean, source?: SourcePreview) => {
    setContextApproval(undefined);
    setContextSource(source);
    action.clear();
    setDialog(open ? "context" : undefined);
  };
  const explain = () => item && action.run(async () => {
    const result = await json<Job>("/jobs", post({
      intents: ["explain"], targets: [item.occurrence_id], target_lang: job.target_lang ?? undefined,
      selection: { project: job.project_id ?? "naev" }, upload_id: job.upload_id ?? undefined,
    }));
    openJob(result.job_id);
  });
  return <>
    {!dialog && <ErrorNote error={action.error} />}
    <div className="panes">
      <section className="pane--list" aria-label={t("Review queue", "复核队列")}>
        <div className="queue__head">{fileQueue ? <span className="seg seg--on">{t(`This job · ${reviewItems.length} awaiting review`, `本任务 · ${reviewItems.length} 条待复核`)}</span> :
          Object.entries({ all: t("All", "全部"), pending: t("Action Needed", "待处理"), conflict: t("Conflict", "冲突") }).map(([value, title]) =>
          <button type="button" className={`seg${filter === value ? " seg--on" : ""}`} key={value} aria-pressed={filter === value}
            onClick={() => { setFilter(value); setSelected(""); }}>{title} {value === "all" ? job.items.length :
              job.items.filter(row => value === "pending" ? pending.has(itemStatus(row)) : itemStatus(row) === value).length}</button>)}</div>
        <div className="queue">{rows.map(row => <button key={itemKey(row)} type="button"
          className={`queue__item${itemKey(row) === key ? " queue__item--active" : ""}`} aria-pressed={itemKey(row) === key}
          onClick={() => { setSelected(itemKey(row)); action.clear(); }}>
          <span className="queue__idx">{row.location?.line ?? row.location?.row ?? job.items.indexOf(row) + 1}</span>
          <span className={`dot ${tone(itemStatus(row)) ? "dot--" + tone(itemStatus(row)) : ""}`} />
          <span className="queue__src">{row.source}</span><span className="queue__meta"><span className="clip">{row.file || intentLabel(job.steps[row.step]?.intent ?? "", locale)}</span>
            <span>{label(itemStatus(row), locale)} · v{row.version ?? row.expected_version}</span></span>
        </button>)}</div>
      </section>
      <section className="pane--main" aria-label={t("Source and translation", "原文与译文")}>
        {!item ? <Empty title={fileQueue ? t("No reviewable items for this file in job", "此任务中该文件暂无待复核条目") : t("No entries under this filter", "此筛选条件下没有条目")} /> : <>
          <div className="field"><div className="row-set"><span className="label">{t("Source", "原文")}</span><span className="sp" />
              <span className="mono hint">{locationText(item.location, item.file)} · v{item.source_version}</span>
              <Button small ghost onClick={() => toggleContext(true)}>{t("Inspect Context", "查看上下文")}</Button></div>
            <div className="source text-block">{item.source}</div>
          </div>
          <div className="field"><div className="row-set"><label className="label" htmlFor="review-target">{t("Translation", "译文")}</label><span className="sp" />
            <span className="mono hint">{job.target_lang} · v{item.version ?? item.expected_version} · {label(itemStatus(item), locale)}</span></div>
            <textarea id="review-target" className="target" value={target} readOnly={!edit} maxLength={5000}
              onChange={event => changeDraft(event.target.value)} />
            {checks}
            {item.reason && <div className="note note--warn">{item.display_reason && item.display_reason !== item.reason ?
              item.display_reason : reasonText(item.reason, locale)} <code>{item.reason}</code></div>}
          </div>
          {edit && <Field label={t("Edit Reason (optional, saved to feedback)", "修改原因（可选，进入反馈库）")}>
            <input className="input" maxLength={1000} value={reasons[key] || ""} onChange={e => setReasons(old => ({ ...old, [key]: e.target.value }))} />
          </Field>}
          {!!Object.entries(item.metadata ?? {}).length && <div className="ctxgrid">{Object.entries(item.metadata).map(([name, value]) =>
            <div className="ctxcell" key={name}><span>{metadataLabels[name] ? (locale === "zh" ? metadataLabels[name] : ({ branch_id: "Branch", speaker_id: "Speaker", addressee_id: "Addressee", phase_id: "Phase", native_context: "Context" }[name] ?? name)) : name}</span><b>{value}</b></div>)}</div>}
          <div className="row-set review-actions">
            {(item.next_actions.includes("lock") || item.next_actions.includes("unlock")) &&
              <Button small ghost disabled={action.busy} onClick={() => void action.run(() =>
                json(`${historyUrl}/${item.locked ? "unlock" : "lock"}`, post({ expected_version: item.head_version })))}>
                {item.locked ? t("Unlock", "解锁") : t("Lock", "锁定")}</Button>}
            {item.head_version > 0 && <Button small ghost onClick={() => { action.clear(); setDialog("history"); }}>{t("History", "版本历史")}</Button>}
            {item.head_version > 0 && job.source_kind !== "text" && <Button small ghost disabled={action.busy} onClick={explain}>{t("Explain Version", "解释当前版本")}</Button>}
            {item.next_actions.includes("create_new_job") && <Button small ghost disabled={action.busy} onClick={() => void action.run(async () => {
              const result = await json<Job>("/jobs", post({
                selection: { project: job.project_id ?? "naev" }, targets: [item.occurrence_id],
                upload_id: job.upload_id ?? undefined, target_lang: job.target_lang ?? undefined, intents: ["translate", "export"],
              }));
              openJob(result.job_id);
            })}>{t("New Job on Latest", "用当前版本新建任务")}</Button>}
            {!["queued", "running"].includes(item.status) && <Button small ghost onClick={() => setDialog("feedback")}>{t("Feedback", "登记反馈")}</Button>}
            {item.next_actions.includes("retry") && <Button small ghost onClick={() => retry(item.occurrence_id)}>{t("Retry Item", "重试该条")}</Button>}
            <span className="sp" />
            {edit && <><Button small ghost onClick={saveDraft}>{saved === key ? t("Saved locally", "已保存到本地") : t("Save Draft", "保存草稿")}</Button>
              <Button small primary disabled={!target.trim() || action.busy} onClick={() => approve()}>{t("Approve", "批准译文")} <span className="kbd">Ctrl↵</span></Button></>}
          </div>
        </>}
      </section>
      {item && <EvidenceDrawer evidence={evidence.data} error={evidence.error} openSource={source => toggleContext(true, source)} />}
    </div>
    {item && dialog === "history" && <Modal title={t("Version History", "译文版本历史")} close={() => { setDialog(undefined); action.clear(); }}>
      <ErrorNote error={history.error || action.error} />
      {history.data?.versions.map(version => <div className="block stack" key={version.version}>
        <div className="row-set"><b>v{version.version} · {t("Source", "源")} v{version.source_version}</b>
          {version.version === history.data?.head_version && <Status value="active" />}
          {version.rollback_available && item.next_actions.includes("rollback") && <Button small ghost disabled={action.busy}
            onClick={() => void action.run(() => json(historyUrl + "/rollback", post({
              expected_version: history.data?.head_version, to_version: version.version,
            })))}>{t("Rollback to Version", "回滚至此版本")}</Button>}
        </div><div className="target text-block">{version.target}</div>
      </div>)}
    </Modal>}
    {item && dialog === "feedback" && <FeedbackDialog job={job} item={item} close={() => setDialog(undefined)} />}
    {dialog === "context" && <Modal title={t("Entry Context", "条目上下文")} close={() => toggleContext(false)}
      className={contextApproval === "complete" ? undefined : "context-graph-dialog"}>
      {contextApproval === "complete" ? <>
        <div className="note note--ok" role="status">{fileQueue ? t("All reviewable entries for this file in the job are done.", "此任务中该文件的可复核条目已处理完。") : t("All reviewable entries are complete.", "当前可复核条目已处理完。")}</div>
        {fileQueue && <div className="dialog__foot"><Button onClick={fileQueue.back}>{t("Back to Review Queue", "返回文件复核队列")}</Button></div>}
      </> : <>
        <ErrorNote error={action.error || (updatingContext && refreshError) || evidence.error} />
        {updatingContext && <div className="row-set" role="status">
          {refreshError ? <>{t("Translation approved.", "译文已批准。")} <Button small onClick={changed}>{t("Reload queue", "重新读取复核队列")}</Button></> : t("Translation approved, advancing queue…", "译文已批准，正在更新复核队列…")}
        </div>}
        {item && <ContextGraph key={key} item={item} items={job.items} translation={target} evidence={evidence.data}
          loadingEvidence={!evidence.data && !evidence.error} initialSource={contextSource}
          editor={edit ? { change: changeDraft, save: saveDraft, saved: saved === key, checks, approve: () => approve(true),
            busy: action.busy || !!updatingContext, hasNext: hasNextReview } : undefined} />}
      </>}
    </Modal>}
  </>;
}

type PoReport = {
  validation: { ok: boolean; level: string; diagnostics?: string; reason?: string }; applied: number;
  download_ready: boolean; conflicts: { entry_id: string; source: string; versions: { occurrence_id: string; target: string; version: number }[] }[];
  exclusions: { occurrence_id?: string; entry_id?: string; reason: string }[];
};
export function PoDelivery({ job, close }: { job: Job; close: () => void }) {
  const { locale, t } = useI18n();
  const [choices, setChoices] = useState<Record<string, string>>({});
  const [report, setReport] = useState<PoReport>();
  const [checked, setChecked] = useState(false);
  const action = useAction();
  const path = `/jobs/${job.job_id}/po`;
  return <Modal title={t("PO Export Validation", "PO 导出预检")} close={close}>
    <ErrorNote error={action.error} />
    {report && <>
      <div className="row-set"><Status value={report.validation.ok ? "passed" : "failed"} />
        <span>{t(`Write back ${report.applied} keys · ${report.validation.level}`, `写回 ${report.applied} 个键 · ${report.validation.level}`)}</span></div>
      <ErrorNote reason={report.validation.reason} />
      {report.validation.diagnostics && <pre className="text-block">{report.validation.diagnostics}</pre>}
      {report.conflicts.map(entry => <Field key={entry.entry_id} label={t(`Shared PO key · ${entry.source}`, `共享 PO 键 · ${entry.source}`)}>
        <select className="input" value={choices[entry.entry_id] ?? ""} onChange={event => {
          const value = event.target.value;
          setChecked(false); setChoices(previous => {
            const updated = { ...previous };
            if (value) updated[entry.entry_id] = value; else delete updated[entry.entry_id];
            return updated;
          });
        }}><option value="">{t("Choose approved translation", "选择已批准译文")}</option>
          {entry.versions.map(version => <option key={version.occurrence_id} value={version.occurrence_id}>{version.target} · v{version.version}</option>)}
        </select>
      </Field>)}
      {!!report.exclusions.length && <details><summary>{t(`Excluded (${report.exclusions.length})`, `排除项（${report.exclusions.length}）`)}</summary>
        <ul className="scroll-list">{report.exclusions.map((row, i) =>
          <li key={i} className="text-block">{row.occurrence_id ?? row.entry_id} · {reasonText(row.reason, locale)} <code>{row.reason}</code></li>)}</ul>
      </details>}
    </>}
    <div className="dialog__foot">
      <Button primary={!checked} disabled={action.busy} onClick={() => void action.run(async () => {
        setReport(await json<PoReport>(path + "/preview", post({ choices }))); setChecked(true);
      })}>{report ? t("Re-check", "重新检查") : t("Validate Export", "检查导出")}</Button>
      <Button disabled={action.busy || !checked} onClick={() => void action.run(() =>
        download(path + "/report", "naev-po-changes.json", post({ choices })))}>{t("Download Report", "下载报告")}</Button>
      <Button primary={checked} disabled={action.busy || !checked || !report?.download_ready} onClick={() => void action.run(() =>
        download(path + "/download", "naev-zh_Hans.po", post({ choices })))}>{t("Download PO", "下载 PO")}</Button>
    </div>
  </Modal>;
}

export function JobDetail({ id, refresh, changed, openJob, back, deliver, fileReview, reviewSelection, onExit, openChat }: {
  id: string; refresh: number; changed: () => void; openJob: (id: string) => void; back: () => void; deliver: (projectId: string) => void;
  fileReview?: FileReviewScope; reviewSelection?: string; onExit?: () => void; openChat?: () => void;
}) {
  const { locale, t } = useI18n();
  const status = useQuery<Job>(`/jobs/${id}`, refresh);
  const fileQueue = useQuery<FileReviewPage>(fileReview ? `/projects/${fileReview.projectId}/files/review?${params({
    file: fileReview.filename, job_id: id, limit: 64,
  })}` : null, refresh);
  const job = status.data;
  const action = useAction(changed);
  const [dialog, setDialog] = useState<"resume" | "retry" | "cancel" | "partial" | "po">();
  const [retryItem, setRetryItem] = useState<string>();
  const [unknown, setUnknown] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const can = (value: string) => job?.next_actions.includes(value);
  const change = (path: string, payload: unknown = {}) => json(`/jobs/${id}/${path}`, post(payload));
  const showRetry = (item?: string) => { action.clear(); setRetryItem(item); setUnknown(false); setConfirmed(false); setDialog("retry"); };
  const close = () => { setDialog(undefined); action.clear(); };
  if (fileReview) return <>
    <Head title={t("File Review", "文件复核")} sub={<>{fileReview.filename} · <span className="mono">J-{id.slice(0, 6)}</span></>}>
      <Button small ghost onClick={() => openJob(id)}>{t("Full Job", "查看完整任务")}</Button>
      <Button small onClick={back}>{t("← Back to File Queue", "← 返回文件复核队列")}</Button>
    </Head>
    <ErrorNote error={status.error || fileQueue.error} />
    {job && fileQueue.data ? <Review job={job} refresh={refresh} refreshError={status.error || fileQueue.error}
      changed={changed} openJob={openJob} retry={showRetry} fileQueue={{ items: fileQueue.data.items, selected: reviewSelection, back }} /> :
      !status.error && !fileQueue.error && <Empty title={t("Loading review entries…", "正在读取复核条目…")} />}
  </>;
  if (!job) return <><Head title={t("Job Details", "任务详情")}>
    {onExit && <Button ghost onClick={onExit}>{t("← Back", "← 返回上一页")}</Button>}
    <Button ghost onClick={back}>{t("All Jobs", "全部任务")}</Button>
  </Head>
    <ErrorNote error={status.error} />{!status.error && <Empty title={t("Loading job…", "正在读取任务…")} />}</>;
  const failedRows = job.items.filter(item => item.next_actions.includes("retry"));
  const jobRetry = can("retry") && (job.source_kind === "graph_build" || !job.steps.length || !!failedRows.length);
  const graphResult = job.results.find(result => result.intent === "build_graph");
  const output = job.artifacts.find(item => item.type === "translation_export");
  const reviewRequired = can("continue") || job.items.some(item => item.next_actions.includes("review_item"));
  return <>
    <Head title={job.graph_build ? t("Graph Build Job", "建图任务") : job.items.length ? t("Job Review & Details", "任务审校与详情") : t("Job Details", "任务详情")} sub={<>
      <span className="mono">J-{id.slice(0, 6)}</span> · <Status value={job.status} pill />
      {job.source_lang && job.target_lang && ` · ${langLabel(job.source_lang, locale)} → ${langLabel(job.target_lang, locale)}`}
      {" · "}<span className="mono">{job.budget.reserved_or_spent} / {job.budget.limit_calls}</span> {t("calls", "次调用")}
      {job.bulk_id && ` · ${t("Batch", "批次")} ${job.bulk_id.slice(0, 6)}`}
    </>}>
      {onExit && <Button small ghost onClick={onExit}>{t("← Back", "← 返回上一页")}</Button>}
      <Button small ghost onClick={back}>{t("All Jobs", "全部任务")}</Button>
      {openChat && <Button small ghost onClick={openChat}>{t("Agent Chat", "回到 Agent 对话")}</Button>}
      {can("pause") && <Button small ghost disabled={action.busy} onClick={() => void action.run(() => change("pause"))}>{t("Pause", "暂停")}</Button>}
      {can("resume") && <Button small onClick={() => setDialog("resume")}>{t("Resume", "继续")}</Button>}
      {jobRetry && <Button small onClick={() => showRetry(failedRows[0]?.occurrence_id)}>{t("Retry", "重试")}</Button>}
      {can("accept_partial") && <Button small ghost onClick={() => setDialog("partial")}>{t("Accept Partial", "接受已完成")}</Button>}
      {can("cancel") && <Button small ghost onClick={() => setDialog("cancel")}>{t("Cancel", "取消任务")}</Button>}
    </Head>
    <ErrorNote error={status.error || (!dialog && action.error)} />
    {job.pause_requested && <div className="note note--warn">{t("Pausing after current item or batch completes.", "当前条目或批次结束后暂停。")}</div>}
    {job.blocked_by && <div className="note note--warn"><div className="row-set">{t("Waiting for previous batch to finish.", "此批等待前一批结束。")}
      <Button small ghost onClick={() => openJob(job.blocked_by!)}>{t("Open Previous Batch", "查看前一批任务")}</Button>
    </div></div>}
    {job.reason && !can("continue") && <div className="note">{reasonText(job.reason, locale)} <code>{job.reason}</code></div>}
    {!!job.steps.length && <div className="row-set job-steps">{job.steps.map((step, i) => <span className="row-set" key={step.index}>
      {i > 0 && <span aria-hidden="true">→</span>}<span>{intentLabel(step.intent, locale)}</span><Status value={step.status} />
    </span>)}</div>}
    {can("continue") && <Clarify key={JSON.stringify(job.missing)} job={job} changed={changed} />}
    {job.graph_build && <>
      <Section>{t("Graph Build Progress", "建图进度")}</Section>
      <div className="row-set"><span className="mono">{job.graph_build.completed.length} / {job.graph_build.batches} {t("batches", "批")}</span>
        {graphResult && <span>{t(`${String(graphResult.nodes)} nodes · ${String(graphResult.edges)} relations`, `${String(graphResult.nodes)} 个节点 · ${String(graphResult.edges)} 条关系`)}</span>}</div>
      <div className="meter"><i style={{ width: `${terminal.has(job.status) && job.status === "completed" ? 100 :
        100 * job.graph_build.completed.length / (job.graph_build.batches || 1)}%` }} /></div>
      {job.graph_build.completed.map((batch, i) => <details key={i}>
        <summary>{t(`Batch ${i + 1} · ${batch.entities} entities · ${batch.relations} relations · ${batch.rejected.length} skipped`, `第 ${i + 1} 批 · ${batch.entities} 实体 · ${batch.relations} 关系 · ${batch.rejected.length} 项未收录`)}</summary>
        {!!batch.rejected.length && <pre className="text-block">{JSON.stringify(batch.rejected, null, 2)}</pre>}
      </details>)}
      {graphResult && <p className="mono hint">{String(graphResult.graph_snapshot)}</p>}
    </>}
    {!!job.items.length && <>
      <div className="stats">
        {[[job.counts.passed, t("Passed", "已通过"), t("Translated & reviewed", "翻译与审校")], [job.counts.awaiting_review, t("Pending", "待处理"), t("Needs confirmation", "需要编辑确认")],
          [job.items.filter(item => itemStatus(item) === "conflict").length, t("Conflicts", "版本冲突"), t("Check current translation", "检查当前译文")],
          [job.items.filter(item => item.locked).length, t("Locked", "已锁定"), t("Pinned version", "已保留译文版本")]].map(([count, title, description]) =>
          <div className="stat" key={String(title)}><b>{count}</b><span>{title}</span><em>{description}</em></div>)}
      </div>
      <Review job={job} refresh={refresh} refreshError={status.error} changed={changed} openJob={openJob} retry={showRetry} />
    </>}
    {!job.items.length && !job.graph_build && !reviewRequired && !terminal.has(job.status) && !job.blocked_by &&
      <Empty title={job.status === "paused" ? t("Job paused", "任务等待继续") : t("Preparing job", "任务正在准备")}>{t("Results will update here shortly.", "执行结果会在此更新。")}</Empty>}
    {job.results.filter(result => ["explain", "propose_update"].includes(result.intent)).map((result, i) => <details open key={i}>
      <summary>{intentLabel(result.intent, locale)} {t("Result", "结果")}</summary>
      <pre className="text-block">{JSON.stringify(result, null, 2)}</pre>
    </details>)}
    {(output || job.artifacts.some(artifact => artifact.type === "po_preview") || terminal.has(job.status)) && <>
      <Section>{t("Delivery", "交付")}</Section>
      <div className="row-set">
        {job.artifacts.some(artifact => artifact.type === "po_preview") && <Button onClick={() => setDialog("po")}>{t("PO Export Check", "PO 导出预检")}</Button>}
        {job.project_id && job.source_kind === "project" && <Button onClick={() => deliver(job.project_id!)}>{t("Go to File Export", "前往文件交付")}</Button>}
        {output && <a className="btn" href={output.url}>{t(`Download JSON (${job.counts.exportable})`, `下载批准译文 JSON · ${job.counts.exportable} 条`)}</a>}
        {job.artifacts.filter(artifact => artifact.type === "translation_report").map(artifact =>
          <a className="btn btn--ghost" key={artifact.url} href={artifact.url}>{t("Download Report", "下载任务报告")}</a>)}
      </div>
    </>}
    {dialog === "po" && <PoDelivery job={job} close={close} />}
    {dialog === "resume" && <Modal title={t("Resume Job", "继续任务")} close={close}>
      <ErrorNote error={action.error} />
      <form className="stack" onSubmit={event => {
        event.preventDefault();
        const requirements = String(new FormData(event.currentTarget).get("requirements") || "").trim();
        void action.run(async () => { await change("resume", { requirements: requirements || undefined }); close(); });
      }}>
        {!job.graph_build && <Field label={t("Extra Requirements for Remaining Items (Optional)", "后续条目的补充要求（可选）")}><textarea name="requirements" className="textarea" maxLength={5000} /></Field>}
        <div className="dialog__foot"><Button primary type="submit" disabled={action.busy}>{t("Resume", "继续执行")}</Button></div>
      </form>
    </Modal>}
    {dialog === "retry" && <Modal title={t("Retry Job", "重试任务")} close={close}><ErrorNote error={action.error} />
      {!!failedRows.length && <Field label={t("Item to Retry", "重试条目")}><select className="input" value={retryItem ?? ""} onChange={e => {
        setRetryItem(e.target.value); setUnknown(false); setConfirmed(false); action.clear();
      }}>{failedRows.map(item => <option key={item.occurrence_id} value={item.occurrence_id}>{item.source.slice(0, 100)}</option>)}</select></Field>}
      {unknown && <label className="check"><input type="checkbox" checked={confirmed} onChange={e => setConfirmed(e.target.checked)} />{t("Confirm allowing a potentially duplicate model call", "确认可能重复发出模型调用")}</label>}
      <div className="dialog__foot"><Button primary disabled={action.busy || (unknown && !confirmed)} onClick={() => void action.run(async () => {
        try { await change("retry", { occurrence_id: retryItem, confirm_unknown_call: confirmed }); close(); }
        catch (error) { if (error instanceof ApiError && error.reason === "unknown_provider_result_requires_confirmation") setUnknown(true); throw error; }
      })}>{t("Retry", "重试")}</Button></div>
    </Modal>}
    {(dialog === "cancel" || dialog === "partial") && <Modal title={dialog === "cancel" ? t("Cancel Job", "取消任务") : t("Accept Completed Items", "接受已完成部分")} close={close}>
      <ErrorNote error={action.error} />
      <p>{dialog === "cancel" ? t("Remaining entries will not be processed after cancelling.", "取消后，本任务不会继续处理剩余条目。") : t("Items awaiting review, failed, or conflicted will be excluded; passed items proceed to delivery.", "待复核、失败和冲突条目会被排除，已通过条目继续执行交付步骤。")}</p>
      <div className="dialog__foot"><Button ghost onClick={close}>{t("Back", "返回")}</Button>
        <Button primary disabled={action.busy} onClick={() => void action.run(async () => {
          await change(dialog === "cancel" ? "cancel" : "accept-partial"); close();
        })}>{t("Confirm", "确认")}</Button></div>
    </Modal>}
  </>;
}
