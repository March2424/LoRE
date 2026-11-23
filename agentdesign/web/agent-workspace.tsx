import { useEffect, useMemo, useRef, useState } from "react";
import {
  type AskResponse, type FileReviewScope, type InspectData, type Job, type JobPage, type Project,
  type Resource, type ResourceCounts, type Selection, type TranslationStatus, type Workspace,
  intents, json, language, locationText, params, post, reasonText, stored, terminal, useQuery,
} from "./api";
import { Button, Empty, ErrorNote, Field, Modal, Pager, Section, Status, label, tone, useAction } from "./ui";
import { type FileProgress, type Import, ImportDialog, type Upload } from "./resources";
import { Clarify, FeedbackDialog, PoDelivery, TranslationChecks, itemStatus, reviewLabels, toolLabels } from "./jobs";

type ChatEntry =
  | { id: string; role: "user"; text: string; meta?: string }
  | { id: string; role: "ask"; occurrenceId: string; source: string; data: AskResponse }
  | { id: string; role: "job"; jobId: string }
  | { id: string; role: "error"; text: string };

const MEMORY_KIND_LABELS: Record<string, string> = {
  term: "术语", style: "角色笔记", translation: "译例", summary: "摘要", source: "原文片段",
};
const META_LABELS: Record<string, string> = {
  text_type: "文本类型", native_context: "上下文", document: "文档", scene: "场景", block: "对话块",
  branch_id: "剧情分支", phase_id: "阶段", speaker_id: "说话人", addressee_id: "听话人", topic_id: "话题", requirements: "翻译要求",
};

function looksLikeQuestion(text: string, hasFocus: boolean, multiSelected: boolean) {
  if (!hasFocus || multiSelected) return false;
  const trimmed = text.trim();
  if (/^(把|请把|将|帮我|批量)?.*(翻译|重译|导出|审校|检查并导出|改成|更新术语)/.test(trimmed)) return false;
  return /[?？]|为什么|为何|怎么|依据|记忆|图谱|候选|查证|review|审校|理由|解释|哪条|哪些/i.test(trimmed);
}

