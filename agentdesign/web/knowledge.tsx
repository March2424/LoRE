import { useState } from "react";
import { type Location, type Project, type Resource, type Selection, json, locationText, params, post, roles, useQuery } from "./api";
import { Button, Empty, ErrorNote, Field, Grid, Head, Modal, Pager, Section, Status, useAction } from "./ui";

type Profile = { profile_id: string; name: string; model: string; base_url: string };
type Role = keyof typeof roles;
export function Models({ project, offline, changed }: { project: Project; offline: boolean; changed: () => void }) {
  const [refresh, setRefresh] = useState(0);
  const [role, setRole] = useState<Role>("reviewer");
  const [probe, setProbe] = useState<{ profile: string; passed: boolean; calls: number; reason: string | null }>();
  const [saved, setSaved] = useState(false);
  const profiles = useQuery<{ profiles: Profile[] }>("/model-profiles", refresh);
  const action = useAction(() => setRefresh(n => n + 1));
  return <>
    <Head title="模型设置" sub={project.name} />
    <ErrorNote error={action.error || profiles.error} />
    <Section>按环节绑定</Section>
    <form className="stack" key={JSON.stringify([project.models, profiles.data?.profiles.map(profile => profile.profile_id)])} onSubmit={event => {
      event.preventDefault();
      const form = new FormData(event.currentTarget);
      void action.run(async () => {
        await json(`/projects/${project.project_id}/model-roles`, post({
          roles: Object.fromEntries([...form.entries()].filter(([, value]) => !!value)),
        }, "PUT")); setSaved(true); changed();
      });
    }}>
      <div className="form-grid">
        {Object.entries(roles).map(([key, title]) => <Field key={key} label={title}>
          <select name={key} className="input" defaultValue={project.models[key] ?? ""} onChange={() => setSaved(false)}>
            <option value="">工作台默认模型</option>
            {profiles.data?.profiles.map(profile => <option key={profile.profile_id} value={profile.profile_id}>{profile.name}</option>)}
          </select>
        </Field>)}
      </div>
      <div className="row-set"><Button primary type="submit" disabled={action.busy || !profiles.data}>保存绑定</Button>
        {saved && <Status value="completed" />}</div>
    </form>
    <Section>模型连接</Section>
    <div className="row-set"><Field label="测试环节"><select className="input" value={role} onChange={e => setRole(e.target.value as Role)}>
      {Object.entries(roles).map(([key, title]) => <option key={key} value={key}>{title}</option>)}
    </select></Field>{offline && <p className="muted">离线模式中可登记模型与绑定，连接测试需要启动在线服务。</p>}</div>
    {profiles.data?.profiles.map(profile => <div className="stack" key={profile.profile_id}>
      <Grid columns="1fr 1fr 1fr 100px"><b>{profile.name}</b><span className="mono clip">{profile.model}</span>
        <span className="mono clip" title={profile.base_url}>{profile.base_url}</span>
        <Button small ghost disabled={action.busy || offline} onClick={() => void action.run(async () => {
          setProbe(undefined);
          const result = await json<Omit<NonNullable<typeof probe>, "profile">>(`/model-profiles/${profile.profile_id}/probe`, post({ role }));
          setProbe({ ...result, profile: profile.profile_id });
        })}>测试可用性</Button>
      </Grid>
      {probe?.profile === profile.profile_id && <div className="row-set"><Status value={probe.passed ? "passed" : "failed"} />
        <span className="mono muted">{probe.calls} 次调用</span><ErrorNote reason={probe.reason} /></div>}
    </div>)}
    <details><summary>登记模型连接</summary>
      <form className="stack" onSubmit={event => {
        event.preventDefault();
        const form = event.currentTarget;
        const fields = new FormData(form);
        void action.run(async () => {
          await json("/model-profiles", post({
            name: fields.get("name"), model: fields.get("model"), base_url: fields.get("url"),
            api_key_env: String(fields.get("key")).trim() || undefined, timeout_seconds: Number(fields.get("timeout")),
          }));
          form.reset();
        });
      }}>
        <div className="form-grid">
          <Field label="显示名称"><input className="input" name="name" required maxLength={120} /></Field>
          <Field label="模型标识"><input className="input" name="model" required maxLength={200} /></Field>
          <Field label="Responses 兼容地址"><input className="input" type="url" name="url" required /></Field>
          <Field label="密钥环境变量名"><input className="input" name="key" pattern="[A-Z][A-Z0-9_]{1,80}" placeholder="WORKBENCH_MODEL_EXAMPLE_KEY" /></Field>
          <Field label="超时（秒）"><input className="input" type="number" min="2" max="360" name="timeout" defaultValue="75" required /></Field>
        </div>
        <div><Button type="submit" disabled={action.busy}>登记连接</Button></div>
      </form>
    </details>
  </>;
}

