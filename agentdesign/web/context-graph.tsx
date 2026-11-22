import { useEffect, useRef, useState, type ReactNode } from "react";
import { Controls, Handle, Position, ReactFlow, type Edge, type Node, type NodeProps } from "@xyflow/react";
import "@xyflow/react/dist/base.css";
import { type Evidence, type Item, type Location, locationText, params, useQuery } from "./api";
import { Button, Empty, ErrorNote } from "./ui";

type SourceRef = { occurrence_id: string; source_version: number };
export type SourcePreview = { id: string; location: string; source: SourceRef };
type SourceLine = SourceRef & { text: string; location: Location; speaker_id: string | null; focused: boolean };
type ContextEntry = { id: string; label: string; text: string; location?: string; preview?: SourcePreview };
type ContextGroup = {
  id: string; title: string; summary: string; relation?: string; entries: ContextEntry[]; empty?: string;
};
type ContextNode = Node<{
  title: string; summary: string; source: boolean; active: boolean; select: () => void;
}, "context">;

function ContextNodeButton({ data }: NodeProps<ContextNode>) {
  return <>
    {!data.source && <Handle type="target" position={Position.Left} isConnectable={false} />}
    <button type="button" className={data.source ? "context-graph__node context-graph__node--source nopan" : "context-graph__node nopan"}
      aria-pressed={data.active} onClick={data.select}>
      <span className="context-graph__node-title">{data.title}</span>
      <span className="context-graph__node-summary">{data.summary}</span>
    </button>
    {data.source && <Handle type="source" position={Position.Right} isConnectable={false} />}
  </>;
}

const nodeTypes = { context: ContextNodeButton };
const contextLabels: Record<string, string> = {
  source: "原文", style: "角色笔记", translation: "译例", summary: "摘要", term: "术语",
};
const hasSourceLine = (location?: Location | null) => !!location?.path && (location.row != null || location.line != null);

export function evidenceCitation(ref: Evidence["actual_sources"][number], index: number) {
  const id = `source-${ref.occurrence_id}:${ref.source_version}:${index}`;
  const location = [locationText(ref.location), `源 v${ref.source_version}`].filter(Boolean).join(" · ");
  return { id, location, preview: hasSourceLine(ref.location) ? { id, location, source: ref } : undefined };
}

function SourceContext({ url, location, back }: { url: string; location: string; back: () => void }) {
  const [retry, setRetry] = useState(0);
  const { data, error } = useQuery<{ lines: SourceLine[] }>(url, retry);
  const list = useRef<HTMLOListElement>(null);
  const focusedLine = useRef<HTMLLIElement>(null);
  useEffect(() => {
    const panel = list.current;
    const line = focusedLine.current;
    if (panel && line) panel.scrollTop = Math.max(0, line.offsetTop - Math.max(0, (panel.clientHeight - line.offsetHeight) / 2));
  }, [data]);
  return <>
    <div className="row-set"><h4>原文前后文</h4><span className="sp" /><Button small ghost autoFocus onClick={back}>返回详情</Button></div>
    <span className="hint">{location}</span>
    <ErrorNote error={error} />
    {error ? <Button small onClick={() => setRetry(value => value + 1)}>重试读取前后文</Button> : !data && <p role="status">正在读取原文前后文…</p>}
    {data && <ol className="source-context__lines" aria-label="原文片段" tabIndex={0} ref={list}>
      {data.lines.map(line => <li key={line.occurrence_id} ref={line.focused ? focusedLine : undefined}
        className={line.focused ? "source-context__line source-context__line--focused" : "source-context__line"}
        aria-current={line.focused ? "true" : undefined}>
        <div className="row-set"><span className="mono hint">第 {line.location.row ?? line.location.line} 行</span>
          <span className="label">{line.speaker_id || "说话人未标注"}</span>
          {line.focused && <span className="label">原句</span>}</div>
        <div className="text-block">{line.text}</div>
      </li>)}
    </ol>}
  </>;
}

