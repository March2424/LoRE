import json
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from fastapi.testclient import TestClient

from run_api import create_app
from localization_agent.schemas.memory import MemoryRef, text_hash
from workbench.job_models import JobModels
from workbench.job_store import Jobs, SCHEMA, encode
from workbench.models import ModelFailure

NORMAL = "naev_df1b7434b65015d2499f"
HUMAN = "naev_0c16ee4efca1905b14d8"


class TaskApiTests(unittest.TestCase):
    def post(self, client, path, key, payload=None):
        kwargs = {} if payload is None else {"json": payload}
        return client.post(path, headers={"Idempotency-Key": key}, **kwargs)

    def wait(self, client, job_id, status):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            data = client.get(f"/jobs/{job_id}").json()
            if data["status"] == status:
                return data
            if data["status"] in {"failed", "cancelled", "completed", "partial"}:
                self.fail(data)
            time.sleep(0.15)
        self.fail(data)

    def test_standalone_translation_runs_both_directions_and_exports_approved_text(self):
        with tempfile.TemporaryDirectory() as tmp, TestClient(create_app(Path(tmp), offline=True)) as client:
            app = client.app.state.workbench
            verdict = {"verdict": "pass", "reason": "controlled fixture"}
            for index, (source_lang, target_lang, source, translation) in enumerate((
                ("zh", "en", "  飞船已抵达 {station}。\n请等待。  ", "  The ship has arrived at {station}.\nPlease wait.  "),
                ("en", "zh", "The hatch is sealed.", "舱门已经关闭。"),
            ), start=1):
                run_id = UUID(int=index)
                item_id = f"text-{run_id.hex}"
                fixture = {"selector": '{"selected_ids":[],"rationale":"fixture"}',
                           "translator": translation, "reviewer": [verdict]}
                if target_lang == "en":
                    fixture.update(translator=translation.replace("{station}", "the station"), repairer=translation,
                                   reviewer=[{"verdict": "repair", "reason": "Restore placeholder"}, verdict])
                app.fixture[item_id] = fixture
                payload = {"source_text": source, "target_lang": target_lang}
                with patch("workbench.jobs.uuid4", return_value=run_id):
                    response = self.post(client, "/jobs", f"text-{index}", payload)
                self.assertEqual(response.status_code, 202, response.text)
                job = self.wait(client, run_id.hex, "completed")
                self.assertEqual((job["source_lang"], job["target_lang"]), (source_lang, target_lang))
                item, = job["items"]
                self.assertEqual((item["source"], item["target"]), (source, translation))
                evidence = client.get(item["evidence_url"]).json()
                self.assertEqual(evidence["retrieved"], [])
                self.assertEqual(len(evidence["reviews"]), 2 if target_lang == "en" else 1)
                export = client.get(f"/jobs/{run_id.hex}/files/export.json").json()
                self.assertEqual((export["source_lang"], export["target_lang"]), (source_lang, target_lang))
                self.assertEqual(export["records"], [{"occurrence_id": item_id, "target": translation, "version": 1}])
                with closing(app.open()) as repo:
                    run = repo.memory.run(run_id.hex)
                    self.assertEqual((run["project"], run["source_lang"], run["target_lang"]),
                                     (f"text:{run_id.hex}", source_lang, target_lang))
                    calls = repo.audit(run_id.hex, "model_call", item_id, lane=f"0:{item_id}")
                    for call in (call for call in calls if call["role"] in {"translator", "repairer", "reviewer"}):
                        prompt = json.loads(call["input"].split("Resource payload:\n", 1)[1])
                        self.assertEqual((prompt["source_lang"], prompt["target_lang"]), (source_lang, target_lang))

    def test_reviewed_tm_from_the_same_job_is_rechecked_and_saved_as_a_dependency(self):
        first = "naev_b4f6f496fb782035fac1"
        second = "naev_55be14f062140f966e4e"
        with tempfile.TemporaryDirectory() as tmp, TestClient(create_app(Path(tmp), offline=True)) as client:
            app = client.app.state.workbench
            verdict = {"verdict": "pass", "reason": "controlled fixture"}
            for item, target, reviews in (
                (first, "皇帝之拳", [verdict]),
                (second, "婚礼在皇帝之拳举行。", [
                    {"tool": "search_tm", "lookup": {"query": "Emperor's Fist"}, "result": verdict}, verdict]),
            ):
                app.catalog[item] = app.resources[item]
                app.cases[item] = {"initial_evidence_ids": [item], "allowed_evidence_ids": [item]}
                app.fixture[item] = {"selector": '{"selected_ids":[],"rationale":"fixture"}',
                                     "translator": target, "reviewer": reviews}
            response = self.post(client, "/jobs", "tm-test", {"intent": "translate", "targets": [first, second],
                                                               "target_lang": "zh"})
            self.assertEqual(response.status_code, 202, response.text)
            job = self.wait(client, response.json()["job_id"], "completed")
            self.assertEqual([item["status"] for item in job["items"]], ["committed", "committed"])
            details = client.get(job["items"][1]["evidence_url"]).json()
            receipt, = details["source_lookups"]
            evidence, = receipt["result"]["evidence"]
            self.assertEqual((receipt["name"], evidence["occurrence_id"]), ("search_tm", first))
            self.assertEqual(details["actual_context"]["memory_refs"], [evidence["memory_ref"]])
            self.assertEqual(len(details["reviews"]), 2)
            with closing(app.open()) as repo:
                saved = repo.memory.get(MemoryRef(**repo.version(second, 1)["memory_ref"]))
                self.assertEqual([asdict(ref) for ref in saved.dependencies], [evidence["memory_ref"]])

    def test_clarification_restart_duplicate_approval_and_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            with TestClient(create_app(directory, offline=True)) as client:
                request = {"text": "翻译并导出"}
                first = self.post(client, "/jobs", "create", request)
                self.assertEqual(first.status_code, 202, first.text)
                job = first.json()["job_id"]
                self.assertEqual(self.post(client, "/jobs", "create", request).json()["job_id"], job)
                pending = self.wait(client, job, "awaiting_input")
                self.assertEqual(set(pending["missing"]), {"targets", "target_lang"})
                endpoint = f"/jobs/{job}/continue"
                empty = self.post(client, endpoint, "empty-clarification", {})
                self.assertEqual(empty.status_code, 422, empty.text)
                self.assertEqual(empty.json()["reason"], "clarification_requires_new_information")
                invalid = {"targets": ["1"], "target_lang": "zh"}
                first_attempt = self.post(client, endpoint, "invalid-resource", invalid)
                self.assertEqual(first_attempt.status_code, 202, first_attempt.text)
                self.assertIn("1", self.wait(client, job, "awaiting_input")["missing"])
                self.assertEqual(self.post(client, endpoint, "invalid-resource", invalid).json(), first_attempt.json())
                self.assertEqual(self.post(client, endpoint, "unchanged-resource", invalid).status_code, 422)
                accepted = self.post(client, f"/jobs/{job}/continue", "continue",
                                     {"targets": [NORMAL, HUMAN], "target_lang": "zh"})
                self.assertEqual(accepted.status_code, 202, accepted.text)
                review = self.wait(client, job, "awaiting_review")
                self.assertEqual([i["status"] for i in review["items"]], ["committed", "needs_human"])
                self.assertEqual(review["steps"][1]["depends_on"], [0])
                self.assertEqual(client.get(f"/jobs/{job}/files/export.json").status_code, 404)

            with TestClient(create_app(directory, offline=True)) as client:
                recovered = self.wait(client, job, "awaiting_review")
                item = recovered["items"][1]
                self.assertEqual(item["draft_hash"], review["items"][1]["draft_hash"])
                evidence = client.get(item["evidence_url"]).json()
                self.assertEqual(evidence["source_lookups"][0]["result"]["status"], "out_of_scope")
                endpoint = f"/jobs/{job}/items/0/{HUMAN}/approve"
                approval = {"expected_version": 0, "expected_draft_hash": item["draft_hash"],
                            "target": "伊尼奥斯修道院", "reason": "复核专名译法"}
                lock = f"/jobs/{job}/versions/{HUMAN}"
                self.assertEqual(self.post(client, lock + "/lock", "lock", {"expected_version": 0}).status_code, 200)
                self.assertEqual(self.post(client, endpoint, "locked-attempt", approval).status_code, 409)
                self.assertEqual(self.post(client, lock + "/unlock", "unlock", {"expected_version": 0}).status_code, 200)
                self.assertEqual(self.post(client, endpoint, "stale", {**approval, "expected_draft_hash": "stale"}).status_code, 409)
                with ThreadPoolExecutor(max_workers=2) as pool:
                    responses = list(pool.map(lambda _: self.post(client, endpoint, "approve", approval), range(2)))
                self.assertEqual([r.status_code for r in responses], [200, 200], [r.text for r in responses])
                self.assertEqual(responses[0].json(), responses[1].json())
                self.assertEqual(responses[0].json()["version"], 1)
                feedback, = client.get("/feedback").json()["feedback"]
                self.assertEqual((feedback["before"], feedback["after"], feedback["reason"]),
                                 (item["target"], approval["target"], approval["reason"]))
                completed = self.wait(client, job, "completed")
                self.assertEqual(completed["counts"]["exportable"], 2)
                export = client.get(f"/jobs/{job}/files/export.json").json()
                self.assertEqual([entry["occurrence_id"] for entry in export["records"]], [NORMAL, HUMAN])
                self.assertEqual(export["exclusions"], [])
                self.assertIn("translation_export", [artifact["type"] for artifact in completed["artifacts"]])
                events = client.get(f"/jobs/{job}/events").text
                ids = [int(line[4:]) for line in events.splitlines() if line.startswith("id: ")]
                tail = client.get(f"/jobs/{job}/events", headers={"Last-Event-ID": str(ids[-2])}).text
                self.assertEqual([int(line[4:]) for line in tail.splitlines() if line.startswith("id: ")], ids[-1:])
                for intent, extra in (
                    ("review", {"target_lang": "zh"}),
                    ("explain", {}),
                    ("propose_update", {"term": {"source": "landing", "target": "着陆"}}),
                ):
                    response = self.post(client, "/jobs", intent, {"intent": intent, "targets": [NORMAL], **extra})
                    record = self.wait(client, response.json()["job_id"], "completed")
                    self.assertEqual(record["results"][0]["intent"], intent)
            with sqlite3.connect(directory / "workbench.sqlite") as db:
                self.assertEqual(db.execute("SELECT count(*) FROM p2_versions WHERE item=?", (HUMAN,)).fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT count(*) FROM p2_calls WHERE job=? AND lane='intent'", (job,)).fetchone()[0], 1)
                saved, = db.execute("SELECT data FROM p2_versions WHERE item=?", (HUMAN,)).fetchone()
                self.assertEqual(json.loads(saved)["reviewer"], "human:editor")

    def test_call_ledger_reuses_receipt_and_stops_unknown_attempt(self):
        class Provider:
            config = {"max_calls": 5}
            offline = True
            calls = 0
            trace = staticmethod(lambda *args, **kwargs: None)

            def ask(self, role, prompt, **kwargs):
                self.calls += 1
                return "saved response", "provider-1"

        with closing(Jobs(":memory:")) as repo:
            repo.db.executescript(SCHEMA)
            provider = Provider()
            first = JobModels(provider, repo, "job", "item")
            self.assertEqual(first.ask("selector", "choose")[1], "provider-1")
            with repo.db:
                signature = text_hash(encode(["translator", "translate", None]))
                repo.db.execute("INSERT INTO p2_calls VALUES (?,?,?,?,?,?,?,NULL)",
                                ("job", "item", 2, 0, signature, "started", 1))
            after_restart = JobModels(provider, repo, "job", "item")
            self.assertEqual(after_restart.ask("selector", "choose")[1], "provider-1")
            with self.assertRaisesRegex(ModelFailure, "unknown_provider_result"):
                after_restart.ask("translator", "translate")
            self.assertEqual(provider.calls, 1)
