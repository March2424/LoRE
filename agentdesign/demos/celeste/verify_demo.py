"""Exercise LoRE's real localhost API with labelled, deterministic Celeste fixtures."""

import argparse
from collections import Counter
import csv
from io import StringIO
import json
from pathlib import Path
import sys
from threading import Thread
from time import monotonic, sleep
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import httpx
import uvicorn

from build_dataset import COLUMNS, CSV_NAME, DEFAULT_OUTPUT, PORTRAIT

AGENTDESIGN = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(AGENTDESIGN))

from run_api import create_app
from run_p0 import DEFAULT_DB
from workbench.models import Responses
from workbench.workflow import hard_problems

GRAPH_LABELS = {
    "POSTCARD_AREA_2": (
        (("Strawberries", "item", "Strawberries"),),
        (("DEFINES", None, "Strawberries", "Strawberries", ""),),
    ),
    "CH2_BADELINE_INTRO": (
        (("Badeline", "character", "[BADELINE left normal]"),
         ("Madeline", "character", "{+MADELINE}")),
        (("SPOKEN_BY", None, "Badeline", "[BADELINE left normal]", ""),
         ("PART_OF", "Badeline", "Madeline", "I'm {+PART_OF_YOU}.",
          "Badeline addresses Madeline and identifies herself as part of her.")),
    ),
    "CH3_MEMO": (
        (("Celestial Resort Hotel", "location", "Celestial Resort Hotel"),
         ("Mr. Oshiro", "character", r"Mr\. Oshiro")),
        (("MENTIONS", None, "Celestial Resort Hotel", "Celestial Resort Hotel will close indefinitely", ""),
         ("MENTIONS", None, "Mr. Oshiro", r"-Mr\. Oshiro", "")),
    ),
    "CH6_THEO_SAY_TRUST": (
        (("Theo", "character", "[THEO left normal]"), ("my sister", "character", "my sister")),
        (("SPOKEN_BY", None, "Theo", "[THEO left normal]", ""),
         ("MENTIONS", None, "my sister", "You remind me of my sister.", "")),
    ),
}

PASS = {"verdict": "pass", "reason": "演示固定回执：译文符合该条的预期用途。"}
CASES = {
    "MENU_BEGIN": ("攀登", None, [PASS], "committed"),
    "AREA_CHAPTER": ("第 x 章", "第 {x} 章", [
        {"verdict": "repair", "reason": "演示故障注入：章节号占位符 {x} 被删除。"}, PASS,
    ], "committed"),
    "MOUNTAIN_COMPLETE_NORMAL_FULLCLEAR": ("清除", None, [
        {"verdict": "needs_human", "reason": "演示固定回执：Clear 有多义，请核对章节完成界面含义。"},
    ], "needs_human"),
    "CH0_INTRO": ("就是这样，玛德琳。\n只需呼吸。\n为什么你这么紧张？", None, [
        {"verdict": "needs_human", "reason": "演示故障注入：玩家名变量被写成了固定人名。"},
    ], "needs_human"),
}


def fixture_responses(resources):
    """Replace only the demo extractor's fixture. LoRE still validates and writes it."""
    original = Responses.ask

    def ask(model, role, prompt, **kwargs):
        if role == "extractor" and model.offline:
            rows_json, catalog_json = prompt.rsplit("\nRows: ", 1)[1].split("\nExisting catalog: ", 1)
            catalog = {entry["name"]: entry["id"] for entry in json.loads(catalog_json)}
            entities, relations = {}, []
            for row in json.loads(rows_json):
                row_id = row["id"]
                if row_id not in resources:
                    continue
                key = resources[row_id]
                labels, edges = GRAPH_LABELS[key]
                def evidence(quote):
                    return [{"id": row_id, "field": "source", "quote": quote}]

                for name, kind, quote in labels:
                    entities[name] = {"key": name, "name": name, "entity_type": kind,
                                      "reuse_id": catalog.get(name), "evidence": evidence(quote)}
                for relation, left, right, quote, reason in edges:
                    relations.append({"key": f"{key}:{relation}:{right}",
                                      "source": f"entity:{left}" if left else f"text:{row_id}",
                                      "target": f"entity:{right}", "relation": relation, "conditions": {},
                                      "evidence": evidence(quote),
                                      **({"basis": "inferred", "reason": reason} if reason else {})})
            kwargs["fixture"] = json.dumps({"entities": list(entities.values()), "relations": relations})
        return original(model, role, prompt, **kwargs)

    return ask


