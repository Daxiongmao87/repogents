from __future__ import annotations

import json
import tempfile
import unittest
from unittest import mock
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from repogents.database import Database
from repogents.github import RepositoryInfo, parse_repository_identity
from repogents.sandbox import Mount, RunLayout, SandboxManager, SandboxPolicy
from repogents.onboarding import (
    GitSourceManager,
    MissingRepositoryInput,
    OnboardingService,
    RepositoryInference,
    RepositoryInspection,
    RepositoryInspector,
    SandboxEnvironmentProvisioner,
)
from repogents.team import EvidenceTeamFormulator


@dataclass
class FakeGitHub:
    info: RepositoryInfo
    calls: int = 0

    def get_repository(self, identity: str) -> RepositoryInfo:
        self.calls += 1
        return self.info


class FakeSources:
    def __init__(
        self, files: dict[str, str] | None = None, error: Exception | None = None
    ) -> None:
        self.files = files or {}
        self.error = error
        self.calls = 0

    def prepare(self, repository: RepositoryInfo, destination: Path) -> str:
        self.calls += 1
        if self.error:
            raise self.error
        destination.mkdir(parents=True, exist_ok=True)
        for relative, content in self.files.items():
            path = destination / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        return "a" * 40


class RecordingSandbox:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def run(
        self,
        policy: object,
        layout: object,
        command: tuple[str, ...],
        **options: object,
    ) -> object:
        self.calls.append((policy, layout, command, options))
        return SimpleNamespace(
            returncode=0,
            stderr="",
            log_path=Path(getattr(layout, "logs")) / f"{len(self.calls)}.json",
        )


class FakeTeamFormulator:
    def __init__(
        self,
        *,
        runtime: str = "configured",
        model: str = "configured",
        role_prefixes: list[str] | None = None,
    ) -> None:
        self.runtime = runtime
        self.model = model
        self.role_prefixes = list(role_prefixes or [])
        self.inspections: list[RepositoryInspection] = []

    def formulate(self, inspection: RepositoryInspection) -> list[dict[str, object]]:
        self.inspections.append(inspection)
        prefix = (
            self.role_prefixes.pop(0)
            if self.role_prefixes
            else (inspection.languages[0] if inspection.languages else "repository")
        )
        instructions = inspection.summary
        return [
            {
                "stable_key": f"{prefix}-coordination",
                "role": f"{prefix} delivery coordinator",
                "execution_class": "lead",
                "coordinates": True,
                "independent_verifier": False,
                "responsibilities": "Coordinate assignments and integrate outputs.",
                "permitted_tools": ["read", "git_diff", "git_commit"],
                "runtime": self.runtime,
                "model": self.model,
                "instructions": instructions,
            },
            {
                "stable_key": f"{prefix}-implementation",
                "role": f"{prefix} implementation maintainer",
                "execution_class": "implementer",
                "coordinates": False,
                "independent_verifier": False,
                "responsibilities": "Implement repository changes.",
                "permitted_tools": ["read", "write", "run", "git_diff"],
                "runtime": self.runtime,
                "model": self.model,
                "instructions": instructions,
            },
            {
                "stable_key": f"{prefix}-verification",
                "role": f"{prefix} behavior verifier",
                "execution_class": "verifier",
                "coordinates": False,
                "independent_verifier": True,
                "responsibilities": "Independently verify repository behavior.",
                "permitted_tools": ["read", "run", "git_diff"],
                "runtime": self.runtime,
                "model": self.model,
                "instructions": instructions,
            },
        ]


