"""Pins the user-facing documentation of the parse cache."""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _read(name):
    p = ROOT / name
    if not p.is_file():
        pytest.skip(f"{name} not available")
    return p.read_text(encoding="utf-8")


def test_readme_documents_the_cache():
    readme = _read("README.md")
    assert "(#parse-cache)" in readme
    section = readme.split("## Parse cache", 1)[1].split("\n## ", 1)[0]
    for need in ("~/.cache/cctrack/cache.sqlite3", "$XDG_CACHE_HOME/cctrack",
                 "$CCTRACK_CACHE_DIR", "--rebuild-cache", "--no-cache", "CCTRACK_CACHE=0",
                 "never", "prompt or response text", "first run after any change to cctrack",
                 "two seconds", "30 days", "CCTRACK_REMOTE_PARSE=0", "cctrack: cache",
                 "cache: N reused, M read"):
        assert need in section, need


def test_claude_md_documents_the_cache():
    text = _read("CLAUDE.md")
    for need in ("4a. **Parse cache**", "cctrack --rebuild-cache", "CCTRACK_CACHE=0",
                 "tests/test_docs.py", "_remote_bundle_cache"):
        assert need in text, need
    assert "Results are cached per host" not in text
