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
    config.addinivalue_line(
        "markers", "real_remote_bundle: don't stub cctrack.fetch_remote_bundle")


@pytest.fixture(autouse=True)
def _isolate_sandy(request, monkeypatch, tmp_path):
    monkeypatch.setenv("SANDY_HOME", str(tmp_path / "no-sandy"))
    # main() tries remote parsing before the raw fetches; tests that mock the
    # raw fetches must never reach a real `ssh host python3 -`.
    if not request.node.get_closest_marker("real_remote_bundle"):
        monkeypatch.setattr(cctrack, "fetch_remote_bundle",
                            lambda host, *a, **k: {"fallback": True})
    if not request.node.get_closest_marker("real_sandy_probe"):
        monkeypatch.setattr(cctrack, "_sandy_print_state", lambda: None)
    cctrack._remote_roots_cache.clear()
    cctrack._remote_bundle_cache.clear()
    monkeypatch.setattr(cctrack, "_sandy_state_cache", cctrack._SANDY_STATE_UNSET)
    yield
    cctrack._remote_roots_cache.clear()
