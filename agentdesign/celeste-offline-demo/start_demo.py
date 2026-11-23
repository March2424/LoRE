"""Open the packed Celeste demo in an installed LoRE workbench."""

import argparse
from pathlib import Path
import shutil
import sys
from tempfile import mkdtemp


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agentdesign", type=Path, default=Path.cwd())
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    app_root = args.agentdesign.resolve()
    if not (app_root / "run_api.py").is_file():
        parser.error("Run from LoRE's agentdesign directory or pass --agentdesign <directory>.")
    if not (app_root / "web" / "dist" / "app.js").is_file():
        parser.error("Build the LoRE frontend with npm --prefix web run build first.")

    sys.path.insert(0, str(app_root))
    import uvicorn
    from run_api import create_app

    runs = app_root / "runs" / "celeste-packaged"
    runs.mkdir(parents=True, exist_ok=True)
    workspace = Path(mkdtemp(prefix="run-", dir=runs))
    source = Path(__file__).with_name("workbench.sqlite")
    shutil.copyfile(source, workspace / source.name)
    print(f"Celeste demo copy: {workspace}", flush=True)
    uvicorn.run(create_app(workspace, offline=True, projects_only=True), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
