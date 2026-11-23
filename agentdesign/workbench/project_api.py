"""HTTP contracts for imported projects, graph builds, model profiles and delivery."""

import asyncio
from contextlib import closing
import json
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote
from zipfile import BadZipFile, ZipFile
from io import BytesIO

from fastapi import APIRouter, Header, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, ValidationError
from localization_agent.schemas.memory import MemoryRef

from .job_store import JobError
from .project_files import META

Key = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=120)]
OptionalKey = Annotated[str | None, Header(alias="Idempotency-Key", min_length=1, max_length=120)]
Language = Literal["en", "zh"]
Role = Literal["extractor", "selector", "translator", "reviewer", "repairer", "intent"]
MAX_UPLOAD_BYTES = 16 * 1024 * 1024


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ProjectInput(Input):
    name: Annotated[str, Field(min_length=1, max_length=120)]
    source_lang: Language
    target_lang: Language


class ImportOptions(Input):
    id_column: str = "id"
    source_column: str = "source_text"
    sheet: str | None = None
    target_column: str | None = None
    columns: dict[str, str] = Field(default_factory=dict, max_length=len(META))


class GraphBuild(Input):
    extract: bool = True
    rebuild: bool = False
    max_items: int | None = Field(default=None, ge=1, le=50000)
    batch_size: int = Field(default=24, ge=1, le=64)
    max_calls: int = Field(default=64, ge=1, le=5000)
    max_input_tokens: int = Field(default=8000, ge=2000, le=32768)
    max_output_tokens: int = Field(default=12288, ge=512, le=16384)


class Profile(Input):
    name: Annotated[str, Field(min_length=1, max_length=120)]
    model: Annotated[str, Field(min_length=1, max_length=200)]
    base_url: HttpUrl
    api_key: Annotated[str, Field(min_length=1, max_length=2000)] | None = None
    api_key_env: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{1,80}$")] | None = None
    timeout_seconds: int = Field(default=75, ge=2, le=360)
    mode: Literal["api", "local"] | None = None
    api_format: Literal["chat_completions", "responses"] = "chat_completions"
    thinking: Literal["auto", "disabled"] = "disabled"


class Bindings(Input):
    roles: dict[Role, str]


class ModelProbe(Input):
    role: Role


class TranslationBatches(Input):
    file: str | None = None
    targets: list[str] | None = Field(default=None, min_length=1, max_length=100_000)
    batch_size: int = Field(default=12, ge=1, le=64)
    max_calls_per_batch: int = Field(default=72, ge=1, le=1000)
    requirements: Annotated[str, Field(min_length=1, max_length=5000)] | None = None


MemoryText = Annotated[str, Field(min_length=1, max_length=2000)]


class MemoryDraft(Input):
    kind: Literal["term", "style"]
    occurrence_id: str
    source_version: int = Field(ge=1)
    quote: MemoryText
    source: MemoryText | None = None
    target: MemoryText | None = None
    note: MemoryText | None = None
    mandatory: bool = False


class MemoryDecision(Input):
    reason: MemoryText


class ProjectNoteInput(Input):
    category: Literal["background", "system_prompt", "style_guide"] = "background"
    title: Annotated[str, Field(min_length=1, max_length=120)]
    content: Annotated[str, Field(min_length=1, max_length=8000)]
    enabled: bool = True


def validate_workbook_zip(content):
    try:
        with ZipFile(BytesIO(content)) as book:
            if sum(entry.file_size for entry in book.infolist()) > 64 * 1024 * 1024:
                raise JobError(413, "uncompressed_workbook_too_large")
    except BadZipFile:
        raise JobError(422, "invalid_xlsx_archive") from None


