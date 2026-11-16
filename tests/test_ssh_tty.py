"""ssh must never leave the user's terminal in raw mode (a timed-out ssh is
killed and can't restore it, so the report printed as a staircase)."""

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import cctrack

SRC = str(Path(cctrack.__file__).resolve().parent.parent)


def test_ssh_calls_use_no_pty_and_no_terminal_stdin():
    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        cctrack.fetch_remote_jsonl("dgx")
        cctrack.fetch_remote_codex("dgx")
        cctrack.fetch_remote_hook_data("dgx")
    ssh_calls = [(c, kw) for c, kw in calls if c[0] == "ssh"]
    assert len(ssh_calls) >= 4  # sandbox roots + three fetches
    for cmd, kw in ssh_calls:
        assert "-T" in cmd
        assert kw.get("stdin") is subprocess.DEVNULL or "input" in kw


@pytest.mark.skipif(not hasattr(os, "fork") or sys.platform == "win32", reason="needs a pty")
def test_terminal_settings_restored_after_killed_child():
    import pty
    code = (
        "import sys, subprocess, termios\n"
        f"sys.path.insert(0, {SRC!r})\n"
        "import cctrack\n"
        "try:\n"
        "    cctrack._run_ssh(['sh', '-c', 'stty raw -onlcr < /dev/tty; sleep 5'],\n"
        "                     capture_output=True, timeout=1)\n"
        "except subprocess.TimeoutExpired:\n"
        "    pass\n"
        "print('ONLCR', bool(termios.tcgetattr(1)[1] & termios.ONLCR))\n"
    )
    pid, fd = pty.fork()
    if pid == 0:
        os.execv(sys.executable, [sys.executable, "-c", code])
    out = b""
    while True:
        try:
            chunk = os.read(fd, 1024)
        except OSError:
            break
        if not chunk:
            break
        out += chunk
    os.waitpid(pid, 0)
    assert "ONLCR True" in out.decode(errors="replace")


@pytest.mark.parametrize("fetch,what", [
    (cctrack.fetch_remote_jsonl, "Claude Code transcripts"),
    (cctrack.fetch_remote_hook_data, "statusline hook data"),
    (cctrack.fetch_remote_codex, "Codex rollouts"),
])
def test_fetch_timeout_is_configurable_and_reported(fetch, what, monkeypatch, capsys):
    monkeypatch.setenv("CCTRACK_SSH_TIMEOUT", "42")
    monkeypatch.setattr(cctrack, "remote_sandbox_roots", lambda h: [])
    seen = []

    def fake_run(cmd, **kw):
        seen.append(kw["timeout"])
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])

    with patch("subprocess.run", side_effect=fake_run):
        assert fetch("dgx") == ""
    assert seen == [42.0]
    err = capsys.readouterr().err
    assert "timed out after 42s" in err and what in err and "MISSING" in err
