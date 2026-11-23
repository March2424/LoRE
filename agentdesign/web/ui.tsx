import { cloneElement, useEffect, useId, useRef, useState, type ButtonHTMLAttributes, type ReactElement, type ReactNode } from "react";
import { ApiError, reasonText } from "./api";
import { type Locale, getLocale, useI18n } from "./i18n";

const states: Record<string, [string, string, string?]> = {
  queued: ["Queued", "已排队", "info"], running: ["Running", "执行中", "info"], awaiting_input: ["Needs Input", "待补充", "warn"],
  awaiting_review: ["Needs Review", "待复核", "warn"], paused: ["Paused", "已暂停", "warn"], completed: ["Completed", "已完成", "ok"],
  partial: ["Partial", "部分完成", "warn"], cancelled: ["Cancelled", "已取消"], failed: ["Failed", "失败", "bad"],
  needs_human: ["Needs Human", "待人工", "warn"], conflict: ["Conflict", "版本冲突", "bad"], locked: ["Locked", "已锁定", "lock"],
  committed: ["Committed", "已提交", "ok"], reviewed: ["Reviewed", "已审校", "ok"], no_change: ["Unchanged", "沿用", "ok"], passed: ["Passed", "通过", "ok"],
  excluded: ["Excluded", "已排除"], draft: ["Pending", "待批准", "warn"], approved: ["Approved", "已生效", "ok"], invalid: ["Invalid", "已失效", "bad"],
  valid: ["Valid", "有效", "ok"], active: ["Active", "生效中", "ok"], pending: ["Pending", "待建图", "warn"], preview: ["Preview", "预览"],
  applied: ["Applied", "已应用", "ok"], rolled_back: ["Rolled Back", "已回滚"], published: ["Published", "已发布", "ok"], retained: ["Archived", "已归档"],
};
export const label = (status: string, locale: Locale = getLocale()) => {
  const row = states[status];
  return row ? (locale === "zh" ? row[1] : row[0]) : status;
};
export const tone = (status: string) => states[status]?.[2];

export function Status({ value, pill = false }: { value: string; pill?: boolean }) {
  const { locale } = useI18n();
  const color = tone(value);
  return pill ? <span className={`pill ${color ? "pill--" + color : ""}`}>{label(value, locale)}</span> :
    <span className="status"><span className={`dot ${color ? "dot--" + color : ""}`} />{label(value, locale)}</span>;
}

export function Button({ children, primary, ghost, small, className, ...props }:
  ButtonHTMLAttributes<HTMLButtonElement> & { primary?: boolean; ghost?: boolean; small?: boolean }) {
  return <button type="button" {...props} className={`btn${primary ? " btn--primary" : ""}${ghost ? " btn--ghost" : ""}${small ? " btn--sm" : ""}${className ? " " + className : ""}`}>{children}</button>;
}

export function Head({ title, sub, children }: { title: string; sub?: ReactNode; children?: ReactNode }) {
  return <header className="page-head"><div><h1>{title}</h1>{sub && <div className="sub">{sub}</div>}</div>
    {children && <div className="actions">{children}</div>}</header>;
}

export function Section({ children }: { children: ReactNode }) { return <h2 className="section">{children}</h2>; }
export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return <div className="empty"><b>{title}</b>{children}</div>;
}
export function Field({ label, children }: { label: string; children: ReactElement<{ id?: string }> }) {
  const id = useId();
  return <div className="field"><label className="label" htmlFor={id}>{label}</label>{cloneElement(children, { id })}</div>;
}

export function ErrorNote({ error, reason }: { error?: unknown; reason?: string | null }) {
  const { locale, t } = useI18n();
  const code = error instanceof ApiError ? error.reason
    : error instanceof Error ? (error.message || "operation_failed")
      : error ? "operation_failed" : reason;
  if (!code) return null;
  const details = error instanceof ApiError ? error.details : undefined;
  const hasExtraField = !!details?.some(d => d.type === "extra_forbidden");
  const fields = error instanceof ApiError && error.fields?.length
    ? t(`Affected fields: ${error.fields.map(field => field.join(".")).join(", ")}`, `涉及字段：${error.fields.map(field => field.join(".")).join("、")}`)
    : "";
  const msg = hasExtraField
    ? t(
        "The frontend submitted a field rejected by the running backend (usually because run_api.py has not been restarted after updating code). Press Ctrl+C and restart the backend server, then retry.",
        "前端提交了后端当前尚未识别的字段（通常是更新代码后尚未重启 run_api.py 后端进程导致）。请先 Ctrl+C 停止并重启后端服务后重试。",
      )
    : details?.length
      ? `${reasonText(code, locale)} (${details.map(d => `${d.field}: ${d.msg}`).join("; ")})`
      : `${reasonText(code, locale)}${fields ? ` (${fields})` : ""}`;
  return <div className="toast" role="alert"><div>
    <div>{msg}</div>
    <div className="hint" style={{ marginTop: 4 }}>{t("Error code: ", "错误代码：")}<code>{code}</code></div>
  </div></div>;
}

export function useAction(onDone?: () => void) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  const running = useRef(false);
  async function run(action: () => Promise<unknown>) {
    if (running.current) return;
    running.current = true;
    setBusy(true);
    setError(undefined);
    try { await action(); onDone?.(); }
    catch (error) { setError(error); }
    finally { running.current = false; setBusy(false); }
  }
  return { busy, error, run, clear: () => setError(undefined) };
}

export function Grid({ columns, children, header = false }: { columns: string; children: ReactNode; header?: boolean }) {
  return <div className={`grid-row${header ? " grid-head" : ""}`} style={{ gridTemplateColumns: columns }}>{children}</div>;
}

export function Pager({ total, offset, size, setOffset }: { total: number; offset: number; size: number; setOffset: (n: number) => void }) {
  const { t } = useI18n();
  return <div className="row-set pager">
    <span className="mono muted">{total ? `${offset + 1}–${Math.min(offset + size, total)} / ${total}` : "0"}</span>
    <Button small ghost disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - size))}>{t("Prev", "上一页")}</Button>
    <Button small ghost disabled={offset + size >= total} onClick={() => setOffset(offset + size)}>{t("Next", "下一页")}</Button>
  </div>;
}

export function Modal({ title, children, close, className }: {
  title: string; children: ReactNode; close: () => void; className?: string;
}) {
  const { t } = useI18n();
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => { dialog.current?.showModal(); }, []);
  const dismiss = () => { dialog.current?.close(); close(); };
  return <dialog className={className ? `dialog ${className}` : "dialog"} ref={dialog} aria-label={title}
    onCancel={event => { event.preventDefault(); dismiss(); }}>
    <div className="row-set"><h3>{title}</h3><span className="sp" /><Button small ghost onClick={dismiss}>{t("Close", "关闭")}</Button></div>
    <div className="stack dialog__content">{children}</div>
  </dialog>;
}
