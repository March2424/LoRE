"""Blue-styled terminal views for jobs, SSE streams, evidence, PO, P4, P5, and P6."""

from typing import Any

from .theme import draw_panel, draw_table, fg, terminal_panel_width, truncate_display

STATUS_ZH = {
    "queued": "排队中",
    "running": "执行中",
    "paused": "已暂停",
    "awaiting_input": "待补充",
    "awaiting_review": "待复核",
    "completed": "已完成",
    "partial": "部分完成",
    "failed": "失败",
    "cancelled": "已取消",
    "committed": "已提交",
    "needs_human": "待人工",
    "locked": "已锁定",
    "conflict": "版本冲突",
    "excluded": "已排除",
    "no_change": "无须修改",
    "passed": "已通过",
    "reviewed": "已审校",
}


def status_badge(status: str) -> str:
    """Colorize a status string in the amber palette."""
    label = STATUS_ZH.get(status, status)
    if status in {"completed", "committed", "passed", "reviewed", "no_change", "applied"}:
        return fg("gold", f"● {label}", bold=True)
    if status in {"awaiting_input", "awaiting_review", "needs_human", "paused", "partial"}:
        return fg("coral", f"◐ {label}", bold=True)
    if status in {"failed", "conflict", "cancelled", "locked"}:
        return fg("danger", f"✕ {label}", bold=True)
    return fg("amber", f"◌ {label}")


def render_job_summary(job: dict[str, Any]) -> str:
    """Render a compact panel summarizing job progress, counts, and actions."""
    width = terminal_panel_width(108)
    counts = job.get("counts", {})
    budget = job.get("budget", {})
    lines = [
        f"{fg('muted', 'Job ID  ')} {fg('amber_bright', job['job_id'][:12])}  "
        f"{fg('muted', '类型')} {fg('cream', job.get('source_kind', 'naev'))}  "
        f"{fg('muted', '状态')} {status_badge(job['status'])}",
        f"{fg('muted', '统计    ')} 通过 {fg('gold', str(counts.get('passed', 0)))} · "
        f"可导出 {fg('amber_bright', str(counts.get('exportable', 0)))} · "
        f"待复核 {fg('coral', str(counts.get('awaiting_review', 0)))} · "
        f"调用 {fg('amber', str(budget.get('reserved_or_spent', 0)))}/{budget.get('limit_calls', '-')}",
    ]
    if job.get("reason"):
        lines.append(f"{fg('muted', '原因    ')} {fg('coral', str(job['reason']))}")
    if job.get("steps"):
        step_str = " → ".join(
            f"{fg('amber_bright', s['intent'])}({STATUS_ZH.get(s['status'], s['status'])})"
            for s in job["steps"]
        )
        lines.append(f"{fg('muted', '步骤    ')} {step_str}")
    if job.get("graph_build"):
        gb = job["graph_build"]
        lines.append(f"{fg('muted', '建图进度')} {len(gb.get('completed', []))}/{gb.get('batches', 0)} 批")
    if job.get("next_actions"):
        lines.append(f"{fg('muted', '可执行  ')} {fg('amber', ', '.join(job['next_actions']))}")
    out = ["\n".join(draw_panel(lines, width=width, title="任务状态"))]
    items = job.get("items") or []
    if items:
        rows = []
        for idx, item in enumerate(items, 1):
            reason = item.get("display_reason") or item.get("reason") or "-"
            rows.append([
                str(idx),
                truncate_display(item["occurrence_id"], 22),
                status_badge(item["status"]),
                truncate_display((item.get("source") or "").replace("\n", " "), 28),
                truncate_display((item.get("target") or "-").replace("\n", " "), 28),
                truncate_display(str(reason), 22),
            ])
        out.append(draw_table(["#", "Occurrence", "状态", "原文", "译文", "说明"], rows))
    return "\n".join(out)


def render_sse_event(event_name: str, data: dict[str, Any]) -> str | None:
    """Format a live SSE event into a single concise amber timeline line."""
    if event_name == "progress":
        stage = data.get("stage", "step")
        occ = (data.get("occurrence_id") or "")[:20]
        extra = f" → {fg('coral', data['result'])}" if data.get("result") else ""
        return f"  {fg('amber_dim', '├─')} {fg('amber_bright', f'[{stage}]'):<22} {fg('cream', occ)}{extra}"
    if event_name == "graph_batch":
        return f"  {fg('amber_dim', '├─')} {fg('amber_bright', '[graph_batch]')} 完成一批实体/关系提取"
    if event_name == "approved":
        occ = (data.get("occurrence_id") or "")[:20]
        return f"  {fg('gold', '✓')} {fg('gold', '[approved]')} {fg('cream', occ)} v{data.get('version', '?')}"
    if event_name in {"planned", "step", "job"}:
        status = data.get("status") or event_name
        return f"  {fg('amber', '◈')} {fg('amber', f'[{event_name}]')} {status_badge(status)}"
    return None


