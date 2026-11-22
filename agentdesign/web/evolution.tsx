import { useState } from "react";
import { type JobPage, intents, json, params, post, terminal, useQuery } from "./api";
import { type PlanSummary } from "./glossary";
import { Button, Empty, ErrorNote, Field, Grid, Head, Modal, Section, Status, useAction } from "./ui";

type Ref = { job_id: string; step: number; occurrence_id: string };
type Feedback = Ref & {
  feedback_id: string; reason: string; category: string; before?: string; after?: string;
  alias?: { query: string; canonical: string };
};
type Spec = { prompts: Record<string, string>; handoff_template: string; aliases: Record<string, string>;
  lookup_results: number; skill_issues: string[]; memory_plan_id: string | null };
type Release = { release_id: string; parent: string | null; reason?: string; components: Record<string, string>; spec: Spec };
type Candidate = { candidate_id: string; base: Release; spec: Spec; components: Record<string, string>;
  decision?: { decision: "publish" | "retain"; reason: string } };
type ReplayCase = Ref & { source: string; file?: string };
type Arm = Record<string, unknown>;
type Replay = { replay_id: string; dataset: string; mode: string; live: boolean; passed: boolean; evidence_gaps_resolved: number;
  metrics: { before: Record<string, number | null>; after: Record<string, number | null> };
  rows: (Ref & { before: Arm; after: Arm; passed: boolean })[] };
type Policy = { active: Release; releases: Release[]; candidates: Candidate[] };
type Details = { candidate: Candidate; replays: Replay[]; publication_checks: Record<string, boolean> };
const categories: Record<string, string> = { correction: "译文纠偏", retrieval: "检索遗漏", prompt: "提示指令", memory: "记忆问题", skill: "查证流程", harness: "工具调用" };
const gates: Record<string, string> = {
  policy_base_changed: "候选基于当前策略",
  record_replay_and_held_out_validation_required: "反馈案例与独立验证均通过历史回放",
  strategy_replay_and_held_out_validation_required: "两个验证集均通过策略对照",
  prompt_replay_needs_expected_verdicts: "真实审校回放已标注预期结论",
  retrieval_change_needs_observed_evidence_improvement: "证据缺口有可观测改善",
  memory_plan_requires_current_p4_approval: "术语方案已批准且仍生效",
};
const skills = { ambiguity: "歧义", missing_evidence: "证据缺失", terminology: "术语核对", context: "上下文缺失" };
const componentNames: Record<string, string> = { prompt: "提示", harness: "工具", skill: "技能", memory: "记忆" };
const metrics: Record<string, string> = {
  cases: "案例数", evidence_hit_rate: "证据命中率", outside_scope: "越界引用", hard_rule_violations: "硬规则违规",
  unsafe_commits: "不安全提交", human_rate: "转人工率", calls: "模型调用", tokens: "消耗篇幅", duration_ms: "耗时（毫秒）",
};
const datasetName = (value: string) => value === "feedback" ? "反馈案例" : "独立验证";
const modeName = (value: string) => value === "record" ? "历史回放" : "策略对照";
const caseKey = (ref: Ref) => `${ref.job_id}:${ref.step}:${ref.occurrence_id}`;

