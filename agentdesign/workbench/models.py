"""Bounded Responses calls and the subprocess interface to the Pi Agent."""

import json
import os
from queue import Empty, Queue
import subprocess
from threading import Thread
from time import monotonic
from uuid import uuid4

from openai import APIError, BadRequestError, OpenAI

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
    def __init__(self, config, trace, pi_path, *, offline=False, secret_resolver=None):
        self.config, self.trace, self.pi_path = config, trace, pi_path
        self.offline = offline
        self.secret_resolver = secret_resolver
        self.calls = 0

    def _resolve_api_key(self, config):
        if config.get("api_key"):
            return config["api_key"]
        if config.get("profile_id") and self.secret_resolver:
            secret = self.secret_resolver(config["profile_id"])
            if secret:
                return secret
        credential = config.get("api_key_env")
        if "api_key_env" not in config:
            credential = "DEEPSEEK_API_KEY"
        if credential:
            return os.environ.get(credential)
        endpoint = config.get("base_url", "")
        if config.get("mode") == "local" or any(h in endpoint for h in ("localhost", "127.0.0.1", "::1")):
            return "local"
        return None

    def reserve(self, count=1):
        if self.calls + count > self.config["max_calls"]:
            raise ModelFailure("call_budget_exhausted")
        self.calls += count

    def ask(self, role, prompt, *, item=None, fixture=None, schema=None):
        self.reserve()
        config = for_role(self.config, role)
        api_format = config.get("api_format") or "responses"
        disable_thinking = role == "extractor" or config.get("thinking") == "disabled"
        call = {"id": uuid4().hex, "role": role, "item": item, "input": prompt,
                "transport": "offline_fixture" if self.offline else api_format, "model": config["model"]}
        started = monotonic()
        try:
            if self.offline:
                text = fixture
            else:
                key = self._resolve_api_key(config)
                if not key:
                    raise ModelFailure("model_credential_unavailable", call["id"])
                client = OpenAI(api_key=key, base_url=config["base_url"], timeout=config["timeout_seconds"], max_retries=0)
                max_out = config["max_output_tokens"].get(role, 12288 if role == "extractor" else 2048)
                with client:
                    if api_format == "chat_completions":
                        text, meta = self._ask_chat_completions(client, config["model"], prompt, role, max_out, schema, disable_thinking)
                    else:
                        text, meta = self._ask_responses(client, config["model"], prompt, role, max_out, schema, disable_thinking)
                call.update(meta)
                if meta.get("response_status") != "completed":
                    raise ModelFailure(f"response_{meta.get('response_status')}:{meta.get('incomplete_reason')}", call["id"])
            if not text or not text.strip():
                raise ModelFailure("empty_response", call["id"])
            call["output"] = text
            return text, call["id"]
        except APIError as error:
            status = getattr(error, "status_code", None)
            code = getattr(error, "code", None)
            detail = code or (f"http_{status}" if status else None) or str(error).splitlines()[0][:120]
            call["http_status"] = status
            call["error"] = f"{type(error).__name__}:{detail}"
            raise ModelFailure(call["error"], call["id"]) from None
        except ModelFailure as error:
            call["error"] = error.code
            raise
        finally:
            call["elapsed_ms"] = round(1000 * (monotonic() - started))
            self.trace("model_call", **call)

    @staticmethod
    def _ask_responses(client, model, prompt, role, max_out, schema, disable_thinking):
        base = {"model": model, "input": prompt, "store": False, "max_output_tokens": max_out}
        text_fmt = None
        if schema is not None:
            text_fmt = {"format": {"type": "json_schema", "name": "context_selection", "strict": True, "schema": schema}}
        elif role == "extractor":
            text_fmt = {"format": {"type": "json_object"}}

        attempts = []
        if disable_thinking:
            attempts.append({"reasoning": {"effort": "none"}, **({"text": text_fmt} if text_fmt else {})})
            attempts.append({"extra_body": {"thinking": {"type": "disabled"}}, "reasoning": {"effort": "minimal"},
                             **({"text": text_fmt} if text_fmt else {})})
            attempts.append({"reasoning": {"effort": "low"}, **({"text": text_fmt} if text_fmt else {})})
        elif schema is not None:
            attempts.append({"reasoning": {"effort": "low"}, "text": text_fmt})
        attempts.append({**({"text": text_fmt} if text_fmt else {})})
        if text_fmt and text_fmt["format"]["type"] == "json_schema":
            attempts.append({"text": {"format": {"type": "json_object"}}})
        if text_fmt:
            attempts.append({})

        response = None
        last_err = None
        for opts in attempts:
            try:
                response = client.responses.create(**base, **opts)
                break
            except BadRequestError as err:
                last_err = err
                continue
        if response is None:
            raise last_err
        detail = response.incomplete_details or response.error
        reason = getattr(detail, "reason", None) or getattr(detail, "code", None)
        meta = {
            "response_id": response.id,
            "response_status": response.status,
            "response_model": response.model,
            "output_types": [part.type for part in response.output],
            "usage": response.usage.model_dump() if response.usage else None,
        }
        if reason:
            meta["incomplete_reason"] = reason
        return response.output_text, meta

    @staticmethod
    def _ask_chat_completions(client, model, prompt, role, max_out, schema, disable_thinking):
        base = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_out,
        }
        if role == "extractor":
            base["temperature"] = 0.1
        fmt_options = []
        if schema is not None:
            fmt_options.append({"response_format": {"type": "json_schema", "json_schema": {
                "name": "context_selection", "strict": True, "schema": schema,
            }}})
            fmt_options.append({"response_format": {"type": "json_object"}})
        elif role == "extractor":
            fmt_options.append({"response_format": {"type": "json_object"}})
        fmt_options.append({})

        thinking_options = ([{"extra_body": {"thinking": {"type": "disabled"}}}, {"reasoning_effort": "none"}, {}]
                            if disable_thinking else [{}])
        attempts = [{**t_opt, **f_opt} for f_opt in fmt_options for t_opt in thinking_options]

        response = None
        last_err = None
        for opts in attempts:
            try:
                response = client.chat.completions.create(**base, **opts)
                break
            except BadRequestError as err:
                last_err = err
                continue
        if response is None:
            raise last_err
        choice = response.choices[0] if response.choices else None
        text = choice.message.content if choice and choice.message else ""
        finish = choice.finish_reason if choice else "error"
        status = "incomplete" if finish == "length" else "completed" if finish in {"stop", "tool_calls", None} else finish
        meta = {
            "response_id": response.id,
            "response_status": status,
            "response_model": response.model,
            "output_types": ["message"],
            "usage": response.usage.model_dump() if response.usage else None,
        }
        if status != "completed":
            meta["incomplete_reason"] = finish or "unknown"
        return text, meta

    def pi(self, prompt, *, item=None, fixture=None, mode="review", challenge=None, schema=REVIEW_SCHEMA,
           tool=None):
        reserved = 2 if mode == "tools" or tool is not None else 1
        self.reserve(reserved)
        role = "intent" if mode == "intent" else "reviewer" if item else "pi_probe"
        call = {"id": uuid4().hex, "role": role, "item": item, "input": prompt}
        started = monotonic()
        config = for_role(self.config, role)
        call["model"] = config["model"]
        runtime_config = config
        if not self.offline:
            secret = self._resolve_api_key(config)
            if not secret:
                call["error"] = "model_credential_unavailable"
                raise ModelFailure("model_credential_unavailable", call["id"])
            runtime_config = {**config, "api_key": secret}
        request = {"config": runtime_config, "mode": mode, "prompt": prompt, "schema": schema, "challenge": challenge}
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
