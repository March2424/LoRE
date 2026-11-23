"""Build or launch the portable Shattered Pixel Dungeon LoRE workspace."""

import argparse
from collections import Counter
from contextlib import closing
import csv
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
import shutil
import sqlite3
import sys
from tempfile import TemporaryDirectory, mkdtemp
from time import monotonic, sleep
from unittest.mock import patch
from uuid import uuid4

HERE = Path(__file__).resolve().parent
AGENTDESIGN = HERE.parents[1]
RUNS = AGENTDESIGN / "runs" / "shattered-demo"
DATA = AGENTDESIGN.parent / "game-localization-agent" / "data" / "shattered" / "v4.0.2"
SNAPSHOT = HERE / "workbench.sqlite"
FILENAME = "shattered-demo-en-zh.csv"
FIELDS = ("id", "source_text", "text_type", "context")
REVISION = "57a4e06a4caf162446d1c28caa7983f0493fecf0"
QUOTAS = {"character_name": 14, "dialogue": 65, "narrative": 90, "item_name": 100,
          "item_description": 120, "skill_description": 55, "ui": 129, "system_message": 25}

SUCCESS = "scenes.welcomescene.continue"
REPAIR = "actors.mobs.npcs.imp.cya"
AMBIGUOUS = "windows.wndkeybindings.back"
WRONG_APPROVAL = "actors.mobs.npcs.wandmaker.reminder_dust"
CASES = (SUCCESS, REPAIR, AMBIGUOUS, WRONG_APPROVAL)
GRAPH = {
    "items.quest.corpsedust.name": (("corpse dust", "item", "corpse dust", "DEFINES"),),
    WRONG_APPROVAL: (("corpse dust", "item", "corpse dust", "MENTIONS"),),
    "actors.mobs.npcs.wandmaker.name": (("old wandmaker", "character", "old wandmaker", "DEFINES"),),
    "actors.mobs.npcs.blacksmith.name": (("troll blacksmith", "character", "troll blacksmith", "DEFINES"),),
    "actors.mobs.npcs.shopkeeper.talk_caves": (
        ("troll blacksmith", "character", "Troll blacksmith", "MENTIONS"),
        ("hammer", "item", "owes me money for that hammer I sold him", "MENTIONS"),
    ),
    "actors.mobs.npcs.impshopkeeper.desc": (
        ("imp", "character", "The imp", "MENTIONS"),
        ("inner halls", "location", "inner halls", "MENTIONS"),
    ),
    "actors.mobs.npcs.imp.quest_in_progress": (("vault entrance", "location", "vault entrance", "MENTIONS"),),
}

sys.path.insert(0, str(AGENTDESIGN))

from run_api import create_app


def json_lines(path):
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle]