function JobBubbleCard({
  jobId, refresh, changed, onInspectItem, onSyncJob, onOpenFullJob,
}: {
  jobId: string; refresh: number; changed: () => void;
  onInspectItem: (occId: string, jobId: string) => void;
  onSyncJob: (jobId: string) => void;
  onOpenFullJob: (jobId: string) => void;
}) {
  const query = useQuery<Job>(`/jobs/${jobId}`, refresh);
  const job = query.data;
  const action = useAction(changed);
  const [dialog, setDialog] = useState<"resume" | "retry" | "po">();
  const [unknown, setUnknown] = useState(false);
  const [confirmed, setConfirmed] = useState(false);

  if (query.error) return <div className="chat-card"><ErrorNote error={query.error} /></div>;
  if (!job) return <div className="chat-card"><span className="hint">正在读取任务 J-{jobId.slice(0, 6)}…</span></div>;

  const can = (act: string) => job.next_actions.includes(act);
  const change = (path: string, payload: unknown = {}) => json(`/jobs/${jobId}/${path}`, post(payload));
  const failedRows = job.items.filter(item => item.next_actions.includes("retry"));
  const exportArtifact = job.artifacts.find(a => a.type === "translation_export");
  const reportArtifact = job.artifacts.find(a => a.type === "translation_report");
  const explainResults = job.results.filter(r => r.intent === "explain");
  const updateResults = job.results.filter(r => r.intent === "propose_update");

  return <div className="chat-card">
    <div className="row-set">
      <span className="mono">J-{job.job_id.slice(0, 6)}</span>
      <Status value={job.status} pill />
      {job.source_lang && job.target_lang && <span className="hint">{language[job.source_lang]} → {language[job.target_lang]}</span>}
      <span className="mono hint">{job.budget.reserved_or_spent}/{job.budget.limit_calls} 调用</span>
      <span className="sp" />
      {!!job.items.length && <Button small ghost onClick={() => onSyncJob(job.job_id)}>在右侧追踪 ({job.items.length})</Button>}
      <Button small ghost onClick={() => onOpenFullJob(job.job_id)}>打开完整任务页</Button>
    </div>

    {!!job.steps.length && <div className="row-set job-steps">
      {job.steps.map((step, i) => <span className="row-set" key={step.index}>
        {i > 0 && <span className="muted">→</span>}
        <span>{intents[step.intent] ?? step.intent}</span>
        <Status value={step.status} />
      </span>)}
    </div>}

    <ErrorNote error={action.error} />
    {job.pause_requested && <div className="note note--warn">当前条目或批次结束后暂停…</div>}
    {job.reason && !can("continue") && <div className="note">{reasonText(job.reason)} <code>{job.reason}</code></div>}

    {can("continue") && <Clarify key={JSON.stringify(job.missing)} job={job} changed={changed} />}

    {job.graph_build && <div className="stack">
      <div className="row-set">
        <span>建图批次 {job.graph_build.completed.length} / {job.graph_build.batches}</span>
        {job.results[0] && <span className="hint">{String(job.results[0].nodes)} 节点 · {String(job.results[0].edges)} 关系</span>}
      </div>
      <div className="meter"><i style={{ width: `${job.status === "completed" ? 100 : 100 * job.graph_build.completed.length / (job.graph_build.batches || 1)}%` }} /></div>
    </div>}

    {!!job.items.length && <div className="stack">
      <div className="row-set hint">
        <span>共 {job.counts.items} 条</span>
        <span>· 通过 {job.counts.passed}</span>
        {job.counts.awaiting_review > 0 && <span>· 待复核 {job.counts.awaiting_review}</span>}
        {job.counts.exportable > 0 && <span>· 可导出 {job.counts.exportable}</span>}
      </div>
      <div className="chat-items">
        {job.items.slice(0, 8).map(item => <button type="button" className="chat-item-row"
          key={`${item.step}:${item.occurrence_id}`} onClick={() => onInspectItem(item.occurrence_id, job.job_id)}>
          <span className={`dot ${tone(itemStatus(item)) ? "dot--" + tone(itemStatus(item)) : ""}`} />
          <span className="clip" style={{ flex: 1, textAlign: "left" }}>{item.source}</span>
          {item.target && <span className="clip muted" style={{ maxWidth: "40%" }}>→ {item.target}</span>}
          <span className="mono hint">{label(itemStatus(item))}</span>
        </button>)}
        {job.items.length > 8 && <span className="hint">另有 {job.items.length - 8} 条，可点击右上角「打开完整任务页」查看全部。</span>}
      </div>
    </div>}

    {explainResults.map((res, idx) => <div className="panel" key={`exp-${idx}`}>
      <h4>依据解释结果</h4>
      {((res.records as Array<{ occurrence_id: string; version: number; target: string; reviews?: Array<{ verdict: string; reason: string }>; actual_context?: { items?: Array<{ category: string; text: string }> } }>) ?? []).map(rec => (
        <div key={rec.occurrence_id} className="block stack">
          <div className="row-set">
            <span className="mono hint">{rec.occurrence_id.slice(0, 18)} · v{rec.version}</span>
            <span className="sp" />
            <Button small ghost onClick={() => onInspectItem(rec.occurrence_id, job.job_id)}>右侧看详情</Button>
          </div>
          <div className="target text-block">{rec.target}</div>
          {!!rec.actual_context?.items?.length && <div className="hint">实际输入证据 {rec.actual_context.items.length} 条</div>}
          {!!rec.reviews?.length && <div className="hint">审校：{rec.reviews.map(r => `${reviewLabels[r.verdict] ?? r.verdict} (${r.reason})`).join("；")}</div>}
        </div>
      ))}
    </div>)}

    {updateResults.map((res, idx) => <div className="panel" key={`upd-${idx}`}>
      <h4>术语变更影响预演</h4>
      {((res.records as Array<{ occurrence_id: string; status: string; diff: { current_target?: string; proposed_target?: string } }>) ?? []).map(rec => (
        <div key={rec.occurrence_id} className="block stack">
          <div className="row-set">
            <span className="mono hint">{rec.occurrence_id.slice(0, 18)}</span>
            <Status value={rec.status} />
            <span className="sp" />
            <Button small ghost onClick={() => onInspectItem(rec.occurrence_id, job.job_id)}>查看</Button>
          </div>
          <div className="text-block">{rec.diff.current_target ?? "暂无译文"}{rec.diff.proposed_target ? ` → ${rec.diff.proposed_target}` : ""}</div>
        </div>
      ))}
    </div>)}

    <div className="row-set">
      {can("pause") && <Button small ghost disabled={action.busy} onClick={() => void action.run(() => change("pause"))}>暂停</Button>}
      {can("resume") && <Button small primary onClick={() => setDialog("resume")}>补充要求并继续</Button>}
      {can("retry") && <Button small onClick={() => { setUnknown(false); setConfirmed(false); setDialog("retry"); }}>重试</Button>}
      {can("accept_partial") && <Button small onClick={() => void action.run(() => change("accept-partial"))}>接受已完成部分</Button>}
      {can("cancel") && <Button small ghost onClick={() => void action.run(() => change("cancel"))}>取消</Button>}
      {job.artifacts.some(a => a.type === "po_preview") && <Button small primary onClick={() => setDialog("po")}>PO 导出预检</Button>}
      {exportArtifact && <a className="btn btn--sm" href={exportArtifact.url}>下载译文 JSON</a>}
      {reportArtifact && terminal.has(job.status) && <a className="btn btn--sm btn--ghost" href={reportArtifact.url}>任务报告</a>}
    </div>

    {dialog === "resume" && <Modal title="补充后续要求并继续" close={() => setDialog(undefined)}>
      <ErrorNote error={action.error} />
      <form className="stack" onSubmit={e => {
        e.preventDefault();
        const req = String(new FormData(e.currentTarget).get("requirements") || "").trim();
        void action.run(async () => { await change("resume", { requirements: req || undefined }); setDialog(undefined); });
      }}>
        {!job.graph_build && <Field label="补充新要求（仅作用于尚未开始的条目）"><textarea name="requirements" className="textarea" maxLength={5000} /></Field>}
        <div className="dialog__foot"><Button primary type="submit" disabled={action.busy}>继续执行</Button></div>
      </form>
    </Modal>}

    {dialog === "retry" && <Modal title="重试任务" close={() => setDialog(undefined)}>
      <ErrorNote error={action.error} />
      <p className="muted">将重试失败或阻塞的步骤。</p>
      {unknown && <label className="check"><input type="checkbox" checked={confirmed} onChange={e => setConfirmed(e.target.checked)} />确认前次调用结果未明，允许再次向模型发出请求</label>}
      <div className="dialog__foot"><Button primary disabled={action.busy || (unknown && !confirmed)} onClick={() => void action.run(async () => {
        try {
          await change("retry", { occurrence_id: failedRows[0]?.occurrence_id, confirm_unknown_call: confirmed });
          setDialog(undefined);
        } catch (err) {
          if (err && typeof err === "object" && "reason" in err && err.reason === "unknown_provider_result_requires_confirmation") setUnknown(true);
          throw err;
        }
      })}>确认重试</Button></div>
    </Modal>}

    {dialog === "po" && <PoDelivery job={job} close={() => setDialog(undefined)} />}
  </div>;
}