function contextGroups(item: Item, items: Item[], evidence?: Evidence): ContextGroup[] {
  const metadata = { ...item.metadata, ...evidence?.actual_context?.resolved_metadata };
  const { speaker_id: speaker, addressee_id: addressee, branch_id: branch } = metadata;
  const adopted = evidence?.actual_context?.items ?? [];
  const adoptedIds = new Set(adopted.map(entry => entry.id));
  const candidates = (evidence?.retrieved ?? []).filter(entry => !adoptedIds.has(entry.id));
  const mandatory = evidence?.mandatory_terms ?? [];
  const adoptedEntries = adopted.map(entry => ({
    ...entry, label: `实际采用 · ${contextLabels[entry.category] ?? "上下文"}`,
  }));
  const candidateEntries = candidates.map(entry => ({
    ...entry, label: `${evidence?.actual_context ? "未采用的候选" : "检索候选"} · ${contextLabels[entry.category] ?? "上下文"}`,
  }));
  const terms = [
    ...mandatory.map(({ source, target }, index) => ({ id: `mandatory-${index}`, label: "实际采用 · 强制术语", text: `${source} → ${target}` })),
    ...adoptedEntries.filter(entry => entry.category === "term" && (!entry.mandatory || !mandatory.length)),
    ...candidateEntries.filter(entry => entry.category === "term"),
  ];
  const sources = (evidence?.actual_sources ?? []).map((ref, index) => ({
    ...evidenceCitation(ref, index), label: "实际依据 · 原文引文", text: ref.quote,
  }));
  const references = [
    ...sources,
    ...adoptedEntries.filter(entry => entry.category !== "term"),
    ...candidateEntries.filter(entry => entry.category !== "term"),
  ];
  const groups: ContextGroup[] = [];
  if (speaker || addressee) {
    const roles: ContextEntry[] = [];
    if (speaker) roles.push({ id: "speaker", label: "说话人", text: speaker });
    if (addressee) roles.push({ id: "addressee", label: "听话人", text: addressee });
    groups.push({ id: "roles", title: "对话角色", summary: roles.map(role => role.text).join(" → "),
      relation: speaker ? "由谁说出" : "说给谁听", entries: roles });
  }
  if (branch) {
    const sameBranch = items.filter(row => row.step === item.step && row.occurrence_id !== item.occurrence_id &&
      row.metadata?.branch_id === branch);
    groups.push({ id: "branch", title: "剧情分支", summary: branch, relation: "出现于",
      entries: sameBranch.map(row => ({ id: row.occurrence_id, label: "本批同分支条目", text: row.source,
        location: locationText(row.location, row.file) })), empty: "本批没有其他同分支条目。" });
  }
  if (terms.length) {
    groups.push({ id: "terms", title: "术语译法", summary: `${terms.length} 条`, relation: "术语上下文", entries: terms });
  }
  if (references.length) {
    const used = sources.length + adoptedEntries.filter(entry => entry.category !== "term").length;
    const summary = [used && `${used} 条依据`, references.length > used && `${references.length - used} 条候选`]
      .filter(Boolean).join(" · ");
    groups.push({ id: "evidence", title: "翻译依据", summary, relation: "查阅依据", entries: references });
  }
  return groups;
}

