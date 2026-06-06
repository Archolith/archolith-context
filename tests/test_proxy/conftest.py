"""Pytest configuration for test_proxy tests."""

import pytest

from archolith_proxy.proxy.session import _reset_sessions


@pytest.fixture(autouse=True)
def reset_sessions_before_test():
    """Reset session state before each test for isolation."""
    _reset_sessions()
    yield
    _reset_sessions()
