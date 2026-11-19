"""Interactive workflows for SSE watching, clarification, review, and module API setup."""

from collections.abc import Callable
from dataclasses import dataclass
import getpass
import os
from pathlib import Path
import subprocess
from tempfile import NamedTemporaryFile
from typing import Any

from .client import ApiError, LoreClient
from .state import SessionState, save_cli_config
from .theme import draw_panel, fg, terminal_panel_width
from .views import render_evidence, render_job_summary, render_model_overview, render_sse_event

MODULE_ROLES = ("extractor", "selector", "translator", "reviewer", "repairer", "intent")
ROLE_HINTS = {
    "extractor": "P6 实体关系建图提取",
    "selector": "上下文软证据选择",
    "translator": "主译文生成",
    "reviewer": "Pi Agent 审校与取证",
    "repairer": "硬检查/审校问题修复",
    "intent": "自然语言意图识别",
}


@dataclass
class ConsoleIO:
    """Injectable terminal IO hooks for clean REPL testing."""

    ask: Callable[[str], str] = input
    ask_secret: Callable[[str], str] = getpass.getpass
    emit: Callable[[str], None] = print


def prompt_value(io: ConsoleIO, label: str, default: str = "") -> str:
    """Prompt for text with an optional default shown in amber."""
    hint = f" [{fg('muted', default)}]" if default else ""
    answer = io.ask(f"  {fg('amber', '?')} {fg('cream', label)}{hint}: ").strip()
    return answer or default


def open_external_editor(initial: str) -> str | None:
    """Edit multiline game text safely using $EDITOR."""
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
    with NamedTemporaryFile("w+", suffix=".txt", encoding="utf-8", delete=False) as tmp:
        path = Path(tmp.name)
        tmp.write(initial)
    try:
        subprocess.run([editor, str(path)], check=True)
        return path.read_text(encoding="utf-8")
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        path.unlink(missing_ok=True)


def watch_job(client: LoreClient, state: SessionState, job_id: str, io: ConsoleIO, *,
              auto_interact: bool = True) -> dict[str, Any]:
    """Watch one job and submit at most one round of interactive clarification."""
    state.active_job_id = job_id
    job = _watch_once(client, state, job_id, io)
    if not auto_interact:
        return job

    if job["status"] == "awaiting_input":
        if not handle_clarification(client, state, job, io):
            return job
        job = _watch_once(client, state, job_id, io)
        if job["status"] == "awaiting_input":
            outstanding = ", ".join(f"{key} ({field['reason']})" for key, field in (job.get("missing") or {}).items())
            io.emit(fg("coral", f"仍需补充信息：{outstanding}。用 /watch 继续补充，或用 /cancel 取消。"))
    if job["status"] == "awaiting_review":
        ans = prompt_value(io, "存在待人工复核条目，是否立即进入复核向导？(y/n)", "y").lower()
        if ans in {"y", "yes", ""}:
            return handle_review(client, state, job, io)
    return job


def _watch_once(client: LoreClient, state: SessionState, job_id: str, io: ConsoleIO) -> dict[str, Any]:
    non_runnable = {"awaiting_input", "awaiting_review", "paused", "completed", "partial", "failed", "cancelled"}
    job = client.job_detail(job_id)
    if job["status"] not in non_runnable:
        io.emit(fg("amber", f"\n◈ 监听任务 {job_id[:12]}（Ctrl+C 可中断监听，任务在后台继续）"))
        try:
            while True:
                after = state.event_cursors.get(job_id, 0)
                stopped = False
                for event_id, event_name, payload in client.stream_events(job_id, after=after):
                    state.event_cursors[job_id] = max(state.event_cursors.get(job_id, 0), event_id)
                    line = render_sse_event(event_name, payload)
                    if line:
                        io.emit(line)
                    if event_name == "job" and payload.get("status") in non_runnable:
                        job = client.job_detail(job_id)
                        if job["status"] in non_runnable:
                            stopped = True
                            break
                if stopped:
                    break
                job = client.job_detail(job_id)
                if job["status"] in non_runnable:
                    break
        except KeyboardInterrupt:
            io.emit(fg("coral", "\n  已退出前台监听，稍后可用 /watch 继续查看。"))
        except ApiError as err:
            io.emit(fg("danger", f"  SSE 中断: {err}"))
        job = client.job_detail(job_id)

    io.emit(render_job_summary(job))
    return job


