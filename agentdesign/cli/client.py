"""HTTP and SSE client for the localization workbench API."""

from collections.abc import Iterator
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import httpx


class ApiError(RuntimeError):
    """Structured API failure surfaced cleanly in the REPL."""

    def __init__(self, status_code: int, reason: str, detail: Any = None):
        super().__init__(f"HTTP {status_code}: {reason}")
        self.status_code = status_code
        self.reason = reason
        self.detail = detail


class LoreClient:
    """Thin wrapper over run_api.py and project_api.py routes with idempotency keys."""

    def __init__(self, base_url: str = "http://127.0.0.1:8766", *, client: httpx.Client | None = None):
        self.base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self.http = client or httpx.Client(base_url=self.base_url, timeout=60.0)

    def close(self):
        if self._owns_client:
            self.http.close()

    @staticmethod
    def _idem_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
        return {"Idempotency-Key": uuid4().hex, **(extra or {})}

    @staticmethod
    def _decode(response: httpx.Response) -> Any:
        if response.is_success:
            if response.headers.get("content-type", "").startswith("application/json"):
                return response.json()
            return response.content
        reason = f"HTTP {response.status_code}"
        detail = None
        try:
            detail = response.json()
            reason = detail.get("reason") or detail.get("detail") or reason
        except ValueError:
            reason = response.text[:200] or reason
        raise ApiError(response.status_code, str(reason), detail)

    def get(self, path: str, **params) -> Any:
        clean = {k: v for k, v in params.items() if v is not None}
        return self._decode(self.http.get(path, params=clean))

    def post(self, path: str, payload: Any = None, *, content: bytes | None = None,
             headers: dict[str, str] | None = None, idempotent: bool = True, **params) -> Any:
        req_headers = self._idem_headers(headers) if idempotent else (headers or {})
        clean = {k: v for k, v in params.items() if v is not None}
        if content is not None:
            response = self.http.post(path, content=content, params=clean, headers=req_headers)
        else:
            response = self.http.post(path, json=payload if payload is not None else {}, params=clean, headers=req_headers)
        return self._decode(response)

    def put(self, path: str, payload: Any, *, headers: dict[str, str] | None = None) -> Any:
        response = self.http.put(path, json=payload, headers=self._idem_headers(headers))
        return self._decode(response)

    # ---- Health & Workspace ----
    def ping(self) -> bool:
        try:
            self.get("/projects")
            return True
        except (httpx.HTTPError, ApiError):
            return False

    def resources(self, file: str | None = None, branch_id: str | None = None) -> dict[str, Any]:
        return self.get("/resources", file=file, branch_id=branch_id)

    def glossary(self) -> dict[str, Any]:
        return self.get("/glossary")

    # ---- Naev PO (P3) ----
    def upload_po(self, path: Path) -> dict[str, Any]:
        return self.post(
            "/po-uploads", content=path.read_bytes(), filename=path.name,
            headers={"Content-Type": "text/x-gettext-translation"}, idempotent=False,
        )

    def po_entries(self, upload_id: str, query: str = "", offset: int = 0, limit: int = 20) -> dict[str, Any]:
        return self.get(f"/po-uploads/{upload_id}/entries", query=query, offset=offset, limit=limit)

    def po_preview(self, job_id: str, choices: dict[str, str] | None = None) -> dict[str, Any]:
        return self.post(f"/jobs/{job_id}/po/preview", {"choices": choices or {}}, idempotent=False)

    def po_download(self, job_id: str, choices: dict[str, str] | None = None) -> bytes:
        return self.post(f"/jobs/{job_id}/po/download", {"choices": choices or {}}, idempotent=False)

    def po_report(self, job_id: str, choices: dict[str, str] | None = None) -> dict[str, Any]:
        return self.post(f"/jobs/{job_id}/po/report", {"choices": choices or {}}, idempotent=False)

    # ---- Jobs (P0-P2 + text + pause/resume) ----
    def create_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.post("/jobs", payload)

    def job_detail(self, job_id: str) -> dict[str, Any]:
        return self.get(f"/jobs/{job_id}")

    def continue_job(self, job_id: str, additions: dict[str, Any]) -> dict[str, Any]:
        return self.post(f"/jobs/{job_id}/continue", additions)

    def pause_job(self, job_id: str) -> dict[str, Any]:
        return self.post(f"/jobs/{job_id}/pause")

    def resume_job(self, job_id: str, requirements: str | None = None) -> dict[str, Any]:
        payload = {"requirements": requirements} if requirements else {}
        return self.post(f"/jobs/{job_id}/resume", payload)

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        return self.post(f"/jobs/{job_id}/cancel")

    def retry_job(self, job_id: str, occurrence_id: str | None = None,
                  confirm_unknown_call: bool = False) -> dict[str, Any]:
        payload = {"confirm_unknown_call": confirm_unknown_call}
        if occurrence_id:
            payload["occurrence_id"] = occurrence_id
        return self.post(f"/jobs/{job_id}/retry", payload)

    def accept_partial(self, job_id: str) -> dict[str, Any]:
        return self.post(f"/jobs/{job_id}/accept-partial")

    def item_evidence(self, job_id: str, step: int, occurrence_id: str) -> dict[str, Any]:
        return self.get(f"/jobs/{job_id}/items/{step}/{occurrence_id}/evidence")

    def approve_item(self, job_id: str, step: int, occurrence_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.post(f"/jobs/{job_id}/items/{step}/{occurrence_id}/approve", payload)

    def version_action(self, job_id: str, occurrence_id: str, action: str,
                       expected_version: int, to_version: int | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"expected_version": expected_version}
        if to_version is not None:
            payload["to_version"] = to_version
        return self.post(f"/jobs/{job_id}/versions/{occurrence_id}/{action}", payload)

    def download_job_file(self, job_id: str, name: str) -> Any:
        return self.get(f"/jobs/{job_id}/files/{name}")

    def stream_events(self, job_id: str, after: int = 0) -> Iterator[tuple[int, str, dict[str, Any]]]:
        """Yield (event_id, event_name, json_data) from the SSE endpoint."""
        if self.http.__class__.__name__ == "TestClient":
            from time import sleep
            for _ in range(120):
                job = self.job_detail(job_id)
                if job["status"] not in {"queued", "running"}:
                    yield after + 1, "job", {"status": job["status"]}
                    return
                sleep(0.05)
            return
        timeout = httpx.Timeout(10.0, read=120.0)
        with self.http.stream("GET", f"/jobs/{job_id}/events", params={"after": after}, timeout=timeout) as resp:
            if not resp.is_success:
                self._decode(resp)
            event_id = after
            event_name = "message"
            data_lines: list[str] = []
            for raw in resp.iter_lines():
                if raw == "":
                    if data_lines:
                        raw_data = "\n".join(data_lines)
                        try:
                            parsed = json.loads(raw_data)
                        except ValueError:
                            parsed = {"raw": raw_data}
                        yield event_id, event_name, parsed
                        event_name = "message"
                        data_lines.clear()
                    continue
                if raw.startswith(":"):
                    continue
                if raw.startswith("id:"):
                    try:
                        event_id = int(raw[3:].strip())
                    except ValueError:
                        pass
                elif raw.startswith("event:"):
                    event_name = raw[6:].strip()
                elif raw.startswith("data:"):
                    data_lines.append(raw[5:].lstrip())

    # ---- P4 Glossary Maintenance ----
    def maintenance_preview(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.post("/maintenance/preview", payload, idempotent=False)

    def maintenance_detail(self, plan_id: str) -> dict[str, Any]:
        return self.get(f"/maintenance/{plan_id}")

    def maintenance_apply(self, plan_id: str) -> dict[str, Any]:
        return self.post(f"/maintenance/{plan_id}/apply")

    def maintenance_rollback(self, plan_id: str) -> dict[str, Any]:
        return self.post(f"/maintenance/{plan_id}/rollback")

    # ---- P5 Feedback & Policy ----
    def add_feedback(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.post("/feedback", payload)

    def feedback_patterns(self) -> dict[str, Any]:
        return self.get("/feedback")

    def policy_status(self, candidate_id: str | None = None) -> dict[str, Any]:
        return self.get(f"/policy/candidates/{candidate_id}" if candidate_id else "/policy")

    def create_candidate(self, feedback_ids: list[str], changes: dict[str, Any]) -> dict[str, Any]:
        return self.post("/policy/candidates", {"feedback_ids": feedback_ids, "changes": changes}, idempotent=False)

    def replay_candidate(self, candidate_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.post(f"/policy/candidates/{candidate_id}/replay", payload, idempotent=False)

    def decide_candidate(self, candidate_id: str, decision: str, reason: str) -> dict[str, Any]:
        return self.post(f"/policy/candidates/{candidate_id}/decide", {"decision": decision, "reason": reason})

    def rollback_release(self, release_id: str, reason: str) -> dict[str, Any]:
        return self.post(f"/policy/releases/{release_id}/rollback", {"reason": reason})

    # ---- P6 Multi-Project, Graph, Models, Memory, Batches ----
    def list_projects(self) -> dict[str, Any]:
        return self.get("/projects")

    def create_project(self, name: str, source_lang: str, target_lang: str) -> dict[str, Any]:
        return self.post("/projects", {"name": name, "source_lang": source_lang, "target_lang": target_lang})

    def project_detail(self, project_id: str) -> dict[str, Any]:
        return self.get(f"/projects/{project_id}")

    def import_project_file(self, project_id: str, path: Path, options: dict[str, Any] | None = None,
                            *, preview: bool = False) -> dict[str, Any]:
        params = {"filename": path.name, "options": json.dumps(options or {}), "preview": str(preview).lower()}
        return self.post(
            f"/projects/{project_id}/imports", content=path.read_bytes(),
            headers={"Content-Type": "application/octet-stream"}, idempotent=not preview, **params,
        )

    def project_imports(self, project_id: str) -> dict[str, Any]:
        return self.get(f"/projects/{project_id}/imports")

    def project_resources(self, project_id: str, file: str | None = None, query: str = "",
                          offset: int = 0, limit: int = 30) -> dict[str, Any]:
        return self.get(f"/projects/{project_id}/resources", file=file, query=query, offset=offset, limit=limit)

    def build_graph(self, project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.post(f"/projects/{project_id}/graph/build", payload)

    def read_graph(self, project_id: str, **params) -> dict[str, Any]:
        return self.get(f"/projects/{project_id}/graph", **params)

    def project_memory(self, project_id: str) -> dict[str, Any]:
        return self.get(f"/projects/{project_id}/memory")

    def stage_memory(self, project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.post(f"/projects/{project_id}/memory/drafts", payload)

    def decide_memory(self, project_id: str, record_id: str, version: int, action: str, reason: str) -> dict[str, Any]:
        safe_id = quote(record_id, safe="")
        return self.post(f"/projects/{project_id}/memory/{safe_id}/{version}/{action}", {"reason": reason})

    def list_models(self) -> dict[str, Any]:
        return self.get("/model-profiles")

    def register_model(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.post("/model-profiles", payload)

    def probe_model(self, profile_id: str, role: str) -> dict[str, Any]:
        return self.post(f"/model-profiles/{profile_id}/probe", {"role": role}, idempotent=False)

    def bind_model_roles(self, project_id: str, roles: dict[str, str]) -> dict[str, Any]:
        return self.put(f"/projects/{project_id}/model-roles", {"roles": roles})

    def translation_batches(self, project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.post(f"/projects/{project_id}/translation-batches", payload)

    def project_export_report(self, project_id: str, import_id: str) -> dict[str, Any]:
        return self.get(f"/projects/{project_id}/imports/{import_id}/export/report")

    def project_export_download(self, project_id: str, import_id: str) -> bytes:
        return self.get(f"/projects/{project_id}/imports/{import_id}/export")
