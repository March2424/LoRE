"""Versioned resource imports, durable graph builds and project memory."""

from contextlib import closing
from dataclasses import asdict, replace
from hashlib import sha256
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from localization_agent.memory import MemoryService
from localization_agent.models.chat_completions import estimate_tokens
from localization_agent.models.structured import parse_json_reply
from localization_agent.retrieval.builder import AutoGraphBuilder
from localization_agent.retrieval.graph import GraphStore
from localization_agent.schemas.graph import GraphNode, GraphRef, GraphSnapshot
from localization_agent.schemas.memory import Evidence, MemoryRecord, MemoryRef, Occurrence, is_mandatory, scope_match, text_hash

from run_p0 import PI
from .job_models import JobModels
from .job_store import JobError, encode
from .models import ModelFailure, Responses, SELECTION_SCHEMA
from .project_files import parse, po_source_forms, po_writeback_exclusions, project_po, render_project_po, render_table

ROLES = {"extractor", "selector", "translator", "reviewer", "repairer", "intent"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS p6_projects (id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS p6_imports (
    id TEXT PRIMARY KEY, project TEXT NOT NULL, filename TEXT NOT NULL, content BLOB NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS p6_files (
    project TEXT NOT NULL, filename TEXT NOT NULL, import_id TEXT NOT NULL, PRIMARY KEY(project,filename));
CREATE TABLE IF NOT EXISTS p6_resources (
    import_id TEXT NOT NULL, occurrence TEXT NOT NULL, version INTEGER NOT NULL, data TEXT NOT NULL,
    PRIMARY KEY(import_id,occurrence));
CREATE INDEX IF NOT EXISTS p6_resource_history ON p6_resources(occurrence,version);
CREATE TABLE IF NOT EXISTS p6_models (id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS p6_secrets (id TEXT PRIMARY KEY, api_key TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS p6_project_notes (id TEXT PRIMARY KEY, project TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS p6_pause (job TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS p6_extracted (
    project TEXT NOT NULL, occurrence TEXT NOT NULL, version INTEGER NOT NULL,
    PRIMARY KEY(project,occurrence,version));
CREATE TABLE IF NOT EXISTS p6_workspace_models (role TEXT PRIMARY KEY, profile TEXT NOT NULL);
"""


class ProjectMemory(MemoryService):
    def __init__(self, store, current_tm):
        super().__init__(store)
        self.current_tm = current_tm

    def eligible(self, occurrence, view):
        return [record for record in super().eligible(occurrence, view)
                if record.kind != "translation" or self.current_tm(record)]


class ExtractionMemory(MemoryService):
    def __init__(self, store, models, prompt_limit):
        super().__init__(store)
        self.models, self.prompt_limit = models, prompt_limit

    def _call(self, task, template, *, _reuse_logged=False, **values):
        prompt = (self.config.prompt_dir / template).read_text(encoding="utf-8").format(**values)
        tokens = estimate_tokens(prompt)
        if tokens > self.prompt_limit:
            raise ModelFailure(f"graph_input_token_limit:{tokens}")
        fixture = '{"entities":[],"relations":[]}'
        output, call = self.models.ask("extractor", prompt, fixture=fixture)
        return json.dumps(parse_json_reply(output), ensure_ascii=False), call


class Projects:
    def __init__(self, workbench):
        self.workbench = workbench

    @staticmethod
    def get(repo, project):
        row = repo.db.execute("SELECT data FROM p6_projects WHERE id=?", (project,)).fetchone()
        if row is None:
            raise JobError(404, "project_not_found")
        return json.loads(row[0])

    @staticmethod
    def save(repo, project):
        repo.db.execute("INSERT INTO p6_projects VALUES (?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                        (project["project_id"], encode(project)))

    def create(self, spec, key):
        with closing(self.workbench.open()) as repo, repo.transaction():
            scope = "p6-project-create"
            if receipt := repo.action(scope, key, spec):
                return receipt
            project = {"project_id": uuid4().hex, **spec, "revision": 0, "knowledge_epoch": 0,
                       "graph_snapshot": None, "graph_revision": None, "build_job_id": None, "models": {}}
            self.save(repo, project)
            repo.save_action(scope, key, spec, project)
            return project

    @staticmethod
    def profile(repo, profile_id):
        row = repo.db.execute("SELECT data FROM p6_models WHERE id=?", (profile_id,)).fetchone()
        if not row:
            raise JobError(404, "model_profile_not_found")
        return json.loads(row[0])

    @staticmethod
    def _mask_key(secret):
        secret = secret.strip()
        if len(secret) <= 8:
            return "••••" + secret[-2:]
        return f"{secret[:4]}••••{secret[-4:]}"

    @staticmethod
    def _secret_ids(repo):
        return {row[0] for row in repo.db.execute("SELECT id FROM p6_secrets")}

    def resolve_secret(self, profile_id):
        if not profile_id:
            return None
        with closing(self.workbench.open()) as repo:
            row = repo.db.execute("SELECT api_key FROM p6_secrets WHERE id=?", (profile_id,)).fetchone()
            return row[0] if row else None

    @staticmethod
    def _enrich_profile(profile, *, has_secret=False):
        endpoint = urlsplit(profile.get("base_url", ""))
        local = endpoint.hostname in {"localhost", "127.0.0.1", "::1"}
        credential = profile.get("api_key_env")
        mode = profile.get("mode") or ("local" if local and not credential and not has_secret else "api")
        source = "direct" if has_secret else "env" if credential else "local"
        return {
            **profile,
            "mode": mode,
            "api_format": profile.get("api_format") or "chat_completions",
            "thinking": profile.get("thinking") or "disabled",
            "credential_source": source,
            "credential_configured": bool(has_secret or (os.environ.get(credential) if credential else local)),
        }

    def list_models(self):
        with closing(self.workbench.open()) as repo:
            secrets = self._secret_ids(repo)
            return [
                self._enrich_profile(data, has_secret=data["profile_id"] in secrets)
                for row in repo.db.execute("SELECT data FROM p6_models ORDER BY rowid")
                for data in (json.loads(row[0]),)
            ]

    def _prepare_model_payload(self, spec, *, existing=None, has_existing_secret=False):
        endpoint = urlsplit(spec["base_url"])
        local = endpoint.hostname in {"localhost", "127.0.0.1", "::1"}
        if endpoint.username or endpoint.password or endpoint.fragment or endpoint.query or not endpoint.hostname:
            raise JobError(422, "invalid_model_base_url")
        if endpoint.scheme != "https" and not (endpoint.scheme == "http" and local):
            raise JobError(422, "model_requires_https_or_loopback")
        raw_key = (spec.get("api_key") or "").strip() or None
        credential = spec.get("api_key_env")
        if raw_key and not credential:
            credential = None
        effective_secret = bool(raw_key) or (has_existing_secret and not credential)
        mode = spec.get("mode") or ("local" if local and not credential and not effective_secret else "api")
        if mode == "local" and not local:
            raise JobError(422, "model_local_requires_loopback")
        if mode == "api" and not effective_secret and not credential and not local:
            raise JobError(422, "model_api_requires_credential")
        if credential and credential.startswith("WORKBENCH_MODEL_") and credential.endswith("_KEY"):
            configured_url = os.environ.get(credential.removesuffix("_KEY") + "_URL", "")
            if configured_url and configured_url.rstrip("/") != spec["base_url"].rstrip("/"):
                raise JobError(422, "model_credential_endpoint_not_configured")
        safe = {k: v for k, v in spec.items() if k != "api_key"}
        safe["api_key_env"] = credential
        safe["mode"] = mode
        if raw_key:
            safe["api_key_masked"] = self._mask_key(raw_key)
        elif credential or mode == "local":
            safe.pop("api_key_masked", None)
        elif existing and existing.get("api_key_masked"):
            safe["api_key_masked"] = existing["api_key_masked"]
        return safe, raw_key

    def register_model(self, spec, key):
        safe, raw_key = self._prepare_model_payload(spec)
        action_payload = {**safe, **({"key_sha256": sha256(raw_key.encode()).hexdigest()[:12]} if raw_key else {})}
        with closing(self.workbench.open()) as repo, repo.transaction():
            if receipt := repo.action("p6-model-create", key, action_payload):
                secrets = self._secret_ids(repo)
                return self._enrich_profile(receipt, has_secret=receipt["profile_id"] in secrets)
            profile = {**safe, "profile_id": uuid4().hex}
            repo.db.execute("INSERT INTO p6_models VALUES (?,?)", (profile["profile_id"], encode(profile)))
            if raw_key:
                repo.db.execute("INSERT OR REPLACE INTO p6_secrets VALUES (?,?)", (profile["profile_id"], raw_key))
            repo.save_action("p6-model-create", key, action_payload, profile)
            return self._enrich_profile(profile, has_secret=bool(raw_key))

    def update_model(self, profile_id, spec, key):
        with closing(self.workbench.open()) as repo, repo.transaction():
            existing = self.profile(repo, profile_id)
            has_secret = profile_id in self._secret_ids(repo)
            safe, raw_key = self._prepare_model_payload(spec, existing=existing, has_existing_secret=has_secret)
            action_payload = {
                "profile_id": profile_id, **safe,
                **({"key_sha256": sha256(raw_key.encode()).hexdigest()[:12]} if raw_key else {}),
            }
            scope = "p6-model-update:" + profile_id
            if receipt := repo.action(scope, key, action_payload):
                secrets = self._secret_ids(repo)
                return self._enrich_profile(receipt, has_secret=profile_id in secrets)
            profile = {**safe, "profile_id": profile_id}
            repo.db.execute("UPDATE p6_models SET data=? WHERE id=?", (encode(profile), profile_id))
            if raw_key:
                repo.db.execute("INSERT OR REPLACE INTO p6_secrets VALUES (?,?)", (profile_id, raw_key))
                has_secret = True
            elif spec.get("api_key_env") or safe["mode"] == "local":
                repo.db.execute("DELETE FROM p6_secrets WHERE id=?", (profile_id,))
                has_secret = False
            repo.save_action(scope, key, action_payload, profile)
            return self._enrich_profile(profile, has_secret=has_secret)

    def delete_model(self, profile_id):
        with closing(self.workbench.open()) as repo, repo.transaction():
            self.profile(repo, profile_id)
            in_project = any(
                profile_id in json.loads(row[0]).get("models", {}).values()
                for row in repo.db.execute("SELECT data FROM p6_projects")
            )
            in_workspace = repo.db.execute(
                "SELECT 1 FROM p6_workspace_models WHERE profile=?", (profile_id,)
            ).fetchone() is not None
            if in_project or in_workspace:
                raise JobError(409, "model_profile_in_use")
            repo.db.execute("DELETE FROM p6_models WHERE id=?", (profile_id,))
            repo.db.execute("DELETE FROM p6_secrets WHERE id=?", (profile_id,))
            return {"deleted": profile_id}

    def workspace_model_bindings(self, repo):
        return {row["role"]: row["profile"]
                for row in repo.db.execute("SELECT role, profile FROM p6_workspace_models")}

    def workspace_models(self, repo):
        return {role: self.profile(repo, profile) for role, profile in self.workspace_model_bindings(repo).items()}

    def bind_workspace_models(self, bindings, key):
        if set(bindings) - ROLES:
            raise JobError(422, "unknown_model_role")
        with closing(self.workbench.open()) as repo, repo.transaction():
            scope = "p6-workspace-model-roles"
            if result := repo.action(scope, key, bindings):
                return result
            for profile in bindings.values():
                self.profile(repo, profile)
            repo.db.execute("DELETE FROM p6_workspace_models")
            repo.db.executemany("INSERT INTO p6_workspace_models VALUES (?,?)", list(bindings.items()))
            result = {"roles": bindings}
            repo.save_action(scope, key, bindings, result)
            return result

    def model_roles(self, repo, project):
        merged = self.workspace_models(repo)
        merged.update({role: self.profile(repo, profile) for role, profile in project["models"].items()})
        return merged

    def bind_models(self, project_id, bindings, key):
        if set(bindings) - ROLES:
            raise JobError(422, "unknown_model_role")
        with closing(self.workbench.open()) as repo, repo.transaction():
            scope = "p6-model-roles:" + project_id
            if result := repo.action(scope, key, bindings):
                return result
            project = self.get(repo, project_id)
            for profile in bindings.values():
                self.profile(repo, profile)
            project["models"] = bindings
            self.save(repo, project)
            repo.save_action(scope, key, bindings, project)
            return project

    def probe(self, profile_id, role):
        if self.workbench.offline:
            raise JobError(409, "model_probe_requires_live_workspace")
        with closing(self.workbench.open()) as repo:
            profile = self.profile(repo, profile_id)
        config = {**self.workbench.config, "max_calls": 2, "roles": {"probe": profile, "pi_probe": profile}}
        models = Responses(config, lambda *args, **kwargs: None, PI, secret_resolver=self.resolve_secret)
        try:
            if role == "reviewer":
                receipt, _ = models.pi("Call p0_echo, then return its token exactly.", mode="tools",
                                       challenge="workbench-probe", schema=None)
                ok = "workbench-probe" in receipt["text"] and any(
                    event["type"] == "tool_execution_end" and not event["error"] for event in receipt["events"])
            elif role in {"extractor", "selector", "intent"}:
                answer, _ = models.ask("probe", 'Return JSON {"selected_ids":[],"rationale":"ok"}.',
                                       schema=SELECTION_SCHEMA)
                result = parse_json_reply(answer)
                ok = isinstance(result, dict) and result.get("selected_ids") == []
            else:
                answer, _ = models.ask("probe", "Return only WORKBENCH_OK.")
                ok = "WORKBENCH_OK" in answer
        except (ValueError, ModelFailure) as error:
            return {"passed": False, "model": profile["model"], "calls": models.calls,
                    "reason": getattr(error, "code", "invalid_probe_reply")}
        return {"passed": ok, "model": profile["model"], "calls": models.calls,
                "reason": None if ok else "model_capability_not_confirmed"}

    def translation_batches(self, project_id, options, key):
        with closing(self.workbench.open()) as repo, repo.transaction():
            project = self.get(repo, project_id)
            scope = "p6-translation-batches:" + project_id
            if receipt := repo.action(scope, key, options):
                return receipt
            if not self.ready(project):
                raise JobError(409, "project_graph_not_current")
            active = self.resources(repo, project_id, options.get("file"))
            targets = list(dict.fromkeys(options.get("targets") or active))
            if not targets or any(item not in active for item in targets):
                raise JobError(422, "batch_targets_outside_project_scope")
            jobs, previous = [], None
            size = options["batch_size"]
            bulk_id = uuid4().hex
            for offset in range(0, len(targets), size):
                batch = targets[offset:offset + size]
                request = {"selection": {"project": project_id}, "targets": batch, "intents": ["translate", "export"],
                           "requirements": options.get("requirements"), "max_calls": options["max_calls_per_batch"]}
                job = self.workbench._new_job(repo, request)
                job.update(prerequisite=previous, scheduled_project_revision=project["revision"], bulk_id=bulk_id)
                repo.put(job, event="job")
                previous = job["job_id"]
                jobs.append({"job_id": previous, "targets": len(batch)})
            result = {"bulk_id": bulk_id, "project_id": project_id, "rows": len(targets), "jobs": jobs}
            repo.save_action(scope, key, options, result)
            return result

    @staticmethod
    def resources(repo, project, filename=None, *, import_id=None):
        if import_id:
            result = repo.db.execute("SELECT data FROM p6_resources WHERE import_id=? ORDER BY rowid", (import_id,))
        else:
            result = repo.db.execute("""
                SELECT r.data FROM p6_resources r JOIN p6_files f ON f.import_id=r.import_id
                WHERE f.project=? AND (? IS NULL OR f.filename=?) ORDER BY f.filename,r.rowid
                """, (project, filename, filename))
        return {row["occurrence_id"]: row for (data,) in result for row in (json.loads(data),)}

    @staticmethod
    def _resource_row(repo, project, item, version, *, import_ids=None, revision=None):
        imports = encode(import_ids) if import_ids is not None else None
        row = repo.db.execute("""
            SELECT r.data, r.import_id, r.rowid FROM p6_resources r JOIN p6_imports i ON i.id=r.import_id
            WHERE i.project=? AND r.occurrence=? AND r.version=?
              AND (? IS NULL OR r.import_id IN (SELECT value FROM json_each(?)))
              AND (? IS NULL OR json_extract(i.data, '$.project_revision') <= ?)
            ORDER BY r.rowid DESC LIMIT 1
            """, (project, item, version, imports, imports, revision, revision)).fetchone()
        if row is None:
            raise JobError(404, "resource_not_found")
        return row

    def resource(self, repo, project, item, version, *, import_ids=None, revision=None):
        row = self._resource_row(repo, project, item, version, import_ids=import_ids, revision=revision)
        return json.loads(row["data"])

    def source_window(self, repo, project, item, version, *, import_ids=None, revision=None):
        anchor = self._resource_row(repo, project, item, version, import_ids=import_ids, revision=revision)
        position = (anchor["import_id"], anchor["rowid"])
        before = repo.db.execute("""
            SELECT data FROM p6_resources WHERE import_id=? AND rowid<? ORDER BY rowid DESC LIMIT 2
            """, position).fetchall()
        after = repo.db.execute("""
            SELECT data FROM p6_resources WHERE import_id=? AND rowid>? ORDER BY rowid LIMIT 2
            """, position).fetchall()
        return [json.loads(row["data"]) for row in (*reversed(before), anchor, *after)]

    @staticmethod
    def _current_file_reviews(repo, project, filename=None):
        matches = repo.db.execute("""
            SELECT f.filename, r.occurrence, r.version, r.data AS resource, t.job, t.step
            FROM p2_jobs j JOIN p2_items t ON t.job=j.id
            JOIN p6_files f ON f.project=json_extract(j.data, '$.project_id')
            JOIN p6_resources r ON r.import_id=f.import_id AND r.occurrence=t.item
            LEFT JOIN p2_heads h ON h.item=r.occurrence
            WHERE f.project=? AND (? IS NULL OR f.filename=?) AND j.status='awaiting_review'
              AND t.step=json_extract(j.data, '$.cursor') AND json_extract(t.data, '$.status')='needs_human'
              AND r.version=json_extract(t.data, '$.source_version')
              AND COALESCE(h.locked, 0)=0 AND COALESCE(h.version, 0)=json_extract(t.data, '$.expected_version')
              AND (json_extract(j.data, '$.steps[' || t.step || '].intent')='review'
                   OR (json_extract(j.data, '$.project_revision')=? AND json_extract(j.data, '$.knowledge_epoch')=?))
            ORDER BY f.filename, r.rowid, j.rowid DESC
            """, (project["project_id"], filename, filename, project["revision"], project["knowledge_epoch"]))
        # A source entry can be nominated by multiple jobs. Keep the newest actionable task.
        items = {}
        for row in matches:
            items.setdefault(row["occurrence"], row)
        return items.values()

    def file_review(self, project_id, filename, offset, limit, job_id=None):
        with closing(self.workbench.open()) as repo:
            repo.db.execute("BEGIN")
            project = self.get(repo, project_id)
            if not repo.db.execute("SELECT 1 FROM p6_files WHERE project=? AND filename=?", (project_id, filename)).fetchone():
                raise JobError(404, "source_file_not_found")
            entries = [row for row in self._current_file_reviews(repo, project, filename)
                       if job_id is None or row["job"] == job_id]
            items = []
            for entry in entries[offset:offset + limit]:
                resource = json.loads(entry["resource"])
                items.append({"job_id": entry["job"], "step": entry["step"], "occurrence_id": entry["occurrence"],
                              "source_version": entry["version"], "source": resource["label"], "location": resource["location"]})
            return {"project_revision": project["revision"], "total": len(entries), "items": items}

    def file_progress(self, project_id):
        with closing(self.workbench.open()) as repo:
            repo.db.execute("BEGIN")
            project = self.get(repo, project_id)
            pending = {}
            for row in self._current_file_reviews(repo, project):
                pending.setdefault(row["filename"], set()).add(row["occurrence"])
            records = repo.db.execute("""
                SELECT f.filename, i.id, i.data FROM p6_files f JOIN p6_imports i ON i.id=f.import_id
                WHERE f.project=? ORDER BY f.filename
                """, (project_id,)).fetchall()
            files = []
            for record in records:
                filename, import_id = record["filename"], record["id"]
                rows = self.resources(repo, project_id, import_id=import_id)
                targets, _ = self._delivery_targets(repo, import_id, rows.values())
                deliverable = set(targets)
                if json.loads(record["data"])["format"] == "po":
                    deliverable.difference_update(po_writeback_exclusions(po_source_forms(rows.values()), targets, project["target_lang"]))
                review = pending.get(filename, set())
                files.append({"filename": filename, "import_id": import_id, "total": len(rows),
                              "untranslated": len(rows) - len(deliverable | review), "awaiting_review": len(review),
                              "deliverable": len(deliverable)})
            return {"project_revision": project["revision"], "files": files}

    @staticmethod
    def ready(project):
        return bool(project["graph_snapshot"] and project["graph_revision"] == project["revision"])

    def import_file(self, project_id, filename, content, options, key, *, preview=False):
        parsed, skipped, target_column = parse(filename, content, options)
        if not parsed:
            raise JobError(422, "no_source_rows")
        signature = {"filename": filename, "sha256": sha256(content).hexdigest(), "options": options}
        with closing(self.workbench.open()) as repo, repo.transaction():
            project = self.get(repo, project_id)
            if filename.lower().endswith(".po"):
                po = project_po(content)
                language = po.metadata.get("Language", "").replace("_", "-").split("-", 1)[0].lower()
                if language and language != project["target_lang"] and any(
                        entry.msgstr or any(entry.msgstr_plural.values()) for entry in po if not entry.obsolete):
                    raise JobError(422, "existing_po_translations_use_another_target_language")
            scope = "p6-import:" + project_id
            if not preview and (receipt := repo.action(scope, key, signature)):
                return receipt
            previous = repo.db.execute("""
                SELECT i.data FROM p6_files f JOIN p6_imports i ON f.import_id=i.id
                WHERE f.project=? AND f.filename=?
                """, (project_id, filename)).fetchone()
            if not preview and previous and all(json.loads(previous[0]).get(k) == v for k, v in signature.items()):
                result = json.loads(previous[0])
                repo.save_action(scope, key, signature, result)
                return result

            old = self.resources(repo, project_id, filename)
            prepared, changed = [], {}
            counts = dict(added=0, changed=0, unchanged=0, removed=0)
            for row in parsed:
                item = "resource:" + text_hash(encode([project_id, filename, row["key"]]))
                previous_row = old.get(item)
                if previous_row and (previous_row["label"], previous_row["metadata"]) == (row["source"], row["metadata"]):
                    version = previous_row["source_version"]
                    counts["unchanged"] += 1
                else:
                    latest = repo.db.execute("SELECT MAX(version) FROM sources WHERE project=? AND id=?",
                                             (project_id, item)).fetchone()[0]
                    version = (latest or 0) + 1
                    counts["changed" if previous_row else "added"] += 1
                    if latest:
                        changed[item] = version
                prepared.append({"occurrence_id": item, "source_version": version, "label": row["source"],
                                 "branch_id": row["metadata"].get("branch_id"), "file": filename, **row})
            removed = set(old) - {row["occurrence_id"] for row in prepared}
            counts["removed"] = len(removed)
            result = {**signature, "project_id": project_id, "format": Path(filename).suffix.lower()[1:],
                      "rows": len(prepared), "skipped": skipped, "changes": counts, "target_column": target_column}
            if preview:
                return result
            if project["build_job_id"] and repo.get(project["build_job_id"])["status"] in {"queued", "running", "paused"}:
                raise JobError(409, "project_graph_build_active")

            import_id = uuid4().hex
            for number, row in enumerate(prepared):
                item, version = row["occurrence_id"], row["source_version"]
                occurrence = Occurrence(project_id, f"{project_id}:{filename}", item, number, row["label"], version,
                                        row["metadata"], project["source_lang"], project["target_lang"])
                node = GraphNode(item, "occurrence", repo.memory.game_scope(occurrence), {"occurrence_id": item},
                                 (Evidence(item, version, occurrence.source),), version=version)
                repo.db.execute("INSERT OR IGNORE INTO sources VALUES (?,?,?,?)",
                                (project_id, item, version, encode(asdict(occurrence))))
                repo.db.execute("INSERT OR IGNORE INTO graph_items VALUES (?,?,?,?,?)",
                                ("node", item, version, project_id, encode(asdict(node))))
                repo.db.execute("INSERT INTO p6_resources VALUES (?,?,?,?)", (import_id, item, version, encode(row)))
            self.invalidate(repo, project_id, {**changed, **dict.fromkeys(removed)})
            if any(counts[k] for k in ("added", "changed", "removed")):
                project["revision"] += 1
            result.update(import_id=import_id, project_revision=project["revision"])
            repo.db.execute("INSERT INTO p6_imports VALUES (?,?,?,?,?)",
                            (import_id, project_id, filename, content, encode(result)))
            repo.db.execute("""
                INSERT INTO p6_files VALUES (?,?,?) ON CONFLICT(project,filename) DO UPDATE SET import_id=excluded.import_id
                """, (project_id, filename, import_id))
            self.save(repo, project)
            repo.save_action(scope, key, signature, result)
            return result

    @staticmethod
    def invalidate(repo, project, affected):
        if not affected:
            return
        for table, select in (("records", "id,version,payload"), ("graph_items", "kind,id,version,payload")):
            for row in repo.db.execute(f"SELECT {select} FROM {table} WHERE project=?", (project,)).fetchall():
                sources = json.loads(row["payload"])["evidence"]
                if not any(ref["occurrence_id"] in affected for ref in sources):
                    continue
                if table == "records":
                    repo.memory._invalidate(MemoryRef(row["id"], row["version"]), "project_source_changed")
                else:
                    if row["kind"] == "node" and affected.get(row["id"]) == row["version"]:
                        continue
                    repo.db.execute("INSERT OR IGNORE INTO graph_invalidations VALUES (?,?,?,?)",
                                    (row["kind"], row["id"], row["version"], "project_source_changed"))

    def graph_job(self, project_id, options, key):
        with closing(self.workbench.open()) as repo, repo.transaction():
            project = self.get(repo, project_id)
            scope = "p6-graph:" + project_id
            if receipt := repo.action(scope, key, options):
                return receipt
            if project["build_job_id"] and repo.get(project["build_job_id"])["status"] in {"queued", "running", "paused"}:
                raise JobError(409, "project_graph_build_active")
            resources = self.resources(repo, project_id)
            if not resources:
                raise JobError(409, "import_project_sources_first")
            batches, batch, size, context = [], [], 0, None
            # Reserve tokens for discover_graph.txt (~1600), entity catalog (up to 1200), and framing buffer.
            source_budget = min(5000, max(600, options["max_input_tokens"] - 3000))
            coverage = set() if options["rebuild"] else {tuple(row) for row in repo.db.execute(
                "SELECT occurrence,version FROM p6_extracted WHERE project=?", (project_id,))}
            pending = [row for row in resources.values() if (row["occurrence_id"], row["source_version"]) not in coverage]
            if options.get("max_items"):
                pending = pending[:options["max_items"]]
            for row in pending:
                units = estimate_tokens(row["label"] + row["metadata"].get("native_context", "")) + 48
                grouping = tuple(row["metadata"].get(field) for field in ("branch_id", "phase_id", "requirements"))
                if batch and (len(batch) >= options["batch_size"] or size + units > source_budget or grouping != context):
                    batches.append(batch)
                    batch, size = [], 0
                batch.append(["node", row["occurrence_id"], row["source_version"]])
                size, context = size + units, grouping
            if batch:
                batches.append(batch)
            if not options["extract"]:
                batches = []
            request = {"selection": {"project": project_id}, "intents": []}
            job = self.workbench._new_job(repo, request)
            job.update(source_kind="graph_build", project_id=project_id,
                       build={**options, "revision": project["revision"], "batches": batches, "completed": []})
            settings = job["config"]
            job["config"] = {**settings, "max_calls": options["max_calls"],
                             "max_output_tokens": {**settings["max_output_tokens"], "extractor": options["max_output_tokens"]}}
            project["build_job_id"] = job["job_id"]
            self.save(repo, project)
            repo.put(job, event="job")
            result = {"job_id": job["job_id"], "status": "queued", "project_id": project_id,
                      "rows": len(pending), "total_rows": len(resources), "batches": len(batches), "max_calls": options["max_calls"]}
            repo.save_action(scope, key, options, result)
            return result

    def _publish_snapshot(self, repo, graph, project_id, job, build, *, final=False):
        project = self.get(repo, project_id)
        active = self.resources(repo, project_id)
        refs = []
        for row in repo.db.execute("""
            SELECT kind,id,MAX(version) AS version FROM graph_items WHERE project=? GROUP BY kind,id ORDER BY kind,id
            """, (project_id,)):
            ref = GraphRef(row["kind"], row["id"], row["version"])
            obj = graph.get(ref)
            if all(e.occurrence_id in active for e in obj.evidence) and graph.valid(ref):
                refs.append(ref)
        nodes = {ref for ref in refs if ref.kind == "node"}
        refs = tuple(ref for ref in refs if ref.kind == "node" or
                     (graph.get(ref).source in nodes and graph.get(ref).target in nodes))
        snap_id = job["job_id"] + (":graph" if final else f":graph:{len(build['completed'])}")
        snapshot = GraphSnapshot(snap_id, project_id, refs)
        repo.db.execute("INSERT OR REPLACE INTO graph_snapshots VALUES (?,?)", (snapshot.id, encode(asdict(snapshot))))
        project.update(graph_snapshot=snapshot.id, graph_revision=build["revision"])
        self.save(repo, project)
        summary = {"intent": "build_graph", "graph_snapshot": snapshot.id,
                   "nodes": len(nodes), "edges": len(refs) - len(nodes),
                   "completed_batches": len(build["completed"]), "total_batches": len(build["batches"])}
        job["results"] = [item for item in job.get("results", []) if item.get("intent") != "build_graph"] + [summary]

    def advance(self, repo, job):
        build, project_id = job["build"], job["project_id"]
        graph = GraphStore(repo.memory)
        index = len(build["completed"])
        if build["extract"] and index < len(build["batches"]):
            if index >= build["max_calls"]:
                with repo.transaction():
                    current = repo.get(job["job_id"])
                    if current["status"] != "running":
                        return
                    self._publish_snapshot(repo, graph, project_id, job, build, final=True)
                    status = "partial" if index > 0 else "failed"
                    reason = "graph_partial_budget_reached" if index > 0 else "call_budget_exhausted"
                    self.workbench._update_running(repo, job, status, reason)
                return
            trace = self.workbench._trace(repo, job["job_id"], lane=f"graph:{index}")
            models = JobModels(Responses(job["config"], trace, PI, offline=self.workbench.offline,
                                         secret_resolver=self.resolve_secret),
                               repo, job["job_id"], f"graph:{index}")
            memory = ExtractionMemory(repo.memory, models, build["max_input_tokens"])
            try:
                with graph.atomic():
                    result = AutoGraphBuilder(graph, memory, keep_valid_items=True).build(
                        tuple(GraphRef(*ref) for ref in build["batches"][index]))
                    if repo.get(job["job_id"])["status"] != "running":
                        raise JobError(409, "graph_build_interrupted")
                    build["completed"].append({"entities": len(result.entities), "relations": len(result.edges),
                                               "model_call": result.extraction_call, "rejected": list(result.rejected)})
                    repo.db.executemany("INSERT OR IGNORE INTO p6_extracted VALUES (?,?,?)",
                                        ((project_id, ref[1], ref[2]) for ref in build["batches"][index]))
                    self._publish_snapshot(repo, graph, project_id, job, build, final=False)
                    repo.put(job, event="graph_batch")
            except (ValueError, ModelFailure) as error:
                code = getattr(error, "code", str(error))
                with repo.transaction():
                    if build["completed"]:
                        self._publish_snapshot(repo, graph, project_id, job, build, final=True)
                    if code == "call_budget_exhausted" and build["completed"]:
                        self.workbench._update_running(repo, job, "partial", "graph_partial_budget_reached")
                    else:
                        self.workbench._update_running(repo, job, "failed", code)
            return

        with repo.transaction():
            current = repo.get(job["job_id"])
            if current["status"] != "running":
                return
            self._publish_snapshot(repo, graph, project_id, job, build, final=True)
            job.update(status="completed", reason=None)
            repo.put(job, event="job")

    def graph_view(self, project_id, snapshot_id, query, node_id, branch_id, offset, limit):
        with closing(self.workbench.open()) as repo:
            project = self.get(repo, project_id)
            snapshot_id = snapshot_id or project["graph_snapshot"]
            if not snapshot_id:
                raise JobError(404, "project_graph_not_built")
            graph = GraphStore(repo.memory)
            try:
                snapshot = graph.snapshot(snapshot_id)
            except KeyError:
                raise JobError(404, "graph_snapshot_not_found") from None
            if snapshot.project_id != project_id:
                raise JobError(404, "graph_snapshot_not_found")
            objects = {ref: graph.get(ref) for ref in snapshot.refs}
            location_cache = {}

            def row_for(evidence):
                key = (evidence.occurrence_id, evidence.source_version)
                if key not in location_cache:
                    location_cache[key] = self.resource(repo, project_id, *key)
                return location_cache[key]

            if branch_id is not None:
                objects = {ref: obj for ref, obj in objects.items()
                           if all(row_for(evidence)["branch_id"] == branch_id for evidence in obj.evidence)}
            nodes = {ref for ref in objects if ref.kind == "node"}
            if node_id or query:
                nodes = {ref for ref in nodes if (ref.id == node_id if node_id else
                         query.casefold() in str(objects[ref].attributes.get("name") or
                                                 objects[ref].evidence[0].quote).casefold())}
            ordered = sorted(nodes, key=lambda ref: ref.id)
            anchors = set(ordered[offset:offset + limit])
            edges = [ref for ref, edge in objects.items() if ref.kind == "edge"
                     and edge.source in objects and edge.target in objects
                     and (edge.source in anchors or edge.target in anchors)][:limit * 3]
            nearby = {end for ref in edges for end in (objects[ref].source, objects[ref].target)}

            def item(ref):
                obj = objects[ref]
                return {**asdict(obj), "valid": graph.valid(ref),
                        "evidence": [{**asdict(e), "location": row_for(e)["location"]}
                                     for e in obj.evidence]}
            return {"project_id": project_id, "graph_snapshot": snapshot.id, "total": len(ordered),
                    "nodes": [item(ref) for ref in sorted(anchors | nearby, key=lambda ref: ref.id)],
                    "edges": [item(ref) for ref in edges]}

    def memory(self, repo, project_id):
        resources = self.resources(repo, project_id)
        approved = list(repo.memory._approved(project=project_id))
        for item in resources:
            version, _ = repo.head(item)
            if self.workbench._eligible(repo, item, version):
                approved.append(repo.memory.get(MemoryRef(**repo.version(item, version)["memory_ref"])))
        return approved

    def stage_memory(self, project_id, spec, key):
        with closing(self.workbench.open()) as repo, repo.transaction():
            self.get(repo, project_id)
            scope_key = "p6-memory-draft:" + project_id
            if receipt := repo.action(scope_key, key, spec):
                return receipt
            source_id = spec["occurrence_id"]
            row = self.resources(repo, project_id).get(source_id)
            if not row or row["source_version"] != spec["source_version"]:
                raise JobError(409, "memory_requires_current_source")
            evidence = Evidence(source_id, spec["source_version"], spec["quote"])
            source = repo.memory.source(project_id, evidence)
            if evidence.quote not in source.source:
                raise JobError(422, "memory_quote_not_in_source")
            scope = repo.memory.game_scope(source)
            if spec["kind"] == "term":
                if not spec.get("source") or not spec.get("target") or spec["source"] not in evidence.quote:
                    raise JobError(422, "memory_term_requires_source_quote_and_target")
                content = {"source": spec["source"], "target": spec["target"], "mandatory": spec["mandatory"]}
            else:
                speaker = source.metadata.get("speaker_id")
                if not speaker or not spec.get("note"):
                    raise JobError(422, "memory_note_requires_known_speaker")
                scope = replace(scope, conditions={**scope.conditions, "speaker_id": (speaker,)})
                content = {"speaker_id": speaker, "note": spec["note"]}
            record = MemoryRecord("project-memory:" + uuid4().hex, 0, spec["kind"], scope, content, (evidence,))
            result = asdict(repo.memory._insert(record, 0))
            repo.save_action(scope_key, key, spec, result)
            return result

    def decide_memory(self, project_id, ref, action, reason, key):
        payload = {"action": action, "reason": reason}
        with closing(self.workbench.open()) as repo, repo.transaction():
            scope_key = f"p6-memory:{project_id}:{ref.record_id}:{ref.version}"
            if receipt := repo.action(scope_key, key, payload):
                return receipt
            project = self.get(repo, project_id)
            try:
                record = repo.memory.get(ref)
            except KeyError:
                raise JobError(404, "memory_not_found") from None
            if record.scope.project_id != project_id or record.kind not in {"term", "style"}:
                raise JobError(404, "memory_not_found")
            latest = repo.memory.latest_version(record.record_id)
            if latest != ref.version or not repo.memory.is_valid(ref):
                raise JobError(409, "memory_version_changed")
            if action == "approve" and record.status == "draft":
                saved = repo.memory._insert(replace(record, status="approved", reviewer="human:editor"), latest)
                result = asdict(saved)
            elif action == "retire" and record.status == "approved":
                repo.memory._invalidate(ref, reason)
                result = {"ref": asdict(ref), "status": "retired"}
            else:
                raise JobError(409, "memory_action_does_not_match_status")
            project["knowledge_epoch"] += 1
            self.save(repo, project)
            repo.save_action(scope_key, key, payload, result)
            return result

    @staticmethod
    def list_notes(repo, project_id):
        return [json.loads(row[0]) for row in repo.db.execute(
            "SELECT data FROM p6_project_notes WHERE project=? ORDER BY rowid", (project_id,))]

    @classmethod
    def active_notes_prompt(cls, repo, project_id):
        if not project_id or project_id == "naev":
            return None
        labels = {"background": "游戏背景", "system_prompt": "System Prompt", "style_guide": "文风规范"}
        lines = [f"[{labels.get(n.get('category'), '设定')}] {n['title']}: {n['content']}"
                 for n in cls.list_notes(repo, project_id) if n.get("enabled", True)]
        return "\n".join(lines) if lines else None

    def create_note(self, project_id, spec, key):
        with closing(self.workbench.open()) as repo, repo.transaction():
            self.get(repo, project_id)
            scope_key = "p6-note-create:" + project_id
            if receipt := repo.action(scope_key, key, spec):
                return receipt
            note = {"note_id": uuid4().hex, "project_id": project_id, **spec}
            repo.db.execute("INSERT INTO p6_project_notes VALUES (?,?,?)",
                            (note["note_id"], project_id, encode(note)))
            repo.save_action(scope_key, key, spec, note)
            return note

    def update_note(self, project_id, note_id, spec, key):
        with closing(self.workbench.open()) as repo, repo.transaction():
            self.get(repo, project_id)
            row = repo.db.execute("SELECT data FROM p6_project_notes WHERE id=? AND project=?",
                                  (note_id, project_id)).fetchone()
            if not row:
                raise JobError(404, "memory_not_found")
            scope_key = f"p6-note-update:{project_id}:{note_id}"
            if receipt := repo.action(scope_key, key, spec):
                return receipt
            note = {**json.loads(row[0]), **spec, "note_id": note_id, "project_id": project_id}
            repo.db.execute("UPDATE p6_project_notes SET data=? WHERE id=? AND project=?",
                            (encode(note), note_id, project_id))
            repo.save_action(scope_key, key, spec, note)
            return note

    def delete_note(self, project_id, note_id):
        with closing(self.workbench.open()) as repo, repo.transaction():
            self.get(repo, project_id)
            deleted = repo.db.execute("DELETE FROM p6_project_notes WHERE id=? AND project=?",
                                      (note_id, project_id)).rowcount
            if not deleted:
                raise JobError(404, "memory_not_found")
            return {"deleted": note_id}

    @staticmethod
    def term_problems(repo, project, release, record):
        source = repo.memory.source(project, record.evidence[0])
        effective = replace(source, metadata={**source.metadata, **record.content.get("metadata", {})})
        return [term.content["source"] for term in repo.memory._approved(project=project)
                if term.kind == "term" and is_mandatory(term) and scope_match(term.scope, effective) == "match"
                and term.content["source"] in source.source and term.content["target"] not in release["target"]]

    def _delivery_targets(self, repo, import_id, rows):
        heads = {row["item"]: (row["version"], json.loads(row["data"])) for row in repo.db.execute("""
            SELECT h.item, h.version, v.data FROM p6_resources r JOIN p2_heads h ON h.item=r.occurrence
            JOIN p2_versions v ON v.item=h.item AND v.version=h.version WHERE r.import_id=?
            """, (import_id,))}
        targets, skipped = {}, {}
        for row in rows:
            item = row["occurrence_id"]
            current, release = heads.get(item, (0, None))
            if not release:
                skipped[item] = "no_approved_translation"
            elif release["source_version"] != row["source_version"]:
                skipped[item] = "export_source_version_mismatch"
            elif not self.workbench._eligible(repo, item, current):
                skipped[item] = "translation_validation_stale"
            else:
                targets[item] = release["target"]
        return targets, skipped

    def export(self, project_id, import_id):
        with closing(self.workbench.open()) as repo:
            repo.db.execute("BEGIN")
            self.get(repo, project_id)
            record = repo.db.execute("SELECT * FROM p6_imports WHERE project=? AND id=?", (project_id, import_id)).fetchone()
            if not record:
                raise JobError(404, "import_not_found")
            rows = list(self.resources(repo, project_id, import_id=import_id).values())
            targets, skipped = self._delivery_targets(repo, import_id, rows)
            info = json.loads(record["data"])
            validation = None
            if info["format"] == "po":
                project = self.get(repo, project_id)
                payload, plural_skipped, validation = render_project_po(record["content"], rows, targets, project["target_lang"])
                skipped.update(plural_skipped)
            else:
                payload = render_table(record["content"], record["filename"], rows, targets, info["target_column"])
            details = [{"occurrence_id": item, "source": row["label"][:180], "location": row["location"],
                        "reason": skipped[item]} for row in rows for item in (row["occurrence_id"],) if item in skipped]
            return record["filename"], payload, {"translated": len(rows) - len(skipped), "excluded": list(skipped),
                                                 "exclusion_details": details, "validation": validation}
