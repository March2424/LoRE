"""Durable model operations. Replayed calls preserve their receipts and budget usage."""

import json

from localization_agent.schemas.memory import text_hash

from .job_store import encode
from .models import ModelFailure


class JobModels:
    def __init__(self, base, jobs, job_id, lane):
        self.base, self.jobs = base, jobs
        self.job_id, self.lane = job_id, lane
        self.config, self.offline = base.config, base.offline
        self.slot = 0

    def _run(self, fingerprint, reserve, call, replay=None):
        self.slot += 1
        slot_key = (self.job_id, self.lane, self.slot)
        signature = text_hash(encode(fingerprint))
        db = self.jobs.db
        with self.jobs.transaction():
            row = db.execute(
                "SELECT * FROM p2_calls WHERE job=? AND lane=? AND slot=? ORDER BY attempt DESC LIMIT 1", slot_key,
            ).fetchone()
            if row and row["signature"] != signature:
                raise ModelFailure("call_input_changed")
            if row and row["status"] == "done":
                value = json.loads(row["data"])
            elif row and row["status"] in {"started", "unknown"}:
                raise ModelFailure("unknown_provider_result")
            elif row and row["status"] == "failed":
                raise ModelFailure(row["data"])
            else:
                spent = db.execute("SELECT COALESCE(SUM(units),0) FROM p2_calls WHERE job=?",
                                   (self.job_id,)).fetchone()[0]
                if spent + reserve > self.config["max_calls"]:
                    raise ModelFailure("call_budget_exhausted")
                attempt = row["attempt"] + 1 if row else 0
                db.execute("INSERT INTO p2_calls VALUES (?,?,?,?,?,?,?,NULL)",
                           (*slot_key, attempt, signature, "started", reserve))
                value = None
        if value is not None:
            if replay:
                replay(value)
            return tuple(value)

        started = self.base.calls
        saved = False
        original_trace = self.base.trace

        def persist_receipt(event, **entry):
            nonlocal saved
            if event == "model_call" and not entry.get("error"):
                output = entry["receipt"] if "receipt" in entry else entry["output"]
                self._finish(slot_key, attempt, "done", max(1, self.base.calls - started),
                             encode([output, entry["id"]]))
                saved = True
            original_trace(event, **entry)

        self.base.trace = persist_receipt
        try:
            value = call()
        except ModelFailure as error:
            unknown = (
                error.code.startswith(("APIConnectionError", "APITimeoutError", "pi_process:"))
                or error.code == "pi_provider_error"
            )
            status = "unknown" if unknown else "failed"
            self._finish(slot_key, attempt, status, reserve, error.code)
            raise ModelFailure("unknown_provider_result" if unknown else error.code, error.call_id) from error
        finally:
            self.base.trace = original_trace
        if not saved:
            self._finish(slot_key, attempt, "done", max(1, self.base.calls - started), encode(value))
        return value

    def _finish(self, key, attempt, status, units, value):
        with self.jobs.db:
            self.jobs.db.execute("UPDATE p2_calls SET status=?,units=?,data=? WHERE job=? AND lane=? AND slot=? AND attempt=?",
                                 (status, units, value, *key, attempt))

    def ask(self, role, prompt, *, item=None, fixture=None, schema=None):
        return self._run(
            [role, prompt, schema], 1,
            lambda: self.base.ask(role, prompt, item=item, fixture=fixture, schema=schema),
        )

    def pi(self, prompt, **kwargs):
        tool = kwargs.get("tool")
        def replay(value):
            if not tool:
                return
            events = value[0]["events"]
            results = {event["id"]: event["result"]["details"] for event in events
                       if event["type"] == "tool_execution_end" and not event["error"]}
            for event in events:
                if event["type"] == "tool_execution_start" and event["id"] in results:
                    actual = tool(event["name"], event["args"], event["id"])
                    if actual != results[event["id"]]:
                        raise ModelFailure("stale_tool_receipt")
        return self._run(
            ["pi", kwargs.get("mode"), prompt, kwargs.get("schema"), bool(tool)], 2 if tool else 1,
            lambda: self.base.pi(prompt, **kwargs), replay,
        )
