from __future__ import annotations

import argparse
import json
from importlib.metadata import PackageNotFoundError, version
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Sequence

from .app import build_runtime
from .interface import LocalInterfaceServer


def _checkout_project_version() -> str | None:
    """Return the version for the source tree containing this module, if any."""
    package_directory = Path(__file__).resolve().parent
    pyproject = package_directory.parent / "pyproject.toml"
    if not pyproject.is_file():
        return None

    source = pyproject.read_text(encoding="utf-8")
    project_header = re.search(r"(?m)^\s*\[project\]\s*(?:#.*)?$", source)
    if project_header is None:
        return None
    next_table = re.search(
        r"(?m)^\s*\[{1,2}[^]\r\n]+\]{1,2}\s*(?:#.*)?$",
        source[project_header.end():],
    )
    project_section = source[project_header.end():]
    if next_table is not None:
        project_section = project_section[:next_table.start()]

    def project_value(key: str) -> str | None:
        match = re.search(
            rf"^\s*{re.escape(key)}\s*=\s*['\"]([^'\"]+)['\"]\s*(?:#.*)?$",
            project_section,
            re.MULTILINE,
        )
        return None if match is None else match.group(1)

    # A pyproject merely adjacent to site-packages is not evidence that this
    # package came from it. Repogents project metadata identifies a source tree.
    if project_value("name") != "repogents":
        return None
    checkout_version = project_value("version")
    if checkout_version is None:
        raise RuntimeError(f"{pyproject} has no [project] version")
    return checkout_version


def _repogents_version() -> str:
    checkout_version = _checkout_project_version()
    if checkout_version is not None:
        return checkout_version
    try:
        return version("repogents")
    except PackageNotFoundError as error:
        raise RuntimeError(
            "Repogents version metadata is unavailable for this installation"
        ) from error


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        prog="repogents",
        description="Local restart-safe repository agent orchestrator",
    )
    value.add_argument(
        "--version",
        action="version",
        version=_repogents_version(),
    )
    value.add_argument(
        "--data-dir",
        type=Path,
        default=os.environ.get("REPOGENTS_DATA_DIR", "~/.local/share/repogents"),
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
    raw_arguments = list(sys.argv[1:] if argv is None else argv)
    if raw_arguments == ["--version"]:
        print(_repogents_version())
        return 0
    arguments = parser().parse_args(raw_arguments)
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
