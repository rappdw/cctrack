"""Shared test isolation.

cctrack discovers Sandy sandboxes by running `sandy --print-state` and by
reading $SANDY_HOME. Tests must never touch a developer's real sandboxes, so by
default the sandy probe returns "unavailable" and SANDY_HOME points at an empty
temp dir. Tests that exercise the probe itself use the `real_sandy_probe` marker.
"""
import pytest

import cctrack


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "real_sandy_probe: don't stub cctrack._sandy_print_state")


@pytest.fixture(autouse=True)
def _isolate_sandy(request, monkeypatch, tmp_path):
    monkeypatch.setenv("SANDY_HOME", str(tmp_path / "no-sandy"))
    if not request.node.get_closest_marker("real_sandy_probe"):
        monkeypatch.setattr(cctrack, "_sandy_print_state", lambda: None)
    cctrack._remote_roots_cache.clear()
    yield
    cctrack._remote_roots_cache.clear()
