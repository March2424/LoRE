"""Verify alignment, score summaries, and frozen evaluator inputs without model dependencies."""
import json
from collections import Counter
from hashlib import sha256
from pathlib import Path


SUITES = {"general": "general", "naev100": "naev100", "ablation300": "naev300"}


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def physical(text):
    return text.replace("\\", "\\\\").replace("\r", "\\r").replace("\n", "\\n")


def check(condition, detail):
    if not condition:
        raise ValueError(detail)


def load_test_data():
    datasets = {}
    for name, spec in read(Path("data/test_manifest.json"))["datasets"].items():
        folder = Path("data") / name / spec["direction"]
        for file, checksum in spec["files_sha256"].items():
            check(digest(folder / file) == checksum, f"Unexpected test data hash: {folder / file}")
        rows = read(folder / "records.json")
        check(len(rows) == spec["records"], f"Test record count changed: {name}")
        for doc, count in spec["documents"].items():
            source, target = spec["direction"].split("-")
            group = [row for row in rows if row["document_id"] == doc]
            check([row["line"] for row in group] == list(range(1, count + 1)),
                  f"Test document order differs: {name}/{doc}")
            for language, key in ((source, "src"), (target, "ref")):
                expected = "".join(physical(row[key]) + "\n" for row in group)
                check((folder / f"{language}.{doc}").read_text(encoding="utf-8") == expected,
                      f"Test language file differs: {name}/{language}.{doc}")
        datasets[name] = rows
    return datasets


def summary_key(row):
    experiment = row["experiment"]
    if experiment == "general":
        return experiment, f'{row["model"]}/{row["dataset"]}/{row["method"]}'
    if experiment == "naev100":
        return experiment, f'{row["model"]}_{row["method"]}'
    return experiment, row["method"]


def verify():
    test = load_test_data()
    result_dir = Path("results")
    conditions = read(result_dir / "manifest.json")["conditions"]
    summary = {summary_key(row): row for row in read(Path("evaluate/summary.json"))["rows"]}
    check(len(conditions) == len(summary) == 38, "Expected 38 result conditions")
    counts = Counter()
    for spec in conditions:
        key = (spec["experiment"], spec["condition"])
        dataset = Path(spec["path"]).parts[0]
        folder = result_dir / spec["path"]
        rows = read(folder / "records.json")
        expected = [(row["source_id"], row["document_id"], row["line"]) for row in test[dataset]]
        actual = [(row["source_id"], row["document_id"], row["line"]) for row in rows]
        check(actual == expected and len(rows) == spec["n"], f"Record order differs: {key}")
        target = spec["direction"].split("-")[1]
        for doc, count in spec["documents"].items():
            values = [physical(row["hyp"]) for row in rows if row["document_id"] == doc]
            check(len(values) == count, f"Hypothesis count differs: {key}/{doc}")
            check((folder / f"{target}.{doc}").read_text(encoding="utf-8") == "".join(x + "\n" for x in values),
                  f"Exported hypothesis text differs: {key}/{doc}")
        state = Path("evaluate/frozen") / SUITES[key[0]] / key[1] / "state.json"
        frozen = [(job["source_id"], job["eval_reply"]["text"], job["status"])
                  for job in read(state)["jobs"]]
        check(frozen == [(row["source_id"], row["hyp"], row["status"]) for row in rows],
              f"Frozen scorer inputs differ: {key}")
        scores = read(folder / "scores.json")
        observed = {"scomet": scores["scomet"]["scomet"], "failed": scores["scomet"]["failed"]}
        if key[0] == "general":
            observed["dcomet"] = scores["dcomet"]["dcomet"]
        else:
            observed.update(bleu4=scores["bleu"]["bleu4"],
                            ltcr1=scores["ltcr_common"]["ltcr_1"],
                            ltcr1f=scores["ltcr_common"]["ltcr_1f"])
        check(all(summary[key][name] == value for name, value in observed.items()),
              f"Summary does not match score files: {key}")
        counts[key[0]] += len(rows)
    check(sum(counts.values()) == 4788, "Expected 4,788 hypothesis records")
    print(f"Verified {len(conditions)} conditions, {sum(counts.values())} hypotheses, "
          f"{sum(map(len, test.values()))} test pairs, and matching score summaries.")


if __name__ == "__main__":
    verify()
