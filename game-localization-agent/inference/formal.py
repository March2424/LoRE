"""Prepare and rerun an individual formal comparison using its frozen source snapshot."""
import argparse
from contextlib import closing
from hashlib import file_digest
import os
from pathlib import Path
import sqlite3
import tarfile

from evaluate.verify import read
from localization_agent.memory import MemoryStore
from localization_agent.selection.checkpoints import load_state, write_json
from localization_agent.selection.teacher import file_hash

FROZEN = Path("evaluate/frozen")
NAEV_GRAPH = Path("outputs/naev_full_2026-11-01/graph.sqlite")
MODELS = {"7b": "QWEN_7B", "8b": "QWEN_8B", "14b": "QWEN_14B"}


def unpack_graphs(output):
    with tarfile.open("data/graphs/source-graphs.tar.gz", "r:gz") as archive:
        archive.extractall(output, filter="data")


def prepare(output):
    output.mkdir(parents=True, exist_ok=False)
    assets = output / "assets"
    unpack_graphs(assets)
    general = read(FROZEN / "general/manifest.json")
    for spec in general["graphs"].values():
        parts = Path(spec["path"]).parts
        path = assets.joinpath(*parts[parts.index("outputs"):])
        with path.open("rb") as handle:
            if file_digest(handle, "sha256").hexdigest() != spec["sha256"]:
                raise ValueError(f"Frozen graph changed: {path.name}")
        spec["path"] = str(path.resolve())
    naev = assets / NAEV_GRAPH
    expected = read(FROZEN / "naev300/manifest.json")["graph_sha256"]
    if file_hash(naev) != expected:
        raise ValueError("Frozen Naev graph changed")
    write_json(output / "assets/general-manifest.json", general)
    print(f"Prepared seven source-only databases under {assets}.")


def duplicate_graph(path, output, project):
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as source:
        if source.execute("SELECT count(*) FROM runs").fetchone()[0]:
            raise ValueError("Source graph contains runtime memory")
        with closing(MemoryStore(output)) as store:
            source.backup(store.db)
            store.create_snapshot("teacher-knowledge", project)


def common_config():
    conf = read(Path("evaluate/experiment/naev-config.json"))
    conf["tokenizer"] = os.environ.get("BUDGET_TOKENIZER", "data/graphs/tokenizer.json")
    if env := os.environ.get("MEMORY_API_KEY_ENV"):
        conf["models"]["translation"]["api_key_env"] = env
    return conf


def job(row, project, condition, group, split):
    return dict(game=project, run_id=condition, source_id=row["source_id"], split=split,
                group_id=group, phase=row.get("phase", "target"), status="pending",
                trajectory_schema_version=2)


def run_naev(work, suite, key, run_folder):
    import run_naev_ablation as runner

    model, method = key.split("_", 1) if suite == "naev100" else ("14b", key)
    kind = "selector" if method == "base_selector" else method
    path = run_folder / kind
    runner.TRANSLATOR = Path(os.environ[MODELS[model]])
    manifest = read(FROZEN / suite / "manifest.json")
    if not (path / "state.json").exists():
        path.mkdir(parents=True, exist_ok=False)
        write_json(run_folder / "manifest.json", manifest)
        duplicate_graph(work / "assets" / NAEV_GRAPH, path / "naev.sqlite", "naev")
        sample = [r for r in manifest["samples"] if kind != "baseline" or r["phase"] == "target"]
        jobs = [job(r, "naev", f"{suite}:{key}", r["group_id"], "external_test") for r in sample]
        write_json(path / "manifest.json", {
            "translation_decoding": runner.TRANSLATION_POLICY,
            "translation_prompt_sha256": manifest["translator_prompt_sha256"],
        })
        write_json(path / "config.json", common_config())
        write_json(path / "state.json", {"status": "prepared", "jobs": jobs})
        write_json(path / "budget.json", {"max_cost": 3.0, "max_calls": 1800, "max_tokens": 6000000})
    runner.run(run_folder, kind, selector_variant="base" if method == "base_selector" else "dpo")
    return load_state(path)["jobs"]


