import { useState } from "react";
import { type Job, type JobPage, intents, json, params, post, terminal, useQuery } from "./api";
import { type PlanSummary } from "./glossary";
import { Button, Empty, ErrorNote, Field, Grid, Head, Modal, Section, Status, useAction } from "./ui";

type Ref = { job_id: string; step: number; occurrence_id: string };
type Feedback = Ref & {
  feedback_id: string; reason: string; category: string; before?: string; after?: string;
  alias?: { query: string; canonical: string };
};
type Spec = {
  prompts: Record<string, string>; handoff_template: string; aliases: Record<string, string>;
  lookup_results: number; skill_issues: string[]; memory_plan_id: string | null;
};
type Release = { release_id: string; parent: string | null; reason?: string; components: Record<string, string>; spec: Spec };
type Candidate = {
  candidate_id: string; base: Release; spec: Spec; components: Record<string, string>;
  changes?: Record<string, unknown>;
  decision?: { decision: "publish" | "retain"; reason: string };
};
type ReplayCase = Ref & { source: string; file?: string };
type Arm = {
  execution?: string; disposition?: string; evidence_ids?: string[]; evidence_hit?: boolean | null;
  outside_scope?: number; hard_problems?: string[]; display?: string;
  lookup_receipts?: { effective_query?: string; args?: { query?: string }; excluded_hits?: number; result?: { status?: string } }[];
};
type Replay = {
  replay_id: string; dataset: string; mode: string; live: boolean; passed: boolean;
  evidence_gaps_resolved: number;
  metrics: { before: Record<string, number | null>; after: Record<string, number | null> };
  rows: (Ref & { before: Arm; after: Arm; passed: boolean })[];
};
type Policy = { active: Release; releases: Release[]; candidates: Candidate[] };
type Details = { candidate: Candidate; replays: Replay[]; publication_checks: Record<string, boolean> };

const categories: Record<string, string> = {
  correction: "译文纠偏", retrieval: "检索遗漏", prompt: "提示指令",
  memory: "记忆问题", skill: "查证流程", harness: "工具调用",
};
const gates: Record<string, string> = {
  policy_base_changed: "候选基于当前生效版本（无版本分叉）",
  record_replay_and_held_out_validation_required: "「反馈案例」与「独立验证」均通过历史回放",
  strategy_replay_and_held_out_validation_required: "「反馈案例」与「独立验证」均通过策略对照",
  prompt_replay_needs_expected_verdicts: "真实模型策略回放已标注预期审校结论",
  retrieval_change_needs_observed_evidence_improvement: "对比回放中观测到了证据缺口改善",
  memory_plan_requires_current_p4_approval: "关联的术语预演方案已在术语库正式批准",
};
const skills: Record<string, string> = { ambiguity: "歧义", missing_evidence: "证据缺失", terminology: "术语核对", context: "上下文缺失" };
const metrics: Record<string, string> = {
  cases: "案例数", evidence_hit_rate: "证据命中率", outside_scope: "越界引用", hard_rule_violations: "硬规则违规",
  unsafe_commits: "不安全提交", human_rate: "转人工率", calls: "模型调用", tokens: "消耗 Token", duration_ms: "耗时（ms）",
};
const datasetName = (value: string) => value === "feedback" ? "反馈案例" : "独立验证";
const modeName = (value: string) => value === "record" ? "历史回放" : "策略对照";
const caseKey = (ref: Ref) => `${ref.job_id}:${ref.step}:${ref.occurrence_id}`;