def sample():
    references = {row["record_id"]: row["reference_text"] for row in json_lines(DATA / "references.jsonl")}
    provenance = {row["record_id"]: row for row in json_lines(DATA / "background" / "provenance.jsonl")}
    by_key = {}
    with (DATA / "shattered_entity_input.csv").open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            if references.get(row["id"]):
                evidence = provenance[row["id"]]
                if evidence["revision"] != REVISION:
                    raise ValueError("Shattered data does not match the pinned revision")
                by_key.setdefault(evidence["localization_key"], row)
    required = {*CASES, *GRAPH}
    chosen = set(required)
    for kind, quota in QUOTAS.items():
        remaining = quota - sum(by_key[key]["text_type"] == kind for key in required)
        eligible = [key for key, row in by_key.items() if row["text_type"] == kind and key not in required]
        eligible.sort(key=lambda key: sha256(key.encode()).digest())
        assert 0 <= remaining <= len(eligible), (kind, remaining)
        chosen.update(eligible[:remaining])
    rows = {key: row for key, row in by_key.items() if key in chosen}
    assert len(rows) == 600
    stream = StringIO(newline="")
    writer = csv.DictWriter(stream, FIELDS, lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(rows.values())
    return rows, references, stream.getvalue().encode("utf-8")


def graph_fixture(keys):
    from workbench.models import Responses

    original = Responses.ask

    def ask(model, role, prompt, **kwargs):
        if role == "extractor" and model.offline:
            lines, catalog_json = prompt.rsplit("\nRows: ", 1)[1].split("\nExisting catalog: ", 1)
            catalog = {item["name"]: item["id"] for item in json.loads(catalog_json)}
            entities, edges = {}, []
            for row in json.loads(lines):
                for name, kind, quote, relation in GRAPH.get(keys.get(row["id"]), ()):
                    evidence = [{"id": row["id"], "field": "source", "quote": quote}]
                    entities.setdefault(name, {"key": name, "name": name, "entity_type": kind,
                                               "reuse_id": catalog.get(name), "evidence": evidence})
                    edges.append({"key": f"{row['id']}:{name}", "source": f"text:{row['id']}",
                                  "target": f"entity:{name}", "relation": relation, "conditions": {},
                                  "evidence": evidence})
            kwargs["fixture"] = json.dumps({"entities": list(entities.values()), "relations": edges})
        return original(model, role, prompt, **kwargs)

    return patch.object(Responses, "ask", ask)


def get(client, path, **kwargs):
    return client.get(path, **kwargs).raise_for_status().json()


def post(client, path, *, expected=None, **kwargs):
    response = client.post(path, headers={"Idempotency-Key": uuid4().hex}, **kwargs)
    if expected is not None:
        assert response.status_code == expected, response.text
    else:
        response.raise_for_status()
    return response.json()


def wait(client, job, status):
    deadline = monotonic() + 90
    while monotonic() < deadline:
        detail = get(client, f"/jobs/{job}")
        if detail["status"] not in {"queued", "running"}:
            assert detail["status"] == status, detail
            return detail
        sleep(.1)
    raise TimeoutError(job)


def approve(client, job, item, target, reason, *, expected=None):
    path = f"/jobs/{job}/items/{item['step']}/{item['occurrence_id']}/approve"
    return post(client, path, expected=expected, json={"target": target, "reason": reason,
                "expected_version": item["expected_version"], "expected_draft_hash": item["draft_hash"]})


def seed(client, workbench):
    rows, references, csv_bytes = sample()
    project = post(client, "/projects", json={"name": "Shattered Pixel Dungeon | EN → zh-Hans | Offline demo",
                                             "source_lang": "en", "target_lang": "zh"})["project_id"]
    base = f"/projects/{project}"
    imported = post(client, base + "/imports", params={"filename": FILENAME}, content=csv_bytes)
    assert get(client, base)["resources"] == 600 and get(client, base)["approved_memory"] == 0
    ids = {}
    for key in {*CASES, *GRAPH}:
        source_id = rows[key]["id"]
        resources = get(client, base + "/resources", params={"query": source_id})["resources"]
        ids[key] = next(row["occurrence_id"] for row in resources if row["key"] == source_id)

    with graph_fixture({ids[key]: key for key in GRAPH}):
        graph_job = post(client, base + "/graph/build", json={"batch_size": 24, "max_calls": 100,
                                                               "max_input_tokens": 16000, "max_output_tokens": 2048})
        graph = wait(client, graph_job["job_id"], "completed")["results"][0]
    assert (graph["nodes"], graph["edges"]) == (607, 9), graph
    hammer = get(client, base + "/graph", params={"query": "hammer"})
    assert hammer["edges"][0]["evidence"][0]["location"]["path"] == FILENAME

    targets = {key: references[rows[key]["id"]] for key in CASES}
    pass_review = {"verdict": "pass", "reason": "Offline scripted pass for the demonstration."}
    drafts = {SUCCESS: targets[SUCCESS], REPAIR: targets[REPAIR].replace("%s", "", 1),
              AMBIGUOUS: "后背", WRONG_APPROVAL: targets[WRONG_APPROVAL].replace("%s", "", 1)}
    reviews = {SUCCESS: [pass_review], REPAIR: [{"verdict": "repair", "reason": "Restore the missing %s."}, pass_review],
               AMBIGUOUS: [{"verdict": "needs_human", "reason": "Back is ambiguous without the UI role. Confirm the navigation meaning."}],
               WRONG_APPROVAL: [{"verdict": "needs_human", "reason": "The quest reminder has lost the player-name token %s."}]}
    for key in CASES:
        workbench.fixture[ids[key]] = {"selector": '{"selected_ids":[],"rationale":"Offline demonstration"}',
                                      "translator": drafts[key], "repairer": targets[key], "reviewer": reviews[key]}
    job = post(client, "/jobs", json={"selection": {"project": project}, "targets": [ids[key] for key in CASES],
                                      "intents": ["translate", "export"], "max_calls": 24})["job_id"]
    detail = wait(client, job, "awaiting_review")
    items = {item["occurrence_id"]: item for item in detail["items"]}
    statuses = {key: items[ids[key]]["status"] for key in CASES}
    assert list(statuses.values()) == ["committed", "committed", "needs_human", "needs_human"], statuses
    assert items[ids[REPAIR]]["target"] == targets[REPAIR]
    item = items[ids[WRONG_APPROVAL]]
    result = approve(client, job, item, drafts[WRONG_APPROVAL], "Demonstrate the format gate", expected=422)
    assert "Placeholder" in result["reason"], result
    approve(client, job, item, targets[WRONG_APPROVAL], "Restore the player-name token")
    snapshot = {"game": "Shattered Pixel Dungeon", "upstream_revision": REVISION, "license": "GPL-3.0-or-later",
                "source_lang": "en", "target_lang": "zh-Hans", "mode": "offline_fixture",
                "records": len(rows), "official_references": len(rows),
                "types": dict(sorted(Counter(row["text_type"] for row in rows.values()).items())),
                "project_id": project, "translation_job_id": job, "import_id": imported["import_id"],
                "graph_nodes": graph["nodes"], "graph_edges": graph["edges"], "graph_batches": graph_job["batches"],
                "initial_status_by_localization_key": statuses, "invalid_approval_http": 422,
                "approved_exports": 3, "awaiting_review": 1, "source_csv_sha256": sha256(csv_bytes).hexdigest()}
    return snapshot, targets[AMBIGUOUS]


def verify_reopened(client, snapshot, back_target):
    base = f"/projects/{snapshot['project_id']}"
    assert get(client, base)["resources"] == snapshot["records"]
    queue = get(client, base + "/files/review", params={"file": FILENAME})
    pending, = queue["items"]
    assert pending["source"] == "Back"
    export = base + f"/imports/{snapshot['import_id']}/export"
    assert get(client, export + "/report")["translated"] == 3
    job = snapshot["translation_job_id"]
    item = next(item for item in get(client, f"/jobs/{job}")["items"]
                if item["occurrence_id"] == pending["occurrence_id"])
    approve(client, job, item, back_target, "Confirm this is a back-navigation button")
    wait(client, job, "completed")
    assert get(client, export + "/report")["translated"] == 4
    content = client.get(export).raise_for_status().content.decode("utf-8-sig")
    rows = list(csv.DictReader(StringIO(content, newline="")))
    assert len(rows) == 600 and sum(bool(row["translation"]) for row in rows) == 4


def build():
    from fastapi.testclient import TestClient

    RUNS.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=RUNS) as temp:
        temp = Path(temp)
        original = temp / "seed"
        app = create_app(original, offline=True, projects_only=True)
        with TestClient(app) as client:
            snapshot, back_target = seed(client, app.state.workbench)
        packed = temp / "workbench.sqlite"
        with closing(sqlite3.connect(original / "workbench.sqlite")) as database, \
                closing(sqlite3.connect(packed)) as copy:
            database.backup(copy)
            copy.execute("PRAGMA journal_mode=DELETE")
            assert copy.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        restored = temp / "restored"
        restored.mkdir()
        shutil.copyfile(packed, restored / "workbench.sqlite")
        with TestClient(create_app(restored, offline=True, projects_only=True)) as client:
            verify_reopened(client, snapshot, back_target)
        snapshot["snapshot_sha256"] = sha256(packed.read_bytes()).hexdigest()
        snapshot["reopened_export_after_approval"] = 4
        packed.replace(SNAPSHOT)
        (HERE / "snapshot.json").write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")


def serve(port, workspace):
    import uvicorn

    if workspace is None:
        RUNS.mkdir(parents=True, exist_ok=True)
        workspace = Path(mkdtemp(prefix="run-", dir=RUNS))
    else:
        workspace.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(SNAPSHOT, workspace / "workbench.sqlite")
    print(f"Using writable demo copy at {workspace}", flush=True)
    uvicorn.run(create_app(workspace, offline=True, projects_only=True), host="127.0.0.1", port=port)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("serve", "build"), nargs="?", default="serve")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--workspace", type=Path, help="New writable directory to serve from")
    args = parser.parse_args()
    if args.mode == "build":
        build()
    else:
        serve(args.port, args.workspace)