def render_evidence(evidence: dict[str, Any]) -> str:
    """Render retrieved vs actual ContextPack, Pi lookups, and review verdicts."""
    width = terminal_panel_width(108)
    lines = [f"{fg('muted', 'Occurrence:')} {fg('amber_bright', evidence['occurrence_id'])}  "
             f"{fg('muted', '状态:')} {status_badge(evidence['status'])}"]
    if evidence.get("reason"):
        lines.append(f"{fg('muted', '原因:')} {fg('coral', str(evidence['reason']))}")

    lookups = evidence.get("source_lookups") or []
    lines.append("---")
    lines.append(fg("amber_bright", f"主动取证调用 ({len(lookups)})", bold=True))
    if not lookups:
        lines.append(fg("muted", "  (未触发 search_source_graph)"))
    for entry in lookups:
        q = entry.get("effective_query") or entry.get("args", {}).get("query", "")
        res = entry.get("result", {})
        lines.append(f"  {fg('amber', '◎')} {entry.get('name')} query={fg('cream', repr(q))} → "
                     f"{fg('coral', res.get('status', '?'))} ({len(res.get('evidence', []))} 条)")

    actual_items = (evidence.get("actual_context") or {}).get("items", [])
    actual_sources = evidence.get("actual_sources") or []
    lines.append("---")
    lines.append(fg("amber_bright", f"实际送入模型的上下文 ({len(actual_items)} 项)", bold=True))
    for item in actual_items:
        lines.append(f"  {fg('amber', item['id'])}: {item['text'].replace(chr(10), ' ')}")
    for src in actual_sources:
        loc = src.get("location") or {}
        loc_str = f"{loc.get('path', '')}:{loc.get('line', loc.get('row', ''))}" if loc else ""
        lines.append(f"  {fg('gold', src['occurrence_id'])}@{src.get('source_version', '?')} {fg('muted', loc_str)}")
        lines.append(f"    {fg('cream', src.get('quote', '').replace(chr(10), ' '))}")

    retrieved = evidence.get("retrieved") or []
    lines.append("---")
    lines.append(fg("muted", f"初始检索候选池: {len(retrieved)} 条"))
    for cand in retrieved[:6]:
        lines.append(f"  {fg('muted', cand['id'])}: {cand['text'].replace(chr(10), ' ')}")
    if len(retrieved) > 6:
        lines.append(fg("muted", f"  ... 其余 {len(retrieved) - 6} 条省略"))

    reviews = evidence.get("reviews") or []
    if reviews:
        lines.append("---")
        lines.append(fg("amber_bright", "Pi Reviewer 裁决历史", bold=True))
        for idx, rev in enumerate(reviews, 1):
            lines.append(f"  #{idx} [{fg('coral', rev.get('verdict', '?'))}] {rev.get('reason', '')}")
    return "\n".join(draw_panel(lines, width=width, title="证据溯源"))


def render_po_report(report: dict[str, Any]) -> str:
    """Render PO validation summary, conflicts, and exclusions."""
    val = report.get("validation", {})
    ok = val.get("ok", False)
    head = (
        f"{fg('gold', '✓ 校验通过') if ok else fg('danger', '✕ 校验失败')} · "
        f"级别 {fg('amber', val.get('level', '-'))} · "
        f"写回 {fg('amber_bright', str(report.get('applied', 0)))} 个键 · "
        f"可下载={'是' if report.get('download_ready') else '否'}"
    )
    out = [head]
    if val.get("diagnostics"):
        out.append(fg("coral", val["diagnostics"].strip()))
    conflicts = report.get("conflicts") or []
    if conflicts:
        out.append(fg("coral", f"\n发现 {len(conflicts)} 处共享 PO 键译文冲突："))
        for idx, entry in enumerate(conflicts, 1):
            out.append(f"  [{idx}] {fg('amber_bright', entry['source'])} (entry={entry['entry_id'][:10]})")
            for v_idx, ver in enumerate(entry["versions"], 1):
                out.append(f"      ({v_idx}) {ver['occurrence_id']} v{ver['version']}: {fg('cream', ver['target'])}")
    exclusions = report.get("exclusions") or []
    if exclusions:
        rows = [[ex.get("occurrence_id") or ex.get("entry_id") or "-", ex.get("reason", "-")] for ex in exclusions]
        out.append(draw_table(["排除对象", "原因"], rows, wrap=True))
    return "\n".join(out)


