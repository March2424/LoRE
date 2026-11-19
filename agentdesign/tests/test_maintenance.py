from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import unittest

from fastapi.testclient import TestClient

from run_api import create_app
from localization_agent.schemas.memory import Evidence, MemoryRef, TranslationCheck, text_hash

REVISE = "naev_df1b7434b65015d2499f"
SATISFIED = "naev_9d73a8a0226dd4866195"
OUTSIDE = "naev_08f14716b9b6ab4510f4"
LOCKED = "naev_fca9687c34e3c61cebcd"
DEPENDENT = "naev_0c16ee4efca1905b14d8"
SEED = {REVISE: "准许降落。", SATISFIED: "准许着陆（降落）。", OUTSIDE: "拒绝降落请求。", LOCKED: "允许降落。"}


class MaintenanceTests(unittest.TestCase):
    def seed(self, service):
        with closing(service.open()) as repo:
            repo.memory.create_snapshot("seed-knowledge", "naev")
            repo.memory.start_run("seed", "naev", None, "en", "zh", "seed-knowledge", mode="game_resources")
            for key, target in SEED.items():
                source = repo.memory.source("naev", Evidence(key, 1, ""))
                check = TranslationCheck(key, 1, text_hash(target), "seed-approved-fixture", "human:test", True)
                saved = repo.memory.commit_translation(source, "seed", target, check)
                with repo.db:
                    repo.append_version(key, 1, {
                        "origin_job": "seed", "source_version": 1, "target": target,
                        "memory_ref": {"record_id": saved.ref.record_id, "version": saved.ref.version},
                        "reviewer": check.reviewer,
                    })
            with repo.db:
                repo.set_head(LOCKED, 1, True)

    def test_preview_apply_conflict_and_append_only_rollback(self):
        with TemporaryDirectory() as temp, TestClient(create_app(Path(temp), offline=True)) as client:
            service = client.app.state.workbench
            self.seed(service)

            def preview(body):
                response = client.post("/maintenance/preview", json=body)
                self.assertEqual(response.status_code, 201, response.text)
                return response.json()

            def action(plan, verb, key):
                return client.post(f'/maintenance/{plan["plan_id"]}/{verb}', headers={"Idempotency-Key": key})

            request = {"source": "Landing", "target": "降落", "scope": {"occurrence_ids": [REVISE, SATISFIED, LOCKED]}}
            seed_plan = preview(request)
            seed_rule = action(seed_plan, "apply", "create")
            self.assertEqual(seed_rule.status_code, 202, seed_rule.text)
            self.assertIsNone(seed_rule.json()["job_id"])
            changed = {**request, "rule_id": seed_plan["rule"]["rule_id"], "target": "着陆"}
            obsolete = preview(changed)
            with closing(service.open()) as repo, repo.transaction():
                repo.append_version(REVISE, 2, repo.version(REVISE, 1))
            self.assertEqual(action(obsolete, "apply", "stale").status_code, 409)
            with closing(service.open()) as repo:
                before = (repo.head(REVISE), repo.db.execute("SELECT COUNT(*) FROM records").fetchone()[0])
            plan = preview(changed)
            states = {row["occurrence_id"]: row["status"] for row in plan["items"]}
            self.assertEqual(states, {REVISE: "needs_review", SATISFIED: "no_change",
                                      OUTSIDE: "out_of_scope", LOCKED: "locked"})
            with closing(service.open()) as repo:
                self.assertEqual(before, (repo.head(REVISE), repo.db.execute("SELECT COUNT(*) FROM records").fetchone()[0]))
            applied = action(plan, "apply", "apply")
            self.assertEqual(applied.status_code, 202, applied.text)
            self.assertEqual(applied.json(), action(plan, "apply", "apply").json())
            job = applied.json()["job_id"]
            deadline = monotonic() + 30
            while monotonic() < deadline:
                state = client.get(f"/jobs/{job}").json()
                if state["status"] in {"completed", "failed", "awaiting_review"}:
                    break
                sleep(.15)
            self.assertEqual(state["status"], "completed", state)
            result = client.get(f'/maintenance/{plan["plan_id"]}').json()
            revised = next(row for row in result["items"] if row["occurrence_id"] == REVISE)
            self.assertEqual((revised["after"], revised["after_version"], revised["model_calls"]), ("准许着陆。", 3, 4))
            self.assertTrue(all(row["model_calls"] == 0 for row in result["items"] if row is not revised))
            with closing(service.open()) as repo:
                for key in (SATISFIED, OUTSIDE, LOCKED):
                    self.assertEqual(repo.head(key)[0], 1)
                self.assertEqual(repo.head(LOCKED), (1, True))
                release = repo.version(REVISE, 3)
                saved = repo.memory.get(MemoryRef(**release["memory_ref"]))
                self.assertIn(MemoryRef(**result["rule"]["memory_ref"]), saved.dependencies)
                self.assertFalse(service._eligible(repo, LOCKED, 1))
            reverted = action(plan, "rollback", "rollback")
            self.assertEqual(reverted.status_code, 200, reverted.text)
            self.assertEqual(reverted.json()["rule_version"], 3)
            self.assertEqual(reverted.json()["versions"][0]["after_version"], 4)
            with closing(service.open()) as repo:
                self.assertEqual(repo.version(REVISE, 4)["target"], SEED[REVISE])
                self.assertEqual(repo.version(REVISE, 3)["target"], "准许着陆。")
            moved = preview({**changed, "scope": {"occurrence_ids": [OUTSIDE]}})
            move_rows = {row["occurrence_id"]: row for row in moved["items"]}
            self.assertEqual(move_rows[REVISE]["reason"], "rule_no_longer_applies")
            self.assertEqual(move_rows[OUTSIDE]["reason"], "new_term_missing")
            other = preview({"source": "permission", "target": "准许", "scope": {"occurrence_ids": [REVISE]}})
            self.assertEqual(action(other, "apply", "new-knowledge").status_code, 202)
            self.assertEqual(action(moved, "apply", "knowledge-conflict").status_code, 409)

            with closing(service.open()) as repo:
                neighbor = repo.memory.source("naev", Evidence(REVISE, 1, ""))
                source = repo.memory.source("naev", Evidence(DEPENDENT, 1, ""))
                target = "伊尼奥斯修道院"
                check = TranslationCheck(DEPENDENT, 1, text_hash(target), "seed-related", "human:test", True)
                record = repo.memory.commit_translation(source, "seed", target, check, context_manifest={
                    "sources": [{"occurrence_id": REVISE, "source_version": 1, "quote": neighbor.source}],
                    "items": [{"category": "source", "text": neighbor.source}],
                })
                with repo.db:
                    repo.append_version(DEPENDENT, 1, {"origin_job": "seed", "source_version": 1, "target": target,
                                                        "memory_ref": {"record_id": record.ref.record_id, "version": 1}})
                    repo.set_head(DEPENDENT, 1, True)
            cascade = preview(changed)
            dependent = next(row for row in cascade["items"] if row["occurrence_id"] == DEPENDENT)
            self.assertEqual(dependent["status"], "locked")
            self.assertIn("reverse_dependency", dependent["discovery"])