type Ref = { id: string; version: number };
type Evidence = { occurrence_id: string; source_version: number; quote: string; location?: Location };
type GraphItem = {
  id: string; version: number; kind?: string; relation?: string; valid: boolean; attributes: Record<string, unknown>;
  source?: Ref; target?: Ref; evidence: Evidence[];
};
type Graph = { total: number; graph_snapshot: string; nodes: GraphItem[]; edges: GraphItem[] };
type Memory = { record_id: string; version: number; kind: string; content: Record<string, unknown>; evidence: Evidence[] };
const memoryKinds: Record<string, string> = { term: "术语", style: "角色笔记", translation: "译例", summary: "摘要" };

function Build({ project, offline, close, openJob }: {
  project: Project; offline: boolean; close: () => void; openJob: (id: string) => void;
}) {
  const [extract, setExtract] = useState(!offline);
  const action = useAction();
  return <Modal title="构建项目图谱" close={close}>
    <ErrorNote error={action.error} />
    <form className="stack" onSubmit={event => {
      event.preventDefault();
      const data = new FormData(event.currentTarget);
      void action.run(async () => {
        const job = await json<{ job_id: string }>(`/projects/${project.project_id}/graph/build`, post({
          extract, rebuild: data.has("rebuild"), ...(extract ? {
            batch_size: Number(data.get("size")), max_calls: Number(data.get("limit")),
            max_input_tokens: Number(data.get("input")), max_output_tokens: Number(data.get("output")),
          } : {}),
        }));
        openJob(job.job_id);
      });
    }}>
      <label className="check"><input type="checkbox" checked={extract} onChange={e => setExtract(e.target.checked)} />提取实体与关系</label>
      {extract ? <><label className="check"><input type="checkbox" name="rebuild" />全量重建</label><div className="form-grid">
        {([["size", "每批条数", 1, 36, 12], ["limit", "调用上限", 1, 5000, 32],
          ["input", "单批输入 token", 1000, 16000, 6000], ["output", "单批输出 token", 256, 8192, 2048]] as const)
          .map(([key, label, min, max, start]) => <Field label={label} key={key}>
            <input className="input" type="number" name={key} min={min} max={max} defaultValue={start} required />
          </Field>)}
      </div></> : <p>发布结构快照，模型调用为 0。</p>}
      <div className="dialog__foot"><Button primary type="submit" disabled={action.busy}>开始建图</Button></div>
    </form>
  </Modal>;
}

function DraftMemory({ project, selection, close, changed }: {
  project: Project; selection: Selection; close: () => void; changed: () => void;
}) {
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState("");
  const [resource, setResource] = useState<Resource>();
  const [kind, setKind] = useState("term");
  const base = `/projects/${project.project_id}`;
  const resources = useQuery<{ resources: Resource[] }>(base + `/resources?${params({ query: search, file: selection.file, limit: 100 })}`);
  const ordered = (resources.data?.resources ?? []).slice().sort((a, b) =>
    Number(selection.ids.includes(b.occurrence_id)) - Number(selection.ids.includes(a.occurrence_id)));
  const action = useAction();
  return <Modal title="起草项目记忆" close={close}>
    <ErrorNote error={action.error || resources.error} />
    <form className="row-set" onSubmit={e => { e.preventDefault(); setSearch(query.trim()); }}>
      <input className="input" aria-label="查找原文依据" value={query} onChange={e => setQuery(e.target.value)} placeholder="按原文查找依据" />
      <Button type="submit">查找</Button>
    </form>
    <form className="stack" onSubmit={event => {
      event.preventDefault();
      if (!resource) return;
      const fields = new FormData(event.currentTarget);
      void action.run(async () => {
        await json(base + "/memory/drafts", post({
          kind, occurrence_id: resource.occurrence_id, source_version: resource.source_version, quote: fields.get("quote"),
          ...(kind === "term" ? { source: fields.get("source"), target: fields.get("target"), mandatory: fields.has("mandatory") }
            : { note: fields.get("note") }),
        })); changed(); close();
      });
    }}>
      <Field label="记忆类型"><select className="input" value={kind} onChange={e => { setKind(e.target.value); setResource(undefined); }}>
        <option value="term">术语</option><option value="style">角色笔记</option>
      </select></Field>
      <Field label={kind === "term" ? "源条目" : "有说话人标注的源条目"}>
        <select className="input" required value={resource?.occurrence_id ?? ""} onChange={e => setResource(ordered.find(item => item.occurrence_id === e.target.value))}>
          <option value="">选择源条目</option>
          {resource && !ordered.some(item => item.occurrence_id === resource.occurrence_id) &&
            <option value={resource.occurrence_id}>{resource.label.slice(0, 100)}</option>}
          {ordered.filter(item => kind === "term" || item.metadata?.speaker_id).map(item => <option key={item.occurrence_id} value={item.occurrence_id}>
            {item.label.slice(0, 100)} · {locationText(item.location, item.file)}</option>)}
        </select>
      </Field>
      {resource && <div className="source text-block">{resource.label}</div>}
      <Field label="原文引文"><textarea key={resource?.occurrence_id} name="quote" className="textarea" required
        maxLength={2000} defaultValue={resource?.label.slice(0, 2000)} /></Field>
      {kind === "term" ? <><div className="form-grid"><Field label="原文术语"><input className="input" name="source" maxLength={2000} required /></Field>
        <Field label="规定译法"><input className="input" name="target" maxLength={2000} required /></Field></div>
        <label className="check"><input type="checkbox" name="mandatory" />强制使用此译法</label></> :
        <Field label="角色笔记"><textarea className="textarea" name="note" maxLength={2000} required /></Field>}
      <div className="dialog__foot"><Button primary type="submit" disabled={!resource || action.busy}>提交草稿</Button></div>
    </form>
  </Modal>;
}