function FeedbackRegisterModal({ scope, close, created }: {
  scope?: string; close: () => void; created: () => void;
}) {
  const jobs = useQuery<JobPage>(`/jobs?${params({ project: scope, limit: 30 })}`);
  const finishedJobs = (jobs.data?.jobs ?? []).filter(j => terminal.has(j.status) && j.source_kind !== "graph_build");
  const [jobId, setJobId] = useState("");
  const detail = useQuery<Job>(jobId ? `/jobs/${jobId}` : null);
  const items = (detail.data?.items ?? []).filter(i => !["queued", "running"].includes(i.status));
  const [occKey, setOccKey] = useState("");
  const [category, setCategory] = useState("retrieval");
  const [reason, setReason] = useState("");
  const [aliasQuery, setAliasQuery] = useState("");
  const [aliasCanonical, setAliasCanonical] = useState("");
  const action = useAction();

  return <Modal title="登记 Agent 进化反馈" close={close}>
    <ErrorNote error={jobs.error || detail.error || action.error} />
    <div className="note">
      从已完成的历史任务中选取一条出现问题的条目（例如因别名拼写没查到设定、审校漏判或提示不合适），登记后即可用于生成下一代策略候选。
    </div>
    <form className="stack" onSubmit={event => {
      event.preventDefault();
      const picked = items.find(i => `${i.step}:${i.occurrence_id}` === occKey);
      if (!picked) return;
      void action.run(async () => {
        await json("/feedback", post({
          job_id: jobId,
          step: picked.step,
          occurrence_id: picked.occurrence_id,
          category,
          reason: reason.trim(),
          ...(aliasQuery.trim() && aliasCanonical.trim()
            ? { alias: { query: aliasQuery.trim(), canonical: aliasCanonical.trim() } } : {}),
        }));
        created();
        close();
      });
    }}>
      <Field label="选择历史任务">
        <select className="input" required value={jobId} onChange={e => { setJobId(e.target.value); setOccKey(""); }}>
          <option value="">请选择已结束的翻译/审校任务…</option>
          {finishedJobs.map(j => <option key={j.job_id} value={j.job_id}>
            J-{j.job_id.slice(0, 6)} · {j.title || j.intents.map(i => intents[i] ?? i).join(" → ") || "任务"}
          </option>)}
        </select>
      </Field>
      {jobId && <Field label="选择问题条目">
        <select className="input" required value={occKey} onChange={e => setOccKey(e.target.value)}>
          <option value="">请选择具体条目…</option>
          {items.map(i => <option key={`${i.step}:${i.occurrence_id}`} value={`${i.step}:${i.occurrence_id}`}>
            {i.source.slice(0, 80)}{i.target ? ` → ${i.target.slice(0, 40)}` : ""}
          </option>)}
        </select>
      </Field>}
      <Field label="问题类别">
        <select className="input" value={category} onChange={e => setCategory(e.target.value)}>
          {Object.entries(categories).map(([k, title]) => <option key={k} value={k}>{title}</option>)}
        </select>
      </Field>
      <Field label="问题说明 / 改译原因">
        <textarea className="textarea" rows={3} value={reason} onChange={e => setReason(e.target.value)}
          placeholder="例如：原文写 Emperors Fist 缺少撇号，导致 Pi 查图谱未命中 Emperor's Fist 的阵营设定"
          maxLength={2000} required />
      </Field>
      <div className="form-grid">
        <Field label="实体别名原词（可选，检索失败时填）">
          <input className="input" value={aliasQuery} onChange={e => setAliasQuery(e.target.value)} placeholder="如 Emperors Fist" maxLength={120} />
        </Field>
        <Field label="对应标准实体名（可选）">
          <input className="input" value={aliasCanonical} onChange={e => setAliasCanonical(e.target.value)} placeholder="如 Emperor's Fist" maxLength={120} />
        </Field>
      </div>
      <div className="dialog__foot">
        <Button ghost onClick={close}>取消</Button>
        <Button primary type="submit" disabled={!occKey || !reason.trim() || action.busy}>
          {action.busy ? "正在保存…" : "登记反馈"}
        </Button>
      </div>
    </form>
  </Modal>;
}