def router(service):
    api = APIRouter()

    @api.post("/projects", status_code=201)
    def create_project(body: ProjectInput, key: Key):
        if body.source_lang == body.target_lang:
            raise JobError(422, "project_languages_must_differ")
        return service().projects.create(body.model_dump(), key)

    @api.get("/projects")
    def list_projects():
        with closing(service().open()) as repo:
            return {"projects": [json.loads(row[0]) for row in repo.db.execute("SELECT data FROM p6_projects ORDER BY rowid")]}

    @api.get("/projects/{project_id}")
    def project_detail(project_id: str):
        projects = service().projects
        with closing(service().open()) as repo:
            project = projects.get(repo, project_id)
            resources = projects.resources(repo, project_id)
            extracted = {tuple(row) for row in repo.db.execute(
                "SELECT occurrence,version FROM p6_extracted WHERE project=?", (project_id,))}
            unextracted = sum(1 for row in resources.values()
                              if (row["occurrence_id"], row["source_version"]) not in extracted)
            return {**project, "graph_ready": projects.ready(project),
                    "resources": len(resources),
                    "unextracted": unextracted,
                    "approved_memory": len(projects.memory(repo, project_id))}

    @api.post("/projects/{project_id}/imports", status_code=201)
    async def import_sources(project_id: str, request: Request, filename: Annotated[str, Query(min_length=1, max_length=255)],
                             options: str = "{}", preview: bool = False, key: OptionalKey = None):
        filename = Path(filename).name
        suffix = Path(filename).suffix.lower()
        if suffix not in {".csv", ".xlsx", ".po"}:
            raise JobError(422, "supported_import_formats_csv_xlsx_po")
        if not preview and not key:
            raise JobError(400, "idempotency_key_required")
        try:
            spec = ImportOptions.model_validate_json(options).model_dump(exclude_none=True)
        except ValidationError:
            raise JobError(422, "invalid_import_options") from None
        if set(spec["columns"]) - META:
            raise JobError(422, "unknown_source_metadata_mapping")
        content = bytearray()
        async for chunk in request.stream():
            if len(content) + len(chunk) > MAX_UPLOAD_BYTES:
                raise JobError(413, "project_import_too_large")
            content.extend(chunk)
        raw = bytes(content)
        if suffix == ".xlsx":
            await asyncio.to_thread(validate_workbook_zip, raw)
        return await asyncio.to_thread(service().projects.import_file, project_id, filename, raw, spec, key, preview=preview)

    @api.get("/projects/{project_id}/resources")
    def project_resources(project_id: str, file: str | None = None, query: str = "",
                          status: Literal["all", "translated", "untranslated", "awaiting_review"] = "all",
                          offset: Annotated[int, Query(ge=0)] = 0,
                          limit: Annotated[int, Query(ge=1, le=250)] = 50):
        projects = service().projects
        with closing(service().open()) as repo:
            projects.get(repo, project_id)
            needle = query.casefold()
            rows = [row for row in projects.resources(repo, project_id, file).values()
                    if not needle or any(needle in value.casefold()
                                         for value in (row["label"], row["key"], row.get("file"), *row["metadata"].values())
                                         if isinstance(value, str))]
            enriched, counts = service().enrich_resources(repo, rows)
            if status != "all":
                enriched = [row for row in enriched if row["translation_status"] == status]
            return {"total": len(enriched), "counts": counts, "resources": enriched[offset:offset + limit]}

    @api.get("/projects/{project_id}/imports")
    def project_imports(project_id: str):
        with closing(service().open()) as repo:
            service().projects.get(repo, project_id)
            return {"imports": [json.loads(row[0]) for row in repo.db.execute(
                "SELECT data FROM p6_imports WHERE project=? ORDER BY rowid DESC", (project_id,))]}

    @api.get("/projects/{project_id}/files/progress")
    def file_progress(project_id: str):
        return service().projects.file_progress(project_id)

    @api.get("/projects/{project_id}/files/review")
    def file_review(project_id: str, file: Annotated[str, Query(min_length=1, max_length=255)],
                    offset: Annotated[int, Query(ge=0)] = 0, limit: Annotated[int, Query(ge=1, le=250)] = 30,
                    job_id: Annotated[str | None, Query(pattern=r"^[a-f0-9]{32}$")] = None):
        return service().projects.file_review(project_id, file, offset, limit, job_id)

    @api.post("/projects/{project_id}/graph/build", status_code=202)
    def build_graph(project_id: str, body: GraphBuild, key: Key):
        return service().projects.graph_job(project_id, body.model_dump(), key)

    @api.get("/projects/{project_id}/graph")
    def read_graph(project_id: str, snapshot_id: str | None = None, query: str = "", node_id: str | None = None,
                   branch_id: str | None = None,
                   offset: Annotated[int, Query(ge=0)] = 0, limit: Annotated[int, Query(ge=1, le=100)] = 30):
        return service().projects.graph_view(project_id, snapshot_id, query, node_id, branch_id, offset, limit)

    @api.get("/projects/{project_id}/memory")
    def memory(project_id: str):
        projects = service().projects
        with closing(service().open()) as repo:
            projects.get(repo, project_id)
            drafts = [json.loads(row[0]) for row in repo.db.execute("""
                SELECT payload FROM records r WHERE project=? AND status='draft'
                AND version=(SELECT MAX(version) FROM records WHERE id=r.id)
                AND NOT EXISTS (SELECT 1 FROM invalidations i WHERE i.id=r.id AND i.version=r.version)
                ORDER BY rowid
                """, (project_id,))]
            return {"approved": projects.memory(repo, project_id), "drafts": drafts,
                    "notes": projects.list_notes(repo, project_id)}

    @api.post("/projects/{project_id}/notes", status_code=201)
    def create_note(project_id: str, body: ProjectNoteInput, key: Key):
        return service().projects.create_note(project_id, body.model_dump(), key)

    @api.put("/projects/{project_id}/notes/{note_id}")
    def update_note(project_id: str, note_id: str, body: ProjectNoteInput, key: Key):
        return service().projects.update_note(project_id, note_id, body.model_dump(), key)

    @api.delete("/projects/{project_id}/notes/{note_id}")
    def delete_note(project_id: str, note_id: str):
        return service().projects.delete_note(project_id, note_id)

    @api.post("/projects/{project_id}/memory/drafts", status_code=201)
    def draft_memory(project_id: str, body: MemoryDraft, key: Key):
        return service().projects.stage_memory(project_id, body.model_dump(exclude_none=True), key)

    @api.post("/projects/{project_id}/memory/{record_id}/{version}/{action}")
    def memory_action(project_id: str, record_id: str, version: int, action: Literal["approve", "retire"],
                      body: MemoryDecision, key: Key):
        return service().projects.decide_memory(project_id, MemoryRef(record_id, version), action, body.reason, key)

    @api.post("/model-profiles", status_code=201)
    def register_model(body: Profile, key: Key):
        return service().projects.register_model(body.model_dump(mode="json"), key)

    @api.put("/model-profiles/{profile_id}")
    def update_model(profile_id: str, body: Profile, key: Key):
        return service().projects.update_model(profile_id, body.model_dump(mode="json"), key)

    @api.get("/model-profiles")
    def model_profiles():
        return {"profiles": service().projects.list_models()}

    @api.delete("/model-profiles/{profile_id}")
    def delete_model(profile_id: str):
        return service().projects.delete_model(profile_id)

    @api.put("/workspace/model-roles")
    def workspace_model_roles(body: Bindings, key: Key):
        return service().projects.bind_workspace_models(body.roles, key)

    @api.post("/model-profiles/{profile_id}/probe")
    def probe(profile_id: str, body: ModelProbe):
        return service().projects.probe(profile_id, body.role)

    @api.put("/projects/{project_id}/model-roles")
    def model_roles(project_id: str, body: Bindings, key: Key):
        return service().projects.bind_models(project_id, body.roles, key)

    @api.post("/projects/{project_id}/translation-batches", status_code=202)
    def translation_batches(project_id: str, body: TranslationBatches, key: Key):
        return service().projects.translation_batches(project_id, body.model_dump(exclude_none=True), key)

    @api.get("/projects/{project_id}/imports/{import_id}/export/report")
    def export_summary(project_id: str, import_id: str):
        return service().projects.export(project_id, import_id)[2]

    @api.get("/projects/{project_id}/imports/{import_id}/export")
    def export(project_id: str, import_id: str):
        filename, payload, report = service().projects.export(project_id, import_id)
        if report["validation"] and not report["validation"]["ok"]:
            raise JobError(409, "project_export_validation_failed")
        return Response(payload, media_type="application/octet-stream",
                        headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(filename)})

    return api
