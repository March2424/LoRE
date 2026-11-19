"""Scoped glossary revisions and version-bound maintenance plans."""

from contextlib import closing
from dataclasses import asdict, replace
import json
from uuid import uuid4

from localization_agent.retrieval.service import candidate_from_memory
from localization_agent.schemas.memory import Evidence, MemoryRecord, MemoryRef, Scope

from .evidence import ScopedReviewerRetriever
from .job_store import JobError, encode

SCHEMA = """
CREATE TABLE IF NOT EXISTS p4_glossary_clock (epoch INTEGER NOT NULL);
INSERT INTO p4_glossary_clock SELECT 0 WHERE NOT EXISTS (SELECT 1 FROM p4_glossary_clock);
CREATE TABLE IF NOT EXISTS p4_rules (
    id TEXT NOT NULL, version INTEGER NOT NULL, data TEXT NOT NULL, PRIMARY KEY(id, version));
CREATE TABLE IF NOT EXISTS p4_plans (id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS p4_pending (
    item TEXT NOT NULL, version INTEGER NOT NULL, plan TEXT NOT NULL, PRIMARY KEY(item, version, plan));
"""
SCOPE_KEY = "p4_occurrence_id"
MAX_REVISIONS_PER_PLAN = 12


def epoch(repo):
    return repo.db.execute("SELECT epoch FROM p4_glossary_clock").fetchone()[0]


def latest_rules(repo):
    return [json.loads(row[0]) for row in repo.db.execute("""
        SELECT r.data FROM p4_rules AS r
        WHERE r.version = (SELECT MAX(version) FROM p4_rules WHERE id=r.id) ORDER BY r.id
    """)]


def applicable(rule, source_id):
    return bool(rule and rule.get("memory_ref") and source_id in rule["members"])


def term_problems(repo, source_id, target):
    return [
        f'Mandatory term missing: {rule["source"]} -> {rule["target"]}'
        for rule in latest_rules(repo) if applicable(rule, source_id) and rule["target"] not in target
    ]


class GlossaryRetriever(ScopedReviewerRetriever):
    def bindings(self, occurrence, snapshot_id):
        metadata, refs = super().bindings(occurrence, snapshot_id)
        return {**metadata, SCOPE_KEY: occurrence.occurrence_id}, refs

    def retrieve(self, occurrence, view, snapshot_id):
        pool = super().retrieve(occurrence, view, snapshot_id)
        effective = replace(occurrence, metadata={**occurrence.metadata, **pool.resolved_metadata})
        hard = {}
        for record in self.memory.eligible(effective, view):
            if record.kind != "term" or not record.content.get("glossary_rule") or record.content["source"] not in occurrence.source:
                continue
            candidate = candidate_from_memory(record, mandatory=True, relations=("MEMORY_TERM",))
            hard[candidate.id] = replace(candidate, text=f'Source: {record.content["source"]}\nMandatory term: {record.content["target"]}')
        ordinary = tuple(item for item in pool.candidates if item.id not in hard)
        if not hard and SCOPE_KEY in pool.resolved_metadata:
            # Preserve old ContextPack hashes for the pre-glossary workflow.
            metadata = {key: value for key, value in pool.resolved_metadata.items() if key != SCOPE_KEY}
            return replace(pool, resolved_metadata=metadata)
        return replace(pool, candidates=tuple(hard.values()) + ordinary)

