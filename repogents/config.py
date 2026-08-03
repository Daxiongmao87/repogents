from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ServiceConfig:
    data_dir: Path
    github_token: str
    host: str = "0.0.0.0"
    port: int = 8766
    poll_seconds: float = 60.0
    pr_silence_seconds: float = 3600.0
    codex_api_base: str = "http://127.0.0.1:8787/v1"
    model: str = "gpt-5.6-sol"
    similarity_threshold: float = 0.75
    promotion_threshold: int = 3
    stale_run_threshold: int = 3

    @classmethod
    def from_env(cls) -> "ServiceConfig":
        github_token = os.getenv("REPOGENTS_GITHUB_TOKEN", "").strip()
        if not github_token:
            raise ValueError("REPOGENTS_GITHUB_TOKEN is required")
        config = cls(
            data_dir=Path(os.getenv("REPOGENTS_DATA_DIR", "runtime")),
            github_token=github_token,
            host=os.getenv("REPOGENTS_LAN_HOST", "0.0.0.0"),
            port=int(os.getenv("REPOGENTS_LAN_PORT", "8766")),
            poll_seconds=float(os.getenv("REPOGENTS_POLL_SECONDS", "60")),
            pr_silence_seconds=float(
                os.getenv("REPOGENTS_PR_SILENCE_SECONDS", "3600")
            ),
            codex_api_base=os.getenv(
                "REPOGENTS_CODEX_API_BASE", "http://127.0.0.1:8787/v1"
            ),
            similarity_threshold=float(
                os.getenv("REPOGENTS_SIMILARITY_THRESHOLD", "0.75")
            ),
            promotion_threshold=int(
                os.getenv("REPOGENTS_NODE_PROMOTION_THRESHOLD", "3")
            ),
            stale_run_threshold=int(
                os.getenv("REPOGENTS_NODE_STALE_RUN_THRESHOLD", "3")
            ),
        )
        if not config.host:
            raise ValueError("REPOGENTS_LAN_HOST must be nonempty")
        if not 0 <= config.port <= 65535:
            raise ValueError("REPOGENTS_LAN_PORT must be between 0 and 65535")
        if config.poll_seconds <= 0:
            raise ValueError("REPOGENTS_POLL_SECONDS must be positive")
        if (
            not math.isfinite(config.pr_silence_seconds)
            or config.pr_silence_seconds <= 0
        ):
            raise ValueError("REPOGENTS_PR_SILENCE_SECONDS must be positive")
        if not 0 <= config.similarity_threshold < 1:
            raise ValueError(
                "REPOGENTS_SIMILARITY_THRESHOLD must be at least 0 and less than 1"
            )
        if config.promotion_threshold <= 0 or config.stale_run_threshold <= 0:
            raise ValueError("node thresholds must be positive")
        return config
