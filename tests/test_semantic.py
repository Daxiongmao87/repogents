import json
import math
from pathlib import Path

import pytest

from repogents.semantic import (
    SemanticRouter,
    SentenceTransformerEmbedder,
    cosine_similarity,
    validate_classification,
)


class StubEmbedder:
    def __init__(self, vectors):
        self.vectors = vectors

    def embed(self, label):
        return self.vectors[label]


def test_validate_classification_accepts_trimmed_one_or_two_level_labels():
    assert validate_classification("  backend  ") == "backend"
    assert validate_classification("  backend / api  ") == "backend / api"


@pytest.mark.parametrize(
    "label",
    ["", "   ", "/backend", "backend/", "backend//api", "one/two/three"],
)
def test_validate_classification_rejects_invalid_labels(label):
    with pytest.raises(ValueError):
        validate_classification(label)


def test_cosine_similarity_has_deterministic_direction_and_zero_behavior():
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)
    assert cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0
    assert cosine_similarity([0.0, 0.0], [0.0, 0.0]) == 0.0


def test_router_fuzzily_selects_closest_persisted_vector_by_cosine():
    vector = [1.0, 1.0]
    router = SemanticRouter(StubEmbedder({"services/web": vector}))
    nodes = [
        {"id": 1, "classification": "large magnitude", "vector": [100.0, 0.0]},
        {"id": 2, "classification": "platform/api", "vector": [0.8, 0.6]},
    ]

    node, generated_vector = router.route("services/web", nodes, threshold=0.9)

    assert node is nodes[1]
    assert generated_vector is vector


def test_router_requires_similarity_to_be_strictly_greater_than_threshold():
    vector = [1.0, 0.0]
    router = SemanticRouter(StubEmbedder({"backend/api": vector}))
    nodes = [{"id": 1, "vector": [1.0, 0.0]}]

    node, generated_vector = router.route("backend/api", nodes, threshold=1.0)

    assert node is None
    assert generated_vector is vector


def test_router_returns_none_and_generated_vector_on_semantic_miss():
    vector = [1.0, 0.0]
    router = SemanticRouter(StubEmbedder({"backend/api": vector}))
    nodes = [{"id": 1, "vector": [0.0, 1.0]}]

    node, generated_vector = router.route("backend/api", nodes, threshold=0.5)

    assert node is None
    assert generated_vector is vector


def test_router_uses_supplied_persisted_vector_without_embedding():
    class FailingEmbedder:
        def embed(self, label):
            raise AssertionError(f"unexpected embedding for {label}")

    vector = [1.0, 0.0]
    nodes = [{"id": 1, "vector": [1.0, 0.0]}]
    router = SemanticRouter(FailingEmbedder())

    node, routed_vector = router.route(
        "backend/api", nodes, threshold=0.75, vector=vector
    )

    assert node is nodes[0]
    assert routed_vector is vector


def _complete_snapshot(cache_root, revision):
    snapshot = (
        cache_root
        / "models--sentence-transformers--all-MiniLM-L6-v2"
        / "snapshots"
        / revision
    )
    for relative_path in (
        "modules.json",
        "config.json",
        "model.safetensors",
        "tokenizer.json",
        "tokenizer_config.json",
        "vocab.txt",
        "1_Pooling/config.json",
    ):
        path = snapshot / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
    return snapshot


@pytest.fixture
def transformer_calls(monkeypatch):
    calls = []

    class FakeSentenceTransformer:
        def __init__(self, model_name_or_path, **kwargs):
            calls.append((model_name_or_path, kwargs))

        def encode(self, label, **kwargs):
            calls.append((label, kwargs))
            return [3, 4]

    monkeypatch.setattr(
        "repogents.semantic.SentenceTransformer", FakeSentenceTransformer
    )
    return calls


def test_default_embedder_prefers_hf_hub_cache_and_loads_local_cpu(
    tmp_path, monkeypatch, transformer_calls
):
    hub_snapshot = _complete_snapshot(tmp_path / "hub", "hub-revision")
    _complete_snapshot(tmp_path / "hf-home" / "hub", "home-revision")
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hub"))
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf-home"))

    SentenceTransformerEmbedder()

    assert transformer_calls[0] == (
        str(hub_snapshot),
        {"local_files_only": True, "device": "cpu"},
    )


def test_default_embedder_uses_hf_home_cache_when_hub_cache_is_unset(
    tmp_path, monkeypatch, transformer_calls
):
    snapshot = _complete_snapshot(tmp_path / "hf-home" / "hub", "home-revision")
    monkeypatch.delenv("HF_HUB_CACHE", raising=False)
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf-home"))

    SentenceTransformerEmbedder()

    assert transformer_calls[0][0] == str(snapshot)


def test_default_embedder_uses_standard_user_cache(
    tmp_path, monkeypatch, transformer_calls
):
    snapshot = _complete_snapshot(
        tmp_path / ".cache" / "huggingface" / "hub", "standard-revision"
    )
    monkeypatch.delenv("HF_HUB_CACHE", raising=False)
    monkeypatch.delenv("HF_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    SentenceTransformerEmbedder()

    assert transformer_calls[0][0] == str(snapshot)


def test_explicit_model_path_is_loaded_directly_and_local_only(
    tmp_path, transformer_calls
):
    model_path = tmp_path / "caller-model"

    SentenceTransformerEmbedder(model_path)

    assert transformer_calls[0] == (
        model_path,
        {"local_files_only": True, "device": "cpu"},
    )


def test_default_embedder_fails_explicitly_without_a_complete_snapshot(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("HF_HUB_CACHE", raising=False)
    monkeypatch.delenv("HF_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    incomplete = (
        tmp_path
        / ".cache"
        / "huggingface"
        / "hub"
        / "models--sentence-transformers--all-MiniLM-L6-v2"
        / "snapshots"
        / "incomplete"
    )
    incomplete.mkdir(parents=True)
    (incomplete / "modules.json").write_text("{}")
    monkeypatch.setattr(
        "repogents.semantic.SentenceTransformer",
        lambda *args, **kwargs: pytest.fail("incomplete snapshot was loaded"),
    )

    with pytest.raises(RuntimeError, match="all-MiniLM-L6-v2.*cache"):
        SentenceTransformerEmbedder()


def test_sentence_transformer_embedder_returns_normalized_json_floats(
    tmp_path, transformer_calls
):
    model_path = tmp_path / "caller-model"
    vector = SentenceTransformerEmbedder(model_path).embed("backend/api")

    assert transformer_calls[1] == (
        "backend/api",
        {"normalize_embeddings": True},
    )
    assert vector == pytest.approx([0.6, 0.8])
    assert all(type(value) is float and math.isfinite(value) for value in vector)
    assert json.loads(json.dumps(vector)) == vector