def render_maintenance_plan(plan: dict[str, Any]) -> str:
    """Render P4 glossary maintenance preview/applied plan."""
    rule = plan["rule"]
    header = (
        f"计划 {fg('amber_bright', plan['plan_id'][:12])} · "
        f"{fg('cream', rule['source'])} → {fg('gold', rule['target'])} (v{rule['version']}) · "
        f"命中 {rule['matched_occurrences']} 处 · {status_badge(plan['status'])}"
    )
    rows = []
    for item in plan.get("items", []):
        st = item.get("result") or item["status"]
        after = item.get("after") or item.get("proposed") or "-"
        rows.append([
            truncate_display(item["occurrence_id"], 20),
            status_badge(st),
            truncate_display(item["before"], 24),
            truncate_display(after, 24),
            ",".join(item.get("discovery", [])),
            str(item.get("model_calls", 0)),
        ])
    table = draw_table(["Occurrence", "状态", "改前", "拟改/改后", "发现来源", "调用"], rows) if rows else fg("muted", "(无受影响条目)")
    return f"{header}\n{table}"


def render_replay_report(report: dict[str, Any]) -> str:
    """Render P5 baseline vs candidate replay comparison table."""
    m = report["metrics"]
    b, a = m["before"], m["after"]
    passed = fg("gold", "PASS") if report["passed"] else fg("danger", "FAIL")
    title = (
        f"回放 {fg('amber_bright', report['replay_id'][:12])} · 模式={report['mode']} · "
        f"数据集={report['dataset']} · live={report['live']} · 结果={passed} · "
        f"缺口解决={report.get('evidence_gaps_resolved', 0)}"
    )
    rows = [
        ["样本数", str(b["cases"]), str(a["cases"])],
        ["证据命中率", str(b["evidence_hit_rate"]), str(a["evidence_hit_rate"])],
        ["越界引用", str(b["outside_scope"]), str(a["outside_scope"])],
        ["硬规则违规", str(b["hard_rule_violations"]), str(a["hard_rule_violations"])],
        ["不安全提交", str(b["unsafe_commits"]), str(a["unsafe_commits"])],
        ["转人工率", f"{b['human_rate']:.2f}", f"{a['human_rate']:.2f}"],
        ["模型调用数", str(b["calls"]), str(a["calls"])],
        ["耗时(ms)", str(b["duration_ms"]), str(a["duration_ms"])],
    ]
    return f"{title}\n{draw_table(['指标', '现行策略 (before)', '候选策略 (after)'], rows)}"


def render_model_overview(profiles: list[dict[str, Any]], bindings: dict[str, str],
                          roles: list[str], env_status: dict[str, bool]) -> str:
    """Render registered model profiles, credential status, and per-module role bindings."""
    out = []
    if profiles:
        p_rows = []
        for idx, prof in enumerate(profiles, 1):
            env_name = prof.get("api_key_env") or "(none/local)"
            ready = "✓" if (not prof.get("api_key_env") or env_status.get(env_name, False)) else "?"
            p_rows.append([
                str(idx),
                prof["profile_id"][:10],
                prof["name"],
                prof["model"],
                truncate_display(str(prof["base_url"]), 34),
                f"{env_name} [{ready}]",
            ])
        out.append(fg("amber_bright", "已注册模型接口 (Model Profiles)", bold=True))
        out.append(draw_table(["#", "ID", "名称", "模型", "Base URL", "凭据环境变量"], p_rows, wrap=True))
    else:
        out.append(fg("muted", "当前工作区尚未注册自定义 Model Profile，使用默认配置 configs/p0.json (deepseek-v4.1-flash-1017)。"))

    by_id = {p["profile_id"]: p for p in profiles}
    r_rows = []
    for role in roles:
        pid = bindings.get(role)
        prof = by_id.get(pid) if pid else None
        r_rows.append([
            fg("amber", role),
            f"{prof['name']} ({prof['model']})" if prof else fg("muted", "(默认网关/全局模型)"),
            pid[:10] if pid else "-",
        ])
    out.append(fg("amber_bright", "\n各模块角色绑定 (Module Role Bindings)", bold=True))
    out.append(draw_table(["模块角色", "当前绑定模型", "Profile ID"], r_rows))
    return "\n".join(out)
