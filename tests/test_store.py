import copy
import sqlite3
import threading

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


def specification_package():
    return {
        "specifications": [
            {
                "key": "context",
                "title": "Retain repository context",
                "description": "The existing context remains valid.",
                "acceptance_criteria": ["The context is retained."],
                "dependencies": [],
                "executable": False,
                "work_items": [],
            },
            {
                "key": "repair",
                "title": "Repair the service",
                "description": "Implement and verify the requested repair.",
                "acceptance_criteria": ["The repair works.", "The regression is covered."],
                "dependencies": ["context"],
                "executable": True,
                "work_items": [
                    {
                        "key": "verify",
                        "title": "Verify the repair",
                        "description": "Exercise the repaired behavior.",
                        "classification": "quality/testing",
                        "dependencies": ["implement"],
                    },
                    {
                        "key": "implement",
                        "title": "Implement the repair",
                        "description": "Change the repository behavior.",
                        "classification": "backend/python",
                        "dependencies": [],
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
                "executable": True,
                "work_items": [
                    {
                        "key": key,
                        "title": "Complete the change",
                        "description": "Implement the atomic change.",
                        "classification": classification,
                        "dependencies": [],
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
        "executable",
        "work_items",
    ):
        package = specification_package()
        package["specifications"][1].pop(field)
        cases.append((f"missing specification {field}", package))
    for field in ("key", "title", "description", "classification", "dependencies"):
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
    cases.append(("self-dependent specification", package))

    package = specification_package()
    package["specifications"][0]["dependencies"] = ["repair"]
    cases.append(("cyclic specifications", package))

    package = specification_package()
    package["specifications"][1]["work_items"][0]["dependencies"] = ["verify"]
    cases.append(("self-dependent work", package))

    package = specification_package()
    package["specifications"][1]["work_items"][1]["dependencies"] = ["verify"]
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
                "executable": True,
                "work_items": [
                    {
                        "key": "prepare-one",
                        "title": "Prepare the first input",
                        "description": "Prepare the first repair input.",
                        "classification": "backend",
                        "dependencies": [],
                    },
                    {
                        "key": "prepare-two",
                        "title": "Prepare the second input",
                        "description": "Prepare the second repair input.",
                        "classification": "backend",
                        "dependencies": [],
                    },
                ],
            },
            {
                "key": "repair",
                "title": "Complete the repair",
                "description": "Use the prepared inputs to complete the repair.",
                "acceptance_criteria": ["The repair is complete."],
                "dependencies": ["prepare"],
                "executable": True,
                "work_items": [
                    {
                        "key": "foundation",
                        "title": "Build the foundation",
                        "description": "Build the downstream foundation.",
                        "classification": "quality",
                        "dependencies": [],
                    },
                    {
                        "key": "dependent",
                        "title": "Finish the dependent work",
                        "description": "Finish work after the foundation.",
                        "classification": "quality",
                        "dependencies": ["foundation"],
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
    assert repository["tracked"] is True
    assert store.list_repositories() == [repository]
    assert store.get_repository(repository["id"]) == repository

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

    with sqlite3.connect(store_path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"

    with pytest.raises(sqlite3.IntegrityError):
        store.create_run(999_999, 1, {"number": 1})


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

    claimed_implement = store.claim_node_work(backend["id"])
    assert claimed_implement["key"] == "implement"
    assert claimed_implement["state"] == "RUNNING"
    assert store.claim_node_work(backend["id"]) is None

    independent_pass = store.create_pass(run["id"], "ADDITIONAL_WORK", {})
    independent = store.save_specification_package(
        run["id"], independent_pass["id"], single_work_package("independent", "quality/testing")
    )["work_items"][0]
    store.assign_work(independent["id"], quality["id"])
    claimed_independent = store.claim_node_work(quality["id"])
    assert claimed_independent["key"] == "independent"
    failed = store.fail_work(independent["id"], result_payload("failed"))
    assert failed["state"] == "FAILED"
    assert failed["result"]["output"] == {"summary": "failed"}

    assert store.complete_work(
        claimed_implement["id"], result_payload("implemented")
    ) is None
    claimed_verify = store.claim_node_work(backend["id"])
    assert claimed_verify["key"] == "verify"
    assert store.complete_work(claimed_verify["id"], result_payload("verified")) is None

    current = {work["key"]: work for work in store.list_work_items(run["id"])}
    assert current["implement"]["state"] == "COMPLETED"
    assert current["implement"]["result"]["repository_state"] == {"head": "sha-implemented"}
    assert current["verify"]["state"] == "COMPLETED"
    assert store.validation_barrier_ready(run["id"], execution_pass["id"]) is True


def test_claim_waits_for_all_specification_work_and_accepts_terminal_failures(store):
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

    assert store.claim_node_work(quality["id"]) is None
    first = store.claim_node_work(backend["id"])
    assert first["key"] == "prepare-one"
    store.fail_work(first["id"], result_payload("prepare-one-failed"))
    assert store.claim_node_work(quality["id"]) is None

    second = store.claim_node_work(backend["id"])
    assert second["key"] == "prepare-two"
    store.fail_work(second["id"], result_payload("prepare-two-failed"))
    foundation = store.claim_node_work(quality["id"])
    assert foundation["key"] == "foundation"

    store.fail_work(foundation["id"], result_payload("foundation-failed"))
    assert store.claim_node_work(quality["id"])["key"] == "dependent"


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

    assert store.claim_node_work(node["id"])["key"] == "implement"


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
    store.claim_node_work(node["id"])

    with pytest.raises(ValueError):
        store.complete_work(
            root["id"],
            result_payload("partial"),
            {"classification": "quality/testing", "context": {}, "artifacts": [], "dependencies": []},
        )
    assert store.list_work_items(run["id"])[0]["state"] == "RUNNING"

    handoff = {
        "classification": "quality/testing",
        "context": {"summary": "Implementation is ready for targeted verification."},
        "artifacts": [{"path": "artifact.txt"}],
        "dependencies": ["root"],
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
    claimed_child = store.claim_node_work(node["id"])
    assert claimed_child["id"] == child["id"]
    assert store.complete_work(child["id"], result_payload("handoff-complete")) is None
    assert store.validation_barrier_ready(run["id"], execution_pass["id"]) is True


def test_validation_barrier_rejects_outstanding_node_work_from_same_run(store):
    repository = add_repository(store)
    run = add_run(store, repository["id"])
    node = store.create_dynamic_node(repository["id"], "backend", [1], "Handle work.")

    first_pass = store.create_pass(run["id"], "ISSUE", {})
    first_work = store.save_specification_package(
        run["id"], first_pass["id"], single_work_package("first", "backend")
    )["work_items"][0]
    store.assign_work(first_work["id"], node["id"])
    store.claim_node_work(node["id"])
    store.complete_work(first_work["id"], result_payload("first"))
    assert store.validation_barrier_ready(run["id"], first_pass["id"]) is True

    second_pass = store.create_pass(run["id"], "ADDITIONAL_WORK", {})
    second_work = store.save_specification_package(
        run["id"], second_pass["id"], single_work_package("second", "backend")
    )["work_items"][0]
    store.assign_work(second_work["id"], node["id"])
    assert store.validation_barrier_ready(run["id"], first_pass["id"]) is False
    store.claim_node_work(node["id"])
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
    claimed = store.claim_node_work(node["id"])
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
    assert reopened.claim_node_work(node["id"])["key"] == "implement"


def test_repository_add_operation_lifecycle_metadata_uses_injected_clock(tmp_path):
    """Pending rows stay nonterminal and terminal transitions get one clock age."""
    now = [1000.0]
    store = Store(tmp_path / "operation-metadata.sqlite", clock=lambda: now[0])

    pending = store.begin_repository_add_operation(
        "operation-pending", "acme/pending", None
    )
    assert pending["created_at"] == 1000.0
    assert pending["updated_at"] == 1000.0
    assert pending["terminal_at"] is None

    # Idempotent lookup/replay does not refresh age and cannot shorten retention.
    now[0] = 1010.0
    replayed = store.begin_repository_add_operation(
        "operation-pending", "acme/pending", None
    )
    assert replayed["created_at"] == 1000.0
    assert replayed["updated_at"] == 1000.0
    assert replayed["terminal_at"] is None

    now[0] = 1020.0
    failed = store.fail_repository_add_operation(
        "operation-pending", "repository lookup failed"
    )
    assert failed["state"] == "FAILED"
    assert failed["created_at"] == 1000.0
    assert failed["updated_at"] == 1020.0
    assert failed["terminal_at"] == 1020.0

    now[0] = 1030.0
    store.begin_repository_add_operation(
        "operation-committed", "acme/committed", "release"
    )
    now[0] = 1040.0
    repository = store.add_repository_for_operation(
        "operation-committed", "acme/committed", "release", 0.72
    )
    committed = store.get_repository_add_operation("operation-committed")
    assert committed["state"] == "COMMITTED"
    assert committed["repository_id"] == repository["id"]
    assert committed["created_at"] == 1030.0
    assert committed["updated_at"] == 1040.0
    assert committed["terminal_at"] == 1040.0


def test_repository_add_operation_schema_migrates_existing_rows_conservatively(tmp_path):
    """Legacy operation rows survive upgrade and start a fresh retention window."""
    path = tmp_path / "legacy-operation.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA foreign_keys=ON;
        CREATE TABLE repositories (
            id INTEGER PRIMARY KEY,
            github_repository TEXT NOT NULL UNIQUE,
            target_branch TEXT NOT NULL,
            similarity_threshold REAL NOT NULL,
            tracked INTEGER NOT NULL DEFAULT 1 CHECK (tracked IN (0, 1))
        );
        CREATE TABLE repository_add_operations (
            operation_id TEXT PRIMARY KEY,
            github_repository TEXT NOT NULL,
            target_branch TEXT,
            state TEXT NOT NULL CHECK (state IN ('PENDING', 'COMMITTED', 'FAILED')),
            repository_id INTEGER REFERENCES repositories(id),
            error TEXT
        );
        INSERT INTO repositories(
            id, github_repository, target_branch, similarity_threshold, tracked
        ) VALUES (7, 'acme/committed', 'main', 0.72, 1);
        INSERT INTO repository_add_operations VALUES
            ('pending', 'acme/pending', NULL, 'PENDING', NULL, NULL),
            ('committed', 'acme/committed', 'main', 'COMMITTED', 7, NULL),
            ('failed', 'acme/failed', NULL, 'FAILED', NULL, 'not found');
        """
    )
    connection.commit()
    connection.close()

    store = Store(path, clock=lambda: 5000.0)
    pending = store.get_repository_add_operation("pending")
    committed = store.get_repository_add_operation("committed")
    failed = store.get_repository_add_operation("failed")

    for operation in (pending, committed, failed):
        assert operation["created_at"] == 5000.0
        assert operation["updated_at"] == 5000.0
    assert pending["terminal_at"] is None
    assert committed["terminal_at"] == 5000.0
    assert failed["terminal_at"] == 5000.0
    assert committed["repository_id"] == 7
    assert failed["error"] == "not found"

    with sqlite3.connect(path) as connection:
        columns = {
            row[1]: row for row in connection.execute(
                "PRAGMA table_info(repository_add_operations)"
            )
        }
        assert {"created_at", "updated_at", "terminal_at"} <= columns.keys()
        indexes = {
            row[1] for row in connection.execute(
                "PRAGMA index_list(repository_add_operations)"
            )
        }
        assert "repository_add_operations_terminal_age" in indexes


def test_repository_add_operation_purge_is_bounded_and_terminal_only(tmp_path):
    now = [1000.0]
    store = Store(tmp_path / "operation-retention.sqlite", clock=lambda: now[0])

    store.begin_repository_add_operation("pending-old", "acme/pending", None)
    store.begin_repository_add_operation("failed-old-a", "acme/failed-a", None)
    store.fail_repository_add_operation("failed-old-a", "not found")
    store.begin_repository_add_operation("failed-old-b", "acme/failed-b", None)
    store.fail_repository_add_operation("failed-old-b", "not found")
    store.begin_repository_add_operation("committed-old", "acme/committed", "main")
    repository = store.add_repository_for_operation(
        "committed-old", "acme/committed", "main", 0.72
    )

    now[0] = 1099.0
    assert store.purge_expired_repository_add_operations(100.0, limit=2) == 0
    for operation_id in ("pending-old", "failed-old-a", "failed-old-b", "committed-old"):
        assert store.get_repository_add_operation(operation_id) is not None

    now[0] = 1100.0
    assert store.purge_expired_repository_add_operations(100.0, limit=2) == 2
    assert store.get_repository_add_operation("pending-old")["state"] == "PENDING"
    assert store.get_repository(repository["id"])["github_repository"] == "acme/committed"
    assert len(store.list_nodes(repository["id"])) == 2

    # A second short transaction drains only the remaining eligible batch. The
    # old PENDING operation remains recoverable regardless of its age.
    assert store.purge_expired_repository_add_operations(100.0, limit=2) == 1
    assert store.purge_expired_repository_add_operations(100.0, limit=2) == 0
    assert store.get_repository_add_operation("pending-old")["state"] == "PENDING"
    assert store.get_repository(repository["id"]) is not None


def test_repository_add_operation_purge_isolates_recent_and_unrelated_rows(tmp_path):
    now = [2000.0]
    store = Store(tmp_path / "operation-retention-isolation.sqlite", clock=lambda: now[0])
    store.begin_repository_add_operation("expired", "acme/expired", None)
    store.fail_repository_add_operation("expired", "missing")

    now[0] = 2090.0
    store.begin_repository_add_operation("recent", "acme/recent", None)
    store.fail_repository_add_operation("recent", "missing")
    unrelated = store.add_repository("acme/unrelated", "main", 0.72)

    now[0] = 2101.0
    assert store.purge_expired_repository_add_operations(100.0, limit=10) == 1
    assert store.get_repository_add_operation("expired") is None
    assert store.get_repository_add_operation("recent")["state"] == "FAILED"
    assert store.get_repository(unrelated["id"])["github_repository"] == "acme/unrelated"


def test_repository_add_operation_purge_validates_policy(tmp_path):
    store = Store(tmp_path / "operation-retention-validation.sqlite")
    for retention in (0, -1, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="retention_seconds"):
            store.purge_expired_repository_add_operations(retention, limit=1)
    for limit in (0, -1, True, 1.5):
        with pytest.raises(ValueError, match="limit"):
            store.purge_expired_repository_add_operations(60, limit=limit)


def test_terminal_operation_retention_preserves_status_and_idempotent_replay_until_cutoff(tmp_path):
    """Terminal results remain authoritative through the inclusive retention window."""
    now = [1000.0]
    store = Store(tmp_path / "operation-retention-window.sqlite", clock=lambda: now[0])

    store.begin_repository_add_operation("failed-window", "acme/failed-window", None)
    failed = store.fail_repository_add_operation("failed-window", "not found")
    store.begin_repository_add_operation(
        "committed-window", "acme/committed-window", "release"
    )
    repository = store.add_repository_for_operation(
        "committed-window", "acme/committed-window", "release", 0.72
    )

    now[0] = 1099.999
    assert store.purge_expired_repository_add_operations(100.0, limit=10) == 0
    assert store.begin_repository_add_operation(
        "failed-window", "acme/failed-window", None
    ) == failed
    replayed = store.begin_repository_add_operation(
        "committed-window", "acme/committed-window", "release"
    )
    assert replayed["state"] == "COMMITTED"
    assert replayed["repository_id"] == repository["id"]
    assert replayed["terminal_at"] == 1000.0

    # Eligibility is inclusive at terminal_at <= now - retention_seconds.
    now[0] = 1100.0
    assert store.purge_expired_repository_add_operations(100.0, limit=10) == 2
    assert store.get_repository_add_operation("failed-window") is None
    assert store.get_repository_add_operation("committed-window") is None
    assert store.get_repository(repository["id"])["github_repository"] == (
        "acme/committed-window"
    )
    assert len(store.list_nodes(repository["id"])) == 2


def test_cleanup_serializes_with_concurrent_registration_failure_and_commit(tmp_path):
    """Concurrent lifecycle transitions cannot age-purge PENDING or fresh terminals."""
    now = [1000.0]
    store = Store(tmp_path / "operation-retention-concurrency.sqlite", clock=lambda: now[0])

    for index in range(12):
        operation_id = f"expired-{index}"
        store.begin_repository_add_operation(
            operation_id, f"acme/expired-{index}", None
        )
        store.fail_repository_add_operation(operation_id, "expired failure")

    pending_ids = [f"active-{index}" for index in range(12)]
    for index, operation_id in enumerate(pending_ids):
        store.begin_repository_add_operation(
            operation_id, f"acme/active-{index}", "main"
        )

    now[0] = 1200.0
    start = threading.Barrier(4)
    errors = []

    def purge_batches():
        try:
            start.wait(timeout=2)
            for _ in range(8):
                store.purge_expired_repository_add_operations(100.0, limit=2)
        except BaseException as error:
            errors.append(error)

    def fail_active():
        try:
            start.wait(timeout=2)
            for index in range(0, 12, 2):
                store.fail_repository_add_operation(
                    pending_ids[index], "fresh failure"
                )
        except BaseException as error:
            errors.append(error)

    def commit_active():
        try:
            start.wait(timeout=2)
            for index in range(1, 12, 2):
                store.add_repository_for_operation(
                    pending_ids[index], f"acme/active-{index}", "main", 0.72
                )
        except BaseException as error:
            errors.append(error)

    threads = [
        threading.Thread(target=purge_batches),
        threading.Thread(target=fail_active),
        threading.Thread(target=commit_active),
    ]
    for thread in threads:
        thread.start()
    start.wait(timeout=2)
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert errors == []
    for index, operation_id in enumerate(pending_ids):
        operation = store.get_repository_add_operation(operation_id)
        expected_state = "FAILED" if index % 2 == 0 else "COMMITTED"
        assert operation["state"] == expected_state
        assert operation["terminal_at"] == 1200.0
        if expected_state == "COMMITTED":
            repository = store.get_repository(operation["repository_id"])
            assert repository["github_repository"] == f"acme/active-{index}"
    assert all(
        store.get_repository_add_operation(f"expired-{index}") is None
        for index in range(12)
    )


def test_repository_add_commit_clamps_lifecycle_time_after_clock_rollback(tmp_path):
    """A backward clock cannot roll back the atomic repository/operation commit."""
    now = [1000.0]
    store = Store(tmp_path / "operation-clock-rollback-commit.sqlite", clock=lambda: now[0])
    store.begin_repository_add_operation(
        "rollback-commit", "acme/rollback-commit", "release"
    )

    now[0] = 900.0
    repository = store.add_repository_for_operation(
        "rollback-commit", "acme/rollback-commit", "release", 0.72
    )
    operation = store.get_repository_add_operation("rollback-commit")

    assert operation["state"] == "COMMITTED"
    assert operation["created_at"] == 1000.0
    assert operation["updated_at"] == 1000.0
    assert operation["terminal_at"] == 1000.0
    assert operation["repository_id"] == repository["id"]
    assert store.get_repository(repository["id"])["github_repository"] == (
        "acme/rollback-commit"
    )
    assert len(store.list_nodes(repository["id"])) == 2

    now[0] = 1101.0
    assert store.purge_expired_repository_add_operations(100.0, limit=10) == 1
    assert store.get_repository_add_operation("rollback-commit") is None
    assert store.get_repository(repository["id"]) is not None


def test_repository_add_failure_clamps_to_existing_lifecycle_floor(tmp_path):
    """FAILED settlement cannot precede creation or a prior durable update."""
    now = [1000.0]
    path = tmp_path / "operation-clock-rollback-failure.sqlite"
    store = Store(path, clock=lambda: now[0])
    store.begin_repository_add_operation(
        "rollback-failure", "acme/rollback-failure", None
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE repository_add_operations SET updated_at = 1050 WHERE operation_id = ?",
            ("rollback-failure",),
        )
        connection.commit()

    now[0] = 900.0
    failed = store.fail_repository_add_operation(
        "rollback-failure", "metadata unavailable"
    )

    assert failed["state"] == "FAILED"
    assert failed["created_at"] == 1000.0
    assert failed["updated_at"] == 1050.0
    assert failed["terminal_at"] == 1050.0
    assert failed["error"] == "metadata unavailable"
    assert store.list_repositories() == []


def test_pending_add_recovery_clamps_each_operation_after_clock_rollback(tmp_path):
    """Startup recovery uses each PENDING row's own durable lifecycle floor."""
    now = [1000.0]
    path = tmp_path / "operation-clock-rollback-recovery.sqlite"
    store = Store(path, clock=lambda: now[0])
    store.begin_repository_add_operation("rollback-old", "acme/old", None)
    now[0] = 1100.0
    store.begin_repository_add_operation("rollback-new", "acme/new", None)

    now[0] = 900.0
    assert store.recover_pending_repository_add_operations() == 2
    old = store.get_repository_add_operation("rollback-old")
    new = store.get_repository_add_operation("rollback-new")

    assert (old["state"], old["updated_at"], old["terminal_at"]) == (
        "FAILED", 1000.0, 1000.0
    )
    assert (new["state"], new["updated_at"], new["terminal_at"]) == (
        "FAILED", 1100.0, 1100.0
    )
    assert store.list_repositories() == []


def test_reopening_migrated_operation_history_does_not_rewrite_rows(tmp_path):
    """Normal startup must not update complete retained idempotency history."""
    path = tmp_path / "operation-noop-reopen.sqlite"
    store = Store(path, clock=lambda: 1000.0)
    for index in range(300):
        operation_id = f"complete-{index:03d}"
        store.begin_repository_add_operation(
            operation_id, f"acme/complete-{index}", None
        )
        store.fail_repository_add_operation(operation_id, "retained failure")

    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE operation_update_audit(count INTEGER NOT NULL);
            INSERT INTO operation_update_audit VALUES (0);
            CREATE TRIGGER audit_repository_add_operation_update
            AFTER UPDATE ON repository_add_operations
            BEGIN
                UPDATE operation_update_audit SET count = count + 1;
            END;
            """
        )
        before = connection.execute(
            """SELECT operation_id, state, created_at, updated_at, terminal_at
            FROM repository_add_operations ORDER BY operation_id"""
        ).fetchall()
        connection.commit()

    # A different clock makes any accidental startup rewrite observable in both the
    # audit trigger and row values. Schema/index checks remain safe to repeat.
    Store(path, clock=lambda: 5000.0)
    Store(path, clock=lambda: 6000.0)

    with sqlite3.connect(path) as connection:
        after = connection.execute(
            """SELECT operation_id, state, created_at, updated_at, terminal_at
            FROM repository_add_operations ORDER BY operation_id"""
        ).fetchall()
        updates = connection.execute(
            "SELECT count FROM operation_update_audit"
        ).fetchone()[0]
    assert updates == 0
    assert after == before


def test_operation_lifecycle_backfill_repairs_only_incomplete_rows(tmp_path):
    """Selective migration repairs lifecycle metadata without touching valid rows."""
    path = tmp_path / "operation-selective-backfill.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE repositories (
            id INTEGER PRIMARY KEY,
            github_repository TEXT NOT NULL UNIQUE,
            target_branch TEXT NOT NULL,
            similarity_threshold REAL NOT NULL,
            tracked INTEGER NOT NULL DEFAULT 1 CHECK (tracked IN (0, 1))
        );
        CREATE TABLE repository_add_operations (
            operation_id TEXT PRIMARY KEY,
            github_repository TEXT NOT NULL,
            target_branch TEXT,
            state TEXT NOT NULL CHECK (state IN ('PENDING', 'COMMITTED', 'FAILED')),
            repository_id INTEGER REFERENCES repositories(id),
            error TEXT,
            created_at REAL,
            updated_at REAL,
            terminal_at REAL
        );
        INSERT INTO repository_add_operations VALUES
            ('complete', 'acme/complete', NULL, 'FAILED', NULL, 'kept', 10, 20, 20),
            ('missing-created', 'acme/missing-created', NULL, 'FAILED', NULL, 'repair', NULL, 15, NULL),
            ('regressed-updated', 'acme/regressed-updated', NULL, 'PENDING', NULL, NULL, 30, 25, 99),
            ('terminal-before-update', 'acme/terminal-before-update', NULL, 'FAILED', NULL, 'repair', 40, 50, 45);
        CREATE TABLE operation_update_audit(operation_id TEXT PRIMARY KEY, count INTEGER NOT NULL);
        INSERT INTO operation_update_audit VALUES
            ('complete', 0), ('missing-created', 0),
            ('regressed-updated', 0), ('terminal-before-update', 0);
        CREATE TRIGGER audit_repository_add_operation_update
        AFTER UPDATE ON repository_add_operations
        BEGIN
            UPDATE operation_update_audit
            SET count = count + 1 WHERE operation_id = NEW.operation_id;
        END;
        """
    )
    connection.commit()
    connection.close()

    store = Store(path, clock=lambda: 1000.0)
    complete = store.get_repository_add_operation("complete")
    missing = store.get_repository_add_operation("missing-created")
    pending = store.get_repository_add_operation("regressed-updated")
    terminal = store.get_repository_add_operation("terminal-before-update")

    assert (complete["created_at"], complete["updated_at"], complete["terminal_at"]) == (
        10.0, 20.0, 20.0
    )
    assert (missing["created_at"], missing["updated_at"], missing["terminal_at"]) == (
        1000.0, 1000.0, 1000.0
    )
    assert (pending["created_at"], pending["updated_at"], pending["terminal_at"]) == (
        30.0, 30.0, None
    )
    assert (terminal["created_at"], terminal["updated_at"], terminal["terminal_at"]) == (
        40.0, 50.0, 50.0
    )
    with sqlite3.connect(path) as connection:
        counts = dict(connection.execute(
            "SELECT operation_id, count FROM operation_update_audit"
        ))
    assert counts == {
        "complete": 0,
        "missing-created": 1,
        "regressed-updated": 1,
        "terminal-before-update": 1,
    }
