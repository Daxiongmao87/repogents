import copy
import sqlite3

import pytest

from repogents.store import Store


RUN_STATES = [
    "QUEUED",
    "SPECIFYING",
    "EXECUTING",
    "WAITING_FOR_WORK_COMPLETION",
    "VALIDATING",
    "CREATING_PR",
    "PR_LISTENING",
    "PENDING_MERGE",
    "COMPLETED",
    "CLOSED",
]


@pytest.fixture
def store_path(tmp_path):
    return tmp_path / "store.sqlite"


@pytest.fixture
def store(store_path):
    return Store(store_path)


def add_repository(store, name="owner/project"):
    return store.add_repository(name, "main", 0.72)


def add_run(store, repository_id, issue_number=17):
    run, created = store.create_run(
        repository_id,
        issue_number,
        {"number": issue_number, "title": "Repair the service", "labels": ["agent:ready"]},
    )
    assert created is True
    return run


def result_payload(label="done"):
    return {
        "output": {"summary": label},
        "artifacts": [{"path": "artifact.txt"}],
        "test_results": {"passed": 1, "failed": 0},
        "repository_state": {"head": f"sha-{label}"},
    }


def dependency_evidence(*dependencies):
    return [
        {
            "dependency": dependency,
            "reason": f"{dependency} must provide its outcome first.",
            "evidence": [f"The graph declares {dependency} as a prerequisite."],
        }
        for dependency in dependencies
    ]


def specification_package():
    return {
        "specifications": [
            {
                "key": "context",
                "title": "Retain repository context",
                "description": "The existing context remains valid.",
                "acceptance_criteria": ["The context is retained."],
                "dependencies": [],
                "dependency_evidence": [],
                "executable": False,
                "work_items": [],
            },
            {
                "key": "repair",
                "title": "Repair the service",
                "description": "Implement and verify the requested repair.",
                "acceptance_criteria": ["The repair works.", "The regression is covered."],
                "dependencies": ["context"],
                "dependency_evidence": dependency_evidence("context"),
                "executable": True,
                "work_items": [
                    {
                        "key": "verify",
                        "title": "Verify the repair",
                        "description": "Exercise the repaired behavior.",
                        "classification": "quality/testing",
                        "dependencies": ["implement"],
                        "dependency_evidence": dependency_evidence("implement"),
                    },
                    {
                        "key": "implement",
                        "title": "Implement the repair",
                        "description": "Change the repository behavior.",
                        "classification": "backend/python",
                        "dependencies": [],
                        "dependency_evidence": [],
                    },
                ],
            },
        ]
    }


def single_work_package(key="root", classification="backend/python"):
    return {
        "specifications": [
            {
                "key": "spec",
                "title": "One atomic change",
                "description": "Complete one independently understandable change.",
                "acceptance_criteria": ["The change is complete."],
                "dependencies": [],
                "dependency_evidence": [],
                "executable": True,
                "work_items": [
                    {
                        "key": key,
                        "title": "Complete the change",
                        "description": "Implement the atomic change.",
                        "classification": classification,
                        "dependencies": [],
                        "dependency_evidence": [],
                    }
                ],
            }
        ]
    }


def invalid_package_cases():
    cases = [
        ("missing specifications", {}),
        ("empty specifications", {"specifications": []}),
    ]
    for field in (
        "key",
        "title",
        "description",
        "acceptance_criteria",
        "dependencies",
        "dependency_evidence",
        "executable",
        "work_items",
    ):
        package = specification_package()
        package["specifications"][1].pop(field)
        cases.append((f"missing specification {field}", package))
    for field in (
        "key",
        "title",
        "description",
        "classification",
        "dependencies",
        "dependency_evidence",
    ):
        package = specification_package()
        package["specifications"][1]["work_items"][0].pop(field)
        cases.append((f"missing work {field}", package))

    package = specification_package()
    package["specifications"][1]["acceptance_criteria"] = []
    cases.append(("empty acceptance criteria", package))

    package = specification_package()
    package["specifications"][1]["work_items"] = []
    cases.append(("executable without work", package))

    package = specification_package()
    package["specifications"][1]["key"] = "context"
    cases.append(("duplicate specification key", package))

    package = specification_package()
    package["specifications"][1]["dependencies"] = ["missing"]
    cases.append(("unknown specification dependency", package))

    package = specification_package()
    package["specifications"][1]["work_items"][0]["key"] = "implement"
    cases.append(("duplicate work key", package))

    package = specification_package()
    package["specifications"][1]["work_items"][0]["dependencies"] = ["missing"]
    cases.append(("unknown work dependency", package))

    for label in ("", "backend/", "one/two/three"):
        package = specification_package()
        package["specifications"][1]["work_items"][0]["classification"] = label
        cases.append((f"invalid classification {label!r}", package))
    return cases


def dependency_cycle_cases():
    cases = []

    package = specification_package()
    package["specifications"][0]["dependencies"] = ["context"]
    package["specifications"][0]["dependency_evidence"] = dependency_evidence(
        "context"
    )
    cases.append(("self-dependent specification", package))

    package = specification_package()
    package["specifications"][0]["dependencies"] = ["repair"]
    package["specifications"][0]["dependency_evidence"] = dependency_evidence(
        "repair"
    )
    cases.append(("cyclic specifications", package))

    package = specification_package()
    package["specifications"][1]["work_items"][0]["dependencies"] = ["verify"]
    package["specifications"][1]["work_items"][0][
        "dependency_evidence"
    ] = dependency_evidence("verify")
    cases.append(("self-dependent work", package))

    package = specification_package()
    package["specifications"][1]["work_items"][1]["dependencies"] = ["verify"]
    package["specifications"][1]["work_items"][1][
        "dependency_evidence"
    ] = dependency_evidence("verify")
    cases.append(("cyclic work", package))

    return cases


def dependency_scheduling_package():
    return {
        "specifications": [
            {
                "key": "prepare",
                "title": "Prepare the repair",
                "description": "Prepare both inputs required by the repair.",
                "acceptance_criteria": ["Both inputs are prepared."],
                "dependencies": [],
                "dependency_evidence": [],
                "executable": True,
                "work_items": [
                    {
                        "key": "prepare-one",
                        "title": "Prepare the first input",
                        "description": "Prepare the first repair input.",
                        "classification": "backend",
                        "dependencies": [],
                        "dependency_evidence": [],
                    },
                    {
                        "key": "prepare-two",
                        "title": "Prepare the second input",
                        "description": "Prepare the second repair input.",
                        "classification": "backend",
                        "dependencies": [],
                        "dependency_evidence": [],
                    },
                ],
            },
            {
                "key": "repair",
                "title": "Complete the repair",
                "description": "Use the prepared inputs to complete the repair.",
                "acceptance_criteria": ["The repair is complete."],
                "dependencies": ["prepare"],
                "dependency_evidence": dependency_evidence("prepare"),
                "executable": True,
                "work_items": [
                    {
                        "key": "foundation",
                        "title": "Build the foundation",
                        "description": "Build the downstream foundation.",
                        "classification": "quality",
                        "dependencies": [],
                        "dependency_evidence": [],
                    },
                    {
                        "key": "dependent",
                        "title": "Finish the dependent work",
                        "description": "Finish work after the foundation.",
                        "classification": "quality",
                        "dependencies": ["foundation"],
                        "dependency_evidence": dependency_evidence("foundation"),
                    },
                ],
            },
        ]
    }


