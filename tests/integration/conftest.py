"""Fixtures of the integration suite: one environment (database, migrations, API) per test session."""

from collections.abc import AsyncIterator

import pytest

from tests.integration.environment import Environment
from tests.integration.support import set_environment


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark every test of this package as an integration test."""
    for item in items:
        if "tests/integration" in item.nodeid.replace("\\", "/"):
            item.add_marker(pytest.mark.integration)


@pytest.fixture(scope="session", autouse=True)
async def environment() -> AsyncIterator[Environment]:
    """Create the database, migrate it and serve the API for the whole session."""
    env = Environment()
    try:
        await env.start()
        set_environment(env)
        yield env
    finally:
        await env.stop()
