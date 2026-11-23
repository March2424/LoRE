import { useEffect, useRef, useState, type ReactNode } from "react";
import { type Evidence, type Item, type Location, locationText, params, useQuery } from "./api";
import { type Locale, getLocale, pick, useI18n } from "./i18n";
import { Button, Empty, ErrorNote } from "./ui";

type SourceRef = { occurrence_id: string; source_version: number };
export type SourcePreview = { id: string; location: string; source: SourceRef };
type SourceLine = SourceRef & { text: string; location: Location; speaker_id: string | null; focused: boolean };
type ContextEntry = { id: string; label: string; text: string; location?: string; preview?: SourcePreview };
type ContextGroup = {
  id: string; title: string; summary: string; relation?: string; entries: ContextEntry[]; empty?: string;
};

const contextLabels = (category: string, locale: Locale) => ({
  source: pick("Source", "原文", locale),
  style: pick("Character Note", "角色笔记", locale),
  translation: pick("TM Example", "译例", locale),
  summary: pick("Summary", "摘要", locale),
  term: pick("Term", "术语", locale),
}[category] ?? pick("Context", "上下文", locale));
const hasSourceLine = (location?: Location | null) => !!location?.path && (location.row != null || location.line != null);

export function evidenceCitation(ref: Evidence["actual_sources"][number], index: number) {
  const id = `source-${ref.occurrence_id}:${ref.source_version}:${index}`;
  const location = [locationText(ref.location), `v${ref.source_version}`].filter(Boolean).join(" · ");
  return { id, location, preview: hasSourceLine(ref.location) ? { id, location, source: ref } : undefined };
}

function SourceContext({ url, location, back }: { url: string; location: string; back: () => void }) {
  const { t } = useI18n();
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
    <div className="row-set"><h4>{t("Surrounding Source Lines", "原文前后文")}</h4><span className="sp" /><Button small ghost autoFocus onClick={back}>{t("← Back to Details", "← 返回详情")}</Button></div>
    <span className="hint">{location}</span>
    <ErrorNote error={error} />
    {error ? <Button small onClick={() => setRetry(value => value + 1)}>{t("Retry loading context", "重试读取前后文")}</Button> : !data && <p role="status">{t("Loading surrounding source…", "正在读取原文前后文…")}</p>}
    {data && <ol className="source-context__lines" aria-label={t("Source excerpt", "原文片段")} tabIndex={0} ref={list}>
      {data.lines.map(line => <li key={line.occurrence_id} ref={line.focused ? focusedLine : undefined}
        className={line.focused ? "source-context__line source-context__line--focused" : "source-context__line"}
        aria-current={line.focused ? "true" : undefined}>
        <div className="row-set"><span className="mono hint">{t(`Line ${line.location.row ?? line.location.line}`, `第 ${line.location.row ?? line.location.line} 行`)}</span>
          <span className="label">{line.speaker_id || t("Unlabeled speaker", "说话人未标注")}</span>
          {line.focused && <span className="label">{t("Target line", "原句")}</span>}</div>
        <div className="text-block">{line.text}</div>
      </li>)}
    </ol>}
  </>;
}

