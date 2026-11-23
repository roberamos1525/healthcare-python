"""Command line: ``python -m healthcare`` serves the API on port 8080; ``python -m healthcare migrate`` migrates.

``serve`` runs Uvicorn with ``HEALTHCARE_WORKERS`` worker processes (default 1); on Linux, Gunicorn with
Uvicorn workers can be used instead (see docs/deployment.md). Uvicorn handles SIGINT/SIGTERM and drains
in-flight requests before the lifespan stops the background workers.
"""

import asyncio
import logging
import sys

from healthcare.infrastructure.config import load_settings
from healthcare.infrastructure.telemetry import configure_logging

_LOGGER = logging.getLogger("healthcare")


def migrate() -> int:
    """Apply pending migrations as the schema owner and exit."""
    from healthcare.infrastructure.persistence.migrate import migrate as run_migrations

    settings = load_settings()
    configure_logging(settings.log_level)
    if not settings.migrations:
        _LOGGER.error("ConnectionStrings:Migrations is required to run migrations.")
        return 1
    asyncio.run(run_migrations(settings.migrations))
    return 0


def serve() -> int:
    """Run the API until a shutdown signal arrives."""
    import uvicorn

    settings = load_settings()
    configure_logging(settings.log_level)
    if settings.migrate_on_startup:
        if not settings.migrations:
            _LOGGER.error("ConnectionStrings:Migrations is required when Database:MigrateOnStartup is enabled.")
            return 1
        from healthcare.infrastructure.persistence.migrate import migrate as run_migrations

        asyncio.run(run_migrations(settings.migrations))
    uvicorn.run(
        "healthcare.api.app:create_production_app",
        factory=True,
        host="0.0.0.0",  # noqa: S104 - the container listens on every interface behind the load balancer
        port=settings.http_port,
        workers=max(settings.workers_per_host, 1),
        http="httptools",
        loop="asyncio",
        server_header=False,
        access_log=False,
        log_config=None,
        timeout_graceful_shutdown=settings.shutdown_timeout_seconds,
        limit_concurrency=None,
        backlog=4096,
    )
    return 0


def main() -> int:
    """Dispatch to ``serve`` (default) or ``migrate``."""
    command = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if command == "migrate":
        return migrate()
    if command == "serve":
        return serve()
    _LOGGER.error("Unknown command %r (use 'serve' or 'migrate').", command)
    return 2


if __name__ == "__main__":
    sys.exit(main())