def wait_for_job(client, job_id, expected):
    deadline = monotonic() + 90
    while monotonic() < deadline:
        job = client.get(f"/jobs/{job_id}").raise_for_status().json()
        if job["status"] not in {"queued", "running"}:
            assert job["status"] == expected, (job["status"], job["reason"], job.get("items"))
            return job
        sleep(.25)
    raise TimeoutError(job_id)


def approval(item, target, reason):
    return {"expected_version": item["expected_version"], "expected_draft_hash": item["draft_hash"],
            "target": target, "reason": reason}


def audit_references(rows, references):
    no_memory = SimpleNamespace(memory=None)
    empty_pack = SimpleNamespace(selected=[])
    checked = {row["id"]: hard_problems(no_memory, SimpleNamespace(source=row["source_text"]),
                                        references[row["id"]], empty_pack) for row in rows}
    portrait_drift = [row["id"] for row in rows
                      if PORTRAIT.findall(row["source_text"]) != PORTRAIT.findall(references[row["id"]])]
    return {"total": len(rows), "workbench_gate_pass": sum(not problems for problems in checked.values()),
            "format_findings": dict(Counter(problem for problems in checked.values() for problem in problems)),
            "portrait_sequence_differs": len(portrait_drift), "portrait_examples": portrait_drift[:6],
            "reference_failures": {key: reasons for key, reasons in checked.items() if reasons}}