function OccurrenceInspector({
  scope, project, occurrenceId, refresh, changed, onBack, onAskFocused, onOpenJob, onOpenGraphNode,
}: {
  scope: string; project?: Project; occurrenceId: string; refresh: number; changed: () => void;
  onBack: () => void; onAskFocused: (presetQuestion?: string) => void;
  onOpenJob: (jobId: string) => void; onOpenGraphNode: () => void;
}) {
  const [tab, setTab] = useState<"overview" | "memory" | "graph" | "review">("overview");
  const inspectQuery = useQuery<InspectData>(`/occurrences/${scope}/${encodeURIComponent(occurrenceId)}/inspect`, refresh);
  const data = inspectQuery.data;
  const action = useAction(changed);
  const [draftText, setDraftText] = useState<string>();
  const [editReason, setEditReason] = useState("");
  const [feedbackOpen, setFeedbackOpen] = useState(false);

  useEffect(() => {
    setDraftText(undefined);
    setEditReason("");
  }, [occurrenceId, data?.active_item?.draft_hash]);

  if (inspectQuery.error) return <div className="stack">
    <div className="row-set"><Button small ghost onClick={onBack}>← 返回文本列表</Button></div>
    <ErrorNote error={inspectQuery.error} />
  </div>;
  if (!data) return <div className="stack">
    <div className="row-set"><Button small ghost onClick={onBack}>← 返回文本列表</Button></div>
    <Empty title="正在读取条目完整溯源细节…" />
  </div>;

  const { resource, translation, active_item: activeItem, evidence, graph, memory } = data;
  const canEdit = !!activeItem?.next_actions.includes("review_item");
  const editorValue = draftText ?? activeItem?.target ?? translation.target ?? "";
  const latestSelection = evidence?.selection?.at(-1);
  const actualItems = evidence?.actual_context?.items ?? [];
  const actualIds = new Set(actualItems.map(i => i.id));
  const unusedCandidates = (evidence?.retrieved ?? []).filter(i => !actualIds.has(i.id));
  const lookups = evidence?.source_lookups ?? [];
  const excludedTotal = lookups.reduce((sum, l) => sum + (l.excluded_hits || 0), 0);
  const nodeNames = new Map(graph.nodes.map(n => [n.id, String(n.attributes?.name || n.evidence[0]?.quote || n.id)]));

  const approveItem = () => {
    if (!activeItem || !canEdit || !editorValue.trim()) return;
    void action.run(async () => {
      await json(activeItem.evidence_url.replace(/\/evidence$/, "/approve"), post({
        target: editorValue,
        expected_version: activeItem.expected_version,
        expected_draft_hash: activeItem.draft_hash,
        reason: editReason.trim() || undefined,
      }));
      setDraftText(undefined);
      setEditReason("");
    });
  };

  return <div className="stack inspector">
    <div className="row-set">
      <Button small ghost onClick={onBack}>← 返回列表</Button>
      <Status value={resource.translation_status ?? "untranslated"} pill />
      {translation.locked && <Status value="locked" pill />}
      <span className="mono hint">v{translation.head_version} · 源 v{resource.source_version}</span>
      <span className="sp" />
      <Button small primary onClick={() => onAskFocused()}>在左侧提问此条</Button>
    </div>

    <div className="segs inspector__tabs">
      {([
        ["overview", "概览与译文"],
        ["memory", `记忆与选择${evidence ? ` (${actualItems.length}/${evidence.retrieved.length})` : ""}`],
        ["graph", `图谱 (${graph.edges.length})`],
        ["review", `审校与查证 (${evidence?.reviews.length ?? 0})`],
      ] as const).map(([k, title]) => (
        <button key={k} type="button" className={`seg${tab === k ? " seg--on" : ""}`}
          aria-pressed={tab === k} onClick={() => setTab(k)}>{title}</button>
      ))}
    </div>

    <ErrorNote error={action.error} />

    {tab === "overview" && <div className="stack">
      <div className="field">
        <div className="row-set">
          <span className="label">原文</span><span className="sp" />
          <span className="mono hint">{locationText(resource.location, resource.file) || resource.occurrence_id.slice(0, 18)}</span>
        </div>
        <div className="source text-block">{resource.label}</div>
      </div>

      {!!Object.keys(resource.metadata ?? {}).length && <div className="ctxgrid">
        {Object.entries(resource.metadata ?? {}).map(([k, v]) => <div className="ctxcell" key={k}>
          <span>{META_LABELS[k] ?? k}</span><b>{v}</b>
        </div>)}
      </div>}

      <div className="field">
        <div className="row-set">
          <span className="label">{canEdit ? "待复核草稿（可直接编辑并批准）" : "当前译文"}</span>
          <span className="sp" />
          {activeItem && <Button small ghost onClick={() => onOpenJob(activeItem.job_id)}>查看来源任务 J-{activeItem.job_id.slice(0, 6)}</Button>}
        </div>
        {canEdit ? <>
          <textarea className="target" value={editorValue} onChange={e => setDraftText(e.target.value)} maxLength={5000} />
          <TranslationChecks source={resource.label} target={editorValue} terms={evidence?.mandatory_terms} />
          {activeItem?.reason && <div className="note note--warn">{activeItem.display_reason && activeItem.display_reason !== activeItem.reason ? activeItem.display_reason : reasonText(activeItem.reason)} <code>{activeItem.reason}</code></div>}
          <Field label="修改原因（可选，自动写入反馈库）">
            <input className="input" value={editReason} onChange={e => setEditReason(e.target.value)} maxLength={1000} placeholder="例如：补充设定语气或修正术语" />
          </Field>
          <div className="row-set">
            <Button primary disabled={action.busy || !editorValue.trim()} onClick={approveItem}>批准译文</Button>
          </div>
        </> : <>
          {translation.target ? <div className="target text-block">{translation.target}</div>
            : <Empty title="该条目尚未翻译">可在左侧直接输入「翻译这条」或点击上方按钮提问。</Empty>}
        </>}
      </div>

      {activeItem && <div className="row-set">
        {(activeItem.next_actions.includes("lock") || activeItem.next_actions.includes("unlock")) && (
          <Button small ghost disabled={action.busy} onClick={() => void action.run(() =>
            json(`/jobs/${activeItem.job_id}/versions/${encodeURIComponent(resource.occurrence_id)}/${translation.locked ? "unlock" : "lock"}`,
              post({ expected_version: translation.head_version })))}>
            {translation.locked ? "解锁版本" : "锁定版本"}
          </Button>
        )}
        {!["queued", "running"].includes(activeItem.status) && <Button small ghost onClick={() => setFeedbackOpen(true)}>登记反馈</Button>}
        <Button small ghost onClick={() => onAskFocused("为什么这样翻译？请结合选中的记忆与审校依据说明。")}>提问：为什么这样翻？</Button>
      </div>}

      {!!translation.versions.length && <details>
        <summary>历史版本（{translation.versions.length}）</summary>
        <div className="stack">
          {translation.versions.map(v => <div className="block stack" key={v.version}>
            <div className="row-set">
              <b>v{v.version}</b><span className="hint">源 v{v.source_version} · {v.reviewer ?? "agent"}</span>
              <span className="sp" />
              {activeItem && v.version < translation.head_version && !translation.locked &&
                <Button small ghost disabled={action.busy} onClick={() => void action.run(() =>
                  json(`/jobs/${activeItem.job_id}/versions/${encodeURIComponent(resource.occurrence_id)}/rollback`,
                    post({ expected_version: translation.head_version, to_version: v.version })))}>回滚</Button>}
            </div>
            <div className="text-block">{v.target}</div>
          </div>)}
        </div>
      </details>}
    </div>}

    {tab === "memory" && <div className="stack">
      {!!data.project_notes?.length && <div className="panel">
        <h4>全局背景 / System Prompt 记忆（{data.project_notes.length}）</h4>
        {data.project_notes.map(n => <div className="block stack" key={n.note_id}>
          <div className="row-set"><b>{n.title}</b><span className="pill pill--plain">{n.category}</span></div>
          <div className="text-block">{n.content}</div>
        </div>)}
      </div>}
      {!evidence ? <Empty title="暂无本次翻译/审校的上下文选择记录">请先对该条目发起一次翻译或审校。</Empty> : <>
        {latestSelection?.decision?.rationale && <div className="note note--ok">
          <b>ContextSelector 选择器决策理由</b>
          <div className="text-block">{latestSelection.decision.rationale}</div>
          {latestSelection.fallback && <div className="hint">触发回退：{latestSelection.fallback}</div>}
        </div>}

        {!!evidence.mandatory_terms.length && <div className="panel">
          <h4>强制硬术语（绕过剪枝，必须遵守）</h4>
          {evidence.mandatory_terms.map((t, i) => <div className="term" key={i}><b>{t.target}</b>← <code>{t.source}</code><em>强制</em></div>)}
        </div>}

        <div className="panel">
          <div className="row-set">
            <h4>实际传入 ContextPack 的证据（{actualItems.length}）</h4>
            <span className="sp" />
            <Button small ghost onClick={() => onAskFocused("ContextSelector 为什么选择了这些记忆而放弃了其他候选？")}>针对选证提问</Button>
          </div>
          {!actualItems.length ? <p className="muted">本次未注入额外软证据。</p>
            : actualItems.map(item => <div className="block stack" key={item.id}>
              <div className="row-set"><span className="pill pill--plain">{MEMORY_KIND_LABELS[item.category] ?? item.category}</span>
                {item.mandatory && <span className="pill pill--warn">强制</span>}
                <span className="mono hint">{item.id}</span></div>
              <div className="text-block">{item.text}</div>
            </div>)}
        </div>

        {!!unusedCandidates.length && <details>
          <summary>检索到但未被选择器采用的候选（{unusedCandidates.length}）</summary>
          <div className="stack">
            {unusedCandidates.map(c => <div className="block stack" key={c.id}>
              <span className="mono hint">{MEMORY_KIND_LABELS[c.category] ?? c.category} · {c.id}</span>
              <div className="text-block">{c.text}</div>
            </div>)}
          </div>
        </details>}
      </>}

      {!!memory.length && <details open={!evidence}>
        <summary>当前项目/示范区匹配记忆（{memory.length}）</summary>
        <div className="stack">
          {memory.map(m => <div className="block" key={`${m.record_id}:${m.version}`}>
            <span className="pill pill--plain">{MEMORY_KIND_LABELS[m.kind] ?? m.kind}</span>
            <div className="text-block">{[m.content.source, m.content.target, m.content.note].filter(Boolean).join(" → ")}</div>
          </div>)}
        </div>
      </details>}
    </div>}

    {tab === "graph" && <div className="stack">
      <div className="row-set">
        <span className="hint">{graph.snapshot_id ? `快照：${graph.snapshot_id}` : "当前尚未发布图谱快照"}</span>
        <span className="sp" />
        {project && <Button small ghost onClick={onOpenGraphNode}>在「图谱与记忆」中查看</Button>}
      </div>
      {!graph.edges.length && !graph.nodes.filter(n => n.kind === "entity").length
        ? <Empty title="该条目暂无关联实体关系">若刚导入文件，请先构建知识图谱；或点击上方对整份文件抽图。</Empty>
        : <>
          <div className="panel">
            <h4>关联实体节点（{graph.nodes.filter(n => n.kind === "entity").length}）</h4>
            {graph.nodes.filter(n => n.kind === "entity").map(n => <div className="block stack" key={n.id}>
              <div className="row-set">
                <b>{String(n.attributes?.name || n.id)}</b>
                <span className="pill pill--info">{String(n.attributes?.entity_type || "entity")}</span>
                <Status value={n.valid ? "valid" : "invalid"} />
              </div>
              {n.evidence.slice(0, 2).map((e, i) => <div key={i} className="hint text-block">引文：{e.quote}</div>)}
            </div>)}
          </div>
          <div className="panel">
            <h4>关联关系边（{graph.edges.length}）</h4>
            {graph.edges.map(e => <div className="block stack" key={e.id}>
              <div className="row-set">
                <span>{nodeNames.get(e.source) ?? e.source}</span>
                <span className="pill pill--plain">{e.relation}</span>
                <span>{nodeNames.get(e.target) ?? e.target}</span>
                <Status value={e.valid ? "valid" : "invalid"} />
              </div>
              {e.evidence.slice(0, 2).map((evItem, i) => <div key={i} className="hint text-block">依据：{evItem.quote}</div>)}
            </div>)}
          </div>
        </>}
    </div>}

    {tab === "review" && <div className="stack">
      {!evidence ? <Empty title="暂无审校与查证轨迹" /> : <>
        {excludedTotal > 0 && <div className="note note--warn">作用域隔离：已自动拦截并屏蔽 {excludedTotal} 条越界或跨分支命中。</div>}

        {!!evidence.hard_checks?.length && <div className="panel">
          <h4>程序硬检查（{evidence.hard_checks.length} 轮）</h4>
          {evidence.hard_checks.map((hc, i) => <div className="row-set" key={i}>
            <Status value={hc.problems.length ? "failed" : "passed"} />
            <span>{hc.problems.length ? hc.problems.join("；") : "占位符、换行、空白与强制术语均通过"}</span>
          </div>)}
        </div>}

        <div className="panel">
          <h4>Pi Reviewer 主动工具查证（{lookups.length}）</h4>
          {!lookups.length ? <p className="muted">本次审校未发起额外只读工具查证。</p>
            : lookups.map((entry, idx) => <div className="block stack" key={idx}>
              <div className="row-set">
                <b>{toolLabels[entry.name] ?? entry.name}</b>
                <span className="pill pill--plain">{reviewLabels[entry.result.status] ?? entry.result.status}</span>
                {entry.excluded_hits > 0 && <span className="hint">屏蔽 {entry.excluded_hits} 条越界</span>}
              </div>
              <div className="mono hint">查询：{entry.effective_query ?? entry.args.query ?? "{}"}</div>
              {entry.result.evidence.map((item, j) => <div key={j} className="text-block block">
                {String(item.quote || item.target || item.note || JSON.stringify(item))}
              </div>)}
            </div>)}
        </div>

        <div className="panel">
          <div className="row-set">
            <h4>审校结论（{evidence.reviews.length} 轮）</h4>
            <span className="sp" />
            <Button small ghost onClick={() => onAskFocused("Pi Reviewer 给出了什么结论和原因？如果经过了修复，修改了哪里？")}>提问审校详情</Button>
          </div>
          {!evidence.reviews.length ? <p className="muted">暂无审校记录。</p>
            : evidence.reviews.map((rev, idx) => <div className="block stack" key={idx}>
              <div className="row-set"><b>第 {idx + 1} 轮</b><span className="pill pill--plain">{reviewLabels[rev.verdict] ?? rev.verdict}</span></div>
              <div className="text-block">{rev.reason}</div>
            </div>)}
        </div>

        {!!evidence.repairs?.length && <div className="panel">
          <h4>定向修复对比</h4>
          {evidence.drafts?.[0] && <div className="hint text-block">初稿：{evidence.drafts[0].target}</div>}
          {evidence.repairs.map((rep, i) => <div key={i} className="target text-block">修复 {i + 1}：{rep.target}</div>)}
        </div>}
      </>}
    </div>}

    {activeItem && feedbackOpen && <FeedbackDialog job={{ job_id: activeItem.job_id } as Job} item={activeItem} close={() => setFeedbackOpen(false)} />}
  </div>;
}