function CandidateForm({ active, linked, naev, close, created }: {
  active: Release; linked: string[]; naev: boolean; close: () => void; created: (id: string) => void;
}) {
  const plans = useQuery<{ plans: PlanSummary[] }>(naev ? "/maintenance" : null);
  const action = useAction();
  const [manual, setManual] = useState(false);
  return <Modal title="创建策略候选" close={close}><ErrorNote error={action.error || plans.error} />
    <div className="note">
      修改仅作用于策略覆盖层（不改大模型权重）：支持调整查证别名扩展、审校补充提示、返回结果上限与转人工模板；保存后需先通过「隔离对照回放」才可发布。
    </div>
    <form className="stack" onSubmit={event => {
      event.preventDefault();
      const data = new FormData(event.currentTarget);
      const prompt = String(data.get("prompt")).trim();
      const lines = String(data.get("aliases") || "").split("\n").map(line => line.trim()).filter(Boolean);
      const aliases = lines.map(line => line.split("→").map(part => part.trim()));
      const aliasBox = event.currentTarget.elements.namedItem("aliases") as HTMLTextAreaElement | null;
      if (aliases.some(pair => pair.length !== 2 || !pair[0] || !pair[1])) {
        aliasBox?.setCustomValidity("每行填写一组「查询词 → 标准实体名」。"); aliasBox?.reportValidity(); return;
      }
      void action.run(async () => {
        const candidate = await json<Candidate>("/policy/candidates", post({
          feedback_ids: linked, changes: {
            prompts: prompt ? { reviewer_source: prompt } : undefined,
            handoff_template: data.get("handoff"), lookup_results: Number(data.get("limit")),
            aliases: manual ? Object.fromEntries(aliases) : undefined,
            skill_issues: data.getAll("issues"),
            ...(naev ? { memory_plan_id: data.get("plan") || null } : {}),
          },
        }));
        created(candidate.candidate_id);
      });
    }}>
      <p>关联 {linked.length} 条反馈 · 基于当前版本 <code>{active.release_id.slice(0, 8)}</code></p>
      <Field label="审校 Agent 追加提示（可选）">
        <textarea className="textarea" name="prompt" maxLength={4000} defaultValue={active.spec.prompts.reviewer_source}
          placeholder="例如：遇到专有名词变体先尝试去掉/补上所有格撇号再查源图" />
      </Field>
      <Field label="转人工原因模板（必须包含 {reason}）">
        <input className="input" name="handoff" maxLength={500} defaultValue={active.spec.handoff_template} required />
      </Field>
      <label className="check">
        <input type="checkbox" checked={manual} onChange={e => setManual(e.target.checked)} />手动编辑实体别名映射（不勾则自动从重复反馈中抽取）
      </label>
      {manual && <Field label="每行一组：查询词 → 标准实体名">
        <textarea className="textarea" name="aliases" placeholder="Emperors Fist → Emperor's Fist"
          onChange={e => e.target.setCustomValidity("")} />
      </Field>}
      <div className="form-grid">
        <Field label="单次工具检索返回条数">
          <select className="input" name="limit" defaultValue={active.spec.lookup_results}>
            <option value="2">2 条（默认）</option><option value="1">1 条（精简上下文）</option>
          </select>
        </Field>
        {naev && <Field label="沙盒术语预演方案（Naev）">
          <select className="input" name="plan" defaultValue={active.spec.memory_plan_id ?? ""}>
            <option value="">不挂载术语变更</option>
            {plans.data?.plans.filter(plan => plan.status === "preview" || plan.plan_id === active.spec.memory_plan_id)
              .map(plan => <option key={plan.plan_id} value={plan.plan_id}>{plan.rule.source} → {plan.rule.target}</option>)}
          </select>
        </Field>}
      </div>
      <div className="field"><span className="label">允许启用别名扩展的问题场景</span>
        <div className="row-set">
          {Object.entries(skills).map(([key, title]) => <label key={key} className="check">
            <input type="checkbox" name="issues" value={key} defaultChecked={active.spec.skill_issues.includes(key)} />{title}
          </label>)}
        </div>
      </div>
      <div className="dialog__foot">
        <Button ghost onClick={close}>取消</Button>
        <Button primary type="submit" disabled={action.busy}>保存策略候选</Button>
      </div>
    </form>
  </Modal>;
}

