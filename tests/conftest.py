"""Shared pytest configuration. Registers the `slow` marker so
tests/test_reallocation.py's full-run test carries a known mark
(skipped by default; enabled with RUN_SLOW=1)."""


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "slow: full physics run, skipped unless RUN_SLOW=1")