function CandidateForm({ active, linked, close, created }: {
  active: Release; linked: string[]; close: () => void; created: (id: string) => void;
}) {
  const plans = useQuery<{ plans: PlanSummary[] }>("/maintenance");
  const action = useAction();
  const [manual, setManual] = useState(false);
  return <Modal title="创建策略候选" close={close}><ErrorNote error={action.error || plans.error} />
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
            skill_issues: data.getAll("issues"), memory_plan_id: data.get("plan") || null,
          },
        }));
        created(candidate.candidate_id);
      });
    }}>
      <p>关联 {linked.length} 条反馈 · 基线 {active.release_id.slice(0, 8)}</p>
      <Field label="审校查证补充指令"><textarea className="textarea" name="prompt" maxLength={4000} defaultValue={active.spec.prompts.reviewer_source} /></Field>
      <Field label="转人工话术（保留 {reason}）"><input className="input" name="handoff" maxLength={500}
        defaultValue={active.spec.handoff_template} required /></Field>
      <label className="check"><input type="checkbox" checked={manual} onChange={e => setManual(e.target.checked)} />手动补充别名映射</label>
      {manual ? <Field label="每行一组别名，使用 → 连接"><textarea className="textarea" name="aliases"
        onChange={e => e.target.setCustomValidity("")} /></Field> : <p>从关联反馈提取重复出现且无冲突的别名。</p>}
      <Field label="单次工具结果上限"><select className="input" name="limit" defaultValue={active.spec.lookup_results}><option>1</option><option>2</option></select></Field>
      <div className="field"><span className="label">启用查证扩展的场景</span><div className="row-set">
        {Object.entries(skills).map(([key, title]) => <label key={key} className="check">
          <input type="checkbox" name="issues" value={key} defaultChecked={active.spec.skill_issues.includes(key)} />{title}
        </label>)}</div></div>
      <Field label="沙盒挂载术语方案"><select className="input" name="plan" defaultValue={active.spec.memory_plan_id ?? ""}>
        <option value="">无</option>{plans.data?.plans.filter(plan => plan.status === "preview" || plan.plan_id === active.spec.memory_plan_id)
          .map(plan => <option key={plan.plan_id} value={plan.plan_id}>{plan.rule.source} → {plan.rule.target}</option>)}
      </select></Field>
      <div className="dialog__foot"><Button primary type="submit" disabled={action.busy}>保存候选</Button></div>
    </form>
  </Modal>;
}

function ReplayForm({ id, offline, close, changed }: { id: string; offline: boolean; close: () => void; changed: () => void }) {
  const [mode, setMode] = useState("record");
  const [dataset, setDataset] = useState("feedback");
  const [jobId, setJobId] = useState("");
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<ReplayCase[]>([]);
  const jobs = useQuery<JobPage>(dataset === "validation" ? `/jobs?limit=50&offset=${offset}` : null);
  const cases = useQuery<{ cases: ReplayCase[] }>(dataset === "feedback" || jobId ?
    `/policy/candidates/${id}/cases?${params({ dataset, job_id: jobId })}` : null);
  const action = useAction(changed);
  return <Modal title="隔离回放" close={close}><ErrorNote error={jobs.error || cases.error || action.error} />
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
        <Field label="回放模式"><select className="input" value={mode} onChange={e => setMode(e.target.value)}>
          <option value="record">历史回放</option><option value="strategy">策略对照</option>
        </select></Field>
        <Field label="数据集"><select className="input" value={dataset} onChange={e => { setDataset(e.target.value); setSelected([]); }}>
          <option value="feedback">反馈案例</option><option value="validation">独立验证</option>
        </select></Field>
      </div>
      {mode === "strategy" && <label className="check"><input type="checkbox" name="live" disabled={offline} />真实模型复核</label>}
      {dataset === "validation" && <><Field label="历史任务"><select className="input" value={jobId} onChange={e => setJobId(e.target.value)}>
        <option value="">选择一个任务</option>{jobs.data?.jobs.filter(job => terminal.has(job.status) && job.source_kind !== "graph_build").map(job =>
          <option key={job.job_id} value={job.job_id}>J-{job.job_id.slice(0, 6)} · {job.title || job.intents.map(intent => intents[intent]).join(" → ")}</option>)}</select></Field>
        <div className="row-set"><Button ghost small disabled={!offset} onClick={() => { setOffset(offset - 50); setJobId(""); }}>上一页任务</Button>
          <Button ghost small disabled={offset + 50 >= (jobs.data?.total ?? 0)} onClick={() => { setOffset(offset + 50); setJobId(""); }}>下一页任务</Button></div></>}
      {cases.data && !cases.data.cases.length && <Empty title="没有可用于这个验证集的审校轨迹" />}
      <div className="scroll-list">{cases.data?.cases.map(item => {
        const checked = selected.some(row => caseKey(row) === caseKey(item));
        return <label className="check text-block" key={caseKey(item)}><input type="checkbox" checked={checked}
          disabled={!checked && selected.length >= 8} onChange={e => setSelected(old => e.target.checked ? [...old, item] :
            old.filter(row => caseKey(row) !== caseKey(item)))} /><span>{item.source} <small className="mono">{item.file}</small></span></label>;
      })}</div>
      {selected.map(item => <details key={caseKey(item)}><summary>预期结果 · {item.source.slice(0, 72)}</summary>
        <div className="stack">
          <Field label="期望命中的证据 ID（最多 6 个，使用逗号分隔）"><input className="input" name={"evidence-" + caseKey(item)} /></Field>
          <Field label="预期审校结论"><select className="input" name={"verdict-" + caseKey(item)}>
            <option value="">无指定</option><option value="pass">通过</option><option value="repair">修复</option><option value="needs_human">转人工</option>
          </select></Field>
          <Button small ghost onClick={() => setSelected(old => old.filter(row => caseKey(row) !== caseKey(item)))}>移除此案例</Button>
        </div>
      </details>)}
      <div className="dialog__foot"><Button primary type="submit" disabled={!selected.length || action.busy}>
        {action.busy ? "正在执行回放…" : `运行回放（${selected.length}/8）`}
      </Button></div>
    </form>
  </Modal>;
}

