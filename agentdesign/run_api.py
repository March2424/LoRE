#!/usr/bin/env python3
"""Serve the local P2 Naev task API and its single persistent worker."""

import argparse
import asyncio
from contextlib import asynccontextmanager, closing
import os
from pathlib import Path
from threading import Event, Thread
from typing import Annotated, Literal

from fastapi import FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from run_p0 import DEFAULT_DB, HERE
from workbench.job_intent import scope_resources
from workbench.job_store import JobError, encode
from workbench.jobs import TERMINAL, Workbench
from workbench.po import MAX_UPLOAD_BYTES
from workbench.project_api import router as project_router

Intent = Literal["translate", "review", "propose_update", "explain", "export"]
Key = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=120)]
Text = Annotated[str, Field(min_length=1, max_length=5000)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Selection(Input):
    project: Annotated[str, Field(pattern=r"^(naev|[a-f0-9]{32})$")] = "naev"
    file: str | None = None
    branch_id: str | None = None
    source_ids: list[str] = Field(default_factory=list, max_length=64)
    target_lang: str | None = None


class Term(Input):
    source: Text
    target: Text
    previous_target: Text | None = None


class Clarification(Input):
    intents: list[Intent] | None = Field(default=None, min_length=1, max_length=5)
    targets: list[str] | None = Field(default=None, min_length=1, max_length=64)
    target_lang: str | None = None
    source_lang: str | None = None
    requirements: Text | None = None
    term: Term | None = None
    drafts: dict[str, Text] | None = None


class NewJob(Clarification):
    text: Text | None = None
    intent: Intent | None = None
    selection: Selection = Field(default_factory=Selection)
    upload_id: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")] | None = None
    source_text: Annotated[str, StringConstraints(strip_whitespace=False, min_length=1, max_length=5000)] | None = None
    max_calls: int | None = Field(default=None, ge=1, le=1000)


class Retry(Input):
    occurrence_id: str | None = None
    confirm_unknown_call: bool = False


class Resume(Input):
    requirements: Text | None = None


class Approval(Input):
    model_config = ConfigDict(str_strip_whitespace=False)
    expected_version: int = Field(ge=0)
    expected_draft_hash: str | None
    target: Text | None = None
    reason: Annotated[str, Field(min_length=1, max_length=1000)] | None = None


class VersionAction(Input):
    expected_version: int = Field(ge=0)
    to_version: int | None = Field(default=None, ge=1)


class ExportChoices(Input):
    choices: dict[str, str] = Field(default_factory=dict, max_length=12)


class GlossaryScope(Input):
    file: str | None = None
    branch_id: str | None = None
    occurrence_ids: list[str] = Field(default_factory=list, max_length=100)


class GlossaryChange(Input):
    rule_id: str | None = None
    source: Annotated[str, Field(min_length=1, max_length=250)]
    target: Annotated[str, Field(min_length=1, max_length=250)]
    previous_target: Annotated[str, Field(min_length=1, max_length=250)] | None = None
    scope: GlossaryScope = Field(default_factory=GlossaryScope)


class TraceRef(Input):
    job_id: str
    step: int = Field(ge=0)
    occurrence_id: str


class EntityAlias(Input):
    query: Annotated[str, Field(min_length=1, max_length=120)]
    canonical: Annotated[str, Field(min_length=1, max_length=120)]


class Feedback(TraceRef):
    category: Literal["correction", "retrieval", "prompt", "memory", "skill", "harness"]
    reason: Text
    after: Text | None = None
    alias: EntityAlias | None = None


class PolicyChanges(Input):
    prompts: dict[str, str] = Field(default_factory=dict, max_length=5)
    handoff_template: str = Field(default="{reason}", max_length=500)
    aliases: dict[str, str] = Field(default_factory=dict, max_length=30)
    lookup_results: int = Field(default=2, ge=1, le=2)
    skill_issues: list[str] = Field(default_factory=list, max_length=4)
    memory_plan_id: str | None = None


class Candidate(Input):
    feedback_ids: list[str] = Field(min_length=1, max_length=24)
    changes: PolicyChanges = Field(default_factory=PolicyChanges)


class ReplayCase(TraceRef):
    expected_evidence_ids: list[str] = Field(default_factory=list, max_length=6)
    expected_verdict: Literal["pass", "repair", "needs_human"] | None = None


class Replay(Input):
    mode: Literal["record", "strategy"]
    dataset: Literal["feedback", "validation"]
    live: bool = False
    cases: list[ReplayCase] = Field(min_length=1, max_length=8)


class PolicyDecision(Input):
    decision: Literal["publish", "retain"]
    reason: Text


class PolicyRollback(Input):
    reason: Text


class AskOccurrence(Input):
    question: Text


def lock_worker(lease):
    if os.name == "nt":
        import msvcrt

        lease.seek(0)
        msvcrt.locking(lease.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)


def create_app(data_dir: Path, source_db: Path = DEFAULT_DB, *, offline=False, projects_only=False):
    @asynccontextmanager
    async def lifespan(app):
        data_dir.mkdir(parents=True, exist_ok=True)
        with (data_dir / "worker.lock").open("a") as lease:
            try:
                lock_worker(lease)
            except OSError as error:
                raise RuntimeError("This workspace is already served by another worker") from error
            service = Workbench(data_dir, source_db, offline=offline, projects_only=projects_only)
            service.initialize()
            app.state.workbench = service
            stop = Event()
            thread = Thread(target=service.serve, args=(stop,), name="naev-task-worker")
            thread.start()
            try:
                yield
            finally:
                stop.set()
                await asyncio.to_thread(thread.join)

    app = FastAPI(title="LoRE localization workbench", version="0.2.0", lifespan=lifespan)

    def service():
        return app.state.workbench

    app.include_router(project_router(service))

    def receipt(result):
        return {"reason": None, "artifacts": [], "evidence_refs": [], "next_actions": [], **result}

    @app.exception_handler(JobError)
    async def job_error(request, error):
        return JSONResponse(
            receipt({"status": "error", "reason": error.reason, "job_id": request.path_params.get("job_id")}),
            status_code=error.status,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        return JSONResponse(receipt({
            "status": "error", "reason": "invalid_request", "job_id": request.path_params.get("job_id"),
            "fields": [list(item["loc"]) for item in error.errors()],
            "details": [{"field": ".".join(str(x) for x in item["loc"]), "type": item["type"], "msg": item["msg"]}
                        for item in error.errors()],
        }), status_code=422)

    @app.get("/resources")
    def resources(file: str | None = None, branch_id: str | None = None,
                  status: Literal["all", "translated", "untranslated", "awaiting_review"] = "all"):
        rows = list(scope_resources(service().catalog, {"file": file, "branch_id": branch_id}).values())
        with closing(service().open()) as repo:
            enriched, counts = service().enrich_resources(repo, rows)
        if status != "all":
            enriched = [row for row in enriched if row["translation_status"] == status]
        return {"project": "naev", "counts": counts, "resources": enriched}

    @app.get("/occurrences/{project_id}/{occurrence_id:path}/inspect")
    def inspect_occurrence(project_id: str, occurrence_id: str):
        return service().inspect_occurrence(project_id, occurrence_id)

    @app.post("/occurrences/{project_id}/{occurrence_id:path}/ask")
    def ask_occurrence(project_id: str, occurrence_id: str, body: AskOccurrence):
        return service().ask_occurrence(project_id, occurrence_id, body.question)

    @app.get("/glossary")
    def glossary():
        return service().maintenance.rules()

    @app.post("/maintenance/preview", status_code=201)
    def preview_maintenance(body: GlossaryChange):
        return service().maintenance.preview(body.model_dump(exclude_none=True))

    @app.get("/maintenance/{plan_id}")
    def maintenance_detail(plan_id: str):
        return service().maintenance.detail(plan_id)

    @app.get("/maintenance")
    def maintenance_plans():
        return service().maintenance.plans()

    @app.post("/maintenance/{plan_id}/apply", status_code=202)
    def apply_maintenance(plan_id: str, key: Key):
        return service().maintenance.apply(plan_id, key)

    @app.post("/maintenance/{plan_id}/rollback")
    def rollback_maintenance(plan_id: str, key: Key):
        return service().maintenance.rollback(plan_id, key)

    @app.post("/feedback", status_code=201)
    def add_feedback(body: Feedback, key: Key):
        return service().feedback.feedback(body.model_dump(exclude_none=True), key)

    @app.get("/feedback")
    def feedback_patterns():
        return service().feedback.patterns()

    @app.get("/policy")
    def policy_status():
        return service().feedback.status()

    @app.post("/policy/candidates", status_code=201)
    def policy_candidate(body: Candidate):
        return service().feedback.candidate(body.feedback_ids, body.changes.model_dump(exclude_unset=True))

    @app.get("/policy/candidates/{candidate_id}")
    def policy_candidate_status(candidate_id: str):
        return service().feedback.status(candidate_id)

    @app.get("/policy/candidates/{candidate_id}/cases")
    def policy_replay_cases(candidate_id: str, dataset: Literal["feedback", "validation"], job_id: str | None = None):
        return service().feedback.replay_cases(candidate_id, dataset, job_id)

    @app.post("/policy/candidates/{candidate_id}/replay", status_code=201)
    def policy_replay(candidate_id: str, body: Replay):
        return service().feedback.replay(candidate_id, body.model_dump(exclude_none=True))

    @app.post("/policy/candidates/{candidate_id}/decide")
    def policy_decide(candidate_id: str, body: PolicyDecision, key: Key):
        return service().feedback.decide(candidate_id, body.decision, body.reason, key)

    @app.post("/policy/releases/{release_id}/rollback")
    def policy_rollback(release_id: str, body: PolicyRollback, key: Key):
        return service().feedback.rollback(release_id, body.reason, key)

    no_cache = {"Cache-Control": "no-store"}

    @app.get("/", include_in_schema=False)
    def web_home():
        return FileResponse(HERE / "web/index.html", headers=no_cache)

    @app.get("/workbench.js", include_in_schema=False)
    def web_bundle():
        path = HERE / "web/dist/app.js"
        if not path.is_file():
            raise JobError(503, "build_web_with_npm_prefix_web_run_build")
        return FileResponse(path, media_type="text/javascript", headers=no_cache)

    @app.get("/workbench.css", include_in_schema=False)
    def web_styles():
        return FileResponse(HERE / "web/design/ui.css", media_type="text/css", headers=no_cache)

    @app.get("/workbench-bundle.css", include_in_schema=False)
    def web_bundle_styles():
        path = HERE / "web/dist/app.css"
        if not path.is_file():
            raise JobError(503, "build_web_with_npm_prefix_web_run_build")
        return FileResponse(path, media_type="text/css", headers=no_cache)

    @app.get("/assets/{filename:path}", include_in_schema=False)
    def web_assets(filename: str):
        assets_dir = (HERE / "web/assets").resolve()
        candidate = (assets_dir / filename).resolve()
        if candidate == assets_dir or assets_dir not in candidate.parents or not candidate.is_file():
            raise JobError(404, "asset_not_found")
        return FileResponse(candidate, headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/workspace")
    def workspace():
        with closing(service().open()) as repo:
            default_models = service().projects.workspace_model_bindings(repo)
        return {
            "offline": offline,
            "naev": not projects_only,
            "naev_snapshot": service().snapshot,
            "capabilities": {"direct_api_key": True, "partial_graph_build": True},
            "default_models": default_models,
            "default_model_config": {
                "model": service().config["model"],
                "base_url": service().config["base_url"],
                "api_key_env": "DEEPSEEK_API_KEY",
                "credential_configured": bool(os.environ.get("DEEPSEEK_API_KEY")),
            },
        }

    @app.get("/jobs")
    def list_jobs(project: str | None = None, kind: str | None = None, intent: str | None = None,
                  status: str | None = None, offset: Annotated[int, Query(ge=0)] = 0,
                  limit: Annotated[int, Query(ge=1, le=100)] = 30):
        return service().list_jobs(project, offset, limit, kind=kind, intent=intent, status=status)

    @app.get("/job-events")
    async def job_events(request: Request):
        def revision():
            with closing(service().open()) as repo:
                return repo.db.execute("SELECT COALESCE(MAX(seq),0) FROM p2_events WHERE public=1").fetchone()[0]

        async def stream():
            previous = None
            idle = 0
            while not await request.is_disconnected():
                current = await asyncio.to_thread(revision)
                if current != previous:
                    previous = current
                    yield f"data: {current}\n\n"
                    idle = 0
                elif idle == 0:
                    yield ": keep-alive\n\n"
                idle = (idle + 1) % 20
                await asyncio.sleep(0.5)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/po-uploads", status_code=201)
    async def upload_po(request: Request, filename: Annotated[str, Query(min_length=1, max_length=255)]):
        filename = Path(filename).name
        if Path(filename).suffix.lower() != ".po":
            raise JobError(422, "po_extension_required")
        content = bytearray()
        async for chunk in request.stream():
            if len(content) + len(chunk) > MAX_UPLOAD_BYTES:
                raise JobError(413, "po_upload_too_large")
            content.extend(chunk)
        return await asyncio.to_thread(service().import_po, filename, bytes(content))

    @app.get("/po-uploads/{upload_id}/entries")
    def po_entries(upload_id: str, query: Annotated[str, Query(max_length=250)] = "",
                   offset: Annotated[int, Query(ge=0)] = 0,
                   limit: Annotated[int, Query(ge=1, le=50)] = 30):
        return service().po_entries(upload_id, query, offset, limit)

    @app.post("/jobs/{job_id}/po/preview")
    def po_preview(job_id: str, body: ExportChoices):
        return service().export_po(job_id, body.choices).report

    @app.post("/jobs/{job_id}/po/download")
    def po_download(job_id: str, body: ExportChoices):
        export = service().export_po(job_id, body.choices)
        if not export.report["download_ready"]:
            raise JobError(409, "resolve_po_export_issues_first")
        return Response(export.content, media_type="text/x-gettext-translation; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="naev-zh_Hans.po"'})

    @app.post("/jobs/{job_id}/po/report")
    def po_report(job_id: str, body: ExportChoices):
        return JSONResponse(service().export_po(job_id, body.choices).report,
                            headers={"Content-Disposition": 'attachment; filename="naev-po-changes.json"'})

    @app.post("/jobs", status_code=202)
    def create(body: NewJob, key: Key):
        request = body.model_dump(exclude_none=True)
        if body.intent:
            if body.intents and body.intents != [body.intent]:
                raise JobError(422, "conflicting_intent_fields")
            request["intents"] = [request.pop("intent")]
        if body.source_text is not None:
            if not body.source_text.strip():
                raise JobError(422, "source_text_required")
            if (body.upload_id or body.targets or body.selection.source_ids or body.selection.file
                    or body.selection.branch_id or body.selection.project != "naev"):
                raise JobError(422, "text_source_cannot_be_combined_with_resource_selection")
            if not request.get("text") and not request.get("intents"):
                request["intents"] = ["translate", "export"]
        if body.upload_id and body.selection.project != "naev":
            raise JobError(422, "use_project_import_for_project_po")
        if not request.get("text") and not request.get("intents"):
            raise JobError(422, "intent_or_text_required")
        return service().detail(service().create(request, key))

    @app.get("/jobs/{job_id}")
    def detail(job_id: str):
        return service().detail(job_id)

    @app.post("/jobs/{job_id}/continue", status_code=202)
    def continue_job(job_id: str, body: Clarification, key: Key):
        return receipt(service().continue_job(job_id, body.model_dump(exclude_none=True), key))

    @app.post("/jobs/{job_id}/cancel")
    def cancel(job_id: str, key: Key):
        return receipt(service().job_action(job_id, "cancel", {}, key))

    @app.post("/jobs/{job_id}/pause", status_code=202)
    def pause(job_id: str, key: Key):
        return receipt(service().job_action(job_id, "pause", {}, key))

    @app.post("/jobs/{job_id}/resume", status_code=202)
    def resume(job_id: str, body: Resume, key: Key):
        return receipt(service().job_action(job_id, "resume", body.model_dump(exclude_none=True), key))

    @app.post("/jobs/{job_id}/retry", status_code=202)
    def retry(job_id: str, body: Retry, key: Key):
        return receipt(service().job_action(job_id, "retry", body.model_dump(), key))

    @app.post("/jobs/{job_id}/accept-partial", status_code=202)
    def accept_partial(job_id: str, key: Key):
        return receipt(service().job_action(job_id, "accept_partial", {}, key))

    @app.post("/jobs/{job_id}/items/{step}/{occurrence_id}/approve")
    def approve(job_id: str, step: int, occurrence_id: str, body: Approval, key: Key):
        return receipt(service().approve(job_id, step, occurrence_id, body.model_dump(), key))

    @app.get("/jobs/{job_id}/items/{step}/{occurrence_id}/evidence")
    def evidence(job_id: str, step: int, occurrence_id: str):
        return service().item_evidence(job_id, step, occurrence_id)

    @app.get("/jobs/{job_id}/items/{step}/{occurrence_id}/evidence/context")
    def source_context(job_id: str, step: int, occurrence_id: str,
                       source_id: Annotated[str, Query(min_length=1, max_length=512)],
                       source_version: Annotated[int, Query(ge=1)]):
        return service().source_context(job_id, step, occurrence_id, source_id, source_version)

    @app.post("/jobs/{job_id}/versions/{occurrence_id}/{action}")
    def version_action(job_id: str, occurrence_id: str, action: Literal["lock", "unlock", "rollback"],
                       body: VersionAction, key: Key):
        return receipt(service().version_action(job_id, occurrence_id, action, body.model_dump(), key))

    @app.get("/jobs/{job_id}/versions/{occurrence_id}")
    def version_history(job_id: str, occurrence_id: str):
        return service().version_history(job_id, occurrence_id)

    @app.get("/jobs/{job_id}/files/{name}")
    def file(job_id: str, name: Literal["report.json", "export.json"]):
        report = service().detail(job_id)
        if name == "export.json":
            export = next((step for step in report["results"] if step["intent"] == "export"), None)
            if export is None:
                raise JobError(404, "export_not_ready")
            report = {"job_id": job_id, "status": report["status"], **export}
        return JSONResponse(report, headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/jobs/{job_id}/events")
    async def events(job_id: str, request: Request, after: Annotated[int, Query(ge=0)] = 0,
                     last_event_id: Annotated[int | None, Header(alias="Last-Event-ID", ge=0)] = None):
        def fetch(cursor):
            with closing(service().open()) as repo:
                status = repo.get(job_id)["status"]
                return repo.events(job_id, cursor), status

        cursor = max(after, last_event_id or 0)
        await asyncio.to_thread(fetch, cursor)  # Resolve 404 before sending headers.

        async def stream():
            next_id = cursor
            idle = 0
            while not await request.is_disconnected():
                batch, status = await asyncio.to_thread(fetch, next_id)
                if batch:
                    idle = 0
                    for event in batch:
                        next_id = event["id"]
                        yield f'id: {next_id}\nevent: {event["event"]}\ndata: {encode(event)}\n\n'
                    continue
                if status in TERMINAL:
                    return
                if idle == 0:
                    yield ": keep-alive\n\n"
                idle = (idle + 1) % 20
                await asyncio.sleep(0.5)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return app


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="Use deterministic fixtures through the local Pi runtime")
    parser.add_argument("--projects-only", action="store_true", help="Initialize an empty workspace without the Naev snapshot")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    prefix = "projects" if args.projects_only else "p2"
    mode = "offline" if args.offline else "service"
    data_dir = args.data_dir or HERE / "runs" / f"{prefix}-{mode}"
    uvicorn.run(create_app(data_dir, args.db, offline=args.offline, projects_only=args.projects_only),
                host="127.0.0.1", port=args.port)
