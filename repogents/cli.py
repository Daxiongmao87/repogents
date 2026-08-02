from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path
from typing import Sequence


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        prog="repogents",
        description="Local restart-safe repository agent orchestrator",
    )
    value.add_argument(
        "--version",
        action="version",
        version=version("repogents"),
    )
    value.add_argument(
        "--data-dir",
        type=Path,
        default=Path(
            os.environ.get("REPOGENTS_DATA_DIR", "~/.local/share/repogents")
        ).expanduser(),
        help="durable application data directory",
    )
    value.add_argument(
        "--model",
        default=os.environ.get("REPOGENTS_MODEL"),
        help="optional bootstrap mini-SWE model selector",
    )
    value.add_argument(
        "--model-base-url",
        default=os.environ.get("REPOGENTS_MODEL_BASE_URL"),
        help="optional bootstrap OpenAI-compatible model endpoint",
    )
    subcommands = value.add_subparsers(dest="command", required=True)

    serve = subcommands.add_parser(
        "serve", help="run scheduler and local web interface"
    )
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--poll-interval", type=float, default=10.0)

    subcommands.add_parser("tick", help="perform one orchestration poll")

    onboard = subcommands.add_parser("onboard", help="onboard one GitHub repository")
    onboard.add_argument("repository")
    onboard.add_argument(
        "--inputs-json", default="{}", help="repository-specific inputs JSON object"
    )

    subcommands.add_parser("state", help="print current durable state as JSON")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parser().parse_args(argv)

    from .app import build_runtime
    from .interface import LocalInterfaceServer

    token = _github_token()
    poll_interval = arguments.poll_interval if arguments.command == "serve" else 10.0
    runtime = build_runtime(
        arguments.data_dir,
        github_token=token,
        model=arguments.model,
        model_base_url=arguments.model_base_url,
        poll_interval=poll_interval,
    )
    if arguments.command == "tick":
        runtime.orchestrator.tick()
        if runtime.orchestrator.last_errors:
            for error in runtime.orchestrator.last_errors:
                print(error, file=sys.stderr)
            return 1
        return 0
    if arguments.command == "onboard":
        inputs = json.loads(arguments.inputs_json)
        if not isinstance(inputs, dict):
            raise SystemExit("--inputs-json must decode to an object")
        repository_id = runtime.actions.add_repository(arguments.repository, inputs)
        print(repository_id)
        return 0
    if arguments.command == "state":
        print(json.dumps(runtime.actions.state(), sort_keys=True, indent=2))
        return 0
    server = LocalInterfaceServer(
        actions=runtime.actions,
        host=arguments.host,
        port=arguments.port,
    )
    runtime.scheduler.start()
    host, port = server.address
    print(f"Repogents listening on http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        runtime.scheduler.stop()
        server.close()
    return 0


def _github_token() -> str | None:
    configured = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if configured:
        return configured
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    token = result.stdout.strip()
    return token if result.returncode == 0 and token else None


if __name__ == "__main__":
    raise SystemExit(main())
