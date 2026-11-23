import { useEffect, useMemo, useState } from "react";
import { type Location, type Project, type ProjectNote, type Resource, type Selection, json, locationText, params, post, useQuery } from "./api";
import { Button, Empty, ErrorNote, Field, Grid, Head, Modal, Pager, Section, Status, useAction } from "./ui";

type Ref = { id: string; version: number };
type Evidence = { occurrence_id: string; source_version: number; quote: string; location?: Location };
type GraphItem = {
  id: string; version: number; kind?: string; relation?: string; valid: boolean; attributes: Record<string, unknown>;
  source?: Ref; target?: Ref; evidence: Evidence[];
};
type Graph = { total: number; graph_snapshot: string; nodes: GraphItem[]; edges: GraphItem[] };
type Memory = { record_id: string; version: number; kind: string; content: Record<string, unknown>; evidence: Evidence[] };
const memoryKinds: Record<string, string> = { term: "术语", style: "角色笔记", translation: "译例", summary: "摘要" };
const noteCategories: Record<ProjectNote["category"], string> = {
  background: "游戏世界观 / 背景",
  system_prompt: "System Prompt / 全局指令",
  style_guide: "统一文风 / 人称规范",
};

function Build({ project, offline, close, openJob }: {
  project: Project; offline: boolean; close: () => void; openJob: (id: string) => void;
}) {
  const [extract, setExtract] = useState(!offline);
  const [rebuild, setRebuild] = useState(false);
  const [maxItems, setMaxItems] = useState("");
  const [batchSize, setBatchSize] = useState(24);
  const [maxInput, setMaxInput] = useState(8000);
  const [maxOutput, setMaxOutput] = useState(12288);
  const pendingRows = rebuild ? (project.resources ?? 0) : (project.unextracted ?? project.resources ?? 0);
  const targetRows = maxItems.trim() ? Math.min(pendingRows, Math.max(1, Number(maxItems))) : pendingRows;
  const estimatedBatches = Math.max(1, Math.ceil(Math.max(1, targetRows) / Math.max(1, batchSize)));
  const [maxCalls, setMaxCalls] = useState(() => Math.max(16, estimatedBatches));
  const action = useAction();

  const applyPreset = (size: number, input: number, output: number) => {
    setBatchSize(size);
    setMaxInput(input);
    setMaxOutput(output);
    const est = Math.max(1, Math.ceil(Math.max(1, targetRows) / size));
    setMaxCalls(est);
  };

  return <Modal title="构建项目图谱" close={close}>
    <ErrorNote error={action.error} />
    <form className="stack" onSubmit={event => {
      event.preventDefault();
      void action.run(async () => {
        const parsedMaxItems = maxItems.trim() ? Number(maxItems) : undefined;
        const job = await json<{ job_id: string }>(`/projects/${project.project_id}/graph/build`, post({
          extract, rebuild, ...(extract ? {
            ...(parsedMaxItems ? { max_items: parsedMaxItems } : {}),
            batch_size: batchSize, max_calls: maxCalls,
            max_input_tokens: maxInput, max_output_tokens: maxOutput,
          } : {}),
        }));
        openJob(job.job_id);
      });
    }}>
      <label className="check"><input type="checkbox" checked={extract} onChange={e => setExtract(e.target.checked)} />使用模型提取实体与关系（extractor）</label>
      {extract ? <>
        <label className="check"><input type="checkbox" checked={rebuild} onChange={e => {
          setRebuild(e.target.checked);
        }} />全量重建（默认仅对新增或尚未抽取的条目增量抽取）</label>
        <div className="note">
          <b>支持部分构建与实时增量更新</b>
          待抽取条目共 <b>{pendingRows}</b> 条。可填写「本次最多构建条数」或设置较小的「调用上限 max_calls」分批构建。每完成 1 个批次都会实时更新到「图谱与记忆」。
        </div>
        <div className="row-set">
          <span className="micro">参数预设</span>
          <Button small ghost onClick={() => applyPreset(24, 8000, 12288)}>推荐均衡 (24条 / 12288)</Button>
          <Button small ghost onClick={() => applyPreset(8, 4500, 8192)}>稳健小批 · 本地模型 (8条)</Button>
          <Button small ghost onClick={() => { setMaxItems("30"); applyPreset(10, 6000, 12288); }}>快速试跑 (前 30 条)</Button>
        </div>
        <div className="form-grid">
          <Field label="本次最多构建条数（留空为全部）"><input className="input" type="number" min="1" max="50000" value={maxItems}
            placeholder={`全部待抽（${pendingRows} 条）`} onChange={e => setMaxItems(e.target.value)} /></Field>
          <Field label="每批条数 (1–64)"><input className="input" type="number" min="1" max="64" value={batchSize}
            onChange={e => setBatchSize(Number(e.target.value))} required /></Field>
          <Field label="调用上限 (max_calls)"><input className="input" type="number" min="1" max="5000" value={maxCalls}
            onChange={e => setMaxCalls(Number(e.target.value))} required /></Field>
          <Field label="单批输入 token 上限"><input className="input" type="number" min="2000" max="32768" value={maxInput}
            onChange={e => setMaxInput(Number(e.target.value))} required /></Field>
          <Field label="单批输出 token 上限"><input className="input" type="number" min="512" max="16384" value={maxOutput}
            onChange={e => setMaxOutput(Number(e.target.value))} required /></Field>
        </div>
        {maxCalls < estimatedBatches && <div className="note">已设调用上限为 <b>{maxCalls}</b> 批（全量预计 {estimatedBatches} 批）：任务会在完成 {maxCalls} 批后保留进度并即时更新图谱。</div>}
      </> : <p className="muted">仅发布基础结构快照，不调用模型（模型调用为 0）。</p>}
      <div className="dialog__foot">
        <Button ghost onClick={close}>取消</Button>
        <Button primary type="submit" disabled={action.busy}>{action.busy ? "正在创建建图任务…" : "开始建图"}</Button>
      </div>
    </form>
  </Modal>;
}