export function AgentWorkspace({
  project, scope, projects, offline, naev, upload, setUpload, selection, setSelection,
  refresh, changed, choose, createProject, navigate, openJob, openGraph,
}: {
  project?: Project; scope: string; projects: Project[]; offline: boolean; naev: boolean;
  upload: Upload | null; setUpload: (upload: Upload) => void; selection: Selection;
  setSelection: (selection: Selection) => void; refresh: number; changed: () => void;
  choose: (id: string) => void; createProject: () => void;
  navigate: (page: "home" | "jobs" | "knowledge" | "models" | "glossary" | "evolution") => void;
  openJob: (id: string) => void; composeText?: () => void; openGraph: () => void;
}) {
  const storageKey = `lore-chat-${scope || "empty"}`;
  const [messages, setMessages] = useState<ChatEntry[]>(() => stored(storageKey, []));
  useEffect(() => { setMessages(stored(storageKey, [])); }, [storageKey]);
  const pushMessages = (updater: (old: ChatEntry[]) => ChatEntry[]) => {
    setMessages(old => {
      const next = updater(old).slice(-40);
      try { localStorage.setItem(storageKey, JSON.stringify(next)); } catch { /* ignore */ }
      return next;
    });
  };

  const [rightOpen, setRightOpen] = useState<boolean>(() => stored("lore-chat-right-open", true));
  const toggleRight = (next?: boolean) => setRightOpen(prev => {
    const val = typeof next === "boolean" ? next : !prev;
    try { localStorage.setItem("lore-chat-right-open", JSON.stringify(val)); } catch { /* ignore */ }
    return val;
  });

  const [input, setInput] = useState("");
  const [inputMode, setInputMode] = useState<"auto" | "ask" | "job">("auto");
  const [requirements, setRequirements] = useState("");
  const [budget, setBudget] = useState("");
  const [showOpts, setShowOpts] = useState(false);

  const [statusFilter, setStatusFilter] = useState<"all" | TranslationStatus>("all");
  const [searchInput, setSearchInput] = useState("");
  const [search, setSearch] = useState("");
  const [offset, setOffset] = useState(0);
  const [focusedId, setFocusedId] = useState<string>("");
  const [importOpen, setImportOpen] = useState(false);
  const [linkedJobId, setLinkedJobId] = useState<string>(() => {
    const last = [...stored<ChatEntry[]>(storageKey, [])].reverse().find(m => m.role === "job");
    return last?.role === "job" ? last.jobId : "";
  });
  const [onlyLinkedJob, setOnlyLinkedJob] = useState(false);

  const chatEndRef = useRef<HTMLDivElement>(null);
  const chatAction = useAction(changed);

  const size = 25;
  const limit = project ? 64 : 12;
  const base = project ? `/projects/${project.project_id}` : null;

  const resourcesQuery = useQuery<{ resources: Resource[]; total: number; counts?: ResourceCounts }>(
    base ? `${base}/resources?${params({ query: search, file: selection.file, status: statusFilter, offset, limit: size })}`
      : scope === "naev" ? `/resources?${params({ status: statusFilter })}` : null,
    refresh,
  );
  const imports = useQuery<{ imports: Import[] }>(base && `${base}/imports`, refresh);
  const progress = useQuery<{ project_revision: number; files: FileProgress[] }>(base && `${base}/files/progress`, refresh);
  const recentJobs = useQuery<JobPage>(scope ? `/jobs?${params({ project: scope, limit: 5 })}` : null, refresh);
  const linkedJobQuery = useQuery<Job>(linkedJobId ? `/jobs/${linkedJobId}` : null, refresh);

  const currentFiles = [...new Map((imports.data?.imports ?? []).slice().reverse().map(file => [file.filename, file])).values()];
  const rawResources = resourcesQuery.data?.resources ?? [];
  const filteredNaev = !project && scope === "naev"
    ? rawResources.filter(item => [item.label, item.file, item.occurrence_id].some(v => v?.toLowerCase().includes(search.toLowerCase())))
    : [];
  const linkedIds = new Set((linkedJobQuery.data?.items ?? []).map(i => i.occurrence_id));
  const baseRows = project ? rawResources : filteredNaev.slice(offset, offset + size);
  const rows = onlyLinkedJob && linkedIds.size > 0 ? baseRows.filter(r => linkedIds.has(r.occurrence_id)) : baseRows;
  const total = onlyLinkedJob && linkedIds.size > 0 ? rows.length : project ? (resourcesQuery.data?.total ?? 0) : filteredNaev.length;
  const counts: ResourceCounts = resourcesQuery.data?.counts ?? { all: total, translated: 0, untranslated: 0, awaiting_review: 0 };

  const focusedResource = rows.find(r => r.occurrence_id === focusedId) ?? rawResources.find(r => r.occurrence_id === focusedId);
  const effectiveMode = inputMode === "ask" && focusedId ? "ask"
    : inputMode === "job" ? "job"
      : looksLikeQuestion(input, !!focusedId, selection.ids.length > 1) ? "ask" : "job";

  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages.length]);

  const toggleSelect = (id: string) => {
    const ids = selection.ids.includes(id) ? selection.ids.filter(i => i !== id) : [...selection.ids, id];
    setSelection({ ...selection, ids });
  };
  const visibleIds = rows.map(r => r.occurrence_id);
  const allVisibleSelected = visibleIds.length > 0 && visibleIds.every(id => selection.ids.includes(id));
  const toggleAllVisible = () => {
    if (allVisibleSelected) {
      const set = new Set(visibleIds);
      setSelection({ ...selection, ids: selection.ids.filter(id => !set.has(id)) });
    } else {
      const merged = [...selection.ids];
      for (const id of visibleIds) {
        if (merged.length >= limit) break;
        if (!merged.includes(id)) merged.push(id);
      }
      setSelection({ ...selection, ids: merged });
    }
  };

  const sendMessage = (customText?: string, forceMode?: "ask" | "job", explicitTargets?: string[]) => {
    const text = (customText ?? input).trim();
    if (!text || !scope) return;
    const modeToUse = forceMode ?? (looksLikeQuestion(text, !!focusedId, selection.ids.length > 1) && inputMode !== "job" ? "ask" : inputMode === "ask" && focusedId ? "ask" : "job");

    if (modeToUse === "ask" && focusedId) {
      const targetOcc = focusedId;
      const sourceLabel = focusedResource?.label ?? targetOcc;
      if (!customText) setInput("");
      pushMessages(old => [...old, { id: crypto.randomUUID(), role: "user", text, meta: `针对条目提问 · ${sourceLabel.slice(0, 26)}` }]);
      void chatAction.run(async () => {
        const res = await json<AskResponse>(`/occurrences/${scope}/${encodeURIComponent(targetOcc)}/ask`, post({ question: text }));
        pushMessages(old => [...old, { id: crypto.randomUUID(), role: "ask", occurrenceId: targetOcc, source: sourceLabel, data: res }]);
      });
      return;
    }

    const targetsToSend = explicitTargets ?? (selection.ids.length ? selection.ids : focusedId ? [focusedId] : []);
    if (!customText) setInput("");
    const metaParts = [
      targetsToSend.length ? `绑定 ${targetsToSend.length} 条` : undefined,
      selection.file ? `文件 ${selection.file}` : undefined,
    ].filter(Boolean).join(" · ");
    pushMessages(old => [...old, { id: crypto.randomUUID(), role: "user", text, meta: metaParts || "工作区指令" }]);

    void chatAction.run(async () => {
      const job = await json<Job>("/jobs", post({
        text,
        selection: { project: scope, source_ids: targetsToSend, file: selection.file },
        upload_id: selection.upload,
        requirements: requirements.trim() || undefined,
        max_calls: Number(budget) || undefined,
      }));
      pushMessages(old => [...old, { id: crypto.randomUUID(), role: "job", jobId: job.job_id }]);
      setLinkedJobId(job.job_id);
    });
  };

  const runDirectTask = (intentsList: string[], labelText: string) => {
    const targetsToSend = selection.ids.length ? selection.ids : focusedId ? [focusedId] : [];
    if (!targetsToSend.length || !scope) return;
    pushMessages(old => [...old, { id: crypto.randomUUID(), role: "user", text: labelText, meta: `选中 ${targetsToSend.length} 条` }]);
    void chatAction.run(async () => {
      const job = await json<Job>("/jobs", post({
        intents: intentsList,
        selection: { project: scope, source_ids: targetsToSend, file: selection.file, target_lang: project?.target_lang ?? "zh" },
        upload_id: selection.upload,
        requirements: requirements.trim() || undefined,
        max_calls: Number(budget) || undefined,
      }));
      pushMessages(old => [...old, { id: crypto.randomUUID(), role: "job", jobId: job.job_id }]);
      setLinkedJobId(job.job_id);
    });
  };

  const composerControls = <>
    {(focusedId || selection.ids.length > 0) && <div className="chat-context-bar">
      {focusedResource && <span className="pill pill--info">
        聚焦：{focusedResource.label.slice(0, 26)}…
        <button type="button" className="chip-x" aria-label="取消聚焦" onClick={() => setFocusedId("")}>×</button>
      </span>}
      {selection.ids.length > 0 && <span className="pill pill--warn">
        已勾选 {selection.ids.length}/{limit} 条
        <button type="button" className="chip-x" aria-label="清空勾选" onClick={() => setSelection({ ...selection, ids: [] })}>×</button>
      </span>}
      <span className="sp" />
      {(selection.ids.length > 0 || focusedId) && <>
        <Button small disabled={!!project && !project.graph_ready}
          onClick={() => runDirectTask(["translate", "export"], "翻译并导出选中文本")}>翻译并导出</Button>
        <Button small ghost disabled={!!project && !project.graph_ready}
          onClick={() => runDirectTask(["review"], "审校选中文本")}>仅审校</Button>
      </>}
    </div>}

    <div className="row-set" style={{ marginBottom: 4 }}>
      <span className="micro">模式</span>
      <button type="button" className={`seg${effectiveMode === "ask" ? " seg--on" : ""}`} disabled={!focusedId}
        onClick={() => setInputMode("ask")}>提问当前条目{!focusedId ? "（右侧点选）" : ""}</button>
      <button type="button" className={`seg${effectiveMode === "job" ? " seg--on" : ""}`}
        onClick={() => setInputMode("job")}>下达任务</button>
      <button type="button" className={`seg${inputMode === "auto" ? " seg--on" : ""}`}
        onClick={() => setInputMode("auto")}>自动识别</button>
      <span className="sp" />
      <Button small ghost onClick={() => setShowOpts(v => !v)}>{showOpts ? "收起参数" : "参数 / 预算"}</Button>
    </div>

    {showOpts && <div className="form-grid" style={{ marginBottom: 6 }}>
      <Field label="附加翻译要求（可选）"><input className="input" value={requirements} onChange={e => setRequirements(e.target.value)}
        placeholder="例如：保持科幻军事口吻" /></Field>
      <Field label="调用上限 max_calls（可选）"><input className="input" type="number" min="1" max="1000" value={budget}
        onChange={e => setBudget(e.target.value)} placeholder="默认" /></Field>
    </div>}

    <form className="chat-input-row" onSubmit={e => { e.preventDefault(); sendMessage(); }}>
      <textarea
        className={messages.length === 0 ? "center-panel__input" : "textarea chat-textarea"}
        rows={2}
        value={input}
        disabled={chatAction.busy}
        placeholder={effectiveMode === "ask"
          ? "针对右侧当前查看的译文提问，例如：为什么这样翻译？使用了哪些记忆与图谱关系？"
          : "输入自然语言任务指令（Enter 发送，Shift+Enter 换行），例如：把选中的条目翻译成简体中文并导出"}
        onChange={e => setInput(e.target.value)}
        onKeyDown={e => {
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            sendMessage();
          }
        }} />
      <Button primary type="submit" disabled={!input.trim() || chatAction.busy}>
        <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M5 12h14" />
          <path d="m13 6 6 6-6 6" />
        </svg>
        <span>{chatAction.busy ? "处理中…" : effectiveMode === "ask" ? "提问" : "发送"}</span>
      </Button>
    </form>
  </>;

  return <div className={`agent-shell${rightOpen ? "" : " agent-shell--single"}`}>
    {/* ==================== LEFT: CONVERSATIONAL AGENT ==================== */}
    <section className={`chat-pane${scope && !messages.length ? " chat-pane--hero" : ""}`} aria-label="Agent 对话工作区">
      <header className="chat-pane__head">
        <div>
          <div className="row-set">
            <h1>{project ? project.name : scope === "naev" ? "Naev 示范区" : "LoRE Agent 对话"}</h1>
            {project && <span className={`pill ${project.graph_ready ? "pill--ok" : "pill--warn"}`}>
              {project.graph_ready ? "图谱就绪" : "待建图"}
            </span>}
            {offline && <span className="pill pill--plain">离线演示</span>}
          </div>
          <div className="sub">
            {project ? `${language[project.source_lang]} → ${language[project.target_lang]} · 源 v${project.revision} · 已批准记忆 ${project.approved_memory ?? 0}`
              : scope === "naev" ? "英语 → 简体中文 · 支持自然语言任务、主动取证与译文溯源提问" : "请先在右侧或点击新建项目"}
          </div>
        </div>
        <div className="actions">
          {project && !project.graph_ready && (project.resources ?? 0) > 0 && <Button small primary onClick={openGraph}>去构建图谱</Button>}
          {!!messages.length && <Button small ghost onClick={() => pushMessages(() => [])}>清空对话</Button>}
          <Button small ghost={!rightOpen} onClick={() => toggleRight()} aria-expanded={rightOpen}
            title={rightOpen ? "收起右侧译文与透明检视栏" : "展开右侧译文与透明检视栏"}>
            {rightOpen ? "收起右侧栏" : `展开译文栏${scope ? ` (${counts.all})` : ""}`}
          </Button>
        </div>
      </header>

      {!scope ? <div className="chat-feed">
        <div className="chat-hero">
          <h2 className="chat-hero__title">欢迎使用 LoRE 本地化工作台</h2>
          <p className="chat-hero__sub">请先创建一个本地化项目并导入文本文件，或进入「模型设置」配置 OpenAI 协议接口与本地模型连接。</p>
          <div className="row-set">
            <Button primary onClick={createProject}>+ 新建项目</Button>
            <Button onClick={() => navigate("models")}>配置模型连接</Button>
          </div>
        </div>
      </div> : <>
        <div className="chat-feed">
          {!messages.length && <div className="chat-hero">
            <h2 className="chat-hero__title">{focusedId ? "向 Agent 追问当前译文细节" : "今天想推进哪部分本地化？"}</h2>
            <p className="chat-hero__sub">
              直接用自然语言下达翻译、审校或术语变更指令；或在右侧挑选任意条目，查看记忆检索依据、知识图谱关联与 Pi 审校细节。
            </p>
            <div className="center-panel glass">
              {composerControls}
            </div>
            <div className="row-set" style={{ justifyContent: "center" }}>
              {focusedId ? <>
                <Button small ghost onClick={() => sendMessage("为什么这样翻译？用了哪些记忆和依据？", "ask")}>提问：为什么这样翻？</Button>
                <Button small ghost onClick={() => sendMessage("上下文选择器为什么选中了这些记忆，没选其他候选？", "ask")}>提问：记忆选择理由</Button>
                <Button small ghost onClick={() => sendMessage("这条文本在知识图谱里关联了哪些实体和关系？", "ask")}>提问：图谱关系</Button>
                <Button small ghost onClick={() => sendMessage("Pi Reviewer 做了什么查证与审校判定？", "ask")}>提问：Review 细节</Button>
              </> : <>
                <Button small ghost onClick={() => setInput("把选中的条目翻译成简体中文并导出")}>示例：翻译选中条目并导出</Button>
                <Button small ghost onClick={() => setInput("帮我审校选中的译文，检查是否漏掉上下文设定")}>示例：审校选中译文</Button>
                {scope === "naev" && <Button small ghost onClick={() => setInput("把术语 Landing 改成 着陆，预演会影响哪些句子")}>示例：术语变更预演</Button>}
              </>}
            </div>
            {!!recentJobs.data?.jobs.length && <div className="row-set" style={{ justifyContent: "center" }}>
              <span className="micro">最近任务</span>
              {recentJobs.data.jobs.map(j => <Button key={j.job_id} small ghost onClick={() => {
                if (!messages.some(m => m.role === "job" && m.jobId === j.job_id)) {
                  pushMessages(old => [...old, { id: crypto.randomUUID(), role: "job", jobId: j.job_id }]);
                }
              }}>J-{j.job_id.slice(0, 6)} · {label(j.status)}</Button>)}
            </div>}
          </div>}

          {messages.map(msg => {
            if (msg.role === "user") {
              return <div className="chat-msg chat-msg--user" key={msg.id}>
                <div className="chat-bubble chat-bubble--user">
                  <div className="text-block">{msg.text}</div>
                  {msg.meta && <span className="chat-meta">{msg.meta}</span>}
                </div>
              </div>;
            }
            if (msg.role === "ask") {
              return <div className="chat-msg chat-msg--agent" key={msg.id}>
                <div className="chat-card">
                  <div className="row-set">
                    <b>LoRE 溯源回答</b>
                    <span className="pill pill--plain mono">{msg.data.model}</span>
                    <span className="sp" />
                    <Button small ghost onClick={() => setFocusedId(msg.occurrenceId)}>查看右侧完整细节</Button>
                  </div>
                  <div className="text-block chat-answer">{msg.data.answer}</div>
                  {!!msg.data.citations.length && <div className="row-set">
                    {msg.data.citations.slice(0, 6).map((c, idx) => <span key={idx} className="pill pill--plain mono" title={c.text}>
                      {c.kind}: {c.text.slice(0, 36)}
                    </span>)}
                  </div>}
                </div>
              </div>;
            }
            if (msg.role === "error") {
              return <div className="chat-msg chat-msg--agent" key={msg.id}><ErrorNote reason={msg.text} /></div>;
            }
            return <div className="chat-msg chat-msg--agent" key={msg.id}>
              <JobBubbleCard jobId={msg.jobId} refresh={refresh} changed={changed}
                onInspectItem={(occId, jId) => { setLinkedJobId(jId); setFocusedId(occId); toggleRight(true); }}
                onSyncJob={jId => { setLinkedJobId(jId); setOnlyLinkedJob(true); setFocusedId(""); toggleRight(true); }}
                onOpenFullJob={openJob} />
            </div>;
          })}
          <ErrorNote error={chatAction.error} />
          <div ref={chatEndRef} />
        </div>

        {!!messages.length && <footer className="chat-composer">
          {composerControls}
        </footer>}
      </>}
    </section>

    {/* ==================== RIGHT: TRANSLATED / UNTRANSLATED LIST & TRANSPARENT INSPECTOR ==================== */}
    {rightOpen && <aside className="inspect-pane" aria-label="文本列表与透明细节">
      <div className="row-set inspect-pane__bar">
        <span className="micro">{focusedId ? "译文透明检视" : "译文与待译列表"}</span>
        <span className="sp" />
        <Button small ghost onClick={() => toggleRight(false)}>收起</Button>
      </div>
      {!scope ? <div className="panel">
        <h4>已有项目</h4>
        {naev && <div className="row-set"><b>Naev 示范区</b><span className="sp" /><Button small onClick={() => choose("naev")}>打开</Button></div>}
        {projects.map(p => <div className="row-set" key={p.project_id}><b>{p.name}</b><span className="sp" /><Button small onClick={() => choose(p.project_id)}>打开</Button></div>)}
      </div> : focusedId ? <OccurrenceInspector
        scope={scope} project={project} occurrenceId={focusedId} refresh={refresh} changed={changed}
        onBack={() => setFocusedId("")}
        onAskFocused={preset => {
          setInputMode("ask");
          if (preset) sendMessage(preset, "ask");
        }}
        onOpenJob={openJob}
        onOpenGraphNode={openGraph}
      /> : <div className="stack" style={{ minHeight: 0, flex: 1 }}>
        {linkedJobQuery.data && linkedIds.size > 0 && <div className="note note--ok row-set">
          <span>联动任务 <b className="mono" style={{ display: "inline" }}>J-{linkedJobId.slice(0, 6)}</b>（{linkedIds.size} 条）</span>
          <span className="sp" />
          <Button small ghost={!onlyLinkedJob} primary={onlyLinkedJob}
            onClick={() => setOnlyLinkedJob(v => !v)}>{onlyLinkedJob ? "显示全部条目" : "只看本任务"}</Button>
          <Button small ghost onClick={() => openJob(linkedJobId)}>详情</Button>
          <button type="button" className="chip-x" aria-label="取消任务联动" onClick={() => { setLinkedJobId(""); setOnlyLinkedJob(false); }}>×</button>
        </div>}

        <div className="row-set">
          {project ? <>
            <select className="input" style={{ flex: 1, minWidth: 0 }} aria-label="按文件筛选" value={selection.file ?? ""} onChange={e => {
              setOffset(0); setSelection({ ids: [], file: e.target.value || undefined });
            }}>
              <option value="">全部文件 ({counts.all})</option>
              {currentFiles.map(f => <option key={f.import_id} value={f.filename}>{f.filename}</option>)}
            </select>
            {!project.resources && <Button small primary onClick={() => setImportOpen(true)}>+ 导入文件</Button>}
          </> : <>
            <span className="label">Naev 示范资源</span>
            <span className="sp" />
            <label className="btn btn--sm">上传 PO<input className="file-control" type="file" accept=".po"
              onChange={event => {
                const file = event.target.files?.[0];
                if (file) void json<Upload>(`/po-uploads?${params({ filename: file.name })}`, { method: "POST", body: file })
                  .then(next => { setUpload(next); setSelection({ ids: [], upload: next.upload_id }); });
              }} /></label>
          </>}
        </div>

        {project && !!progress.data?.files.length && <div className="meter" title={`可交付 ${counts.translated} / ${counts.all}`}>
          <i style={{ width: `${counts.all ? Math.round(100 * counts.translated / counts.all) : 0}%` }} />
        </div>}

        <div className="segs" role="tablist" aria-label="按翻译状态筛选">
          {([
            ["all", `全部 ${counts.all}`],
            ["untranslated", `未翻译 ${counts.untranslated}`],
            ["awaiting_review", `待复核 ${counts.awaiting_review}`],
            ["translated", `已翻译 ${counts.translated}`],
          ] as const).map(([key, title]) => (
            <button key={key} type="button" role="tab" aria-selected={statusFilter === key}
              className={`seg${statusFilter === key ? " seg--on" : ""}`}
              onClick={() => { setStatusFilter(key); setOffset(0); }}>{title}</button>
          ))}
        </div>

        <form className="row-set" onSubmit={e => { e.preventDefault(); setOffset(0); setSearch(searchInput.trim()); }}>
          <input className="input" style={{ flex: 1 }} placeholder="搜索原文或上下文…" value={searchInput}
            onChange={e => setSearchInput(e.target.value)} />
          <Button small type="submit">搜</Button>
          {search && <Button small ghost onClick={() => { setSearchInput(""); setSearch(""); setOffset(0); }}>清除</Button>}
        </form>

        <div className="row-set">
          <label className="check">
            <input type="checkbox" checked={allVisibleSelected} onChange={toggleAllVisible} />全选本页
          </label>
          <span className="sp" />
          <span className="hint">点击任意文本查看细节（记忆/图谱/Review）</span>
        </div>

        <ErrorNote error={resourcesQuery.error} />
        <div className="inspect-list">
          {!rows.length ? <Empty title={search ? "没有匹配的文本" : "当前分类下暂无文本"}>{project && !project.resources ? "请点击上方「+ 导入」上传 CSV、XLSX 或 UTF-8 PO 文件。" : undefined}</Empty>
            : rows.map(row => {
              const st = row.translation_status ?? "untranslated";
              const dotTone = st === "translated" ? "ok" : st === "awaiting_review" ? "warn" : undefined;
              return <div key={row.occurrence_id} className="inspect-row">
                <input type="checkbox" aria-label={`勾选 ${row.occurrence_id}`}
                  checked={selection.ids.includes(row.occurrence_id)}
                  disabled={!selection.ids.includes(row.occurrence_id) && selection.ids.length >= limit}
                  onChange={() => toggleSelect(row.occurrence_id)} />
                <button type="button" className="inspect-row__body" onClick={() => setFocusedId(row.occurrence_id)}>
                  <div className="row-set">
                    <span className={`dot ${dotTone ? "dot--" + dotTone : ""}`} />
                    <span className="clip" style={{ flex: 1, fontWeight: 500 }}>{row.label}</span>
                    {row.locked && <span className="mono hint">锁定</span>}
                    {!!row.target_version && <span className="mono hint">v{row.target_version}</span>}
                  </div>
                  <div className="row-set">
                    <span className="clip muted" style={{ flex: 1 }}>
                      {row.target ? `译：${row.target}` : st === "awaiting_review" ? "待人工复核" : "尚未翻译"}
                    </span>
                    <span className="mono hint">{locationText(row.location, row.file)}</span>
                  </div>
                </button>
              </div>;
            })}
        </div>
        <Pager total={total} offset={offset} size={size} setOffset={setOffset} />
      </div>}
    </aside>}

    {project && importOpen && <ImportDialog project={project} close={() => setImportOpen(false)} changed={() => {
      setSelection({ ids: [] }); setOffset(0); changed();
    }} />}
  </div>;
}