function ReplayForm({ id, offline, close, changed }: { id: string; offline: boolean; close: () => void; changed: () => void }) {
  const [mode, setMode] = useState("strategy");
  const [dataset, setDataset] = useState("feedback");
  const [jobId, setJobId] = useState("");
  const [selected, setSelected] = useState<ReplayCase[]>([]);
  const jobs = useQuery<JobPage>(dataset === "validation" ? `/jobs?limit=50` : null);
  const cases = useQuery<{ cases: ReplayCase[]; auto_discovered?: boolean }>(
    `/policy/candidates/${id}/cases?${params({ dataset, job_id: dataset === "validation" ? (jobId || undefined) : undefined })}`,
  );
  const action = useAction(changed);

  return <Modal title="双集隔离对照回放" close={close} className="context-graph-dialog">
    <ErrorNote error={jobs.error || cases.error || action.error} />
    <div className="note">
      <b>为什么要运行双集回放？</b>
      必须同时跑通「<b>反馈案例（历史回放+策略对照）</b>」确认痛点已解决，并跑通「<b>独立验证（历史回放+策略对照）</b>」确保没有改坏不相关的历史句子，才能解锁正式发布。
    </div>
    <form className="stack" onSubmit={event => {
      event.preventDefault();
      const data = new FormData(event.currentTarget);
      void action.run(async () => {
        await json(`/policy/candidates/${id}/replay`, post({
          mode, dataset, live: data.has("live"), cases: selected.map(item => ({
            job_id: item.job_id, step: item.step, occurrence_id: item.occurrence_id,
            expected_evidence_ids: String(data.get("evidence-" + caseKey(item)) || "").split(/[,\s]+/).filter(Boolean),
            expected_verdict: data.get("verdict-" + caseKey(item)) || undefined,
          })),
        }));
        close();
      });
    }}>
      <div className="form-grid">
        <Field label="测试模式">
          <select className="input" value={mode} onChange={e => setMode(e.target.value)}>
            <option value="strategy">策略对照（重查知识图谱验证检索改进）</option>
            <option value="record">历史回放（零调用校验模板与硬规则）</option>
          </select>
        </Field>
        <Field label="验证数据集">
          <select className="input" value={dataset} onChange={e => { setDataset(e.target.value); setSelected([]); setJobId(""); }}>
            <option value="feedback">反馈案例（验证原问题已修好）</option>
            <option value="validation">独立验证（防回归：自动排除相关文件/条目）</option>
          </select>
        </Field>
      </div>
      {mode === "strategy" && <label className="check">
        <input type="checkbox" name="live" disabled={offline} />启用真实模型重新执行首次审校（仅改动审校提示时需要勾选）
      </label>}
      {dataset === "validation" && <Field label="按指定任务筛选（默认已自动汇总全部合规无泄漏案例）">
        <select className="input" value={jobId} onChange={e => { setJobId(e.target.value); setSelected([]); }}>
          <option value="">自动推荐全部不重叠验证案例（推荐）</option>
          {(jobs.data?.jobs ?? []).filter(job => terminal.has(job.status) && job.source_kind !== "graph_build").map(job =>
            <option key={job.job_id} value={job.job_id}>
              J-{job.job_id.slice(0, 6)} · {job.title || job.intents.map(i => intents[i] ?? i).join(" → ")}
            </option>)}
        </select>
      </Field>}

      {cases.data && !cases.data.cases.length && <Empty title="暂无可用于当前验证集的合规案例">
        {dataset === "validation"
          ? "提示：独立验证案例必须来自“与反馈不同文件或不同源文本”的历史已完成审校任务。请先多完成至少 1 份其他条目/文件的翻译审校任务。"
          : "所选反馈条目的历史轨迹不完整，请选用已完成审校阶段的条目。"}
      </Empty>}

      {!!cases.data?.cases.length && <div className="stack">
        <div className="row-set">
          <span className="micro">勾选测试样本（最多 8 条）</span>
          <span className="sp" />
          <Button small ghost onClick={() => setSelected(cases.data!.cases.slice(0, 8))}>全选前 {Math.min(8, cases.data.cases.length)} 条</Button>
        </div>
        <div className="scroll-list stack" style={{ maxHeight: 200 }}>
          {cases.data.cases.map(item => {
            const checked = selected.some(row => caseKey(row) === caseKey(item));
            return <label className="check text-block" key={caseKey(item)}>
              <input type="checkbox" checked={checked} disabled={!checked && selected.length >= 8}
                onChange={e => setSelected(old => e.target.checked ? [...old, item] :
                  old.filter(row => caseKey(row) !== caseKey(item)))} />
              <span>{item.source} <small className="mono hint">{item.file || `J-${item.job_id.slice(0, 6)}`}</small></span>
            </label>;
          })}
        </div>
      </div>}

      {selected.map(item => <details key={caseKey(item)}><summary>预期断言（可选）· {item.source.slice(0, 70)}</summary>
        <div className="stack">
          <Field label="期望命中的证据 ID（如 source:naev_xxx@1，逗号分隔）">
            <input className="input" name={"evidence-" + caseKey(item)} />
          </Field>
          <Field label="预期审校结论（启用真实模型回放时填写）">
            <select className="input" name={"verdict-" + caseKey(item)}>
              <option value="">不强制指定</option><option value="pass">通过 (pass)</option>
              <option value="repair">修复 (repair)</option><option value="needs_human">转人工 (needs_human)</option>
            </select>
          </Field>
        </div>
      </details>)}

      <div className="dialog__foot">
        <Button ghost onClick={close}>取消</Button>
        <Button primary type="submit" disabled={!selected.length || action.busy}>
          {action.busy ? "正在沙盒回放…" : `运行回放（已选 ${selected.length}/8 条）`}
        </Button>
      </div>
    </form>
  </Modal>;
}

