"""Point a frozen training recipe at local, user-provided data and model paths."""
import argparse
from pathlib import Path

import yaml


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("sft", "dpo"))
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    recipe = yaml.safe_load((Path("train/recipes") / (args.stage + ".yaml")).read_text())
    recipe.update(model_name_or_path=str(args.model.resolve(strict=True)),
                  dataset_dir=str(args.dataset.resolve(strict=True)),
                  output_dir=str(args.run_dir.resolve()))
    for file in ("dataset_info.json", f"selector_{args.stage}_train.json"):
        if not (args.dataset / file).is_file():
            raise FileNotFoundError(args.dataset / file)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as destination:
        yaml.safe_dump(recipe, destination, sort_keys=False, allow_unicode=True)


if __name__ == "__main__":
    main()