def run_general(work, key, run_folder):
    import run_general_comet_eval as runner

    model, dataset, method = key.split("/")
    for name in (MODELS if method == "doc2doc" else (model,)):
        runner.MODELS[name] = os.environ[MODELS[name]]
    path = run_folder / model / dataset / method
    manifest = read(work / "assets/general-manifest.json")
    if not (path / "state.json").exists():
        sample = [r for r in manifest["samples"] if r["dataset"] == dataset]
        jobs = [job(r, r["project_id"], f"general:{key}:{r['unit_id']}", r["unit_id"], r["split"]) for r in sample]
        path.mkdir(parents=True, exist_ok=False)
        write_json(run_folder / "manifest.json", manifest)
        if method == "framework":
            conf = common_config()
            conf["memory"]["prompt_dir"] = "prompts/general_eval/memory"
            conf["allowed_source_ids"] = {}
            for row in sample:
                conf["allowed_source_ids"].setdefault(row["project_id"], []).append(row["source_id"])
            for project in conf["allowed_source_ids"]:
                source = Path(manifest["graphs"][project]["path"])
                duplicate_graph(source, path / (project + ".sqlite"), project)
            write_json(path / "config.json", conf)
            write_json(path / "budget.json", {"max_cost": 3.0, "max_calls": 1000, "max_tokens": 3000000})
        write_json(path / "state.json", {"status": "prepared", "jobs": jobs})
    if method == "framework":
        runner.framework(run_folder, model, dataset)
    else:
        runner.direct(run_folder, model, dataset, method)
    return load_state(path)["jobs"]


def run_delta(work, suite, key, path):
    from delta_runtime import LocalQwen
    from run_delta_eval import execute_rows, source_payload

    config = read(Path("configs/delta_eval.json"))
    if suite == "general":
        model, corpus, _ = key.split("/")
        manifest = read(work / "assets/general-manifest.json")
        samples = [r for r in manifest["samples"] if r["dataset"] == corpus]
        rows = [
            dict(r, phase="target", game=r["project_id"], group_id=r["unit_id"],
                 source=source_payload(manifest["graphs"][r["project_id"]]["path"],
                                       r["project_id"], r["source_id"])["source"],
                 metadata={}, mandatory_rules=[])
            for r in samples
        ]
    else:
        model = key.split("_", 1)[0]
        # The published 14B comparison uses the 100-target prefix of the 300-target run.
        source_suite = "naev300" if model == "14b" else suite
        rows = []
        for r in read(FROZEN / source_suite / "manifest.json")["samples"]:
            source = source_payload(work / "assets" / NAEV_GRAPH, "naev", r["source_id"])
            rows.append(dict(r, source=source["source"], source_version=source["source_version"],
                             source_lang="en", target_lang="zh", metadata=source["metadata"],
                             mandatory_rules=[], unit_id="naev_processing_schedule", dataset="naev",
                             game="naev", split="external_test"))
    if not (path / "state.json").exists():
        write_json(path / "state.json", {"status": "prepared", "jobs": [
            dict(source_id=r["source_id"], phase=r["phase"], status="pending") for r in rows]})
    if not execute_rows(path, rows, config, LocalQwen(os.environ[MODELS[model]], config)):
        raise ValueError("DelTA run stopped before completion")
    return load_state(path)["jobs"]


def run(work, suite, key):
    import run_naev_scomet as assets

    output = work / "runs" / suite / key
    if (output / "state.json").exists():
        raise ValueError("This comparison is already complete")
    assets.QWEN = Path(os.environ.get("QWEN_8B", assets.QWEN))
    assets.SFT = Path(os.environ.get("SELECTOR_SFT", assets.SFT))
    assets.DPO = Path(os.environ.get("SELECTOR_DPO", assets.DPO))
    assets.TOKENIZER = Path(os.environ.get("BUDGET_TOKENIZER", "data/graphs/tokenizer.json"))
    path = output / "checkpoints"
    if key.endswith(("_delta", "/delta")):
        jobs = run_delta(work, suite, key, path)
    elif suite == "general":
        jobs = run_general(work, key, path)
    else:
        jobs = run_naev(work, suite, key, path)
    samples = read(FROZEN / suite / "manifest.json")["samples"]
    if suite == "general":
        samples = [r for r in samples if r["dataset"] == key.split("/")[1]]
    by_id = {j["source_id"]: j for j in jobs if j["phase"] == "target"}
    targets = [by_id[r["source_id"]] for r in samples if r.get("phase", "target") == "target"]
    if any(j["status"] not in ("complete", "failed") for j in targets):
        raise ValueError("Comparison has unfinished targets")
    write_json(output / "state.json", {"status": "complete", "jobs": [
        {"source_id": j["source_id"], "phase": "target", "status": j["status"],
         "eval_reply": {"text": j.get("eval_reply", {}).get("text", "")},
         "selector_status": j.get("selector_status")} for j in targets
    ]})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("prepare").add_argument("--output", type=Path, required=True)
    launch = commands.add_parser("run")
    launch.add_argument("--work", type=Path, required=True)
    launch.add_argument("--suite", choices=("general", "naev100", "naev300"), required=True)
    launch.add_argument("--condition", required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.output)
    else:
        valid = read(FROZEN / args.suite / "provenance.json")["conditions"]
        if args.condition not in valid:
            parser.error(f"Choose one of {', '.join(valid)}")
        run(args.work, args.suite, args.condition)


if __name__ == "__main__":
    main()