function ProjectNoteModal({ project, initial, close, changed }: {
  project: Project; initial?: ProjectNote; close: () => void; changed: () => void;
}) {
  const [category, setCategory] = useState<ProjectNote["category"]>(initial?.category ?? "background");
  const [title, setTitle] = useState(initial?.title ?? "");
  const [content, setContent] = useState(initial?.content ?? "");
  const [enabled, setEnabled] = useState(initial?.enabled ?? true);
  const action = useAction();
  const base = `/projects/${project.project_id}/notes`;

  return <Modal title={initial ? "编辑全局背景 / System Prompt 记忆" : "添加全局背景 / System Prompt 记忆"} close={close}>
    <ErrorNote error={action.error} />
    <div className="note">
      此类记忆<b>不绑定单条原文</b>，适用于游戏世界观设定、角色关系总览、System Prompt、人称与文风要求；启用后会自动注入本项目的翻译、审校与对话溯源。
    </div>
    <form className="stack" onSubmit={event => {
      event.preventDefault();
      void action.run(async () => {
        const payload = { category, title: title.trim(), content: content.trim(), enabled };
        if (initial) {
          await json(`${base}/${initial.note_id}`, post(payload, "PUT"));
        } else {
          await json(base, post(payload));
        }
        changed();
        close();
      });
    }}>
      <Field label="记忆类别">
        <select className="input" value={category} onChange={e => setCategory(e.target.value as ProjectNote["category"])}>
          <option value="background">游戏世界观 / 背景设定</option>
          <option value="system_prompt">System Prompt / 全局翻译指令</option>
          <option value="style_guide">统一文风 / 人称规范</option>
        </select>
      </Field>
      <Field label="标题（简述这条设定）">
        <input className="input" value={title} onChange={e => setTitle(e.target.value)}
          placeholder="例如：星海帝国背景设定 / 整体译文口吻 Prompt" required maxLength={120} autoFocus />
      </Field>
      <Field label="内容（越清晰具体越好）">
        <textarea className="textarea" rows={5} value={content} onChange={e => setContent(e.target.value)}
          placeholder="例如：本作背景为硬核太空歌剧；所有帝国军官自称「下官」或使用冷静克制的书面军事用语，切忌现代网络俚语。"
          required maxLength={8000} />
      </Field>
      <label className="check">
        <input type="checkbox" checked={enabled} onChange={e => setEnabled(e.target.checked)} />
        立即启用（翻译、审校与对话时自动注入）
      </label>
      <div className="dialog__foot">
        <Button ghost onClick={close}>取消</Button>
        <Button primary type="submit" disabled={action.busy || !title.trim() || !content.trim()}>
          {action.busy ? "正在保存…" : initial ? "保存修改" : "保存记忆"}
        </Button>
      </div>
    </form>
  </Modal>;
}

