"""The FastAPI application: routes, the reference's 404/405 behaviour, operational endpoints and the lifespan."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib import resources
from typing import Any

from fastapi import FastAPI
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import Response

from healthcare.api.context import EndpointOptions, RequestPreparer
from healthcare.api.pipeline import ASGIApp, OuterMiddleware
from healthcare.api.responses import json_response
from healthcare.api.routes import register_routes
from healthcare.api.routing import Ctx, Router
from healthcare.infrastructure.bootstrap import Container, build_container
from healthcare.infrastructure.config import Settings, load_settings
from healthcare.infrastructure.telemetry import configure_logging, configure_tracing, shutdown_tracing

_LOGGER = logging.getLogger("healthcare.api")
ALL_METHODS = ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]


def openapi_document() -> bytes:
    """Return the API contract served at /openapi/v1.yaml (a copy of docs/openapi.yaml)."""
    return resources.files("healthcare.api").joinpath("openapi.yaml").read_bytes()


def _health(status: int, text: str) -> Response:
    """Write a plain-text health report that caches must not store."""
    return Response(
        text.encode("ascii"),
        status_code=status,
        headers={
            "content-type": "text/plain",
            "pragma": "no-cache",
            "expires": "Thu, 01 Jan 1970 00:00:00 GMT",
        },
    )


def register_operational_routes(api: Router, container: Container) -> None:
    """Declare the health checks, the signing key set and the API contract."""
    jwks = container.signing_key.jwks()
    contract = openapi_document()

    async def live(ctx: Ctx) -> Response:
        """Report that the process is running."""
        return _health(200, "Healthy")

    async def ready(ctx: Ctx) -> Response:
        """Report whether the database and the cache can be used."""
        try:
            await container.database.ping()
        except Exception:
            return _health(503, "Unhealthy")
        if not await container.cache.probe():
            return _health(200, "Degraded")
        return _health(200, "Healthy")

    for method in ALL_METHODS:
        api.route(method, "/health/live", anonymous=True, unlimited=True)(live)
        api.route(method, "/health/ready", anonymous=True, unlimited=True)(ready)

    @api.route("GET", "/.well-known/jwks.json", anonymous=True)
    async def keys(ctx: Ctx) -> Response:
        """Serve the public signing keys."""
        return json_response(jwks)

    @api.route("GET", "/openapi/v1.yaml", anonymous=True)
    async def openapi(ctx: Ctx) -> Response:
        """Serve the API contract."""
        return Response(contract, headers={"content-type": "application/yaml; charset=utf-8"})


def create_app(container: Container, start_workers: bool = True) -> ASGIApp:
    """Build the ASGI application around a wired container."""
    settings = container.settings
    preparer = RequestPreparer(settings, container.jwt, container.access)
    api = Router(preparer, container.clock, settings.forwarded_headers)
    register_routes(api, container.services)
    register_operational_routes(api, container)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        """Start the background workers and release every resource on shutdown."""
        if start_workers:
            container.start_workers()
        _LOGGER.info("Now listening", extra={"port": settings.http_port, "environment": settings.environment})
        try:
            yield
        finally:
            _LOGGER.info("Shutting down")
            await container.close()
            shutdown_tracing()
            _LOGGER.info("Stopped")

    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None, lifespan=lifespan, redirect_slashes=False)
    app.state.container = container
    for spec in api.routes:
        app.add_api_route(spec.path, api.endpoint(spec), methods=[spec.method], include_in_schema=False)

    async def http_exception(request: Request, error: Exception) -> Response:
        """Turn the router's 404 and 405 into the reference's empty responses after authentication."""
        if isinstance(error, StarletteHTTPException) and api.resolve(request.method, request.scope["path"]) == 405:
            return await api.method_not_allowed(request)
        return await api.unmatched(request)

    app.add_exception_handler(StarletteHTTPException, http_exception)
    tracer = configure_tracing(settings.otlp_endpoint)
    return OuterMiddleware(app, settings, tracer)


def create_production_app() -> Any:
    """Application factory for Uvicorn and Gunicorn workers: configuration from the environment."""
    settings: Settings = load_settings()
    configure_logging(settings.log_level)
    return create_app(build_container(settings))


__all__ = ["EndpointOptions", "create_app", "create_production_app"]
