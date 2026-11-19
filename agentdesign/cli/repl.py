"""Interactive amber REPL router for all LoRE workbench modules (P0-P6)."""

from collections.abc import Callable
import json
from pathlib import Path
import re
import shlex
import sys
from typing import Any

from .client import ApiError, LoreClient
from .state import SessionState, save_cli_config
from .theme import build_startup_banner, draw_panel, draw_table, fg, terminal_panel_width, truncate_display, use_color
from .views import (
    render_evidence,
    render_job_summary,
    render_maintenance_plan,
    render_po_report,
    render_replay_report,
)
from .workflows import (
    ConsoleIO,
    handle_api_wizard,
    handle_review,
    prompt_value,
    watch_job,
)

COMMANDS = (
    "/help", "/status", "/api", "/project", "/import", "/search", "/select",
    "/graph", "/text", "/run", "/batch", "/watch", "/pause", "/resume",
    "/cancel", "/retry", "/review", "/evidence", "/version", "/export",
    "/glossary", "/memory", "/feedback", "/policy", "/clear", "/exit",
)
DIRECT_TRANSLATION = re.compile(
    r"(?:请(?:你)?\s*)?(?:帮我\s*)?(?:翻译(?:一下)?(?:\s*(?:成|为|到)\s*"
    r"(?P<language>英文|英语|简体中文|中文|en|zh))?|(?P<direction>中译英|英译中))"
    r"\s*[:：]\s*(?P<source>.*)", re.IGNORECASE | re.DOTALL,
)


def _setup_readline():
    """Enable command history and slash-command completion via stdlib readline."""
    try:
        import readline
    except ImportError:
        return
    def complete(text: str, state_idx: int) -> str | None:
        if not text.startswith("/"):
            return None
        matches = [cmd for cmd in COMMANDS if cmd.startswith(text)]
        return matches[state_idx] if state_idx < len(matches) else None

    readline.set_completer_delims(" \t\n")
    readline.set_completer(complete)
    readline.parse_and_bind("tab: complete")