function contextGroups(item: Item, items: Item[], evidence: Evidence | undefined, locale: Locale): ContextGroup[] {
  const metadata = { ...item.metadata, ...evidence?.actual_context?.resolved_metadata };
  const { speaker_id: speaker, addressee_id: addressee, branch_id: branch } = metadata;
  const adopted = evidence?.actual_context?.items ?? [];
  const adoptedIds = new Set(adopted.map(entry => entry.id));
  const candidates = (evidence?.retrieved ?? []).filter(entry => !adoptedIds.has(entry.id));
  const mandatory = evidence?.mandatory_terms ?? [];
  const adoptedEntries = adopted.map(entry => ({
    ...entry, label: `${pick("Used", "实际采用", locale)} · ${contextLabels(entry.category, locale)}`,
  }));
  const candidateEntries = candidates.map(entry => ({
    ...entry, label: `${evidence?.actual_context ? pick("Unused candidate", "未采用的候选", locale) : pick("Retrieved", "检索候选", locale)} · ${contextLabels(entry.category, locale)}`,
  }));
  const terms = [
    ...mandatory.map(({ source, target }, index) => ({ id: `mandatory-${index}`, label: pick("Used · Mandatory Term", "实际采用 · 强制术语", locale), text: `${source} → ${target}` })),
    ...adoptedEntries.filter(entry => entry.category === "term" && (!entry.mandatory || !mandatory.length)),
    ...candidateEntries.filter(entry => entry.category === "term"),
  ];
  const sources = (evidence?.actual_sources ?? []).map((ref, index) => ({
    ...evidenceCitation(ref, index), label: pick("Used · Source Quote", "实际依据 · 原文引文", locale), text: ref.quote,
  }));
  const references = [
    ...sources,
    ...adoptedEntries.filter(entry => entry.category !== "term"),
    ...candidateEntries.filter(entry => entry.category !== "term"),
  ];
  const groups: ContextGroup[] = [];
  if (speaker || addressee) {
    const roles: ContextEntry[] = [];
    if (speaker) roles.push({ id: "speaker", label: pick("Speaker", "说话人", locale), text: speaker });
    if (addressee) roles.push({ id: "addressee", label: pick("Addressee", "听话人", locale), text: addressee });
    groups.push({ id: "roles", title: pick("Dialogue Roles", "对话角色", locale), summary: roles.map(role => role.text).join(" → "),
      relation: speaker ? pick("Spoken by", "由谁说出", locale) : pick("Spoken to", "说给谁听", locale), entries: roles });
  }
  if (branch) {
    const sameBranch = items.filter(row => row.step === item.step && row.occurrence_id !== item.occurrence_id &&
      row.metadata?.branch_id === branch);
    groups.push({ id: "branch", title: pick("Story Branch", "剧情分支", locale), summary: branch, relation: pick("Appears in", "出现于", locale),
      entries: sameBranch.map(row => ({ id: row.occurrence_id, label: pick("Same-branch entry", "本批同分支条目", locale), text: row.source,
        location: locationText(row.location, row.file) })), empty: pick("No other entries on this branch in the current batch.", "本批没有其他同分支条目。", locale) });
  }
  if (terms.length) {
    groups.push({ id: "terms", title: pick("Terminology", "术语译法", locale), summary: pick(`${terms.length} items`, `${terms.length} 条`, locale), relation: pick("Term Context", "术语上下文", locale), entries: terms });
  }
  if (references.length) {
    const used = sources.length + adoptedEntries.filter(entry => entry.category !== "term").length;
    const summary = [used && pick(`${used} used`, `${used} 条依据`, locale), references.length > used && pick(`${references.length - used} unused`, `${references.length - used} 条候选`, locale)]
      .filter(Boolean).join(" · ");
    groups.push({ id: "evidence", title: pick("Evidence & References", "翻译依据", locale), summary, relation: pick("Consulted", "查阅依据", locale), entries: references });
  }
  return groups;
}