def handle_clarification(client: LoreClient, state: SessionState, job: dict[str, Any],
                         io: ConsoleIO) -> bool:
    """Collect missing intent, target language, targets, or term fields and resume the job."""
    missing = job.get("missing") or {}
    if not missing:
        return False
    io.emit(fg("coral", "\n◐ 任务需要补充信息以继续："))
    additions: dict[str, Any] = {}
    if "intent" in missing:
        choices = missing["intent"].get("choices", [])
        raw = prompt_value(io, f"请选择意图 ({', '.join(choices)})", "translate,export")
        additions["intents"] = [part.strip() for part in raw.split(",") if part.strip()]
    for key, label in (("source_lang", "原文语言"), ("target_lang", "目标语言")):
        if key in missing:
            choices = missing[key].get("choices", [])
            default = state.target_lang if key == "target_lang" and state.target_lang in choices else next(iter(choices), "")
            additions[key] = prompt_value(io, f"{label} ({'/'.join(choices)})", default)
    invalid_targets = {key: field for key, field in missing.items()
                       if field.get("reason") in {"ambiguous_target", "target_not_in_scope"}}
    if "targets" in missing or invalid_targets:
        if invalid_targets:
            io.emit(fg("coral", "  未能唯一匹配资源：" + ", ".join(invalid_targets)))
        elif state.selected_ids:
            use_sel = prompt_value(io, f"使用当前已选 {len(state.selected_ids)} 个资源？(y/n)", "y").lower()
            if use_sel in {"y", "yes", ""}:
                additions["targets"] = list(state.selected_ids)
        if "targets" not in additions:
            fields = [missing.get("targets", {}), *invalid_targets.values()]
            choices = list(dict.fromkeys(choice for field in fields for choice in field.get("choices", [])))[:10]
            if choices:
                io.emit(fg("muted", "  可选 ID 示例: " + ", ".join(choices)))
            raw = prompt_value(io, "输入全部目标 occurrence ID（逗号分隔，留空返回）")
            additions["targets"] = [part.strip() for part in raw.split(",") if part.strip()]
    if "term" in missing:
        src = prompt_value(io, "术语英文原文 (term.source)")
        tgt = prompt_value(io, "术语新译法 (term.target)")
        prev = prompt_value(io, "旧译法 (可选)")
        additions["term"] = {"source": src, "target": tgt, **({"previous_target": prev} if prev else {})}

    unresolved = missing.keys() - additions.keys() - invalid_targets.keys()
    term = additions.get("term")
    if not additions or not all(additions.values()) or unresolved or (term and not (term["source"] and term["target"])):
        io.emit(fg("muted", "  已返回命令行。用 /watch 补充信息，或用 /cancel 取消任务。"))
        return False
    client.continue_job(job["job_id"], additions)
    return True