function ReplayReports({ replays }: { replays: Replay[] }) {
  if (!replays.length) return <p className="muted">尚未运行任何回放测试。请点击上方「运行隔离对照回放」。</p>;
  return <div className="stack">
    {replays.slice().reverse().map(report => <details key={report.replay_id} open={replays.length <= 2}>
      <summary>
        {datasetName(report.dataset)} · {modeName(report.mode)}{report.live ? " · 真实模型" : ""}
        {" · "}{report.passed ? "✓ 通过" : "✗ 未通过"} · 解决 {report.evidence_gaps_resolved} 个证据缺口
      </summary>
      <div className="stack">
        <div className="table">
          <Grid columns="1fr 100px 100px" header><span>对比指标</span><span>现行策略</span><span>候选策略</span></Grid>
          {Object.entries(report.metrics.before).map(([key, before]) => <Grid key={key} columns="1fr 100px 100px">
            <span>{metrics[key] ?? key}</span>
            <span className="mono">{before == null ? "—" : Number(before.toFixed(3))}</span>
            <span className="mono">{report.metrics.after[key] == null ? "—" : Number(report.metrics.after[key]!.toFixed(3))}</span>
          </Grid>)}
        </div>
        {report.rows.map(row => <div className="block stack" key={caseKey(row)}>
          <div className="row-set">
            <span className="mono hint">J-{row.job_id.slice(0, 6)} · {row.occurrence_id.slice(0, 22)}</span>
            <span className="sp" />
            <Status value={row.passed ? "passed" : "failed"} pill />
          </div>
          <div className="form-grid">
            <div><span className="micro">改动前（现行）</span>
              <div className="text-block">判定：{row.before.disposition ?? "—"} · 越界 {row.before.outside_scope ?? 0} 条</div>
              {row.before.display && <div className="hint text-block">{row.before.display}</div>}
              {!!row.before.evidence_ids?.length && <div className="mono hint">命中证据：{row.before.evidence_ids.join(", ")}</div>}
            </div>
            <div><span className="micro">改动后（候选）</span>
              <div className="text-block">判定：{row.after.disposition ?? "—"} · 越界 {row.after.outside_scope ?? 0} 条</div>
              {row.after.display && <div className="hint text-block">{row.after.display}</div>}
              {!!row.after.evidence_ids?.length && <div className="mono hint">命中证据：{row.after.evidence_ids.join(", ")}</div>}
            </div>
          </div>
        </div>)}
      </div>
    </details>)}
  </div>;
}

