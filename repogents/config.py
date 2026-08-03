from __future__ import annotations

import math
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
    codex_api_base: str = "http://127.0.0.1:8787/v1"
    model: str = "gpt-5.6-sol"
    similarity_threshold: float = 0.75
    promotion_threshold: int = 3
    stale_run_threshold: int = 3
    github_request_timeout: float = 30.0
    git_command_timeout: float = 300.0
    http_request_io_timeout: float = 30.0
    repository_add_operation_retention_seconds: float = 7 * 24 * 60 * 60
    repository_add_operation_cleanup_batch_size: int = 100

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
            github_request_timeout=float(
                os.getenv("REPOGENTS_GITHUB_REQUEST_TIMEOUT", "30")
            ),
            git_command_timeout=float(
                os.getenv("REPOGENTS_GIT_COMMAND_TIMEOUT", "300")
            ),
            http_request_io_timeout=float(
                os.getenv("REPOGENTS_HTTP_REQUEST_IO_TIMEOUT", "30")
            ),
            repository_add_operation_retention_seconds=float(
                os.getenv("REPOGENTS_ADD_OPERATION_RETENTION_SECONDS", str(7 * 24 * 60 * 60))
            ),
            repository_add_operation_cleanup_batch_size=int(
                os.getenv("REPOGENTS_ADD_OPERATION_CLEANUP_BATCH_SIZE", "100")
            ),
        )
        if not config.host:
            raise ValueError("REPOGENTS_LAN_HOST must be nonempty")
        if not 0 <= config.port <= 65535:
            raise ValueError("REPOGENTS_LAN_PORT must be between 0 and 65535")
        if config.poll_seconds <= 0:
            raise ValueError("REPOGENTS_POLL_SECONDS must be positive")
        if not 0 <= config.similarity_threshold < 1:
            raise ValueError(
                "REPOGENTS_SIMILARITY_THRESHOLD must be at least 0 and less than 1"
            )
        if config.promotion_threshold <= 0 or config.stale_run_threshold <= 0:
            raise ValueError("node thresholds must be positive")
        if (
            not math.isfinite(config.github_request_timeout)
            or config.github_request_timeout <= 0
        ):
            raise ValueError(
                "REPOGENTS_GITHUB_REQUEST_TIMEOUT must be finite and positive"
            )
        if (
            not math.isfinite(config.git_command_timeout)
            or config.git_command_timeout <= 0
        ):
            raise ValueError(
                "REPOGENTS_GIT_COMMAND_TIMEOUT must be finite and positive"
            )
        if (
            not math.isfinite(config.http_request_io_timeout)
            or config.http_request_io_timeout <= 0
        ):
            raise ValueError(
                "REPOGENTS_HTTP_REQUEST_IO_TIMEOUT must be finite and positive"
            )
        if (
            not math.isfinite(config.repository_add_operation_retention_seconds)
            or config.repository_add_operation_retention_seconds <= 0
        ):
            raise ValueError(
                "REPOGENTS_ADD_OPERATION_RETENTION_SECONDS must be finite and positive"
            )
        if config.repository_add_operation_cleanup_batch_size <= 0:
            raise ValueError("REPOGENTS_ADD_OPERATION_CLEANUP_BATCH_SIZE must be positive")
        return config