export function ContextGraph({ item, items, translation, editor, evidence, loadingEvidence, initialSource }: {
  item: Item; items: Item[]; translation: string;
  editor?: { change: (text: string) => void; save: () => void; saved: boolean; checks: ReactNode;
    approve: () => void; busy: boolean; hasNext: boolean };
  evidence?: Evidence; loadingEvidence: boolean; initialSource?: SourcePreview;
}) {
  const [selection, setSelection] = useState(initialSource ? "evidence" : "current");
  const [sourcePreview, setSourcePreview] = useState(initialSource);
  const returnToSource = useRef("");
  const [visible, setVisible] = useState(false);
  // Wait until the native dialog opens so React Flow fits a measurable canvas.
  useEffect(() => { setVisible(true); }, []);
  const groups = contextGroups(item, items, evidence);
  const originalLocation = [locationText(item.location, item.file), `源 v${item.source_version}`].filter(Boolean).join(" · ");
  const current: ContextGroup = {
    id: "current", title: "当前原文", summary: item.source,
    entries: [{ id: "original", label: "原文", text: item.source, location: originalLocation,
      preview: hasSourceLine(item.location) ? { id: "original", location: originalLocation, source: item } : undefined }],
  };
  const selected = groups.find(group => group.id === selection) ?? current;
  const rowSpacing = 82;
  const nodes: ContextNode[] = [current, ...groups].map((group, index) => ({
    id: group.id,
    type: "context",
    position: index === 0 ? { x: 0, y: Math.max(0, (groups.length - 1) * rowSpacing / 2) } :
      { x: 360, y: (index - 1) * rowSpacing },
    data: { title: group.title, summary: group.summary, source: index === 0, active: selected.id === group.id,
      select: () => { setSourcePreview(undefined); setSelection(group.id); } },
  }));
  const edges: Edge[] = groups.map(group => ({
    id: group.id, source: current.id, target: group.id, label: group.relation, type: "straight",
  }));
  return (
    <div className="context-graph">
      <section className="context-graph__translation" aria-label="当前译文">
        {editor ? <>
          <div className="row-set">
            <label className="label" htmlFor="context-graph-target">当前译文</label><span className="sp" />
            <Button small ghost disabled={editor.busy} onClick={editor.save}>{editor.saved ? "已保存到本地" : "保存草稿"}</Button>
          </div>
          <textarea id="context-graph-target" className="target" value={translation} maxLength={5000}
            autoFocus readOnly={editor.busy} placeholder="输入译文" onChange={event => editor.change(event.target.value)} />
          {editor.checks}
          <div className="dialog__foot">
            <Button small primary disabled={editor.busy || !translation.trim()} onClick={editor.approve}>
              {editor.busy ? "正在提交…" : editor.hasNext ? "批准并看下一条" : "批准译文"}
            </Button>
          </div>
        </> : <>
          <h4 className="label">当前译文</h4>
          {translation.trim() ? <div className="text-block">{translation}</div> : <span className="muted">暂无译文</span>}
        </>}
      </section>
      <div className="context-graph__canvas">
        {visible && <ReactFlow key={groups.map(group => group.id).join(":")} nodes={nodes} edges={edges} nodeTypes={nodeTypes}
          aria-label="当前条目的上下文关系" fitView fitViewOptions={{ padding: .14, maxZoom: 1.15 }} minZoom={.2}
          nodesDraggable={false} nodesConnectable={false} elementsSelectable={false}
          nodesFocusable={false} edgesFocusable={false} deleteKeyCode={null}
          zoomOnDoubleClick={false} ariaLabelConfig={{
            "controls.ariaLabel": "图谱缩放", "controls.zoomIn.ariaLabel": "放大", "controls.zoomOut.ariaLabel": "缩小",
            "controls.fitView.ariaLabel": "显示全图",
          }}>
          <Controls showInteractive={false} />
        </ReactFlow>}
      </div>
      <section className="context-graph__details" key={sourcePreview?.id ?? selected.id} aria-label="上下文详情">
        {sourcePreview ? <SourceContext location={sourcePreview.location}
          url={`${item.evidence_url}/context?${params({ source_id: sourcePreview.source.occurrence_id, source_version: sourcePreview.source.source_version })}`}
          back={() => { returnToSource.current = sourcePreview.id; setSourcePreview(undefined); }} /> : <>
          <h4>{selected.id === current.id ? "原文详情" : selected.title}</h4>
          {selected.entries.map(({ id, label, text, location, preview }) => <div className="context-graph__entry" key={id}>
            <span className="label">{label}</span>
            <div className="text-block">{text}</div>
            {location && (preview ? <button type="button" className="hint context-graph__location"
              aria-label={`查看 ${location} 的前后文`} onClick={() => setSourcePreview(preview)}
              ref={button => {
                if (button && returnToSource.current === id) {
                  returnToSource.current = "";
                  button.focus();
                }
              }}>{location}</button> : <span className="hint">{location}</span>)}
          </div>)}
          {selected.empty && <Empty title={selected.empty} />}
          {loadingEvidence && <p role="status">正在读取条目依据…</p>}
          {evidence && !groups.length && <Empty title="该条目暂无关联上下文" />}
        </>}
      </section>
    </div>
  );
}
