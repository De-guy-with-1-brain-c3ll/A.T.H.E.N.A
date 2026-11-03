"""Minerva test configuration for async support."""
import pytest


@pytest.fixture
def event_loop():
    """Create an event loop for each test."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


pytestmark = pytest.mark.asyncio
