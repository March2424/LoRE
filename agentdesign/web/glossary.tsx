import { useState } from "react";
import { json, post, reasonText, terminal, useQuery } from "./api";
import { Button, ErrorNote, Field, Head, Section, Status, useAction } from "./ui";

type Scope = { file?: string; branch_id?: string; occurrence_ids?: string[] };
type Rule = { rule_id: string; source: string; target: string; version: number; scope: Scope; matched_occurrences: number };
export type PlanSummary = { plan_id: string; status: string; rule: Rule };
type Plan = PlanSummary & { job_id: string | null; job_status?: string; items: {
  occurrence_id: string; source: string; before_version: number; before?: string; after?: string; after_version?: number;
  proposed?: string; status: string; reason?: string; discovery: string[]; result?: string; result_reason?: string; model_calls?: number;
}[] };

export function Glossary({ selected, openJob, refresh, changed }: {
  selected: string[]; openJob: (id: string) => void; refresh: number; changed: () => void;
}) {
  const [ruleId, setRuleId] = useState("");
  const [source, setSource] = useState("");
  const [previous, setPrevious] = useState("");
  const [target, setTarget] = useState("");
  const [scope, setScope] = useState(selected.length ? "selected" : "project");
  const [planId, setPlanId] = useState("");
  const rules = useQuery<{ rules: Rule[] }>("/glossary", refresh);
  const plans = useQuery<{ plans: PlanSummary[] }>("/maintenance", refresh);
  const planResult = useQuery<Plan>(planId ? `/maintenance/${planId}` : null, refresh);
  const plan = planResult.data;
  const existing = rules.data?.rules.find(rule => rule.rule_id === ruleId);
  const action = useAction(changed);
  return <>
    <Head title="术语变更" sub="Naev 示范区 · 预演、应用与版本回滚" />
    <ErrorNote error={rules.error || plans.error || planResult.error || action.error} />
    <Section>预演变更</Section>
    <form className="stack" onSubmit={event => {
      event.preventDefault();
      const fields = new FormData(event.currentTarget);
      void action.run(async () => {
        const prepared = await json<Plan>("/maintenance/preview", post({
          rule_id: ruleId || undefined, source, target, previous_target: previous || undefined,
          scope: scope === "existing" ? existing?.scope : {
            occurrence_ids: scope === "selected" ? selected : undefined,
            file: String(fields.get("file") || "").trim() || undefined,
            branch_id: String(fields.get("branch") || "").trim() || undefined,
          },
        }));
        setPlanId(prepared.plan_id);
      });
    }}>
      <div className="form-grid">
        <Field label="已登记规则"><select className="input" value={ruleId} onChange={event => {
          const id = event.target.value;
          const rule = rules.data?.rules.find(item => item.rule_id === id);
          setRuleId(id); setSource(rule?.source ?? ""); setTarget(""); setPrevious(rule?.target ?? "");
          setScope(rule ? "existing" : selected.length ? "selected" : "project");
        }}><option value="">新规则</option>{rules.data?.rules.map(rule =>
          <option key={rule.rule_id} value={rule.rule_id}>{rule.source} → {rule.target} · v{rule.version}</option>)}</select></Field>
        <Field label="原文术语"><input className="input" required maxLength={250} value={source} onChange={e => setSource(e.target.value)} /></Field>
        <Field label="新译法"><input className="input" required maxLength={250} value={target} onChange={e => setTarget(e.target.value)} /></Field>
        <Field label="旧译法（可选）"><input className="input" maxLength={250} value={previous} onChange={e => setPrevious(e.target.value)} /></Field>
        <Field label="作用范围"><select className="input" value={scope} onChange={e => setScope(e.target.value)}>
          <option value="project">项目或指定文件</option><option value="selected">所选位置（{selected.length}）</option>
          {existing && <option value="existing">沿用已有范围</option>}</select></Field>
        {scope !== "existing" && <><Field label="文件（可选）"><input name="file" className="input" /></Field>
          <Field label="剧情分支（可选）"><input name="branch" className="input" /></Field></>}
      </div>
      <div><Button type="submit" primary={!plan} disabled={action.busy || (scope === "selected" && !selected.length)}>预览术语变更</Button></div>
    </form>
    <Section>历史方案</Section>
    <Field label="打开预演或变更记录"><select className="input" value={planId} onChange={e => setPlanId(e.target.value)}>
      <option value="">选择方案</option>{plans.data?.plans.map(item => <option key={item.plan_id} value={item.plan_id}>
        {item.rule.source} → {item.rule.target} · {item.plan_id.slice(0, 8)}</option>)}</select></Field>
    {plan && <>
      <div className="row-set"><b>{plan.rule.source} → {plan.rule.target}</b><Status value={plan.status} />
        <span>规则 v{plan.rule.version} · {plan.rule.matched_occurrences} 个源位置</span>
        {plan.job_status && <Status value={plan.job_status} />}
        {plan.status === "preview" && <Button primary disabled={action.busy} onClick={() => void action.run(async () => {
          const result = await json<{ job_id: string | null }>(`/maintenance/${planId}/apply`, post({}));
          if (result.job_id) openJob(result.job_id);
        })}>确认应用</Button>}
        {plan.job_id && <Button ghost onClick={() => openJob(plan.job_id!)}>打开维护任务</Button>}
        {plan.status === "applied" && (!plan.job_status || terminal.has(plan.job_status)) &&
          <Button ghost disabled={action.busy} onClick={() => void action.run(() => json(`/maintenance/${planId}/rollback`, post({})))}>回滚此次变更</Button>}
      </div>
      {plan.items.map(item => <details key={item.occurrence_id}>
        <summary>{item.source.slice(0, 100)} · {item.result ?? item.status}</summary>
        <div className="stack"><div className="source text-block">{item.source}</div>
          <div className="target text-block">v{item.before_version} {item.before} → {item.proposed ?? item.after ?? "等待审校或保持原样"}</div>
          <p>{reasonText(item.result_reason ?? item.reason)} <code>{item.result_reason ?? item.reason}</code></p>
          <p className="mono hint">{item.discovery.join(" / ")} · {item.model_calls ?? 0} 次调用</p>
        </div>
      </details>)}
    </>}
  </>;
}
