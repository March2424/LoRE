#!/usr/bin/env python3
"""Run the fixed Naev P0 or P1 batch, or verify the Responses gateway."""

import argparse
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import file_digest, sha256
from importlib.metadata import version
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
from time import monotonic

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
RESEARCH = HERE.parent / "game-localization-agent"
sys.path.insert(0, str(RESEARCH / "src"))

from localization_agent.memory import MemoryService, MemoryStore
from localization_agent.retrieval.graph import GraphStore
from localization_agent.retrieval.service import GraphConfig, GraphRetriever
from localization_agent.schemas.memory import Evidence

from workbench.evidence import ScopedReviewerRetriever, read_provenance
from workbench.models import ModelFailure, Responses, parse_review
from workbench.workflow import TranslationAgent, budget_units

DEFAULT_DB = REPO / "outputs/naev_full_2026-11-01/graph.sqlite"
CONFIG = HERE / "configs/p0.json"
MANIFEST = HERE / "configs/naev-p0.json"
P1_MANIFEST = HERE / "configs/naev-p1.json"
NAEV_PROVENANCE = RESEARCH / "data/naev/snapshot-123fdc1/background/provenance.jsonl"
PI = HERE / "pi/reviewer.mjs"


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def digest(path):
    with path.open("rb") as handle:
        return file_digest(handle, "sha256").hexdigest()


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def versions():
    return {
        "python": sys.version.split()[0],
        "node": subprocess.check_output(["node", "--version"], text=True).strip(),
        "langgraph": version("langgraph"),
        "openai": version("openai"),
        "pi_core": load(HERE / "pi/package.json")["dependencies"]["@earendil-works/pi-agent-core"],
        "pi_ai": load(HERE / "pi/package.json")["dependencies"]["@earendil-works/pi-ai"],
        "npm_lock_sha256": digest(HERE / "pi/package-lock.json"),
    }


def probe(models):
    challenge = secrets.token_hex(8)
    results = {}
    try:
        plain, call = models.ask("probe", "Reply with exactly OK.")
        results["responses"] = {"ok": plain.strip() == "OK", "call_id": call}
    except ModelFailure as error:
        results["responses"] = {"ok": False, "error": error.code, "call_id": error.call_id}
    try:
        receipt, call = models.pi('Return verdict pass with reason "probe".')
        results["structured_sse"] = {
            "ok": parse_review(receipt["text"])["verdict"] == "pass"
            and any(entry["events"].get("response.completed") for entry in receipt["calls"]),
            "call_id": call,
        }
    except (ModelFailure, ValueError) as error:
        results["structured_sse"] = {"ok": False, "error": getattr(error, "code", "review_structure"),
                                     "call_id": getattr(error, "call_id", None)}
    try:
        receipt, call = models.pi(
            "Call p0_echo exactly once. After it returns, respond with exactly the returned token.",
            mode="tools", challenge=challenge, schema=None,
        )
        events = [event for event in receipt["events"] if event["type"] == "tool_execution_end"]
        results["tool_continuation"] = {
            "ok": len(receipt["calls"]) == 2 and len(events) == 1 and not events[0]["error"]
            and events[0]["name"] == "p0_echo" and receipt["text"].strip() == challenge
            and "function_call_output" in receipt["calls"][1]["input_types"],
            "call_id": call,
        }
    except ModelFailure as error:
        results["tool_continuation"] = {"ok": False, "error": error.code, "call_id": error.call_id}
    return results