class Maintenance:
    def __init__(self, workbench):
        self.workbench = workbench

    def _members(self, source, scope):
        keys = scope.get("occurrence_ids") or []
        catalog = self.workbench.resources
        if any(key not in catalog for key in keys):
            raise JobError(422, "unknown_scope_occurrence")
        return sorted(key for key, row in catalog.items()
                      if (not keys or key in keys)
                      and (not scope.get("file") or row["file"] == scope["file"])
                      and (not scope.get("branch_id") or row["branch_id"] == scope["branch_id"])
                      and source in row["label"])

    def _plan_rows(self, repo, old, new):
        heads = [(row["item"], row["version"], bool(row["locked"])) for row in repo.db.execute(
            "SELECT item,version,locked FROM p2_heads WHERE version>0 ORDER BY item")
                 if row["item"] in self.workbench.resources]
        old_ref = MemoryRef(**old["memory_ref"]) if old and old.get("memory_ref") else None
        old_ids = set(old["members"]) if old_ref else set()
        new_ids = set(new["members"])
        rows = {}
        review = set()
        for key, version, locked in heads:
            release = repo.version(key, version)
            saved = repo.memory.get(MemoryRef(**release["memory_ref"]))
            manifest = saved.content.get("context_manifest", {})
            refs = set(saved.dependencies) | {MemoryRef(**ref) for ref in manifest.get("memory_refs", [])}
            source_refs = {ref["occurrence_id"] for ref in manifest.get("sources", [])} - {key}
            source_texts = [item.get("text", "") for item in manifest.get("items", []) if item.get("category") == "source"]
            supplied = ((bool(source_refs & new_ids) and any(new["source"] in text for text in source_texts))
                        or (bool(source_refs & old_ids) and any(old["source"] in text for text in source_texts)))
            reasons = (["old_scope"] if key in old_ids else []) + (["new_scope"] if key in new_ids else [])
            if old_ref in refs:
                reasons.append("rule_dependency")
            if supplied:
                reasons.append("retrieved_source_evidence")
            if key in new_ids and new["target"] not in release["target"] and not locked:
                review.add(key)
            rows[key] = (version, locked, release, saved, refs, source_refs, reasons)
        # Reverse walk over the exact dependencies and source IDs supplied to current releases.
        reached = review.copy()
        affected_refs = {rows[key][3].ref for key in reached}
        while True:
            added = [key for key, row in rows.items() if key not in reached
                     and (row[4] & affected_refs or row[5] & reached)]
            if not added:
                break
            for key in added:
                rows[key][6].append("reverse_dependency")
            unlocked = [key for key in added if not rows[key][1]]
            reached.update(added)
            affected_refs.update(rows[key][3].ref for key in unlocked)

        items = []
        count = 0
        for key, (version, locked, release, saved, _, _, reasons) in rows.items():
            source = self.workbench.resources[key]["label"]
            lexical = new["source"] in source or bool(old and old["source"] in source)
            if not (reasons or lexical):
                continue
            target = release["target"]
            proposed = None
            if not reasons:
                state, reason = "out_of_scope", "term_occurs_outside_both_scopes"
            elif locked:
                state, reason = "locked", "human_lock"
            elif key in new_ids and new["target"] in target:
                state, reason = "no_change", "target_already_satisfies_term"
            elif key in new_ids or "reverse_dependency" in reasons or "retrieved_source_evidence" in reasons:
                if not repo.memory.is_valid(saved.ref):
                    state, reason = "insufficient_evidence", "previous_translation_evidence_stale"
                elif count >= MAX_REVISIONS_PER_PLAN:
                    state, reason = "insufficient_evidence", "revision_batch_limit"
                else:
                    count += 1
                    state, reason = "needs_review", ("new_term_missing" if key in new_ids else "context_dependency_changed")
                    previous = new.get("previous_target") or (old["target"] if old_ref else None)
                    if key in new_ids and previous and previous in target:
                        proposed = target.replace(previous, new["target"])
            else:
                state, reason = "no_change", "rule_no_longer_applies"
            items.append({"occurrence_id": key, "source": source, "before_version": version, "locked": locked,
                          "status": state, "reason": reason, "discovery": reasons or ["lexical_outside_scope"],
                          "before": target, "proposed": proposed})
        return items

    @staticmethod
    def _load(repo, plan_id):
        row = repo.db.execute("SELECT data FROM p4_plans WHERE id=?", (plan_id,)).fetchone()
        if row is None:
            raise JobError(404, "maintenance_plan_not_found")
        return json.loads(row[0])

    @staticmethod
    def _save(repo, plan):
        repo.db.execute("INSERT OR REPLACE INTO p4_plans VALUES (?,?)", (plan["plan_id"], encode(plan)))

    @staticmethod
    def _check_conflicts(rules, new):
        for rule in rules:
            if (rule["rule_id"] != new["rule_id"] and rule.get("memory_ref") and rule["source"] == new["source"]
                    and rule["target"] != new["target"] and set(rule["members"]).intersection(new["members"])):
                raise JobError(409, "overlapping_conflicting_term")

    def preview(self, request):
        with closing(self.workbench.open()) as repo, repo.transaction():
            rules = latest_rules(repo)
            old = next((rule for rule in rules if rule["rule_id"] == request.get("rule_id")), None)
            if request.get("rule_id") and not old:
                raise JobError(404, "glossary_rule_not_found")
            rule_id = old["rule_id"] if old else uuid4().hex
            scope = request.get("scope", {})
            members = self._members(request["source"], scope)
            if not members:
                raise JobError(422, "term_not_found_in_scope")
            new = {**request, "rule_id": rule_id, "version": (old["version"] if old else 0) + 1, "members": members}
            self._check_conflicts(rules, new)
            plan = {
                "plan_id": uuid4().hex, "status": "preview", "knowledge_epoch": epoch(repo),
                "source_sha256": self.workbench.source_hash, "graph_snapshot": self.workbench.snapshot,
                "old_rule": old, "rule": new, "items": self._plan_rows(repo, old, new),
                "job_id": None,
            }
            self._save(repo, plan)
        return self._public(plan)

    @staticmethod
    def _public(plan):
        return {**plan, "old_rule": Maintenance._summarize_rule(plan["old_rule"]),
                "rule": Maintenance._summarize_rule(plan["rule"])}

    @staticmethod
    def _summarize_rule(rule):
        if rule is None:
            return None
        return {**{key: value for key, value in rule.items() if key != "members"},
                "matched_occurrences": len(rule["members"])}

    def detail(self, plan_id):
        with closing(self.workbench.open()) as repo:
            plan = self._load(repo, plan_id)
            job_id = plan["job_id"]
            outputs = {item["occurrence_id"]: item for item in repo.items(job_id)} if job_id else {}
            restored = {item["occurrence_id"]: item for item in plan.get("rollback", {}).get("versions", [])}
            for item in plan["items"]:
                row = outputs.get(item["occurrence_id"])
                lane = "0:" + item["occurrence_id"]
                item["model_calls"] = repo.db.execute(
                    "SELECT COALESCE(SUM(units),0) FROM p2_calls WHERE job=? AND lane=?", (job_id, lane)
                ).fetchone()[0]
                if row:
                    item.update(result=row["status"], after_version=row.get("version", item["before_version"]),
                                after=row["target"] if row["status"] == "committed" else item["before"],
                                draft=row.get("target"), result_reason=row["reason"],
                                evidence_url=f'/jobs/{job_id}/items/0/{item["occurrence_id"]}/evidence')
                else:
                    item.update(result=item["status"], after_version=item["before_version"], after=item["before"])
                if item["occurrence_id"] in restored:
                    reversal = restored[item["occurrence_id"]]
                    item.update(result="rolled_back", after_version=reversal["after_version"], after=reversal["target"])
            out = self._public(plan)
            if job_id:
                out["job_status"] = repo.get(job_id)["status"]
            return out

    def rules(self):
        with closing(self.workbench.open()) as repo:
            return {"knowledge_epoch": epoch(repo), "rules": list(map(self._summarize_rule, latest_rules(repo)))}

    @staticmethod
    def _publish_rule(repo, previous, rule, *, retired=False):
        """Create approved native memory in the caller's atomic transaction."""
        if previous and previous.get("memory_ref"):
            ref = MemoryRef(**previous["memory_ref"])
            # Retire the term from read views. Release history stays addressable for rollback.
            repo.db.execute("INSERT OR IGNORE INTO invalidations VALUES (?,?,?)",
                            (ref.record_id, ref.version, "glossary_revision_superseded"))
        rule = dict(rule)
        ref = None
        if not retired:
            item = rule["members"][0]
            source = repo.db.execute("SELECT MAX(version) FROM sources WHERE project=? AND id=?",
                                     ("naev", item)).fetchone()[0]
            record = repo.memory._insert(MemoryRecord(
                f'glossary:{rule["rule_id"]}:{rule["version"]}', 0, "term",
                Scope("naev", conditions={SCOPE_KEY: tuple(rule["members"])}),
                {"source": rule["source"], "target": rule["target"], "mandatory": True,
                 "glossary_rule": rule["rule_id"], "rule_version": rule["version"]},
                (Evidence(item, source, rule["source"]),), status="approved", reviewer="human:editor",
            ), 0)
            ref = asdict(record.ref)
        rule["memory_ref"] = ref
        repo.db.execute("INSERT INTO p4_rules VALUES (?,?,?)",
                        (rule["rule_id"], rule["version"], encode(rule)))
        repo.db.execute("UPDATE p4_glossary_clock SET epoch=epoch+1")
        return rule

    def apply(self, plan_id, key):
        with closing(self.workbench.open()) as repo, repo.transaction():
            scope, payload = "p4:" + plan_id, {"action": "apply"}
            repeat = repo.action(scope, key, payload)
            if repeat:
                return repeat
            plan = self._load(repo, plan_id)
            if plan["status"] != "preview":
                raise JobError(409, "maintenance_plan_already_used")
            old, new = plan["old_rule"], plan["rule"]
            if (plan["knowledge_epoch"] != epoch(repo) or plan["source_sha256"] != self.workbench.source_hash
                    or plan["graph_snapshot"] != self.workbench.snapshot
                    or plan["items"] != self._plan_rows(repo, old, new)):
                raise JobError(409, "maintenance_plan_stale_preview_again")
            plan["rule"] = self._publish_rule(repo, old, new)
            selected = [item for item in plan["items"] if item["status"] == "needs_review"]
            repo.db.executemany("INSERT INTO p4_pending VALUES (?,?,?)", [
                (item["occurrence_id"], item["before_version"], plan_id)
                for item in plan["items"] if item["status"] in {"needs_review", "insufficient_evidence"}
                or item["status"] == "locked" and "reverse_dependency" in item["discovery"]
            ])
            if selected:
                plan["job_id"] = self.workbench.enqueue_maintenance(repo, plan, selected)
            plan["status"] = "applied"
            self._save(repo, plan)
            result = {"plan_id": plan_id, "status": "applied", "job_id": plan["job_id"],
                      "rule_version": new["version"]}
            repo.save_action(scope, key, payload, result)
        return result

    def rollback(self, plan_id, key):
        with closing(self.workbench.open()) as repo, repo.transaction():
            scope, payload = "p4:" + plan_id, {"action": "rollback"}
            repeat = repo.action(scope, key, payload)
            if repeat:
                return repeat
            plan = self._load(repo, plan_id)
            if plan["status"] != "applied":
                raise JobError(409, "maintenance_plan_not_rollbackable")
            rule, old = plan["rule"], plan["old_rule"]
            rules = latest_rules(repo)
            active = next(row for row in rules if row["rule_id"] == rule["rule_id"])
            if active["version"] != rule["version"]:
                raise JobError(409, "glossary_version_changed")
            job_id = plan["job_id"]
            if job_id and repo.get(job_id)["status"] not in {"completed", "partial", "failed", "cancelled"}:
                raise JobError(409, "finish_or_cancel_maintenance_first")
            changed = [row for row in repo.items(job_id) if row["status"] == "committed"] if job_id else []
            restores = []
            for row in changed:
                item = row["occurrence_id"]
                current, locked = repo.head(item)
                if locked or current != row["version"]:
                    raise JobError(409, "rollback_translation_locked_or_changed")
                past = repo.version(item, row["expected_version"])
                if not self.workbench._verified_release(repo, item, past):
                    raise JobError(409, "rollback_translation_evidence_stale")
                if repo.db.execute("SELECT 1 FROM p4_pending WHERE item=? AND version=? AND plan<>? LIMIT 1",
                                   (item, row["expected_version"], plan_id)).fetchone():
                    raise JobError(409, "rollback_translation_has_other_pending_review")
                restores.append((item, current, past, row["expected_version"]))
            if old and old.get("memory_ref"):
                self._check_conflicts(rules, old)
            restored = self._publish_rule(repo, rule, {**(old or rule), "version": rule["version"] + 1,
                                                       "rollback_of": rule["version"]},
                                          retired=not old or not old.get("memory_ref"))
            repo.db.execute("DELETE FROM p4_pending WHERE plan=?", (plan_id,))
            versions = []
            for item, current, past, prior in restores:
                if term_problems(repo, item, past["target"]):
                    raise JobError(409, "rollback_target_conflicts_with_active_rule")
                repo.append_version(item, current + 1, {**past, "rolled_back_from": prior,
                                                        "maintenance_rollback": plan_id})
                versions.append({"occurrence_id": item, "from_version": current, "after_version": current + 1,
                                 "restored_version": prior, "target": past["target"]})
            result = {"plan_id": plan_id, "status": "rolled_back", "rule_version": restored["version"],
                      "versions": versions}
            plan.update(status="rolled_back", rollback=result)
            self._save(repo, plan)
            repo.save_action(scope, key, payload, result)
        return result
