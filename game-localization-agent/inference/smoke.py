"""Run the submission's two-resource workflow from CSV import to offline scoring."""
import argparse
import csv
import json
from pathlib import Path
import subprocess
import sys

from inference.fixture import make_model
from inference.translate import run
from localization_agent.models.chat_completions import CallBudget, ChatCompletionsModel


def read_lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--backend", choices=("fixture", "deepseek"), default="fixture")
    args = parser.parse_args()
    out = args.output
    config_path = Path("configs/submission.smoke.json")
    config = json.loads(config_path.read_text())
    fixture = args.backend == "fixture"
    out.mkdir(parents=True, exist_ok=False)

    def execute(*args):
        subprocess.run([sys.executable, *args], check=True)

    execute("scripts/import_localization_csv.py", "data/demo/localization.csv",
            "--dataset-id", "submission-demo", "--output", str(out / "import"))
    imported = read_lines(out / "import/source.jsonl")
    with Path("data/demo/benchmark.csv").open(newline="", encoding="utf-8") as handle:
        resources = list(csv.DictReader(handle))
    if [row["source_text"] for row in imported] != [row["source_text"] for row in resources]:
        raise ValueError("Imported source rows differ from the source-only graph fixture")
    model_args = ("--model-factory", "inference.fixture:make_model", "--batch-size", "2") if fixture else (
        "--config", str(config_path))
    graph = out / "graph.sqlite"
    graph_report = out / "graph.json"
    execute("scripts/run_benchmark_agent.py", "data/demo/benchmark.csv", "--project", "submission-demo",
            "--db", str(graph), "--output", str(graph_report), *model_args)
    extracted = json.loads(graph_report.read_text())
    entity_count = sum(len(b["entities"]) for b in extracted["builds"])
    relation_count = sum(len(b["edges"]) for b in extracted["builds"])
    if not entity_count or not relation_count:
        raise ValueError("Graph fixture did not produce admitted entities and relations")

    budget = None if fixture else CallBudget(**config["budget"])
    chat = make_model() if fixture else ChatCompletionsModel(**config["model"], budget=budget)
    summary = run(graph, out / "translation", "submission-demo", "en", "zh", chat,
                  selector=chat, budget=budget)
    translated = read_lines(out / "translation/translations.jsonl")
    references = {row["record_id"]: row["reference_text"]
                  for row in read_lines(out / "import/references.jsonl")}
    exact = sum(row["hyp"] == references[source["record_id"]]
                for row, source in zip(translated, imported))
    metrics = {"backend": args.backend, "sources": len(resources), "entities": entity_count,
               "relations": relation_count, "translation": summary, "exact_reference_matches": exact}
    if tuple(row["source_id"] for row in translated) != ("weapon", "line"):
        raise ValueError("Missing or misaligned translation")
    if summary["statuses"] != {"recorded_unreviewed": 2} or not summary["visible_memory"].get("term"):
        raise ValueError(f"Translation or memory write-back failed: {metrics}")
    if not any(key.startswith("memory:") for key in translated[1]["candidate_ids"]):
        raise ValueError("Second resource did not retrieve the first resource's memory")
    if fixture and exact != 2:
        raise ValueError("Offline fixture output differs from its offline reference")
    (out / "smoke.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(metrics, ensure_ascii=False))


if __name__ == "__main__":
    main()