class ScriptedEvidenceAnalyzer:
    def __init__(self, outcomes: list[RepositoryInference | Exception]) -> None:
        self.outcomes = list(outcomes)
        self.inspections: list[RepositoryInspection] = []
        self.prior_failures: list[str | None] = []

    def analyze(
        self,
        inspection: RepositoryInspection,
        *,
        prior_failure: str | None = None,
    ) -> RepositoryInference:
        self.inspections.append(inspection)
        self.prior_failures.append(prior_failure)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class MiniSweRepositoryEvidenceAnalyzerTests(unittest.TestCase):
    def test_uses_structured_generic_evidence_and_explicit_model_endpoint(
        self,
    ) -> None:
        from repogents.onboarding import MiniSweRepositoryEvidenceAnalyzer

        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory) / "checkout"
            checkout.mkdir()
            (checkout / "Gemfile").write_text(
                "source 'https://rubygems.org'\ngem 'rspec'\n",
                encoding="utf-8",
            )
            inspection = RepositoryInspector().inspect(checkout)
            state_root = Path(directory) / "runtime-state"
            with mock.patch("repogents.onboarding.MiniSweInference") as inference_type:
                inference_type.return_value.infer.return_value = {
                    "languages": ["ruby"],
                    "manifests": ["Gemfile"],
                    "provisioning_commands": [["bundle", "install"]],
                    "dependency_services": ["rubygems.org:443"],
                    "validation_commands": [["bundle", "exec", "rspec"]],
                }

                inference = MiniSweRepositoryEvidenceAnalyzer(
                    model="openai/gpt-stored",
                    base_url="https://models.example.test/v1",
                    timeout=601,
                    state_root=state_root,
                ).analyze(
                    inspection,
                    prior_failure=("previous provisioning failure: denied destination"),
                )

        self.assertEqual(inference.languages, ("ruby",))
        self.assertEqual(inference.validation_commands, (("bundle", "exec", "rspec"),))
        inference_type.assert_called_once_with(
            model="openai/gpt-stored",
            base_url="https://models.example.test/v1",
            api_key=None,
            timeout=601,
        )
        inference_call = inference_type.return_value.infer
        inference_call.assert_called_once()
        inference_arguments = inference_call.call_args.kwargs
        inference_state = Path(inference_arguments["state_directory"])
        self.assertTrue(inference_state.is_relative_to(state_root))
        self.assertIn(
            "exactly one JSON object",
            inference_arguments["system_prompt"],
        )
        prompt = str(inference_arguments["prompt"])
        self.assertIn('"Gemfile"', prompt)
        self.assertIn("gem 'rspec'", prompt)
        self.assertIn("toolchain bootstrap", prompt)
        self.assertIn("/repository-state/home", prompt)
        self.assertIn("/run-data/dependency-delta", prompt)
        self.assertIn("/workspace/node_modules is already", prompt)
        payload = json.loads(prompt)
        self.assertEqual(
            payload["prior_failure"],
            "previous provisioning failure: denied destination",
        )
        task = payload["task"]
        self.assertIn("no root privileges", task)
        self.assertIn("/usr, /bin, /lib, and /lib64 are read-only", task)
        self.assertIn("/etc is an empty non-writable directory", task)
        self.assertIn("Do not invoke system package managers", task)
        self.assertIn("/repository-state/bin", task)
        self.assertEqual(
            inference_arguments["response_schema"],
            {
                "type": "object",
                "properties": {
                    "languages": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "manifests": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "provisioning_commands": {
                        "type": "array",
                        "items": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "dependency_services": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "validation_commands": {
                        "type": "array",
                        "items": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                },
                "required": [
                    "languages",
                    "manifests",
                    "provisioning_commands",
                    "dependency_services",
                    "validation_commands",
                ],
                "additionalProperties": False,
            },
        )

    def test_bounds_oversized_repository_prompt_without_losing_core_evidence(
        self,
    ) -> None:
        from repogents.onboarding import MiniSweRepositoryEvidenceAnalyzer

        files = (
            "pyproject.toml",
            *(f"src/generated/file-{index:05d}.py" for index in range(12_000)),
        )
        inspection = RepositoryInspection(
            languages=("python",),
            manifests=("pyproject.toml",),
            lockfiles=(),
            instruction_files=(),
            validation_commands=(("python", "-m", "unittest"),),
            file_count=50_000,
            summary="large repository " + ("summary " * 40_000),
            source_files=files,
            source_evidence=(
                ("pyproject.toml", '[project]\nname = "large"\n' + ("#" * 200_000)),
                ("src/generated/file-00000.py", "value = 1\n" * 20_000),
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch("repogents.onboarding.MiniSweInference") as inference_type:
                inference_type.return_value.infer.return_value = {
                    "languages": ["python"],
                    "manifests": ["pyproject.toml"],
                    "provisioning_commands": [],
                    "dependency_services": [],
                    "validation_commands": [["python", "-m", "unittest"]],
                }

                MiniSweRepositoryEvidenceAnalyzer(
                    model="openai/local/default",
                    state_root=Path(directory),
                ).analyze(
                    inspection,
                    prior_failure="é" * 100_000,
                )

        prompt = str(inference_type.return_value.infer.call_args.kwargs["prompt"])
        self.assertLessEqual(len(prompt.encode("utf-8")), 96_000)
        payload = json.loads(prompt)
        self.assertLessEqual(
            len(payload["prior_failure"].encode("utf-8")),
            8_000,
        )
        repository = payload["repository"]
        self.assertEqual(repository["file_count"], 50_000)
        self.assertEqual(
            repository["initial_observations"]["languages"],
            ["python"],
        )
        self.assertEqual(
            repository["initial_observations"]["validation_commands"],
            [["python", "-m", "unittest"]],
        )
        self.assertIn("pyproject.toml", repository["files"])
        self.assertTrue(repository["contents"])
        self.assertEqual(
            repository["contents"][0]["path"],
            "pyproject.toml",
        )

    def test_rejects_nonpositive_inference_timeout(self) -> None:
        from repogents.onboarding import MiniSweRepositoryEvidenceAnalyzer

        with self.assertRaisesRegex(ValueError, "timeout must be positive"):
            MiniSweRepositoryEvidenceAnalyzer(
                model="openai/gpt-stored",
                timeout=0,
                state_root=Path("unused"),
            )

    def test_rejects_unsafe_or_malformed_structured_inference(self) -> None:
        from repogents.onboarding import MiniSweRepositoryEvidenceAnalyzer

        inspection = RepositoryInspection(
            languages=(),
            manifests=(),
            lockfiles=(),
            instruction_files=(),
            validation_commands=(),
            file_count=1,
            summary="one file",
            source_files=("unknown.project",),
            source_evidence=(("unknown.project", "content"),),
        )
        invalid_values = (
            {
                "languages": ["unknown"],
                "manifests": ["unknown.project"],
                "provisioning_commands": [[""]],
                "dependency_services": [],
                "validation_commands": [["tool", "test"]],
            },
            {
                "languages": ["unknown"],
                "manifests": ["unknown.project"],
                "provisioning_commands": [],
                "dependency_services": ["https://unsafe.example"],
                "validation_commands": [["tool", "test"]],
            },
        )
        for value in invalid_values:
            with self.subTest(value=value):
                with mock.patch(
                    "repogents.onboarding.MiniSweInference"
                ) as inference_type:
                    inference_type.return_value.infer.return_value = value
                    with self.assertRaisesRegex(
                        RuntimeError, "invalid repository inference"
                    ):
                        MiniSweRepositoryEvidenceAnalyzer(
                            model="openai/gpt-stored",
                            state_root=Path("unused"),
                        ).analyze(inspection)


class OnboardingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.db = Database(self.root / "repogents.sqlite3")
        self.db.initialize()
        self.repository = RepositoryInfo(
            node_id="R_public",
            database_id=101,
            owner="example",
            name="demo",
            url="https://github.com/example/demo",
            default_branch="main",
            is_private=False,
        )

    def service(self, sources: FakeSources) -> tuple[OnboardingService, FakeGitHub]:
        github = FakeGitHub(self.repository)
        return (
            OnboardingService(
                database=self.db,
                data_root=self.root / "data",
                github=github,
                sources=sources,
                inspector=RepositoryInspector(),
                team_formulator=FakeTeamFormulator(),
            ),
            github,
        )

    def test_repository_identity_accepts_url_and_owner_name(self) -> None:
        self.assertEqual(parse_repository_identity("example/demo"), ("example", "demo"))
        self.assertEqual(
            parse_repository_identity("https://github.com/example/demo.git"),
            ("example", "demo"),
        )
        with self.assertRaises(ValueError):
            parse_repository_identity("https://example.com/example/demo")

    def test_git_source_manager_transports_configured_token_only_in_git_environment(
        self,
    ) -> None:
        completed = [
            SimpleNamespace(returncode=0, stdout="", stderr=""),
            SimpleNamespace(returncode=0, stdout="b" * 40 + "\n", stderr=""),
        ]
        destination = self.root / "source"

        with mock.patch(
            "repogents.onboarding.subprocess.run",
            side_effect=completed,
        ) as run:
            sha = GitSourceManager(token="github-token").prepare(
                self.repository,
                destination,
            )

        self.assertEqual(sha, "b" * 40)
        self.assertEqual(run.call_count, 2)
        for call in run.call_args_list:
            argv = call.args[0]
            environment = call.kwargs["env"]
            self.assertNotIn("github-token", argv)
            self.assertEqual(
                environment["REPOGENTS_GITHUB_TOKEN"],
                "github-token",
            )
            self.assertIn("GIT_ASKPASS", environment)

    def test_inspector_derives_commands_and_evidence_from_source(self) -> None:
        checkout = self.root / "checkout"
        checkout.mkdir()
        (checkout / "package.json").write_text(
            json.dumps({"scripts": {"test": "vitest run", "lint": "eslint ."}}),
            encoding="utf-8",
        )
        (checkout / "package-lock.json").write_text("{}", encoding="utf-8")
        (checkout / "AGENTS.md").write_text(
            "Run tests before commits.", encoding="utf-8"
        )
        inspection = RepositoryInspector().inspect(checkout)
        self.assertEqual(inspection.languages, ("javascript",))
        self.assertEqual(
            inspection.validation_commands, (("npm", "test"), ("npm", "run", "lint"))
        )
        self.assertIn("AGENTS.md", inspection.instruction_files)
        self.assertIn("package-lock.json", inspection.lockfiles)
        self.assertEqual(
            inspection.instructions,
            (("AGENTS.md", "Run tests before commits."),),
        )

    def test_inspector_rejects_symlinked_evidence_and_escape_paths(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "package.json").write_text(
            '{"scripts":{"test":"node test.js"}}',
            encoding="utf-8",
        )
        (outside / "AGENTS.md").write_text(
            "Instructions outside the source root.",
            encoding="utf-8",
        )
        cases = (
            ("manifest", "package.json", outside / "package.json"),
            ("instruction", "AGENTS.md", outside / "AGENTS.md"),
            ("directory", "escaped", outside),
        )
        for label, relative, target in cases:
            with self.subTest(label=label):
                checkout = self.root / f"checkout-{label}"
                checkout.mkdir()
                (checkout / relative).symlink_to(
                    target,
                    target_is_directory=target.is_dir(),
                )
                with self.assertRaisesRegex(RuntimeError, "symlink|outside"):
                    RepositoryInspector().inspect(checkout)

    def test_inspector_derives_standard_library_python_validation_without_project_config(
        self,
    ) -> None:
        checkout = self.root / "python-checkout"
        checkout.mkdir()
        (checkout / "requirements.txt").write_text("Flask==2.3.2\n", encoding="utf-8")
        (checkout / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        (checkout / "test_app.py").write_text(
            "import unittest\n\nclass Smoke(unittest.TestCase):\n    pass\n",
            encoding="utf-8",
        )
        inspection = RepositoryInspector().inspect(checkout)
        self.assertEqual(
            inspection.validation_commands,
            (
                ("python3", "-m", "unittest", "discover"),
                ("python3", "-m", "compileall", "-q", "."),
            ),
        )

    def test_evidence_analyzer_onboards_ruby_without_manual_commands(self) -> None:
        inference = RepositoryInference(
            languages=("ruby",),
            manifests=("Gemfile", "Rakefile"),
            provisioning_commands=(("bundle", "install"),),
            dependency_services=("rubygems.org:443",),
            validation_commands=(("bundle", "exec", "rspec"),),
        )
        analyzer = ScriptedEvidenceAnalyzer([inference])
        sandbox = RecordingSandbox()
        service = OnboardingService(
            database=self.db,
            data_root=self.root / "data",
            github=FakeGitHub(self.repository),
            sources=FakeSources(
                {
                    "Gemfile": "source 'https://rubygems.org'\ngem 'rspec'\n",
                    "Rakefile": "require 'rspec/core/rake_task'\nRSpec::Core::RakeTask.new\n",
                    "spec/widget_spec.rb": "RSpec.describe('widget') { it { expect(1).to eq(1) } }\n",
                }
            ),
            inspector=RepositoryInspector(),
            evidence_analyzer=analyzer,
            team_formulator=FakeTeamFormulator(),
            provisioner=SandboxEnvironmentProvisioner(
                data_root=self.root / "data",
                sandbox=sandbox,
            ),
        )

        repository_id = service.onboard("example/demo")

        repository = service.get_repository(repository_id)
        self.assertEqual(repository["onboarding_state"], "ready")
        self.assertEqual(len(analyzer.inspections), 1)
        inspection_packet = analyzer.inspections[0]
        self.assertIn("Gemfile", inspection_packet.source_files)
        self.assertIn(
            ("Gemfile", "source 'https://rubygems.org'\ngem 'rspec'\n"),
            inspection_packet.source_evidence,
        )
        self.assertEqual(
            [call[2] for call in sandbox.calls],
            [("bundle", "install")],
        )
        with self.db.connect() as connection:
            sandbox_version = connection.execute(
                "SELECT policy_json, evidence_json FROM sandbox_versions"
            ).fetchone()
            validation = connection.execute(
                "SELECT command_json, source FROM validation_commands"
            ).fetchone()
        policy = json.loads(sandbox_version["policy_json"])
        evidence = json.loads(sandbox_version["evidence_json"])
        self.assertIn("rubygems.org:443", policy["allowed_services"])
        self.assertEqual(evidence["languages"], ["ruby"])
        self.assertEqual(evidence["provisioning_commands"], [["bundle", "install"]])
        self.assertEqual(
            json.loads(validation["command_json"]),
            ["bundle", "exec", "rspec"],
        )
        self.assertEqual(validation["source"], "repository inference")

    def test_model_cannot_remove_manifest_derived_dependency_service(
        self,
    ) -> None:
        inference = RepositoryInference(
            languages=("javascript",),
            manifests=("package.json",),
            provisioning_commands=(("npm", "install"),),
            dependency_services=("packages.example.test:443",),
            validation_commands=(("npm", "test"),),
        )
        service = OnboardingService(
            database=self.db,
            data_root=self.root / "data",
            github=FakeGitHub(self.repository),
            sources=FakeSources(
                {
                    "package.json": json.dumps(
                        {
                            "scripts": {"test": "node test.js"},
                            "dependencies": {"example": "1.0.0"},
                        }
                    ),
                    "test.js": "",
                }
            ),
            inspector=RepositoryInspector(),
            evidence_analyzer=ScriptedEvidenceAnalyzer([inference]),
            team_formulator=FakeTeamFormulator(),
            provisioner=SandboxEnvironmentProvisioner(
                data_root=self.root / "data",
                sandbox=RecordingSandbox(),
            ),
        )

        service.onboard("example/demo")

        with self.db.connect() as connection:
            policy_json = connection.execute(
                "SELECT policy_json FROM sandbox_versions"
            ).fetchone()["policy_json"]
        services = set(json.loads(policy_json)["allowed_services"])
        self.assertEqual(
            services,
            {
                "packages.example.test:443",
                "nodejs.org:443",
                "registry.npmjs.org:443",
            },
        )

    def test_inference_failure_is_visible_and_reonboarding_retries_it(self) -> None:
        inference = RepositoryInference(
            languages=("ruby",),
            manifests=("Gemfile",),
            provisioning_commands=(),
            dependency_services=("rubygems.org:443",),
            validation_commands=(("ruby", "-c", "app.rb"),),
        )
        analyzer = ScriptedEvidenceAnalyzer(
            [RuntimeError("repository inference unavailable"), inference]
        )
        service = OnboardingService(
            database=self.db,
            data_root=self.root / "data",
            github=FakeGitHub(self.repository),
            sources=FakeSources(
                {
                    "Gemfile": "source 'https://rubygems.org'\n",
                    "app.rb": "puts 'ok'\n",
                }
            ),
            inspector=RepositoryInspector(),
            evidence_analyzer=analyzer,
            team_formulator=FakeTeamFormulator(),
        )

        repository_id = service.onboard("example/demo")

        blocked = service.get_repository(repository_id)
        self.assertEqual(blocked["onboarding_state"], "blocked")
        self.assertEqual(
            blocked["blocking_reason"],
            "repository inference unavailable",
        )
        service.reonboard(repository_id)
        ready = service.get_repository(repository_id)
        self.assertEqual(ready["onboarding_state"], "ready")
        self.assertIsNone(ready["blocking_reason"])
        self.assertEqual(len(analyzer.inspections), 2)
        self.assertEqual(
            analyzer.prior_failures,
            [None, "repository inference unavailable"],
        )

    def test_inference_path_persists_distinct_repository_sandboxes_and_teams(
        self,
    ) -> None:
        analyzer = ScriptedEvidenceAnalyzer(
            [
                RepositoryInference(
                    languages=("javascript", "python"),
                    manifests=("package.json", "pyproject.toml"),
                    provisioning_commands=(("npm", "ci"),),
                    dependency_services=("registry.npmjs.org:443",),
                    validation_commands=(
                        ("npm", "test"),
                        ("python3", "-m", "pytest"),
                    ),
                ),
                RepositoryInference(
                    languages=("ruby",),
                    manifests=("Gemfile",),
                    provisioning_commands=(("bundle", "install"),),
                    dependency_services=("rubygems.org:443",),
                    validation_commands=(("bundle", "exec", "rspec"),),
                ),
            ]
        )
        github = FakeGitHub(self.repository)
        sources = FakeSources(
            {
                "package.json": '{"scripts":{"test":"vitest run"}}',
                "pyproject.toml": "[tool.pytest.ini_options]\n",
            }
        )
        service = OnboardingService(
            database=self.db,
            data_root=self.root / "data",
            github=github,
            sources=sources,
            inspector=RepositoryInspector(),
            evidence_analyzer=analyzer,
            team_formulator=FakeTeamFormulator(
                runtime="mini-swe-agent",
                model="openai/gpt-stored",
            ),
        )
        first_id = service.onboard("example/demo")
        github.info = RepositoryInfo(
            node_id="R_ruby",
            database_id=202,
            owner="example",
            name="ruby-demo",
            url="https://github.com/example/ruby-demo",
            default_branch="main",
            is_private=False,
        )
        sources.files = {
            "Gemfile": "source 'https://rubygems.org'\n",
            "spec/widget_spec.rb": "RSpec.describe('widget') {}\n",
        }

        second_id = service.onboard("example/ruby-demo")

        with self.db.connect() as connection:
            rows = connection.execute(
                """SELECT repositories.id, sandbox_versions.policy_json,
                          sandbox_versions.evidence_json,
                          COUNT(team_members.id) AS member_count,
                          MIN(team_members.action_timeout_seconds) AS action_timeout,
                          MIN(team_members.runtime) AS runtime,
                          MIN(team_members.model) AS model
                   FROM repositories
                   JOIN sandbox_versions
                     ON sandbox_versions.id=repositories.current_sandbox_version_id
                   JOIN team_versions
                     ON team_versions.id=repositories.current_team_version_id
                   JOIN team_members
                     ON team_members.team_version_id=team_versions.id
                   WHERE repositories.id IN (?, ?)
                   GROUP BY repositories.id
                   ORDER BY repositories.id""",
                (first_id, second_id),
            ).fetchall()
            role_rows = connection.execute(
                """SELECT team_versions.repository_id, team_members.atomic_role
                   FROM team_versions
                   JOIN team_members
                     ON team_members.team_version_id=team_versions.id
                   WHERE team_versions.repository_id IN (?, ?)
                   ORDER BY team_versions.repository_id, team_members.stable_key""",
                (first_id, second_id),
            ).fetchall()
        stored = {str(row["id"]): row for row in rows}
        first_evidence = json.loads(stored[first_id]["evidence_json"])
        second_evidence = json.loads(stored[second_id]["evidence_json"])
        self.assertEqual(first_evidence["languages"], ["javascript", "python"])
        self.assertEqual(second_evidence["languages"], ["ruby"])
        self.assertEqual(stored[first_id]["member_count"], 3)
        self.assertEqual(stored[second_id]["member_count"], 3)
        self.assertEqual(stored[first_id]["action_timeout"], 600)
        self.assertEqual(stored[second_id]["action_timeout"], 600)
        self.assertEqual(stored[first_id]["runtime"], "mini-swe-agent")
        self.assertEqual(stored[second_id]["runtime"], "mini-swe-agent")
        self.assertEqual(stored[first_id]["model"], "openai/gpt-stored")
        self.assertEqual(stored[second_id]["model"], "openai/gpt-stored")
        roles_by_repository: dict[str, list[str]] = {}
        for row in role_rows:
            roles_by_repository.setdefault(str(row["repository_id"]), []).append(
                str(row["atomic_role"])
            )
        self.assertTrue(
            any("javascript" in role for role in roles_by_repository[first_id])
        )
        self.assertTrue(any("ruby" in role for role in roles_by_repository[second_id]))
        self.assertIn(
            "registry.npmjs.org:443",
            json.loads(stored[first_id]["policy_json"])["allowed_services"],
        )
        self.assertIn(
            "rubygems.org:443",
            json.loads(stored[second_id]["policy_json"])["allowed_services"],
        )

    def test_environment_provisioning_derives_python_and_node_dependencies(
        self,
    ) -> None:
        source = self.root / "source"
        source.mkdir()
        (source / "requirements.txt").write_text("Flask==2.3.2\n", encoding="utf-8")
        (source / "package.json").write_text(
            '{"scripts":{"build":"vite build"}}', encoding="utf-8"
        )
        (source / "package-lock.json").write_text(
            '{"lockfileVersion":3}', encoding="utf-8"
        )
        inspection = RepositoryInspector().inspect(source)
        sandbox_root = self.root / "sandbox" / "1"
        sandbox_root.mkdir(parents=True)
        sandbox = RecordingSandbox()
        policy = SandboxPolicy(
            persistent_root=sandbox_root,
            mounts=(Mount(source, "/mnt/inputs/source"),),
            allowed_services=(
                "files.pythonhosted.org:443",
                "pypi.org:443",
                "registry.npmjs.org:443",
            ),
        )
        evidence = SandboxEnvironmentProvisioner(
            data_root=self.root / "data",
            sandbox=sandbox,
        ).provision(
            repository_id="repo-1",
            version=1,
            source_path=source,
            sandbox_path=sandbox_root,
            inspection=inspection,
            policy=policy,
            provisioning_commands=(),
        )
        commands = [call[2] for call in sandbox.calls]
        self.assertEqual(commands[0][:3], ("python3", "-m", "venv"))
        self.assertIn("pip", commands[1])
        self.assertEqual(commands[2][:2], ("npm", "ci"))
        self.assertTrue(all(call[3]["persistent_writable"] for call in sandbox.calls))
        self.assertTrue(all(call[3]["timeout"] == 600 for call in sandbox.calls))
        services = set(sandbox.calls[0][0].allowed_services)
        self.assertIn("pypi.org:443", services)
        self.assertIn("registry.npmjs.org:443", services)
        self.assertEqual(len(evidence["commands"]), 3)

    def test_inferred_provisioning_persists_ecosystem_neutral_dependency_delta(
        self,
    ) -> None:
        source = self.root / "inferred-source"
        source.mkdir()
        (source / "Gemfile").write_text(
            "source 'https://rubygems.org'\ngem 'rspec'\n",
            encoding="utf-8",
        )
        inspection = RepositoryInspector().inspect(source)
        sandbox_root = self.root / "inferred-sandbox"
        sandbox_root.mkdir()

        class InstallingSandbox(RecordingSandbox):
            def run(
                self,
                policy: object,
                layout: object,
                command: tuple[str, ...],
                **options: object,
            ) -> object:
                result = super().run(policy, layout, command, **options)
                checkout = Path(getattr(layout, "checkout"))
                if (checkout / "Gemfile").is_file():
                    installed = (
                        Path(getattr(layout, "dependency_delta"))
                        / "ruby"
                        / "gems"
                        / "fixture.gemspec"
                    )
                    installed.parent.mkdir(parents=True, exist_ok=True)
                    installed.write_text("Gem::Specification.new\n", encoding="utf-8")
                return result

        sandbox = InstallingSandbox()
        policy = SandboxPolicy(
            persistent_root=sandbox_root,
            mounts=(Mount(source, "/mnt/inputs/source"),),
        )
        SandboxEnvironmentProvisioner(
            data_root=self.root / "inferred-data",
            sandbox=sandbox,
        ).provision(
            repository_id="repo-1",
            version=1,
            source_path=source,
            sandbox_path=sandbox_root,
            inspection=inspection,
            policy=policy,
            provisioning_commands=(("bundle", "install"),),
        )

        self.assertTrue(
            (
                sandbox_root / "dependencies" / "ruby" / "gems" / "fixture.gemspec"
            ).is_file()
        )
        later = RunLayout.create(self.root / "inferred-data", "repo-1", "run-1")
        SandboxManager().build_command(policy, later, ("bundle", "exec", "rspec"))
        restored = later.dependency_delta / "ruby" / "gems" / "fixture.gemspec"
        self.assertTrue(restored.is_symlink())
        self.assertEqual(
            str(restored.readlink()),
            "/repository-state/dependencies/ruby/gems/fixture.gemspec",
        )

    def test_supplied_inputs_are_normalized_applied_and_stored(self) -> None:
        dataset = self.root / "dataset"
        dataset.mkdir()
        sandbox = RecordingSandbox()
        resolved_references: list[str] = []

        def resolve_secret(
            reference: str, *, repository_id: str | None = None
        ) -> str:
            self.assertIsNotNone(repository_id)
            resolved_references.append(reference)
            return "resolved-package-token"

        service = OnboardingService(
            database=self.db,
            data_root=self.root / "data",
            github=FakeGitHub(self.repository),
            sources=FakeSources({"README.md": "Repository instructions.\n"}),
            inspector=RepositoryInspector(),
            team_formulator=FakeTeamFormulator(),
            provisioner=SandboxEnvironmentProvisioner(
                data_root=self.root / "data",
                sandbox=sandbox,
                secret_resolver=resolve_secret,
            ),
        )
        inputs = {
            "allowed_host_paths": [
                {
                    "path": str(dataset),
                    "target": "/mnt/inputs/dataset",
                    "mode": "read-only",
                }
            ],
            "allowed_services": ["Packages.Example.COM"],
            "secret_bindings": [
                {
                    "name": "PACKAGE_TOKEN",
                    "reference": "secret://package-token",
                    "commands": [["python3", "provision.py"]],
                }
            ],
            "provisioning_commands": [["python3", "provision.py"]],
            "validation_commands": [["python3", "-m", "unittest"]],
        }

        repository_id = service.onboard("example/demo", inputs)

        repository = service.get_repository(repository_id)
        self.assertEqual(repository["onboarding_state"], "ready")
        self.assertEqual(len(sandbox.calls), 1)
        provisioning_policy, _, command, options = sandbox.calls[0]
        self.assertEqual(command, ("python3", "provision.py"))
        self.assertIn("packages.example.com:443", provisioning_policy.allowed_services)
        self.assertEqual(provisioning_policy.allowed_secret_names, ("PACKAGE_TOKEN",))
        self.assertEqual(resolved_references, ["secret://package-token"])
        self.assertEqual(
            options["secrets"],
            {"PACKAGE_TOKEN": "resolved-package-token"},
        )
        self.assertIn(
            (dataset.resolve(), "/mnt/inputs/dataset", False),
            tuple(
                (mount.host_path, mount.sandbox_path, mount.writable)
                for mount in provisioning_policy.mounts
            ),
        )
        with self.db.connect() as connection:
            sandbox_version = connection.execute(
                "SELECT policy_json, evidence_json FROM sandbox_versions WHERE repository_id=?",
                (repository_id,),
            ).fetchone()
            validation = connection.execute(
                """SELECT command_json, source FROM validation_commands
                   ORDER BY position"""
            ).fetchall()
        stored_policy = json.loads(sandbox_version["policy_json"])
        self.assertEqual(
            stored_policy["allowed_host_paths"],
            [
                {
                    "mode": "read-only",
                    "path": str(dataset.resolve()),
                    "target": "/mnt/inputs/dataset",
                }
            ],
        )
        self.assertEqual(
            stored_policy["secret_bindings"],
            [
                {
                    "commands": [["python3", "provision.py"]],
                    "name": "PACKAGE_TOKEN",
                    "reference": "secret://package-token",
                }
            ],
        )
        stored_evidence = json.loads(sandbox_version["evidence_json"])
        self.assertEqual(
            stored_evidence["validation_commands"],
            [["python3", "-m", "unittest"]],
        )
        self.assertEqual(
            [(json.loads(row["command_json"]), row["source"]) for row in validation],
            [(["python3", "-m", "unittest"], "repository input override")],
        )

    def test_invalid_sandbox_policy_blocks_before_provisioning(self) -> None:
        first = self.root / "first-dataset"
        second = self.root / "second-dataset"
        first.mkdir()
        second.mkdir()
        sandbox = RecordingSandbox()
        service = OnboardingService(
            database=self.db,
            data_root=self.root / "data",
            github=FakeGitHub(self.repository),
            sources=FakeSources({"README.md": "Repository instructions.\n"}),
            inspector=RepositoryInspector(),
            team_formulator=FakeTeamFormulator(),
            provisioner=SandboxEnvironmentProvisioner(
                data_root=self.root / "data",
                sandbox=sandbox,
            ),
        )

        repository_id = service.onboard(
            "example/demo",
            {
                "allowed_host_paths": [
                    {
                        "path": str(first),
                        "target": "/mnt/inputs/dataset",
                    },
                    {
                        "path": str(second),
                        "target": "/mnt/inputs/dataset",
                    },
                ],
                "validation_commands": [["python3", "-m", "unittest"]],
            },
        )

        repository = service.get_repository(repository_id)
        self.assertEqual(repository["onboarding_state"], "blocked")
        self.assertIn("unique", repository["blocking_reason"])
        self.assertEqual(sandbox.calls, [])
        with self.db.connect() as connection:
            versions = connection.execute(
                "SELECT COUNT(*) FROM sandbox_versions WHERE repository_id=?",
                (repository_id,),
            ).fetchone()[0]
        self.assertEqual(versions, 0)

    def test_unsupported_secret_reference_is_rejected_before_provisioning(self) -> None:
        service, _ = self.service(
            FakeSources({"README.md": "Repository instructions.\n"})
        )

        repository_id = service.onboard(
            "example/demo",
            {
                "secret_bindings": [
                    {
                        "name": "TOKEN",
                        "reference": "env://GITHUB_TOKEN",
                        "commands": [["python3", "tool.py"]],
                    }
                ],
                "validation_commands": [["python3", "-m", "unittest"]],
            },
        )

        repository = service.get_repository(repository_id)
        self.assertEqual(repository["onboarding_state"], "blocked")
        self.assertIn("secret://", repository["blocking_reason"])

    def test_other_repository_saved_secret_needs_input_before_provisioning(self) -> None:
        from repogents.resources import RepositoryResourceStore

        foreign_repository = RepositoryInfo(
            node_id="R_foreign",
            database_id=999,
            owner="example",
            name="foreign",
            url="https://github.com/example/foreign",
            default_branch="main",
            is_private=False,
        )
        foreign_service, foreign_github = self.service(
            FakeSources({"README.md": "Repository instructions.\n"})
        )
        foreign_github.info = foreign_repository
        foreign_repository_id = foreign_service.onboard(
            "example/foreign",
            {"validation_commands": [["python3", "-m", "unittest"]]},
        )
        foreign_secret = RepositoryResourceStore(
            self.db, self.root / "data"
        ).update_secret(
            foreign_repository_id,
            name="PRODUCT_KEY",
            action="replace",
            value="foreign-product-key",
        )

        sandbox = RecordingSandbox()
        service = OnboardingService(
            database=self.db,
            data_root=self.root / "data",
            github=FakeGitHub(self.repository),
            sources=FakeSources({"README.md": "Repository instructions.\n"}),
            inspector=RepositoryInspector(),
            team_formulator=FakeTeamFormulator(),
            provisioner=SandboxEnvironmentProvisioner(
                data_root=self.root / "data",
                sandbox=sandbox,
                secret_resolver=RepositoryResourceStore(
                    self.db, self.root / "data"
                ).resolve_secret,
            ),
        )

        repository_id = service.onboard(
            "example/demo",
            {
                "secret_bindings": [
                    {
                        "name": "PRODUCT_KEY",
                        "reference": foreign_secret["reference"],
                        "commands": [["python3", "provision.py"]],
                    }
                ],
                "provisioning_commands": [["python3", "provision.py"]],
                "validation_commands": [["python3", "-m", "unittest"]],
            },
        )

        repository = service.get_repository(repository_id)
        self.assertEqual(repository["onboarding_state"], "needs_input")
        self.assertIn("saved secret PRODUCT_KEY is not configured", repository["blocking_reason"])
        self.assertEqual(sandbox.calls, [])

    def test_missing_saved_secret_needs_input_before_provisioning(self) -> None:
        sandbox = RecordingSandbox()
        service = OnboardingService(
            database=self.db,
            data_root=self.root / "data",
            github=FakeGitHub(self.repository),
            sources=FakeSources({"README.md": "Repository instructions.\n"}),
            inspector=RepositoryInspector(),
            team_formulator=FakeTeamFormulator(),
            provisioner=SandboxEnvironmentProvisioner(
                data_root=self.root / "data",
                sandbox=sandbox,
            ),
        )

        repository_id = service.onboard(
            "example/demo",
            {
                "secret_bindings": [
                    {
                        "name": "PRODUCT_KEY",
                        "reference": "secret://repository/github-123/missing-product-key",
                        "commands": [["python3", "provision.py"]],
                    }
                ],
                "provisioning_commands": [["python3", "provision.py"]],
                "validation_commands": [["python3", "-m", "unittest"]],
            },
        )

        repository = service.get_repository(repository_id)
        self.assertEqual(repository["onboarding_state"], "needs_input")
        self.assertIn("saved secret PRODUCT_KEY is not configured", repository["blocking_reason"])
        self.assertEqual(sandbox.calls, [])

    def test_repository_without_usable_validation_needs_input(self) -> None:
        service, _ = self.service(
            FakeSources({"README.md": "No validation command is discoverable.\n"})
        )

        repository_id = service.onboard("example/demo")

        repository = service.get_repository(repository_id)
        self.assertEqual(repository["onboarding_state"], "needs_input")
        self.assertIn("validation_commands", repository["blocking_reason"])
        with self.db.connect() as connection:
            versions = connection.execute(
                "SELECT COUNT(*) FROM sandbox_versions WHERE repository_id=?",
                (repository_id,),
            ).fetchone()[0]
        self.assertEqual(versions, 0)

    def test_unusable_validation_override_needs_input(self) -> None:
        service, _ = self.service(
            FakeSources({"README.md": "No discovered validation.\n"})
        )

        repository_id = service.onboard(
            "example/demo",
            {"validation_commands": [["   "]]},
        )

        repository = service.get_repository(repository_id)
        self.assertEqual(repository["onboarding_state"], "needs_input")
        self.assertIn("validation_commands", repository["blocking_reason"])
        with self.db.connect() as connection:
            commands = connection.execute(
                "SELECT COUNT(*) FROM validation_commands"
            ).fetchone()[0]
        self.assertEqual(commands, 0)

    def test_successful_onboarding_persists_ready_versions_and_inventory(self) -> None:
        service, github = self.service(
            FakeSources(
                {
                    "pyproject.toml": "[tool.pytest.ini_options]\naddopts = '-q'\n",
                    "src/app.py": "print('ok')\n",
                }
            )
        )
        repository_id = service.onboard("example/demo")
        inventory = service.list_repositories()
        self.assertEqual(github.calls, 1)
        self.assertEqual(len(inventory), 1)
        record = inventory[0]
        self.assertEqual(record["id"], repository_id)
        self.assertEqual(record["onboarding_state"], "ready")
        self.assertIsNotNone(record["current_sandbox_version_id"])
        self.assertIsNotNone(record["current_team_version_id"])
        with self.db.connect() as connection:
            commands = connection.execute(
                "SELECT command_json FROM validation_commands ORDER BY position"
            ).fetchall()
            members = connection.execute(
                """SELECT role, atomic_role, action_timeout_seconds
                   FROM team_members
                   ORDER BY CASE role
                     WHEN 'lead' THEN 0
                     WHEN 'implementer' THEN 1
                     ELSE 2
                   END"""
            ).fetchall()
            design_contract_version = connection.execute(
                """SELECT design_contract_version
                   FROM team_versions
                   WHERE id=?""",
                (record["current_team_version_id"],),
            ).fetchone()[0]
        self.assertEqual(
            json.loads(commands[0]["command_json"]),
            ["python3", "-m", "pytest"],
        )
        self.assertEqual(
            [row["role"] for row in members],
            ["lead", "implementer", "verifier"],
        )
        self.assertEqual(design_contract_version, 2)
        self.assertEqual(
            [row["atomic_role"] for row in members],
            [
                "python delivery coordinator",
                "python implementation maintainer",
                "python behavior verifier",
            ],
        )
        self.assertEqual(
            [row["action_timeout_seconds"] for row in members],
            [600, 600, 600],
        )

    def test_reopen_lists_inventory_without_rerunning_onboarding(self) -> None:
        sources = FakeSources({"go.mod": "module example.com/demo\n"})
        service, github = self.service(sources)
        service.onboard("example/demo")
        reopened = OnboardingService(
            database=Database(self.root / "repogents.sqlite3"),
            data_root=self.root / "data",
            github=github,
            sources=sources,
            inspector=RepositoryInspector(),
            team_formulator=FakeTeamFormulator(),
        )
        reopened.database.initialize()
        self.assertEqual(len(reopened.list_repositories()), 1)
        self.assertEqual(github.calls, 1)
        self.assertEqual(sources.calls, 1)

    def test_readding_archived_ready_repository_restores_stored_versions(self) -> None:
        sources = FakeSources({"go.mod": "module example.com/demo\n"})
        service, github = self.service(sources)
        repository_id = service.onboard("example/demo")
        before = service.get_repository(repository_id)
        with self.db.transaction() as connection:
            connection.execute(
                """UPDATE repositories
                   SET enabled=0, removed_at='2026-01-02T00:00:00Z'
                   WHERE id=?""",
                (repository_id,),
            )
        self.assertEqual(service.list_repositories(), [])

        restored_id = service.onboard("example/demo")

        restored = service.get_repository(repository_id)
        self.assertEqual(restored_id, repository_id)
        self.assertEqual(restored["enabled"], 1)
        self.assertIsNone(restored["removed_at"])
        self.assertEqual(
            restored["current_sandbox_version_id"],
            before["current_sandbox_version_id"],
        )
        self.assertEqual(
            restored["current_team_version_id"],
            before["current_team_version_id"],
        )
        self.assertEqual(len(service.list_repositories()), 1)
        self.assertEqual(github.calls, 2)
        self.assertEqual(sources.calls, 1)

    def test_restart_recovery_blocks_interrupted_onboarding_with_retained_inputs(
        self,
    ) -> None:
        service, github = self.service(
            FakeSources({"go.mod": "module example.com/demo\n"})
        )
        repository_id = service.onboard(
            "example/demo",
            {"allowed_services": ["api.example.com:443"]},
        )
        before = service.get_repository(repository_id)
        timestamp = "2026-07-21T00:00:00Z"
        with self.db.transaction() as connection:
            connection.execute(
                """UPDATE repositories
                   SET onboarding_state='inspecting', blocking_reason=NULL
                   WHERE id=?""",
                (repository_id,),
            )
            connection.execute(
                """INSERT INTO repositories
                   (id, github_node_id, owner, name, url, default_branch,
                    onboarding_state, inputs_json, created_at, updated_at)
                   VALUES ('github-102', 'R_second', 'example', 'second',
                           'https://github.com/example/second', 'main',
                           'provisioning', ?, ?, ?)""",
                (
                    json.dumps(
                        {"validation_commands": [["python3", "-m", "unittest"]]}
                    ),
                    timestamp,
                    timestamp,
                ),
            )
        github_calls = github.calls

        recovered = service.recover_interrupted()

        self.assertEqual(recovered, (repository_id, "github-102"))
        self.assertEqual(github.calls, github_calls)
        first = service.get_repository(repository_id)
        second = service.get_repository("github-102")
        for record, interrupted_state in (
            (first, "inspecting"),
            (second, "provisioning"),
        ):
            self.assertEqual(record["onboarding_state"], "blocked")
            self.assertIn(interrupted_state, record["blocking_reason"])
            self.assertIn("retry", record["blocking_reason"])
        self.assertEqual(first["inputs_json"], before["inputs_json"])
        self.assertEqual(
            first["current_sandbox_version_id"],
            before["current_sandbox_version_id"],
        )
        self.assertEqual(
            json.loads(second["inputs_json"]),
            {"validation_commands": [["python3", "-m", "unittest"]]},
        )

    def test_reonboarding_refreshes_metadata_and_retains_or_replaces_sanitized_inputs(
        self,
    ) -> None:
        service, github = self.service(
            FakeSources({"go.mod": "module example.com/demo\n"})
        )
        repository_id = service.onboard(
            "example/demo",
            {"allowed_services": ["API.Example.COM:443"]},
        )
        first = service.get_repository(repository_id)
        github.info = RepositoryInfo(
            node_id="R_renamed",
            database_id=101,
            owner="example-renamed",
            name="demo-renamed",
            url="https://github.com/example-renamed/demo-renamed",
            default_branch="trunk",
            is_private=False,
        )

        service.reonboard(repository_id)

        refreshed = service.get_repository(repository_id)
        self.assertEqual(refreshed["github_node_id"], "R_renamed")
        self.assertEqual(refreshed["owner"], "example-renamed")
        self.assertEqual(refreshed["name"], "demo-renamed")
        self.assertEqual(refreshed["default_branch"], "trunk")
        self.assertEqual(
            json.loads(refreshed["inputs_json"]),
            {"allowed_services": ["api.example.com:443"]},
        )
        self.assertNotEqual(
            refreshed["current_sandbox_version_id"],
            first["current_sandbox_version_id"],
        )

        service.reonboard(
            repository_id,
            {"validation_commands": [["go", "test", "./pkg/..."]]},
        )

        replaced = service.get_repository(repository_id)
        self.assertEqual(
            json.loads(replaced["inputs_json"]),
            {"validation_commands": [["go", "test", "./pkg/..."]]},
        )

    def test_explicit_reonboarding_creates_new_versions(self) -> None:
        sources = FakeSources({"Cargo.toml": "[package]\nname='demo'\n"})
        formulator = FakeTeamFormulator(
            role_prefixes=["initial", "renewed"],
        )
        service = OnboardingService(
            database=self.db,
            data_root=self.root / "data",
            github=FakeGitHub(self.repository),
            sources=sources,
            inspector=RepositoryInspector(),
            team_formulator=formulator,
        )
        repository_id = service.onboard("example/demo")
        first = service.get_repository(repository_id)
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO issues
                   (id, repository_id, github_node_id, number, url, title, body,
                    discussion_json, updated_at)
                   VALUES ('issue-1', ?, 'I1', 1, 'issue-url', 'Issue', 'Body', '[]', ?)""",
                (repository_id, "2026-01-01T00:00:00Z"),
            )
            connection.execute(
                """INSERT INTO activation_events
                   (id, repository_id, issue_id, github_event_id, applied_at)
                   VALUES ('activation-1', ?, 'issue-1', 'event-1', ?)""",
                (repository_id, "2026-01-01T00:00:00Z"),
            )
            connection.execute(
                """INSERT INTO runs
                   (id, repository_id, issue_id, activation_event_id,
                    sandbox_version_id, team_version_id, intended_base_branch,
                    base_sha, state, created_at, updated_at)
                   VALUES ('run-1', ?, 'issue-1', 'activation-1', ?, ?, 'main',
                           ?, 'queued', ?, ?)""",
                (
                    repository_id,
                    first["current_sandbox_version_id"],
                    first["current_team_version_id"],
                    "a" * 40,
                    "2026-01-01T00:00:00Z",
                    "2026-01-01T00:00:00Z",
                ),
            )
            connection.execute(
                """INSERT INTO ready_issue_discovery
                   (repository_id, status, issues_json, last_success_at,
                    last_attempt_at, error)
                   VALUES (?, 'available', ?, ?, ?, NULL)""",
                (
                    repository_id,
                    '[{"number":1,"title":"Old issue","url":"old","updated_at":"2026-01-01T00:00:00Z"}]',
                    "2026-01-01T00:00:00Z",
                    "2026-01-01T00:00:00Z",
                ),
            )
        service.reonboard(repository_id)
        second = service.get_repository(repository_id)
        with self.db.connect() as connection:
            discovery = connection.execute(
                "SELECT * FROM ready_issue_discovery WHERE repository_id=?",
                (repository_id,),
            ).fetchone()
        self.assertEqual(second["onboarding_state"], "ready")
        self.assertEqual(discovery["status"], "stale")
        self.assertIn('\"number\":1', discovery["issues_json"])
        self.assertNotEqual(
            first["current_sandbox_version_id"], second["current_sandbox_version_id"]
        )
        self.assertNotEqual(
            first["current_team_version_id"], second["current_team_version_id"]
        )
        with self.db.connect() as connection:
            sandbox_versions = connection.execute(
                "SELECT version FROM sandbox_versions ORDER BY version"
            ).fetchall()
            team_versions = connection.execute(
                "SELECT version FROM team_versions ORDER BY version"
            ).fetchall()
            run_version = connection.execute(
                "SELECT sandbox_version_id, team_version_id FROM runs WHERE id='run-1'"
            ).fetchone()
            stored_roles = connection.execute(
                """SELECT team_version_id, atomic_role
                   FROM team_members
                   WHERE team_version_id IN (?, ?)
                   ORDER BY team_version_id, stable_key""",
                (
                    first["current_team_version_id"],
                    second["current_team_version_id"],
                ),
            ).fetchall()
        self.assertEqual([row["version"] for row in sandbox_versions], [1, 2])
        self.assertEqual([row["version"] for row in team_versions], [1, 2])
        self.assertEqual(
            run_version["sandbox_version_id"],
            first["current_sandbox_version_id"],
        )
        self.assertEqual(
            run_version["team_version_id"],
            first["current_team_version_id"],
        )
        roles_by_version: dict[str, list[str]] = {}
        for row in stored_roles:
            roles_by_version.setdefault(str(row["team_version_id"]), []).append(
                str(row["atomic_role"])
            )
        self.assertTrue(
            all(
                role.startswith("initial ")
                for role in roles_by_version[first["current_team_version_id"]]
            )
        )
        self.assertTrue(
            all(
                role.startswith("renewed ")
                for role in roles_by_version[second["current_team_version_id"]]
            )
        )
        self.assertEqual(len(formulator.inspections), 2)

    def test_missing_input_and_other_failure_remain_visible(self) -> None:
        missing_service, _ = self.service(
            FakeSources(error=MissingRepositoryInput("license", "SDK license path"))
        )
        missing_id = missing_service.onboard("example/demo")
        missing = missing_service.get_repository(missing_id)
        self.assertEqual(missing["onboarding_state"], "needs_input")
        self.assertIn("SDK license path", missing["blocking_reason"])

        other_repo = RepositoryInfo(
            node_id="R_other",
            database_id=102,
            owner="example",
            name="other",
            url="https://github.com/example/other",
            default_branch="main",
            is_private=False,
        )
        blocked = OnboardingService(
            database=self.db,
            data_root=self.root / "data",
            github=FakeGitHub(other_repo),
            sources=FakeSources(error=RuntimeError("clone failed")),
            inspector=RepositoryInspector(),
            team_formulator=FakeTeamFormulator(),
        )
        blocked_id = blocked.onboard("example/other")
        record = blocked.get_repository(blocked_id)
        self.assertEqual(record["onboarding_state"], "blocked")
        self.assertIn("clone failed", record["blocking_reason"])
        self.assertEqual(len(blocked.list_repositories()), 2)

    def test_missing_pinned_artifact_revision_needs_input(self) -> None:
        from repogents.resources import RepositoryResourceStore

        service, _ = self.service(FakeSources())
        validation_commands = [["python", "-c", "pass"]]
        repository_id = service.onboard(
            "example/demo", {"validation_commands": validation_commands}
        )
        store = RepositoryResourceStore(self.db, self.root / "data")
        artifact = store.upload_artifact(
            repository_id,
            name="fixture-sdk",
            description="Licensed fixture SDK",
            content=b"fixture revision one",
        )
        store.remove_artifact_revision(
            repository_id, artifact["name"], int(artifact["revision"])
        )

        service.reonboard(
            repository_id,
            {
                "artifact_bindings": [artifact],
                "validation_commands": validation_commands,
            },
        )

        repository = service.get_repository(repository_id)
        self.assertEqual(repository["onboarding_state"], "needs_input")
        self.assertIn("artifact fixture-sdk revision 1", repository["blocking_reason"])
        self.assertIn("missing or inaccessible", repository["blocking_reason"])

    def test_reonboarding_pins_distinct_artifact_revisions_without_bytes_in_evidence(
        self,
    ) -> None:
        from repogents.resources import RepositoryResourceStore

        service, _ = self.service(FakeSources())
        validation_commands = [["python", "-c", "pass"]]
        repository_id = service.onboard(
            "example/demo", {"validation_commands": validation_commands}
        )
        store = RepositoryResourceStore(self.db, self.root / "data")
        first = store.upload_artifact(
            repository_id,
            name="fixture-sdk",
            description="Licensed fixture SDK",
            content=b"private fixture revision one",
        )
        service.reonboard(
            repository_id,
            {
                "artifact_bindings": [first],
                "validation_commands": validation_commands,
            },
        )
        first_repository = service.get_repository(repository_id)
        first_sandbox_id = first_repository["current_sandbox_version_id"]

        second = store.upload_artifact(
            repository_id,
            name="fixture-sdk",
            description="Licensed fixture SDK replacement",
            content=b"private fixture revision two",
        )
        service.reonboard(
            repository_id,
            {
                "artifact_bindings": [second],
                "validation_commands": validation_commands,
            },
        )
        second_repository = service.get_repository(repository_id)
        second_sandbox_id = second_repository["current_sandbox_version_id"]

        self.assertNotEqual(first_sandbox_id, second_sandbox_id)
        with self.db.connect() as connection:
            pinned = connection.execute(
                """SELECT sandbox_artifact_revisions.sandbox_version_id,
                          artifact_revisions.revision, sandbox_versions.evidence_json
                   FROM sandbox_artifact_revisions
                   JOIN artifact_revisions
                     ON artifact_revisions.id=sandbox_artifact_revisions.artifact_revision_id
                   JOIN sandbox_versions
                     ON sandbox_versions.id=sandbox_artifact_revisions.sandbox_version_id
                   WHERE sandbox_versions.repository_id=?
                   ORDER BY artifact_revisions.revision""",
                (repository_id,),
            ).fetchall()
        self.assertEqual(
            [(row["sandbox_version_id"], row["revision"]) for row in pinned],
            [(first_sandbox_id, 1), (second_sandbox_id, 2)],
        )
        for row in pinned:
            evidence = json.loads(row["evidence_json"])
            projected = evidence["resources"]["artifacts"][0]
            self.assertIn("description", projected)
            self.assertIn("sandbox_path", projected)
            self.assertNotIn("storage_path", projected)
            self.assertNotIn("private fixture revision", row["evidence_json"])


    def test_reonboarding_resolves_artifact_metadata_and_storage_from_durable_revision(
        self,
    ) -> None:
        from repogents.resources import RepositoryResourceStore

        service, _ = self.service(FakeSources())
        validation_commands = [["python", "-c", "pass"]]
        repository_id = service.onboard(
            "example/demo", {"validation_commands": validation_commands}
        )
        store = RepositoryResourceStore(self.db, self.root / "data")
        uploaded = store.upload_artifact(
            repository_id,
            name="fixture-sdk",
            description="Controller-owned fixture",
            content=b"authoritative artifact bytes",
        )
        spoofed = dict(uploaded)
        spoofed.update(
            {
                "description": "Caller-controlled description",
                "content_hash": "sha256:" + "0" * 64,
                "size": 999999,
                "created_at": "1900-01-01T00:00:00Z",
                "storage_path": str(self.root / "caller-controlled.bin"),
            }
        )

        service.reonboard(
            repository_id,
            {
                "artifact_bindings": [spoofed],
                "validation_commands": validation_commands,
            },
        )

        repository = service.get_repository(repository_id)
        self.assertEqual(repository["onboarding_state"], "ready")
        with self.db.connect() as connection:
            sandbox = connection.execute(
                "SELECT policy_json, evidence_json FROM sandbox_versions WHERE id=?",
                (repository["current_sandbox_version_id"],),
            ).fetchone()
        policy_artifact = json.loads(sandbox["policy_json"])["artifact_bindings"][0]
        evidence_artifact = json.loads(sandbox["evidence_json"])["resources"]["artifacts"][0]
        self.assertEqual(policy_artifact["storage_path"], uploaded["storage_path"])
        self.assertEqual(policy_artifact["content_hash"], uploaded["content_hash"])
        self.assertEqual(policy_artifact["size"], uploaded["size"])
        self.assertEqual(evidence_artifact["description"], "Controller-owned fixture")
        self.assertNotIn("Caller-controlled", sandbox["evidence_json"])

    def test_reonboarding_rejects_tampered_durable_artifact_bytes(self) -> None:
        from repogents.resources import RepositoryResourceStore

        service, _ = self.service(FakeSources())
        validation_commands = [["python", "-c", "pass"]]
        repository_id = service.onboard(
            "example/demo", {"validation_commands": validation_commands}
        )
        store = RepositoryResourceStore(self.db, self.root / "data")
        uploaded = store.upload_artifact(
            repository_id,
            name="fixture-sdk",
            description="Controller-owned fixture",
            content=b"original artifact bytes",
        )
        artifact_path = Path(uploaded["storage_path"])
        artifact_path.chmod(0o600)
        artifact_path.write_bytes(b"tampered artifact bytes")

        service.reonboard(
            repository_id,
            {
                "artifact_bindings": [uploaded],
                "validation_commands": validation_commands,
            },
        )

        repository = service.get_repository(repository_id)
        self.assertEqual(repository["onboarding_state"], "needs_input")
        self.assertIn("failed its stored integrity check", repository["blocking_reason"])


if __name__ == "__main__":
    unittest.main()