function DraftMemory({ project, selection, close, changed }: {
  project: Project; selection: Selection; close: () => void; changed: () => void;
}) {
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState("");
  const [resource, setResource] = useState<Resource>();
  const [kind, setKind] = useState<"term" | "style">("term");
  const [quote, setQuote] = useState("");
  const [editQuote, setEditQuote] = useState(false);
  const [sourceTerm, setSourceTerm] = useState("");
  const [targetTerm, setTargetTerm] = useState("");
  const [mandatory, setMandatory] = useState(true);
  const [noteText, setNoteText] = useState("");
  const base = `/projects/${project.project_id}`;

  useEffect(() => {
    const timer = setTimeout(() => setSearch(query.trim()), 180);
    return () => clearTimeout(timer);
  }, [query]);

  const resources = useQuery<{ resources: Resource[]; total: number }>(
    base + `/resources?${params({ query: search, file: selection.file, limit: 50 })}`,
  );
  const ordered = (resources.data?.resources ?? []).slice().sort((a, b) =>
    Number(selection.ids.includes(b.occurrence_id)) - Number(selection.ids.includes(a.occurrence_id)));
  const candidates = kind === "style" ? ordered.filter(item => !!item.metadata?.speaker_id) : ordered;
  const action = useAction();

  const pickResource = (item: Resource) => {
    setResource(item);
    setQuote(item.label.slice(0, 2000));
    setEditQuote(false);
  };

  const quickWords = resource
    ? [...new Set((resource.label.match(/[A-Za-z][A-Za-z0-9'-]{1,}|[\u4e00-\u9fa5]{2,8}/g) ?? []).slice(0, 12))]
    : [];
  const quoteMissingTerm = kind === "term" && !!sourceTerm.trim() && !quote.includes(sourceTerm.trim());

  return <Modal title="起草单句术语 / 角色记忆" close={close} className="context-graph-dialog">
    <ErrorNote error={action.error || resources.error} />
    <div className="stack">
      <div className="row-set">
        <span className="label">记忆类型</span>
        <Button small primary={kind === "term"} ghost={kind !== "term"} onClick={() => { setKind("term"); }}>术语对齐</Button>
        <Button small primary={kind === "style"} ghost={kind !== "style"} onClick={() => {
          setKind("style");
          if (resource && !resource.metadata?.speaker_id) setResource(undefined);
        }}>角色台词笔记（按说话人）</Button>
      </div>

      <form className="row-set" onSubmit={e => { e.preventDefault(); setSearch(query.trim()); }}>
        <input className="input" style={{ flex: 1 }} aria-label="实时搜索源条目" value={query}
          onChange={e => setQuery(e.target.value)}
          placeholder="输入原文关键词、Key、说话人或文件名实时检索（不用点查找即可自动筛选）…" autoFocus />
        {query && <Button small ghost onClick={() => { setQuery(""); setSearch(""); }}>清除</Button>}
        <Button small type="submit">立即搜</Button>
      </form>

      <div className="split">
        <div className="split__main" style={{ minHeight: 0 }}>
          <div className="row-set">
            <span className="micro">第 1 步 · 点击选择原文依据</span>
            <span className="sp" />
            <span className="hint">{resources.data ? `匹配 ${resources.data.total} 条（显示 ${candidates.length} 条${kind === "style" ? " · 仅含说话人标注" : ""}）` : "正在加载…"}</span>
          </div>
          <div className="scroll-list stack" style={{ maxHeight: 280, border: "1px solid var(--line)", padding: 8, background: "var(--bg)" }}>
            {!candidates.length ? <Empty title={search ? "没有匹配的源条目" : kind === "style" ? "当前范围内没有带说话人标注的条目" : "暂无源条目"}>
              {kind === "style" ? "提示：全局文风或无说话人设定请使用外面的「+ 添加背景 / System Prompt 记忆」。" : undefined}
            </Empty> : candidates.map(item => {
              const active = resource?.occurrence_id === item.occurrence_id;
              return <button key={item.occurrence_id} type="button" onClick={() => pickResource(item)}
                className={`ctx-ledger__card${active ? " ctx-ledger__card--active" : ""}`}>
                <div className="row-set">
                  {selection.ids.includes(item.occurrence_id) && <span className="pill pill--info">已勾选</span>}
                  {item.metadata?.speaker_id && <span className="pill pill--plain">说话人: {item.metadata.speaker_id}</span>}
                  <span className="mono hint">{locationText(item.location, item.file) || item.key || item.occurrence_id.slice(0, 12)}</span>
                </div>
                <div className="text-block" style={{ fontSize: "var(--fs-ui)", color: "var(--text)" }}>{item.label.slice(0, 180)}</div>
              </button>;
            })}
          </div>
        </div>

        <aside className="split__side" style={{ width: 340, flexBasis: 340 }}>
          <form className="panel stack" onSubmit={event => {
            event.preventDefault();
            if (!resource) return;
            void action.run(async () => {
              await json(base + "/memory/drafts", post({
                kind,
                occurrence_id: resource.occurrence_id,
                source_version: resource.source_version,
                quote: quote.trim(),
                ...(kind === "term"
                  ? { source: sourceTerm.trim(), target: targetTerm.trim(), mandatory }
                  : { note: noteText.trim() }),
              }));
              changed();
              close();
            });
          }}>
            <h4>第 2 步 · 填写记忆规则</h4>
            {!resource ? <p className="muted">请在左侧列表中点选一条源文本作为依据。</p> : <>
              <div className="source text-block" style={{ maxHeight: 110, overflow: "auto" }}>{resource.label}</div>
              {kind === "term" ? <>
                {!!quickWords.length && <div className="stack" style={{ gap: 4 }}>
                  <span className="micro">点击选词填入原文术语：</span>
                  <div className="row-set">
                    {quickWords.map(w => <Button key={w} small ghost onClick={() => setSourceTerm(w)}>{w}</Button>)}
                  </div>
                </div>}
                <Field label="原文术语">
                  <input className="input" value={sourceTerm} onChange={e => setSourceTerm(e.target.value)}
                    placeholder="点击上方词或直接输入原文词" maxLength={2000} required />
                </Field>
                {quoteMissingTerm && <div className="note note--warn">「{sourceTerm}」未出现在引文原文中，提交时会被拦截。</div>}
                <Field label="规定译法">
                  <input className="input" value={targetTerm} onChange={e => setTargetTerm(e.target.value)}
                    placeholder="填写希望固定使用的译法" maxLength={2000} required />
                </Field>
                <label className="check">
                  <input type="checkbox" checked={mandatory} onChange={e => setMandatory(e.target.checked)} />
                  强制使用此译法（违反时自动触发审校/拦截）
                </label>
              </> : <>
                <div className="kv"><span>绑定说话人</span><b>{resource.metadata?.speaker_id || "未标注"}</b></div>
                <Field label="角色口吻 / 翻译笔记">
                  <textarea className="textarea" rows={3} value={noteText} onChange={e => setNoteText(e.target.value)}
                    placeholder="例如：该角色说话骄傲冷漠，多用反问句" maxLength={2000} required />
                </Field>
              </>}

              <div>
                <Button small ghost onClick={() => setEditQuote(v => !v)}>
                  {editQuote ? "▾ 收起引文范围" : "▸ 默认整句作为引文依据（点击可微调）"}
                </Button>
                {editQuote && <Field label="原文引文">
                  <textarea className="textarea" rows={2} value={quote} onChange={e => setQuote(e.target.value)} maxLength={2000} required />
                </Field>}
              </div>

              <div className="dialog__foot">
                <Button ghost onClick={close}>取消</Button>
                <Button primary type="submit" disabled={!resource || quoteMissingTerm || action.busy}>
                  {action.busy ? "正在提交…" : "提交草稿"}
                </Button>
              </div>
            </>}
          </form>
        </aside>
      </div>
    </div>
  </Modal>;
}

export function Knowledge({ project, offline, refresh, changed, selection, openJob, onBack }: {
  project: Project; offline: boolean; refresh: number; changed: () => void;
  selection: Selection; openJob: (id: string) => void; onBack?: () => void;
}) {
  const [tab, setTab] = useState<"graph" | "memory">("graph");
  const [ledgerView, setLedgerView] = useState<"nodes" | "edges">("nodes");
  const [kindFilter, setKindFilter] = useState<"all" | "entity" | "occurrence">("all");
  const [dialog, setDialog] = useState<"build" | "draft" | "note">();
  const [editingNote, setEditingNote] = useState<ProjectNote>();
  const [query, setQuery] = useState("");
  const [revision, setRevision] = useState("");
  const [branch, setBranch] = useState("");
  const [node, setNode] = useState("");
  const [filters, setFilters] = useState({ query, snapshot_id: revision, branch_id: branch, node_id: node });
  const [graphItem, setGraphItem] = useState<GraphItem>();
  const [offset, setOffset] = useState(0);
  const [memorySearch, setMemorySearch] = useState("");
  const [memoryKind, setMemoryKind] = useState("all");
  const [memoryTab, setMemoryTab] = useState<"approved" | "drafts">("approved");
  const [memoryOffset, setMemoryOffset] = useState(0);
  const [decision, setDecision] = useState<{ record: Memory; action: "approve" | "retire" }>();
  const action = useAction(changed);
  const size = 25;
  const base = `/projects/${project.project_id}`;
  const graph = useQuery<Graph>(project.graph_snapshot && tab === "graph" ?
    base + `/graph?${params({ ...filters, offset, limit: size })}` : null, refresh);
  const memory = useQuery<{ approved: Memory[]; drafts: Memory[]; notes?: ProjectNote[] }>(
    tab === "memory" ? base + "/memory" : null, refresh,
  );

  const nodeMap = useMemo(() => {
    const map = new Map<string, GraphItem>();
    for (const item of graph.data?.nodes ?? []) {
      map.set(`${item.id}:${item.version}`, item);
      if (!map.has(item.id)) map.set(item.id, item);
    }
    return map;
  }, [graph.data?.nodes]);

  function nodeLabel(item: GraphItem) {
    return String(item.attributes?.name || item.evidence[0]?.quote || item.id);
  }
  function nodeKindText(item: GraphItem) {
    return item.kind === "entity" ? `实体 · ${String(item.attributes?.entity_type ?? "entity")}` : "文本节点";
  }
  function referenceLabel(ref?: Ref) {
    if (!ref) return "—";
    const target = nodeMap.get(`${ref.id}:${ref.version}`) ?? nodeMap.get(ref.id);
    return target ? nodeLabel(target) : ref.id;
  }
  const focusNode = (id: string) => {
    setNode(id);
    setOffset(0);
    setLedgerView("nodes");
    setKindFilter("all");
    setFilters(old => ({ ...old, node_id: id }));
  };
  const resetFilters = () => {
    setQuery(""); setRevision(""); setBranch(""); setNode(""); setOffset(0); setKindFilter("all");
    setFilters({ query: "", snapshot_id: "", branch_id: "", node_id: "" });
  };
  const hasFilters = !!(filters.query || filters.node_id || filters.branch_id || filters.snapshot_id);

  const filteredNodes = (graph.data?.nodes ?? []).filter(item =>
    kindFilter === "all" || (kindFilter === "entity" ? item.kind === "entity" : item.kind !== "entity"),
  );
  const relatedEdges = graphItem
    ? (graph.data?.edges ?? []).filter(e => e.source?.id === graphItem.id || e.target?.id === graphItem.id)
    : [];
  const rows = (memory.data?.[memoryTab] ?? []).filter(item =>
    (memoryKind === "all" || item.kind === memoryKind)
    && JSON.stringify(item.content).toLowerCase().includes(memorySearch.toLowerCase()),
  );

  return <>
    <Head title="图谱与记忆" sub={project.graph_ready
      ? `当前源版本 v${project.revision} · 已抽取 ${(project.resources ?? 0) - (project.unextracted ?? 0)}/${project.resources ?? 0} 条${(project.unextracted ?? 0) > 0 ? `（尚有 ${project.unextracted} 条可增量构建）` : ""}`
      : "当前源版本等待构建或发布图谱"}>
      {onBack && <Button ghost onClick={onBack}>← 返回上一页</Button>}
      {tab === "graph" ? <>{project.build_job_id && <Button ghost onClick={() => openJob(project.build_job_id!)}>查看建图任务</Button>}
        <Button primary disabled={!project.resources} onClick={() => setDialog("build")}>构建图谱</Button></> :
        <>
          <Button primary onClick={() => { setEditingNote(undefined); setDialog("note"); }}>+ 添加背景 / System Prompt</Button>
          <Button disabled={!project.resources} onClick={() => setDialog("draft")}>+ 起草术语 / 角色记忆</Button>
        </>}
    </Head>

    <div className="row-set">
      <Button small ghost aria-pressed={tab === "graph"} onClick={() => setTab("graph")}>知识图谱（表格台账）</Button>
      <Button small ghost aria-pressed={tab === "memory"} onClick={() => setTab("memory")}>项目记忆</Button>
    </div>
    <ErrorNote error={graph.error || memory.error} />

    {tab === "graph" && <>
      {!project.graph_snapshot ? <Empty title="还没有已发布的图谱">导入源文件后，点击右上角「构建图谱」启动实体与关系提取。</Empty> : <>
        <form className="row-set" onSubmit={e => {
          e.preventDefault(); setOffset(0); setFilters({ query: query.trim(), snapshot_id: revision.trim(), branch_id: branch.trim(), node_id: node.trim() });
        }}>
          <input className="input" aria-label="图谱关键词" placeholder="关键词检索实体或原文…" value={query} onChange={e => setQuery(e.target.value)} />
          <input className="input" aria-label="节点 ID" placeholder="节点 ID（可选）" value={node} onChange={e => setNode(e.target.value)} />
          <input className="input" aria-label="剧情分支" placeholder="剧情分支（可选）" value={branch} onChange={e => setBranch(e.target.value)} />
          <Button type="submit">查询</Button>
          {hasFilters && <Button ghost onClick={resetFilters}>退出聚焦 / 重置</Button>}
        </form>

        {graph.data && <div className="split">
          <div className="split__main">
            <div className="row-set">
              <div className="segs" role="tablist" aria-label="图谱台账视图">
                <button type="button" className={`seg${ledgerView === "nodes" ? " seg--on" : ""}`}
                  onClick={() => setLedgerView("nodes")}>节点与实体 ({graph.data.nodes.length})</button>
                <button type="button" className={`seg${ledgerView === "edges" ? " seg--on" : ""}`}
                  onClick={() => setLedgerView("edges")}>关系三元组 ({graph.data.edges.length})</button>
              </div>
              {ledgerView === "nodes" && <div className="segs" aria-label="按节点分类">
                {([["all", "全部"], ["entity", "仅实体"], ["occurrence", "仅原文"]] as const).map(([k, labelText]) => (
                  <button key={k} type="button" className={`seg${kindFilter === k ? " seg--on" : ""}`}
                    onClick={() => setKindFilter(k)}>{labelText}</button>
                ))}
              </div>}
              <span className="sp" />
              <span className="hint mono">匹配节点 {graph.data.total} · 快照 {graph.data.graph_snapshot.slice(-14)}</span>
            </div>

            {ledgerView === "nodes" ? <>
              {!filteredNodes.length ? <Empty title="没有匹配的节点或实体">{hasFilters ? <Button small ghost onClick={resetFilters}>清除筛选</Button> : undefined}</Empty>
                : <div className="table">
                  <Grid columns="minmax(160px, 1.4fr) 120px 80px 140px" header>
                    <span>名称 / 原文</span><span>分类</span><span>状态</span><span>操作</span>
                  </Grid>
                  {filteredNodes.map(item => <div key={item.id}
                    className={graphItem?.id === item.id ? "grid-row--active" : undefined}>
                    <Grid columns="minmax(160px, 1.4fr) 120px 80px 140px">
                      <span className="clip" style={{ fontWeight: 500 }} title={nodeLabel(item)}>{nodeLabel(item)}</span>
                      <span className="hint clip">{nodeKindText(item)}</span>
                      <Status value={item.valid ? "valid" : "invalid"} />
                      <div className="row-set">
                        <Button small ghost onClick={() => setGraphItem(item)}>详情</Button>
                        <Button small ghost onClick={() => focusNode(item.id)}>邻域</Button>
                      </div>
                    </Grid>
                  </div>)}
                </div>}
              <Pager total={graph.data.total} offset={offset} size={size} setOffset={setOffset} />
            </> : <>
              {!graph.data.edges.length ? <Empty title="当前筛选范围内暂无关系三元组" /> : <div className="table">
                <Grid columns="minmax(110px, 1fr) 130px minmax(110px, 1fr) 76px 76px" header>
                  <span>起点</span><span>关系</span><span>终点</span><span>状态</span><span />
                </Grid>
                {graph.data.edges.map(edge => <div key={edge.id}
                  className={graphItem?.id === edge.id ? "grid-row--active" : undefined}>
                  <Grid columns="minmax(110px, 1fr) 130px minmax(110px, 1fr) 76px 76px">
                    <span className="clip" title={referenceLabel(edge.source)}>{referenceLabel(edge.source)}</span>
                    <span className="pill pill--plain clip">{edge.relation ?? edge.kind}</span>
                    <span className="clip" title={referenceLabel(edge.target)}>{referenceLabel(edge.target)}</span>
                    <Status value={edge.valid ? "valid" : "invalid"} />
                    <Button small ghost onClick={() => setGraphItem(edge)}>详情</Button>
                  </Grid>
                </div>)}
              </div>}
            </>}
          </div>

          <aside className="split__side">
            <div className="panel">
              <div className="row-set">
                <h4>{graphItem ? "条目详情与原文依据" : "图谱说明"}</h4>
                <span className="sp" />
                {graphItem && <Button small ghost onClick={() => setGraphItem(undefined)}>关闭详情</Button>}
              </div>
              {!graphItem ? <>
                <p className="muted">采用轻量化文本与表格台账展示知识图谱：在左侧点击任意节点或关系「详情」，即可查看实体属性、原文出处与相连的三元组关系。</p>
                <div className="kv"><span>当前页实体</span><b>{graph.data.nodes.filter(n => n.kind === "entity").length} 个</b></div>
                <div className="kv"><span>当前页原文节点</span><b>{graph.data.nodes.filter(n => n.kind !== "entity").length} 条</b></div>
                <div className="kv"><span>当前局域关系</span><b>{graph.data.edges.length} 条</b></div>
              </> : <>
                <div className="row-set">
                  <b>{graphItem.source && graphItem.target
                    ? `${referenceLabel(graphItem.source)} → ${graphItem.relation ?? graphItem.kind} → ${referenceLabel(graphItem.target)}`
                    : nodeLabel(graphItem)}</b>
                  <Status value={graphItem.valid ? "valid" : "invalid"} />
                </div>
                <div className="mono hint">{graphItem.kind} {graphItem.attributes?.entity_type ? `· ${String(graphItem.attributes.entity_type)}` : ""} · v{graphItem.version} · {graphItem.id}</div>
                {!!Object.entries(graphItem.attributes ?? {}).length && <div className="ctxgrid">
                  {Object.entries(graphItem.attributes).map(([k, v]) => <div className="ctxcell" key={k}>
                    <span>{k}</span><b>{String(v)}</b>
                  </div>)}
                </div>}
                <Section>原文引文依据</Section>
                {graphItem.evidence.map((entry, i) => <div key={i} className="block">
                  <div className="text-block">{entry.quote}</div>
                  <p className="mono hint">{locationText(entry.location)} · 源 v{entry.source_version}</p>
                </div>)}
                {!graphItem.source && <>
                  <Button small onClick={() => focusNode(graphItem.id)}>以此节点过滤局部邻域</Button>
                  {!!relatedEdges.length && <>
                    <Section>关联关系（{relatedEdges.length}）</Section>
                    <div className="scroll-list stack">
                      {relatedEdges.map(edge => <div key={edge.id} className="block row-set">
                        <span>{referenceLabel(edge.source)}</span>
                        <span className="pill pill--plain">{edge.relation ?? edge.kind}</span>
                        <span>{referenceLabel(edge.target)}</span>
                      </div>)}
                    </div>
                  </>}
                </>}
              </>}
            </div>
          </aside>
        </div>}
      </>}
    </>}

    {tab === "memory" && <div className="stack">
      <div className="row-set">
        <Section>全局背景与 System Prompt 记忆（{memory.data?.notes?.length ?? 0}）</Section>
      </div>
      {!memory.data?.notes?.length ? <Empty title="暂无全局背景或 System Prompt 记忆">
        可在右上角点击「+ 添加背景 / System Prompt」补充游戏世界观、全局翻译规范或 System Prompt（无需绑定单条原文，启用后自动生效）。
      </Empty> : <div className="stack">
        {memory.data.notes.map(note => <div key={note.note_id} className="panel">
          <div className="row-set">
            <b>{note.title}</b>
            <span className="pill pill--plain">{noteCategories[note.category] ?? note.category}</span>
            <Status value={note.enabled ? "active" : "excluded"} pill />
            <span className="sp" />
            <Button small ghost disabled={action.busy} onClick={() => void action.run(() =>
              json(`${base}/notes/${note.note_id}`, post({
                category: note.category, title: note.title, content: note.content, enabled: !note.enabled,
              }, "PUT")))}>
              {note.enabled ? "停用" : "启用"}
            </Button>
            <Button small ghost disabled={action.busy} onClick={() => { setEditingNote(note); setDialog("note"); }}>编辑</Button>
            <Button small ghost disabled={action.busy} onClick={() => void action.run(() =>
              json(`${base}/notes/${note.note_id}`, { method: "DELETE" }))}>删除</Button>
          </div>
          <div className="source text-block">{note.content}</div>
        </div>)}
      </div>}

      <Section>单句术语与角色记忆</Section>
      <div className="row-set">
        {(["approved", "drafts"] as const).map(key => <Button small ghost key={key} aria-pressed={memoryTab === key}
          onClick={() => { setMemoryTab(key); setMemoryOffset(0); }}>{key === "approved" ? "已生效" : "待批准"}（{memory.data?.[key].length ?? 0}）</Button>)}
        <select className="input" aria-label="按类型筛选" value={memoryKind} onChange={e => { setMemoryKind(e.target.value); setMemoryOffset(0); }}>
          <option value="all">全部类型</option>
          {Object.entries(memoryKinds).map(([k, label]) => <option key={k} value={k}>{label}</option>)}
        </select>
        <input className="input" aria-label="搜索记忆" placeholder="搜索记忆内容" value={memorySearch} onChange={e => { setMemorySearch(e.target.value); setMemoryOffset(0); }} />
      </div>
      {memory.data && !rows.length && <Empty title="没有匹配的术语或角色记忆" />}
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
    </div>}

    {dialog === "build" && <Build project={project} offline={offline} close={() => setDialog(undefined)} openJob={openJob} />}
    {dialog === "note" && <ProjectNoteModal project={project} initial={editingNote}
      close={() => { setDialog(undefined); setEditingNote(undefined); }} changed={changed} />}
    {dialog === "draft" && <DraftMemory project={project} selection={selection} close={() => setDialog(undefined)} changed={changed} />}
    {decision && <Modal title={decision.action === "approve" ? "批准记忆" : "退役记忆"} close={() => { setDecision(undefined); action.clear(); }}>
      <ErrorNote error={action.error} />
      <form className="stack" onSubmit={event => {
        event.preventDefault(); const reason = new FormData(event.currentTarget).get("reason");
        void action.run(async () => {
          await json(`${base}/memory/${encodeURIComponent(decision.record.record_id)}/${decision.record.version}/${decision.action}`, post({ reason }));
          setDecision(undefined); setMemoryOffset(0);
        });
      }}><Field label="处理原因"><textarea className="textarea" name="reason" required maxLength={2000} /></Field>
        <div className="dialog__foot">
          <Button ghost onClick={() => { setDecision(undefined); action.clear(); }}>取消</Button>
          <Button primary type="submit" disabled={action.busy}>确认</Button>
        </div>
      </form>
    </Modal>}
  </>;
}
