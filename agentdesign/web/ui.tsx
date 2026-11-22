import { cloneElement, useEffect, useId, useRef, useState, type ButtonHTMLAttributes, type ReactElement, type ReactNode } from "react";
import { ApiError, reasonText } from "./api";

const states: Record<string, [string, string?]> = {
  queued: ["已排队", "info"], running: ["执行中", "info"], awaiting_input: ["待补充", "warn"],
  awaiting_review: ["待复核", "warn"], paused: ["已暂停", "warn"], completed: ["已完成", "ok"],
  partial: ["部分完成", "warn"], cancelled: ["已取消"], failed: ["失败", "bad"],
  needs_human: ["待人工", "warn"], conflict: ["版本冲突", "bad"], locked: ["已锁定", "lock"],
  committed: ["已提交", "ok"], reviewed: ["已审校", "ok"], no_change: ["沿用", "ok"], passed: ["通过", "ok"],
  excluded: ["已排除"], draft: ["待批准", "warn"], approved: ["已生效", "ok"], invalid: ["已失效", "bad"],
  valid: ["有效", "ok"], active: ["生效中", "ok"], pending: ["待建图", "warn"], preview: ["预览"],
  applied: ["已应用", "ok"], rolled_back: ["已回滚"], published: ["已发布", "ok"], retained: ["已归档"],
};
export const label = (status: string) => states[status]?.[0] ?? status;
export const tone = (status: string) => states[status]?.[1];

export function Status({ value, pill = false }: { value: string; pill?: boolean }) {
  const color = tone(value);
  return pill ? <span className={`pill ${color ? "pill--" + color : ""}`}>{label(value)}</span> :
    <span className="status"><span className={`dot ${color ? "dot--" + color : ""}`} />{label(value)}</span>;
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
  const code = error instanceof ApiError ? error.reason : error ? "operation_failed" : reason;
  if (!code) return null;
  return <div className="toast" role="alert"><div>{reasonText(code)}
    <br /><code>{code}{error instanceof ApiError && error.fields?.length ? ` · ${error.fields.map(field => field.join(".")).join("、")}` : ""}</code>
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
  return <div className="row-set pager">
    <span className="mono muted">{total ? `${offset + 1}–${Math.min(offset + size, total)} / ${total}` : "0"}</span>
    <Button small ghost disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - size))}>上一页</Button>
    <Button small ghost disabled={offset + size >= total} onClick={() => setOffset(offset + size)}>下一页</Button>
  </div>;
}

export function Modal({ title, children, close, className }: {
  title: string; children: ReactNode; close: () => void; className?: string;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => { dialog.current?.showModal(); }, []);
  const dismiss = () => { dialog.current?.close(); close(); };
  return <dialog className={className ? `dialog ${className}` : "dialog"} ref={dialog} aria-label={title}
    onCancel={event => { event.preventDefault(); dismiss(); }}>
    <div className="row-set"><h3>{title}</h3><span className="sp" /><Button small ghost onClick={dismiss}>关闭</Button></div>
    <div className="stack dialog__content">{children}</div>
  </dialog>;
}
