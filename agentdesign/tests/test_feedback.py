from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import unittest

from fastapi.testclient import TestClient

from run_api import create_app

TRAIN = ("naev_b4f6f496fb782035fac1", "naev_afca4e62aa91b6bb509f")
HOLDOUT = "naev_55be14f062140f966e4e"
EVIDENCE = "naev_2e54989ac7194e593066"
PLACEHOLDER = "naev_3d06247b787ae70eb060"
ALIAS = {"query": "Emperors Fist", "canonical": "Emperor's Fist"}


class FeedbackTests(unittest.TestCase):
    def test_release_from_isolated_feedback_and_holdout_replays(self):
        with TemporaryDirectory() as path, TestClient(create_app(Path(path), offline=True)) as client:
            app = client.app.state.workbench

            def post(route, payload, key="test"):
                return client.post(route, json=payload, headers={"Idempotency-Key": key})

            for item in (*TRAIN, HOLDOUT, PLACEHOLDER):
                app.catalog[item] = app.resources[item]
                app.cases[item] = {"id": item, "initial_evidence_ids": [item],
                                   "allowed_evidence_ids": [item, EVIDENCE] if item != PLACEHOLDER else [item]}
                verdict = {"verdict": "needs_human", "reason": "evidence not verified"}
                reply = {"lookup": {"issue": "ambiguity", "kind": "entity", "query": ALIAS["query"]},
                         "result": verdict}
                app.fixture[item] = {"selector": '{"selected_ids":[],"rationale":"fixture"}', "translator": "待核对译文",
                                     "reviewer": [reply if item != PLACEHOLDER else verdict]}

            def historical(item, suffix=""):
                response = post("/jobs", {"intent": "translate", "targets": [item], "target_lang": "zh"},
                                "case-" + item + suffix)
                self.assertEqual(response.status_code, 202, response.text)
                job_id = response.json()["job_id"]
                until = monotonic() + 25
                while monotonic() < until:
                    job = client.get(f"/jobs/{job_id}").json()
                    if job["status"] not in {"queued", "running"}:
                        break
                    sleep(.15)
                self.assertEqual(job["status"], "awaiting_review", job)
                return {"job_id": job_id, "step": 0, "occurrence_id": item}, job

            training = [historical(item)[0] for item in TRAIN]
            held, _ = historical(HOLDOUT)
            hard, _ = historical(PLACEHOLDER)
            locked = post(f'/jobs/{held["job_id"]}/versions/{HOLDOUT}/lock', {"expected_version": 0}, "lock")
            self.assertEqual(locked.status_code, 200, locked.text)
            expected = [f"source:{EVIDENCE}@1"]
            feedback_ids = []
            for case in training:
                response = post("/feedback", {**case, "category": "retrieval",
                                "reason": "Editor confirmed spelling of the scoped entity name.", "alias": ALIAS},
                                case["occurrence_id"])
                self.assertEqual(response.status_code, 201, response.text)
                feedback_ids.append(response.json()["feedback_id"])
            self.assertEqual(client.get("/feedback").json()["patterns"][0]["occurrences"], 2)
            response = post("/policy/candidates", {"feedback_ids": feedback_ids, "changes": {
                "handoff_template": "编辑接手 · {reason}", "skill_issues": ["ambiguity"], "lookup_results": 1,
            }})
            self.assertEqual(response.status_code, 201, response.text)
            candidate = response.json()
            self.assertEqual(candidate["spec"]["aliases"]["emperors fist"], "Emperor's Fist")
            endpoint = "/policy/candidates/" + candidate["candidate_id"]
            publish = {"decision": "publish", "reason": "Records and independent evidence passed."}
            self.assertEqual(post(endpoint + "/decide", publish, "too-early").status_code, 409)
            self.assertEqual(post(endpoint + "/replay", {"mode": "record", "dataset": "validation",
                                                        "cases": training}).status_code, 422)

            reports = []
            with closing(app.open()) as repo:
                before = tuple(repo.db.execute("SELECT (SELECT COUNT(*) FROM records),"
                                               "(SELECT COUNT(*) FROM p2_versions), (SELECT COUNT(*) FROM p4_rules)").fetchone())
            for dataset, cases in (("feedback", training), ("validation", [held, hard])):
                for mode in ("record", "strategy"):
                    rows = [{**case, "expected_evidence_ids": expected if case != hard else []} for case in cases]
                    response = post(endpoint + "/replay", {"mode": mode, "dataset": dataset, "cases": rows})
                    self.assertEqual(response.status_code, 201, response.text)
                    report = response.json()
                    reports.append(report)
                    self.assertTrue(report["passed"], report)
                    self.assertTrue(report["isolated"])
                    self.assertEqual(report["metrics"]["after"]["outside_scope"], 0)
                    if mode == "strategy":
                        self.assertEqual(report["evidence_gap_resolution_rate"], 1)
                        self.assertEqual(report["metrics"]["after"]["calls"], 0)
                        scoped_lookup = report["rows"][0]["after"]["lookup_receipts"][0]
                        self.assertGreater(scoped_lookup["excluded_hits"], 0)
                        self.assertEqual(report["rows"][0]["after"]["evidence_ids"], expected)
                    else:
                        self.assertTrue(report["rows"][0]["after"]["display"].startswith("编辑接手"))
                        self.assertEqual(report["rows"][0]["before"]["disposition"],
                                         report["rows"][0]["after"]["disposition"])
                    if dataset == "validation":
                        self.assertTrue(report["rows"][0]["after"]["locked"])
                        self.assertTrue(report["rows"][1]["after"]["hard_problems"])
                        self.assertFalse(report["rows"][1]["after"]["would_commit"])
            with closing(app.open()) as repo:
                after = tuple(repo.db.execute("SELECT (SELECT COUNT(*) FROM records),"
                                              "(SELECT COUNT(*) FROM p2_versions), (SELECT COUNT(*) FROM p4_rules)").fetchone())
                self.assertEqual(before, after)
            release = post(endpoint + "/decide", publish, "publish")
            self.assertEqual(release.status_code, 200, release.text)
            policy_id = release.json()["release_id"]
            runtime, job = historical(TRAIN[0], "-new-policy")
            self.assertEqual(job["policy_release_id"], policy_id)
            self.assertTrue(job["items"][0]["display_reason"].startswith("编辑接手"))
            evidence = client.get(job["items"][0]["evidence_url"]).json()
            self.assertEqual(evidence["source_lookups"][0]["effective_query"], "Emperor's Fist")
            self.assertEqual(evidence["source_lookups"][0]["result"]["status"], "found")
            rollback = post(f"/policy/releases/{policy_id}/rollback", {"reason": "Verify rollback."}, "rollback")
            self.assertEqual(rollback.status_code, 200, rollback.text)
            self.assertEqual(client.get("/policy").json()["active"]["spec"]["aliases"], {})
            self.assertEqual(client.get(f'/jobs/{runtime["job_id"]}').json()["policy_release_id"], policy_id)

            preview = post("/maintenance/preview", {"source": "Imperial", "target": "帝国",
                                                    "scope": {"occurrence_ids": [HOLDOUT]}})
            self.assertEqual(preview.status_code, 201, preview.text)
            plan_id = preview.json()["plan_id"]
            memory = post("/policy/candidates", {"feedback_ids": feedback_ids,
                                                 "changes": {"memory_plan_id": plan_id, "aliases": {}}})
            self.assertEqual(memory.status_code, 201, memory.text)
            memory_candidate = memory.json()
            rejected = post(f'/policy/candidates/{memory_candidate["candidate_id"]}/replay',
                            {"mode": "strategy", "dataset": "validation", "cases": [held]})
            self.assertEqual(rejected.status_code, 201, rejected.text)
            memory_report = rejected.json()
            self.assertFalse(memory_report["passed"])
            self.assertIn("Mandatory term missing: Imperial -> 帝国", memory_report["rows"][0]["after"]["hard_problems"])
            self.assertEqual(client.get(f"/maintenance/{plan_id}").json()["status"], "preview")
            with closing(app.open()) as repo:
                self.assertEqual(repo.db.execute("SELECT COUNT(*) FROM p4_rules").fetchone()[0], before[2])

            self.acceptance = {"candidate": candidate, "reports": reports, "decision": release.json(),
                               "rollback": rollback.json(), "memory_rejection": memory_report,
                               "business_rows_unchanged_by_replay": before == after}
