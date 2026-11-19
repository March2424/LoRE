"""Live DeepSeek smoke test. Three Flare rows and one extraction request."""

from contextlib import closing
import csv
import json
import os
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep

from fastapi.testclient import TestClient

from run_api import HERE, create_app

ROWS = {"flr_c55a24b0c6699b1f4c8c", "flr_cec6a23bbef71042f372", "flr_72548a36bc705ec253fe"}


def main():
    if not os.environ.get("DEEPSEEK_API_KEY"):
        raise SystemExit("DEEPSEEK_API_KEY is required")
    with (HERE / "flare_entity_input_v0.2.csv").open(newline="") as original:
        reader = csv.DictReader(original)
        sample = [row for row in reader if row["id"] in ROWS]
        text = StringIO(newline="")
        writer = csv.DictWriter(text, fieldnames=reader.fieldnames)
        writer.writeheader()
        writer.writerows(sample)
    assert len(sample) == 3

    with TemporaryDirectory(prefix="flare-live-graph-") as work, TestClient(create_app(Path(work), projects_only=True)) as client:
        def post(url, key, **kwargs):
            response = client.post(url, headers={"Idempotency-Key": key}, **kwargs)
            if not response.is_success:
                raise RuntimeError(response.text)
            return response.json()

        project = post("/projects", "new", json={"name": "Flare DeepSeek smoke", "source_lang": "en", "target_lang": "zh"})
        route = "/projects/" + project["project_id"]
        imported = post(route + "/imports?filename=flare_sample.csv", "import", content=text.getvalue().encode())
        queued = post(route + "/graph/build", "build", json={"batch_size": 3, "max_calls": 1,
                                                              "max_input_tokens": 3800, "max_output_tokens": 1800})
        deadline = monotonic() + 100
        while monotonic() < deadline:
            job = client.get("/jobs/" + queued["job_id"]).json()
            if job["status"] in {"completed", "failed"}:
                break
            sleep(0.2)

        with closing(client.app.state.workbench.open()) as repo:
            calls = [json.loads(row[0]) for row in repo.db.execute("""
                SELECT data FROM p2_events WHERE job=? AND kind='model_call'
                AND json_extract(data,'$.lane')='graph:0' ORDER BY seq
                """, (queued["job_id"],))]
        graph = client.get(route + "/graph?limit=20").json()
        build = job["graph_build"]
        memory = client.get(route + "/memory").json()["approved"]
        result = {"source_ids": sorted(ROWS), "rows": imported["rows"], "status": job["status"], "reason": job["reason"],
                  "calls": job["budget"]["reserved_or_spent"],
                  "model": calls[0].get("model") if calls else client.app.state.workbench.config["model"],
                  "usage": calls[0].get("usage") if calls else None, "model_error": calls[0].get("error") if calls else None,
                  "http_status": calls[0].get("http_status") if calls else None, "batches": build["completed"],
                  "approved_memory": len(memory), "graph": graph}
        out = HERE / "runs" / "p6-deepseek-smoke" / "acceptance.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({key: result[key] for key in ("rows", "status", "reason", "calls", "model",
                                                      "usage", "model_error", "http_status", "approved_memory")}, ensure_ascii=False))
        if (result["status"] != "completed" or result["calls"] != 1 or memory
                or not build["completed"][0]["entities"] or not build["completed"][0]["relations"]):
            raise SystemExit(1)


if __name__ == "__main__":
    main()