export function ContextGraph({ item, items, translation, editor, evidence, loadingEvidence, initialSource }: {
  item: Item; items: Item[]; translation: string;
  editor?: { change: (text: string) => void; save: () => void; saved: boolean; checks: ReactNode;
    approve: () => void; busy: boolean; hasNext: boolean };
  evidence?: Evidence; loadingEvidence: boolean; initialSource?: SourcePreview;
}) {
  const { locale, t } = useI18n();
  const [selection, setSelection] = useState(initialSource ? "evidence" : "current");
  const [sourcePreview, setSourcePreview] = useState(initialSource);
  const returnToSource = useRef("");
  const groups = contextGroups(item, items, evidence, locale);
  const originalLocation = [locationText(item.location, item.file), `v${item.source_version}`].filter(Boolean).join(" · ");
  const current: ContextGroup = {
    id: "current", title: t("Current Source", "当前原文"), summary: item.source,
    entries: [{ id: "original", label: t("Source", "原文"), text: item.source, location: originalLocation,
      preview: hasSourceLine(item.location) ? { id: "original", location: originalLocation, source: item } : undefined }],
  };
  const allGroups = [current, ...groups];
  const selected = groups.find(group => group.id === selection) ?? current;

  return (
    <div className="context-graph">
      <section className="context-graph__translation" aria-label={t("Current Translation", "当前译文")}>
        {editor ? <>
          <div className="row-set">
            <label className="label" htmlFor="context-graph-target">{t("Current Translation", "当前译文")}</label><span className="sp" />
            <Button small ghost disabled={editor.busy} onClick={editor.save}>{editor.saved ? t("Saved locally", "已保存到本地") : t("Save Draft", "保存草稿")}</Button>
          </div>
          <textarea id="context-graph-target" className="target" value={translation} maxLength={5000}
            autoFocus readOnly={editor.busy} placeholder={t("Enter translation", "输入译文")} onChange={event => editor.change(event.target.value)} />
          {editor.checks}
          <div className="dialog__foot">
            <Button small primary disabled={editor.busy || !translation.trim()} onClick={editor.approve}>
              {editor.busy ? t("Submitting…", "正在提交…") : editor.hasNext ? t("Approve & Next", "批准并看下一条") : t("Approve Translation", "批准译文")}
            </Button>
          </div>
        </> : <>
          <h4 className="label">{t("Current Translation", "当前译文")}</h4>
          {translation.trim() ? <div className="text-block">{translation}</div> : <span className="muted">{t("No translation yet", "暂无译文")}</span>}
        </>}
      </section>

      <div className="ctx-ledger" role="tablist" aria-label={t("Context groups", "当前条目的上下文分组")}>
        {allGroups.map(group => <button key={group.id} type="button" role="tab" aria-selected={selected.id === group.id}
          className={`ctx-ledger__card${selected.id === group.id ? " ctx-ledger__card--active" : ""}`}
          onClick={() => { setSourcePreview(undefined); setSelection(group.id); }}>
          <div className="row-set">
            <b>{group.title}</b>
            {group.relation && <span className="pill pill--plain">{group.relation}</span>}
            <span className="sp" />
            <span className="mono hint">{t(`${group.entries.length} items`, `${group.entries.length} 项`)}</span>
          </div>
          <div className="ctx-ledger__summary">{group.summary}</div>
        </button>)}
      </div>

      <section className="context-graph__details" key={sourcePreview?.id ?? selected.id} aria-label={t("Context details", "上下文详情")}>
        {sourcePreview ? <SourceContext location={sourcePreview.location}
          url={`${item.evidence_url}/context?${params({ source_id: sourcePreview.source.occurrence_id, source_version: sourcePreview.source.source_version })}`}
          back={() => { returnToSource.current = sourcePreview.id; setSourcePreview(undefined); }} /> : <>
          <h4>{selected.id === current.id ? t("Source Details", "原文详情") : selected.title}</h4>
          {selected.entries.map(({ id, label, text, location, preview }) => <div className="context-graph__entry" key={id}>
            <span className="label">{label}</span>
            <div className="text-block">{text}</div>
            {location && (preview ? <button type="button" className="hint context-graph__location"
              aria-label={t(`Inspect surrounding lines at ${location}`, `查看 ${location} 的前后文`)} onClick={() => setSourcePreview(preview)}
              ref={button => {
                if (button && returnToSource.current === id) {
                  returnToSource.current = "";
                  button.focus();
                }
              }}>{location}</button> : <span className="hint">{location}</span>)}
          </div>)}
          {selected.empty && <Empty title={selected.empty} />}
          {loadingEvidence && <p role="status">{t("Loading evidence…", "正在读取条目依据…")}</p>}
          {evidence && !groups.length && <Empty title={t("No linked context for this entry", "该条目暂无关联上下文")} />}
        </>}
      </section>
    </div>
  );
}
