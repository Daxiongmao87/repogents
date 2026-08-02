import math
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from sentence_transformers import SentenceTransformer


DEFAULT_MODEL = "all-MiniLM-L6-v2"
MODEL_CACHE_DIRECTORY = "models--sentence-transformers--all-MiniLM-L6-v2"
REQUIRED_SNAPSHOT_FILES = (
    "modules.json",
    "config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "1_Pooling/config.json",
)


def _is_complete_snapshot(snapshot: Path) -> bool:
    has_weights = (snapshot / "model.safetensors").is_file() or (
        snapshot / "pytorch_model.bin"
    ).is_file()
    return has_weights and all(
        (snapshot / relative_path).is_file()
        for relative_path in REQUIRED_SNAPSHOT_FILES
    )


def _cache_roots() -> list[Path]:
    roots = []
    if hub_cache := os.environ.get("HF_HUB_CACHE"):
        roots.append(Path(hub_cache))
    if hf_home := os.environ.get("HF_HOME"):
        roots.append(Path(hf_home) / "hub")
    roots.append(Path.home() / ".cache" / "huggingface" / "hub")
    return roots


def _find_default_snapshot() -> Path:
    for cache_root in _cache_roots():
        repository_cache = cache_root / MODEL_CACHE_DIRECTORY
        snapshots = repository_cache / "snapshots"
        preferred = []
        main_ref = repository_cache / "refs" / "main"
        if main_ref.is_file():
            revision = main_ref.read_text().strip()
            if revision:
                preferred.append(snapshots / revision)
        if snapshots.is_dir():
            preferred.extend(sorted(snapshots.iterdir()))
        for snapshot in preferred:
            if _is_complete_snapshot(snapshot):
                return snapshot
    raise RuntimeError(
        "all-MiniLM-L6-v2 is not installed in the Hugging Face cache"
    )


def validate_classification(label: str) -> str:
    normalized = label.strip()
    levels = normalized.split("/")
    if len(levels) not in (1, 2) or any(not level.strip() for level in levels):
        raise ValueError("classification must contain one or two nonempty levels")
    return normalized


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    dot_product = sum(float(a) * float(b) for a, b in zip(left, right))
    left_norm = math.sqrt(sum(float(value) ** 2 for value in left))
    right_norm = math.sqrt(sum(float(value) ** 2 for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot_product / (left_norm * right_norm)


class SentenceTransformerEmbedder:
    def __init__(self, model_name_or_path: str | Path | None = None):
        model = (
            model_name_or_path
            if model_name_or_path is not None
            else str(_find_default_snapshot())
        )
        self._model = SentenceTransformer(
            model,
            local_files_only=True,
            device="cpu",
        )

    def embed(self, label: str) -> list[float]:
        encoded = self._model.encode(label, normalize_embeddings=True)
        vector = [float(value) for value in encoded]
        norm = math.sqrt(sum(value**2 for value in vector))
        if norm == 0.0:
            return vector
        return [float(value / norm) for value in vector]


class Embedder(Protocol):
    def embed(self, label: str) -> list[float]: ...


class SemanticRouter:
    def __init__(self, embedder: Embedder):
        self._embedder = embedder

    def route(
        self,
        classification: str,
        nodes: list[dict],
        threshold: float,
        vector: list[float] | None = None,
    ) -> tuple[dict | None, list[float]]:
        normalized = validate_classification(classification)
        routed_vector = (
            self._embedder.embed(normalized) if vector is None else vector
        )
        closest = None
        closest_similarity = -math.inf
        for node in nodes:
            similarity = cosine_similarity(routed_vector, node["vector"])
            if similarity > closest_similarity:
                closest = node
                closest_similarity = similarity
        if closest_similarity > threshold:
            return closest, routed_vector
        return None, routed_vector