def verify(client, app, dataset, workspace, graph_resources):
    raw = (dataset / CSV_NAME).read_bytes()
    reader = csv.DictReader(StringIO(raw.decode("utf-8"), newline=""))
    rows = list(reader)
    references = {row["id"]: row["reference"] for row in map(json.loads,
                  (dataset / "references.jsonl").read_text(encoding="utf-8").splitlines())}
    ids = {row["id"] for row in rows}
    assert tuple(reader.fieldnames) == COLUMNS and len(ids) == len(rows) >= 500
    assert ids == references.keys() and all(references.values())
    prefix = uuid4().hex

    def post(path, action, **kwargs):
        return client.post(path, headers={"Idempotency-Key": f"{prefix}-{action}"}, **kwargs).raise_for_status().json()

    def get(path, **kwargs):
        return client.get(path, **kwargs).raise_for_status().json()

    project = post("/projects", "create", json={"name": "Celeste · EN → 简体中文 · 离线演示",
                                               "source_lang": "en", "target_lang": "zh"})
    project_id = project["project_id"]
    project_path = f"/projects/{project_id}"
    upload = {"filename": CSV_NAME}
    preview = post(project_path + "/imports", "preview", params={**upload, "preview": "true"}, content=raw)
    assert preview["changes"]["added"] == len(rows) and get(project_path)["resources"] == 0
    imported = post(project_path + "/imports", "import", params=upload, content=raw)
    assert get(project_path)["resources"] == len(rows) and get(project_path)["approved_memory"] == 0
    resource_keys = {}
    for key in (*GRAPH_LABELS, *CASES):
        found = get(project_path + "/resources", params={"query": key})["resources"]
        resource_keys[key] = next(row["occurrence_id"] for row in found if row["key"] == key)
    graph_resources.update({resource_keys[key]: key for key in GRAPH_LABELS})

    structural = post(project_path + "/graph/build", "structure", json={"extract": False})
    structure = wait_for_job(client, structural["job_id"], "completed")["results"][0]
    assert (structure["nodes"], structure["edges"]) == (len(rows), 0)
    graph_settings = {"batch_size": 24, "max_calls": 200, "max_input_tokens": 16000, "max_output_tokens": 2048}
    built = post(project_path + "/graph/build", "graph", json=graph_settings)
    extracted_job = wait_for_job(client, built["job_id"], "completed")
    extracted = extracted_job["results"][0]
    assert extracted["nodes"] == len(rows) + 7 and extracted["edges"] == 7, extracted
    for query, relation in (("Badeline", "PART_OF"), ("Strawberries", "DEFINES"),
                            ("Celestial Resort Hotel", "MENTIONS"), ("my sister", "MENTIONS")):
        graph = get(project_path + "/graph", params={"query": query})
        assert any(edge["relation"] == relation and edge["valid"] and edge["evidence"][0]["location"]["row"] > 1
                   for edge in graph["edges"]), graph["edges"]

    for key, (draft, repaired, verdicts, _) in CASES.items():
        app.state.workbench.fixture[resource_keys[key]] = {
            "selector": '{"selected_ids":[],"rationale":"Celeste offline fixture"}',
            "translator": draft, "repairer": repaired, "reviewer": verdicts,
        }
    started = post("/jobs", "translate", json={"selection": {"project": project_id},
                   "targets": [resource_keys[key] for key in CASES], "intents": ["translate", "export"], "max_calls": 24})
    job_id = started["job_id"]
    job = wait_for_job(client, job_id, "awaiting_review")
    by_id = {row["occurrence_id"]: row for row in job["items"]}
    assert get(project_path + "/files/review", params={"file": CSV_NAME})["total"] == 2
    outcomes = {}
    for key, (draft, _, _, expected) in CASES.items():
        item = by_id[resource_keys[key]]
        assert item["status"] == expected, item
        evidence = get(item["evidence_url"])
        if expected == "committed":
            assert item["target"] == references[key], key
        outcomes[key] = {"initial_status": item["status"], "reason": item["reason"],
                         "direct_draft_exact_match": draft == references[key],
                         "initial_workflow_exact_match": item.get("target") == references[key],
                         "review_verdicts": [review.get("verdict") for review in evidence["reviews"]]}
    assert outcomes["AREA_CHAPTER"]["review_verdicts"] == ["repair", "pass"], outcomes

    key = "CH0_INTRO"
    intro = by_id[resource_keys[key]]
    approve_path = f"/jobs/{job_id}/items/{intro['step']}/{intro['occurrence_id']}/approve"
    wrong = client.post(approve_path, headers={"Idempotency-Key": f"{prefix}-reject"},
                        json=approval(intro, CASES[key][0], "演示：尝试提交丢失变量的译文"))
    assert wrong.status_code == 422 and "Placeholder" in wrong.json()["reason"], wrong.text
    post(approve_path, "approve", json=approval(intro, references[key], "演示：恢复玩家名变量并保留三行版式"))
    assert get(project_path + "/files/review", params={"file": CSV_NAME})["total"] == 1
    assert get(project_path)["approved_memory"] == 3
    export_path = project_path + f"/imports/{imported['import_id']}/export"
    report = get(export_path + "/report")
    exported = client.get(export_path).raise_for_status().content
    parsed = {row["id"]: row["translation"] for row in csv.DictReader(StringIO(exported.decode("utf-8-sig"), newline=""))}
    assert len(parsed) == len(rows) and {key for key, text in parsed.items() if text} == {
        "MENU_BEGIN", "AREA_CHAPTER", "CH0_INTRO"}, report
    assert all(parsed[key] == references[key] for key in parsed if parsed[key])
    result = {"mode": "offline_fixture", "project_id": project_id, "job_id": job_id,
              "import_id": imported["import_id"], "records": len(rows), "graph_settings": graph_settings,
              "structure": structure, "fixture_graph": extracted, "fixture_extraction_batches": built["batches"],
              "graph_budget": extracted_job["budget"], "cases": outcomes,
              "direct_draft_exact_matches": sum(case["direct_draft_exact_match"] for case in outcomes.values()),
              "initial_workflow_exact_matches": sum(case["initial_workflow_exact_match"] for case in outcomes.values()),
              "expected_initial_status_matches": sum(by_id[resource_keys[key]]["status"] == case[3] for key, case in CASES.items()),
              "invalid_approval_http": wrong.status_code, "invalid_approval_reason": wrong.json()["reason"],
              "approved_export_rows": sum(bool(text) for text in parsed.values()), "pending_for_live_demo": 1,
              "reference_format_audit": audit_references(rows, references)}
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "acceptance.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (workspace / "approved-export.csv").write_bytes(exported)
    print(f"Celeste API acceptance: {workspace / 'acceptance.json'}", flush=True)
    print(f"Open {client.base_url} | project={project_id} | job={job_id}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--serve", action="store_true", help="Keep the checked workbench open after the run")
    args = parser.parse_args()
    workspace = args.workspace or DEFAULT_OUTPUT.parent / f"api-{uuid4().hex[:8]}"
    app = create_app(workspace, DEFAULT_DB, offline=True, projects_only=True)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="warning"))
    graph_resources = {}
    thread = Thread(target=server.run, name="celeste-demo-http")
    with patch.object(Responses, "ask", fixture_responses(graph_resources)):
        thread.start()
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{args.port}", trust_env=False) as client:
                deadline = monotonic() + 15
                while not server.started and thread.is_alive() and monotonic() < deadline:
                    sleep(.1)
                if not server.started:
                    raise RuntimeError(f"LoRE did not start on port {args.port}")
                assert client.get("/workspace").raise_for_status().json()["offline"]
                verify(client, app, args.dataset, workspace, graph_resources)
            if args.serve:
                thread.join()
        except KeyboardInterrupt:
            pass
        finally:
            server.should_exit = True
            thread.join()


if __name__ == "__main__":
    main()