def test_repository_crud_creates_permanent_graph_and_preserves_history(store, store_path):
    repository = add_repository(store)

    assert repository["github_repository"] == "owner/project"
    assert repository["target_branch"] == "main"
    assert repository["similarity_threshold"] == pytest.approx(0.72)
    assert repository["autonomous_issue_intake"] is False
    assert repository["tracked"] is True
    assert store.list_repositories() == [repository]
    assert store.get_repository(repository["id"]) == repository

    enabled = store.set_autonomous_issue_intake(repository["id"], True)
    assert enabled["autonomous_issue_intake"] is True
    disabled = store.set_autonomous_issue_intake(repository["id"], False)
    assert disabled["autonomous_issue_intake"] is False

    nodes = store.list_nodes(repository["id"])
    assert [(node["classification"], node["persistence"]) for node in nodes] == [
        ("Specify", "PERMANENT"),
        ("Validate", "PERMANENT"),
    ]
    assert all(node["vector"] is None and node["active"] is True for node in nodes)
    assert store.list_dynamic_nodes(repository["id"]) == []

    run = add_run(store, repository["id"])
    store.remove_repository(repository["id"])

    assert store.list_repositories() == []
    assert store.get_repository(repository["id"])["tracked"] is False
    assert store.get_run(run["id"])["issue_json"]["title"] == "Repair the service"
    assert len(store.list_nodes(repository["id"])) == 2
    with pytest.raises(KeyError):
        store.set_autonomous_issue_intake(repository["id"], True)

    with sqlite3.connect(store_path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"

    with pytest.raises(sqlite3.IntegrityError):
        store.create_run(999_999, 1, {"number": 1})


def test_existing_repository_migrates_to_non_autonomous_issue_intake(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE repositories (
                id INTEGER PRIMARY KEY,
                github_repository TEXT NOT NULL UNIQUE,
                target_branch TEXT NOT NULL,
                similarity_threshold REAL NOT NULL,
                tracked INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        connection.execute(
            """
            INSERT INTO repositories(
                github_repository, target_branch, similarity_threshold
            ) VALUES ('owner/legacy', 'main', 0.75)
            """
        )

    migrated = Store(path).get_repository(1)

    assert migrated["autonomous_issue_intake"] is False


def test_classification_vectors_normalize_update_and_initialize_existing_database(
    store, store_path
):
    repository = add_repository(store)
    run = add_run(store, repository["id"])
    with sqlite3.connect(store_path) as connection:
        connection.execute("DROP TABLE IF EXISTS classification_vectors")

    reopened = Store(store_path)

    assert reopened.get_classification_vector(repository["id"], " backend/python ") is None
    assert reopened.save_classification_vector(
        repository["id"], " backend/python ", [1, 2.5]
    ) == [1.0, 2.5]
    assert reopened.get_classification_vector(
        repository["id"], "backend/python"
    ) == [1.0, 2.5]

    assert reopened.save_classification_vector(
        repository["id"], "backend/python", [3, 4]
    ) == [3.0, 4.0]
    assert reopened.get_classification_vector(
        repository["id"], " backend/python "
    ) == [3.0, 4.0]
    with sqlite3.connect(store_path) as connection:
        rows = connection.execute(
            """
            SELECT classification FROM classification_vectors
            WHERE repository_id = ?
            """,
            (repository["id"],),
        ).fetchall()
    assert rows == [("backend/python",)]
    assert reopened.get_run(run["id"])["issue_json"]["title"] == "Repair the service"


def test_active_run_deduplication_exact_states_and_terminal_release(store):
    repository = add_repository(store)
    run, created = store.create_run(repository["id"], 23, {"number": 23, "version": 1})
    duplicate, duplicate_created = store.create_run(
        repository["id"], 23, {"number": 23, "version": 2}
    )

    assert created is True
    assert run["state"] == "QUEUED"
    assert run["issue_json"] == {"number": 23, "version": 1}
    assert duplicate_created is False
    assert duplicate == run
    assert store.list_runs(repository["id"]) == [run]

    for state in RUN_STATES[1:-2]:
        fields = {}
        if state == "CREATING_PR":
            fields = {
                "branch": "agent/issue-23",
                "pull_request": {"number": 41, "url": "https://example.test/pull/41"},
            }
        run = store.transition_run(run["id"], state, **fields)
        assert run["state"] == state

    assert run["branch"] == "agent/issue-23"
    assert run["pull_request"] == {"number": 41, "url": "https://example.test/pull/41"}
    completed = store.transition_run(run["id"], "COMPLETED")
    assert completed["state"] == "COMPLETED"
    with pytest.raises(ValueError, match="terminal"):
        store.transition_run(run["id"], "QUEUED")

    replacement, replacement_created = store.create_run(
        repository["id"], 23, {"number": 23, "version": 3}
    )
    assert replacement_created is True
    assert replacement["id"] != run["id"]
    closed = store.transition_run(replacement["id"], "CLOSED")
    assert closed["state"] == "CLOSED"

    third, third_created = store.create_run(repository["id"], 23, {"number": 23, "version": 4})
    assert third_created is True
    with pytest.raises(ValueError, match="state"):
        store.transition_run(third["id"], "DONE")


def test_pr_listening_since_is_nullable_and_durable(store, store_path):
    repository = add_repository(store)
    run = add_run(store, repository["id"])

    assert run["pr_listening_since"] is None
    listening = store.transition_run(
        run["id"], "PR_LISTENING", pr_listening_since=1234.5
    )

    assert listening["pr_listening_since"] == pytest.approx(1234.5)
    assert Store(store_path).get_run(run["id"])["pr_listening_since"] == pytest.approx(
        1234.5
    )

    cleared = store.transition_run(
        run["id"], "PR_LISTENING", pr_listening_since=None
    )
    assert cleared["pr_listening_since"] is None
    with pytest.raises(ValueError, match="pr_listening_since"):
        store.transition_run(
            run["id"], "PR_LISTENING", pr_listening_since="not-a-timestamp"
        )


def test_issue_order_plan_is_durable_and_replaced_atomically(store, store_path):
    repository = add_repository(store)
    first_snapshot = [{"number": 7, "title": "First"}]
    first_result = {"ordered_issues": [{"issue_number": 7}]}

    saved = store.save_issue_order_plan(
        repository["id"], first_snapshot, first_result
    )

    assert saved == {
        "repository_id": repository["id"],
        "issue_snapshot": first_snapshot,
        "result": first_result,
    }
    reopened = Store(store_path)
    assert reopened.get_issue_order_plan(repository["id"]) == saved

    second_snapshot = [
        {"number": 7, "title": "First"},
        {"number": 8, "title": "Second"},
    ]
    second_result = {
        "ordered_issues": [{"issue_number": 8}, {"issue_number": 7}]
    }
    replaced = reopened.save_issue_order_plan(
        repository["id"], second_snapshot, second_result
    )
    assert replaced["issue_snapshot"] == second_snapshot
    assert replaced["result"] == second_result


def test_pass_creation_and_run_transition_are_atomic_and_guard_latest_pass(store):
    repository = add_repository(store)
    run = add_run(store, repository["id"])
    first_pass = store.create_pass(run["id"], "issue", {})

    adaptive_pass = store.create_pass_and_transition(
        run["id"],
        first_pass["id"],
        "work_failure",
        {"failed_pass_id": first_pass["id"]},
        "SPECIFYING",
    )

    assert adaptive_pass["trigger_type"] == "work_failure"
    assert store.get_run(run["id"])["state"] == "SPECIFYING"
    with pytest.raises(ValueError, match="latest execution pass changed"):
        store.create_pass_and_transition(
            run["id"],
            first_pass["id"],
            "work_failure",
            {"failed_pass_id": first_pass["id"]},
            "SPECIFYING",
        )
    assert store.list_passes(run["id"]) == [first_pass, adaptive_pass]


@pytest.mark.parametrize(
    ("case_name", "package"),
    invalid_package_cases(),
    ids=[case[0] for case in invalid_package_cases()],
)
def test_specification_gate_rejects_incomplete_package_atomically(
    store, case_name, package
):
    repository = add_repository(store)
    run = add_run(store, repository["id"])
    execution_pass = store.create_pass(run["id"], "ISSUE", {"source": "agent:ready"})

    with pytest.raises(ValueError):
        store.save_specification_package(run["id"], execution_pass["id"], copy.deepcopy(package))

    assert store.list_specifications(run["id"]) == [], case_name
    assert store.list_work_items(run["id"]) == [], case_name


@pytest.mark.parametrize(
    ("case_name", "package"),
    dependency_cycle_cases(),
    ids=[case[0] for case in dependency_cycle_cases()],
)
def test_specification_gate_rejects_self_and_cyclic_dependencies_atomically(
    store, case_name, package
):
    repository = add_repository(store)
    run = add_run(store, repository["id"])
    execution_pass = store.create_pass(run["id"], "ISSUE", {})

    with pytest.raises(ValueError, match="acyclic"):
        store.save_specification_package(
            run["id"], execution_pass["id"], copy.deepcopy(package)
        )

    assert store.list_specifications(run["id"]) == [], case_name
    assert store.list_work_items(run["id"]) == [], case_name


@pytest.mark.parametrize(
    "mutate",
    [
        lambda package: package["specifications"][1].update(
            dependency_evidence=[]
        ),
        lambda package: package["specifications"][1]["work_items"][0].update(
            dependency_evidence=[]
        ),
        lambda package: package["specifications"][1].update(
            dependency_evidence=[
                {
                    "dependency": "context",
                    "reason": "",
                    "evidence": ["Observed prerequisite."],
                }
            ]
        ),
        lambda package: package["specifications"][1].update(
            dependency_evidence=[
                {
                    "dependency": "context",
                    "reason": "Context is required.",
                    "evidence": [],
                }
            ]
        ),
    ],
)
def test_dependency_evidence_fails_closed_and_atomically(store, mutate):
    repository = add_repository(store)
    run = add_run(store, repository["id"])
    execution_pass = store.create_pass(run["id"], "ISSUE", {})
    package = specification_package()
    mutate(package)

    with pytest.raises(ValueError, match="dependency"):
        store.save_specification_package(
            run["id"], execution_pass["id"], package
        )

    assert store.list_specifications(run["id"]) == []
    assert store.list_work_items(run["id"]) == []


def test_dependency_evidence_order_is_normalized_to_dependency_order(store):
    repository = add_repository(store)
    run = add_run(store, repository["id"])
    execution_pass = store.create_pass(run["id"], "ISSUE", {})
    package = dependency_scheduling_package()
    dependent = package["specifications"][1]["work_items"][1]
    dependent["dependencies"] = ["prepare-one", "foundation"]
    dependent["dependency_evidence"] = dependency_evidence(
        "foundation", "prepare-one"
    )

    saved = store.save_specification_package(
        run["id"], execution_pass["id"], package
    )

    saved_dependent = next(
        item for item in saved["work_items"] if item["key"] == "dependent"
    )
    assert [
        item["dependency"]
        for item in saved_dependent["dependency_evidence"]
    ] == ["prepare-one", "foundation"]


def test_pass_and_complete_package_persistence_returns_decoded_rows(store, store_path):
    repository = add_repository(store)
    run = add_run(store, repository["id"])
    first_pass = store.create_pass(run["id"], "ISSUE", {"issue_version": 1})

    saved = store.save_specification_package(
        run["id"], first_pass["id"], specification_package()
    )

    assert first_pass["trigger_json"] == {"issue_version": 1}
    assert [specification["key"] for specification in saved["specifications"]] == [
        "context",
        "repair",
    ]
    assert saved["specifications"][1]["acceptance_criteria"] == [
        "The repair works.",
        "The regression is covered.",
    ]
    assert saved["specifications"][1]["dependencies"] == ["context"]
    assert saved["specifications"][1]["executable"] is True
    assert [work["key"] for work in saved["work_items"]] == ["verify", "implement"]
    assert all(work["state"] == "UNASSIGNED" for work in saved["work_items"])
    assert saved["work_items"][0]["dependencies"] == ["implement"]
    assert store.list_specifications(run["id"]) == saved["specifications"]
    assert store.list_work_items(run["id"], first_pass["id"]) == saved["work_items"]

    second_pass = store.create_pass(run["id"], "FEEDBACK", {"external_id": "review:9"})
    second_saved = store.save_specification_package(
        run["id"], second_pass["id"], single_work_package("correct")
    )
    assert len(store.list_work_items(run["id"])) == 3
    assert store.list_work_items(run["id"], second_pass["id"]) == second_saved["work_items"]

    reopened = Store(store_path)
    assert reopened.list_passes(run["id"]) == [first_pass, second_pass]
    assert reopened.list_specifications(run["id"])[0]["acceptance_criteria"] == [
        "The context is retained."
    ]
    assert reopened.list_work_items(run["id"])[0]["dependencies"] == ["implement"]
    assert reopened.list_work_items(run["id"])[0]["dependency_evidence"] == (
        dependency_evidence("implement")
    )


def test_node_queues_claim_dependency_ready_work_and_retain_busy_queue(store):
    repository = add_repository(store)
    run = add_run(store, repository["id"])
    execution_pass = store.create_pass(run["id"], "ISSUE", {})
    saved = store.save_specification_package(
        run["id"], execution_pass["id"], specification_package()
    )
    work_by_key = {work["key"]: work for work in saved["work_items"]}

    backend = store.create_dynamic_node(
        repository["id"], "backend/python", [1, 0], "Work flexibly on Python backend changes."
    )
    quality = store.create_dynamic_node(
        repository["id"], "quality/testing", [0, 1], "Verify behavior with repository tools."
    )
    assert backend["vector"] == [1.0, 0.0]
    assert backend["persistence"] == "EPHEMERAL"
    assert store.list_dynamic_nodes(repository["id"]) == [backend, quality]

    queued_verify = store.assign_work(work_by_key["verify"]["id"], backend["id"])
    queued_implement = store.assign_work(work_by_key["implement"]["id"], backend["id"])
    assert queued_verify["state"] == queued_implement["state"] == "QUEUED"

    claimed_implement = store.claim_node_work(backend["id"], run["id"])
    assert claimed_implement["key"] == "implement"
    assert claimed_implement["state"] == "RUNNING"
    assert store.claim_node_work(backend["id"], run["id"]) is None

    independent_pass = store.create_pass(run["id"], "ADDITIONAL_WORK", {})
    independent = store.save_specification_package(
        run["id"], independent_pass["id"], single_work_package("independent", "quality/testing")
    )["work_items"][0]
    store.assign_work(independent["id"], quality["id"])
    claimed_independent = store.claim_node_work(quality["id"], run["id"])
    assert claimed_independent["key"] == "independent"
    failed = store.fail_work(independent["id"], result_payload("failed"))
    assert failed["state"] == "FAILED"
    assert failed["result"]["output"] == {"summary": "failed"}

    assert store.complete_work(
        claimed_implement["id"], result_payload("implemented")
    ) is None
    claimed_verify = store.claim_node_work(backend["id"], run["id"])
    assert claimed_verify["key"] == "verify"
    assert store.complete_work(claimed_verify["id"], result_payload("verified")) is None

    current = {work["key"]: work for work in store.list_work_items(run["id"])}
    assert current["implement"]["state"] == "COMPLETED"
    assert current["implement"]["result"]["repository_state"] == {"head": "sha-implemented"}
    assert current["verify"]["state"] == "COMPLETED"
    assert store.validation_barrier_ready(run["id"], execution_pass["id"]) is True


def test_claim_node_work_requires_and_filters_the_exact_run(store):
    repository = add_repository(store)
    first_run = add_run(store, repository["id"], issue_number=1)
    second_run = add_run(store, repository["id"], issue_number=2)
    first_pass = store.create_pass(first_run["id"], "ISSUE", {})
    second_pass = store.create_pass(second_run["id"], "ISSUE", {})
    first_work = store.save_specification_package(
        first_run["id"], first_pass["id"], single_work_package("first")
    )["work_items"][0]
    second_work = store.save_specification_package(
        second_run["id"], second_pass["id"], single_work_package("second")
    )["work_items"][0]
    node = store.create_dynamic_node(
        repository["id"], "backend/python", [1], "Handle focused backend work."
    )
    store.assign_work(first_work["id"], node["id"])
    store.assign_work(second_work["id"], node["id"])

    with pytest.raises(TypeError):
        store.claim_node_work(node["id"])

    claimed_second = store.claim_node_work(node["id"], second_run["id"])
    assert claimed_second["id"] == second_work["id"]
    assert claimed_second["run_id"] == second_run["id"]
    store.complete_work(claimed_second["id"], result_payload("second"))

    claimed_first = store.claim_node_work(node["id"], first_run["id"])
    assert claimed_first["id"] == first_work["id"]
    assert claimed_first["run_id"] == first_run["id"]


def test_failure_blocks_dependents_but_preserves_independent_work(store):
    repository = add_repository(store)
    run = add_run(store, repository["id"])
    execution_pass = store.create_pass(run["id"], "ISSUE", {})
    saved = store.save_specification_package(
        run["id"], execution_pass["id"], dependency_scheduling_package()
    )
    work_by_key = {work["key"]: work for work in saved["work_items"]}
    backend = store.create_dynamic_node(repository["id"], "backend", [1], "Prepare inputs.")
    quality = store.create_dynamic_node(repository["id"], "quality", [1], "Complete work.")

    for key in ("prepare-one", "prepare-two"):
        store.assign_work(work_by_key[key]["id"], backend["id"])
    for key in ("foundation", "dependent"):
        store.assign_work(work_by_key[key]["id"], quality["id"])

    assert store.claim_node_work(quality["id"], run["id"]) is None
    first = store.claim_node_work(backend["id"], run["id"])
    assert first["key"] == "prepare-one"
    store.fail_work(first["id"], result_payload("prepare-one-failed"))
    assert store.claim_node_work(quality["id"], run["id"]) is None
    second = store.claim_node_work(backend["id"], run["id"])
    assert second["key"] == "prepare-two"
    assert store.validation_barrier_ready(run["id"], execution_pass["id"]) is False

    assert store.settle_failed_pass_work(
        run["id"],
        execution_pass["id"],
        result_payload("blocked-by-work-failure"),
    ) is False
    store.complete_work(second["id"], result_payload("prepare-two-completed"))
    assert store.settle_failed_pass_work(
        run["id"],
        execution_pass["id"],
        result_payload("blocked-by-work-failure"),
    ) is True
    current = {
        work["key"]: work
        for work in store.list_work_items(run["id"], execution_pass["id"])
    }
    assert current["prepare-one"]["state"] == "FAILED"
    assert current["prepare-two"]["state"] == "COMPLETED"
    assert current["foundation"]["state"] == "FAILED"
    assert current["dependent"]["state"] == "FAILED"
    assert current["foundation"]["result"]["output"] == {
        "summary": "blocked-by-work-failure"
    }


def test_claim_satisfies_nonexecutable_specification_dependency_without_work(store):
    repository = add_repository(store)
    run = add_run(store, repository["id"])
    execution_pass = store.create_pass(run["id"], "ISSUE", {})
    saved = store.save_specification_package(
        run["id"], execution_pass["id"], specification_package()
    )
    implement = next(work for work in saved["work_items"] if work["key"] == "implement")
    node = store.create_dynamic_node(
        repository["id"], "backend/python", [1], "Complete the repair."
    )

    store.assign_work(implement["id"], node["id"])

    assert store.claim_node_work(node["id"], run["id"])["key"] == "implement"


def test_handoff_is_atomic_and_keeps_barrier_closed_until_child_finishes(store):
    repository = add_repository(store)
    run = add_run(store, repository["id"])
    execution_pass = store.create_pass(run["id"], "ISSUE", {})
    root = store.save_specification_package(
        run["id"], execution_pass["id"], single_work_package()
    )["work_items"][0]
    node = store.create_dynamic_node(
        repository["id"], "backend/python", [0.3, 0.7], "Handle repository backend work."
    )
    store.assign_work(root["id"], node["id"])
    store.claim_node_work(node["id"], run["id"])

    with pytest.raises(ValueError):
        store.complete_work(
            root["id"],
            result_payload("partial"),
            {
                "classification": "quality/testing",
                "context": {},
                "artifacts": [],
                "dependencies": [],
                "dependency_evidence": [],
            },
        )
    assert store.list_work_items(run["id"])[0]["state"] == "RUNNING"

    handoff = {
        "classification": "quality/testing",
        "context": {"summary": "Implementation is ready for targeted verification."},
        "artifacts": [{"path": "artifact.txt"}],
        "dependencies": ["root"],
        "dependency_evidence": dependency_evidence("root"),
        "blocking": {"until": "root output is available"},
    }
    child = store.complete_work(root["id"], result_payload("partial"), handoff)

    assert child["state"] == "UNASSIGNED"
    assert child["specification_id"] == root["specification_id"]
    assert child["parent_work_id"] == root["id"]
    assert child["classification"] == "quality/testing"
    assert child["dependencies"] == ["root"]
    assert child["handoff"] == handoff
    parent = next(work for work in store.list_work_items(run["id"]) if work["id"] == root["id"])
    assert parent["state"] == "HANDED_OFF"
    assert parent["result"] == result_payload("partial")
    assert store.validation_barrier_ready(run["id"], execution_pass["id"]) is False

    store.assign_work(child["id"], node["id"])
    claimed_child = store.claim_node_work(node["id"], run["id"])
    assert claimed_child["id"] == child["id"]
    assert store.complete_work(child["id"], result_payload("handoff-complete")) is None
    assert store.validation_barrier_ready(run["id"], execution_pass["id"]) is True


def test_handoff_lineage_holds_ordinary_dependents_until_continuation_completes(store):
    repository = add_repository(store)
    run = add_run(store, repository["id"])
    execution_pass = store.create_pass(run["id"], "ISSUE", {})
    package = single_work_package("root", "implementation")
    package["specifications"][0]["work_items"].append(
        {
            "key": "dependent",
            "title": "Consume the completed outcome",
            "description": "Run only after the full root continuation completes.",
            "classification": "consumer",
            "dependencies": ["root"],
            "dependency_evidence": dependency_evidence("root"),
        }
    )
    saved = store.save_specification_package(
        run["id"], execution_pass["id"], package
    )
    work = {item["key"]: item for item in saved["work_items"]}
    implementation = store.create_dynamic_node(
        repository["id"], "implementation", [1], "Produce the outcome."
    )
    continuation = store.create_dynamic_node(
        repository["id"], "continuation", [1], "Continue incomplete work."
    )
    consumer = store.create_dynamic_node(
        repository["id"], "consumer", [1], "Consume completed outcomes."
    )
    store.assign_work(work["root"]["id"], implementation["id"])
    store.assign_work(work["dependent"]["id"], consumer["id"])
    root = store.claim_node_work(implementation["id"], run["id"])
    child = store.complete_work(
        root["id"],
        result_payload("partial-root"),
        {
            "classification": "continuation",
            "context": {"remaining": "finish the outcome"},
            "artifacts": [],
            "dependencies": ["root"],
            "dependency_evidence": dependency_evidence("root"),
            "blocking": None,
        },
    )
    store.assign_work(child["id"], continuation["id"])

    assert store.claim_node_work(consumer["id"], run["id"]) is None
    claimed_child = store.claim_node_work(continuation["id"], run["id"])
    assert claimed_child["id"] == child["id"]
    assert store.claim_node_work(consumer["id"], run["id"]) is None

    store.complete_work(claimed_child["id"], result_payload("continued-root"))
    assert store.claim_node_work(consumer["id"], run["id"])["key"] == "dependent"


def test_handoff_rejects_runtime_dependency_cycle_atomically(store):
    repository = add_repository(store)
    run = add_run(store, repository["id"])
    execution_pass = store.create_pass(run["id"], "ISSUE", {})
    package = single_work_package("root", "implementation")
    package["specifications"][0]["work_items"].append(
        {
            "key": "dependent",
            "title": "Consume root",
            "description": "Depends on the root outcome.",
            "classification": "consumer",
            "dependencies": ["root"],
            "dependency_evidence": dependency_evidence("root"),
        }
    )
    saved = store.save_specification_package(
        run["id"], execution_pass["id"], package
    )
    root = saved["work_items"][0]
    node = store.create_dynamic_node(
        repository["id"], "implementation", [1], "Produce the outcome."
    )
    store.assign_work(root["id"], node["id"])
    store.claim_node_work(node["id"], run["id"])
    child = store.complete_work(
        root["id"],
        result_payload("partial-root"),
        {
            "classification": "implementation",
            "context": {},
            "artifacts": [],
            "dependencies": ["root"],
            "dependency_evidence": dependency_evidence("root"),
            "blocking": None,
        },
    )
    store.assign_work(child["id"], node["id"])
    store.claim_node_work(node["id"], run["id"])

    with pytest.raises(ValueError, match="handoff dependency graph must be acyclic"):
        store.complete_work(
            child["id"],
            result_payload("partial-continuation"),
            {
                "classification": "continuation",
                "context": {},
                "artifacts": [],
                "dependencies": ["dependent"],
                "dependency_evidence": dependency_evidence("dependent"),
                "blocking": None,
            },
        )

    current = store.list_work_items(run["id"])
    assert len(current) == 3
    assert next(item for item in current if item["key"] == "root")["state"] == "HANDED_OFF"
    assert next(item for item in current if item["id"] == child["id"])["state"] == "RUNNING"


def test_failed_continuation_propagates_only_to_causal_descendants(store):
    repository = add_repository(store)
    run = add_run(store, repository["id"])
    execution_pass = store.create_pass(run["id"], "ISSUE", {})
    package = single_work_package("root", "implementation")
    package["specifications"][0]["work_items"].extend(
        [
            {
                "key": "dependent",
                "title": "Consume root",
                "description": "Requires the completed root lineage.",
                "classification": "consumer",
                "dependencies": ["root"],
                "dependency_evidence": dependency_evidence("root"),
            },
            {
                "key": "independent",
                "title": "Independent work",
                "description": "Does not require the root lineage.",
                "classification": "consumer",
                "dependencies": [],
                "dependency_evidence": [],
            },
        ]
    )
    saved = store.save_specification_package(
        run["id"], execution_pass["id"], package
    )
    work = {item["key"]: item for item in saved["work_items"]}
    implementation = store.create_dynamic_node(
        repository["id"], "implementation", [1], "Produce the outcome."
    )
    continuation = store.create_dynamic_node(
        repository["id"], "continuation", [1], "Continue incomplete work."
    )
    for key in ("dependent", "independent"):
        store.assign_work(work[key]["id"], continuation["id"])
    store.assign_work(work["root"]["id"], implementation["id"])
    root = store.claim_node_work(implementation["id"], run["id"])
    child = store.complete_work(
        root["id"],
        result_payload("partial-root"),
        {
            "classification": "continuation",
            "context": {},
            "artifacts": [],
            "dependencies": ["root"],
            "dependency_evidence": dependency_evidence("root"),
            "blocking": None,
        },
    )
    store.assign_work(child["id"], continuation["id"])
    independent = store.claim_node_work(continuation["id"], run["id"])
    assert independent["key"] == "independent"
    store.complete_work(independent["id"], result_payload("independent-complete"))
    claimed_child = store.claim_node_work(continuation["id"], run["id"])
    assert claimed_child["id"] == child["id"]
    store.fail_work(claimed_child["id"], result_payload("continuation-failed"))

    assert store.settle_failed_pass_work(
        run["id"],
        execution_pass["id"],
        result_payload("blocked-by-continuation"),
    ) is True
    current = {
        item["key"]: item for item in store.list_work_items(run["id"])
    }
    assert current["dependent"]["state"] == "FAILED"
    assert current["independent"]["state"] == "COMPLETED"
    assert store.validation_barrier_ready(run["id"], execution_pass["id"]) is False


def test_persisted_unevidenced_dependency_fails_closed_at_every_gate(store, store_path):
    repository = add_repository(store)
    run = add_run(store, repository["id"])
    execution_pass = store.create_pass(run["id"], "ISSUE", {})
    saved = store.save_specification_package(
        run["id"], execution_pass["id"], specification_package()
    )
    implement = next(
        item for item in saved["work_items"] if item["key"] == "implement"
    )
    node = store.create_dynamic_node(
        repository["id"], "backend/python", [1], "Complete the work."
    )
    store.assign_work(implement["id"], node["id"])
    with sqlite3.connect(store_path) as connection:
        connection.execute(
            """
            UPDATE specifications SET dependency_evidence = '[]'
            WHERE pass_id = ? AND key = 'repair'
            """,
            (execution_pass["id"],),
        )

    with pytest.raises(ValueError, match="persisted specification dependency_evidence"):
        store.claim_node_work(node["id"], run["id"])
    with pytest.raises(ValueError, match="persisted specification dependency_evidence"):
        store.validation_barrier_ready(run["id"], execution_pass["id"])


def test_validation_barrier_rejects_outstanding_node_work_from_same_run(store):
    repository = add_repository(store)
    run = add_run(store, repository["id"])
    node = store.create_dynamic_node(repository["id"], "backend", [1], "Handle work.")

    first_pass = store.create_pass(run["id"], "ISSUE", {})
    first_work = store.save_specification_package(
        run["id"], first_pass["id"], single_work_package("first", "backend")
    )["work_items"][0]
    store.assign_work(first_work["id"], node["id"])
    store.claim_node_work(node["id"], run["id"])
    store.complete_work(first_work["id"], result_payload("first"))
    assert store.validation_barrier_ready(run["id"], first_pass["id"]) is True

    second_pass = store.create_pass(run["id"], "ADDITIONAL_WORK", {})
    second_work = store.save_specification_package(
        run["id"], second_pass["id"], single_work_package("second", "backend")
    )["work_items"][0]
    store.assign_work(second_work["id"], node["id"])
    assert store.validation_barrier_ready(run["id"], first_pass["id"]) is False
    store.claim_node_work(node["id"], run["id"])
    store.fail_work(second_work["id"], result_payload("second-failed"))
    assert store.validation_barrier_ready(run["id"], first_pass["id"]) is True


def test_validation_and_feedback_are_deduplicated_with_decoded_payloads(store, store_path):
    repository = add_repository(store)
    run = add_run(store, repository["id"])
    execution_pass = store.create_pass(run["id"], "ISSUE", {})

    first_validation = store.record_validation(
        run["id"], execution_pass["id"], {"passed": False, "evidence": ["failure"]}
    )
    duplicate_validation = store.record_validation(
        run["id"], execution_pass["id"], {"passed": True, "evidence": ["later"]}
    )
    assert duplicate_validation == first_validation
    assert first_validation["result"] == {"passed": False, "evidence": ["failure"]}
    feedback_pass = store.create_pass(
        run["id"], "FEEDBACK", {"external_id": "review:202"}
    )
    second_validation = store.record_validation(
        run["id"], feedback_pass["id"], {"passed": True, "evidence": ["corrected"]}
    )

    feedback = {
        "feedback": {"kind": "inline", "body": "needs work", "line": 8},
        "diff": "@@ change @@",
    }
    assert store.add_feedback(run["id"], "inline:101", feedback) is True
    assert store.add_feedback(run["id"], "inline:101", {"replacement": True}) is False
    assert store.add_feedback(run["id"], "review:202", {"body": "CHANGES_REQUESTED"}) is True
    assert [(row["external_id"], row["package"]) for row in store.list_feedback(run["id"])] == [
        ("inline:101", feedback),
        ("review:202", {"body": "CHANGES_REQUESTED"}),
    ]

    reopened = Store(store_path)
    assert reopened.list_validations(run["id"]) == [
        first_validation,
        second_validation,
    ]
    assert reopened.record_validation(
        run["id"], execution_pass["id"], {"passed": True}
    ) == first_validation
    assert reopened.add_feedback(run["id"], "inline:101", {}) is False


def test_feedback_scope_result_is_decoded_durable_and_rejects_conflicts(
    store, store_path
):
    repository = add_repository(store)
    run = add_run(store, repository["id"])
    execution_pass = store.create_pass(
        run["id"], "FEEDBACK", {"external_ids": ["inline:101"]}
    )
    result = {
        "feedback_items": [
            {
                "external_id": "inline:101",
                "valid": True,
                "in_scope": False,
                "pr_regression": False,
            }
        ],
        "specifications": [],
    }

    assert store.get_feedback_scope_result(run["id"], execution_pass["id"]) is None
    assert (
        store.record_feedback_scope_result(run["id"], execution_pass["id"], result)
        == result
    )
    assert (
        store.record_feedback_scope_result(
            run["id"], execution_pass["id"], copy.deepcopy(result)
        )
        == result
    )
    with pytest.raises(ValueError, match="different"):
        store.record_feedback_scope_result(
            run["id"],
            execution_pass["id"],
            {"feedback_items": [], "specifications": []},
        )
    with pytest.raises(ValueError, match="object"):
        store.record_feedback_scope_result(run["id"], execution_pass["id"], [])

    assert store.get_feedback_scope_result(run["id"], execution_pass["id"]) == result
    assert Store(store_path).get_feedback_scope_result(
        run["id"], execution_pass["id"]
    ) == result


def test_feedback_scope_result_rejects_invalid_specifications_before_persistence(
    store,
):
    repository = add_repository(store)
    run = add_run(store, repository["id"])
    execution_pass = store.create_pass(
        run["id"], "feedback", {"feedback": [{"external_id": "inline:101"}]}
    )
    result = {
        "dispositions": [
            {
                "external_id": "inline:101",
                "valid": True,
                "in_scope": True,
                "pr_regression": False,
                "explanation": "The defect is in scope.",
                "evidence": ["The current head still contains the defect."],
                "specification_keys": ["spec-1"],
                "follow_up_issue": None,
            }
        ],
        "specifications": [{"key": "spec-1"}],
    }

    with pytest.raises(ValueError, match="missing"):
        store.record_feedback_scope_result(run["id"], execution_pass["id"], result)

    assert store.get_feedback_scope_result(run["id"], execution_pass["id"]) is None


def test_feedback_addressing_is_durable_idempotent_and_preserves_state_on_duplicate(
    store, store_path
):
    repository = add_repository(store)
    run = add_run(store, repository["id"])
    inline_package = {"feedback": {"kind": "inline", "body": "Fix this line"}}
    review_package = {"feedback": {"kind": "review", "body": "Changes requested"}}

    assert store.add_feedback(run["id"], "inline:101", inline_package) is True
    assert store.add_feedback(run["id"], "review:202", review_package) is True
    pending = store.list_feedback(run["id"])
    assert [
        (row["external_id"], row["status"], row["addressed_sha"], row["response_url"])
        for row in pending
    ] == [
        ("inline:101", "PENDING", None, None),
        ("review:202", "PENDING", None, None),
    ]

    store.mark_feedback_addressed(
        run["id"],
        "inline:101",
        "RESOLVED",
        "abc123",
        "https://example.test/reviews/101#resolved",
    )
    store.mark_feedback_addressed(
        run["id"],
        "inline:101",
        "RESOLVED",
        "abc123",
        "https://example.test/reviews/101#resolved",
    )
    store.mark_feedback_addressed(
        run["id"],
        "review:202",
        "ACKNOWLEDGED",
        "def456",
        "https://example.test/pull/41#issuecomment-202",
    )
    assert store.add_feedback(
        run["id"], "inline:101", {"feedback": {"kind": "replacement"}}
    ) is False

    reopened = Store(store_path)
    addressed = reopened.list_feedback(run["id"])
    assert [
        (
            row["external_id"],
            row["package"],
            row["status"],
            row["addressed_sha"],
            row["response_url"],
        )
        for row in addressed
    ] == [
        (
            "inline:101",
            inline_package,
            "RESOLVED",
            "abc123",
            "https://example.test/reviews/101#resolved",
        ),
        (
            "review:202",
            review_package,
            "ACKNOWLEDGED",
            "def456",
            "https://example.test/pull/41#issuecomment-202",
        ),
    ]


def test_feedback_disposition_and_follow_up_are_decoded_durable_and_idempotent(
    store, store_path
):
    repository = add_repository(store)
    run = add_run(store, repository["id"])
    external_id = "inline:outside"
    assert store.add_feedback(
        run["id"], external_id, {"feedback": {"kind": "inline", "body": "Existing bug"}}
    )
    disposition_result = {
        "valid": True,
        "in_scope": False,
        "pr_regression": False,
        "explanation": "The defect predates this pull request.",
        "evidence": ["The target branch contains the same behavior."],
        "specification_keys": [],
    }
    issue = {
        "number": 88,
        "title": "Repair the pre-existing defect",
        "body": "Source-linked follow-up details",
        "url": "https://example.test/issues/88",
    }

    disposition_row = store.record_feedback_disposition(
        run["id"], external_id, "OUT_OF_SCOPE", disposition_result
    )
    assert disposition_row["disposition"] == "OUT_OF_SCOPE"
    assert disposition_row["disposition_result"] == disposition_result
    assert disposition_row["follow_up_issue"] is None
    assert (
        store.record_feedback_disposition(
            run["id"], external_id, "OUT_OF_SCOPE", copy.deepcopy(disposition_result)
        )
        == disposition_row
    )

    followed_up = store.record_feedback_follow_up(run["id"], external_id, issue)
    assert followed_up["follow_up_issue"] == issue
    assert (
        store.record_feedback_follow_up(
            run["id"], external_id, copy.deepcopy(issue)
        )
        == followed_up
    )

    reopened = Store(store_path)
    assert reopened.list_feedback(run["id"]) == [followed_up]


@pytest.mark.parametrize(
    ("disposition", "status"),
    [("OUT_OF_SCOPE", "RESOLVED"), ("INVALID", "ACKNOWLEDGED")],
)
def test_feedback_without_code_is_addressed_without_a_sha(
    store, store_path, disposition, status
):
    repository = add_repository(store)
    run = add_run(store, repository["id"])
    external_id = f"feedback:{disposition.lower()}"
    response_url = f"https://example.test/responses/{disposition.lower()}"
    store.add_feedback(run["id"], external_id, {"feedback": {"kind": "inline"}})
    store.record_feedback_disposition(
        run["id"],
        external_id,
        disposition,
        {"explanation": "No current-branch code change is required."},
    )

    assert (
        store.mark_feedback_without_code(
            run["id"], external_id, status, response_url
        )
        is None
    )
    assert (
        store.mark_feedback_without_code(
            run["id"], external_id, status, response_url
        )
        is None
    )

    row = Store(store_path).list_feedback(run["id"])[0]
    assert row["status"] == status
    assert row["addressed_sha"] is None
    assert row["response_url"] == response_url


def test_feedback_writes_reject_conflicting_replays_without_overwriting(store):
    repository = add_repository(store)
    run = add_run(store, repository["id"])
    external_id = "inline:conflict"
    store.add_feedback(run["id"], external_id, {"feedback": {"kind": "inline"}})
    disposition_result = {"explanation": "Outside the accepted specifications."}
    issue = {
        "number": 91,
        "title": "Follow-up",
        "body": "Original body",
        "url": "https://example.test/issues/91",
    }
    response_url = "https://example.test/replies/original"
    store.record_feedback_disposition(
        run["id"], external_id, "OUT_OF_SCOPE", disposition_result
    )
    store.record_feedback_follow_up(run["id"], external_id, issue)
    store.mark_feedback_without_code(
        run["id"], external_id, "RESOLVED", response_url
    )

    with pytest.raises(ValueError, match="different"):
        store.record_feedback_disposition(
            run["id"],
            external_id,
            "OUT_OF_SCOPE",
            {"explanation": "Conflicting replacement."},
        )
    with pytest.raises(ValueError, match="different"):
        store.record_feedback_follow_up(
            run["id"], external_id, {**issue, "number": 92}
        )
    with pytest.raises(ValueError, match="different"):
        store.mark_feedback_without_code(
            run["id"],
            external_id,
            "RESOLVED",
            "https://example.test/replies/replacement",
        )

    current = store.list_feedback(run["id"])[0]
    assert current["disposition"] == "OUT_OF_SCOPE"
    assert current["disposition_result"] == disposition_result
    assert current["follow_up_issue"] == issue
    assert current["status"] == "RESOLVED"
    assert current["addressed_sha"] is None
    assert current["response_url"] == response_url

    in_scope_id = "inline:in-scope"
    store.add_feedback(run["id"], in_scope_id, {"feedback": {"kind": "inline"}})
    store.record_feedback_disposition(
        run["id"], in_scope_id, "IN_SCOPE", {"explanation": "Requires code."}
    )
    with pytest.raises(ValueError, match="disposition"):
        store.mark_feedback_without_code(
            run["id"],
            in_scope_id,
            "RESOLVED",
            "https://example.test/replies/not-allowed",
        )
    in_scope = store.list_feedback(run["id"])[1]
    assert in_scope["status"] == "PENDING"
    assert in_scope["addressed_sha"] is None
    assert in_scope["response_url"] is None


def test_store_migrates_legacy_feedback_rows_to_pending(store_path):
    with sqlite3.connect(store_path) as connection:
        connection.execute(
            """
            CREATE TABLE feedback (
                id INTEGER PRIMARY KEY,
                run_id INTEGER NOT NULL,
                external_id TEXT NOT NULL,
                package TEXT NOT NULL,
                UNIQUE(run_id, external_id)
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO feedback(id, run_id, external_id, package)
            VALUES (?, ?, ?, ?)
            """,
            [
                (1, 73, "inline:legacy", '{"feedback":{"kind":"inline"}}'),
                (2, 73, "review:legacy", '{"feedback":{"kind":"review"}}'),
            ],
        )

    migrated = Store(store_path)

    assert [
        (
            row["external_id"],
            row["package"],
            row["status"],
            row["addressed_sha"],
            row["response_url"],
        )
        for row in migrated.list_feedback(73)
    ] == [
        (
            "inline:legacy",
            {"feedback": {"kind": "inline"}},
            "PENDING",
            None,
            None,
        ),
        (
            "review:legacy",
            {"feedback": {"kind": "review"}},
            "PENDING",
            None,
            None,
        ),
    ]


def test_store_migrates_existing_runs_and_addressed_feedback_without_data_loss(
    store_path,
):
    with sqlite3.connect(store_path) as connection:
        connection.execute(
            """
            CREATE TABLE repositories (
                id INTEGER PRIMARY KEY,
                github_repository TEXT NOT NULL UNIQUE,
                target_branch TEXT NOT NULL,
                similarity_threshold REAL NOT NULL,
                tracked INTEGER NOT NULL DEFAULT 1 CHECK (tracked IN (0, 1))
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE runs (
                id INTEGER PRIMARY KEY,
                repository_id INTEGER NOT NULL REFERENCES repositories(id),
                issue_number INTEGER NOT NULL,
                issue_json TEXT NOT NULL,
                state TEXT NOT NULL,
                branch TEXT,
                pull_request TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE feedback (
                id INTEGER PRIMARY KEY,
                run_id INTEGER NOT NULL REFERENCES runs(id),
                external_id TEXT NOT NULL,
                package TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING'
                    CHECK (status IN ('PENDING', 'RESOLVED', 'ACKNOWLEDGED')),
                addressed_sha TEXT,
                response_url TEXT,
                UNIQUE(run_id, external_id)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO repositories(
                id, github_repository, target_branch, similarity_threshold, tracked
            ) VALUES (4, 'owner/legacy', 'main', 0.5, 1)
            """
        )
        connection.execute(
            """
            INSERT INTO runs(
                id, repository_id, issue_number, issue_json, state, branch, pull_request
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                11,
                4,
                31,
                '{"number":31,"title":"Legacy issue"}',
                "PR_LISTENING",
                "agent/issue-31",
                '{"number":44,"url":"https://example.test/pulls/44"}',
            ),
        )
        connection.execute(
            """
            INSERT INTO feedback(
                id, run_id, external_id, package,
                status, addressed_sha, response_url
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                21,
                11,
                "review:legacy-addressed",
                '{"feedback":{"kind":"review","body":"Handled"}}',
                "ACKNOWLEDGED",
                "legacy-sha",
                "https://example.test/replies/legacy",
            ),
        )

    migrated = Store(store_path)

    run = migrated.get_run(11)
    assert run["issue_json"] == {"number": 31, "title": "Legacy issue"}
    assert run["state"] == "PR_LISTENING"
    assert run["branch"] == "agent/issue-31"
    assert run["pull_request"] == {
        "number": 44,
        "url": "https://example.test/pulls/44",
    }
    assert run["pr_listening_since"] is None

    feedback = migrated.list_feedback(11)[0]
    assert feedback["package"] == {
        "feedback": {"kind": "review", "body": "Handled"}
    }
    assert feedback["status"] == "ACKNOWLEDGED"
    assert feedback["addressed_sha"] == "legacy-sha"
    assert feedback["response_url"] == "https://example.test/replies/legacy"
    assert feedback["disposition"] is None
    assert feedback["disposition_result"] is None
    assert feedback["follow_up_issue"] is None


def test_success_promotion_and_terminal_run_pruning_are_run_aware(store):
    repository = add_repository(store)
    used = store.create_dynamic_node(repository["id"], "backend", [1, 0], "Backend work.")
    stale = store.create_dynamic_node(repository["id"], "quality", [0, 1], "Quality work.")
    ephemeral = store.create_dynamic_node(repository["id"], "docs", [0.5, 0.5], "Documentation work.")

    first_run = add_run(store, repository["id"], 1)
    used = store.record_node_success(used["id"], first_run["id"], promotion_threshold=2)
    used = store.record_node_success(used["id"], first_run["id"], promotion_threshold=2)
    stale = store.record_node_success(stale["id"], first_run["id"], promotion_threshold=1)
    assert used["success_count"] == 2
    assert used["persistence"] == stale["persistence"] == "PERSISTENT"

    store.transition_run(first_run["id"], "COMPLETED")
    assert store.adapt_nodes_after_run(first_run["id"], stale_run_threshold=2) == [ephemeral["id"]]
    assert store.adapt_nodes_after_run(first_run["id"], stale_run_threshold=2) == []
    current = {node["id"]: node for node in store.list_dynamic_nodes(repository["id"])}
    assert current[used["id"]]["unused_completed_runs"] == 0
    assert current[stale["id"]]["unused_completed_runs"] == 0

    second_run = add_run(store, repository["id"], 2)
    store.record_node_success(used["id"], second_run["id"], promotion_threshold=2)
    store.transition_run(second_run["id"], "CLOSED")
    assert store.adapt_nodes_after_run(second_run["id"], stale_run_threshold=2) == []
    current = {node["id"]: node for node in store.list_dynamic_nodes(repository["id"])}
    assert current[used["id"]]["unused_completed_runs"] == 0
    assert current[stale["id"]]["unused_completed_runs"] == 1

    third_run = add_run(store, repository["id"], 3)
    store.transition_run(third_run["id"], "COMPLETED")
    assert store.adapt_nodes_after_run(third_run["id"], stale_run_threshold=2) == [stale["id"]]
    assert [node["id"] for node in store.list_dynamic_nodes(repository["id"])] == [used["id"]]
    assert [(node["classification"], node["persistence"]) for node in store.list_nodes(repository["id"])[:2]] == [
        ("Specify", "PERMANENT"),
        ("Validate", "PERMANENT"),
    ]


def test_terminal_adaptation_remains_idempotent_after_reopen(store, store_path):
    repository = add_repository(store)
    node = store.create_dynamic_node(repository["id"], "backend", [1], "Handle work.")
    run = add_run(store, repository["id"])
    store.transition_run(run["id"], "COMPLETED")

    assert store.adapt_nodes_after_run(run["id"], stale_run_threshold=3) == [node["id"]]

    reopened = Store(store_path)
    assert reopened.adapt_nodes_after_run(run["id"], stale_run_threshold=3) == []
    assert reopened.list_dynamic_nodes(repository["id"]) == []


def test_recovery_requeues_only_interrupted_work_and_keeps_active_run_unique(store, store_path):
    repository = add_repository(store)
    run = add_run(store, repository["id"])
    execution_pass = store.create_pass(run["id"], "ISSUE", {})
    saved = store.save_specification_package(
        run["id"], execution_pass["id"], specification_package()
    )
    work_by_key = {work["key"]: work for work in saved["work_items"]}
    node = store.create_dynamic_node(
        repository["id"], "backend/python", [1, 0], "Handle backend work."
    )
    store.assign_work(work_by_key["verify"]["id"], node["id"])
    store.assign_work(work_by_key["implement"]["id"], node["id"])
    claimed = store.claim_node_work(node["id"], run["id"])
    assert claimed["key"] == "implement"

    reopened = Store(store_path)
    recovered = reopened.recover_interrupted_work()
    states = {work["key"]: work["state"] for work in reopened.list_work_items(run["id"])}
    duplicate, created = reopened.create_run(
        repository["id"], run["issue_number"], {"number": run["issue_number"], "new": True}
    )

    assert recovered == 1
    assert states == {"verify": "QUEUED", "implement": "QUEUED"}
    assert duplicate["id"] == run["id"]
    assert created is False
    assert reopened.recover_interrupted_work() == 0
    assert reopened.claim_node_work(node["id"], run["id"])["key"] == "implement"
