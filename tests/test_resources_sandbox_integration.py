from __future__ import annotations

import hashlib
import inspect
import json
import shutil
import subprocess
import unittest

from pathlib import Path

from repogents.execution import (
    ExecutionService,
    _sandbox_policy,
    _secret_bindings,
    _variable_bindings,
)
from repogents.lifecycle import RunLayout, RunLifecycle
from repogents.onboarding import SandboxEnvironmentProvisioner
from repogents.resources import RepositoryResourceStore
from repogents.sandbox import SandboxManager
from repogents.team import TeamService
from tests.test_onboarding import FakeSources, OnboardingTests


class RepositoryResourcesSandboxIntegrationTests(OnboardingTests):
    """Exercise persisted resources through onboarding's real SandboxManager path."""

    @staticmethod
    def _save_secret(
        store: RepositoryResourceStore, repository_id: str, name: str, value: str
    ) -> dict[str, object]:
        for candidate in dir(store):
            method = getattr(store, candidate)
            if candidate.startswith("_") or not callable(method):
                continue
            parameters = inspect.signature(method).parameters
            if {"repository_id", "name", "action", "value"}.issubset(parameters):
                return method(
                    repository_id, name=name, action="replace", value=value
                )
        raise AssertionError("repository secret mutation API was not found")

    def _install_secret_resolver(
        self, service: object, store: RepositoryResourceStore
    ) -> None:
        service.provisioner = SandboxEnvironmentProvisioner(
            data_root=self.root / "data",
            sandbox=SandboxManager(),
            secret_resolver=store.resolve_secret,
        )

    def test_onboarding_executes_scoped_resources_and_retains_immutable_versions(
        self,
    ) -> None:
        if shutil.which("bwrap") is None:
            self.skipTest("bubblewrap is unavailable")

        service, _ = self.service(FakeSources())
        repository_id = service.onboard(
            "example/demo",
            {"validation_commands": [["python3", "-c", "pass"]]},
        )
        store = RepositoryResourceStore(self.db, self.root / "data")
        secret_value = "saved-product-key-integration-7391"
        secret = self._save_secret(
            store, repository_id, "PRODUCT_KEY", secret_value
        )
        reference = str(secret["reference"])
        self._install_secret_resolver(service, store)

        provision_script = (
            "import hashlib,json,os,pathlib; "
            "data=pathlib.Path('/repository-resources/artifacts/fixture-sdk').read_bytes(); "
            "assert os.environ['LICENSE_MODE']=='fixture'; "
            "assert os.environ['PRODUCT_KEY']; "
            "pathlib.Path('/repository-state/resource-proof.json').write_text("
            "json.dumps({'hash':hashlib.sha256(data).hexdigest(),'mode':os.environ['LICENSE_MODE']}))"
        )
        validation_script = (
            "import hashlib,json,os,pathlib; "
            "data=pathlib.Path('/repository-resources/artifacts/fixture-sdk').read_bytes(); "
            "proof=json.loads(pathlib.Path('/repository-state/resource-proof.json').read_text()); "
            "assert proof=={'hash':hashlib.sha256(data).hexdigest(),'mode':os.environ['LICENSE_MODE']}; "
            "assert os.environ['PRODUCT_KEY']; print('validated')"
        )
        provision_command = ["python3", "-c", provision_script]
        validation_command = ["python3", "-c", validation_script]

        first_bytes = b"licensed fixture sdk revision one"
        second_bytes = b"licensed fixture sdk revision two"
        first = store.upload_artifact(
            repository_id,
            name="fixture-sdk",
            description="Licensed fixture SDK used by provisioning",
            content=first_bytes,
        )
        first_binding = dict(first)
        first_binding["sandbox_path"] = "/repository-resources/artifacts/fixture-sdk"

        def inputs(binding: dict[str, object]) -> dict[str, object]:
            commands = [provision_command, validation_command]
            return {
                "artifact_bindings": [binding],
                "variable_bindings": [
                    {"name": "LICENSE_MODE", "value": "fixture", "commands": commands}
                ],
                "secret_bindings": [
                    {"name": "PRODUCT_KEY", "reference": reference, "commands": commands}
                ],
                "provisioning_commands": [provision_command],
                "validation_commands": [validation_command],
            }

        service.reonboard(repository_id, inputs(first_binding))
        first_repository = service.get_repository(repository_id)
        self.assertEqual(
            "ready",
            first_repository["onboarding_state"],
            first_repository["blocking_reason"],
        )
        first_sandbox_id = str(first_repository["current_sandbox_version_id"])
        first_team_id = str(first_repository["current_team_version_id"])
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO issues
                   (id, repository_id, github_node_id, number, url, title, body,
                    discussion_json, updated_at)
                   VALUES ('resource-issue', ?, 'resource-issue-node', 7,
                           'https://example.test/issues/7', 'Resources fixture',
                           'Validate retained resources', '[]', ?)""",
                (repository_id, "2026-01-01T00:00:00Z"),
            )
            connection.execute(
                """INSERT INTO activation_events
                   (id, repository_id, issue_id, github_event_id, applied_at)
                   VALUES ('resource-activation', ?, 'resource-issue',
                           'resource-event', ?)""",
                (repository_id, "2026-01-01T00:00:00Z"),
            )
            connection.execute(
                """INSERT INTO runs
                   (id, repository_id, issue_id, activation_event_id,
                    sandbox_version_id, team_version_id, intended_base_branch,
                    base_sha, state, created_at, updated_at)
                   VALUES ('resource-run-v1', ?, 'resource-issue',
                           'resource-activation', ?, ?, 'main', ?, 'queued', ?, ?)""",
                (
                    repository_id,
                    first_sandbox_id,
                    first_team_id,
                    "a" * 40,
                    "2026-01-01T00:00:00Z",
                    "2026-01-01T00:00:00Z",
                ),
            )

        second = store.upload_artifact(
            repository_id,
            name="fixture-sdk",
            description="Licensed fixture SDK replacement",
            content=second_bytes,
        )
        second_binding = dict(second)
        second_binding["sandbox_path"] = "/repository-resources/artifacts/fixture-sdk"
        service.reonboard(repository_id, inputs(second_binding))
        second_repository = service.get_repository(repository_id)
        self.assertEqual("ready", second_repository["onboarding_state"])
        second_sandbox_id = str(second_repository["current_sandbox_version_id"])
        self.assertNotEqual(first_sandbox_id, second_sandbox_id)

        with self.db.connect() as connection:
            pinned = connection.execute(
                """SELECT sandbox_artifact_revisions.sandbox_version_id,
                          artifact_revisions.revision
                   FROM sandbox_artifact_revisions
                   JOIN artifact_revisions
                     ON artifact_revisions.id=sandbox_artifact_revisions.artifact_revision_id
                   WHERE sandbox_artifact_revisions.sandbox_version_id IN (?, ?)
                   ORDER BY artifact_revisions.revision""",
                (first_sandbox_id, second_sandbox_id),
            ).fetchall()
        self.assertEqual(
            [(first_sandbox_id, 1), (second_sandbox_id, 2)],
            [(str(row["sandbox_version_id"]), int(row["revision"])) for row in pinned],
        )

        # Validate the already-created run after v2 exists.  Execution must keep
        # using the run's pinned v1 artifact, variables, secret, and persistent
        # repository state rather than the repository's current sandbox version.
        layout = RunLayout.create(self.root / "data", repository_id, "resource-run-v1")
        layout.checkout.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q"], cwd=layout.checkout, check=True)
        (layout.checkout / "README.md").write_text("fixture\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=layout.checkout, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.test",
                "commit",
                "-qm",
                "fixture",
            ],
            cwd=layout.checkout,
            check=True,
        )
        commit_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=layout.checkout,
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.strip()
        with self.db.transaction() as connection:
            validation_row = connection.execute(
                """SELECT id, command_json FROM validation_commands
                   WHERE sandbox_version_id=? ORDER BY position LIMIT 1""",
                (first_sandbox_id,),
            ).fetchone()
            self.assertIsNotNone(validation_row)
            connection.execute(
                """UPDATE runs SET base_sha=?, checkout_path=?, run_path=?
                   WHERE id='resource-run-v1'""",
                (commit_sha, str(layout.checkout), str(layout.root)),
            )
            connection.execute(
                """INSERT INTO validation_baselines
                   (id, run_id, validation_command_id, command_json, base_sha,
                    mode, started_at, completed_at, exit_status, log_path,
                    findings_json)
                   VALUES ('resource-baseline-v1', 'resource-run-v1', ?, ?, ?,
                           'strict', ?, ?, 0, ?, '[]')""",
                (
                    str(validation_row["id"]),
                    str(validation_row["command_json"]),
                    commit_sha,
                    "2026-01-01T00:00:00Z",
                    "2026-01-01T00:00:01Z",
                    str(layout.logs / "resource-baseline-v1.json"),
                ),
            )
            sandbox_row = connection.execute(
                "SELECT * FROM sandbox_versions WHERE id=?",
                (first_sandbox_id,),
            ).fetchone()
        self.assertIsNotNone(sandbox_row)
        execution = ExecutionService(
            database=self.db,
            lifecycle=type(
                "PinnedRunLifecycle",
                (),
                {"get_run": lambda _self, _run_id: {"state": "queued"}},
            )(),
            teams=object(),
            sandbox=SandboxManager(),
            secret_resolver=store.resolve_secret,
        )
        valid, detail = execution._validate(
            "resource-run-v1",
            commit_sha,
            first_sandbox_id,
            _sandbox_policy(sandbox_row),
            layout,
            _secret_bindings(sandbox_row),
            _variable_bindings(sandbox_row),
            set(),
            commit_sha,
        )
        self.assertTrue(valid, detail)

        expected_hashes = {
            hashlib.sha256(first_bytes).hexdigest(),
            hashlib.sha256(second_bytes).hexdigest(),
        }
        retained_proofs = []
        for proof_path in (self.root / "data").rglob("resource-proof.json"):
            retained_proofs.append(json.loads(proof_path.read_text(encoding="utf-8")))
        self.assertTrue(
            expected_hashes.issubset({str(proof.get("hash")) for proof in retained_proofs}),
            retained_proofs,
        )

        database_bytes = (self.root / "repogents.sqlite3").read_bytes()
        self.assertNotIn(secret_value.encode(), database_bytes)
        durable_text = "".join(
            path.read_text(encoding="utf-8", errors="replace")
            for path in (self.root / "data").rglob("*.json")
        )
        self.assertNotIn(secret_value, durable_text)
        self.assertNotIn(first_bytes.decode(), durable_text)
        self.assertNotIn(second_bytes.decode(), durable_text)


if __name__ == "__main__":
    unittest.main()
