"""Local P2 task lifecycle built over the P1 Reviewer and the shared version store."""

from contextlib import closing
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from typing import TypedDict

from localization_agent.memory import MemoryService
from localization_agent.memory.store import VersionConflict, _json
from localization_agent.retrieval.graph import GraphStore
from localization_agent.retrieval.service import GraphConfig
from localization_agent.schemas.graph import GraphNode, GraphRef, GraphSnapshot
from localization_agent.schemas.memory import Evidence, MemoryRef, Occurrence, ReadView, Scope, TranslationCheck, text_hash

from run_p0 import CONFIG, HERE, MANIFEST, NAEV_PROVENANCE, PI, P1_MANIFEST, digest, load
from .evidence import REVIEW_RELATIONS, read_provenance
from .feedback import FeedbackLoop
from .job_context import restore_pack
from .job_intent import INTENT_SCHEMA, fixture_intent, normalize, parse_intent, resolve
from .job_models import JobModels
from .job_store import JobError, Jobs, SCHEMA, encode
from .maintenance import GlossaryRetriever, Maintenance, SCHEMA as MAINTENANCE_SCHEMA, epoch, term_problems
from .models import ModelFailure, Responses
from .po import NativeCatalog, PO_SCHEMA, read_po, render
from .policy import SCHEMA as POLICY_SCHEMA, active, handoff, prompt_overrides
from .projects import Projects, ProjectMemory, SCHEMA as PROJECT_SCHEMA
from .workflow import TranslationAgent, budget_units, hard_problems

WAITING = {"awaiting_input", "awaiting_review", "paused"}
RUNNABLE = {"queued", "running"}
TERMINAL = {"completed", "partial", "failed", "cancelled"}
ITEM_PENDING = {"needs_human", "conflict", "locked"}
ITEM_OK = {"committed", "no_change", "passed", "reviewed"}


class JobState(TypedDict):
    job_id: str