def handle_review(client: LoreClient, state: SessionState, job: dict[str, Any],
                  io: ConsoleIO) -> dict[str, Any]:
    """Walk through awaiting_review items with evidence inspection and $EDITOR support."""
    job_id = job["job_id"]
    pending = [item for item in job.get("items", []) if item["status"] == "needs_human"]
    if not pending:
        io.emit(fg("muted", "当前任务没有 status=needs_human 的条目。"))
        return job

    acted = False
    for idx, item in enumerate(pending, 1):
        occ = item["occurrence_id"]
        io.emit(fg("amber_bright", f"\n[{idx}/{len(pending)}] 复核条目 {occ}"))
        try:
            evidence = client.item_evidence(job_id, item["step"], occ)
            io.emit(render_evidence(evidence))
        except ApiError as err:
            io.emit(fg("coral", f"  获取证据失败: {err}"))

        io.emit(draw_panel_text("原文", item.get("source", "")))
        io.emit(draw_panel_text("当前草稿", item.get("target") or "(空)"))
        io.emit(fg("muted", "  操作: [a]批准当前稿  [e]调用$EDITOR编辑  [t]单行改译  [s]跳过  [p]接受已完成部分  [q]退出"))
        choice = prompt_value(io, "选择动作", "a").lower()
        if choice == "q":
            break
        if choice == "s":
            continue
        if choice == "p":
            client.accept_partial(job_id)
            acted = True
            break

        target = item.get("target") or ""
        if choice == "e":
            edited = open_external_editor(target)
            if edited is None:
                io.emit(fg("danger", "  外部编辑器未成功保存，改用单行输入。"))
                target = prompt_value(io, "输入新译文", target)
            else:
                target = edited
        elif choice == "t":
            target = prompt_value(io, "输入新译文", target)
        elif choice != "a":
            io.emit(fg("coral", "  未识别动作，已跳过。"))
            continue

        reason = prompt_value(io, "修改/批准原因 (可选，将记录到 P5 反馈)", "")
        payload = {
            "target": target,
            "expected_version": item["expected_version"],
            "expected_draft_hash": item.get("draft_hash"),
            **({"reason": reason} if reason else {}),
        }
        try:
            res = client.approve_item(job_id, item["step"], occ, payload)
            io.emit(fg("gold", f"  ✓ 已提交: {res.get('status')} (v{res.get('version', item['expected_version'])})"))
            acted = True
        except ApiError as err:
            io.emit(fg("danger", f"  ✕ 批准失败: {err.reason}"))

    if acted:
        return watch_job(client, state, job_id, io, auto_interact=False)
    return client.job_detail(job_id)


def draw_panel_text(title: str, content: str) -> str:
    """Render short multiline text in a terminal-adapted sub-panel."""
    lines = [fg("cream", line) for line in (content.splitlines() or [""])]
    return "\n".join(draw_panel(lines, width=terminal_panel_width(104), title=title, border="amber_dark"))


def _env_status(profiles: list[dict[str, Any]], session_env: dict[str, str]) -> dict[str, bool]:
    status = {"DEEPSEEK_API_KEY": bool(session_env.get("DEEPSEEK_API_KEY") or os.environ.get("DEEPSEEK_API_KEY"))}
    for prof in profiles:
        key_env = prof.get("api_key_env")
        if key_env:
            status[key_env] = bool(session_env.get(key_env) or os.environ.get(key_env))
    return status


