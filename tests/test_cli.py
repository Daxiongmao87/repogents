from __future__ import annotations

import io
import os
import unittest
from unittest.mock import patch

from repogents import cli


class CommandLineTests(unittest.TestCase):
    def test_version_reports_source_package_without_configuration_or_runtime(self) -> None:
        stdout = io.StringIO()
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(cli, "__version__", "9.8.7"),
            patch.object(cli, "_github_token") as github_token,
            patch.object(cli, "build_runtime") as build_runtime,
            patch("sys.stdout", stdout),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main(["--version"])

        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(stdout.getvalue(), "9.8.7\n")
        github_token.assert_not_called()
        build_runtime.assert_not_called()

    def test_version_uses_current_source_when_installed_metadata_is_stale(self) -> None:
        stdout = io.StringIO()
        with (
            patch.object(cli, "__version__", "9.8.7"),
            patch("importlib.metadata.version", return_value="1.2.3"),
            patch.object(cli, "_github_token") as github_token,
            patch.object(cli, "build_runtime") as build_runtime,
            patch("sys.stdout", stdout),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main(["--version"])

        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(stdout.getvalue(), "9.8.7\n")
        github_token.assert_not_called()
        build_runtime.assert_not_called()

    def test_version_succeeds_with_invalid_lan_port_without_runtime(self) -> None:
        stdout = io.StringIO()
        with (
            patch.dict(os.environ, {"REPOGENTS_LAN_PORT": "not-a-number"}),
            patch.object(cli, "__version__", "9.8.7"),
            patch.object(cli, "build_runtime") as build_runtime,
            patch("sys.stdout", stdout),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main(["--version"])

        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(stdout.getvalue(), "9.8.7\n")
        build_runtime.assert_not_called()

    def test_version_succeeds_with_invalid_poll_seconds_without_runtime(self) -> None:
        stdout = io.StringIO()
        with (
            patch.dict(os.environ, {"REPOGENTS_POLL_SECONDS": "not-a-number"}),
            patch.object(cli, "__version__", "9.8.7"),
            patch.object(cli, "build_runtime") as build_runtime,
            patch("sys.stdout", stdout),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main(["--version"])

        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(stdout.getvalue(), "9.8.7\n")
        build_runtime.assert_not_called()

    def test_invalid_lan_port_environment_value_is_rejected_before_runtime(self) -> None:
        with (
            patch.dict(os.environ, {"REPOGENTS_LAN_PORT": "not-a-port"}),
            patch.object(cli, "build_runtime") as build_runtime,
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main(["serve", "--port", os.environ["REPOGENTS_LAN_PORT"]])

        self.assertNotEqual(raised.exception.code, 0)
        build_runtime.assert_not_called()

    def test_invalid_poll_seconds_environment_value_is_rejected_before_runtime(self) -> None:
        with (
            patch.dict(os.environ, {"REPOGENTS_POLL_SECONDS": "not-seconds"}),
            patch.object(cli, "build_runtime") as build_runtime,
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main(
                ["serve", "--poll-interval", os.environ["REPOGENTS_POLL_SECONDS"]]
            )

        self.assertNotEqual(raised.exception.code, 0)
        build_runtime.assert_not_called()


if __name__ == "__main__":
    unittest.main()
