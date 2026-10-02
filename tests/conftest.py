"""
Shared pytest fixtures for test isolation.

Problem: caching_manager is a module-level singleton in server.py. Its in-memory
cache (HybridCaching class variables) and disk cache (SQLite at ~/.cache/) persist
across tests within a run and across separate runs. Tests that mock faostat_get
to raise errors depend on a cache miss — without isolation, a disk hit from a
previous test or real server usage causes the mock to be bypassed entirely.

Solution: autouse fixture that resets in-memory state and swaps in a fresh
temporary DiskCache for each test, then restores the original after.
"""

import pathlib
import pytest

from faostat_mcp.client import DiskCache, HybridCaching


@pytest.fixture(autouse=True)
def no_update_check(monkeypatch):
    """Keep unit tests off the network: the PyPI update check is opt-in per test."""
    monkeypatch.setenv("FAOSTAT_NO_UPDATE_CHECK", "1")


@pytest.fixture(autouse=True)
def reset_cache_between_tests(tmp_path):
    """Ensure each test starts with a clean in-memory and disk cache."""
    # 1. Reset in-memory class-level state
    HybridCaching.user_caches = {}
    HybridCaching.min_heap_ttl = []

    # 2. Swap in a fresh temp DiskCache (isolated per test, auto-cleaned by pytest)
    import faostat_mcp.server as server_module
    original_disk_cache = server_module.caching_manager.disk_cache
    server_module.caching_manager.disk_cache = DiskCache(
        db_path=tmp_path / "test_cache.db",
        ttl=3600,
    )

    yield

    # Restore original disk cache after the test
    server_module.caching_manager.disk_cache = original_disk_cache
