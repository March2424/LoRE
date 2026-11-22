"""Feedback, isolated replay and explicit policy release."""

from contextlib import closing, contextmanager
from dataclasses import asdict, replace
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from time import monotonic
from uuid import uuid4

from localization_agent.retrieval.service import candidate_from_memory
from localization_agent.retrieval.game_index import normalize_name
from localization_agent.schemas.memory import MemoryRef, text_hash

from .evidence import SourceReview
from .job_context import restore_pack
from .job_store import JobError, Jobs, encode
from .maintenance import SCOPE_KEY, Maintenance, latest_rules
from .models import ModelFailure, Responses, parse_review
from .policy import DEFAULT, active, amended, handoff, prompt_overrides, versions
from .workflow import hard_problems
from run_p0 import PI


class FeedbackLoop:
    def __init__(self, workbench):
        self.workbench = workbench

    @staticmethod
    def _read(repo, table, identity):
        row = repo.db.execute(f"SELECT data FROM {table} WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise JobError(404, "feedback_or_policy_not_found")
        return json.loads(row[0])

    def capture(self, repo, job, item, before, after, reason, *, category="correction", alias=None):
        event = {"feedback_id": uuid4().hex, "job_id": job["job_id"], "step": item["step"],
                 "occurrence_id": item["occurrence_id"], "source_version": item["source_version"],
                 "before": before, "after": after, "reason": reason, "category": category,
                 "alias": alias, "snapshots": job["snapshots"],
                 "policy": job.get("policy", {"release_id": "baseline"}).get("release_id"),
                 "file": self.workbench.resource(job, item["occurrence_id"])["file"],
                 "source_kind": job.get("source_kind", "naev")}
        repo.db.execute("INSERT INTO p5_feedback VALUES (?,?)", (event["feedback_id"], encode(event)))
        return event

    def feedback(self, spec, key):
        with closing(self.workbench.open()) as repo, repo.transaction():
            repeat = repo.action("feedback", key, spec)
            if repeat:
                return repeat
            job = repo.get(spec["job_id"])
            item = repo.item(job["job_id"], spec["step"], spec["occurrence_id"])
            if item["status"] in {"queued", "running"}:
                raise JobError(409, "wait_for_item_outcome")
            lane = f'{item["step"]}:{item["occurrence_id"]}'
            drafts = repo.audit(job["job_id"], "draft", item["occurrence_id"], lane=lane)
            edits = repo.audit(job["job_id"], "human_approval", item["occurrence_id"], lane=lane)
            before = edits[-1]["before"] if edits else drafts[-1]["target"] if drafts else item.get("target")
            result = self.capture(repo, job, item, before, spec.get("after", item.get("target")), spec["reason"],
                                  category=spec["category"], alias=spec.get("alias"))
            repo.save_action("feedback", key, spec, result)
        return result

    @staticmethod
    def _feedback_rows(repo):
        return [json.loads(row[0]) for row in repo.db.execute("SELECT data FROM p5_feedback ORDER BY rowid")]

    def patterns(self):
        with closing(self.workbench.open()) as repo:
            events = self._feedback_rows(repo)
        groups = {}
        for event in events:
            alias = event.get("alias")
            key = (event["category"], alias["query"].casefold(), alias["canonical"]) if alias else (event["category"],)
            groups.setdefault(key, []).append(event)
        return {
            "feedback": events,
            "patterns": [{"category": key[0], "alias": rows[0]["alias"],
                          "occurrences": len({row["occurrence_id"] for row in rows}),
                          "feedback_ids": [row["feedback_id"] for row in rows]}
                         for key, rows in groups.items() if len({row["occurrence_id"] for row in rows}) >= 2],
        }

    def candidate(self, feedback_ids, changes):
        with closing(self.workbench.open()) as repo, repo.transaction():
            evidence = [self._read(repo, "p5_feedback", key) for key in dict.fromkeys(feedback_ids)]
            if not evidence:
                raise JobError(422, "feedback_required")
            changes = dict(changes)
            if "aliases" not in changes:
                pairs = {}
                for event in evidence:
                    alias = event["alias"]
                    if alias:
                        pairs.setdefault((normalize_name(alias["query"]), alias["canonical"]), set()).add(event["occurrence_id"])
                generated = {}
                for (before, after), cases in pairs.items():
                    if len(cases) < 2:
                        continue
                    if before in generated and generated[before] != after:
                        raise JobError(422, "conflicting_feedback_aliases")
                    generated[before] = after
                if generated:
                    changes["aliases"] = generated
            base = active(repo)
            spec = amended(base["spec"], changes)
            if spec == base["spec"]:
                raise JobError(422, "no_policy_change")
            if spec["memory_plan_id"] != base["spec"]["memory_plan_id"] and spec["memory_plan_id"]:
                plan = Maintenance._load(repo, spec["memory_plan_id"])
                if plan["status"] != "preview":
                    raise JobError(409, "memory_candidate_requires_preview")
            result = {"candidate_id": uuid4().hex, "base": base, "spec": spec, "components": versions(spec),
                      "changes": changes, "feedback_ids": [row["feedback_id"] for row in evidence]}
            repo.db.execute("INSERT INTO p5_candidates VALUES (?,?)", (result["candidate_id"], encode(result)))
        return result

    def status(self, candidate_id=None):
        with closing(self.workbench.open()) as repo:
            current = active(repo)
            if candidate_id is None:
                return {"active": current, "candidates": [json.loads(row[0]) for row in repo.db.execute(
                    "SELECT data FROM p5_candidates ORDER BY rowid DESC")],
                        "releases": [json.loads(row[0]) for row in repo.db.execute(
                            "SELECT data FROM p5_releases ORDER BY seq DESC")] + [
                            {"release_id": "baseline", "parent": None, "components": versions(DEFAULT), "spec": DEFAULT}]}
            candidate = self._read(repo, "p5_candidates", candidate_id)
            reports = [json.loads(row[0]) for row in repo.db.execute(
                "SELECT data FROM p5_replays WHERE candidate=? ORDER BY rowid", (candidate_id,))]
            return {"active": current, "candidate": candidate, "replays": reports,
                    "publication_checks": self.publication_checks(repo, candidate, current, reports)}

    def _feedback_for(self, repo, candidate):
        return [self._read(repo, "p5_feedback", key) for key in candidate["feedback_ids"]]

    def _overlaps_feedback(self, job, item, feedback):
        filename = self.workbench.resource(job, item)["file"]
        return any(row["occurrence_id"] == item or (filename and row["file"] == filename) or
                   (job.get("source_kind") == row.get("source_kind") == "text" and
                    job["snapshots"]["source_sha256"] == row["snapshots"]["source_sha256"]) for row in feedback)

    def replay_cases(self, candidate_id, dataset, job_id):
        with closing(self.workbench.open()) as repo:
            candidate = self._read(repo, "p5_candidates", candidate_id)
            feedback = self._feedback_for(repo, candidate)
            if dataset == "feedback":
                refs = {(row["job_id"], row["step"], row["occurrence_id"]) for row in feedback}
            else:
                if not job_id:
                    raise JobError(422, "job_required_for_validation_cases")
                job = repo.get(job_id)
                refs = {(job_id, row["step"], row["occurrence_id"]) for row in repo.items(job_id)
                        if not self._overlaps_feedback(job, row["occurrence_id"], feedback)}
            cases = []
            for identity, step, item in sorted(refs):
                ref = {"job_id": identity, "step": step, "occurrence_id": item}
                try:
                    case = self._case(repo, ref)
                except JobError:
                    continue
                cases.append({**ref, "source": case["item"]["source"],
                              "file": self.workbench.resource(case["job"], item)["file"]})
            return {"cases": cases}

    def _case(self, repo, ref):
        job = repo.get(ref["job_id"])
        item = repo.item(job["job_id"], ref["step"], ref["occurrence_id"])
        if item["status"] in {"queued", "running"}:
            raise JobError(409, "wait_for_item_outcome")
        source = self.workbench.occurrence(repo, job, item)
        if job.get("source_kind") == "project":
            graph = job["snapshots"]["graph"]
            expected = (text_hash(graph), graph)
        elif job.get("source_kind") == "text":
            expected = (text_hash(source.source), f"{source.occurrence_id}:graph")
        else:
            expected = (self.workbench.source_hash, self.workbench.snapshot)
        if (job["snapshots"]["source_sha256"], job["snapshots"]["graph"]) != expected:
            raise JobError(409, "historical_source_snapshot_unavailable")
        lane = f'{item["step"]}:{item["occurrence_id"]}'
        names = ("selection", "draft", "repair", "review", "review_tool", "hard_check", "model_call", "human_approval")
        trace = {name: repo.audit(job["job_id"], name, item["occurrence_id"], lane=lane) for name in names}
        if not trace["selection"] or not item.get("pack") or not trace["review"]:
            raise JobError(422, "complete_review_trace_required")
        edits = trace["human_approval"]
        drafts = [item.get("input_target"), *[event["target"] for name in ("draft", "repair") for event in trace[name]]]
        drafts += [edits[-1]["before"]] if edits else []
        by_hash = {text_hash(draft): draft for draft in drafts if draft is not None}
        first = by_hash.get(trace["review"][0].get("target_hash"))
        last = by_hash.get(trace["review"][-1].get("target_hash"))
        if first is None or last is None:
            raise JobError(422, "historical_review_draft_unavailable")
        outcome = edits[-1] if edits else item
        return {"spec": ref, "job": job, "item": item, "trace": trace, "draft": last, "initial_draft": first,
                "historical_status": outcome["status"], "historical_reason": outcome.get("reason")}

    @staticmethod
    def _recorded_verdict(case):
        verdict = case["trace"]["review"][-1]
        if case["historical_status"] == "needs_human":
            return {"verdict": "needs_human", "reason": case["historical_reason"] or verdict.get("reason")}
        return verdict

    @staticmethod
    def _usage(events):
        observed = [value["usage"] for event in events for value in event.get("receipt", {}).get("calls", [event])
                    if value.get("usage")]
        return {"tokens": sum(row.get("total_tokens", 0) for row in observed) if observed else None,
                "duration_ms": sum(event.get("elapsed_ms", 0) for event in events)}

    def _record_arm(self, case, spec, retriever, source, locked):
        trace, item = case["trace"], case["item"]
        verdict = self._recorded_verdict(case)
        pack = restore_pack(item["pack"])
        problems = hard_problems(retriever, source, case["draft"], pack)
        receipts = [row["result"] for row in trace["review_tool"]]
        supplied = [entry for result in receipts for entry in result.get("evidence", [])]
        return self._arm(case, spec, verdict, supplied, problems, locked, retriever, source, pack, {
            "execution": "recorded_receipts", "usage": self._usage(trace["model_call"]),
            "lookups": len(receipts), "model_calls": 0, "error": verdict.get("error"),
            "lookup_receipts": trace["review_tool"],
        })

    @staticmethod
    def _arm(case, spec, verdict, supplied, problems, locked, retriever, source, pack, extra):
        item, ref = case["item"], case["spec"]
        ids = [entry["evidence_id"] for entry in supplied]
        violations = []
        for entry in supplied:
            try:
                allowed = retriever.receipt_allowed(source, pack, entry)
            except KeyError:
                allowed = False
            if not allowed:
                violations.append(entry["occurrence_id"])
        expected = set(ref.get("expected_evidence_ids", []))
        disposition = verdict.get("verdict")
        return {**extra, "disposition": disposition, "evidence_ids": ids,
                "evidence_hit": bool(expected.intersection(ids)) if expected else None,
                "outside_scope": len(violations), "scope_violations": violations,
                "hard_problems": problems, "locked": locked,
                "would_commit": bool(disposition == "pass" and not problems and not locked and not violations
                                     and not extra.get("requires_recheck")),
                "display": handoff(spec, verdict.get("reason") or "review_unavailable", item["occurrence_id"])
                if disposition == "needs_human" else verdict.get("reason")}

    def _memory_pack(self, scratch, retriever, case, pack, rule):
        if not rule:
            return pack
        old = rule["old_rule"]
        new = rule["rule"]
        item = case["item"]["occurrence_id"]
        remove = old.get("memory_ref") if old else None
        # Replay rule application against the actual historical pack, with isolated memory records.
        retained = tuple(c for c in pack.pool.candidates if not c.memory_ref or asdict(c.memory_ref) != remove)
        snapshot = case["job"]["snapshots"]["knowledge"]
        refs = json.loads(scratch.db.execute("SELECT refs FROM snapshots WHERE id=?", (snapshot,)).fetchone()[0])
        refs = [ref for ref in refs if ref != remove]
        metadata = pack.pool.resolved_metadata
        if item in new["members"]:
            clone = {**new, "rule_id": "replay-" + uuid4().hex}
            saved = Maintenance._publish_rule(scratch, None, clone)
            record = scratch.memory.get(MemoryRef(**saved["memory_ref"]))
            term = candidate_from_memory(record, mandatory=True)
            retained = (replace(term, text=f'Source: {new["source"]}\nMandatory term: {new["target"]}'),) + retained
            refs.append(saved["memory_ref"])
            metadata = {**metadata, SCOPE_KEY: item}
        scratch.db.execute("UPDATE snapshots SET refs=? WHERE id=?", (encode(refs), snapshot))
        pool = replace(pack.pool, candidates=retained, resolved_metadata=metadata)
        allowed = {c.id for c in retained}
        ids = [c.id for c in pack.selected if c.id in allowed]
        try:
            return retriever.build_context(pool, tuple(ids))
        except ValueError:
            return retriever.build_context(pool)

    @staticmethod
    @contextmanager
    def _isolated_arm(scratch):
        scratch.db.execute("SAVEPOINT policy_arm")
        try:
            yield
        finally:
            scratch.db.execute("ROLLBACK TO policy_arm")
            scratch.db.execute("RELEASE policy_arm")

    def _strategy_arm(self, case, spec, scratch, retriever, source, locked, live, memory_plan):
        started = monotonic()
        job, trace = case["job"], case["trace"]
        pack = restore_pack(trace["selection"][-1]["actual_context_pack"])
        pack = self._memory_pack(scratch, retriever, case, pack, memory_plan)
        draft = case["initial_draft"]
        log = []

        def record(kind, **data):
            log.append({"event": kind, **data})

        limits = {**job["lookup_limits"],
                  "max_results": min(job["lookup_limits"]["max_results"], spec["lookup_results"])}
        tool = SourceReview(retriever, source, pack, limits, record, spec)
        initial = hard_problems(retriever, source, draft, pack)
        verdict = self._recorded_verdict(case)
        calls = 0
        error = None
        if live:
            prompts = prompt_overrides(job.get("policy_prompts_base", job["prompts"]), spec)
            query = {"source": source.source, "source_lang": source.source_lang, "target_lang": source.target_lang,
                     "current_resource": json.loads(pack.manifest["current_source_context"]),
                     "selected_evidence": json.loads(pack.model_context), "draft": draft, "hard_check": initial,
                     "editor_requirement": (job["plan"] or {}).get("requirements")}
            models = Responses({**job["config"], "max_calls": min(job["config"]["max_calls"], 2)}, record, PI)
            try:
                reply, _ = models.pi(prompts["reviewer_source"] + "\nResource payload:\n" + json.dumps(query, ensure_ascii=False),
                                     item=source.occurrence_id, tool=tool.call)
                verdict = parse_review(reply["text"])
            except (ModelFailure, ValueError) as failure:
                error = getattr(failure, "code", "replay_reviewer_structure")
                verdict = {"verdict": "needs_human", "reason": error}
            calls = models.calls
        elif trace["review_tool"]:
            query = trace["review_tool"][0]
            tool.call(query["name"], query["args"], query["tool_id"])
        lookups = [event for event in log if event["event"] == "review_tool"]
        if tool.attempted and tool.status != "found" and live:
            verdict = {"verdict": "needs_human", "reason": tool.status}
        supplied = [entry for event in lookups for entry in event["result"]["evidence"]]
        pack = tool.repacked or pack
        problems = hard_problems(retriever, source, draft, pack)
        return self._arm(case, spec, verdict, supplied, problems, locked, retriever, source, pack, {
            "execution": "live_reviewer" if live else "lookup_only_with_recorded_disposition",
            "usage": self._usage([event for event in log if event["event"] == "model_call"]),
            "lookups": len(lookups), "model_calls": calls, "elapsed_ms": round((monotonic() - started) * 1000),
            "lookup_receipts": lookups, "requires_recheck": tool.repacked is not None, "error": error,
        })

    @staticmethod
    def _metrics(rows):
        known = [row for row in rows if row["evidence_hit"] is not None]
        return {
            "cases": len(rows), "evidence_hit_rate": sum(row["evidence_hit"] for row in known) / len(known) if known else None,
            "outside_scope": sum(row["outside_scope"] for row in rows),
            "hard_rule_violations": sum(bool(row["hard_problems"]) for row in rows),
            "unsafe_commits": sum(row["would_commit"] and (row["outside_scope"] > 0 or bool(row["hard_problems"]) or row["locked"])
                                  for row in rows),
            "human_rate": sum(row["disposition"] == "needs_human" for row in rows) / len(rows),
            "calls": sum(row["model_calls"] for row in rows),
            "tokens": sum(row["usage"]["tokens"] for row in rows if row["usage"]["tokens"] is not None)
                      if any(row["usage"]["tokens"] is not None for row in rows) else None,
            "duration_ms": sum(row.get("elapsed_ms", row["usage"]["duration_ms"]) for row in rows),
        }

    def replay(self, candidate_id, options):
        live = options.get("live", False)
        mode = options["mode"]
        dataset = options["dataset"]
        refs = options["cases"]
        if live and (self.workbench.offline or mode != "strategy"):
            raise JobError(422, "live_strategy_requires_live_workspace")
        with closing(self.workbench.open()) as repo:
            candidate = self._read(repo, "p5_candidates", candidate_id)
            feedback = self._feedback_for(repo, candidate)
            observed = {(row["job_id"], row["step"], row["occurrence_id"]) for row in feedback}
            for ref in refs:
                identity = (ref["job_id"], ref["step"], ref["occurrence_id"])
                if dataset == "feedback" and identity not in observed:
                    raise JobError(422, "feedback_case_must_be_linked")
            if len({(r["job_id"], r["step"], r["occurrence_id"]) for r in refs}) != len(refs):
                raise JobError(422, "duplicate_replay_case")
            cases = [self._case(repo, ref) for ref in refs]
            if dataset == "validation" and any(self._overlaps_feedback(
                    case["job"], case["item"]["occurrence_id"], feedback) for case in cases):
                raise JobError(422, "held_out_case_overlaps_feedback")
            memory_id = candidate["spec"]["memory_plan_id"]
            memory_plan = Maintenance._load(repo, memory_id) if memory_id and (
                memory_id != candidate["base"]["spec"]["memory_plan_id"]) else None
            with TemporaryDirectory(prefix="naev-policy-replay-") as directory:
                copy = Path(directory) / "replay.sqlite"
                with closing(sqlite3.connect(copy)) as target:
                    repo.db.backup(target)
                with closing(Jobs(copy)) as scratch:
                    rows = []
                    for case in cases:
                        job, item = case["job"], case["item"]
                        source = self.workbench.occurrence(scratch, job, item)
                        retriever = self.workbench._retriever(scratch, job, source.occurrence_id)
                        locked = scratch.head(source.occurrence_id)[1]
                        if mode == "record":
                            before = self._record_arm(case, candidate["base"]["spec"], retriever, source, locked)
                            after = self._record_arm(case, candidate["spec"], retriever, source, locked)
                        else:
                            if live or case["trace"]["review_tool"]:
                                retriever.game_index(job["snapshots"]["graph"])
                            before = self._strategy_arm(case, candidate["base"]["spec"], scratch, retriever,
                                                        source, locked, live, None)
                            with self._isolated_arm(scratch):
                                after = self._strategy_arm(case, candidate["spec"], scratch, retriever,
                                                           source, locked, live, memory_plan)
                        passes = after["outside_scope"] == 0 and not after.get("error") and not before.get("error")
                        passes &= not after["would_commit"] or not after["hard_problems"] and not locked
                        passes &= set(after["hard_problems"]) <= set(before["hard_problems"])
                        if mode == "strategy" and after["evidence_hit"] is not None:
                            passes &= after["evidence_hit"]
                        if live and case["spec"].get("expected_verdict"):
                            passes &= after["disposition"] == case["spec"]["expected_verdict"]
                        rows.append({**case["spec"], "source_version": source.source_version,
                                     "snapshots": job["snapshots"], "model": job["config"]["model"],
                                     "knowledge_epoch": job.get("knowledge_epoch", 0),
                                     "policy": job.get("policy", {}).get("components"),
                                     "historical_status": case["historical_status"],
                                     "budget": {"historical": job["config"]["max_calls"],
                                                "comparison_max_calls_per_arm": min(job["config"]["max_calls"], 2) if live else 0,
                                                "lookup_bytes": job["lookup_limits"]["max_result_bytes"]},
                                     "before": before, "after": after, "passed": passes})
        scores = {arm: self._metrics([row[arm] for row in rows]) for arm in ("before", "after")}
        recovered = sum(row["before"]["evidence_hit"] is False and row["after"]["evidence_hit"] is True for row in rows)
        gaps = sum(row["before"]["evidence_hit"] is False for row in rows)
        result = {"replay_id": uuid4().hex, "candidate_id": candidate_id, "mode": mode, "dataset": dataset, "live": live,
                  "passed": all(row["passed"] for row in rows), "rows": rows, "metrics": scores,
                  "evidence_gap_resolution_rate": recovered / gaps if gaps else None,
                  "evidence_gaps_resolved": recovered, "isolated": True}
        with closing(self.workbench.open()) as repo, repo.transaction():
            repo.db.execute("INSERT INTO p5_replays VALUES (?,?,?)", (result["replay_id"], candidate_id, encode(result)))
        return result

    @staticmethod
    def _append_release(repo, spec, parent, candidate, reason):
        release = {"release_id": uuid4().hex, "parent": parent, "candidate_id": candidate,
                   "reason": reason, "components": versions(spec), "spec": spec}
        repo.db.execute("INSERT INTO p5_releases(id,data) VALUES (?,?)", (release["release_id"], encode(release)))
        return release

    @staticmethod
    def publication_checks(repo, candidate, current, reports):
        latest = {(report["mode"], report["dataset"], report["live"]): report for report in reports}
        datasets = ("feedback", "validation")
        base, spec = candidate["base"]["spec"], candidate["spec"]
        checks = {
            "policy_base_changed": candidate["base"]["release_id"] == current["release_id"],
            "record_replay_and_held_out_validation_required": all(
                latest.get(("record", dataset, False), {}).get("passed") for dataset in datasets),
        }
        retrieval_changed = any(base[field] != spec[field] for field in ("aliases", "lookup_results", "skill_issues"))
        prompts_changed = base["prompts"] != spec["prompts"]
        memory_changed = base["memory_plan_id"] != spec["memory_plan_id"]
        if retrieval_changed or prompts_changed or memory_changed:
            trials = [latest.get(("strategy", dataset, prompts_changed), {}) for dataset in datasets]
            checks["strategy_replay_and_held_out_validation_required"] = all(t.get("passed") for t in trials)
            if prompts_changed:
                checks["prompt_replay_needs_expected_verdicts"] = all(
                    any(row.get("expected_verdict") for row in trial.get("rows", [])) for trial in trials)
            if retrieval_changed:
                checks["retrieval_change_needs_observed_evidence_improvement"] = any(
                    bool(trial.get("evidence_gaps_resolved")) for trial in trials)
        if memory_changed and spec["memory_plan_id"]:
            plan = Maintenance._load(repo, spec["memory_plan_id"])
            checks["memory_plan_requires_current_p4_approval"] = plan["status"] == "applied" and any(
                row["memory_ref"] == plan["rule"]["memory_ref"] for row in latest_rules(repo))
        return checks

    def decide(self, candidate_id, decision, reason, key):
        with closing(self.workbench.open()) as repo, repo.transaction():
            scope = "p5-candidate:" + candidate_id
            payload = {"decision": decision, "reason": reason}
            repeat = repo.action(scope, key, payload)
            if repeat:
                return repeat
            candidate = self._read(repo, "p5_candidates", candidate_id)
            current = active(repo)
            if candidate.get("decision"):
                raise JobError(409, "candidate_already_decided")
            if decision == "publish":
                reports = [json.loads(row[0]) for row in repo.db.execute(
                    "SELECT data FROM p5_replays WHERE candidate=? ORDER BY rowid", (candidate_id,))]
                for failed, passed in self.publication_checks(repo, candidate, current, reports).items():
                    if not passed:
                        raise JobError(409, failed)
                current = self._append_release(repo, candidate["spec"], current["release_id"], candidate_id, reason)
            result = {"candidate_id": candidate_id, "decision": decision, "reason": reason,
                      "release_id": current["release_id"]}
            candidate["decision"] = result
            repo.db.execute("UPDATE p5_candidates SET data=? WHERE id=?", (encode(candidate), candidate_id))
            repo.save_action(scope, key, payload, result)
        return result

    def rollback(self, release_id, reason, key):
        with closing(self.workbench.open()) as repo, repo.transaction():
            scope = "p5-rollback:" + release_id
            payload = {"rollback": release_id, "reason": reason}
            repeat = repo.action(scope, key, payload)
            if repeat:
                return repeat
            current = active(repo)
            if current["release_id"] != release_id or not current["parent"]:
                raise JobError(409, "can_only_rollback_current_policy_release")
            previous = self._read(repo, "p5_releases", current["parent"]) if current["parent"] != "baseline" else None
            restored = self._append_release(repo, previous["spec"] if previous else DEFAULT,
                                            release_id, None, reason)
            result = {**restored, "restored_release_id": current["parent"]}
            repo.save_action(scope, key, payload, result)
        return result