function ReplayReports({ replays }: { replays: Replay[] }) {
  return <>{replays.slice().reverse().map(report => <details key={report.replay_id}>
    <summary>{datasetName(report.dataset)} · {modeName(report.mode)}{report.live ? " · 真实模型" : ""}
      {" · "}{report.passed ? "通过" : "待检查"} · 解决 {report.evidence_gaps_resolved} 个证据缺口</summary>
    <div className="stack">
      <Grid columns="1fr 90px 90px" header><span>指标</span><span>现行</span><span>候选</span></Grid>
      {Object.entries(report.metrics.before).map(([key, before]) => <Grid key={key} columns="1fr 90px 90px">
        <span>{metrics[key] ?? key}</span><span className="mono">{before == null ? "—" : Number(before.toFixed(3))}</span>
        <span className="mono">{report.metrics.after[key] == null ? "—" : Number(report.metrics.after[key]!.toFixed(3))}</span>
      </Grid>)}
      {report.rows.map(row => <details key={caseKey(row)}><summary>J-{row.job_id.slice(0, 6)} · {row.occurrence_id.slice(0, 24)} · {row.passed ? "通过" : "待检查"}</summary>
        <div className="form-grid"><pre className="text-block">{JSON.stringify(row.before, null, 2)}</pre>
          <pre className="text-block">{JSON.stringify(row.after, null, 2)}</pre></div></details>)}
    </div>
  </details>)}</>;
}

