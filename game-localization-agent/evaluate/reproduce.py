"""Prepare an isolated scoring workspace and run the original metric protocols."""
import argparse
from hashlib import file_digest
import json
import math
from pathlib import Path
import shutil
from statistics import mean

from evaluate.verify import load_test_data, read, verify
from localization_agent.selection.checkpoints import load_state, write_json
from run_naev_scomet import COMET_SHA256


def write_jsonl(path, rows):
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def prepare(output, generated=None):
    verify()
    data = load_test_data()
    shutil.copytree("evaluate/frozen", output)
    if generated is not None:
        for suite in ("general", "naev100", "naev300"):
            folder = output / suite
            for condition in read(folder / "provenance.json")["conditions"]:
                target = folder / condition / "state.json"
                original = read(target)["jobs"]
                source = generated / "runs" / suite / condition / "state.json"
                jobs = read(source)["jobs"]
                if [row["source_id"] for row in jobs] != [row["source_id"] for row in original]:
                    raise ValueError(f"New run has different occurrence IDs: {suite}/{condition}")
                if any(row["status"] not in ("complete", "failed") for row in jobs):
                    raise ValueError(f"New run has unfinished items: {suite}/{condition}")
                shutil.copyfile(source, target)
    write_json(output / "general-test.json",
               {"news": data["news_commentary"], "guofeng": data["guofeng"]})
    naev = data["naev_ablation_300"]
    write_jsonl(output / "naev-sources.jsonl",
                (dict(game="naev", split="external_test", id=r["source_id"], source_text=r["src"]) for r in naev))
    write_jsonl(output / "naev-references.jsonl",
                (dict(game="naev", split="external_test", record_id=r["source_id"],
                      reference_text=r["ref"]) for r in naev))
    print(f"Prepared isolated scoring input at {output}.")


def scomet(folder, conditions, data, checkpoint, gpus):
    from localization_agent.selection.preferences import CometLengthError, LocalSComet

    if checkpoint is None:
        raise ValueError("Pass --checkpoint with the frozen WMT22 COMET-DA checkpoint")
    with checkpoint.open("rb") as handle:
        if file_digest(handle, "sha256").hexdigest() != COMET_SHA256:
            raise ValueError("WMT22 COMET-DA checkpoint differs from the frozen run")
    scorer = LocalSComet(checkpoint, batch_size=8, gpus=gpus)
    aggregates = {}
    for condition in conditions:
        jobs = load_state(folder / condition)["jobs"]
        selected = data[condition.split("/")[1]] if folder.name == "general" else data
        by_id = {row["source_id"]: row for row in selected}
        if [job["source_id"] for job in jobs] != list(by_id):
            raise ValueError(f"Scoring input IDs differ for {condition}")
        items = []
        oversize = 0
        for job in jobs:
            row = by_id[job["source_id"]]
            item = {"src": row["src"], "ref": row["ref"], "mt": job["eval_reply"]["text"]}
            try:
                scorer.validate_length(item)
            except CometLengthError:
                item["mt"] = ""
                scorer.validate_length(item)
                oversize += 1
            items.append(item)
        scores = scorer.score(items)
        if len(scores) != len(items) or not all(math.isfinite(x) for x in scores):
            raise ValueError(f"Invalid scorer output for {condition}")
        write_json(folder / condition / "scomet_scores.json", [
            {"source_id": job["source_id"], "status": job["status"], "scomet": float(score)}
            for job, score in zip(jobs, scores)
        ])
        aggregates[condition] = {
            "scomet": mean(scores), "n": len(jobs), "oversize_as_empty": oversize,
            "failed": sum(job["status"] == "failed" for job in jobs),
            "empty": sum(not item["mt"].strip() for item in items),
        }
    return {"conditions": aggregates, "scorer": scorer.identity}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("prepare")
    setup.add_argument("--output", type=Path, required=True)
    setup.add_argument("--generated", type=Path, help="Optional formal inference workspace with all conditions")
    metric = commands.add_parser("metric")
    metric.add_argument("--work", type=Path, required=True)
    metric.add_argument("--suite", choices=("general", "naev100", "naev300"), required=True)
    metric.add_argument("--metric", choices=("scomet", "bleu", "ltcr"), required=True)
    metric.add_argument("--checkpoint", type=Path)
    metric.add_argument("--gpus", type=int, choices=(0, 1), default=0)
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.output, args.generated)
        return
    if args.suite == "general" and args.metric != "scomet":
        parser.error("The general-domain metrics are sCOMET and Doc-COMET")
    folder = args.work / args.suite
    conditions = tuple(read(folder / "provenance.json")["conditions"])
    count = 100 if args.suite == "naev100" else 300
    if args.metric == "ltcr":
        import report_ltcr_common
        import score_naev_ltcr

        output = f"ltcr_{count}_pooled"
        score_naev_ltcr.SOURCES = args.work / "naev-sources.jsonl"
        score_naev_ltcr.main(folder, conditions=conditions, expected_count=count, result_directory=output)
        report_ltcr_common.main(folder / output, conditions=conditions)
    elif args.metric == "bleu":
        import score_naev_ablation_bleu as bleu

        bleu.REFERENCES = args.work / "naev-references.jsonl"
        write_json(folder / "bleu4_result.json", bleu.score(folder, conditions, count))
    else:
        data = load_test_data()
        data = ({"news": data["news_commentary"], "guofeng": data["guofeng"]} if args.suite == "general"
                else data["naev" if count == 100 else "naev_ablation_300"])
        write_json(folder / "scomet_result.json", scomet(folder, conditions, data, args.checkpoint, args.gpus))


if __name__ == "__main__":
    main()
