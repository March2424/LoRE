from contextlib import closing
import csv
from io import BytesIO, StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic, sleep
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook

from run_api import create_app

CSV = ("id,source_text,text_type,context,branch_id\n"
       "a,Elixir,item_name,,main\n"
       "b,Use the elixir.,system_message,,main\n"
       "c,Elixir,item_name,,alternate\n").encode()
PO = '''msgid ""
msgstr ""
"Language: zh_Hans\\n"
"Content-Type: text/plain; charset=UTF-8\\n"
"Plural-Forms: nplurals=1; plural=0;\\n"

#: ui/menu.cfg:14
msgctxt "action"
msgid "Open"
msgstr "已有译文"
'''.encode()


class ProjectTests(unittest.TestCase):
    def run_until(self, client, job_id, expected):
        until = monotonic() + 25
        while monotonic() < until:
            job = client.get(f"/jobs/{job_id}").json()
            if job["status"] not in {"running", "queued"}:
                self.assertEqual(job["status"], expected, job)
                return job
            sleep(.1)
        self.fail(job)

    def test_project_import_graph_cross_job_tm_export_and_invalidation(self):
        with TemporaryDirectory() as path, TestClient(create_app(Path(path), offline=True, projects_only=True)) as client:
            app = client.app.state.workbench

            def post(url, key, **kwargs):
                response = client.post(url, headers={"Idempotency-Key": key}, **kwargs)
                self.assertTrue(response.is_success, response.text)
                return response.json()

            project = post("/projects", "project", json={"name": "Flare sample", "source_lang": "en", "target_lang": "zh"})
            route = "/projects/" + project["project_id"]
            upload = route + "/imports?filename=game.csv"
            preview = post(upload + "&preview=true", "preview", content=CSV)
            self.assertEqual(preview["changes"]["added"], 3)
            self.assertEqual(client.get(route).json()["resources"], 0)
            imported = post(upload, "import", content=CSV)
            self.assertEqual(post(upload, "new-key", content=CSV)["import_id"], imported["import_id"])
            self.assertEqual(client.get(route).json()["approved_memory"], 0)

            workbook = Workbook()
            sheet = workbook.active
            sheet.title = "Dialog"
            sheet.append(["Key", "English", "Chinese", "Formula"])
            sheet.append(["x", "View quest", "文件自带的译文", "=1+1"])
            output = BytesIO()
            workbook.save(output)
            xlsx = post(route + "/imports", "excel",
                        params={"filename": "localization.xlsx", "options": json.dumps({
                            "id_column": "Key", "source_column": "English", "target_column": "Chinese", "sheet": "Dialog"
                        })}, content=output.getvalue())
            po = post(route + "/imports?filename=localization.po", "po", content=PO)
            self.assertEqual(client.get(route).json()["approved_memory"], 0)
            profile = post("/model-profiles", "model", json={
                "name": "Offline role fixture", "base_url": "http://localhost:8000/v1", "model": "fixture-translation"
            })
            binding = client.put(route + "/model-roles", headers={"Idempotency-Key": "bind"},
                                 json={"roles": {"translator": profile["profile_id"]}})
            self.assertEqual(binding.status_code, 200, binding.text)

            build = post(route + "/graph/build", "build", json={"extract": True, "max_calls": 8})
            completed = self.run_until(client, build["job_id"], "completed")
            self.assertEqual(completed["results"][0]["nodes"], 5)
            self.assertEqual(client.get(route + "/memory").json()["approved"], [])
            rows = {row["key"]: row["occurrence_id"] for row in client.get(route + "/resources?file=game.csv").json()["resources"]}
            xlsx_item = client.get(route + "/resources?file=localization.xlsx").json()["resources"][0]["occurrence_id"]
            po_item = client.get(route + "/resources?file=localization.po").json()["resources"][0]["occurrence_id"]
            verdict = {"verdict": "pass", "reason": "controlled fixture"}
            for item, target in ((rows["a"], "灵药"), (rows["b"], "使用灵药。"), (xlsx_item, "查看任务"), (po_item, "打开")):
                app.fixture[item] = {"selector": '{"selected_ids":[],"rationale":"fixture"}',
                                     "translator": target, "reviewer": [verdict]}
            app.fixture[rows["b"]]["reviewer"] = [
                {"tool": "search_tm", "lookup": {"query": "Elixir"}, "result": verdict}, verdict,
            ]

            def translate(item, key):
                response = post("/jobs", key, json={"selection": {"project": project["project_id"]},
                                                    "targets": [item], "intents": ["translate", "export"]})
                return self.run_until(client, response["job_id"], "completed")

            batches = post(route + "/translation-batches", "batches", json={
                "targets": [rows["a"], rows["b"]], "batch_size": 1, "max_calls_per_batch": 8,
            })
            first_job, second_job = [job["job_id"] for job in batches["jobs"]]
            self.run_until(client, first_job, "completed")
            second = self.run_until(client, second_job, "completed")
            evidence = client.get(second["items"][0]["evidence_url"]).json()
            received, = evidence["source_lookups"][0]["result"]["evidence"]
            self.assertEqual(received["occurrence_id"], rows["a"])
            self.assertEqual(received["location"]["row"], 2)
            self.assertEqual(evidence["actual_context"]["memory_refs"], [received["memory_ref"]])
            listed = client.get("/jobs", params={"project": project["project_id"]}).json()["jobs"]
            self.assertEqual([job["job_id"] for job in listed][:2], [second_job, first_job])
            history = client.get(f"/jobs/{second_job}/versions/{rows['b']}").json()
            self.assertEqual(history["versions"][0]["target"], "使用灵药。")
            feedback = post("/feedback", "same-file-feedback", json={
                "job_id": first_job, "step": 0, "occurrence_id": rows["a"], "category": "correction", "reason": "复核示例",
            })
            candidate = post("/policy/candidates", "candidate", json={
                "feedback_ids": [feedback["feedback_id"]], "changes": {"handoff_template": "请检查 {reason}"},
            })
            candidate_url = f"/policy/candidates/{candidate['candidate_id']}"
            self.assertEqual(client.get(candidate_url + "/cases", params={"job_id": second_job, "dataset": "validation"}).json(),
                             {"cases": []})
            blocked = client.post(candidate_url + "/replay", json={
                "mode": "record", "dataset": "validation",
                "cases": [{"job_id": second_job, "step": 0, "occurrence_id": rows["b"]}],
            })
            self.assertEqual(blocked.json()["reason"], "held_out_case_overlaps_feedback")
            with closing(app.open()) as repo:
                data = repo.get(second["job_id"])
                self.assertEqual(data["config"]["roles"]["translator"]["model"], "fixture-translation")

            entered, release_item = Event(), Event()
            model_item = app._model_item

            def wait_at_item(*args):
                entered.set()
                self.assertTrue(release_item.wait(8))
                return model_item(*args)

            with patch.object(app, "_model_item", side_effect=wait_at_item):
                sheet_job = post("/jobs", "sheet", json={"selection": {"project": project["project_id"]},
                                 "targets": [xlsx_item], "intents": ["translate", "export"]})["job_id"]
                try:
                    self.assertTrue(entered.wait(8))
                    self.assertTrue(post(f"/jobs/{sheet_job}/pause", "pause")["pause_requested"])
                finally:
                    release_item.set()
                paused = self.run_until(client, sheet_job, "paused")
                self.assertEqual(paused["items"][0]["status"], "committed")
                post(f"/jobs/{sheet_job}/resume", "resume", json={"requirements": "确认英文资源样式"})
                self.run_until(client, sheet_job, "completed")
            translate(po_item, "catalog")
            base = route + "/imports/"
            exported = client.get(base + imported["import_id"] + "/export").text
            header, first, second, excluded = csv.reader(StringIO(exported))
            self.assertEqual(header[-1], "translation")
            self.assertEqual([first[-1], second[-1], excluded[-1]], ["灵药", "使用灵药。", ""])
            self.assertEqual(len(excluded), len(header))
            excel = load_workbook(BytesIO(client.get(base + xlsx["import_id"] + "/export").content))
            self.assertEqual((excel["Dialog"]["C2"].value, excel["Dialog"]["D2"].value), ("查看任务", "=1+1"))
            excel.close()
            self.assertIn('msgstr "打开"', client.get(base + po["import_id"] + "/export").text)

            draft = post(route + "/memory/drafts", "term-draft", json={"kind": "term", "occurrence_id": rows["a"],
                         "source_version": 1, "quote": "Elixir", "source": "Elixir", "target": "药水", "mandatory": True})
            self.assertEqual(draft["status"], "draft")
            self.assertEqual(len(client.get(route + "/memory").json()["approved"]), 4)
            approved = post(route + f'/memory/{draft["record_id"]}/{draft["version"]}/approve', "approve-term",
                            json={"reason": "项目确认"})
            self.assertEqual(approved["status"], "approved")
            self.assertNotIn("灵药", [row["content"].get("target") for row in client.get(route + "/memory").json()["approved"]])

            updated = post(upload, "changed", content=CSV.replace(b"a,Elixir,", b"a,Greater Elixir,"))
            self.assertEqual(updated["changes"], {"added": 0, "changed": 1, "removed": 0, "unchanged": 2})
            self.assertFalse(client.get(route).json()["graph_ready"])
            after = client.get(route + "/memory").json()
            self.assertEqual({row["content"]["target"] for row in after["approved"]}, {"查看任务", "打开"})
            rebuilt = post(route + "/graph/build", "update-graph", json={"batch_size": 3, "max_calls": 1})
            self.assertEqual(self.run_until(client, rebuilt["job_id"], "completed")["budget"]["reserved_or_spent"], 1)

    def test_openai_chat_local_profiles_workspace_roles_and_graph_budget(self):
        from types import SimpleNamespace
        from workbench.models import Responses
        from run_p0 import CONFIG, PI, load

        with TemporaryDirectory() as path, TestClient(create_app(Path(path), offline=True, projects_only=True)) as client:
            def post(url, key, **kwargs):
                response = client.post(url, headers={"Idempotency-Key": key}, **kwargs)
                self.assertTrue(response.is_success, response.text)
                return response.json()

            api_profile = post("/model-profiles", "api-prof", json={
                "name": "OpenAI Chat", "model": "gpt-4.1-mini", "base_url": "https://api.openai.com/v1",
                "api_key_env": "OPENAI_API_KEY", "mode": "api", "api_format": "chat_completions", "thinking": "disabled",
            })
            self.assertEqual(api_profile["api_format"], "chat_completions")
            self.assertEqual(api_profile["mode"], "api")

            local_profile = post("/model-profiles", "local-prof", json={
                "name": "Ollama Qwen3", "model": "qwen3:8b", "base_url": "http://127.0.0.1:11434/v1",
                "mode": "local", "api_format": "chat_completions", "thinking": "disabled",
            })
            self.assertTrue(local_profile["credential_configured"])

            # Bind workspace default role and verify delete guard while in use
            res = client.put("/workspace/model-roles", headers={"Idempotency-Key": "ws-bind"},
                             json={"roles": {"extractor": local_profile["profile_id"]}})
            self.assertEqual(res.status_code, 200, res.text)
            self.assertEqual(client.get("/workspace").json()["default_models"]["extractor"], local_profile["profile_id"])
            self.assertEqual(client.delete(f"/model-profiles/{local_profile['profile_id']}").status_code, 409)
            self.assertEqual(client.delete(f"/model-profiles/{api_profile['profile_id']}").status_code, 200)

            # Verify graph build splits large rows dynamically based on max_input_tokens
            project = post("/projects", "p-budget", json={"name": "Budget Test", "source_lang": "en", "target_lang": "zh"})
            long_csv = ("id,source_text,text_type,context\n"
                        + "".join(f"row_{i},{'word ' * 180},dialogue,\n" for i in range(4))).encode()
            post(f"/projects/{project['project_id']}/imports?filename=long.csv", "imp-long", content=long_csv)
            build = post(f"/projects/{project['project_id']}/graph/build", "bld-long", json={
                "extract": True, "batch_size": 10, "max_calls": 10, "max_input_tokens": 3600, "max_output_tokens": 4096,
            })
            self.assertGreater(build["batches"], 1)
            self.run_until(client, build["job_id"], "completed")

        # Verify chat_completions adapter sends extra_body thinking disabled and gracefully falls back on BadRequestError
        from openai import BadRequestError
        calls = []

        class FakeCompletions:
            def create(self, **kwargs):
                calls.append(kwargs)
                if "extra_body" in kwargs:
                    raise BadRequestError("unsupported extra_body", response=SimpleNamespace(status_code=400, headers={}, request=None), body={})
                return SimpleNamespace(
                    id="chatcmpl-1", model=kwargs["model"],
                    choices=[SimpleNamespace(message=SimpleNamespace(content='{"entities":[],"relations":[]}'), finish_reason="stop")],
                    usage=SimpleNamespace(model_dump=lambda: {"total_tokens": 10}),
                )

        class FakeOpenAI:
            def __init__(self, **kwargs):
                self.chat = SimpleNamespace(completions=FakeCompletions())
            def __enter__(self): return self
            def __exit__(self, *args): pass

        cfg = {**load(CONFIG), "roles": {"extractor": {
            "model": "qwen3:8b", "base_url": "http://127.0.0.1:11434/v1", "api_key_env": None,
            "api_format": "chat_completions", "thinking": "disabled", "timeout_seconds": 30,
        }}}
        with patch("workbench.models.OpenAI", FakeOpenAI):
            text, _ = Responses(cfg, lambda *a, **k: None, PI, offline=False).ask("extractor", "extract")
        self.assertIn("entities", text)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].get("extra_body"), {"thinking": {"type": "disabled"}})
        self.assertEqual(calls[1].get("reasoning_effort"), "none")

    def test_resource_status_inspect_and_ask_occurrence(self):
        with TemporaryDirectory() as path, TestClient(create_app(Path(path), offline=True, projects_only=True)) as client:
            def post(url, key, **kwargs):
                response = client.post(url, headers={"Idempotency-Key": key}, **kwargs)
                self.assertTrue(response.is_success, response.text)
                return response.json()

            project = post("/projects", "p-inspect", json={"name": "Inspect Test", "source_lang": "en", "target_lang": "zh"})
            route = f"/projects/{project['project_id']}"
            csv_data = (
                "id,source_text,text_type,context\n"
                "line_1,Drink Elixir.,ui,menu\n"
                "line_2,Open Gate.,ui,menu\n"
            ).encode()
            post(route + "/imports?filename=ui.csv", "imp-inspect", content=csv_data)

            before = client.get(route + "/resources").json()
            rows = {row["key"]: row["occurrence_id"] for row in before["resources"]}
            self.assertEqual(before["counts"], {"all": 2, "translated": 0, "untranslated": 2, "awaiting_review": 0})
            self.assertTrue(all(r["translation_status"] == "untranslated" for r in before["resources"]))

            build = post(route + "/graph/build", "bld-inspect", json={"batch_size": 10, "max_calls": 2})
            self.run_until(client, build["job_id"], "completed")

            app = client.app.state.workbench
            app.fixture[rows["line_1"]] = {
                "selector": '{"selected_ids":[],"rationale":"fixture"}',
                "translator": "饮用灵药。",
                "reviewer": [{"verdict": "pass", "reason": "术语准确"}],
            }
            job = post("/jobs", "tr-inspect", json={
                "selection": {"project": project["project_id"]},
                "targets": [rows["line_1"]],
                "intents": ["translate"],
            })
            completed = self.run_until(client, job["job_id"], "completed")
            self.assertEqual(completed["status"], "completed")

            after = client.get(route + "/resources").json()
            self.assertEqual(after["counts"], {"all": 2, "translated": 1, "untranslated": 1, "awaiting_review": 0})
            translated_only = client.get(route + "/resources", params={"status": "translated"}).json()
            self.assertEqual([r["occurrence_id"] for r in translated_only["resources"]], [rows["line_1"]])
            untranslated_only = client.get(route + "/resources", params={"status": "untranslated"}).json()
            self.assertEqual([r["occurrence_id"] for r in untranslated_only["resources"]], [rows["line_2"]])

            inspected = client.get(f"/occurrences/{project['project_id']}/{rows['line_1']}/inspect")
            self.assertEqual(inspected.status_code, 200, inspected.text)
            payload = inspected.json()
            self.assertEqual(payload["resource"]["occurrence_id"], rows["line_1"])
            self.assertEqual(payload["translation"]["target"], "饮用灵药。")
            self.assertIsNotNone(payload["evidence"])
            self.assertIn("selection", payload["evidence"])
            self.assertIn("hard_checks", payload["evidence"])
            self.assertIn("graph", payload)
            self.assertIn("memory", payload)

            asked = client.post(
                f"/occurrences/{project['project_id']}/{rows['line_1']}/ask",
                headers={"Idempotency-Key": "ask-line1"},
                json={"question": "这条译文用了哪些记忆和图谱，审校结果是什么？"},
            )
            self.assertEqual(asked.status_code, 200, asked.text)
            ask_data = asked.json()
            self.assertTrue(ask_data["offline"])
            self.assertIn("饮用灵药。", ask_data["answer"])
            self.assertIsInstance(ask_data["citations"], list)

    def test_partial_graph_build_incremental_visibility_and_direct_api_key(self):
        from workbench.models import Responses
        from run_p0 import CONFIG, PI, load

        with TemporaryDirectory() as path, TestClient(create_app(Path(path), offline=True, projects_only=True)) as client:
            def post(url, key, **kwargs):
                response = client.post(url, headers={"Idempotency-Key": key}, **kwargs)
                self.assertTrue(response.is_success, response.text)
                return response.json()

            secret_key = "sk-direct-test-secret-9876"
            direct = post("/model-profiles", "direct-prof", json={
                "name": "DeepSeek Chat",
                "model": "deepseek-chat",
                "base_url": "https://api.deepseek.com/v1",
                "api_key": secret_key,
                "mode": "api",
                "api_format": "chat_completions",
                "thinking": "disabled",
            })
            self.assertTrue(direct["credential_configured"])
            self.assertEqual(direct["credential_source"], "direct")
            self.assertNotIn("api_key", direct)
            self.assertEqual(direct["api_key_masked"], "sk-d••••9876")

            listed = client.get("/model-profiles").json()["profiles"]
            self.assertEqual(len(listed), 1)
            self.assertNotIn("api_key", listed[0])

            updated_res = client.put(
                f"/model-profiles/{direct['profile_id']}",
                headers={"Idempotency-Key": "upd-direct"},
                json={
                    "name": "DeepSeek Chat v2",
                    "model": "deepseek-reasoner",
                    "base_url": "https://api.deepseek.com/v1",
                    "mode": "api",
                    "api_format": "chat_completions",
                    "thinking": "disabled",
                },
            )
            self.assertEqual(updated_res.status_code, 200, updated_res.text)
            updated = updated_res.json()
            self.assertTrue(updated["credential_configured"])
            self.assertEqual(updated["model"], "deepseek-reasoner")
            self.assertEqual(updated["api_key_masked"], "sk-d••••9876")

            workbench = client.app.state.workbench
            self.assertEqual(workbench.projects.resolve_secret(direct["profile_id"]), secret_key)
            cfg = {**load(CONFIG), "roles": {"translator": {"profile_id": direct["profile_id"], **updated}}}
            resolved_cfg = Responses(cfg, lambda *a, **k: None, PI, secret_resolver=workbench.projects.resolve_secret)
            self.assertEqual(resolved_cfg._resolve_api_key(resolved_cfg.config["roles"]["translator"]), secret_key)

            project = post("/projects", "p-partial", json={"name": "Partial Graph", "source_lang": "en", "target_lang": "zh"})
            route = f"/projects/{project['project_id']}"
            csv_data = (
                "id,source_text,text_type,context\n"
                "r1,Alpha potion,item_name,shop\n"
                "r2,Beta shield,item_name,shop\n"
                "r3,Gamma sword,item_name,shop\n"
                "r4,Delta boots,item_name,shop\n"
            ).encode()
            post(route + "/imports?filename=items.csv", "imp-partial", content=csv_data)

            # Build only max_items=2 first
            part1 = post(route + "/graph/build", "bld-part1", json={
                "extract": True, "max_items": 2, "batch_size": 1, "max_calls": 10,
            })
            self.assertEqual(part1["rows"], 2)
            self.run_until(client, part1["job_id"], "completed")

            proj_after_1 = client.get(route).json()
            self.assertTrue(proj_after_1["graph_ready"])
            self.assertEqual(proj_after_1["unextracted"], 2)
            graph_1 = client.get(route + "/graph").json()
            self.assertEqual(graph_1["total"], 4)

            # Build remaining 2 batches with max_calls=1 (partial budget -> stops cleanly after 1 batch AND updates graph)
            part2 = post(route + "/graph/build", "bld-part2", json={
                "extract": True, "batch_size": 1, "max_calls": 1,
            })
            job2 = self.run_until(client, part2["job_id"], "partial")
            self.assertEqual(job2["reason"], "graph_partial_budget_reached")
            self.assertEqual(job2["results"][0]["completed_batches"], 1)
            self.assertEqual(job2["results"][0]["total_batches"], 2)
            proj_after_2 = client.get(route).json()
            self.assertEqual(proj_after_2["unextracted"], 1)
            self.assertEqual(proj_after_2["graph_snapshot"], job2["results"][0]["graph_snapshot"])
            graph_2 = client.get(route + "/graph").json()
            self.assertEqual(graph_2["graph_snapshot"], proj_after_2["graph_snapshot"])

    def test_job_filters_project_notes_and_resource_search(self):
        with TemporaryDirectory() as path, TestClient(create_app(Path(path), offline=True, projects_only=True)) as client:
            def post(url, key, **kwargs):
                response = client.post(url, headers={"Idempotency-Key": key}, **kwargs)
                self.assertTrue(response.is_success, response.text)
                return response.json()

            project = post("/projects", "p-notes", json={"name": "Notes & Search", "source_lang": "en", "target_lang": "zh"})
            route = f"/projects/{project['project_id']}"
            csv_data = (
                "id,source_text,text_type,context,speaker_id\n"
                "dlg_01,Drink the Star Elixir now.,dialogue,tavern,Captain_Aria\n"
                "ui_01,Open Star Map,ui,hud,\n"
                "dlg_02,Return to base.,dialogue,bridge,Officer_Ken\n"
            ).encode()
            post(route + "/imports?filename=story_script.csv", "imp-notes", content=csv_data)

            # 1. Verify resource search works by source text, key, speaker_id, and filename
            by_text = client.get(route + "/resources", params={"query": "Elixir"}).json()
            self.assertEqual([r["key"] for r in by_text["resources"]], ["dlg_01"])
            by_speaker = client.get(route + "/resources", params={"query": "Officer_Ken"}).json()
            self.assertEqual([r["key"] for r in by_speaker["resources"]], ["dlg_02"])
            by_key = client.get(route + "/resources", params={"query": "ui_01"}).json()
            self.assertEqual([r["key"] for r in by_key["resources"]], ["ui_01"])
            by_file = client.get(route + "/resources", params={"query": "story_script"}).json()
            self.assertEqual(len(by_file["resources"]), 3)

            # 2. Verify global background / system prompt notes CRUD + injection
            n1 = post(route + "/notes", "note-1", json={
                "category": "background", "title": "星际世界观", "content": "全员处于星际航行时代。", "enabled": True,
            })
            n2 = post(route + "/notes", "note-2", json={
                "category": "system_prompt", "title": "旧版禁用指令", "content": "不要使用此条。", "enabled": True,
            })
            upd = client.put(f"{route}/notes/{n2['note_id']}", headers={"Idempotency-Key": "note-upd"}, json={
                "category": "system_prompt", "title": "旧版禁用指令", "content": "不要使用此条。", "enabled": False,
            })
            self.assertEqual(upd.status_code, 200, upd.text)
            mem = client.get(route + "/memory").json()
            self.assertEqual(len(mem["notes"]), 2)

            build = post(route + "/graph/build", "bld-notes", json={"batch_size": 10, "max_calls": 2})
            self.run_until(client, build["job_id"], "completed")

            app = client.app.state.workbench
            target_occ = by_text["resources"][0]["occurrence_id"]
            app.fixture[target_occ] = {
                "selector": '{"selected_ids":[],"rationale":"fixture"}',
                "translator": "立刻喝下星辰灵药。",
                "reviewer": [{"verdict": "pass", "reason": "ok"}],
            }
            tr_job = post("/jobs", "tr-notes", json={
                "selection": {"project": project["project_id"]},
                "targets": [target_occ],
                "intents": ["translate"],
            })
            self.run_until(client, tr_job["job_id"], "completed")

            inspected = client.get(f"/occurrences/{project['project_id']}/{target_occ}/inspect").json()
            self.assertEqual([n["title"] for n in inspected["project_notes"]], ["星际世界观"])
            asked = client.post(
                f"/occurrences/{project['project_id']}/{target_occ}/ask",
                headers={"Idempotency-Key": "ask-notes"},
                json={"question": "用了什么背景设定？"},
            ).json()
            self.assertIn("星际世界观", asked["answer"])

            del_res = client.delete(f"{route}/notes/{n2['note_id']}")
            self.assertEqual(del_res.status_code, 200)
            self.assertEqual(len(client.get(route + "/memory").json()["notes"]), 1)

            # 3. Verify /jobs kind, intent, and status filtering + summary
            all_jobs = client.get("/jobs", params={"project": project["project_id"]}).json()
            self.assertEqual(all_jobs["summary"]["kinds"]["graph_build"], 1)
            self.assertEqual(all_jobs["summary"]["kinds"]["project"], 1)
            self.assertEqual(all_jobs["summary"]["intents"]["build_graph"], 1)
            self.assertEqual(all_jobs["summary"]["intents"]["translate"], 1)
            only_graph = client.get("/jobs", params={"project": project["project_id"], "kind": "graph_build"}).json()
            self.assertEqual([j["job_id"] for j in only_graph["jobs"]], [build["job_id"]])
            only_translate = client.get("/jobs", params={"project": project["project_id"], "intent": "translate"}).json()
            self.assertEqual([j["job_id"] for j in only_translate["jobs"]], [tr_job["job_id"]])
            only_completed = client.get("/jobs", params={"project": project["project_id"], "status": "completed"}).json()
            self.assertEqual(only_completed["total"], 2)

    def test_project_rsi_feedback_replay_publish_and_rollback(self):
        with TemporaryDirectory() as path, TestClient(create_app(Path(path), offline=True, projects_only=True)) as client:
            def post(url, key, **kwargs):
                response = client.post(url, headers={"Idempotency-Key": key}, **kwargs)
                self.assertTrue(response.is_success, response.text)
                return response.json()

            project = post("/projects", "p-rsi", json={"name": "RSI Project", "source_lang": "en", "target_lang": "zh"})
            route = f"/projects/{project['project_id']}"
            post(route + "/imports?filename=file_a.csv", "imp-a",
                 content="id,source_text,text_type,context\na1,Check Stargaze status.,dialogue,bridge\n".encode())
            post(route + "/imports?filename=file_b.csv", "imp-b",
                 content="id,source_text,text_type,context\nb1,Open airlock.,ui,hud\n".encode())

            occ_a = client.get(route + "/resources?file=file_a.csv").json()["resources"][0]["occurrence_id"]
            occ_b = client.get(route + "/resources?file=file_b.csv").json()["resources"][0]["occurrence_id"]
            build = post(route + "/graph/build", "bld-rsi", json={"batch_size": 10, "max_calls": 2})
            self.run_until(client, build["job_id"], "completed")

            app = client.app.state.workbench
            verdict = {"verdict": "needs_human", "reason": "evidence not verified"}
            app.fixture[occ_a] = {
                "selector": '{"selected_ids":[],"rationale":"fixture"}',
                "translator": "查看星望状态。",
                "reviewer": [{"tool": "search_source_graph", "lookup": {"issue": "ambiguity", "kind": "entity", "query": "Stargaze"}, "result": verdict}, verdict],
            }
            app.fixture[occ_b] = {
                "selector": '{"selected_ids":[],"rationale":"fixture"}',
                "translator": "打开气闸。",
                "reviewer": [verdict],
            }

            job_a = self.run_until(client, post("/jobs", "job-a", json={
                "selection": {"project": project["project_id"]}, "targets": [occ_a], "intents": ["translate"],
            })["job_id"], "awaiting_review")
            job_b = self.run_until(client, post("/jobs", "job-b", json={
                "selection": {"project": project["project_id"]}, "targets": [occ_b], "intents": ["translate"],
            })["job_id"], "awaiting_review")

            # Verify exact status filter on /jobs
            reviewing = client.get("/jobs", params={"project": project["project_id"], "status": "awaiting_review"}).json()
            self.assertEqual({j["job_id"] for j in reviewing["jobs"]}, {job_a["job_id"], job_b["job_id"]})
            self.assertEqual(reviewing["summary"]["exact_statuses"]["awaiting_review"], 2)

            fb = post("/feedback", "fb-a", json={
                "job_id": job_a["job_id"], "step": 0, "occurrence_id": occ_a,
                "category": "retrieval", "reason": "Missing apostrophe or space in query.",
                "alias": {"query": "Stargaze", "canonical": "Star Gaze"},
            })
            cand = post("/policy/candidates", "cand-1", json={
                "feedback_ids": [fb["feedback_id"]],
                "changes": {
                    "handoff_template": "人工接管：{reason}",
                    "aliases": {"Stargaze": "Star Gaze"},
                    "skill_issues": ["ambiguity"],
                    "lookup_results": 1,
                },
            })
            cand_id = cand["candidate_id"]

            # Auto-discovery of non-overlapping validation cases without specifying job_id
            auto_val = client.get(f"/policy/candidates/{cand_id}/cases", params={"dataset": "validation"}).json()
            self.assertTrue(auto_val.get("auto_discovered"))
            self.assertEqual([c["occurrence_id"] for c in auto_val["cases"]], [occ_b])

            fb_cases = client.get(f"/policy/candidates/{cand_id}/cases", params={"dataset": "feedback"}).json()["cases"]
            self.assertEqual([c["occurrence_id"] for c in fb_cases], [occ_a])

            for dataset, rows in (("feedback", fb_cases), ("validation", auto_val["cases"])):
                rep = post(f"/policy/candidates/{cand_id}/replay", f"rec-{dataset}", json={
                    "mode": "record", "dataset": dataset,
                    "cases": [{"job_id": r["job_id"], "step": r["step"], "occurrence_id": r["occurrence_id"]} for r in rows],
                })
                self.assertTrue(rep["passed"], rep)
            # Strategy replay (no retrieval gap improvement expected in empty offline graph, verify execution works)
            strat = post(f"/policy/candidates/{cand_id}/replay", "strat-val", json={
                "mode": "strategy", "dataset": "validation",
                "cases": [{"job_id": r["job_id"], "step": r["step"], "occurrence_id": r["occurrence_id"]} for r in auto_val["cases"]],
            })
            self.assertTrue(strat["passed"], strat)
            # Candidate without retrieval improvement cannot publish (retrieval_change_needs_observed_evidence_improvement)
            detail = client.get(f"/policy/candidates/{cand_id}").json()
            self.assertTrue(detail["publication_checks"]["record_replay_and_held_out_validation_required"])

            # Now create a clean handoff-template-only candidate, verify full publish + rollback lifecycle
            cand2 = post("/policy/candidates", "cand-2", json={
                "feedback_ids": [fb["feedback_id"]],
                "changes": {"handoff_template": "人工接管：{reason}"},
            })
            c2 = cand2["candidate_id"]
            for dataset, rows in (("feedback", fb_cases), ("validation", auto_val["cases"])):
                rep = post(f"/policy/candidates/{c2}/replay", f"rec2-{dataset}", json={
                    "mode": "record", "dataset": dataset,
                    "cases": [{"job_id": r["job_id"], "step": r["step"], "occurrence_id": r["occurrence_id"]} for r in rows],
                })
                self.assertTrue(rep["passed"], rep)
            pub = post(f"/policy/candidates/{c2}/decide", "pub-c2", json={
                "decision": "publish", "reason": "Handoff template improved.",
            })
            rel_id = pub["release_id"]
            job_after = self.run_until(client, post("/jobs", "job-after", json={
                "selection": {"project": project["project_id"]}, "targets": [occ_a], "intents": ["translate"],
            })["job_id"], "awaiting_review")
            self.assertEqual(job_after["policy_release_id"], rel_id)
            self.assertTrue(job_after["items"][0]["display_reason"].startswith("人工接管："))

            rb = post(f"/policy/releases/{rel_id}/rollback", "rb-c2", json={"reason": "Revert template."})
            self.assertEqual(rb["restored_release_id"], "baseline")
            self.assertEqual(client.get("/policy").json()["active"]["spec"]["handoff_template"], "{reason}")

    def test_web_font_assets_route(self):
        with TemporaryDirectory() as path, TestClient(create_app(Path(path), offline=True, projects_only=True)) as client:
            for name in ("sans.ttf", "light.ttf", "mono.ttf"):
                res = client.get(f"/assets/{name}")
                self.assertEqual(res.status_code, 200)
                self.assertGreater(len(res.content), 1000)
            self.assertEqual(client.get("/assets/missing.ttf").status_code, 404)
            self.assertEqual(client.get("/assets/..%2Frun_api.py").status_code, 404)
