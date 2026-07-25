from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from repogents.configuration import ModelProviderConfiguration


class ModelProviderConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name) / "application-state"

    def test_saved_settings_override_bootstrap_and_keep_key_out_of_public_state(
        self,
    ) -> None:
        configuration = ModelProviderConfiguration(
            self.root,
            bootstrap_model="openai/bootstrap",
            bootstrap_api_endpoint="https://bootstrap.example/v1",
            environment={},
        )
        self.assertEqual(
            configuration.public_state()["default_model"], "openai/bootstrap"
        )
        self.assertFalse(configuration.public_state()["api_key_configured"])
        self.assertTrue(configuration.public_state()["api_key_required"])
        self.assertFalse(configuration.public_state()["configured"])

        saved = configuration.update(
            {
                "api_endpoint": "https://models.example.test/v1/",
                "api_key": "dashboard-secret",  # pragma: allowlist secret
                "default_model": "openai/default-agent",
                "lead_model": "openai/lead-agent",
                "implementer_model": "openai/implementation-agent",
                "verifier_model": "openai/verification-agent",
            }
        )

        self.assertEqual(
            saved,
            {
                "configured": True,
                "api_endpoint": "https://models.example.test/v1/",
                "default_model": "openai/default-agent",
                "lead_model": "openai/lead-agent",
                "implementer_model": "openai/implementation-agent",
                "verifier_model": "openai/verification-agent",
                "api_key_configured": True,
                "api_key_required": True,
                "api_key_source": "saved",
            },
        )
        serialized_public = json.dumps(saved, sort_keys=True)
        self.assertNotIn("dashboard-secret", serialized_public)
        settings_path = self.root / "configuration" / "model-provider.json"
        key_path = self.root / "secrets" / "model-provider-api-key"
        self.assertNotIn("dashboard-secret", settings_path.read_text(encoding="utf-8"))
        self.assertEqual(
            key_path.read_text(encoding="utf-8"),
            "dashboard-secret",
        )
        self.assertEqual(settings_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(key_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(settings_path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(key_path.parent.stat().st_mode & 0o777, 0o700)

        reloaded = ModelProviderConfiguration(
            self.root,
            bootstrap_model="openai/ignored-bootstrap",
            bootstrap_api_endpoint="https://ignored.example/v1",
            environment={},
        )
        self.assertEqual(reloaded.public_state(), saved)
        self.assertEqual(reloaded.model_for_role("lead"), "openai/lead-agent")
        self.assertEqual(
            reloaded.model_for_role("scout"),
            "openai/default-agent",
        )
        connection = reloaded.connection_for_model("openai/lead-agent")
        self.assertEqual(connection.api_endpoint, "https://models.example.test/v1/")
        self.assertEqual(connection.api_key, "dashboard-secret")
        self.assertNotIn("dashboard-secret", repr(connection))

    def test_blank_key_preserves_saved_key_and_explicit_clear_restores_environment(
        self,
    ) -> None:
        configuration = ModelProviderConfiguration(
            self.root,
            bootstrap_model="openai/bootstrap",
            environment={
                "OPENAI_API_KEY": "environment-secret",  # pragma: allowlist secret
            },
        )
        self.assertEqual(
            configuration.public_state()["api_key_source"],
            "environment",
        )
        configuration.update(
            {
                "default_model": "openai/default",
                "api_endpoint": "",
                "lead_model": "",
                "implementer_model": "",
                "verifier_model": "",
                "api_key": "saved-secret",  # pragma: allowlist secret
            }
        )
        configuration.update(
            {
                "default_model": "openai/default",
                "api_endpoint": "",
                "lead_model": "",
                "implementer_model": "",
                "verifier_model": "",
                "api_key": "",
            }
        )
        self.assertEqual(
            configuration.connection_for_model("openai/default").api_key,
            "saved-secret",
        )

        cleared = configuration.update(
            {
                "default_model": "openai/default",
                "api_endpoint": "",
                "lead_model": "",
                "implementer_model": "",
                "verifier_model": "",
                "clear_api_key": True,
            }
        )
        self.assertEqual(cleared["api_key_source"], "environment")
        self.assertIsNone(configuration.connection_for_model("openai/default").api_key)
        self.assertFalse((self.root / "secrets" / "model-provider-api-key").exists())

    def test_unconfigured_runtime_is_available_but_model_use_has_bounded_error(
        self,
    ) -> None:
        configuration = ModelProviderConfiguration(self.root, environment={})
        self.assertEqual(
            configuration.public_state(),
            {
                "configured": False,
                "api_endpoint": None,
                "default_model": None,
                "lead_model": None,
                "implementer_model": None,
                "verifier_model": None,
                "api_key_configured": False,
                "api_key_required": False,
                "api_key_source": None,
            },
        )
        with self.assertRaisesRegex(RuntimeError, "Model provider.*not configured"):
            configuration.model_for_role("lead")

    def test_provider_without_managed_key_can_be_configured_without_one(self) -> None:
        configuration = ModelProviderConfiguration(self.root, environment={})

        saved = configuration.update(
            {
                "default_model": "ollama/local-model",
                "api_endpoint": "http://127.0.0.1:11434/v1",
            }
        )

        self.assertIs(saved["configured"], True)
        self.assertIs(saved["api_key_required"], False)
        self.assertIs(saved["api_key_configured"], False)

    def test_saved_key_survives_model_provider_prefix_changes(self) -> None:
        configuration = ModelProviderConfiguration(self.root, environment={})
        configuration.update(
            {
                "api_endpoint": "https://models.example.test/v1",
                "default_model": "openai/default",
                "api_key": "openai-secret",  # pragma: allowlist secret
            }
        )

        saved = configuration.update(
            {
                "default_model": "anthropic/claude",
                "lead_model": "openai/lead",
                "api_key": "",
            }
        )

        self.assertEqual(saved["default_model"], "anthropic/claude")
        self.assertEqual(saved["lead_model"], "openai/lead")
        self.assertEqual(saved["api_key_source"], "saved")
        for model in ("anthropic/claude", "openai/lead"):
            connection = configuration.connection_for_model(model)
            self.assertEqual(connection.api_key, "openai-secret")
            self.assertEqual(
                connection.api_endpoint,
                "https://models.example.test/v1",
            )

        aws = ModelProviderConfiguration(
            self.root / "aws",
            environment={
                "AWS_ACCESS_KEY_ID": "access-id",
                "AWS_SECRET_ACCESS_KEY": "secret-access-key",  # pragma: allowlist secret
            },
        )
        saved = aws.update({"default_model": "aws/bedrock-model"})
        self.assertIs(saved["configured"], True)
        self.assertIs(saved["api_key_required"], False)
        self.assertIs(saved["api_key_configured"], False)
        self.assertIsNone(aws.connection_for_model("aws/bedrock-model").api_key)
        with self.assertRaisesRegex(
            ValueError,
            "does not support a managed API key",
        ):
            aws.update(
                {
                    "default_model": "aws/bedrock-model",
                    "api_key": "not-a-complete-credential",
                }
            )

        oauth = ModelProviderConfiguration(
            self.root / "oauth",
            environment={
                "ANTHROPIC_OAUTH_TOKEN": "oauth-token",  # pragma: allowlist secret
            },
        )
        oauth_state = oauth.update({"default_model": "anthropic/claude"})
        self.assertEqual(oauth_state["api_key_source"], "environment")
        self.assertIsNone(oauth.connection_for_model("anthropic/claude").api_key)

    def test_model_catalog_maps_endpoint_ids_without_exposing_key(self) -> None:
        configuration = ModelProviderConfiguration(self.root, environment={})
        configuration.update(
            {
                "api_endpoint": "https://models.example.test/v1",
                "default_model": "openai/default",
                "api_key": "catalog-secret",  # pragma: allowlist secret
            }
        )

        class CatalogResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, limit):
                return json.dumps(
                    {
                        "data": [
                            {"id": "codex/gpt-5.6-sol"},
                            {"id": "codex/gpt-5.6-sol"},
                            {"id": "default"},
                            {"id": ""},
                            {"missing": "id"},
                        ]
                    }
                ).encode("utf-8")

        with patch(
            "repogents.configuration.urllib.request.urlopen",
            return_value=CatalogResponse(),
        ) as fetch:
            catalog = configuration.model_catalog()

        request = fetch.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://models.example.test/v1/models",
        )
        self.assertEqual(
            request.get_header("Authorization"),
            "Bearer catalog-secret",
        )
        self.assertEqual(
            catalog,
            {
                "available": True,
                "reason": None,
                "models": [
                    {"id": "codex/gpt-5.6-sol", "value": "openai/codex/gpt-5.6-sol"},
                    {"id": "default", "value": "openai/default"},
                ],
            },
        )
        self.assertNotIn("catalog-secret", json.dumps(catalog, sort_keys=True))

        missing = ModelProviderConfiguration(self.root / "missing", environment={})
        missing.update(
            {
                "api_endpoint": "https://models.example.test/v1",
                "default_model": "openai/default",
            }
        )
        with patch("repogents.configuration.urllib.request.urlopen") as fetch:
            unavailable = missing.model_catalog()
        fetch.assert_not_called()
        self.assertEqual(
            unavailable,
            {
                "available": False,
                "reason": "API key missing",
                "models": [],
            },
        )

    def test_rejects_invalid_settings_without_changing_state(self) -> None:
        configuration = ModelProviderConfiguration(
            self.root,
            bootstrap_model="openai/bootstrap",
            environment={},
        )
        before = configuration.public_state()
        invalid_values = (
            ({"default_model": ""}, "default model"),
            (
                {
                    "default_model": "openai/default",
                    "api_endpoint": "/relative/v1",
                },
                "absolute http",
            ),
            (
                {
                    "default_model": "openai/default",
                    "api_endpoint": "https://user:password@models.example/v1",
                },
                "credentials",
            ),
            (
                {
                    "default_model": "openai/default",
                    "api_key": "new-secret",
                    "clear_api_key": True,
                },
                "clear",
            ),
        )
        for payload, message in invalid_values:
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(ValueError, message):
                    configuration.update(payload)
                self.assertEqual(configuration.public_state(), before)

    def test_atomic_replace_failure_preserves_previous_settings_file(self) -> None:
        configuration = ModelProviderConfiguration(
            self.root,
            bootstrap_model="openai/bootstrap",
            environment={},
        )
        configuration.update({"default_model": "openai/first"})
        settings_path = self.root / "configuration" / "model-provider.json"
        before = settings_path.read_bytes()
        real_replace = os.replace

        def fail_settings_replace(
            source: str | os.PathLike[str], target: str | os.PathLike[str]
        ) -> None:
            if Path(target) == settings_path:
                raise OSError("simulated replace failure")
            real_replace(source, target)

        with patch(
            "repogents.configuration.os.replace", side_effect=fail_settings_replace
        ):
            with self.assertRaisesRegex(OSError, "simulated"):
                configuration.update({"default_model": "openai/second"})

        self.assertEqual(settings_path.read_bytes(), before)
        self.assertEqual(configuration.public_state()["default_model"], "openai/first")


if __name__ == "__main__":
    unittest.main()