class LoreREPL:
    """Amber REPL dispatching natural language and modular slash commands."""

    def __init__(self, client: LoreClient, state: SessionState, *, io: ConsoleIO | None = None,
                 restart_spawn_fn: Callable[[], LoreClient] | None = None, show_banner: bool = True):
        self.client = client
        self.state = state
        self.io = io or ConsoleIO()
        self.restart_spawn_fn = restart_spawn_fn
        self.show_banner = show_banner

    def banner_info(self) -> dict[str, str]:
        """Collect compact runtime metadata for the startup LoRE card."""
        connected = self.client.ping()
        model_label = "deepseek-v4.1-flash-1017"
        if self.state.spawn_args.get("offline"):
            model_label += " (offline)"
        rules_count = 0
        policy_id = "baseline"
        if connected:
            try:
                rules_count = len(self.client.glossary().get("rules", []))
            except ApiError:
                pass
            try:
                policy_id = self.client.policy_status().get("active", {}).get("release_id", "baseline")[:10]
            except ApiError:
                pass
        proj = self.state.project_name if self.state.project_id == "naev" else f"{self.state.project_name} ({self.state.project_id[:8]})"
        api_status = f"{self.state.api_url} ({'online' if connected else 'unreachable'})"
        return {
            "model": model_label,
            "project": proj,
            "memory": f"{rules_count} rules · policy {policy_id}",
            "api": api_status,
        }

    def print_banner(self):
        self.io.emit(build_startup_banner(self.banner_info()))

    def prompt_str(self) -> str:
        proj = "naev" if self.state.project_id == "naev" else self.state.project_id[:6]
        sel = f"·{len(self.state.selected_ids)}项" if self.state.selected_ids else ""
        job = f"·#{self.state.active_job_id[:6]}" if self.state.active_job_id else ""
        raw = f"\n{fg('amber_bright', '◈', bold=True)} {fg('amber', f'[{proj}{sel}{job}]')} {fg('coral', '>', bold=True)} "
        if use_color() and sys.stdin.isatty():
            return re.sub(r"(\x1b\[[0-9;]*m)", lambda match: f"\x01{match.group(1)}\x02", raw)
        return raw

    def run(self):
        """Main interactive REPL loop."""
        _setup_readline()
        if self.show_banner:
            self.print_banner()
        while True:
            try:
                raw = self.io.ask(self.prompt_str()).strip()
            except (EOFError, KeyboardInterrupt):
                self.io.emit(fg("amber", "\n再见 · LoRE session closed."))
                break
            if not raw:
                continue
            try:
                if not self.dispatch(raw):
                    self.io.emit(fg("amber", "再见 · LoRE session closed."))
                    break
            except ApiError as err:
                self.io.emit(fg("danger", f"✕ 接口错误 ({err.status_code}): {err.reason}"))
            except Exception as err:
                self.io.emit(fg("danger", f"✕ 执行异常: {type(err).__name__}: {err}"))

    def dispatch(self, raw: str) -> bool:
        """Handle one line of user input; return False to exit."""
        if not raw.startswith("/"):
            self._handle_natural_language(raw)
            return True
        try:
            parts = shlex.split(raw)
        except ValueError as err:
            self.io.emit(fg("danger", f"✕ 命令解析失败: {err}"))
            return True
        cmd, args = parts[0].lower(), parts[1:]
        handlers: dict[str, Callable[[list[str]], bool | None]] = {
            "/exit": lambda _: False,
            "/quit": lambda _: False,
            "/help": self._cmd_help,
            "/clear": lambda _: self.print_banner() or True,
            "/status": self._cmd_status,
            "/api": self._cmd_api,
            "/project": self._cmd_project,
            "/import": self._cmd_import,
            "/search": self._cmd_search,
            "/select": self._cmd_select,
            "/graph": self._cmd_graph,
            "/text": self._cmd_text,
            "/run": self._cmd_run,
            "/batch": self._cmd_batch,
            "/watch": self._cmd_watch,
            "/pause": lambda _: self._job_control("pause"),
            "/resume": self._cmd_resume,
            "/cancel": lambda _: self._job_control("cancel"),
            "/retry": self._cmd_retry,
            "/review": self._cmd_review,
            "/evidence": self._cmd_evidence,
            "/version": self._cmd_version,
            "/export": self._cmd_export,
            "/glossary": self._cmd_glossary,
            "/memory": self._cmd_memory,
            "/feedback": self._cmd_feedback,
            "/policy": self._cmd_policy,
        }
        fn = handlers.get(cmd)
        if fn is None:
            self.io.emit(fg("coral", f"未知命令 {cmd}，输入 /help 查看所有命令。"))
            return True
        result = fn(args)
        return True if result is None else bool(result)

    # ---- Natural Language & Direct Translation ----
    def _handle_natural_language(self, text: str):
        direct = DIRECT_TRANSLATION.fullmatch(text)
        if direct:
            source, language, direction = (direct[name] for name in ("source", "language", "direction"))
            if not source.strip():
                self.io.emit(fg("coral", "请在冒号后输入需要翻译的原文。"))
                return
            if language:
                target = "en" if language.lower() in {"英文", "英语", "en"} else "zh"
            elif direction:
                target = "en" if direction == "中译英" else "zh"
            else:
                target = "en" if re.search(r"[\u3400-\u9fff]", source) else "zh"
            self._submit_text_job(source, target)
            return
        has_scope = bool(self.state.selected_ids or self.state.upload_id or self.state.project_id != "naev")
        if not has_scope:
            mode = prompt_value(self.io, "当前未选中资源：[1] 作为工作台自然语言任务  [2] 作为自由文本直译  [q] 取消", "2").lower()
            if mode == "q":
                return
            if mode == "2":
                self._submit_text_job(text, self.state.target_lang)
                return
        payload: dict[str, Any] = {
            "text": text,
            "selection": {
                "project": self.state.project_id,
                "source_ids": self.state.selected_ids,
                "target_lang": self.state.target_lang,
            },
        }
        if self.state.upload_id and self.state.project_id == "naev":
            payload["upload_id"] = self.state.upload_id
        job = self.client.create_job(payload)
        watch_job(self.client, self.state, job["job_id"], self.io)

    def _submit_text_job(self, text: str, target_lang: str):
        job = self.client.create_job({"source_text": text, "target_lang": target_lang})
        watch_job(self.client, self.state, job["job_id"], self.io)

    def _cmd_text(self, args: list[str]):
        if not args:
            lang = prompt_value(self.io, "目标语言 (zh/en)", self.state.target_lang)
            txt = prompt_value(self.io, "输入待翻译文本")
        elif args[0] in {"zh", "en"}:
            lang, txt = args[0], " ".join(args[1:])
        else:
            lang, txt = self.state.target_lang, " ".join(args)
        if not txt.strip():
            self.io.emit(fg("coral", "请提供需要翻译的文本。"))
            return
        self._submit_text_job(txt, lang)

    # ---- Help, Status, API ----
    def _cmd_help(self, args: list[str]):
        details = {
            "project": ("/project [list|new|use <id|naev>|info]",
                        "管理多项目工作区。new 会新建项目并自动切换；use 可切回 naev 或已有项目 ID 前缀；info 查看项目快照/资源数。"),
            "import": ("/import <本地路径> [--preview]",
                       "Naev 工作区导入简体中文 .po；P6 项目导入 .csv / .xlsx / .po（支持自定义 id/source/target 列名，--preview 仅预演不入库）。"),
            "search": ("/search [关键词] [偏移offset]",
                       "分页搜索当前工作区资源（每次最多 20 条），输出序号供 /select 勾选。示例：/search Ember 0"),
            "select": ("/select <序号1 序号2 ... | occurrence_id | clear>",
                       "按 /search 返回的序号或完整 ID 勾选待翻译条目（Naev 单批最多 12 条，P6 最多 100 条）；不带参数查看已选，clear 清空。"),
            "graph": ("/graph [build | view <关键词>]",
                      "P6 项目专用。build 调用 extractor 抽取实体与关系并生成图快照（翻译前必跑）；view 查询已建成图谱中的节点与边。"),
            "text": ("/text [zh|en] <原文>",
                     "直接进行单句双向翻译并支持复核与导出 JSON。也可输入「请你翻译一下：原文」，按原文推断方向，或写「翻译成英文：原文」。"),
            "run": ("/run [自然语言要求]",
                    "对已勾选条目发起任务。不加参数默认执行 translate → export；附带文字时由 Pi 识别意图。示例：/run 保留角色语气并导出"),
            "batch": ("/batch",
                      "P6 项目专用。按文件或选中条目拆分多批自动排队翻译，可设置每批条数（1-64）和调用预算上限。"),
            "watch": ("/watch [job_id]",
                      "重新连接 SSE 实时监听任务进度；默认跟踪当前活跃任务，按 Ctrl+C 仅退出监听不会取消后台任务。"),
            "pause": ("/pause / /resume [job_id] / /cancel / /retry [occ_id]",
                      "任务控制：pause 申请暂停；resume 恢复暂停并可改要求；cancel 取消任务；retry 对失败条目重试。"),
            "review": ("/review [job_id]",
                       "进入待复核条目向导，展示原文/草稿/硬检查/Pi 审校历史。操作：[a]批准当前稿 [e]$EDITOR编辑 [t]单行改译 [s]跳过 [p]接受已完成部分 [q]退出。"),
            "evidence": ("/evidence [occurrence_id]",
                         "查看指定条目（默认第一条）的完整证据溯源：初始召回池、实际喂给模型的 ContextPack、search_source_graph 查询结果与审校裁决。"),
            "version": ("/version <lock|unlock|rollback> <occurrence_id>",
                        "版本管控：lock 锁定防止后续覆盖，unlock 解锁，rollback 追加恢复到指定历史版本号（保留完整审计）。"),
            "export": ("/export [--project] [输出路径]",
                       "导出产物：文本任务导出 JSON；Naev PO 先预检 msgfmt 并交互解决共享键冲突后导出 .po + .report.json；P6 项目加 --project 导出原格式文件。"),
            "glossary": ("/glossary [list|preview|apply|rollback]",
                         "P4 Naev 术语维护：list 列出规则；preview 按作用域预演受影响条目（needs_review/locked/no_change）；apply 发布并触发局部复查；rollback 追加回滚。"),
            "memory": ("/memory [list|draft|approve|retire]",
                       "P6 项目记忆管理：list 查看已批准记忆与草稿；draft 从原文起草术语/角色风格约束；approve/retire 审批生效或退役失效。"),
            "feedback": ("/feedback [list|add]",
                         "P5 人工反馈：list 查看累计反馈与重复别名模式；add 为已处理条目补登问题类别、建议译文与实体别名映射。"),
            "policy": ("/policy [status|candidate|replay|decide|rollback]",
                       "P5 自我迭代：status 看生效策略；candidate 基于反馈生成 Prompt/Skill/Harness/Memory 候选；replay 在临时库隔离回放对照；decide 发布或保留；rollback 回退版本。"),
            "api": ("/api",
                    "接口与分模块配置中心：注册模型 Profile、按 P6 六大角色（extractor/selector/translator/reviewer/repairer/intent）绑定模型、会话注入 Key、发送 /probe 探针及切换后端 URL。"),
        }
        if args:
            key = args[0].lstrip("/").lower()
            if key in details:
                usage, desc = details[key]
                lines = [
                    f"{fg('amber_bright', '用法:', bold=True)} {fg('gold', usage)}",
                    f"{fg('amber_bright', '说明:', bold=True)} {fg('cream', desc)}",
                ]
                self.io.emit("\n".join(draw_panel(lines, width=terminal_panel_width(104), title=f"帮助 /{key}")))
                return
            self.io.emit(fg("coral", f"未找到 /{key} 的单独说明，下面展示完整命令列表："))

        guide_lines = [
            f"{fg('amber_bright', '自然语言输入:', bold=True)} {fg('cream', '输入「请你翻译一下：原文」可直译。其他自然语言可作为项目任务交给 Pi。')}",
            f"{fg('amber_bright', '快捷交互操作:', bold=True)} {fg('cream', '输入 / 按 Tab 自动补全命令；↑/↓ 翻历史；监听任务时 Ctrl+C 仅中断前台查看。')}",
            f"{fg('amber_bright', '典型 CSV 流程:', bold=True)} {fg('gold', '/project new')} → {fg('gold', '/import <csv>')} → {fg('gold', '/graph build')} → {fg('gold', '/search')} → {fg('gold', '/select 1 2')} → {fg('gold', '/run')} → {fg('gold', '/export --project')}",
            f"{fg('amber_bright', '查看单条详情:', bold=True)} {fg('cream', '输入 ')} {fg('coral', '/help <命令名>')} {fg('cream', '例如 /help import、/help review、/help api。')}",
        ]
        self.io.emit("\n".join(draw_panel(guide_lines, width=terminal_panel_width(108), title="LoRE 使用指南")))
        groups = [
            ("项目与资源", [
                ["/project [list|new|use|info]", "列出、新建或切换 P6 项目（use naev 切回默认工作区）"],
                ["/import <path> [--preview]", "Naev 导入简体中文 .po；P6 项目导入 .csv / .xlsx / .po（支持列映射与预演）"],
                ["/search [关键词] [offset]", "按关键词分页搜索资源条目，显示可用于 /select 的编号"],
                ["/select <编号|id|clear>", "勾选或清空目标 occurrence_id（不带参数查看当前已选）"],
            ]),
            ("建图、翻译与任务控制", [
                ["/graph [build|view <词>]", "P6 触发实体关系抽取建图（翻译前必需），或检索图谱节点与关系边"],
                ["/text [zh|en] <原文>", "自由文本双向单句翻译（支持人工复核与导出 export.json）"],
                ["/run [要求] | /batch", "对选中条目运行 translate/自然语言任务；/batch 面向 P6 整文件分批跑"],
                ["/watch /pause /resume /cancel /retry", "SSE 实时跟踪进度、暂停/恢复、取消任务或重试失败条目"],
            ]),
            ("复核、证据与导出", [
                ["/review [job_id]", "人工复核向导：[a]批准 [e]$EDITOR编辑 [t]单行改译 [s]跳过 [p]接受已完成部分 [q]退出"],
                ["/evidence [occurrence_id]", "查看初始召回池、实际喂给模型的 ContextPack、源图主动取证与审校结论"],
                ["/version <lock|unlock|rollback> <id>", "锁定/解锁已提交译文，或追加回滚至指定历史版本"],
                ["/export [--project] [路径]", "导出译文：文本任务出 JSON；PO 先校验并解冲突；P6 项目加 --project 导出原表/PO"],
            ]),
            ("术语、记忆、迭代与接口配置", [
                ["/glossary [list|preview|apply|rollback]", "P4 Naev 术语变更预演（识别应改/已满足/锁定）、应用修订与追加回滚"],
                ["/memory [list|draft|approve|retire]", "P6 项目级术语与角色风格记忆的查看、起草、审批与退役"],
                ["/feedback [list|add] | /policy [...]", "P5 反馈聚合、生成策略候选、临时库隔离回放对照与发布/回滚"],
                ["/api | /status | /clear | /exit", "分模块模型与 API 凭据配置、查看状态、重绘蓝色启动面板、退出"],
            ]),
        ]
        for group_title, rows in groups:
            self.io.emit(fg("amber_bright", f"\n◈ {group_title}", bold=True))
            self.io.emit(draw_table(["命令与参数", "详细说明"], rows, wrap=True))

    def _cmd_status(self, _args: list[str]):
        connected = self.client.ping()
        rows = [
            ["API 地址", f"{self.state.api_url} ({'online' if connected else 'offline/down'})"],
            ["当前项目", f"{self.state.project_name} [{self.state.project_id}]"],
            ["已导入文件", self.state.upload_filename or self.state.import_filename or "(无)"],
            ["已选条目", f"{len(self.state.selected_ids)} 条" + (f" ({', '.join(i[:10] for i in self.state.selected_ids[:3])}...)" if self.state.selected_ids else "")],
            ["当前任务", self.state.active_job_id or "(无)"],
            ["P4/P5 活跃 ID", f"plan={self.state.active_plan_id or '-'} · cand={self.state.active_candidate_id or '-'}"],
        ]
        self.io.emit(draw_table(["状态项", "当前值"], rows, wrap=True))

    def _cmd_api(self, _args: list[str]):
        self.client = handle_api_wizard(self.client, self.state, self.io, self.restart_spawn_fn)

    # ---- P6 Projects & Imports ----
    def _cmd_project(self, args: list[str]):
        sub = args[0].lower() if args else "list"
        if sub == "list":
            projects = self.client.list_projects().get("projects", [])
            rows = [["naev", "Naev (default)", "en→zh", "built-in" if self.state.project_id == "naev" else ""]]
            for proj in projects:
                mark = "★" if proj["project_id"] == self.state.project_id else ""
                rows.append([proj["project_id"][:12], proj["name"], f"{proj['source_lang']}→{proj['target_lang']}",
                             f"{mark} rev={proj['revision']} graph={'✓' if proj.get('graph_snapshot') else '-'}"])
            self.io.emit(draw_table(["Project ID", "名称", "方向", "状态"], rows))
        elif sub == "new":
            name = prompt_value(self.io, "项目名称")
            src = prompt_value(self.io, "源语言 (en/zh)", "en")
            tgt = prompt_value(self.io, "目标语言 (zh/en)", "zh" if src == "en" else "en")
            proj = self.client.create_project(name, src, tgt)
            self.state.set_project(proj["project_id"], proj["name"])
            self.state.target_lang = tgt
            save_cli_config(self.state)
            self.io.emit(fg("gold", f"✓ 已创建并切换到项目: {proj['name']} ({proj['project_id']})"))
        elif sub == "use":
            target = args[1] if len(args) > 1 else prompt_value(self.io, "输入 project_id 或 naev", "naev")
            if target == "naev":
                self.state.set_project("naev", "Naev (default)")
                save_cli_config(self.state)
                self.io.emit(fg("gold", "✓ 已切回 Naev 默认工作区。"))
                return
            projects = self.client.list_projects().get("projects", [])
            match = next((p for p in projects if p["project_id"].startswith(target)), None)
            if not match:
                self.io.emit(fg("danger", f"✕ 未找到项目 {target}"))
                return
            self.state.set_project(match["project_id"], match["name"])
            self.state.target_lang = match["target_lang"]
            save_cli_config(self.state)
            self.io.emit(fg("gold", f"✓ 已切换项目: {match['name']} ({match['project_id']})"))
        elif sub == "info":
            if self.state.project_id == "naev":
                res = self.client.resources()
                self.io.emit(fg("amber", f"Naev 默认工作区 · 可用试点资源 {len(res.get('resources', []))} 条"))
            else:
                detail = self.client.project_detail(self.state.project_id)
                self.io.emit(json.dumps(detail, ensure_ascii=False, indent=2))
        else:
            self.io.emit(fg("coral", "用法: /project [list|new|use <id>|info]"))

    def _cmd_import(self, args: list[str]):
        preview = "--preview" in args
        clean_args = [a for a in args if a != "--preview"]
        raw_path = clean_args[0] if clean_args else prompt_value(self.io, "输入本地文件路径 (.po / .csv / .xlsx)")
        path = Path(raw_path).expanduser()
        if not path.is_file():
            self.io.emit(fg("danger", f"✕ 文件不存在: {path}"))
            return
        if self.state.project_id == "naev":
            if path.suffix.lower() != ".po":
                self.io.emit(fg("coral", "提示：Naev 默认工作区仅接受简体中文 .po；如需导入 .csv/.xlsx 请先 /project new 创建项目。"))
                return
            res = self.client.upload_po(path)
            self.state.upload_id = res["upload_id"]
            self.state.upload_filename = res["filename"]
            self.state.clear_selection()
            self.io.emit(fg("gold", f"✓ PO 上传成功: {res['filename']} · {res['entries']} 条目 · 绑定 {res['mapped_occurrences']} 个出现位置"))
            return

        options: dict[str, Any] = {}
        if path.suffix.lower() in {".csv", ".xlsx"}:
            custom = prompt_value(self.io, "是否自定义列名映射？(y/n)", "n").lower()
            if custom in {"y", "yes"}:
                options["id_column"] = prompt_value(self.io, "ID 列名", "id")
                options["source_column"] = prompt_value(self.io, "源文列名", "source_text")
                if path.suffix.lower() == ".xlsx":
                    sheet = prompt_value(self.io, "工作表名 (可选)")
                    if sheet:
                        options["sheet"] = sheet
                tgt_col = prompt_value(self.io, "导出目标列名 (可选)")
                if tgt_col:
                    options["target_column"] = tgt_col
        res = self.client.import_project_file(self.state.project_id, path, options, preview=preview)
        if preview:
            self.io.emit(fg("amber", f"导入预览: {res['rows']} 条 · 跳过 {len(res.get('skipped', []))} 条 · 变化 {res['changes']}"))
            return
        self.state.import_id = res["import_id"]
        self.state.import_filename = res["filename"]
        self.state.clear_selection()
        self.io.emit(fg("gold", f"✓ 项目文件导入成功: {res['filename']} (import_id={res['import_id'][:12]}, rev={res['project_revision']})"))

    # ---- Search & Select ----
    def _cmd_search(self, args: list[str]):
        query = args[0] if args else ""
        offset = int(args[1]) if len(args) > 1 and args[1].isdigit() else 0
        self.state.last_entries.clear()
        if self.state.project_id != "naev":
            data = self.client.project_resources(self.state.project_id, query=query, offset=offset, limit=20)
            rows = []
            for idx, item in enumerate(data.get("resources", []), 1):
                self.state.last_entries.append({"occurrence_id": item["occurrence_id"], "label": item["label"]})
                loc = item.get("location") or {}
                loc_str = f"{loc.get('path', '')}:{loc.get('line', loc.get('row', ''))}"
                mark = "✓" if item["occurrence_id"] in self.state.selected_ids else ""
                rows.append([str(idx), mark, item["occurrence_id"][:18], loc_str, truncate_display(item["label"].replace("\n", " "), 46)])
            self.io.emit(fg("amber", f"项目资源 共 {data['total']} 条 (offset={offset})"))
            self.io.emit(draw_table(["#", "选", "Occurrence ID", "位置", "原文"], rows))
            return

        if self.state.upload_id:
            data = self.client.po_entries(self.state.upload_id, query=query, offset=offset, limit=20)
            rows = []
            flat_idx = 0
            for entry in data.get("entries", []):
                occs = entry.get("occurrences", [])
                if not occs:
                    continue
                for occ in occs:
                    flat_idx += 1
                    oid = occ["occurrence_id"]
                    self.state.last_entries.append({"occurrence_id": oid, "label": occ["label"]})
                    loc = occ.get("location") or {}
                    mark = "✓" if oid in self.state.selected_ids else ""
                    rows.append([str(flat_idx), mark, oid[:18], f"{loc.get('path', '')}:{loc.get('line', '')}",
                                 truncate_display(occ["label"].replace("\n", " "), 44)])
            self.io.emit(fg("amber", f"PO 条目 共 {data['total']} 组 (offset={offset})"))
            self.io.emit(draw_table(["#", "选", "Occurrence ID", "位置", "原文"], rows))
            return

        data = self.client.resources()
        rows = []
        filtered = [r for r in data.get("resources", []) if not query or query.casefold() in r["label"].casefold()]
        for idx, item in enumerate(filtered[offset:offset + 20], 1):
            oid = item["occurrence_id"]
            self.state.last_entries.append({"occurrence_id": oid, "label": item["label"]})
            mark = "✓" if oid in self.state.selected_ids else ""
            rows.append([str(idx), mark, oid[:18], str(item.get("file") or "-"),
                         truncate_display(item["label"].replace("\n", " "), 46)])
        self.io.emit(fg("amber", f"Naev 试点目录 共 {len(filtered)} 条"))
        self.io.emit(draw_table(["#", "选", "Occurrence ID", "文件", "原文"], rows))

    def _cmd_select(self, args: list[str]):
        if not args:
            if not self.state.selected_ids:
                self.io.emit(fg("muted", "当前未选中任何资源。用 /search 查找后输入 /select 1 2 ..."))
                return
            rows = [[str(i), oid, truncate_display(self.state.selected_labels.get(oid, ""), 48)]
                    for i, oid in enumerate(self.state.selected_ids, 1)]
            self.io.emit(draw_table(["#", "已选 Occurrence ID", "原文摘要"], rows, wrap=True))
            return
        if args[0].lower() == "clear":
            self.state.clear_selection()
            self.io.emit(fg("amber", "已清空选中条目。"))
            return
        limit = 12 if self.state.project_id == "naev" else 100
        for token in args:
            if token.isdigit():
                idx = int(token)
                if not 1 <= idx <= len(self.state.last_entries):
                    self.io.emit(fg("coral", f"序号 {idx} 超出最近搜索列表范围。"))
                    continue
                entry = self.state.last_entries[idx - 1]
                oid, label = entry["occurrence_id"], entry["label"]
            else:
                oid, label = token, token
            if oid in self.state.selected_ids:
                continue
            if len(self.state.selected_ids) >= limit:
                self.io.emit(fg("coral", f"已达单批上限 ({limit} 条)。"))
                break
            self.state.selected_ids.append(oid)
            self.state.selected_labels[oid] = label
        self.io.emit(fg("gold", f"✓ 当前共选中 {len(self.state.selected_ids)} 条。"))

    # ---- P6 Graph ----
    def _cmd_graph(self, args: list[str]):
        if self.state.project_id == "naev":
            self.io.emit(fg("coral", "Naev 默认工作区使用冻结源图；请先切到 P6 项目以建图或浏览。"))
            return
        sub = args[0].lower() if args else "view"
        if sub == "build":
            extract = prompt_value(self.io, "是否调用提取模型抽取实体关系？(y/n)", "y").lower() in {"y", "yes", ""}
            rebuild = prompt_value(self.io, "是否强制全量重建？(y/n)", "n").lower() in {"y", "yes"}
            max_calls = int(prompt_value(self.io, "最大调用预算", "32"))
            res = self.client.build_graph(self.state.project_id, {
                "extract": extract, "rebuild": rebuild, "batch_size": 12,
                "max_calls": max_calls, "max_input_tokens": 6000, "max_output_tokens": 2048,
            })
            self.io.emit(fg("amber", f"已提交建图任务 {res['job_id'][:12]}（共 {res['batches']} 批）"))
            watch_job(self.client, self.state, res["job_id"], self.io)
        else:
            query = " ".join(args[1:]) if sub == "view" else " ".join(args)
            data = self.client.read_graph(self.state.project_id, query=query, limit=20)
            self.io.emit(fg("amber_bright", f"快照 {data['graph_snapshot'][:14]} · 匹配节点 {data['total']} 个"))
            n_rows = [[n["id"][:22], n["kind"], str(n.get("attributes", {}).get("name") or "-"),
                       truncate_display(n["evidence"][0]["quote"], 36) if n.get("evidence") else "-"]
                      for n in data.get("nodes", [])[:15]]
            if n_rows:
                self.io.emit(draw_table(["Node ID", "类型", "名称", "出处片段"], n_rows))
            e_rows = [[e["relation"], e["source"]["id"][:18], e["target"]["id"][:18],
                       str(e.get("attributes", {}).get("basis", "direct"))]
                      for e in data.get("edges", [])[:15]]
            if e_rows:
                self.io.emit(draw_table(["关系", "起点", "终点", "依据"], e_rows))

    # ---- Jobs, Control & Review ----
    def _cmd_run(self, args: list[str]):
        if not self.state.selected_ids:
            self.io.emit(fg("coral", "请先用 /search 与 /select 选中至少 1 条资源。"))
            return
        req_text = " ".join(args).strip()
        payload: dict[str, Any] = {
            "selection": {
                "project": self.state.project_id,
                "source_ids": self.state.selected_ids,
                "target_lang": self.state.target_lang,
            },
        }
        if req_text:
            payload["text"] = req_text
        else:
            payload["intents"] = ["translate", "export"]
            payload["targets"] = list(self.state.selected_ids)
            payload["target_lang"] = self.state.target_lang
        if self.state.upload_id and self.state.project_id == "naev":
            payload["upload_id"] = self.state.upload_id
        job = self.client.create_job(payload)
        watch_job(self.client, self.state, job["job_id"], self.io)

    def _cmd_batch(self, _args: list[str]):
        if self.state.project_id == "naev":
            self.io.emit(fg("coral", "/batch 仅用于 P6 自定义项目批量翻译；Naev 请用 /select + /run。"))
            return
        file_filter = prompt_value(self.io, "限定文件名 (留空表示项目全部资源)") or None
        size = int(prompt_value(self.io, "每批条数 (1-64)", "12"))
        calls = int(prompt_value(self.io, "每批最大调用预算", "72"))
        req = prompt_value(self.io, "附加翻译要求 (可选)") or None
        res = self.client.translation_batches(self.state.project_id, {
            **({"file": file_filter} if file_filter else {}),
            **({"targets": list(self.state.selected_ids)} if self.state.selected_ids else {}),
            "batch_size": size, "max_calls_per_batch": calls,
            **({"requirements": req} if req else {}),
        })
        self.io.emit(fg("gold", f"✓ 已创建批量翻译 {res['bulk_id'][:12]} · 共 {res['rows']} 条 / {len(res['jobs'])} 个任务"))
        if res["jobs"]:
            self.state.active_job_id = res["jobs"][0]["job_id"]
            if prompt_value(self.io, "是否立即跟踪首个批次任务？(y/n)", "y").lower() in {"y", "yes", ""}:
                watch_job(self.client, self.state, self.state.active_job_id, self.io)

    def _require_job_id(self, args: list[str]) -> str | None:
        jid = args[0] if args else self.state.active_job_id
        if not jid:
            self.io.emit(fg("coral", "当前没有活跃任务，请传入 job_id。"))
        return jid

    def _cmd_watch(self, args: list[str]):
        jid = self._require_job_id(args)
        if jid:
            watch_job(self.client, self.state, jid, self.io)

    def _job_control(self, action: str):
        jid = self._require_job_id([])
        if not jid:
            return
        fn = {"pause": self.client.pause_job, "cancel": self.client.cancel_job}[action]
        res = fn(jid)
        self.io.emit(fg("amber", f"操作结果: {res.get('status')} (reason={res.get('reason')})"))

    def _cmd_resume(self, args: list[str]):
        jid = self._require_job_id(args)
        if not jid:
            return
        req = prompt_value(self.io, "更新附加要求 (可选，留空保持原要求)") or None
        self.client.resume_job(jid, req)
        watch_job(self.client, self.state, jid, self.io)

    def _cmd_retry(self, args: list[str]):
        jid = self._require_job_id([])
        if not jid:
            return
        occ = args[0] if args else (prompt_value(self.io, "指定 occurrence_id (若已过规划阶段必填)") or None)
        confirm = prompt_value(self.io, "是否确认重发状态未知调用？(y/n)", "n").lower() in {"y", "yes"}
        self.client.retry_job(jid, occ, confirm)
        watch_job(self.client, self.state, jid, self.io)

    def _cmd_review(self, args: list[str]):
        jid = self._require_job_id(args)
        if jid:
            job = self.client.job_detail(jid)
            handle_review(self.client, self.state, job, self.io)

    def _cmd_evidence(self, args: list[str]):
        jid = self._require_job_id([])
        if not jid:
            return
        job = self.client.job_detail(jid)
        items = job.get("items", [])
        if not items:
            self.io.emit(fg("muted", "该任务暂无条目结果。"))
            return
        occ = args[0] if args else items[0]["occurrence_id"]
        match = next((i for i in items if i["occurrence_id"].startswith(occ)), None)
        if not match:
            self.io.emit(fg("danger", f"✕ 未找到条目 {occ}"))
            return
        self.io.emit(render_evidence(self.client.item_evidence(jid, match["step"], match["occurrence_id"])))

    def _cmd_version(self, args: list[str]):
        jid = self._require_job_id([])
        if not jid:
            return
        if len(args) < 2 or args[0] not in {"lock", "unlock", "rollback"}:
            self.io.emit(fg("coral", "用法: /version <lock|unlock|rollback> <occurrence_id>"))
            return
        action, occ = args[0], args[1]
        job = self.client.job_detail(jid)
        item = next((i for i in job.get("items", []) if i["occurrence_id"].startswith(occ)), None)
        cur_ver = (item.get("version") or item.get("expected_version")) if item else int(prompt_value(self.io, "当前版本号", "1"))
        to_ver = int(prompt_value(self.io, "回滚目标版本号", "1")) if action == "rollback" else None
        res = self.client.version_action(jid, item["occurrence_id"] if item else occ, action, cur_ver, to_ver)
        self.io.emit(fg("gold", f"✓ 版本操作完成: {res}"))

    # ---- Export ----
    def _cmd_export(self, args: list[str]):
        force_project = "--project" in args
        clean_args = [a for a in args if a != "--project"]
        if not force_project and self.state.active_job_id:
            job = self.client.job_detail(self.state.active_job_id)
            if job.get("source_kind") == "text":
                data = self.client.download_job_file(self.state.active_job_id, "export.json")
                out = Path(clean_args[0] if clean_args else f"export-{self.state.active_job_id[:8]}.json")
                out.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                self.io.emit(fg("gold", f"✓ 文本译文 JSON 已导出: {out.resolve()}"))
                return

        if self.state.project_id != "naev":
            imp = self.state.import_id or prompt_value(self.io, "输入 import_id")
            report = self.client.project_export_report(self.state.project_id, imp)
            self.io.emit(fg("amber_bright", f"项目导出报告: 已译 {report['translated']} · 排除 {len(report['excluded'])}"))
            if report.get("validation"):
                self.io.emit(render_po_report({"validation": report["validation"], "applied": report["translated"],
                                               "download_ready": report["validation"].get("ok", False)}))
            out = Path(clean_args[0] if clean_args else (self.state.import_filename or "project-export.out"))
            if prompt_value(self.io, f"是否保存到 {out}？(y/n)", "y").lower() in {"y", "yes", ""}:
                data = self.client.project_export_download(self.state.project_id, imp)
                out.write_bytes(data)
                self.io.emit(fg("gold", f"✓ 已导出至 {out.resolve()}"))
            return

        jid = self._require_job_id([])
        if not jid:
            return
        while True:
            report = self.client.po_preview(jid, self.state.choices)
            self.io.emit(render_po_report(report))
            conflicts = report.get("conflicts") or []
            if not conflicts:
                break
            for entry in conflicts:
                pick = prompt_value(self.io, f"为冲突项 '{entry['source'][:24]}' 选择版本编号 (1..{len(entry['versions'])}) 或 q 取消", "1")
                if pick.lower() == "q":
                    return
                if pick.isdigit() and 1 <= int(pick) <= len(entry["versions"]):
                    self.state.choices[entry["entry_id"]] = entry["versions"][int(pick) - 1]["occurrence_id"]
        if not report.get("download_ready"):
            self.io.emit(fg("coral", "当前未满足下载条件（无已批准写回项或校验未通过）。"))
            return
        out_po = Path(clean_args[0] if clean_args else "naev-zh_Hans.po")
        out_po.write_bytes(self.client.po_download(jid, self.state.choices))
        out_rep = out_po.with_suffix(".report.json")
        out_rep.write_text(json.dumps(self.client.po_report(jid, self.state.choices), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.io.emit(fg("gold", f"✓ 已导出 PO: {out_po.resolve()}\n✓ 变更报告: {out_rep.resolve()}"))

    # ---- P4 Glossary & P6 Memory ----
    def _cmd_glossary(self, args: list[str]):
        sub = args[0].lower() if args else "list"
        if sub == "list":
            data = self.client.glossary()
            self.io.emit(fg("amber_bright", f"Naev 术语库 (knowledge_epoch={data['knowledge_epoch']})"))
            rows = [[r["rule_id"][:12], r["source"], r["target"], f"v{r['version']}", str(r["matched_occurrences"])]
                    for r in data.get("rules", [])]
            self.io.emit(draw_table(["Rule ID", "原文", "译法", "版本", "命中数"], rows) if rows else fg("muted", "(暂无登记术语)"))
        elif sub == "preview":
            src = prompt_value(self.io, "英文术语 (source)")
            tgt = prompt_value(self.io, "新译法 (target)")
            prev = prompt_value(self.io, "旧译法 (可选)") or None
            rule_id = prompt_value(self.io, "更新已有 rule_id (新建留空)") or None
            scope: dict[str, Any] = {}
            if self.state.selected_ids and prompt_value(self.io, f"限定为当前选中 {len(self.state.selected_ids)} 条？(y/n)", "n").lower() in {"y", "yes"}:
                scope["occurrence_ids"] = list(self.state.selected_ids)
            else:
                f_scope = prompt_value(self.io, "限定文件 (可选)")
                b_scope = prompt_value(self.io, "限定分支 (可选)")
                if f_scope:
                    scope["file"] = f_scope
                if b_scope:
                    scope["branch_id"] = b_scope
            plan = self.client.maintenance_preview({
                "source": src, "target": tgt, "scope": scope,
                **({"previous_target": prev} if prev else {}),
                **({"rule_id": rule_id} if rule_id else {}),
            })
            self.state.active_plan_id = plan["plan_id"]
            self.io.emit(render_maintenance_plan(plan))
        elif sub == "apply":
            pid = args[1] if len(args) > 1 else (self.state.active_plan_id or prompt_value(self.io, "输入 plan_id"))
            res = self.client.maintenance_apply(pid)
            self.io.emit(fg("gold", f"✓ 已应用规则 v{res['rule_version']} (plan={pid[:12]})"))
            if res.get("job_id"):
                watch_job(self.client, self.state, res["job_id"], self.io)
        elif sub == "rollback":
            pid = args[1] if len(args) > 1 else (self.state.active_plan_id or prompt_value(self.io, "输入 plan_id"))
            res = self.client.maintenance_rollback(pid)
            self.io.emit(fg("gold", f"✓ 已回滚至规则 v{res['rule_version']}，恢复 {len(res.get('versions', []))} 条译文。"))
        else:
            self.io.emit(fg("coral", "用法: /glossary [list|preview|apply|rollback]"))

    def _cmd_memory(self, args: list[str]):
        if self.state.project_id == "naev":
            self.io.emit(fg("coral", "Naev 请用 /glossary 管理术语；/memory 用于 P6 自定义项目记忆。"))
            return
        sub = args[0].lower() if args else "list"
        if sub == "list":
            data = self.client.project_memory(self.state.project_id)
            for label, bucket in (("已批准记忆", data.get("approved", [])), ("待审草稿", data.get("drafts", []))):
                rows = [[r["record_id"][:18], f"v{r['version']}", r["kind"],
                         truncate_display(json.dumps(r["content"], ensure_ascii=False), 48)] for r in bucket]
                self.io.emit(fg("amber_bright", f"\n{label} ({len(bucket)})"))
                if rows:
                    self.io.emit(draw_table(["Record ID", "版本", "类别", "内容"], rows))
        elif sub == "draft":
            kind = prompt_value(self.io, "类型 (term/style)", "term")
            occ = prompt_value(self.io, "occurrence_id", self.state.selected_ids[0] if self.state.selected_ids else "")
            ver = int(prompt_value(self.io, "source_version", "1"))
            quote = prompt_value(self.io, "原文引用片段 (quote)")
            payload: dict[str, Any] = {"kind": kind, "occurrence_id": occ, "source_version": ver, "quote": quote}
            if kind == "term":
                payload["source"] = prompt_value(self.io, "术语原文 (source)", quote)
                payload["target"] = prompt_value(self.io, "术语译法 (target)")
                payload["mandatory"] = prompt_value(self.io, "是否强制？(y/n)", "y").lower() in {"y", "yes", ""}
            else:
                payload["note"] = prompt_value(self.io, "风格/角色说明 (note)")
            res = self.client.stage_memory(self.state.project_id, payload)
            self.io.emit(fg("gold", f"✓ 已起草记忆: {res['record_id']} v{res['version']}"))
        elif sub in {"approve", "retire"}:
            rec_id = args[1] if len(args) > 1 else prompt_value(self.io, "record_id")
            ver = int(args[2] if len(args) > 2 else prompt_value(self.io, "version", "1"))
            reason = prompt_value(self.io, "审批/退役理由", "editor_decision")
            res = self.client.decide_memory(self.state.project_id, rec_id, ver, sub, reason)
            self.io.emit(fg("gold", f"✓ 操作完成: {res.get('status', 'ok')}"))
        else:
            self.io.emit(fg("coral", "用法: /memory [list|draft|approve|retire]"))

    # ---- P5 Feedback & Policy ----
    def _cmd_feedback(self, args: list[str]):
        sub = args[0].lower() if args else "list"
        if sub == "list":
            data = self.client.feedback_patterns()
            fb = data.get("feedback", [])
            pats = data.get("patterns", [])
            self.io.emit(fg("amber_bright", f"累计反馈 {len(fb)} 条 · 聚合重复模式 {len(pats)} 个"))
            if fb:
                rows = [[f["feedback_id"][:10], f["category"], f["occurrence_id"][:18],
                         truncate_display(f["reason"], 36)] for f in fb[-10:]]
                self.io.emit(draw_table(["ID", "类别", "Occurrence", "原因"], rows, wrap=True))
            if pats:
                p_rows = [[p["category"], str(p.get("alias") or "-"), str(p["occurrences"]),
                           ",".join(i[:8] for i in p["feedback_ids"])] for p in pats]
                self.io.emit(draw_table(["类别", "别名模式", "出现次数", "Feedback IDs"], p_rows, wrap=True))
        elif sub == "add":
            jid = prompt_value(self.io, "job_id", self.state.active_job_id or "")
            step = int(prompt_value(self.io, "step", "0"))
            occ = prompt_value(self.io, "occurrence_id", self.state.selected_ids[0] if self.state.selected_ids else "")
            cat = prompt_value(self.io, "category (correction/retrieval/prompt/memory/skill/harness)", "retrieval")
            reason = prompt_value(self.io, "原因 (reason)")
            after = prompt_value(self.io, "建议译文 (可选)") or None
            q_alias = prompt_value(self.io, "实体查询别名 (可选)")
            alias = {"query": q_alias, "canonical": prompt_value(self.io, "规范实体名")} if q_alias else None
            res = self.client.add_feedback({
                "job_id": jid, "step": step, "occurrence_id": occ, "category": cat, "reason": reason,
                **({"after": after} if after else {}), **({"alias": alias} if alias else {}),
            })
            self.io.emit(fg("gold", f"✓ 已记录反馈: {res['feedback_id'][:12]}"))
        else:
            self.io.emit(fg("coral", "用法: /feedback [list|add]"))

    def _cmd_policy(self, args: list[str]):
        sub = args[0].lower() if args else "status"
        if sub == "status":
            data = self.client.policy_status()
            act = data["active"]
            self.io.emit(fg("amber_bright", f"当前生效策略: {act['release_id']}"))
            self.io.emit(json.dumps(act["spec"], ensure_ascii=False, indent=2))
            cands = data.get("candidates", [])
            if cands:
                rows = [[c["candidate_id"][:12], ",".join(c["changes"].keys()),
                         (c.get("decision") or {}).get("decision", "pending")] for c in cands]
                self.io.emit(draw_table(["Candidate ID", "变更组件", "状态"], rows))
        elif sub == "candidate":
            fb_ids = [s.strip() for s in prompt_value(self.io, "关联 feedback_id（逗号分隔）").split(",") if s.strip()]
            changes: dict[str, Any] = {}
            prompt_ov = prompt_value(self.io, "reviewer_source 补充指令 (可选)")
            if prompt_ov:
                changes["prompts"] = {"reviewer_source": prompt_ov}
            handoff = prompt_value(self.io, "转人工模板 (可选，须含 {reason})")
            if handoff:
                changes["handoff_template"] = handoff
            alias_raw = prompt_value(self.io, "实体别名 JSON，例如 {\"a\":\"b\"} (可选)")
            if alias_raw:
                changes["aliases"] = json.loads(alias_raw)
            issues = prompt_value(self.io, "skill_issues 逗号分隔 (可选)")
            if issues:
                changes["skill_issues"] = [s.strip() for s in issues.split(",") if s.strip()]
            k = prompt_value(self.io, "lookup_results (1/2，可选)")
            if k in {"1", "2"}:
                changes["lookup_results"] = int(k)
            if self.state.active_plan_id and prompt_value(self.io, f"关联 P4 预览 {self.state.active_plan_id[:10]}？(y/n)", "n").lower() in {"y", "yes"}:
                changes["memory_plan_id"] = self.state.active_plan_id
            cand = self.client.create_candidate(fb_ids, changes)
            self.state.active_candidate_id = cand["candidate_id"]
            self.io.emit(fg("gold", f"✓ 已创建策略候选: {cand['candidate_id'][:12]}"))
        elif sub == "replay":
            cid = args[1] if len(args) > 1 else (self.state.active_candidate_id or prompt_value(self.io, "candidate_id"))
            mode = prompt_value(self.io, "回放模式 (record/strategy)", "strategy")
            dataset = prompt_value(self.io, "数据集 (feedback/validation)", "validation")
            live = prompt_value(self.io, "是否真实重跑首次 Pi 审校 live？(y/n)", "n").lower() in {"y", "yes"}
            raw_cases = prompt_value(self.io, "用例列表 JSON（[{\"job_id\":...,\"step\":0,\"occurrence_id\":...}]）")
            rep = self.client.replay_candidate(cid, {"mode": mode, "dataset": dataset, "live": live, "cases": json.loads(raw_cases)})
            self.io.emit(render_replay_report(rep))
        elif sub == "decide":
            cid = args[1] if len(args) > 1 else (self.state.active_candidate_id or prompt_value(self.io, "candidate_id"))
            decision = prompt_value(self.io, "决定 (publish/retain)", "publish")
            reason = prompt_value(self.io, "原因", "replay_verified")
            res = self.client.decide_candidate(cid, decision, reason)
            self.io.emit(fg("gold", f"✓ 策略决定完成: {res['decision']} (release={res['release_id'][:12]})"))
        elif sub == "rollback":
            rid = args[1] if len(args) > 1 else prompt_value(self.io, "当前 release_id")
            reason = prompt_value(self.io, "回滚原因", "revert_policy")
            res = self.client.rollback_release(rid, reason)
            self.io.emit(fg("gold", f"✓ 已回滚发布至 {res['restored_release_id'][:12]} (new_release={res['release_id'][:12]})"))
        else:
            self.io.emit(fg("coral", "用法: /policy [status|candidate|replay|decide|rollback]"))
