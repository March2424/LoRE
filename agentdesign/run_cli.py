#!/usr/bin/env python3
"""Launch the LoRE amber REPL CLI for the localization agent workbench."""

import argparse
import os
from pathlib import Path
import subprocess
import sys
from time import monotonic, sleep

from cli.client import LoreClient
from cli.repl import LoreREPL
from cli.state import SessionState, load_cli_config
from cli.theme import fg
from cli.workflows import ConsoleIO

HERE = Path(__file__).resolve().parent


class ManagedServer:
    """Optional child run_api.py process managed by the CLI lifecycle."""

    def __init__(self, args, state: SessionState):
        self.args = args
        self.state = state
        self.process: subprocess.Popen | None = None

    def start(self) -> LoreClient:
        self.stop()
        cmd = [sys.executable, str(HERE / "run_api.py"), "--port", str(self.args.port)]
        if self.args.offline:
            cmd.append("--offline")
        if self.args.projects_only:
            cmd.append("--projects-only")
        if self.args.data_dir:
            cmd.extend(["--data-dir", str(self.args.data_dir)])
        if self.args.db:
            cmd.extend(["--db", str(self.args.db)])
        env = {**os.environ, **self.state.session_env}
        self.process = subprocess.Popen(cmd, cwd=str(HERE), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        url = f"http://127.0.0.1:{self.args.port}"
        self.state.api_url = url
        self.state.spawned = True
        client = LoreClient(url)
        deadline = monotonic() + 20.0
        while monotonic() < deadline:
            if self.process.poll() is not None:
                err = self.process.stderr.read().decode("utf-8", errors="replace") if self.process.stderr else ""
                raise RuntimeError(f"托管服务启动失败: {err.strip()}")
            if client.ping():
                return client
            sleep(0.2)
        raise RuntimeError("等待托管 run_api.py 就绪超时。")

    def stop(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LoRE · game location agent amber REPL")
    parser.add_argument("--url", help="Workbench API base URL (default: http://127.0.0.1:8766)")
    parser.add_argument("--local", action="store_true", help="Run workbench in-process via ASGI without binding a TCP port")
    parser.add_argument("--spawn", action="store_true", help="Spawn and manage a local run_api.py worker")
    parser.add_argument("--offline", action="store_true", help="Pass --offline to local/spawned workbench")
    parser.add_argument("--projects-only", action="store_true", help="Pass --projects-only to local/spawned workbench")
    parser.add_argument("--data-dir", type=Path, help="Workspace data directory when running --local or --spawn")
    parser.add_argument("--db", type=Path, help="Frozen Naev graph SQLite path when running --local or --spawn")
    parser.add_argument("--port", type=int, default=8766, help="Port for --spawn (default: 8766)")
    parser.add_argument("--no-banner", action="store_true", help="Skip the startup portrait banner")
    return parser


def main(argv: list[str] | None = None):
    args = build_parser().parse_args(argv)
    saved = load_cli_config()
    url = (args.url or saved.get("api_url") or f"http://127.0.0.1:{args.port}").rstrip("/")
    state = SessionState(
        api_url="in-process://workbench" if args.local else url,
        project_id=saved.get("project_id", "naev"),
        project_name=saved.get("project_name", "Naev (default)"),
        target_lang=saved.get("target_lang", "zh"),
        spawn_args={"offline": args.offline, "projects_only": args.projects_only},
    )
    if args.local:
        from fastapi.testclient import TestClient
        from run_api import DEFAULT_DB, create_app

        prefix = "projects" if args.projects_only else "p2"
        mode = "offline" if args.offline else "service"
        data_dir = args.data_dir or HERE / "runs" / f"{prefix}-{mode}"
        app = create_app(data_dir, args.db or DEFAULT_DB, offline=args.offline, projects_only=args.projects_only)
        with TestClient(app) as tc:
            client = LoreClient(client=tc)
            io = ConsoleIO()
            repl = LoreREPL(client, state, io=io, show_banner=not args.no_banner)
            repl.run()
        return

    server = ManagedServer(args, state) if args.spawn else None
    io = ConsoleIO()
    client = server.start() if server else LoreClient(url)

    def restart_spawn() -> LoreClient:
        nonlocal client
        if server is None:
            return client
        client.close()
        client = server.start()
        return client

    try:
        if not client.ping():
            io.emit(fg("coral", f"提示：暂未连上 {state.api_url}，可先启动 run_api.py，或用 --spawn 自动托管，或在 REPL 中输入 /api 切换。"))
        repl = LoreREPL(
            client, state, io=io,
            restart_spawn_fn=restart_spawn if server else None,
            show_banner=not args.no_banner,
        )
        repl.run()
    finally:
        client.close()
        if server:
            server.stop()


if __name__ == "__main__":
    main()