export function Evolution({ offline, refresh, changed, openJob }: {
  offline: boolean; refresh: number; changed: () => void; openJob: (id: string) => void;
}) {
  const [linked, setLinked] = useState<string[]>([]);
  const [candidateId, setCandidateId] = useState("");
  const [dialog, setDialog] = useState<"candidate" | "replay" | "publish" | "retain" | "rollback">();
  const feedback = useQuery<{ feedback: Feedback[]; patterns: { category: string; occurrences: number; alias?: Feedback["alias"]; feedback_ids: string[] }[] }>("/feedback", refresh);
  const policy = useQuery<Policy>("/policy", refresh);
  const detail = useQuery<Details>(candidateId ? `/policy/candidates/${candidateId}` : null, refresh);
  const candidate = detail.data?.candidate;
  const active = policy.data?.active;
  const decision = dialog === "publish" || dialog === "retain" || dialog === "rollback" ? dialog : undefined;
  const action = useAction(changed);
  const close = () => { setDialog(undefined); action.clear(); };
  return <>
    <Head title="进化中心" sub={active && `生效策略 · ${active.release_id.slice(0, 12)}`}>
      <Button primary disabled={!linked.length || !active} onClick={() => setDialog("candidate")}>创建策略候选</Button>
    </Head>
    <ErrorNote error={policy.error || feedback.error || detail.error} />
    <Section>编辑反馈</Section>
    {!feedback.data?.feedback.length && <Empty title="还没有反馈">在复核时填写改译原因，或打开历史条目登记问题。</Empty>}
    {feedback.data?.patterns.map((pattern, i) => <div className="note" key={i}><div className="row-set">
      <span>{categories[pattern.category]} · {pattern.occurrences} 个位置
        {pattern.alias && ` · ${pattern.alias.query} → ${pattern.alias.canonical}`}</span>
      <Button small ghost onClick={() => setLinked(old => [...new Set([...old, ...pattern.feedback_ids])].slice(0, 24))}>选择此组</Button>
    </div></div>)}
    {!!feedback.data?.feedback.length && <div className="scroll-list">
      {feedback.data.feedback.slice().reverse().map(row => <details key={row.feedback_id}>
        <summary>{categories[row.category] ?? row.category} · {row.reason.slice(0, 95)}</summary>
        <div className="stack">
          <label className="check"><input type="checkbox" checked={linked.includes(row.feedback_id)}
            disabled={!linked.includes(row.feedback_id) && linked.length >= 24} onChange={e => setLinked(old =>
              e.target.checked ? [...old, row.feedback_id] : old.filter(id => id !== row.feedback_id))} />关联候选</label>
          <p className="text-block">{row.reason}</p>{row.alias && <p>{row.alias.query} → {row.alias.canonical}</p>}
          {(row.before || row.after) && <div className="form-grid"><div className="source text-block">{row.before}</div><div className="target text-block">{row.after}</div></div>}
          <div><Button small ghost onClick={() => openJob(row.job_id)}>打开来源任务</Button></div>
        </div>
      </details>)}
    </div>}
    {!!linked.length && <div className="row-set">已关联 {linked.length}/24 条反馈 <Button ghost small onClick={() => setLinked([])}>清空</Button></div>}
    <Section>候选与发布检查</Section>
    <Field label="策略候选"><select className="input" value={candidateId} onChange={e => setCandidateId(e.target.value)}>
      <option value="">选择候选</option>{policy.data?.candidates.map(item => <option key={item.candidate_id} value={item.candidate_id}>
        {item.candidate_id.slice(0, 12)} · {item.decision ? item.decision.decision === "publish" ? "已发布" : "已归档" : "待评估"}</option>)}
    </select></Field>
    {candidate && detail.data && <>
      <div className="row-set">{Object.entries(candidate.components).map(([key, value]) => <span key={key} className="mono muted">
        {componentNames[key]} {value.slice(0, 8)}{value === candidate.base.components[key] ? "" : "（已修改）"}
      </span>)}</div>
      {Object.entries(detail.data.publication_checks).map(([key, passed]) => <Grid key={key} columns="1fr 100px">
        <span>{gates[key]}</span><Status value={passed ? "passed" : "pending"} />
      </Grid>)}
      {candidate.decision ? <p>{candidate.decision.decision === "publish" ? "已发布" : "已归档"} · {candidate.decision.reason}</p> :
        <div className="row-set"><Button onClick={() => setDialog("replay")}>运行隔离回放</Button>
          <Button onClick={() => setDialog("publish")} disabled={!Object.values(detail.data.publication_checks).every(Boolean)}>正式发布</Button>
          <Button ghost onClick={() => setDialog("retain")}>保留现行策略</Button></div>}
      <ReplayReports replays={detail.data.replays} />
    </>}
    <Section>发布记录</Section>
    {policy.data?.releases.map(release => <Grid key={release.release_id} columns="132px minmax(110px,1fr) 90px 125px">
      <span className="mono" title={release.release_id}>{release.release_id.slice(0, 12)}</span>
      <span className="clip" title={release.reason}>{release.reason || "初始策略"}</span>
      {release.release_id === active?.release_id ? <Status value="active" /> : <span />}
      {release.release_id === active?.release_id && release.parent && <Button small ghost onClick={() => setDialog("rollback")}>回滚至上一版</Button>}
    </Grid>)}
    {dialog === "candidate" && active && <CandidateForm active={active} linked={linked} close={close} created={id => {
      setCandidateId(id); setLinked([]); close(); changed();
    }} />}
    {dialog === "replay" && <ReplayForm id={candidateId} offline={offline} close={close} changed={changed} />}
    {decision && <Modal title={{ rollback: "回滚策略", publish: "发布候选策略", retain: "保留现行策略" }[decision]} close={close}>
      <ErrorNote error={action.error} />
      <form className="stack" onSubmit={event => {
        event.preventDefault(); const reason = new FormData(event.currentTarget).get("reason");
        void action.run(async () => {
          if (decision === "rollback") await json(`/policy/releases/${active?.release_id}/rollback`, post({ reason }));
          else await json(`/policy/candidates/${candidateId}/decide`, post({ reason, decision }));
          close();
        });
      }}>
        <Field label="处理原因"><textarea className="textarea" name="reason" maxLength={5000} required /></Field>
        <div className="dialog__foot"><Button primary type="submit" disabled={action.busy}>确认</Button></div>
      </form>
    </Modal>}
  </>;
}
