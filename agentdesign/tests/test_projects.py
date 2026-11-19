from contextlib import closing
from io import BytesIO
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
