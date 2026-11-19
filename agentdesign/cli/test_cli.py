"""Offline end-to-end verification tests for the LoRE amber REPL CLI."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from fastapi.testclient import TestClient
import httpx

from cli.client import LoreClient
from cli.repl import LoreREPL
from cli.state import SessionState
from cli.theme import GIRL_PORTRAIT, build_startup_banner, draw_panel, draw_table, display_width, fg, strip_ansi
from cli.workflows import ConsoleIO, watch_job
from run_api import create_app


class ScriptedIO(ConsoleIO):
    """Deterministic scripted console IO for REPL tests."""

    def __init__(self, inputs: list[str], secrets: list[str] | None = None):
        self._inputs = iter(inputs)
        self._secrets = iter(secrets or [])
        self.outputs: list[str] = []
        super().__init__(ask=self._next_input, ask_secret=self._next_secret, emit=self._emit)

    def _next_input(self, prompt: str) -> str:
        self.outputs.append(strip_ansi(prompt))
        try:
            return next(self._inputs)
        except StopIteration as err:
            raise EOFError from err

    def _next_secret(self, prompt: str) -> str:
        self.outputs.append(strip_ansi(prompt))
        return next(self._secrets, "")

    def _emit(self, text: str):
        self.outputs.append(strip_ansi(text))


class LoreCLITests(unittest.TestCase):
    def test_explicit_chinese_source_routes_to_english_text_job_inside_project(self):
        with TemporaryDirectory(prefix="lore-cli-text-") as tmp, TestClient(
                create_app(Path(tmp), offline=True, projects_only=True)) as tc:
            client = LoreClient(client=tc)
            state = SessionState(project_id="a" * 32, selected_ids=["project-resource"], target_lang="zh")
            io = ScriptedIO(["n"])
            source = "灭烛见真火，忘剑得神兵"
            LoreREPL(client, state, io=io).dispatch("请你翻译一下：" + source)
            job = client.job_detail(state.active_job_id)
            self.assertEqual((job["source_kind"], job["source_lang"], job["target_lang"]), ("text", "zh", "en"))
            self.assertEqual(job["items"][0]["source"], source)
            self.assertEqual(job["status"], "awaiting_review")
            self.assertNotIn("任务需要补充信息", "\n".join(io.outputs))

    def test_unresolved_resource_clarification_never_resubmits_empty_or_loops(self):
        client = Mock(spec=LoreClient)
        job = {"job_id": "job", "status": "awaiting_input",
               "missing": {"1": {"reason": "target_not_in_scope", "choices": ["resource-id"]}}}
        client.job_detail.return_value = job
        state = SessionState()
        blank = ScriptedIO([""])
        self.assertEqual(watch_job(client, state, "job", blank), job)
        client.continue_job.assert_not_called()
        self.assertIn("未能唯一匹配资源：1", "\n".join(blank.outputs))

        attempt = ScriptedIO(["another-invalid-id"])
        self.assertEqual(watch_job(client, state, "job", attempt), job)
        client.continue_job.assert_called_once_with("job", {"targets": ["another-invalid-id"]})
        self.assertIn("仍需补充信息", "\n".join(attempt.outputs))

    def test_banner_portrait_alignment_and_branding(self):
        self.assertEqual(len({len(row) for row in GIRL_PORTRAIT}), 1)
        banner = strip_ansi(build_startup_banner({
            "model": "deepseek-v4.1-flash-1017 (offline)",
            "project": "Naev (default)",
            "memory": "0 rules · policy baseline",
            "api": "http://127.0.0.1:8766 (online)",
        }))
        self.assertIn("██╗", banner)
        self.assertIn("g a m e  l o c a t i o n  a g e n t", banner)
        self.assertIn("search_source_graph", banner)
        panel_rows = [line for line in banner.splitlines() if "│" in line]
        self.assertGreater(len(panel_rows), 10)
        box_widths = {display_width(line[line.index("╭") if "╭" in line else line.index("│"):]) for line in panel_rows}
        self.assertEqual(len(box_widths), 1)

    def test_p6_project_import_graph_api_roles_text_and_memory(self):
        with TemporaryDirectory(prefix="lore-cli-test-") as tmp:
            work = Path(tmp)
            csv_path = work / "sample.csv"
            csv_path.write_text(
                "id,source_text,text_type,context\n"
                "row_1,Emperor's Fist is an armored cruiser.,item_desc,branch_id=main\n",
                encoding="utf-8",
            )
            export_csv = work / "exported.csv"
            export_json = work / "text.json"

            app = create_app(work / "data", offline=True, projects_only=True)
            with TestClient(app) as tc:
                client = LoreClient(client=tc)
                state = SessionState(api_url="http://testserver")
                io = ScriptedIO([
                    "/help",
                    "/help /api",
                    "/status",
                    "/project new", "CLI Demo", "en", "zh",
                    f"/import {csv_path}", "n",
                    "/search Emperor",
                    "/select 1",
                    "/graph build", "y", "n", "4",
                    "/graph view",
                    "/memory draft", "term", "", "1", "Emperor's Fist", "Emperor's Fist", "皇帝之拳号", "y",
                    "/memory list",
                    "/api", "1", "local-qwen", "Qwen3-8B", "http://127.0.0.1:8000/v1", "3", "75",
                    "2", "", "1", "1", "", "", "", "q",
                    "/text zh Hello pilot",
                    "y", "t", "你好，飞行员", "离线人工改译测试",
                    f"/export {export_json}",
                    "/glossary list",
                    "/feedback list",
                    "/policy status",
                    "/exit",
                ])
                repl = LoreREPL(client, state, io=io, show_banner=True)
                repl.run()

                transcript = "\n".join(io.outputs)
                self.assertIn("已创建并切换到项目: CLI Demo", transcript)
                self.assertIn("项目文件导入成功: sample.csv", transcript)
                self.assertIn("当前共选中 1 条", transcript)
                self.assertIn("已起草记忆:", transcript)
                self.assertIn("已注册 Profile: local-qwen", transcript)
                self.assertIn("已更新项目", transcript)
                self.assertIn("已提交: committed", transcript)
                self.assertTrue(export_json.is_file())
                self.assertIn("你好，飞行员", export_json.read_text(encoding="utf-8"))

                # Verify project CSV export directly via /project + /export --project
                io2 = ScriptedIO([f"/export --project {export_csv}", "y", "/exit"])
                repl2 = LoreREPL(client, state, io=io2, show_banner=False)
                repl2.run()
                self.assertTrue(export_csv.is_file())
                self.assertIn("Emperor's Fist", export_csv.read_text(encoding="utf-8"))

                # Verify approving drafted project memory
                drafts = client.project_memory(state.project_id)["drafts"]
                self.assertEqual(len(drafts), 1)
                rec = drafts[0]
                io3 = ScriptedIO([f"/memory approve {rec['record_id']} {rec['version']}", "confirmed_term", "/exit"])
                repl3 = LoreREPL(client, state, io=io3, show_banner=False)
                repl3.run()
                self.assertIn("操作完成: approved", "\n".join(io3.outputs))

    def test_wrapping_and_detailed_help(self):
        long_text = fg("amber_bright", "超长文本测试：" + "Emberblade 余烬之刃 " * 8)
        panel_lines = draw_panel([long_text], width=50, title="长文本测试", wrap=True)
        self.assertGreater(len(panel_lines), 3)
        for row in panel_lines:
            self.assertEqual(display_width(row), 50)
        joined = "".join(strip_ansi(r) for r in panel_lines)
        self.assertIn("Emberblade", joined)
        self.assertNotIn("…", joined)

        table = draw_table(
            ["命令", "说明"],
            [["/import <path>", "这是一个非常长的帮助说明，用于验证窄终端下的自动换行而不是截断省略。"]],
            max_width=60,
            wrap=True,
        )
        for row in table.splitlines():
            self.assertLessEqual(display_width(row), 60)
        self.assertNotIn("…", table)

    def test_sse_stream_parsing(self):
        body = (
            ": keep-alive\n\n"
            "id: 1\nevent: progress\ndata: {\"stage\":\"draft\",\"occurrence_id\":\"occ_1\"}\n\n"
            "id: 2\nevent: job\ndata: {\"status\":\"completed\"}\n\n"
        ).encode("utf-8")
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})
        )
        client = LoreClient(client=httpx.Client(base_url="http://mock", transport=transport))
        try:
            events = list(client.stream_events("job123", after=0))
            self.assertEqual(events, [
                (1, "progress", {"stage": "draft", "occurrence_id": "occ_1"}),
                (2, "job", {"status": "completed"}),
            ])
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