def run_batch(args, output, run_id, models, trace):
    manifest_path = P1_MANIFEST if args.p1 else MANIFEST
    manifest = load(manifest_path)
    fixtures_name = "offline-fixtures-p1.json" if args.p1 else "offline-fixtures.json"
    fixture = load(HERE / "configs" / fixtures_name) if args.offline else {}
    lookup_limits = manifest.get("review_lookup")
    provenance = read_provenance(NAEV_PROVENANCE, {
        source_id for case in manifest["cases"] for source_id in case["allowed_evidence_ids"]
    }) if args.p1 else {}
    prompts = {path.stem: path.read_text(encoding="utf-8") for path in (HERE / "prompts").glob("*.txt")}
    db = args.db.resolve()
    config = GraphConfig(**models.config["retrieval"])
    copy = output / "run.sqlite"
    source_fingerprint = digest(db)
    if source_fingerprint != manifest["source_db_sha256"]:
        raise ValueError("Naev source graph does not match the frozen snapshot")
    with closing(sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)) as source, \
            closing(sqlite3.connect(copy)) as target:
        source.backup(target)
    trace(
        "inputs", manifest=manifest, manifest_sha256=digest(manifest_path), source_db_sha256=source_fingerprint,
        snapshot=manifest["graph_snapshot"], retrieval=asdict(config), budget_counter=budget_units.name,
        prompts={role: sha256(text.encode()).hexdigest() for role, text in prompts.items()},
        workflow_sha256=digest(HERE / "workbench/workflow.py"), pi_bridge_sha256=digest(PI),
        source_review_sha256=digest(HERE / "workbench/evidence.py") if args.p1 else None,
        source_provenance_sha256=digest(NAEV_PROVENANCE) if args.p1 else None,
        research_sources_sha256={
            path: digest(RESEARCH / "src/localization_agent" / path)
            for path in ("retrieval/service.py", "retrieval/game_retrieval.py", "memory/store.py",
                         "schemas/graph.py", "schemas/memory.py")
        },
    )
    with closing(MemoryStore(copy)) as store:
        project, snapshot = manifest["project"], manifest["graph_snapshot"]
        graph = GraphStore(store)
        if graph.snapshot(snapshot).project_id != project:
            raise ValueError("Source graph snapshot has an unexpected project")
        sources = [(case, store.source(project, Evidence(case["id"], case["version"], "")))
                   for case in manifest["cases"]]
        knowledge = f"{run_id}:knowledge"
        store.create_snapshot(knowledge, project)
        store.start_run(run_id, project, None, "en", "zh", knowledge, mode="game_resources")
        trace("snapshot", source_snapshot=snapshot, knowledge_snapshot=knowledge,
              view=asdict(store.view(run_id)))
        memory = MemoryService(store)
        results = []
        for case, occurrence in sources:
            allowed = frozenset(case["allowed_evidence_ids"])
            if args.p1:
                initial = frozenset(case.get("initial_evidence_ids", allowed))
                if not initial <= allowed or occurrence.occurrence_id not in initial:
                    raise ValueError("Initial evidence must stay inside the authorized source scope")
                retriever = ScopedReviewerRetriever(graph, memory, config, token_counter=budget_units,
                                                    initial_ids=initial, review_ids=allowed, provenance=provenance)
            else:
                retriever = GraphRetriever(graph, memory, config, token_counter=budget_units, allowed_source_ids=allowed)
            agent = TranslationAgent(retriever, models, prompts, trace)
            result = agent.run(occurrence, run_id, snapshot, case, fixture.get(case["case"], {}),
                               lookup_limits=lookup_limits)
            trace("outcome", **result)
            results.append(result)
        return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--offline", action="store_true", help="Run fixed fixtures through LangGraph and local Pi")
    mode.add_argument("--probe", action="store_true", help="Test Responses, SSE JSON schema and native Pi tool continuation")
    parser.add_argument("--p1", action="store_true", help="Enable the scoped Reviewer tool and run the P1 manifest")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="Read-only Naev source graph")
    parser.add_argument("--output", type=Path, help="New directory for artifacts")
    args = parser.parse_args()
    if args.p1 and args.probe:
        parser.error("--p1 runs the P1 batch and cannot be combined with --probe")
    if not args.offline and not os.environ.get("DEEPSEEK_API_KEY"):
        parser.error("DEEPSEEK_API_KEY is required in live mode")
    if not args.probe and not args.db.is_file():
        parser.error(f"Naev source graph is missing: {args.db}")
    if not (HERE / "pi/node_modules/@earendil-works/pi-agent-core").exists():
        parser.error("Install Node dependencies with npm --prefix pi ci")
    phase = "p1" if args.p1 else "p0"
    run_id = phase + "-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(3)
    output = args.output or HERE / "runs" / run_id
    output.mkdir(parents=True, exist_ok=False)
    started = monotonic()
    config = load(CONFIG)
    mode = "offline" if args.offline else "probe" if args.probe else "live"
    with (output / "trace.jsonl").open("x", encoding="utf-8", buffering=1) as log:
        def trace(event, **data):
            log.write(json.dumps({"event": event, **data}, ensure_ascii=False) + "\n")

        trace("run", run_id=run_id, phase=phase, mode=mode, config=config, dependencies=versions())
        models = Responses(config, trace, PI, offline=args.offline)
        if args.probe:
            results = probe(models)
            ok = all(item["ok"] for item in results.values())
        else:
            results = run_batch(args, output, run_id, models, trace)
            ok = all(item["status"] == item["expected_status"] and
                     item["first_review"] == item["expected_first_review"] and
                     item.get("lookup_status") == item.get("expected_lookup_status") for item in results)
        summary = {"run_id": run_id, "mode": mode, "matches_expectations": ok,
                   "model_calls": models.calls, "elapsed_ms": round(1000 * (monotonic() - started)),
                   "results": results}
        write_json(output / "summary.json", summary)
    print(json.dumps({"output": str(output.resolve()), **summary}, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