function summarizeChanges(candidate: Candidate) {
  const base = candidate.base.spec;
  const spec = candidate.spec;
  const items: string[] = [];
  if (JSON.stringify(base.aliases) !== JSON.stringify(spec.aliases)) {
    const pairs = Object.entries(spec.aliases).map(([k, v]) => `${k} → ${v}`);
    items.push(`实体别名扩展（${pairs.join("；") || "已清空"}）`);
  }
  if (base.handoff_template !== spec.handoff_template) items.push(`转人工模板：${spec.handoff_template}`);
  if (base.lookup_results !== spec.lookup_results) items.push(`查证返回条数上限：${base.lookup_results} → ${spec.lookup_results}`);
  if (JSON.stringify(base.skill_issues) !== JSON.stringify(spec.skill_issues)) {
    items.push(`启用场景：${spec.skill_issues.map(s => skills[s] ?? s).join("、") || "无"}`);
  }
  if (JSON.stringify(base.prompts) !== JSON.stringify(spec.prompts)) items.push("审校查证追加指令已更新");
  if (base.memory_plan_id !== spec.memory_plan_id && spec.memory_plan_id) items.push(`挂载术语方案 ${spec.memory_plan_id.slice(0, 8)}`);
  return items;
}

export function Evolution({ offline, naev = false, scope, refresh, changed, openJob }: {
  offline: boolean; naev?: boolean; scope?: string; refresh: number; changed: () => void; openJob: (id: string) => void;
}) {
  const [linked, setLinked] = useState<string[]>([]);
  const [candidateId, setCandidateId] = useState("");
  const [dialog, setDialog] = useState<"feedback" | "candidate" | "replay" | "publish" | "retain" | "rollback">();
  const feedback = useQuery<{ feedback: Feedback[]; patterns: { category: string; occurrences: number; alias?: Feedback["alias"]; feedback_ids: string[] }[] }>("/feedback", refresh);
  const policy = useQuery<Policy>("/policy", refresh);
  const effectiveCandidateId = candidateId || policy.data?.candidates[0]?.candidate_id || "";
  const detail = useQuery<Details>(effectiveCandidateId ? `/policy/candidates/${effectiveCandidateId}` : null, refresh);
  const candidate = detail.data?.candidate;
  const active = policy.data?.active;
  const decision = dialog === "publish" || dialog === "retain" || dialog === "rollback" ? dialog : undefined;
  const action = useAction(changed);
  const close = () => { setDialog(undefined); action.clear(); };
  const allChecksPassed = !!detail.data && Object.values(detail.data.publication_checks).every(Boolean);

  return <>
    <Head title="进化中心 (RSI)" sub={active ? `当前线上生效策略：${active.release_id === "baseline" ? "默认基线 baseline" : active.release_id.slice(0, 12)}` : "安全策略自我迭代"}>
      <Button ghost onClick={() => setDialog("feedback")}>+ 登记一条反馈</Button>
      <Button primary disabled={!linked.length || !active} onClick={() => setDialog("candidate")}>
        {linked.length ? `用已选 ${linked.length} 条反馈创建策略候选` : "先勾选反馈再创建候选"}
      </Button>
    </Head>
    <ErrorNote error={policy.error || feedback.error || detail.error} />

    <div className="stage-strip" aria-label="RSI 闭环流程">
      <div className={`stage-step${feedback.data?.feedback.length ? " stage-step--done" : " stage-step--todo"}`}>
        <span className="stage-step__num">STEP 01</span>
        <b>收集人类纠偏反馈</b>
        <span className="hint">{feedback.data?.feedback.length ? `已累积 ${feedback.data.feedback.length} 条反馈` : "复核改译时填写原因或右上角手动登记"}</span>
      </div>
      <div className={`stage-step${policy.data?.candidates.length ? " stage-step--done" : linked.length ? " stage-step--todo" : ""}`}>
        <span className="stage-step__num">STEP 02</span>
        <b>拟定策略候选方案</b>
        <span className="hint">不改模型权重，仅调检索别名 / 审校提示 / 话术模板</span>
      </div>
      <div className={`stage-step${detail.data?.replays.length ? (allChecksPassed ? " stage-step--done" : " stage-step--warn") : ""}`}>
        <span className="stage-step__num">STEP 03</span>
        <b>沙盒双集对照回放</b>
        <span className="hint">验证痛点解决且不破坏独立历史样本（防回归）</span>
      </div>
      <div className={`stage-step${active && active.release_id !== "baseline" ? " stage-step--done" : ""}`}>
        <span className="stage-step__num">STEP 04</span>
        <b>门禁发布与随时回滚</b>
        <span className="hint">全部检查通过后正式发布，可一键回滚历史版本</span>
      </div>
    </div>

    <div className="split">
      <div className="split__main">
        <Section>第 1 步 · 人类反馈采集与共性模式</Section>
        {!feedback.data?.feedback.length && <Empty title="还没有任何进化反馈">
          当审校 Agent 漏查实体、误判或翻译口吻不佳时，点击右上角「+ 登记一条反馈」（或在任务复核修改原因里填写），即可开始积累反馈。
        </Empty>}
        {feedback.data?.patterns.map((pattern, i) => <div className="note note--ok" key={i}><div className="row-set">
          <b>发现重复失败模式：{categories[pattern.category] ?? pattern.category} · 在 {pattern.occurrences} 处反复出现
            {pattern.alias && `（${pattern.alias.query} → ${pattern.alias.canonical}）`}</b>
          <span className="sp" />
          <Button small onClick={() => setLinked(old => [...new Set([...old, ...pattern.feedback_ids])].slice(0, 24))}>一键关联此组反馈</Button>
        </div></div>)}

        {!!feedback.data?.feedback.length && <div className="stack">
          {feedback.data.feedback.slice().reverse().map(row => {
            const isChecked = linked.includes(row.feedback_id);
            return <div key={row.feedback_id} className="panel">
              <div className="row-set">
                <label className="check">
                  <input type="checkbox" checked={isChecked} disabled={!isChecked && linked.length >= 24}
                    onChange={e => setLinked(old => e.target.checked ? [...old, row.feedback_id] : old.filter(id => id !== row.feedback_id))} />
                  <b>{categories[row.category] ?? row.category}</b>
                </label>
                {row.alias && <span className="pill pill--info">别名：{row.alias.query} → {row.alias.canonical}</span>}
                <span className="sp" />
                <Button small ghost onClick={() => openJob(row.job_id)}>查看来源任务 J-{row.job_id.slice(0, 6)}</Button>
              </div>
              <div className="text-block">{row.reason}</div>
              {(row.before || row.after) && <div className="form-grid">
                <div className="source text-block">{row.before || "（无初稿）"}</div>
                <div className="target text-block">{row.after || "（无修改后文本）"}</div>
              </div>}
            </div>;
          })}
        </div>}

        {!!linked.length && <div className="selection-bar row-set">
          <span>已选中 <b>{linked.length}</b> 条反馈用于创建候选方案</span>
          <span className="sp" />
          <Button small ghost onClick={() => setLinked([])}>清空勾选</Button>
          <Button small primary onClick={() => setDialog("candidate")}>创建策略候选 →</Button>
        </div>}

        <Section>第 2 & 3 步 · 候选方案评估、回放测试与发布门禁</Section>
        {!policy.data?.candidates.length ? <Empty title="暂无策略候选方案">勾选上方至少 1 条反馈后，点击「创建策略候选」。</Empty> : <>
          <Field label="当前查看的策略候选">
            <select className="input" value={effectiveCandidateId} onChange={e => setCandidateId(e.target.value)}>
              {policy.data.candidates.map(item => <option key={item.candidate_id} value={item.candidate_id}>
                C-{item.candidate_id.slice(0, 8)} · {item.decision ? (item.decision.decision === "publish" ? "已正式发布" : "已归档留存") : "待测试评估"}
              </option>)}
            </select>
          </Field>

          {candidate && detail.data && <div className="panel stack">
            <div className="row-set">
              <b>改动内容摘要</b>
              <span className="sp" />
              {candidate.decision && <Status value={candidate.decision.decision === "publish" ? "published" : "retained"} pill />}
            </div>
            <ul className="guide-steps">
              {summarizeChanges(candidate).map((txt, i) => <li key={i}>• {txt}</li>)}
            </ul>

            <Section>上线前安全检查清单</Section>
            <div className="table">
              {Object.entries(detail.data.publication_checks).map(([key, passed]) => <Grid key={key} columns="1fr 110px">
                <span>{gates[key] ?? key}</span><Status value={passed ? "passed" : "pending"} />
              </Grid>)}
            </div>

            {candidate.decision ? (
              <div className="note">{candidate.decision.decision === "publish" ? "已正式发布生效" : "已归档保留原策略"} · {candidate.decision.reason}</div>
            ) : <div className="row-set">
              <Button primary={!allChecksPassed} onClick={() => setDialog("replay")}>运行隔离对照回放</Button>
              <Button primary={allChecksPassed} onClick={() => setDialog("publish")} disabled={!allChecksPassed}>
                {allChecksPassed ? "全部检查通过 · 正式发布" : "通过全部检查后才可发布"}
              </Button>
              <Button ghost onClick={() => setDialog("retain")}>放弃此候选（归档）</Button>
            </div>}

            <Section>回放测试报告（{detail.data.replays.length}）</Section>
            <ReplayReports replays={detail.data.replays} />
          </div>}
        </>}
      </div>

      <aside className="split__side">
        <div className="panel">
          <h4>当前生效策略详情</h4>
          <div className="kv"><span>版本编号</span><b>{active?.release_id === "baseline" ? "baseline" : active?.release_id.slice(0, 12)}</b></div>
          <div className="kv"><span>别名扩展规则</span><b>{Object.keys(active?.spec.aliases ?? {}).length} 条</b></div>
          <div className="kv"><span>审校返回上限</span><b>{active?.spec.lookup_results ?? 2} 条</b></div>
          <div className="kv"><span>转人工模板</span><b>{active?.spec.handoff_template ?? "{reason}"}</b></div>
        </div>

        <div className="panel">
          <h4>策略发布历史与回滚</h4>
          {policy.data?.releases.map(release => <div key={release.release_id} className="block stack">
            <div className="row-set">
              <span className="mono">{release.release_id === "baseline" ? "baseline" : release.release_id.slice(0, 10)}</span>
              {release.release_id === active?.release_id && <Status value="active" pill />}
            </div>
            <div className="hint">{release.reason || "初始默认基线策略"}</div>
            {release.release_id === active?.release_id && release.parent && (
              <Button small ghost onClick={() => setDialog("rollback")}>回滚至上一版本</Button>
            )}
          </div>)}
        </div>
      </aside>
    </div>

    {dialog === "feedback" && <FeedbackRegisterModal scope={scope} close={close} created={changed} />}
    {dialog === "candidate" && active && <CandidateForm active={active} linked={linked} naev={naev} close={close} created={id => {
      setCandidateId(id); setLinked([]); close(); changed();
    }} />}
    {dialog === "replay" && effectiveCandidateId && <ReplayForm id={effectiveCandidateId} offline={offline} close={close} changed={changed} />}
    {decision && <Modal title={{ rollback: "回滚策略版本", publish: "正式发布候选策略", retain: "归档此候选（保留现行策略）" }[decision]} close={close}>
      <ErrorNote error={action.error} />
      <form className="stack" onSubmit={event => {
        event.preventDefault(); const reason = new FormData(event.currentTarget).get("reason");
        void action.run(async () => {
          if (decision === "rollback") await json(`/policy/releases/${active?.release_id}/rollback`, post({ reason }));
          else await json(`/policy/candidates/${effectiveCandidateId}/decide`, post({ reason, decision }));
          close();
        });
      }}>
        <Field label="操作说明"><textarea className="textarea" name="reason" maxLength={5000} required
          placeholder="例如：独立验证与反馈回放全部通过，发布别名修正规则" /></Field>
        <div className="dialog__foot">
          <Button ghost onClick={close}>取消</Button>
          <Button primary type="submit" disabled={action.busy}>确认执行</Button>
        </div>
      </form>
    </Modal>}
  </>;
}
