from __future__ import annotations

from repogents.agent_runtime import MiniSweRuntime, RuntimeConfig
from repogents.application import Application, ApplicationConfig
from repogents.config import ServiceConfig
from repogents.github import GitHubClient
from repogents.http_api import HttpService
from repogents.semantic import SemanticRouter, SentenceTransformerEmbedder
from repogents.store import Store


def build_service(config: ServiceConfig) -> HttpService:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    store = Store(config.data_dir / "repogents.sqlite3")
    github = GitHubClient(
        config.github_token,
        transport_timeout=config.github_request_timeout,
        git_command_timeout=config.git_command_timeout,
    )
    runtime = MiniSweRuntime(
        RuntimeConfig(api_base=config.codex_api_base, model=config.model)
    )
    router = SemanticRouter(SentenceTransformerEmbedder())
    application = Application(
        store,
        github,
        runtime,
        router,
        ApplicationConfig(
            data_dir=config.data_dir,
            default_similarity_threshold=config.similarity_threshold,
            promotion_threshold=config.promotion_threshold,
            stale_run_threshold=config.stale_run_threshold,
            repository_add_operation_retention_seconds=(
                config.repository_add_operation_retention_seconds
            ),
            repository_add_operation_cleanup_batch_size=(
                config.repository_add_operation_cleanup_batch_size
            ),
        ),
    )
    return HttpService(
        application, config.host, config.port, config.poll_seconds,
        request_io_timeout=config.http_request_io_timeout,
    )


def main() -> None:
    config = ServiceConfig.from_env()
    service = build_service(config)
    host, port = service.address
    print(f"Repogents listening on http://{host}:{port}", flush=True)
    try:
        service.serve_forever()
    except KeyboardInterrupt:
        service.shutdown()


if __name__ == "__main__":
    main()
