from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from repogents import cli


EXPECTED_COMMANDS_JSON = (
    b'{"commands":["onboard","serve","state","tick"],"program":"repogents"}\n'
)
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class CommandsJsonTests(unittest.TestCase):
    def test_commands_json_is_exact_without_credentials_or_resolvable_home(self) -> None:
        environment = os.environ.copy()
        environment.pop("GITHUB_TOKEN", None)
        environment.pop("GH_TOKEN", None)
        environment.update(
            {
                "REPOGENTS_DATA_DIR": "~repogents-user-that-does-not-exist/data",
                "PYTHONPATH": str(REPOSITORY_ROOT),
            }
        )

        result = subprocess.run(
            [sys.executable, "-m", "repogents.cli", "--commands-json"],
            cwd=REPOSITORY_ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, EXPECTED_COMMANDS_JSON)
        self.assertEqual(result.stderr, b"")

    def test_commands_json_after_negative_value_is_exact_in_subprocess(self) -> None:
        environment = os.environ.copy()
        environment.pop("GITHUB_TOKEN", None)
        environment.pop("GH_TOKEN", None)
        environment.update(
            {
                "REPOGENTS_DATA_DIR": "~repogents-user-that-does-not-exist/data",
                "PYTHONPATH": str(REPOSITORY_ROOT),
            }
        )

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "repogents.cli",
                "--model",
                "-1",
                "--commands-json",
            ],
            cwd=REPOSITORY_ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, EXPECTED_COMMANDS_JSON)
        self.assertEqual(result.stderr, b"")

    def test_commands_json_after_lone_hyphen_values_is_exact_in_subprocess(self) -> None:
        environment = os.environ.copy()
        environment.pop("GITHUB_TOKEN", None)
        environment.pop("GH_TOKEN", None)
        environment.update(
            {
                "REPOGENTS_DATA_DIR": "~repogents-user-that-does-not-exist/data",
                "PYTHONPATH": str(REPOSITORY_ROOT),
            }
        )

        for option in ("--data-dir", "--model", "--model-base-url"):
            with self.subTest(option=option):
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "repogents.cli",
                        option,
                        "-",
                        "--commands-json",
                    ],
                    cwd=REPOSITORY_ROOT,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )

                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout, EXPECTED_COMMANDS_JSON)
                self.assertEqual(result.stderr, b"")

    def test_commands_json_after_lone_hyphen_values_bypasses_initialization(self) -> None:
        for option in ("--data-dir", "--model", "--model-base-url"):
            with self.subTest(option=option):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with (
                    patch("repogents.cli.parser") as parser,
                    patch("repogents.cli._github_token") as github_token,
                    patch("repogents.cli.build_runtime") as build_runtime,
                    patch("repogents.cli.LocalInterfaceServer") as server,
                    contextlib.redirect_stdout(stdout),
                    contextlib.redirect_stderr(stderr),
                ):
                    status = cli.main([option, "-", "--commands-json"])

                self.assertEqual(status, 0)
                self.assertEqual(stdout.getvalue().encode(), EXPECTED_COMMANDS_JSON)
                self.assertEqual(stderr.getvalue(), "")
                parser.assert_not_called()
                github_token.assert_not_called()
                build_runtime.assert_not_called()
                server.assert_not_called()

    def test_commands_json_after_negative_global_option_value_is_exact(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch("repogents.cli.parser") as parser,
            patch("repogents.cli._github_token") as github_token,
            patch("repogents.cli.build_runtime") as build_runtime,
            patch("repogents.cli.LocalInterfaceServer") as server,
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            status = cli.main(["--model", "-1", "--commands-json"])

        self.assertEqual(status, 0)
        self.assertEqual(stdout.getvalue().encode(), EXPECTED_COMMANDS_JSON)
        self.assertEqual(stderr.getvalue(), "")
        parser.assert_not_called()
        github_token.assert_not_called()
        build_runtime.assert_not_called()
        server.assert_not_called()

    def test_dash_prefixed_non_values_retain_argparse_errors(self) -> None:
        cases = (
            ("actual option", ["--model", "--data-dir", "somewhere", "--commands-json"]),
            ("unknown option", ["--unknown", "--commands-json"]),
            ("ambiguous option", ["--mod", "value", "--commands-json"]),
            ("missing value", ["--model", "--commands-json"]),
        )
        for label, arguments in cases:
            with self.subTest(label=label):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with (
                    patch("repogents.cli._github_token") as github_token,
                    patch("repogents.cli.build_runtime") as build_runtime,
                    patch("repogents.cli.LocalInterfaceServer") as server,
                    contextlib.redirect_stdout(stdout),
                    contextlib.redirect_stderr(stderr),
                    self.assertRaises(SystemExit) as raised,
                ):
                    cli.main(arguments)

                self.assertEqual(raised.exception.code, 2)
                self.assertEqual(stdout.getvalue(), "")
                self.assertNotIn(cli.COMMANDS_JSON, stderr.getvalue())
                self.assertTrue(stderr.getvalue().startswith("usage: repogents"))
                github_token.assert_not_called()
                build_runtime.assert_not_called()
                server.assert_not_called()

    def test_commands_json_returns_before_parsing_or_runtime_initialization(self) -> None:
        stdout = io.StringIO()
        with (
            patch("repogents.cli.parser") as parser,
            patch("repogents.cli._github_token") as github_token,
            patch("repogents.cli.build_runtime") as build_runtime,
            patch("repogents.cli.LocalInterfaceServer") as server,
            contextlib.redirect_stdout(stdout),
        ):
            status = cli.main(["--commands-json"])

        self.assertEqual(status, 0)
        self.assertEqual(stdout.getvalue().encode(), EXPECTED_COMMANDS_JSON)
        parser.assert_not_called()
        github_token.assert_not_called()
        build_runtime.assert_not_called()
        server.assert_not_called()

    def test_commands_json_after_double_dash_is_onboard_repository(self) -> None:
        runtime = Mock()
        runtime.actions.add_repository.return_value = 42
        stdout = io.StringIO()
        with (
            patch("repogents.cli._github_token", return_value=None),
            patch("repogents.cli.build_runtime", return_value=runtime) as build_runtime,
            contextlib.redirect_stdout(stdout),
        ):
            status = cli.main(["onboard", "--", "--commands-json"])

        self.assertEqual(status, 0)
        self.assertEqual(stdout.getvalue(), "42\n")
        self.assertNotEqual(stdout.getvalue().encode(), EXPECTED_COMMANDS_JSON)
        runtime.actions.add_repository.assert_called_once_with("--commands-json", {})
        build_runtime.assert_called_once()

    def test_commands_json_as_global_option_value_is_not_discovery(self) -> None:
        runtime = Mock()
        runtime.actions.state.return_value = {}
        stdout = io.StringIO()
        with (
            patch("repogents.cli._github_token", return_value=None),
            patch("repogents.cli.build_runtime", return_value=runtime) as build_runtime,
            contextlib.redirect_stdout(stdout),
        ):
            status = cli.main(["--model=--commands-json", "state"])

        self.assertEqual(status, 0)
        self.assertEqual(stdout.getvalue(), "{}\n")
        self.assertNotEqual(stdout.getvalue().encode(), EXPECTED_COMMANDS_JSON)
        self.assertEqual(build_runtime.call_args.kwargs["model"], "--commands-json")
        runtime.actions.state.assert_called_once_with()

    def test_commands_json_in_subcommand_option_value_position_is_not_discovery(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch("repogents.cli._github_token") as github_token,
            patch("repogents.cli.build_runtime") as build_runtime,
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main(
                [
                    "onboard",
                    "owner/repository",
                    "--inputs-json",
                    "--commands-json",
                ]
            )

        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertNotIn(cli.COMMANDS_JSON, stderr.getvalue())
        self.assertIn("--inputs-json", stderr.getvalue())
        github_token.assert_not_called()
        build_runtime.assert_not_called()

    def test_commands_json_after_subcommand_is_not_global_discovery(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch("repogents.cli._github_token") as github_token,
            patch("repogents.cli.build_runtime") as build_runtime,
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main(["state", "--commands-json"])

        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertNotIn(cli.COMMANDS_JSON, stderr.getvalue())
        self.assertIn("--commands-json", stderr.getvalue())
        github_token.assert_not_called()
        build_runtime.assert_not_called()


if __name__ == "__main__":
    unittest.main()
