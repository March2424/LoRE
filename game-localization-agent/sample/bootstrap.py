"""Materialize the frozen game graphs and build an index for trajectory collection."""
import argparse
from pathlib import Path
import shutil
import tarfile

from evaluate.verify import read
from localization_agent.selection.checkpoints import write_json
from localization_agent.selection.teacher import file_hash

GRAPH_PATHS = {
    "flare": "outputs/flare_full_2026-10-31/graph.sqlite",
    "shattered": "outputs/shattered_full_2026-11-01/graph.sqlite",
    "naev": "outputs/naev_full_2026-11-01/graph.sqlite",
}
INDEX = Path("outputs/server_stage_inputs_2026-11-03")


def games():
    with tarfile.open("data/graphs/source-graphs.tar.gz") as archive:
        for path in GRAPH_PATHS.values():
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                continue
            with archive.extractfile(path) as source, target.open("xb") as destination:
                shutil.copyfileobj(source, destination)
    tokenizer = Path("artifacts/retrieval_tokenizer/tokenizer.json")
    tokenizer.parent.mkdir(parents=True, exist_ok=True)
    if not tokenizer.exists():
        shutil.copyfile("data/graphs/tokenizer.json", tokenizer)


def index(public_root, teacher_plan):
    public = read(public_root / "graph_alignment.json")["projects"]
    public_graphs = [dict(project_id=key, source_db=spec["source_db"], sha256=spec["sha256"])
                     for key, spec in public.items()]
    game_graphs = [dict(project_id=key, source_db=str(Path(path).resolve()), sha256=file_hash(path))
                   for key, path in GRAPH_PATHS.items()]
    write_json(INDEX / "stage_inputs.json", {
        "public_source_graphs": public_graphs, "game_source_graphs": game_graphs,
    })
    target = INDEX / "teacher_plan_server/native_bindings.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(teacher_plan / "native_bindings.json", target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public-root", type=Path)
    parser.add_argument("--teacher-plan", type=Path)
    args = parser.parse_args()
    if bool(args.public_root) != bool(args.teacher_plan):
        parser.error("--public-root and --teacher-plan must be supplied together")
    games()
    if args.public_root:
        index(args.public_root, args.teacher_plan)
    print("Source assets are ready.")


if __name__ == "__main__":
    main()
