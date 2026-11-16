"""Live progress line for slow remote fetches and local scans."""

import io
import subprocess
import sys
import time
from unittest.mock import patch

import pytest

import cctrack


def test_progress_silent_when_stderr_is_not_a_terminal(capsys):
    p = cctrack.Progress("x")
    p.update("detail", force=True)
    p.done()
    assert capsys.readouterr().err == ""


def test_progress_renders_bar_detail_and_clears(monkeypatch):
    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    p = cctrack.Progress("dgx: Claude transcripts", enabled=True)
    p.update("1.5 MB received", force=True)
    p.done()
    out = err.getvalue()
    assert "dgx: Claude transcripts [" in out and "1.5 MB received" in out
    assert "███" in out
    assert out.endswith("\r\033[K")          # line cleared, report starts clean


def test_no_progress_env_disables(monkeypatch):
    monkeypatch.setenv("CCTRACK_NO_PROGRESS", "1")
    assert cctrack.progress_enabled() is False


def test_streaming_run_collects_output_and_shows_bytes(monkeypatch):
    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    cmd = ["sh", "-c", "printf 'hello '; sleep 0.3; printf 'world'; echo oops >&2; exit 3"]
    r = cctrack._run_streaming(cmd, cctrack.Progress("t", enabled=True), timeout=10)
    assert (r.returncode, r.stdout, r.stderr) == (3, "hello world", "oops\n")
    assert "B received" in err.getvalue()


def test_streaming_run_times_out_and_kills():
    t0 = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        cctrack._run_streaming(["sleep", "5"], cctrack.Progress("t", enabled=False), timeout=0.3)
    assert time.monotonic() - t0 < 3


def test_run_ssh_streams_only_with_a_terminal_and_a_label(monkeypatch):
    seen = []

    def fake_streaming(cmd, prog, timeout, input=None, text=True):
        seen.append((prog.label, input))
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(cctrack, "_run_streaming", fake_streaming)
    monkeypatch.setattr(cctrack, "progress_enabled", lambda: True)
    cctrack._run_ssh(["true"], progress="dgx: x", capture_output=True, text=True, timeout=5)
    cctrack._run_ssh(["true"], progress="dgx: y", input=b"prog", capture_output=True, timeout=5)
    assert seen == [("dgx: x", None), ("dgx: y", b"prog")]
    with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
        cctrack._run_ssh(["true"], capture_output=True, text=True, timeout=5)  # no label
    assert run.call_count == 1 and len(seen) == 2
    monkeypatch.setattr(cctrack, "progress_enabled", lambda: False)
    with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
        cctrack._run_ssh(["true"], progress="dgx: z", capture_output=True, timeout=5)
    assert run.call_count == 1 and len(seen) == 2                            # no terminal


def test_streaming_run_feeds_input_and_relays_remote_progress(monkeypatch):
    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    script = ("import sys,time; data=sys.stdin.read(); "
              "sys.stderr.write('CCTRACK_PROGRESS parsing: 3/9 files\\n'); sys.stderr.flush(); "
              "time.sleep(0.3); sys.stderr.write('real error\\n'); sys.stdout.write(data.upper())")
    r = cctrack._run_streaming([sys.executable, "-c", script],
                               cctrack.Progress("dgx", enabled=True), timeout=10,
                               input=b"abc", text=False)
    assert r.stdout == b"ABC" and r.stderr == b"real error\n"   # marker lines not in stderr
    assert "parsing: 3/9 files" in err.getvalue()