export function Knowledge({ project, offline, refresh, changed, selection, openJob }: {
  project: Project; offline: boolean; refresh: number; changed: () => void; selection: Selection; openJob: (id: string) => void;
}) {
  const [tab, setTab] = useState("graph");
  const [dialog, setDialog] = useState<"build" | "draft">();
  const [query, setQuery] = useState("");
  const [revision, setRevision] = useState("");
  const [branch, setBranch] = useState("");
  const [node, setNode] = useState("");
  const [filters, setFilters] = useState({ query, snapshot_id: revision, branch_id: branch, node_id: node });
  const [graphItem, setGraphItem] = useState<GraphItem>();
  const [offset, setOffset] = useState(0);
  const [memorySearch, setMemorySearch] = useState("");
  const [memoryTab, setMemoryTab] = useState<"approved" | "drafts">("approved");
  const [memoryOffset, setMemoryOffset] = useState(0);
  const [decision, setDecision] = useState<{ record: Memory; action: "approve" | "retire" }>();
  const action = useAction(changed);
  const size = 25;
  const base = `/projects/${project.project_id}`;
  const graph = useQuery<Graph>(project.graph_snapshot && tab === "graph" ?
    base + `/graph?${params({ ...filters, offset, limit: size })}` : null, refresh);
  const memory = useQuery<{ approved: Memory[]; drafts: Memory[] }>(tab === "memory" ? base + "/memory" : null, refresh);
  function nodeLabel(item: GraphItem) {
    return String(item.attributes?.name || item.evidence[0]?.quote || item.id);
  }
  function referenceLabel(ref?: Ref) {
    const target = graph.data?.nodes.find(item => item.id === ref?.id && item.version === ref?.version);
    return target ? nodeLabel(target) : ref?.id;
  }
  const rows = (memory.data?.[memoryTab] ?? []).filter(item => JSON.stringify(item.content).toLowerCase().includes(memorySearch.toLowerCase()));
  return <>
    <Head title="图谱与记忆" sub={project.graph_ready ? `当前源版本 v${project.revision}` : "当前源版本等待发布图谱"}>
      {tab === "graph" ? <>{project.build_job_id && <Button ghost onClick={() => openJob(project.build_job_id!)}>查看建图任务</Button>}
        <Button primary disabled={!project.resources} onClick={() => setDialog("build")}>构建图谱</Button></> :
        <Button primary disabled={!project.resources} onClick={() => setDialog("draft")}>起草记忆</Button>}
    </Head>
    <div className="row-set"><Button small ghost aria-pressed={tab === "graph"} onClick={() => setTab("graph")}>图谱</Button>
      <Button small ghost aria-pressed={tab === "memory"} onClick={() => setTab("memory")}>项目记忆</Button></div>
    <ErrorNote error={graph.error || memory.error} />
    {tab === "graph" && <>
      {!project.graph_snapshot ? <Empty title="还没有已发布的图谱">导入源文件后，构建项目的第一份图谱。</Empty> : <>
        <form className="row-set" onSubmit={e => {
          e.preventDefault(); setOffset(0); setFilters({ query: query.trim(), snapshot_id: revision.trim(), branch_id: branch.trim(), node_id: node.trim() });
        }}>
          <input className="input" aria-label="图谱关键词" placeholder="原文或实体关键词" value={query} onChange={e => setQuery(e.target.value)} />
          <input className="input" aria-label="快照 ID" placeholder="当前快照" value={revision} onChange={e => setRevision(e.target.value)} />
          <input className="input" aria-label="节点 ID" placeholder="节点 ID" value={node} onChange={e => setNode(e.target.value)} />
          <input className="input" aria-label="剧情分支" placeholder="剧情分支" value={branch} onChange={e => setBranch(e.target.value)} />
          <Button type="submit">查询图谱</Button>
        </form>
        {graph.data && <>
          <div className="hint text-block">{graph.data.graph_snapshot}</div><Section>节点</Section>
          {graph.data.nodes.map(item => <Grid key={item.id} columns="minmax(100px,1fr) 70px 70px 58px">
            <span className="clip" title={nodeLabel(item)}>{nodeLabel(item)}</span><span>{item.kind}</span>
            <Status value={item.valid ? "valid" : "invalid"} /><Button small ghost onClick={() => setGraphItem(item)}>详情</Button>
          </Grid>)}
          {!graph.data.nodes.length && <Empty title="没有匹配的节点" />}
          <Pager total={graph.data.total} offset={offset} size={size} setOffset={setOffset} />
          {!!graph.data.edges.length && <><Section>关系</Section>
            {graph.data.edges.map(item => <Grid key={item.id} columns="minmax(100px,1fr) 110px minmax(100px,1fr) 70px 58px">
              <span className="clip">{referenceLabel(item.source)}</span><span className="clip">{item.relation ?? item.kind}</span>
              <span className="clip">{referenceLabel(item.target)}</span>
              <Status value={item.valid ? "valid" : "invalid"} /><Button small ghost onClick={() => setGraphItem(item)}>详情</Button>
            </Grid>)}</>}
        </>}
      </>}
    </>}
    {tab === "memory" && <>
      <div className="row-set">
        {(["approved", "drafts"] as const).map(key => <Button small ghost key={key} aria-pressed={memoryTab === key}
          onClick={() => { setMemoryTab(key); setMemoryOffset(0); }}>{key === "approved" ? "已生效" : "待批准"}（{memory.data?.[key].length ?? 0}）</Button>)}
        <input className="input" aria-label="搜索记忆" placeholder="搜索记忆" value={memorySearch} onChange={e => { setMemorySearch(e.target.value); setMemoryOffset(0); }} />
      </div>
      {memory.data && !rows.length && <Empty title="没有匹配的记忆" />}
      {rows.slice(memoryOffset, memoryOffset + size).map(record => <details key={record.record_id + record.version}>
        <summary>{memoryKinds[record.kind] ?? record.kind} · {String(record.content.source ?? record.content.note ?? record.content.target ?? record.record_id).slice(0, 130)}</summary>
        <div className="stack">
          <div className="source text-block">{[record.content.source, record.content.target, record.content.note].filter(Boolean).join("\n")}</div>
          {record.evidence?.map((entry, i) => <p key={i}>{entry.quote} <span className="mono hint">{entry.occurrence_id}@{entry.source_version}</span></p>)}
          {(memoryTab === "drafts" || record.kind === "term" || record.kind === "style") &&
            <div><Button small onClick={() => setDecision({ record, action: memoryTab === "drafts" ? "approve" : "retire" })}>
              {memoryTab === "drafts" ? "批准生效" : "退役"}</Button></div>}
        </div>
      </details>)}
      {!!rows.length && <Pager total={rows.length} offset={memoryOffset} size={size} setOffset={setMemoryOffset} />}
    </>}
    {dialog === "build" && <Build project={project} offline={offline} close={() => setDialog(undefined)} openJob={openJob} />}
    {dialog === "draft" && <DraftMemory project={project} selection={selection} close={() => setDialog(undefined)} changed={changed} />}
    {graphItem && <Modal title="图谱来源" close={() => setGraphItem(undefined)}>
      <Status value={graphItem.valid ? "valid" : "invalid"} /><div className="mono text-block">{graphItem.id} · v{graphItem.version}</div>
      {graphItem.evidence.map((entry, i) => <div key={i} className="block"><div className="text-block">{entry.quote}</div>
        <p className="mono hint">{locationText(entry.location)} · 源 v{entry.source_version}</p></div>)}
    </Modal>}
    {decision && <Modal title={decision.action === "approve" ? "批准记忆" : "退役记忆"} close={() => { setDecision(undefined); action.clear(); }}>
      <ErrorNote error={action.error} />
      <form className="stack" onSubmit={event => {
        event.preventDefault(); const reason = new FormData(event.currentTarget).get("reason");
        void action.run(async () => {
          await json(`${base}/memory/${encodeURIComponent(decision.record.record_id)}/${decision.record.version}/${decision.action}`, post({ reason }));
          setDecision(undefined); setMemoryOffset(0);
        });
      }}><Field label="处理原因"><textarea className="textarea" name="reason" required maxLength={2000} /></Field>
        <div className="dialog__foot"><Button primary type="submit" disabled={action.busy}>确认</Button></div></form>
    </Modal>}
  </>;
}
