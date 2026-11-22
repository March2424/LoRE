"""Bounded Responses calls and the subprocess interface to the Pi Agent."""

import json
import os
from queue import Empty, Queue
import subprocess
from threading import Thread
from time import monotonic
from uuid import uuid4

from openai import APIError, OpenAI

from localization_agent.models.structured import parse_json_reply

SELECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "selected_ids": {"type": "array", "items": {"type": "string"}},
        "rationale": {"type": "string"},
    },
    "required": ["selected_ids", "rationale"],
    "additionalProperties": False,
}

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "repair", "needs_human"]},
        "reason": {"type": "string"},
    },
    "required": ["verdict", "reason"],
    "additionalProperties": False,
}


class ModelFailure(RuntimeError):
    def __init__(self, code, call_id=None):
        super().__init__(code)
        self.code = code
        self.call_id = call_id


def for_role(config, role):
    return {**config, **config.get("roles", {}).get(role, {})}


def parse_review(text):
    result = parse_json_reply(text)
    if (
        not isinstance(result, dict) or set(result) != {"verdict", "reason"}
        or result["verdict"] not in ("pass", "repair", "needs_human")
        or not isinstance(result["reason"], str) or not result["reason"].strip()
    ):
        raise ValueError("review_schema")
    return result


def pi_with_tools(path, request, tool, timeout):
    """Serve Pi's JSON-lines tool calls on the worker that owns the read view."""
    deadline = monotonic() + timeout
    with subprocess.Popen(["node", str(path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL) as process:
        lines = Queue()

        def read_stdout():
            try:
                for line in process.stdout:
                    lines.put(line)
            finally:
                lines.put(None)

        def send(payload):
            process.stdin.write(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
            process.stdin.flush()

        reader = Thread(target=read_stdout, name="pi-receipts")
        reader.start()
        try:
            send(request)
            while True:
                try:
                    line = lines.get(timeout=max(0, deadline - monotonic()))
                except Empty:
                    raise subprocess.TimeoutExpired(path, timeout) from None
                if line is None:
                    raise ValueError("Pi ended without a receipt")
                event = json.loads(line)
                if event.get("type") != "tool_request":
                    process.stdin.close()
                    process.wait(timeout=max(0, deadline - monotonic()))
                    return event
                result = tool(event["name"], event["args"], event["id"])
                send({"type": "tool_result", "id": event["id"], "result": result})
        finally:
            if process.poll() is None:
                process.kill()
            reader.join()


class Responses:
    def __init__(self, config, trace, pi_path, *, offline=False):
        self.config, self.trace, self.pi_path = config, trace, pi_path
        self.offline = offline
        self.calls = 0

    def reserve(self, count=1):
        if self.calls + count > self.config["max_calls"]:
            raise ModelFailure("call_budget_exhausted")
        self.calls += count

    def ask(self, role, prompt, *, item=None, fixture=None, schema=None):
        self.reserve()
        config = for_role(self.config, role)
        call = {"id": uuid4().hex, "role": role, "item": item, "input": prompt,
                "transport": "offline_fixture" if self.offline else "responses", "model": config["model"]}
        started = monotonic()
        try:
            if self.offline:
                text = fixture
            else:
                credential = config.get("api_key_env", "DEEPSEEK_API_KEY")
                key = os.environ.get(credential) if credential else "local"
                if not key:
                    raise ModelFailure("model_credential_unavailable", call["id"])
                client = OpenAI(api_key=key, base_url=config["base_url"], timeout=config["timeout_seconds"], max_retries=0)
                options = {"text": {"format": {"type": "json_object"}}} if role == "extractor" else {}
                if schema is not None:
                    options["text"] = {"format": {"type": "json_schema", "name": "context_selection",
                                                  "strict": True, "schema": schema}}
                    options["reasoning"] = {"effort": "low"}
                with client:
                    response = client.responses.create(
                        model=config["model"], input=prompt, store=False,
                        max_output_tokens=config["max_output_tokens"][role], **options,
                    )
                text = response.output_text
                call.update(
                    response_id=response.id, response_status=response.status, response_model=response.model,
                    output_types=[part.type for part in response.output],
                    usage=response.usage.model_dump() if response.usage else None,
                )
                if response.status != "completed":
                    detail = response.incomplete_details or response.error
                    reason = getattr(detail, "reason", None) or getattr(detail, "code", None)
                    raise ModelFailure(f"response_{response.status}:{reason}", call["id"])
            if not text or not text.strip():
                raise ModelFailure("empty_response", call["id"])
            call["output"] = text
            return text, call["id"]
        except APIError as error:
            call["http_status"] = getattr(error, "status_code", None)
            call["error"] = f"{type(error).__name__}:{getattr(error, 'code', None)}"
            raise ModelFailure(call["error"], call["id"]) from None
        except ModelFailure as error:
            call["error"] = error.code
            raise
        finally:
            call["elapsed_ms"] = round(1000 * (monotonic() - started))
            self.trace("model_call", **call)

    def pi(self, prompt, *, item=None, fixture=None, mode="review", challenge=None, schema=REVIEW_SCHEMA,
           tool=None):
        reserved = 2 if mode == "tools" or tool is not None else 1
        self.reserve(reserved)
        role = "intent" if mode == "intent" else "reviewer" if item else "pi_probe"
        call = {"id": uuid4().hex, "role": role, "item": item, "input": prompt}
        started = monotonic()
        config = for_role(self.config, role)
        call["model"] = config["model"]
        request = {"config": config, "mode": mode, "prompt": prompt, "schema": schema, "challenge": challenge}
        timeout = reserved * config["timeout_seconds"] + 10
        if self.offline:
            request["fixture"] = fixture
        try:
            if tool is not None:
                receipt = pi_with_tools(self.pi_path, {**request, "mode": "source_review"}, tool, timeout)
            else:
                result = subprocess.run(
                    ["node", str(self.pi_path)], input=json.dumps(request, ensure_ascii=False),
                    text=True, encoding="utf-8", capture_output=True, check=True, timeout=timeout,
                )
                receipt = json.loads(result.stdout)
            call["receipt"] = receipt
            self.calls -= reserved - len(receipt["calls"])
            if receipt.get("error"):
                last = receipt["calls"][-1] if receipt["calls"] else {}
                if last.get("response_status") == "incomplete":
                    raise ModelFailure(f'pi_response_incomplete:{last.get("error_code")}', call["id"])
                raise ModelFailure("pi_provider_error", call["id"])
            return receipt, call["id"]
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            call["error"] = f"pi_process:{type(error).__name__}"
            raise ModelFailure(call["error"], call["id"]) from None
        except ModelFailure as error:
            call["error"] = error.code
            raise
        finally:
            call["elapsed_ms"] = round(1000 * (monotonic() - started))
            self.trace("model_call", **call)