class Workbench:
    """One local Naev workspace, one background worker, durable per-job checkpoints."""

    def __init__(self, data_dir: Path, source_db: Path, *, offline=False, projects_only=False):
        self.data_dir = data_dir
        self.path = data_dir / "workbench.sqlite"
        self.checkpoint_path = data_dir / "checkpoints.sqlite"
        self.source_db, self.offline = source_db.resolve(), offline
        self.projects_only = projects_only
        self.config = load(CONFIG)
        self.prompts = {path.stem: path.read_text(encoding="utf-8") for path in (HERE / "prompts").glob("*.txt")}
        p0, p1 = load(MANIFEST), load(P1_MANIFEST)
        self.snapshot = p1["graph_snapshot"]
        self.source_hash = "project-local" if projects_only else p1["source_db_sha256"]
        self.lookup_limits = p1["review_lookup"]
        # The service starts from the pilot's declared public resources and evidence scope.
        self.cases = {} if projects_only else {case["id"]: case for case in (*p0["cases"], *p1["cases"])}
        all_ids = {source_id for case in self.cases.values() for source_id in case["allowed_evidence_ids"]}
        self.provenance = {} if projects_only else read_provenance(NAEV_PROVENANCE, all_ids)
        self.fixture = {}
        self.maintenance = Maintenance(self)
        self.feedback = FeedbackLoop(self)
        self.projects = Projects(self)
        if offline and not projects_only:
            for manifest, name in ((p0, "offline-fixtures.json"), (p1, "offline-fixtures-p1.json")):
                fixtures = load(HERE / "configs" / name)
                self.fixture.update({case["id"]: fixtures[case["case"]] for case in manifest["cases"]})

    def initialize(self):
        if not self.path.exists():
            staging = self.path.with_suffix(".initializing")
            if self.projects_only:
                with closing(Jobs(staging)) as repo:
                    GraphStore(repo.memory)
            else:
                if not self.source_db.is_file():
                    raise FileNotFoundError(f"Naev snapshot missing at {self.source_db}. "
                                            "Pass --db with the frozen snapshot or use --projects-only.")
                if digest(self.source_db) != self.source_hash:
                    raise ValueError("Naev source graph does not match the frozen snapshot")
                with closing(sqlite3.connect(f"{self.source_db.as_uri()}?mode=ro", uri=True)) as source, \
                        closing(sqlite3.connect(staging)) as copy:
                    source.backup(copy)
            with closing(sqlite3.connect(staging)) as copy:
                copy.executescript(SCHEMA)
                copy.executemany("INSERT INTO p2_workspace VALUES (?,?)",
                                 (("source_hash", self.source_hash), ("mode", "offline" if self.offline else "live")))
                copy.commit()
            staging.replace(self.path)
        with closing(self.open()) as repo:
            repo.db.execute("PRAGMA journal_mode=WAL")
            with repo.db:
                repo.db.execute(PO_SCHEMA)
                repo.db.executescript(MAINTENANCE_SCHEMA)
                repo.db.executescript(POLICY_SCHEMA)
                repo.db.executescript(PROJECT_SCHEMA)
            workspace = dict(repo.db.execute("SELECT key,value FROM p2_workspace"))
            if workspace != {"source_hash": self.source_hash, "mode": "offline" if self.offline else "live"}:
                raise ValueError("Use a workspace with the same frozen source graph and run mode")
            self.catalog = {}
            for item_id, case in self.cases.items():
                occurrence = repo.memory.source("naev", Evidence(item_id, case["version"], ""))
                self.catalog[item_id] = {
                    "occurrence_id": item_id, "label": occurrence.source, "source_version": occurrence.source_version,
                    "file": self.provenance.get(item_id, {}).get("path"),
                    "branch_id": occurrence.metadata.get("branch_id"),
                }
            self.native = NativeCatalog(repo.db, None if self.projects_only else NAEV_PROVENANCE)
            self.resources = {**self.catalog, **self.native.resources}

    def open(self):
        return Jobs(self.path)

    @staticmethod
    def _uploaded(repo, upload_id):
        row = repo.db.execute("SELECT filename,content FROM p3_uploads WHERE id=?", (upload_id,)).fetchone()
        if row is None:
            raise JobError(404, "po_upload_not_found")
        return row

    def import_po(self, filename, content):
        from hashlib import sha256

        if self.projects_only:
            raise JobError(422, "use_project_import_for_project_po")
        po = read_po(content)
        upload_id = sha256(content).hexdigest()
        with closing(self.open()) as repo, repo.transaction():
            repo.db.execute("INSERT OR IGNORE INTO p3_uploads VALUES (?,?,?)", (upload_id, filename, content))
        return {"upload_id": upload_id, "filename": filename, "entries": sum(not e.obsolete for e in po),
                "mapped_occurrences": len(self.native.scope(po))}

    def po_entries(self, upload_id, query, offset, limit):
        with closing(self.open()) as repo:
            row = self._uploaded(repo, upload_id)
        rows, total = self.native.rows(read_po(row["content"]), query, offset, limit)
        return {"upload_id": upload_id, "filename": row["filename"], "entries": rows, "total": total}

    def export_po(self, job_id, choices):
        with closing(self.open()) as repo:
            job = repo.get(job_id)
            upload_id = job["request"].get("upload_id")
            export = next((step for step in job["results"] if step["intent"] == "export"), None)
            if not upload_id or job["status"] not in {"completed", "partial"} or export is None:
                raise JobError(409, "po_export_not_ready")
            raw = self._uploaded(repo, upload_id)["content"]
            eligible, exclusions = [], list(export["exclusions"])
            for record in export["records"]:
                item = record["occurrence_id"]
                if self._eligible(repo, item, record["version"]):
                    eligible.append(record)
                else:
                    exclusions.append({"occurrence_id": item, "reason": "translation_validation_stale"})
        result = render(raw, self.native, eligible, exclusions, choices)
        result.report.update(job_id=job_id, upload_id=upload_id)
        return result

    def create(self, request, key):
        with closing(self.open()) as repo, repo.transaction():
            repeat = repo.action("create", key, request)
            if repeat:
                return repeat["job_id"]
            if self.projects_only and request["selection"].get("project", "naev") == "naev" and "source_text" not in request:
                raise JobError(422, "select_an_imported_project")
            if request.get("upload_id"):
                self._uploaded(repo, request["upload_id"])
            job = self._new_job(repo, request)
            job_id = job["job_id"]
            repo.put(job, event="job")
            repo.save_action("create", key, request, {"job_id": job_id})
        return job_id

    def _new_job(self, repo, request):
        job_id = uuid4().hex
        policy = active(repo)
        project_id = request["selection"].get("project", "naev")
        custom = self.projects.get(repo, project_id) if project_id != "naev" else None
        config = self.config
        roles = self.projects.model_roles(repo, custom) if custom else self.projects.workspace_models(repo)
        if roles:
            config = {**config, "roles": {**config.get("roles", {}), **roles}}
        if request.get("max_calls") is not None:
            config = {**config, "max_calls": request["max_calls"]}
        job = {
            "job_id": job_id, "status": "queued", "reason": None, "cursor": 0, "steps": [], "results": [],
            "request": request, "additions": {}, "intent": None, "missing": {}, "plan": None,
            "versions": {}, "accepted_partial": False, "config": config,
            "prompts": prompt_overrides(self.prompts, policy["spec"]), "policy_prompts_base": self.prompts,
            "policy": policy, "scopes": {}, "lookup_limits": self.lookup_limits,
            "source_kind": "project" if custom else "text" if "source_text" in request else "naev",
            "snapshots": {"source_sha256": self.source_hash, "graph": self.snapshot, "knowledge": f"{job_id}:knowledge"},
        }
        if custom:
            job["project_id"] = project_id
        return job

    def _item_scope(self, source_id):
        case = self.cases.get(source_id, {})
        authorized = case.get("allowed_evidence_ids", [source_id])
        return {"initial": case.get("initial_evidence_ids", authorized), "all": authorized}

    @staticmethod
    def project(job):
        return job.get("project_id") or (f'text:{job["job_id"]}' if job.get("source_kind") == "text" else "naev")

    @staticmethod
    def occurrence(repo, job, item):
        return repo.memory.source(Workbench.project(job), Evidence(item["occurrence_id"], item["source_version"], ""))

    @staticmethod
    def _register_text(repo, job, plan, text_id):
        project = Workbench.project(job)
        source = Occurrence(project, text_id, text_id, 0, job["request"]["source_text"],
                            source_lang=plan["source_lang"], target_lang=plan["target_lang"])
        node = GraphNode(text_id, "occurrence", Scope.for_occurrence(source), {"occurrence_id": text_id},
                         (Evidence(text_id, source.source_version, source.source),), version=1)
        snapshot = GraphSnapshot(f"{text_id}:graph", project, (node.ref,))
        # Stage the immutable single-occurrence graph together with the plan and run.
        repo.db.execute("INSERT INTO sources VALUES (?,?,?,?)", (project, text_id, source.source_version, encode(asdict(source))))
        repo.db.execute("INSERT INTO graph_items VALUES (?,?,?,?,?)", ("node", text_id, node.version, project, encode(asdict(node))))
        repo.db.execute("INSERT INTO graph_snapshots VALUES (?,?)", (snapshot.id, encode(asdict(snapshot))))
        job["snapshots"].update(graph=snapshot.id, source_sha256=text_hash(source.source))

    def resource(self, job, source_id):
        if job.get("source_kind") == "project":
            return job["project_resources"][source_id]
        if job.get("source_kind") == "text":
            return {"source_version": 1, "label": job["request"]["source_text"], "file": None}
        return self.resources[source_id]

    def enqueue_maintenance(self, repo, plan, items):
        job = self._new_job(repo, {"selection": {"project": "naev"}, "intents": ["translate"]})
        ids = [row["occurrence_id"] for row in items]
        job.update(
            maintenance_plan=plan["plan_id"], maintenance_items={row["occurrence_id"]: row for row in items},
            plan={"intents": ["translate"], "targets": ids, "source_lang": "en", "target_lang": "zh", "drafts": {},
                  "requirements": ("Review the existing translation after a glossary change. Preserve correct text. "
                                   "Apply the hard terminology in the context. For dependency-only review, repair "
                                   "only a concrete issue supported by current evidence; otherwise preserve or hand over.")},
            steps=[{"index": 0, "intent": "translate", "depends_on": [], "status": "queued"}],
            versions={row["occurrence_id"]: row["before_version"] for row in items},
            scopes={key: self._item_scope(key) for key in ids},
        )
        self._ensure_run(repo, job)
        repo.put(job, event="job")
        return job["job_id"]

    def list_jobs(self, project, offset, limit, *, kind=None, intent=None, status=None):
        scope = """COALESCE(json_extract(data,'$.project_id'),
                            CASE WHEN json_extract(data,'$.source_kind')='text' THEN 'text' ELSE 'naev' END)"""
        status_groups = {
            "active": {"queued", "running", "awaiting_input", "awaiting_review", "paused"},
            "completed": {"completed", "partial"},
            "failed": {"failed", "cancelled"},
        }
        with closing(self.open()) as repo:
            repo.db.execute("BEGIN")
            where = f"WHERE (? IS NULL OR {scope}=?)"
            rows = repo.db.execute(f"SELECT data FROM p2_jobs {where} ORDER BY rowid DESC", (project, project))
            kind_counts, intent_counts, status_counts, exact_statuses = {}, {}, {"active": 0, "completed": 0, "failed": 0}, {}
            matched = []
            for (data,) in rows:
                job = json.loads(data)
                job_kind = job.get("source_kind", "naev")
                raw_intents = ["build_graph"] if job_kind == "graph_build" else (
                    (job.get("plan") or {}).get("intents") or job["request"].get("intents")
                    or (["translate"] if job_kind == "text" else [])
                )
                job_status = job["status"]
                kind_counts[job_kind] = kind_counts.get(job_kind, 0) + 1
                for item_intent in raw_intents:
                    intent_counts[item_intent] = intent_counts.get(item_intent, 0) + 1
                exact_statuses[job_status] = exact_statuses.get(job_status, 0) + 1
                for group_name, states in status_groups.items():
                    if job_status in states:
                        status_counts[group_name] += 1
                if kind and kind != "all" and job_kind != kind:
                    continue
                if intent and intent != "all" and intent not in raw_intents:
                    continue
                if status and status != "all":
                    allowed = status_groups.get(status, {status})
                    if job_status not in allowed:
                        continue
                matched.append({
                    "job_id": job["job_id"], "status": job_status, "reason": job.get("reason"),
                    "project_id": job.get("project_id"), "source_kind": job_kind,
                    "intents": raw_intents,
                    "title": (job["request"].get("text") or job["request"].get("source_text") or "")[:120],
                    "bulk_id": job.get("bulk_id"),
                })
            return {
                "total": len(matched),
                "jobs": matched[offset:offset + limit],
                "summary": {
                    "kinds": kind_counts, "intents": intent_counts,
                    "statuses": status_counts, "exact_statuses": exact_statuses,
                },
            }

    def version_history(self, job_id, source_id):
        with closing(self.open()) as repo:
            repo.db.execute("BEGIN")
            job = repo.get(job_id)
            if source_id not in (job["plan"] or {}).get("targets", []):
                raise JobError(404, "resource_not_in_job")
            head, locked = repo.head(source_id)
            rows = repo.db.execute("SELECT version,data FROM p2_versions WHERE item=? ORDER BY version DESC",
                                   (source_id,))
            return {"head_version": head, "locked": locked, "versions": [
                {"version": version, "target": record["target"], "source_version": record["source_version"],
                 "rollback_available": version < head and not locked and self._eligible(repo, source_id, version)}
                for version, data in rows for record in (json.loads(data),)]}

    def detail(self, job_id):
        with closing(self.open()) as repo:
            repo.db.execute("BEGIN")
            job = repo.get(job_id)
            items = repo.items(job_id)
            spent = repo.db.execute("SELECT COALESCE(SUM(units),0) FROM p2_calls WHERE job=?", (job_id,)).fetchone()[0]
            translation = next((step["index"] for step in job["steps"] if step["intent"] == "translate"), None)
            ready = ({item["occurrence_id"] for item in items if item["step"] == translation and
                      item["status"] in {"committed", "no_change"}} if translation is not None else job["versions"])
            exportable = sum(self._eligible(repo, item, version)
                             for item, version in job["versions"].items() if item in ready)
            heads = {item: repo.head(item) for item in job["versions"]}
            item_resources = {row["occurrence_id"]: self.resource(job, row["occurrence_id"]) for row in items}
            prerequisite = job.get("prerequisite")
            blocked_by = (prerequisite if prerequisite and repo.get(prerequisite)["status"] not in {"completed", "partial"}
                          else None)
            pause_requested = bool(repo.db.execute("SELECT 1 FROM p6_pause WHERE job=?", (job_id,)).fetchone())
        status = job["status"]
        actions = {
            "awaiting_input": ["continue", "cancel"], "awaiting_review": ["cancel"],
            "queued": ["cancel"], "running": ["cancel"], "failed": ["retry"],
        }.get(status, []).copy()
        if status in RUNNABLE and not pause_requested and not blocked_by:
            actions.append("pause")
        elif status == "paused":
            actions.extend(("resume", "cancel"))
        if status == "awaiting_review":
            current = [item for item in items if item["step"] == job["cursor"]]
            if any(item["status"] in ITEM_OK for item in current):
                actions.append("accept_partial")
            for item in current:
                head = heads[item["occurrence_id"]]
                if item["status"] == "needs_human" and head == (item["expected_version"], False):
                    actions.append("review_item")
                elif item["status"] in {"failed", "locked"} and head == (item["expected_version"], False):
                    actions.append("retry")
                elif item["status"] in ITEM_PENDING and head[0] != item["expected_version"]:
                    actions.append("create_new_job")
        if status in TERMINAL | {"awaiting_review"}:
            if any(locked for _, locked in heads.values()):
                actions.append("unlock")
            if any(not locked for _, locked in heads.values()):
                actions.append("lock")
            if any(version > 1 and not locked for version, locked in heads.values()):
                actions.append("rollback")

        def item_detail(item):
            source_id = item["occurrence_id"]
            version, locked = heads[source_id]
            resource = item_resources[source_id]
            available = []
            active_item = item["step"] == job["cursor"] and (version, locked) == (item["expected_version"], False)
            if active_item and status == "awaiting_review" and item["status"] == "needs_human":
                available.append("review_item")
            if active_item and status in {"awaiting_review", "failed"} and item["status"] in {"failed", "locked"}:
                available.append("retry")
            conflict = item["step"] == job["cursor"] and status == "awaiting_review" and (
                item["status"] == "conflict" or (item["status"] in ITEM_PENDING and version != item["expected_version"]))
            if conflict and job.get("source_kind") != "text":
                available.append("create_new_job")
            if status in TERMINAL | {"awaiting_review"}:
                available.append("unlock" if locked else "lock")
                if version > 1 and not locked:
                    available.append("rollback")
            return {
                **{k: v for k, v in item.items() if k not in {"pack", "read_view", "input_target"}},
                "evidence_url": f'/jobs/{job_id}/items/{item["step"]}/{source_id}/evidence',
                "display_reason": handoff(policy["spec"], item["reason"], source_id)
                if policy and item["status"] == "needs_human" and item.get("reason") else item["reason"],
                "head_version": version, "locked": locked, "version_conflict": conflict, "next_actions": available,
                "file": resource.get("file"), "location": resource.get("location"),
                "metadata": resource.get("metadata", {}),
            }

        artifacts = [{"type": "translation_report", "url": f"/jobs/{job_id}/files/report.json"}]
        export = next((step for step in job["results"] if step["intent"] == "export"), None)
        if export is not None:
            exportable = len(export["records"])
            artifacts.append({"type": "translation_export", "url": f"/jobs/{job_id}/files/export.json"})
            if job["request"].get("upload_id") and status in {"completed", "partial"}:
                artifacts.append({"type": "po_preview", "url": f"/jobs/{job_id}/po/preview"})
        evidence = list(dict.fromkeys(ref for item in items for ref in item.get("evidence_refs", ())))
        policy = job.get("policy")
        return {
            "job_id": job_id, "status": status, "reason": job.get("reason"), "items": [item_detail(item) for item in items],
            "steps": job["steps"], "results": job["results"], "missing": job["missing"],
            "intent": job["intent"], "snapshots": job["snapshots"], "artifacts": artifacts,
            "policy_release_id": policy["release_id"] if policy else "baseline",
            "upload_id": job["request"].get("upload_id"),
            "source_kind": job.get("source_kind", "naev"),
            "project_id": job.get("project_id"),
            "bulk_id": job.get("bulk_id"),
            "clarification_resources": job.get("clarification_resources", []),
            "resolved_targets": job.get("resolved_targets", []),
            "blocked_by": blocked_by,
            "pause_requested": pause_requested,
            "graph_build": ({"batches": len(job["build"]["batches"]),
                             "completed": job["build"]["completed"]} if job.get("source_kind") == "graph_build" else None),
            "source_lang": (job["plan"] or {}).get("source_lang", "en" if self.project(job) == "naev" else None),
            "target_lang": (job["plan"] or {}).get("target_lang", "zh" if self.project(job) == "naev" else None),
            "evidence_refs": evidence, "next_actions": list(dict.fromkeys(actions)),
            "budget": {"limit_calls": job["config"]["max_calls"], "reserved_or_spent": spent},
            "counts": {"items": len(items), "passed": sum(item["status"] in ITEM_OK for item in items),
                       "awaiting_review": sum(item["status"] in ITEM_PENDING for item in items),
                       "exportable": exportable},
        }

    def item_evidence(self, job_id, step, item_id):
        with closing(self.open()) as repo:
            job = repo.get(job_id)
            row = repo.item(job_id, step, item_id)
            lane = f"{step}:{item_id}"
            manifest = row["pack"]["manifest"] if row.get("pack") else None
            retrieval = repo.audit(job_id, "retrieval", item_id, lane=lane)
            actual_sources = []
            for source in (manifest or {}).get("sources", []):
                if job.get("source_kind") == "project":
                    try:
                        location = self.projects.resource(repo, self.project(job), source["occurrence_id"],
                                                          source["source_version"], revision=job.get("project_revision"),
                                                          import_ids=job.get("project_import_ids"))["location"]
                    except JobError:
                        location = None
                else:
                    location = self._native_source_location(source["occurrence_id"])
                actual_sources.append({**source, "location": location})
            return {
                "job_id": job_id, "status": row["status"], "reason": row["reason"], "occurrence_id": item_id,
                "artifacts": [], "next_actions": [], "evidence_refs": row.get("evidence_refs", []),
                "actual_context": manifest, "actual_sources": actual_sources,
                "mandatory_terms": [
                    repo.memory.get(MemoryRef(**candidate["memory_ref"])).content
                    for candidate in (manifest or {}).get("items", [])
                    if candidate["mandatory"] and candidate["category"] == "term" and candidate["memory_ref"]
                ],
                "retrieved": retrieval[-1]["candidate_pool"]["candidates"] if retrieval else [],
                "selection": repo.audit(job_id, "selection", item_id, lane=lane),
                "drafts": repo.audit(job_id, "draft", item_id, lane=lane),
                "hard_checks": repo.audit(job_id, "hard_check", item_id, lane=lane),
                "repairs": repo.audit(job_id, "repair", item_id, lane=lane),
                "reviews": repo.audit(job_id, "review", item_id, lane=lane),
                "source_lookups": repo.audit(job_id, "review_tool", item_id, lane=lane),
            }

    def enrich_resources(self, repo, rows):
        if not rows:
            return [], {"all": 0, "translated": 0, "untranslated": 0, "awaiting_review": 0}
        heads = {row["item"]: (row["version"], bool(row["locked"]))
                 for row in repo.db.execute("SELECT item,version,locked FROM p2_heads")}
        latest_versions = {}
        for item_id, (version, _) in heads.items():
            if version > 0:
                latest_versions[item_id] = repo.version(item_id, version)
        active_items = {}
        latest_any = {}
        for row in repo.db.execute("""
            SELECT j.status AS job_status, j.data AS job_data, t.step, t.item, t.data AS item_data
            FROM p2_items t JOIN p2_jobs j ON j.id=t.job
            ORDER BY t.rowid DESC
        """):
            item_id = row["item"]
            item_data = json.loads(row["item_data"])
            if item_id not in latest_any:
                latest_any[item_id] = (row["job_status"], item_data)
            if row["job_status"] == "awaiting_review" and item_id not in active_items:
                job_data = json.loads(row["job_data"])
                if row["step"] == job_data.get("cursor") and item_data.get("status") in ITEM_PENDING | {"failed"}:
                    active_items[item_id] = item_data

        counts = {"all": len(rows), "translated": 0, "untranslated": 0, "awaiting_review": 0}
        enriched = []
        for row in rows:
            occ_id = row["occurrence_id"]
            src_ver = row.get("source_version", 1)
            head_ver, locked = heads.get(occ_id, (0, False))
            release = latest_versions.get(occ_id)
            active = active_items.get(occ_id)
            if active and active.get("source_version") == src_ver:
                t_status = "awaiting_review"
                target = active.get("target")
                latest_job = active.get("job_id")
                latest_step = active.get("step")
                review_reason = active.get("reason")
            elif release and release.get("source_version") == src_ver and self._eligible(repo, occ_id, head_ver):
                t_status = "translated"
                target = release.get("target")
                last = latest_any.get(occ_id)
                latest_job = last[1].get("job_id") if last else release.get("origin_job")
                latest_step = last[1].get("step") if last else 0
                review_reason = None
            else:
                t_status = "untranslated"
                target = None
                last = latest_any.get(occ_id)
                latest_job = last[1].get("job_id") if last else None
                latest_step = last[1].get("step") if last else None
                review_reason = last[1].get("reason") if last else None
            counts[t_status] += 1
            enriched.append({
                **row,
                "translation_status": t_status,
                "target": target,
                "target_version": head_ver,
                "locked": locked,
                "origin_job_id": release.get("origin_job") if release else None,
                "latest_job_id": latest_job,
                "latest_step": latest_step,
                "review_reason": review_reason,
            })
        return enriched, counts

    def inspect_occurrence(self, project_id, occurrence_id):
        with closing(self.open()) as repo:
            repo.db.execute("BEGIN")
            if project_id == "naev":
                if occurrence_id not in self.resources:
                    raise JobError(404, "resource_not_found")
                raw = self.resources[occurrence_id]
                occ = repo.memory.source("naev", Evidence(occurrence_id, raw.get("source_version", 1), ""))
                resource = {
                    **raw,
                    "label": occ.source,
                    "source_version": occ.source_version,
                    "metadata": occ.metadata,
                    "location": raw.get("location") or self._native_source_location(occurrence_id),
                }
                snapshot_id = self.snapshot
            else:
                project = self.projects.get(repo, project_id)
                all_res = self.projects.resources(repo, project_id)
                if occurrence_id not in all_res:
                    raise JobError(404, "resource_not_found")
                resource = all_res[occurrence_id]
                snapshot_id = project.get("graph_snapshot")

            enriched_list, _ = self.enrich_resources(repo, [resource])
            enriched = enriched_list[0]
            head_ver, locked = repo.head(occurrence_id)
            versions = [
                {"version": ver, "target": rec["target"], "source_version": rec["source_version"],
                 "origin_job": rec.get("origin_job"), "reviewer": rec.get("reviewer")}
                for ver, data in repo.db.execute(
                    "SELECT version, data FROM p2_versions WHERE item=? ORDER BY version DESC", (occurrence_id,))
                for rec in (json.loads(data),)
            ]

            active_item = None
            evidence = None
            target_job_id = enriched.get("latest_job_id")
            target_step = enriched.get("latest_step")
            if target_job_id is None:
                row = repo.db.execute(
                    "SELECT job, step FROM p2_items WHERE item=? ORDER BY rowid DESC LIMIT 1", (occurrence_id,),
                ).fetchone()
                if row:
                    target_job_id, target_step = row["job"], row["step"]

            if target_job_id is not None and target_step is not None:
                try:
                    job_detail = self.detail(target_job_id)
                    matched = next((it for it in job_detail["items"]
                                    if it["occurrence_id"] == occurrence_id and it["step"] == target_step), None)
                    if matched:
                        active_item = {**matched, "job_status": job_detail["status"],
                                       "target_lang": job_detail.get("target_lang")}
                        evidence = self.item_evidence(target_job_id, target_step, occurrence_id)
                except JobError:
                    pass

            graph_nodes, graph_edges = [], []
            if snapshot_id:
                try:
                    store = GraphStore(repo.memory)
                    snap = store.snapshot(snapshot_id)
                    refs_set = set(snap.refs)
                    occ_refs = {ref for ref in snap.refs if ref.kind == "node" and ref.id == occurrence_id}
                    edges_all = [store.get(ref) for ref in snap.refs if ref.kind == "edge"]
                    direct_edges = [
                        edge for edge in edges_all
                        if edge.source in occ_refs or edge.target in occ_refs
                        or any(ev.occurrence_id == occurrence_id for ev in edge.evidence)
                    ]
                    entity_refs = {
                        end for edge in direct_edges for end in (edge.source, edge.target)
                        if end not in occ_refs and end in refs_set
                    }
                    hop_edges = [
                        edge for edge in edges_all
                        if edge.source in entity_refs and edge.target in entity_refs
                    ]
                    selected_edges = []
                    seen_edge = set()
                    for edge in (*direct_edges, *hop_edges):
                        if edge.ref not in seen_edge:
                            seen_edge.add(edge.ref)
                            selected_edges.append(edge)
                    node_refs = list(occ_refs | entity_refs)[:24]
                    for ref in node_refs:
                        obj = store.get(ref)
                        graph_nodes.append({
                            "id": obj.id, "version": obj.version, "kind": obj.kind,
                            "valid": store.valid(ref), "attributes": obj.attributes,
                            "evidence": [asdict(e) for e in obj.evidence],
                        })
                    node_id_set = {n["id"] for n in graph_nodes}
                    for edge in selected_edges[:30]:
                        if edge.source.id in node_id_set and edge.target.id in node_id_set:
                            graph_edges.append({
                                "id": edge.id, "version": edge.version, "relation": edge.relation,
                                "source": edge.source.id, "target": edge.target.id,
                                "valid": store.valid(edge.ref), "attributes": edge.attributes,
                                "evidence": [asdict(e) for e in edge.evidence],
                            })
                except KeyError:
                    pass

            mem_records = (self.projects.memory(repo, project_id) if project_id != "naev"
                           else repo.memory._approved(project="naev"))
            relevant_memory = []
            src_lower = resource["label"].casefold()
            speaker = (resource.get("metadata") or {}).get("speaker_id")
            for rec in mem_records:
                if rec.kind == "term" and rec.content.get("source", "").casefold() in src_lower:
                    relevant_memory.append(asdict(rec))
                elif rec.kind == "style" and speaker and speaker in rec.scope.conditions.get("speaker_id", ()):
                    relevant_memory.append(asdict(rec))
                elif any(ev.occurrence_id == occurrence_id for ev in rec.evidence):
                    relevant_memory.append(asdict(rec))

            project_notes = (self.projects.list_notes(repo, project_id) if project_id != "naev" else [])
            return {
                "project_id": project_id,
                "resource": enriched,
                "translation": {
                    "target": enriched.get("target"),
                    "head_version": head_ver,
                    "locked": locked,
                    "versions": versions,
                },
                "active_item": active_item,
                "evidence": evidence,
                "project_notes": [n for n in project_notes if n.get("enabled", True)],
                "graph": {"snapshot_id": snapshot_id, "nodes": graph_nodes, "edges": graph_edges},
                "memory": relevant_memory[:30],
            }

    def ask_occurrence(self, project_id, occurrence_id, question):
        inspect = self.inspect_occurrence(project_id, occurrence_id)
        resource = inspect["resource"]
        trans = inspect["translation"]
        ev = inspect.get("evidence") or {}
        graph = inspect.get("graph") or {}
        selection = (ev.get("selection") or [None])[-1] or {}
        reviews = ev.get("reviews") or []
        lookups = ev.get("source_lookups") or []
        actual_items = (ev.get("actual_context") or {}).get("items", [])
        retrieved = ev.get("retrieved") or []
        mandatory = ev.get("mandatory_terms") or []
        hard_checks = ev.get("hard_checks") or []
        repairs = ev.get("repairs") or []

        citations = []
        for entry in actual_items[:6]:
            citations.append({"kind": "context", "id": entry["id"], "text": entry["text"][:160]})
        for node in graph.get("nodes", []):
            if node["kind"] == "entity":
                citations.append({"kind": "graph_entity", "id": node["id"],
                                  "text": f'{node["attributes"].get("name")} ({node["attributes"].get("entity_type")})'})
        for lookup in lookups:
            citations.append({"kind": "tool_lookup", "id": lookup["name"],
                              "text": f'{lookup["name"]}({lookup.get("effective_query") or lookup.get("args", {}).get("query", "")}) -> {lookup["result"]["status"]}'})

        if self.offline:
            parts = [f'条目「{resource["label"][:80]}」当前状态：{resource["translation_status"]}（版本 v{trans["head_version"]}）。']
            if trans.get("target"):
                parts.append(f'当前译文：{trans["target"]}')
            else:
                parts.append("该条目尚未产生已提交或待复核译文，你可以在对话框直接输入「翻译这条」触发 Agent 翻译。")
            if selection:
                dec = selection.get("decision") or {}
                if dec.get("rationale"):
                    parts.append(f'上下文选择器理由：{dec["rationale"]}')
                if selection.get("fallback"):
                    parts.append(f'选择器回退标记：{selection["fallback"]}')
            if mandatory:
                parts.append("强制术语：" + "；".join(f'{t["source"]} → {t["target"]}' for t in mandatory))
            if actual_items or retrieved:
                parts.append(f'上下文证据：候选 {len(retrieved)} 条，实际采纳 {len(actual_items)} 条。')
            if graph.get("edges"):
                node_names = {n["id"]: n["attributes"].get("name") or n["id"] for n in graph.get("nodes", [])}
                rels = [f'{node_names.get(e["source"], e["source"])} -[{e["relation"]}]-> {node_names.get(e["target"], e["target"])}'
                        for e in graph["edges"][:5]]
                parts.append("关联图谱关系：" + "；".join(rels))
            if lookups:
                parts.append("Pi 主动查证：" + "；".join(
                    f'{l["name"]} 查询「{l.get("effective_query") or l.get("args", {}).get("query", "")}」→ {l["result"]["status"]}（屏蔽越界 {l.get("excluded_hits", 0)} 条）'
                    for l in lookups))
            if reviews:
                parts.append("Pi 审校结论：" + "；".join(f'{r["verdict"]}（{r["reason"]}）' for r in reviews))
            if repairs:
                parts.append(f'定向修复：共经历 {len(repairs)} 次修复。')
            if inspect.get("project_notes"):
                parts.append("全局背景/System Prompt 记忆：" + "；".join(n["title"] for n in inspect["project_notes"]))
            return {"answer": "\n".join(parts), "citations": citations, "offline": True, "model": "offline_trace"}

        with closing(self.open()) as repo:
            custom = self.projects.get(repo, project_id) if project_id != "naev" else None
            roles = self.projects.model_roles(repo, custom) if custom else self.projects.workspace_models(repo)
        config = {**self.config, "max_calls": 2}
        if roles:
            config["roles"] = {**config.get("roles", {}), **roles}
        role = "intent" if "intent" in config.get("roles", {}) else "reviewer" if "reviewer" in config.get("roles", {}) else "translator"
        trace_payload = {
            "source": resource["label"],
            "metadata": resource.get("metadata"),
            "translation_status": resource["translation_status"],
            "current_target": trans.get("target"),
            "head_version": trans["head_version"],
            "mandatory_terms": mandatory,
            "selection_decision": selection.get("decision"),
            "selection_fallback": selection.get("fallback"),
            "adopted_context": actual_items,
            "unused_candidates_count": max(0, len(retrieved) - len(actual_items)),
            "unused_candidates": [c for c in retrieved if c["id"] not in {a["id"] for a in actual_items}][:8],
            "graph_nodes": [{"id": n["id"], "kind": n["kind"], "name": n["attributes"].get("name"),
                             "type": n["attributes"].get("entity_type"), "valid": n["valid"]}
                            for n in graph.get("nodes", [])],
            "graph_edges": [{"relation": e["relation"], "source": e["source"], "target": e["target"],
                             "valid": e["valid"], "quotes": [x["quote"] for x in e.get("evidence", [])]}
                            for e in graph.get("edges", [])],
            "hard_checks": hard_checks,
            "pi_tool_lookups": lookups,
            "reviews": reviews,
            "repairs": repairs,
            "project_background_and_system_prompts": inspect.get("project_notes", []),
        }
        prompt = (
            "你是 LoRE 本地化工作台的透明溯源助教。请严格基于下面给出的 JSON 证据与执行轨迹，用简洁清晰的简体中文回答用户关于这条翻译的问题。\n"
            "规则：\n"
            "1. 只能陈述 JSON 轨迹中真实存在的事实、记忆条目、图谱关系、选择器理由、工具查证结果与审校理由；\n"
            "2. 如果某个字段为空或尚未翻译/未调用工具，请如实说明，不得编造设定或假装查询了外部资料；\n"
            "3. 回答时尽量点出具体的术语、记忆 ID、图谱关系或 Pi Reviewer 判定依据。\n\n"
            f"用户问题：{question}\n\n"
            f"条目证据与轨迹：\n{json.dumps(trace_payload, ensure_ascii=False)}"
        )
        models = Responses(config, lambda *a, **k: None, PI, offline=False, secret_resolver=self.projects.resolve_secret)
        try:
            answer, _ = models.ask(role, prompt)
        except ModelFailure as error:
            raise JobError(502, error.code) from None
        return {
            "answer": answer.strip(),
            "citations": citations,
            "offline": False,
            "model": config.get("roles", {}).get(role, {}).get("model", config["model"]),
        }

    def _native_source_location(self, source_id):
        return self.native.resources.get(source_id, {}).get("location") or self.provenance.get(source_id)

    def source_context(self, job_id, step, item_id, source_id, version):
        with closing(self.open()) as repo:
            job = repo.get(job_id)
            item = repo.item(job_id, step, item_id)
            manifest = item["pack"]["manifest"] if item.get("pack") else {}
            refs = (item, *manifest.get("sources", ()))
            if not any((ref["occurrence_id"], ref["source_version"]) == (source_id, version) for ref in refs):
                raise JobError(404, "source_not_in_evidence")

            if job.get("source_kind") == "project":
                window = self.projects.source_window(repo, self.project(job), source_id, version,
                                                     revision=job.get("project_revision"),
                                                     import_ids=job.get("project_import_ids"))
            else:
                window = self._native_source_window(repo, job, source_id, version)
            lines = []
            for row in window:
                focus = (row["occurrence_id"], row["source_version"]) == (source_id, version)
                metadata = row.get("metadata", {})
                if (row["occurrence_id"], row["source_version"]) == (item_id, item["source_version"]):
                    metadata = {**metadata, **manifest.get("resolved_metadata", {})}
                lines.append({"occurrence_id": row["occurrence_id"], "source_version": row["source_version"],
                              "location": row["location"], "text": row["label"],
                              "speaker_id": metadata.get("speaker_id"), "focused": focus})
            return {"lines": lines}

    def _native_source_window(self, repo, job, source_id, version):
        location = self._native_source_location(source_id) or {}
        if not location.get("path") or not isinstance(location.get("line"), int):
            raise JobError(404, "source_context_unavailable")
        anchor = {"occurrence_id": source_id, "source_version": version, "location": location}
        neighbors = [row for row in self.native.resources.values() if row["occurrence_id"] != source_id
                     and row["location"].get("path") == location["path"] and isinstance(row["location"].get("line"), int)]
        ordered = sorted([*neighbors, anchor], key=lambda row: (row["location"]["line"], row["occurrence_id"]))
        center = ordered.index(anchor)
        window = []
        for row in ordered[max(0, center - 2):center + 3]:
            source = self.occurrence(repo, job, row)
            window.append({**row, "label": source.source, "metadata": source.metadata})
        return window

    def _trace(self, repo, job_id, item=None, lane=None):
        def record(event_type, **data):
            fields = {"item": item, "lane": lane, **data}
            with repo.db:
                repo.event(job_id, event_type, public=False, **fields)
                if event_type in {"retrieval", "selection", "draft", "review_tool", "review_repack", "hard_check", "repair"}:
                    repo.event(job_id, "progress", stage=event_type, occurrence_id=fields.get("item"),
                               result=fields.get("result", {}).get("status"))
        return record

    def _prepare(self, repo, job):
        request = job["request"]
        parsed = job["intent"]
        if parsed is None and request.get("text") and not request.get("intents"):
            trace = self._trace(repo, job["job_id"], lane="intent")
            base = Responses(job["config"], trace, PI, offline=self.offline, secret_resolver=self.projects.resolve_secret)
            models = JobModels(base, repo, job["job_id"], "intent")
            try:
                receipt, _ = models.pi(job["prompts"]["intent"] + "\nRequest:\n" + request["text"],
                                       mode="intent", schema=INTENT_SCHEMA,
                                       fixture=fixture_intent(request["text"]))
                parsed = parse_intent(receipt["text"])
            except (ModelFailure, ValueError) as error:
                reason = error.code if isinstance(error, ModelFailure) else "intent_structure"
                with repo.transaction():
                    self._update_running(repo, job, "failed", reason)
                return
        text_id = f'text-{job["job_id"]}' if job.get("source_kind") == "text" else None
        with repo.transaction():
            current = repo.get(job["job_id"])
            if current["status"] != "running":
                return
            catalog = self.catalog
            project = None
            if job.get("source_kind") == "project":
                project = self.projects.get(repo, self.project(job))
                if job.get("scheduled_project_revision", project["revision"]) != project["revision"]:
                    self._update_running(repo, job, "failed", "translation_batch_source_revision_changed")
                    return
                if not self.projects.ready(project):
                    self._update_running(repo, job, "failed", "project_graph_not_current")
                    return
                catalog = self.projects.resources(repo, self.project(job))
            elif request.get("upload_id"):
                catalog = self.native.scope(read_po(self._uploaded(repo, request["upload_id"])["content"]))
            plan, missing = resolve(catalog, request, parsed or {}, job["additions"], text_id=text_id, project=project)
            job["intent"] = parsed or {}
            job["missing"] = missing
            if missing:
                candidate_ids = set(plan["targets"])
                candidate_ids.update(choice for finding in missing.values() for choice in finding.get("choices", []))
                job["clarification_resources"] = [row for source_id, row in catalog.items() if source_id in candidate_ids]
                job["resolved_targets"] = plan["targets"]
                self._update_running(repo, job, "awaiting_input", "clarification_required")
                return
            job.pop("clarification_resources", None)
            job.pop("resolved_targets", None)
            job["plan"] = plan
            if text_id:
                self._register_text(repo, job, plan, text_id)
            if project:
                job["project_revision"] = project["revision"]
                job["project_resources"] = {item: catalog[item] for item in plan["targets"]}
                job["project_import_ids"] = [row[0] for row in repo.db.execute(
                    "SELECT import_id FROM p6_files WHERE project=?", (self.project(job),))]
                job["snapshots"].update(graph=project["graph_snapshot"], source_sha256=text_hash(project["graph_snapshot"]))
            self._ensure_run(repo, job)
            job["versions"] = {item: repo.head(item)[0] for item in plan["targets"]}
            if project:
                job["scope_groups"] = {}
                compatibility_groups = {}
                compatible = ("branch_id", "phase_id", "requirements")
                for row in catalog.values():
                    group = tuple(row["metadata"].get(field) for field in compatible)
                    compatibility_groups.setdefault(group, []).append(row["occurrence_id"])
            for source_id in plan["targets"]:
                if project:
                    group = tuple(catalog[source_id]["metadata"].get(field) for field in compatible)
                    group_id = text_hash(encode(group))
                    job["scope_groups"][group_id] = compatibility_groups[group]
                    job["scopes"][source_id] = group_id
                else:
                    job["scopes"][source_id] = self._item_scope(source_id)
            job["steps"] = [
                {"index": i, "intent": intent, "depends_on": [i - 1] if i else [], "status": "queued"}
                for i, intent in enumerate(plan["intents"])
            ]
            repo.put(job, event="planned")

    def _ensure_run(self, repo, job):
        knowledge = job["snapshots"]["knowledge"]
        plan, project = job["plan"], Workbench.project(job)
        records = (self.projects.memory(repo, project) if job.get("source_kind") == "project"
                   else repo.memory._approved(project=project))
        refs = [asdict(record.ref) for record in records]
        repo.db.execute("INSERT INTO snapshots VALUES (?,?,?)", (knowledge, project, _json(refs)))
        repo.db.execute("INSERT INTO runs VALUES (?,?,?,?,?,?)",
                        (job["job_id"], project, None, plan["source_lang"], plan["target_lang"], knowledge))
        job["knowledge_epoch"] = (self.projects.get(repo, project)["knowledge_epoch"] if job.get("source_kind") == "project"
                                  else epoch(repo) if project == "naev" else 0)

    @staticmethod
    def _knowledge_changed(repo, job):
        if job.get("source_kind") == "project":
            project = Projects.get(repo, Workbench.project(job))
            return (job["project_revision"] != project["revision"] or
                    job["knowledge_epoch"] != project["knowledge_epoch"])
        return Workbench.project(job) == "naev" and job.get("knowledge_epoch", 0) != epoch(repo)

    @staticmethod
    def _update_running(repo, job, status, reason=None):
        if repo.get(job["job_id"])["status"] != "running":
            return
        job.update(status=status, reason=reason)
        repo.put(job, event="job")

    def _retriever(self, repo, job, item_id):
        scope = job["scopes"].get(item_id)
        if scope is None:
            raise JobError(404, "resource_not_in_job")
        if isinstance(scope, str):
            authorized = job["scope_groups"][scope]
            scope = {"initial": authorized, "all": authorized}
        memory_ids = frozenset(scope["all"]) | frozenset(job["plan"]["targets"])

        def current_tm(record):
            key = record.evidence[0].occurrence_id
            version, _ = repo.head(key)
            release = repo.version(key, version)
            return bool(release and release["memory_ref"] == asdict(record.ref) and self._eligible(repo, key, version))

        project = job.get("source_kind") == "project"
        if project:
            catalog = self.projects.resources(repo, self.project(job))
            provenance = {key: row["location"] for key, row in catalog.items() if key in memory_ids}
        else:
            provenance = {key: self.native.resources.get(key, {}).get("location") or self.provenance.get(key, {})
                          for key in memory_ids}
        return GlossaryRetriever(
            GraphStore(repo.memory), ProjectMemory(repo.memory, current_tm) if project else MemoryService(repo.memory),
            GraphConfig(**job["config"]["retrieval"]),
            token_counter=budget_units, initial_ids=frozenset(scope["initial"]),
            review_ids=frozenset(scope["all"]), memory_ids=memory_ids, current_tm=current_tm, provenance=provenance,
        )

    def _initialize_items(self, repo, job, step):
        with repo.transaction():
            if repo.get(job["job_id"])["status"] != "running":
                return
            for source_id in job["plan"]["targets"]:
                expected = job["versions"][source_id]
                head, locked = repo.head(source_id)
                status = "queued"
                reason = None
                old = repo.version(source_id, expected)
                input_target = job["plan"]["drafts"].get(source_id) or (old["target"] if old else None)
                if step["intent"] == "translate":
                    if locked:
                        status, reason = "locked", "translation_locked"
                    elif head != expected:
                        status, reason = "conflict", "translation_version_changed"
                elif input_target is None:
                    status, reason = "needs_human", "draft_required_for_review"
                resource = self.resource(job, source_id)
                repo.put_item({
                    "job_id": job["job_id"], "step": step["index"], "occurrence_id": source_id,
                    "source_version": resource["source_version"], "expected_version": expected,
                    "source": resource["label"],
                    "status": status, "reason": reason, "target": input_target, "input_target": input_target,
                })
            step["status"] = "running"
            repo.put(job, event="step")

    def _model_item(self, repo, job, item, intent):
        source_id = item["occurrence_id"]
        with repo.transaction():
            if repo.get(job["job_id"])["status"] != "running":
                return
            if intent == "translate":
                current, locked = repo.head(source_id)
                if locked or current != item["expected_version"] or self._knowledge_changed(repo, job):
                    item.update(status="locked" if locked else "conflict",
                                reason="translation_locked" if locked else "version_or_knowledge_changed")
                    repo.put_item(item)
                    return
            item["status"] = "running"
            if "read_view" not in item:
                item["read_view"] = asdict(repo.memory.view(job["job_id"]))
            repo.put_item(item)
        source = self.occurrence(repo, job, item)
        retriever = self._retriever(repo, job, source_id)
        lane = f'{item["step"]}:{source_id}'
        trace = self._trace(repo, job["job_id"], source_id, lane)
        base = Responses(job["config"], trace, PI, offline=self.offline, secret_resolver=self.projects.resolve_secret)
        models = JobModels(base, repo, job["job_id"], lane)
        fixture = self.fixture.get(source_id) or {
            "selector": '{"selected_ids":[],"rationale":"offline"}', "translator": source.source,
            "reviewer": [{"verdict": "needs_human", "reason": "offline_fixture_unavailable"}],
        }
        # The offline fixture may deliberately introduce a fault. Live jobs use only editor input.
        notes_prompt = self.projects.active_notes_prompt(repo, job.get("project_id"))
        merged_req = "\n\n".join(p for p in (notes_prompt, job["plan"]["requirements"]) if p) or None
        case = {**self.cases.get(source_id, {})} if self.offline else {}
        case.update(case="task", editor_requirement=merged_req or case.get("editor_requirement"))
        maintenance = job.get("maintenance_items", {}).get(source_id)
        if maintenance:
            case = {"case": "maintenance", "editor_requirement": merged_req}
            if self.offline:
                verdict = {"verdict": "needs_human", "reason": "offline_no_deterministic_replacement"}
                if maintenance["proposed"]:
                    verdict = {"verdict": "repair", "reason": "new mandatory term"}
                fixture = {"selector": '{"selected_ids":[],"rationale":"offline maintenance"}',
                           "reviewer": [verdict, {"verdict": "pass", "reason": "deterministic offline fixture"}],
                           "repairer": maintenance["proposed"]}
        policy = job.get("policy", {}).get("spec", {})
        limits = {**job["lookup_limits"], "max_results": min(job["lookup_limits"]["max_results"],
                                                           policy.get("lookup_results", 2))}
        result = TranslationAgent(retriever, models, job["prompts"], trace).run(
            source, job["job_id"], job["snapshots"]["graph"], case, fixture, lookup_limits=limits, policy=policy,
            defer_commit=True, review_target=item["input_target"] if intent == "review" or maintenance else None,
            read_view=ReadView(**item["read_view"]),
        )
        pack = result["pack"]
        target = result["target"]
        item.update(status=result["status"], reason=result["reason"], target=target, pack=pack,
                    draft_hash=text_hash(target) if target else None,
                    evidence_refs=[entry["id"] for entry in pack["manifest"]["items"]] if pack else [])
        try:
            with repo.transaction():
                job = repo.get(job["job_id"])
                if job["status"] != "running":
                    return
                if item["status"] == "passed" and intent == "translate":
                    self._publish(repo, job, item, retriever, source, restore_pack(pack), TranslationCheck(**result["check"]))
                else:
                    if intent == "review":
                        item["findings"] = repo.audit(job["job_id"], "review", source_id, lane=lane)
                    repo.put_item(item)
        except (JobError, VersionConflict, ValueError) as error:
            with repo.transaction():
                if repo.get(job["job_id"])["status"] != "running":
                    return
                item.update(status="locked" if str(error) == "translation_locked" else "conflict", reason=str(error))
                repo.put_item(item)

    @staticmethod
    def _publish(repo, job, item, retriever, source, pack, check, *, approval=None):
        source_id = item["occurrence_id"]
        if Workbench._knowledge_changed(repo, job):
            raise JobError(409, "knowledge_changed_create_new_job")
        head, locked = repo.head(source_id)
        if locked:
            raise JobError(409, "translation_locked")
        if head != item["expected_version"]:
            raise JobError(409, "translation_version_changed")
        if term_problems(repo, source_id, item["target"]):
            raise JobError(409, "target_conflicts_with_active_glossary")
        previous = repo.version(source_id, head)
        if (previous and previous["target"] == item["target"] and previous["source_version"] == source.source_version
                and repo.memory.is_valid(MemoryRef(**previous["memory_ref"]))):
            fresh = retriever.retrieve(source, pack.pool.view, pack.pool.graph_snapshot)
            rebuilt = retriever.build_context(fresh, tuple(candidate.id for candidate in pack.selected))
            if rebuilt.status != "ready" or rebuilt.context_hash != check.context_hash:
                raise JobError(409, "context_changed_create_new_job")
            repo.db.execute("DELETE FROM p4_pending WHERE item=? AND version=?", (source_id, head))
            item.update(status="no_change", version=head, reason=None)
            repo.put_item(item)
            if approval:
                Workbench._wake_after_review(repo, job)
                repo.save_action(*approval, {"status": "no_change", "version": head, "job_id": job["job_id"]})
            return

        # The native MemoryStore commits on this connection. Stage the release pointer,
        # action receipt and item update before the native write so they commit atomically.
        ref = MemoryRef("tm:" + text_hash(_json([job["job_id"], source_id])), 1)
        version = head + 1
        repo.append_version(source_id, version, {
            "origin_job": job["job_id"], "source_version": source.source_version, "target": item["target"],
            "memory_ref": asdict(ref), "reviewer": check.reviewer,
        })
        job["versions"][source_id] = version
        item.update(status="committed", reason=None, version=version, evidence_refs=[c.id for c in pack.selected])
        repo.put_item(item)
        repo.put(job)
        if approval:
            Workbench._wake_after_review(repo, job)
            repo.save_action(*approval, {"status": "committed", "version": version, "job_id": job["job_id"]})
        repo.event(job["job_id"], "approved", occurrence_id=source_id, version=version)
        retriever.commit(source, item["target"], check, pack, expected_version=0)

    def _verified_release(self, repo, source_id, release):
        if not release:
            return False
        saved = repo.memory.get(MemoryRef(**release["memory_ref"]))
        check = saved.content["check"]
        current = repo.db.execute("SELECT MAX(version) FROM sources WHERE project=? AND id=?",
                                  (saved.scope.project_id, source_id)).fetchone()[0]
        return bool(check["passed"] and check["occurrence_id"] == source_id
                    and check["source_version"] == release["source_version"] == current
                    and check["target_hash"] == text_hash(release["target"])
                    and repo.memory.is_valid(saved.ref))

    def _eligible(self, repo, source_id, version):
        release = repo.version(source_id, version)
        if not self._verified_release(repo, source_id, release) or term_problems(repo, source_id, release["target"]):
            return False
        saved = repo.memory.get(MemoryRef(**release["memory_ref"]))
        project = saved.scope.project_id
        if repo.db.execute("SELECT 1 FROM p6_projects WHERE id=?", (project,)).fetchone() and self.projects.term_problems(
                repo, project, release, saved):
            return False
        pending = repo.db.execute("SELECT 1 FROM p4_pending WHERE item=? AND version=? LIMIT 1",
                                  (source_id, version)).fetchone()
        return pending is None

    def _read_step(self, repo, job, step):
        intent = step["intent"]
        records, exclusions = [], []
        closed = {row["occurrence_id"]: row["reason"] for row in repo.items(job["job_id"])
                  if row["status"] == "excluded"}
        for source_id in job["plan"]["targets"]:
            if intent == "export" and source_id in closed:
                exclusions.append({"occurrence_id": source_id, "reason": "closed_by_editor", "detail": closed[source_id]})
                continue
            version = job["versions"][source_id]
            head = repo.version(source_id, version)
            if intent == "propose_update":
                term = job["plan"]["term"]
                label = self.resource(job, source_id)["label"]
                current, locked = repo.head(source_id)
                if normalize(term["source"]) not in normalize(label):
                    exclusions.append({"occurrence_id": source_id, "reason": "term_not_in_source"})
                    continue
                target = head["target"] if head else None
                state = ("version_conflict" if version != current else "locked" if locked
                         else "no_current_translation" if target is None
                         else "already_satisfied" if term["target"] in target else "needs_review")
                previous = term.get("previous_target")
                proposed = (target.replace(previous, term["target"])
                            if state == "needs_review" and previous and previous in target else None)
                records.append({"occurrence_id": source_id, "expected_version": version,
                                "status": state, "diff": {"current_target": target, "proposed_target": proposed,
                                                           "requested_term": term["target"]}})
                continue
            if head is None:
                exclusions.append({"occurrence_id": source_id, "reason": "no_approved_translation"})
                continue
            if intent == "export":
                if not self._eligible(repo, source_id, version):
                    exclusions.append({"occurrence_id": source_id, "reason": "translation_validation_stale"})
                    continue
                review = next((part for part in job["steps"] if part["intent"] == "review" and
                               part["index"] < step["index"]), None)
                if review:
                    row = repo.item(job["job_id"], review["index"], source_id)
                    if row["target"] != head["target"]:
                        exclusions.append({"occurrence_id": source_id, "reason": "reviewed_draft_differs_from_release"})
                        continue
                records.append({"occurrence_id": source_id, "target": head["target"], "version": version})
                continue
            saved = repo.memory.get(MemoryRef(**head["memory_ref"]))
            old = repo.audit(head["origin_job"], "review", source_id)
            check = saved.content["check"]
            if check["reviewer"].startswith("pi:"):
                old = [review for review in old if review.get("call_id") == check["reviewer"].rsplit(":", 1)[-1]]
            else:
                old = [{"verdict": "pass", "reason": "human_approval", "reviewer": check["reviewer"]}]
            records.append({"occurrence_id": source_id, "version": version, "target": head["target"],
                            "actual_context": saved.content["context_manifest"], "reviews": old})
        return {"intent": intent, "records": records, "exclusions": exclusions,
                **({"format": "json", "validation": "approved_translation",
                    "source_lang": job["plan"].get("source_lang", "en"), "target_lang": job["plan"]["target_lang"]}
                   if intent == "export" else {})}

    def advance(self, job_id):
        with closing(self.open()) as repo:
            with repo.transaction():
                job = repo.get(job_id)
                if job["status"] not in RUNNABLE:
                    return
                if repo.db.execute("DELETE FROM p6_pause WHERE job=?", (job_id,)).rowcount:
                    job.update(status="paused", reason="paused_by_editor")
                    repo.put(job, event="job")
                    return
                if job["status"] == "queued":
                    job.update(status="running", reason=None)
                    repo.put(job, event="job")
            if job.get("source_kind") == "graph_build":
                self.projects.advance(repo, job)
                return
            if job["plan"] is None:
                self._prepare(repo, job)
                return
            if job["cursor"] >= len(job["steps"]):
                with repo.transaction():
                    self._update_running(repo, job, "partial" if job["accepted_partial"] else "completed")
                return
            step = job["steps"][job["cursor"]]
            if step["intent"] in {"translate", "review"}:
                if step["status"] == "queued":
                    self._initialize_items(repo, job, step)
                    return
                items = repo.items(job_id, step["index"])
                ready = next((item for item in items if item["status"] in RUNNABLE), None)
                if ready:
                    self._model_item(repo, job, ready, step["intent"])
                    return
                if any(item["status"] in ITEM_PENDING for item in items) or (
                        any(item["status"] == "failed" for item in items) and any(item["status"] in ITEM_OK for item in items)):
                    with repo.transaction():
                        self._update_running(repo, job, "awaiting_review", "items_need_attention")
                    return
                if any(item["status"] == "failed" for item in items):
                    with repo.transaction():
                        self._update_running(repo, job, "failed", "items_failed")
                    return
                result = {"intent": step["intent"], "items": len(items)}
            else:
                result = self._read_step(repo, job, step)
            with repo.transaction():
                if repo.get(job_id)["status"] != "running":
                    return
                step["status"] = "completed"
                job["results"].append(result)
                job["cursor"] += 1
                repo.put(job, event="step")

    def checkpoint_graph(self, saver, stop):
        graph = StateGraph(JobState)

        def route(state):
            if stop.is_set():
                return END
            with closing(self.open()) as repo:
                status = repo.get(state["job_id"])["status"]
            return "work" if status in RUNNABLE else "wait" if status in WAITING else END

        def work(state):
            self.advance(state["job_id"])
            return {}

        def wait(state):
            interrupt({"job_id": state["job_id"], "required": "editor_action"})
            return {}

        graph.add_node("work", work)
        graph.add_node("wait", wait)
        graph.add_edge(START, "work")
        for node in ("work", "wait"):
            graph.add_conditional_edges(node, route, ["work", "wait", END])
        return graph.compile(checkpointer=saver)

    @staticmethod
    def pump(graph, job_id):
        config = {"configurable": {"thread_id": job_id}, "recursion_limit": 1000}
        state = graph.get_state(config)
        if state.interrupts:
            value = Command(resume=True)
        else:
            value = None if state.next else {"job_id": job_id}
        graph.invoke(value, config)

    def serve(self, stop):
        with SqliteSaver.from_conn_string(str(self.checkpoint_path)) as saver:
            graph = self.checkpoint_graph(saver, stop)
            while not stop.is_set():
                with closing(self.open()) as repo:
                    row = repo.db.execute("""
                        SELECT j.id,json_extract(j.data,'$.source_kind') AS kind FROM p2_jobs j
                        LEFT JOIN p2_jobs dependency ON dependency.id=json_extract(j.data,'$.prerequisite')
                        WHERE j.status IN ('queued','running') AND (dependency.id IS NULL OR
                              dependency.status IN ('completed','partial')) ORDER BY j.rowid LIMIT 1
                    """).fetchone()
                if row is None:
                    stop.wait(0.15)
                    continue
                try:
                    if row["kind"] == "graph_build":
                        self.advance(row["id"])
                    else:
                        self.pump(graph, row["id"])
                except Exception as error:
                    with closing(self.open()) as repo, repo.transaction():
                        job = repo.get(row["id"])
                        if job["status"] in RUNNABLE:
                            reason = f"worker_error:{type(error).__name__}"
                            for item in repo.items(job["job_id"], job["cursor"]):
                                if item["status"] == "running":
                                    item.update(status="failed", reason=reason)
                                    repo.put_item(item)
                            job.update(status="failed", reason=reason)
                            repo.put(job, event="job")

    @staticmethod
    def _wake_after_review(repo, job):
        pending = repo.items(job["job_id"], job["cursor"])
        if job["status"] == "awaiting_review" and not any(
                row["status"] in ITEM_PENDING or row["status"] == "failed" for row in pending):
            job.update(status="queued", reason=None)
            repo.put(job, event="job")

    def continue_job(self, job_id, additions, key):
        payload = {"action": "continue", **additions}
        with closing(self.open()) as repo, repo.transaction():
            repeat = repo.action(job_id, key, payload)
            if repeat:
                return repeat
            job = repo.get(job_id)
            if job["status"] != "awaiting_input":
                raise JobError(409, "job_is_not_awaiting_input")
            inputs = {**job["request"], **job["additions"]}
            if not any(value != inputs.get(key) for key, value in additions.items()):
                raise JobError(422, "clarification_requires_new_information")
            job["additions"].update(additions)
            job.update(status="queued", reason=None)
            repo.put(job, event="job")
            receipt = {"job_id": job_id, "status": "queued"}
            repo.save_action(job_id, key, payload, receipt)
            return receipt

    def job_action(self, job_id, action, options, key):
        payload = {"action": action, **options}
        with closing(self.open()) as repo, repo.transaction():
            repeat = repo.action(job_id, key, payload)
            if repeat:
                return repeat
            job = repo.get(job_id)
            if action == "cancel":
                if job["status"] in TERMINAL:
                    raise JobError(409, "job_already_finished")
                job.update(status="cancelled", reason="cancelled_by_editor")
                repo.db.execute("DELETE FROM p6_pause WHERE job=?", (job_id,))
                for row in repo.items(job_id, job["cursor"]):
                    if row["status"] in RUNNABLE:
                        row.update(status="cancelled", reason="cancelled_by_editor")
                        repo.put_item(row)
            elif action == "accept_partial":
                rows = repo.items(job_id, job["cursor"])
                if job["status"] != "awaiting_review" or not any(row["status"] in ITEM_OK for row in rows):
                    raise JobError(409, "no_completed_items_to_accept")
                for row in rows:
                    if row["status"] in ITEM_PENDING or row["status"] == "failed":
                        row.update(status="excluded", reason=row.get("reason") or "closed_by_editor")
                        repo.put_item(row)
                        job["versions"][row["occurrence_id"]] = 0
                job.update(status="queued", accepted_partial=True, reason=None)
            elif action == "retry":
                if job.get("source_kind") == "graph_build":
                    if job["status"] != "failed":
                        raise JobError(409, "job_not_retryable")
                    project = self.projects.get(repo, self.project(job))
                    if project["revision"] != job["build"]["revision"] or project["build_job_id"] != job_id:
                        raise JobError(409, "graph_build_superseded")
                    lane = f'graph:{len(job["build"]["completed"])}'
                    if repo.db.execute("SELECT 1 FROM p2_calls WHERE job=? AND lane=? AND status IN ('started','unknown')",
                                       (job_id, lane)).fetchone() and not options.get("confirm_unknown_call"):
                        raise JobError(409, "unknown_provider_result_requires_confirmation")
                    repo.db.execute("UPDATE p2_calls SET status='superseded' WHERE job=? AND lane=?", (job_id, lane))
                    job.update(status="queued", reason=None)
                else:
                    self._retry(repo, job, options)
            elif action == "pause":
                if job["status"] not in RUNNABLE:
                    raise JobError(409, "job_is_not_running")
                repo.db.execute("INSERT OR IGNORE INTO p6_pause VALUES (?)", (job_id,))
            elif action == "resume":
                if job["status"] != "paused":
                    raise JobError(409, "job_is_not_paused")
                if options.get("requirements"):
                    if job.get("source_kind") == "graph_build":
                        raise JobError(422, "graph_resume_cannot_change_extraction_inputs")
                    if job["plan"]:
                        job["plan"]["requirements"] = options["requirements"]
                    else:
                        job["additions"]["requirements"] = options["requirements"]
                job.update(status="queued", reason=None)
            else:
                raise JobError(400, "unsupported_job_action")
            repo.put(job, event="job")
            receipt = {"job_id": job_id, "status": job["status"], "reason": job["reason"]}
            if action == "pause":
                receipt["pause_requested"] = True
            repo.save_action(job_id, key, payload, receipt)
            return receipt

    @staticmethod
    def _retry(repo, job, options):
        if job["status"] not in {"failed", "awaiting_review"}:
            raise JobError(409, "job_not_retryable")
        requested = options.get("occurrence_id")
        lane = "intent"
        item = None
        if job["plan"] is not None:
            if not requested:
                raise JobError(422, "occurrence_id_required")
            item = repo.item(job["job_id"], job["cursor"], requested)
            if item["status"] not in {"failed", "locked"} or (
                    item["status"] == "locked" and repo.head(requested)[1]):
                raise JobError(409, "item_not_retryable")
            if repo.head(requested)[0] != item["expected_version"]:
                raise JobError(409, "translation_version_changed_create_new_job")
            lane = f'{job["cursor"]}:{requested}'
        if repo.db.execute("SELECT 1 FROM p2_calls WHERE job=? AND lane=? AND status IN ('started','unknown')",
                           (job["job_id"], lane)).fetchone() and not options.get("confirm_unknown_call"):
            raise JobError(409, "unknown_provider_result_requires_confirmation")
        repo.db.execute("""UPDATE p2_calls SET status='superseded' WHERE job=? AND lane=?
                           AND status IN ('failed','unknown','started')""", (job["job_id"], lane))
        if item:
            item.update(status="queued", reason=None)
            repo.put_item(item)
        job.update(status="queued", reason=None)

    def approve(self, job_id, step, source_id, options, key):
        payload = {"action": "approve", "step": step, "occurrence_id": source_id, **options}
        with closing(self.open()) as repo:
            retriever = self._retriever(repo, repo.get(job_id), source_id)
            try:
                with repo.transaction():
                    repeat = repo.action(job_id, key, payload)
                    if repeat:
                        return repeat
                    job = repo.get(job_id)
                    item = repo.item(job_id, step, source_id)
                    if job["status"] != "awaiting_review" or step != job["cursor"] or item["status"] != "needs_human":
                        raise JobError(409, "item_is_not_awaiting_approval")
                    if options["expected_version"] != item["expected_version"]:
                        raise JobError(409, "stale_expected_version")
                    if options.get("expected_draft_hash") != item.get("draft_hash"):
                        raise JobError(409, "draft_changed")
                    if repo.head(source_id) != (item["expected_version"], False):
                        raise JobError(409, "translation_locked_or_version_changed")
                    target = options.get("target") if options.get("target") is not None else item.get("target")
                    if target is None:
                        raise JobError(422, "draft_required")
                    source = self.occurrence(repo, job, item)
                    if item.get("pack"):
                        pack = restore_pack(item["pack"])
                        supplemental = tuple(c for c in pack.selected if REVIEW_RELATIONS.intersection(c.relations))
                        if supplemental:
                            retriever.activate(pack, supplemental)
                    else:
                        pool = retriever.retrieve(source, repo.memory.view(job_id), job["snapshots"]["graph"])
                        pack = retriever.build_context(pool)
                    problems = hard_problems(retriever, source, target, pack)
                    if problems:
                        raise JobError(422, "hard_check_failed: " + ", ".join(problems))
                    before = item.get("target")
                    repo.event(job_id, "human_approval", public=False, item=source_id, lane=f"{step}:{source_id}",
                               before=before, after=target, status=item["status"], reason=item["reason"],
                               editor_reason=options.get("reason"))
                    if options.get("reason"):
                        self.feedback.capture(repo, job, item, before, target, options["reason"])
                    item.update(target=target, draft_hash=text_hash(target), pack=asdict(pack),
                                evidence_refs=[c.id for c in pack.selected])
                    check = TranslationCheck(source_id, source.source_version, text_hash(target),
                                             pack.context_hash, "human:editor", True)
                    if job["steps"][step]["intent"] == "review":
                        item.update(status="reviewed", reason="acknowledged_by_editor")
                        repo.put_item(item)
                        self._wake_after_review(repo, job)
                        receipt = {"status": item["status"], "job_id": job_id, "version": item["expected_version"]}
                        repo.save_action(job_id, key, payload, receipt)
                        return receipt
                    self._publish(repo, job, item, retriever, source, pack, check, approval=(job_id, key, payload))
                    return {"status": item["status"], "job_id": job_id, "version": item["version"]}
            except VersionConflict as error:
                raise JobError(409, "translation_version_changed_create_new_job") from error
            except ValueError as error:
                if isinstance(error, JobError):
                    raise
                raise JobError(409, "context_changed_create_new_job") from error

    def version_action(self, job_id, source_id, action, options, key):
        payload = {"action": action, "occurrence_id": source_id, **options}
        with closing(self.open()) as repo, repo.transaction():
            repeat = repo.action(job_id, key, payload)
            if repeat:
                return repeat
            job = repo.get(job_id)
            if job["plan"] is None or source_id not in job["plan"]["targets"]:
                raise JobError(404, "resource_not_in_job")
            version, locked = repo.head(source_id)
            if options["expected_version"] != version:
                raise JobError(409, "translation_version_changed")
            if action in {"lock", "unlock"}:
                locked = action == "lock"
                repo.set_head(source_id, version, locked)
            elif action == "rollback":
                if locked:
                    raise JobError(409, "translation_locked")
                historical = options.get("to_version")
                if historical is None or historical >= version:
                    raise JobError(422, "historical_version_required")
                old = repo.version(source_id, historical)
                if not self._eligible(repo, source_id, historical):
                    raise JobError(409, "historical_translation_stale_or_missing")
                version += 1
                repo.append_version(source_id, version, {**old, "rolled_back_from": historical})
            else:
                raise JobError(400, "unsupported_version_action")
            receipt = {"job_id": job_id, "status": action, "version": version, "locked": locked}
            repo.event(job_id, "version_action", action=action, occurrence_id=source_id, version=version, locked=locked)
            repo.save_action(job_id, key, payload, receipt)
            return receipt