def handle_api_wizard(client: LoreClient, state: SessionState, io: ConsoleIO,
                      restart_spawn_fn: Callable[[], LoreClient] | None = None) -> LoreClient:
    """Configure backend URL, per-module model profiles, API keys (in-memory), and role bindings."""
    while True:
        try:
            profiles = client.list_models().get("profiles", [])
        except ApiError:
            profiles = []
        bindings: dict[str, str] = {}
        if state.project_id != "naev":
            try:
                bindings = client.project_detail(state.project_id).get("models", {})
            except ApiError:
                bindings = {}

        io.emit("\n" + render_model_overview(profiles, bindings, list(MODULE_ROLES),
                                             _env_status(profiles, state.session_env)))
        io.emit(fg("muted", f"\n当前工作台 API: {state.api_url}  ·  当前项目: {state.project_id}"))
        io.emit(fg("amber_dim", "  [1] 注册模型接口 Profile (base_url / model / api_key_env)"))
        io.emit(fg("amber_dim", "  [2] 分模块绑定模型 (extractor / selector / translator / reviewer / repairer / intent)"))
        io.emit(fg("amber_dim", "  [3] 设置 API Key 环境变量（仅驻留会话内存，不落盘）"))
        io.emit(fg("amber_dim", "  [4] 测试模型接口探针 (/probe)"))
        io.emit(fg("amber_dim", "  [5] 切换工作台后端 API 地址"))
        io.emit(fg("amber_dim", "  [q] 返回 REPL"))
        action = prompt_value(io, "选择配置项", "q").lower()

        if action in {"q", ""}:
            return client
        if action == "1":
            client = _register_profile_flow(client, state, io, restart_spawn_fn)
        elif action == "2":
            _bind_roles_flow(client, state, profiles, bindings, io)
        elif action == "3":
            client = _set_api_key_flow(client, state, io, restart_spawn_fn)
        elif action == "4":
            _probe_profile_flow(client, profiles, io)
        elif action == "5":
            new_url = prompt_value(io, "输入新的工作台后端 URL", state.api_url).rstrip("/")
            if new_url and new_url != state.api_url:
                client.close()
                state.api_url = new_url
                client = LoreClient(new_url)
                save_cli_config(state)
                io.emit(fg("gold" if client.ping() else "coral",
                           f"  已切换到 {new_url} ({'已连通' if client.ping() else '暂不可达'})"))
    return client


def _register_profile_flow(client: LoreClient, state: SessionState, io: ConsoleIO,
                           restart_spawn_fn: Callable[[], LoreClient] | None) -> LoreClient:
    io.emit(fg("amber_bright", "\n注册新模型 Profile："))
    io.emit(fg("muted", "  说明：远程 HTTPS 需使用 DEEPSEEK_API_KEY 或 WORKBENCH_MODEL_<TAG>_KEY（配套 _URL）。"))
    name = prompt_value(io, "Profile 名称", "deepseek-default")
    model = prompt_value(io, "模型 ID", "deepseek-v4.1-flash-1017")
    base_url = prompt_value(io, "Base URL", "https://tokendance.space/gateway/v1").rstrip("/")
    mode = prompt_value(io, "凭据模式: [1]DEEPSEEK_API_KEY  [2]自定义WORKBENCH_MODEL_*_KEY  [3]本地无密钥", "1")
    api_key_env: str | None = None
    if mode == "1":
        api_key_env = "DEEPSEEK_API_KEY"
    elif mode == "2":
        tag = prompt_value(io, "自定义标签 (大写字母/数字，例如 QWEN)", "CUSTOM").upper()
        api_key_env = f"WORKBENCH_MODEL_{tag}_KEY"
        url_env = f"WORKBENCH_MODEL_{tag}_URL"
        state.session_env[url_env] = base_url
        os.environ[url_env] = base_url
        secret = io.ask_secret(f"  {fg('amber', '?')} 输入 {api_key_env} (不回显，留空跳过): ").strip()
        if secret:
            state.session_env[api_key_env] = secret
            os.environ[api_key_env] = secret
        if state.spawned and restart_spawn_fn:
            io.emit(fg("amber", "  正在用新环境变量重启托管服务以注册端点…"))
            client = restart_spawn_fn()
        else:
            io.emit(fg("coral", f"  提示：若连接外部服务，请确保服务端已 export {url_env}={base_url} 与 {api_key_env}"))

    timeout = int(prompt_value(io, "超时秒数", "75"))
    payload = {"name": name, "model": model, "base_url": base_url,
               "api_key_env": api_key_env, "timeout_seconds": timeout}
    try:
        prof = client.register_model(payload)
        io.emit(fg("gold", f"  ✓ 已注册 Profile: {prof['name']} ({prof['profile_id'][:12]})"))
    except ApiError as err:
        io.emit(fg("danger", f"  ✕ 注册失败: {err.reason}"))
    return client


def _bind_roles_flow(client: LoreClient, state: SessionState, profiles: list[dict[str, Any]],
                     current: dict[str, str], io: ConsoleIO):
    if state.project_id == "naev":
        io.emit(fg("coral", "  提示：Naev 默认工作区固定读取 configs/p0.json；请先用 /project 切换或新建 P6 项目后分模块绑定模型。"))
        return
    if not profiles:
        io.emit(fg("coral", "  请先使用 [1] 注册至少一个 Model Profile。"))
        return
    io.emit(fg("amber_bright", "\n为各模块角色选择 Profile 编号（回车保持不变，'-' 清除绑定）："))
    for idx, prof in enumerate(profiles, 1):
        io.emit(f"  [{idx}] {fg('amber', prof['name'])} ({prof['model']})")
    next_bindings = dict(current)
    for role in MODULE_ROLES:
        hint = ROLE_HINTS[role]
        cur_id = current.get(role)
        cur_idx = next((str(i) for i, p in enumerate(profiles, 1) if p["profile_id"] == cur_id), "")
        raw = prompt_value(io, f"模块 {role} ({hint})", cur_idx)
        if raw == "-":
            next_bindings.pop(role, None)
        elif raw.isdigit() and 1 <= int(raw) <= len(profiles):
            next_bindings[role] = profiles[int(raw) - 1]["profile_id"]
    try:
        client.bind_model_roles(state.project_id, next_bindings)
        io.emit(fg("gold", f"  ✓ 已更新项目 {state.project_id[:10]} 的模块模型绑定。"))
    except ApiError as err:
        io.emit(fg("danger", f"  ✕ 绑定失败: {err.reason}"))


def _set_api_key_flow(client: LoreClient, state: SessionState, io: ConsoleIO,
                      restart_spawn_fn: Callable[[], LoreClient] | None) -> LoreClient:
    env_name = prompt_value(io, "凭据变量名", "DEEPSEEK_API_KEY").upper()
    if env_name.startswith("WORKBENCH_MODEL_") and env_name.endswith("_KEY"):
        url_env = env_name.removesuffix("_KEY") + "_URL"
        url_val = prompt_value(io, f"对应端点变量 {url_env}", state.session_env.get(url_env, ""))
        if url_val:
            state.session_env[url_env] = url_val
            os.environ[url_env] = url_val
    secret = io.ask_secret(f"  {fg('amber', '?')} 输入 {env_name}（隐藏回显，不写磁盘）: ").strip()
    if not secret:
        io.emit(fg("muted", "  未更改密钥。"))
        return client
    state.session_env[env_name] = secret
    os.environ[env_name] = secret
    if state.spawned and restart_spawn_fn:
        io.emit(fg("amber", "  正在重启托管的本地服务以加载新密钥…"))
        return restart_spawn_fn()
    io.emit(fg("coral", f"  已保存至当前 CLI 会话。若连接外部 run_api.py，请在该服务端环境中 export {env_name}。"))
    return client


def _probe_profile_flow(client: LoreClient, profiles: list[dict[str, Any]], io: ConsoleIO):
    if not profiles:
        io.emit(fg("coral", "  暂无已注册 Model Profile。"))
        return
    for idx, prof in enumerate(profiles, 1):
        io.emit(f"  [{idx}] {prof['name']} ({prof['model']})")
    raw = prompt_value(io, "输入 Profile 编号", "1")
    if not raw.isdigit() or not 1 <= int(raw) <= len(profiles):
        io.emit(fg("danger", "  无效编号。"))
        return
    role = prompt_value(io, f"测试模块角色 ({'/'.join(MODULE_ROLES)})", "translator")
    try:
        res = client.probe_model(profiles[int(raw) - 1]["profile_id"], role)
        badge = fg("gold", "通过") if res.get("passed") else fg("danger", f"失败: {res.get('reason')}")
        io.emit(f"  探针结果: {badge} · model={res.get('model')} · calls={res.get('calls')}")
    except ApiError as err:
        io.emit(fg("danger", f"  ✕ 探针请求失败: {err.reason}"))
