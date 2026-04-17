"""Tests for cctrack statusline hook infrastructure."""

import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import cctrack


# ── Hook script validity ──────────────────────────────────────────────

def test_hook_script_is_valid_python():
    """HOOK_SCRIPT constant must be syntactically valid Python."""
    compile(cctrack.HOOK_SCRIPT, "<hook.py>", "exec")


def test_hook_script_has_shebang():
    assert cctrack.HOOK_SCRIPT.startswith("#!/usr/bin/env python3")


# ── is_hook_installed tests ───────────────────────────────────────────

def test_is_hook_installed_false_no_config():
    """Returns False when no config.json exists."""
    with tempfile.TemporaryDirectory() as d:
        fake_cctrack = Path(d) / ".cctrack"
        fake_claude = Path(d) / ".claude"
        with patch.object(cctrack, "HOOK_CONFIG_PATH", fake_cctrack / "config.json"), \
             patch.object(cctrack, "CLAUDE_SETTINGS_PATH", fake_claude / "settings.json"):
            assert cctrack.is_hook_installed() is False


def test_is_hook_installed_false_no_settings():
    """Returns False when config exists but settings.json is missing."""
    with tempfile.TemporaryDirectory() as d:
        fake_cctrack = Path(d) / ".cctrack"
        fake_cctrack.mkdir()
        config_path = fake_cctrack / "config.json"
        config_path.write_text('{"version": "0.3.0"}')

        fake_claude = Path(d) / ".claude"
        with patch.object(cctrack, "HOOK_CONFIG_PATH", config_path), \
             patch.object(cctrack, "CLAUDE_SETTINGS_PATH", fake_claude / "settings.json"), \
             patch.object(cctrack, "HOOK_SCRIPT_PATH", fake_cctrack / "hook.py"):
            assert cctrack.is_hook_installed() is False


def test_is_hook_installed_false_wrong_command():
    """Returns False when settings.json has a statusLine but wrong command."""
    with tempfile.TemporaryDirectory() as d:
        fake_cctrack = Path(d) / ".cctrack"
        fake_cctrack.mkdir()
        config_path = fake_cctrack / "config.json"
        config_path.write_text('{"version": "0.3.0"}')

        fake_claude = Path(d) / ".claude"
        fake_claude.mkdir()
        settings_path = fake_claude / "settings.json"
        settings_path.write_text(json.dumps({
            "statusLine": {"type": "command", "command": "some-other-command"}
        }))

        with patch.object(cctrack, "HOOK_CONFIG_PATH", config_path), \
             patch.object(cctrack, "CLAUDE_SETTINGS_PATH", settings_path), \
             patch.object(cctrack, "HOOK_SCRIPT_PATH", fake_cctrack / "hook.py"):
            assert cctrack.is_hook_installed() is False


def test_is_hook_installed_true():
    """Returns True when properly installed."""
    with tempfile.TemporaryDirectory() as d:
        fake_cctrack = Path(d) / ".cctrack"
        fake_cctrack.mkdir()
        config_path = fake_cctrack / "config.json"
        config_path.write_text('{"version": "0.3.0"}')

        fake_claude = Path(d) / ".claude"
        fake_claude.mkdir()
        settings_path = fake_claude / "settings.json"
        settings_path.write_text(json.dumps({
            "statusLine": {"type": "command", "command": "python3 ~/.claude/hooks/cctrack-hook.py"}
        }))

        with patch.object(cctrack, "HOOK_CONFIG_PATH", config_path), \
             patch.object(cctrack, "CLAUDE_SETTINGS_PATH", settings_path):
            assert cctrack.is_hook_installed() is True


# ── read_hook_config tests ────────────────────────────────────────────

def test_read_hook_config_none_when_missing():
    with tempfile.TemporaryDirectory() as d:
        with patch.object(cctrack, "HOOK_CONFIG_PATH", Path(d) / "nope.json"):
            assert cctrack.read_hook_config() is None


def test_read_hook_config_returns_dict():
    with tempfile.TemporaryDirectory() as d:
        cfg = Path(d) / "config.json"
        cfg.write_text('{"version": "0.3.0", "installed_at": "2026-04-17T00:00:00+00:00"}')
        with patch.object(cctrack, "HOOK_CONFIG_PATH", cfg):
            result = cctrack.read_hook_config()
            assert result["version"] == "0.3.0"


def test_read_hook_config_none_on_bad_json():
    with tempfile.TemporaryDirectory() as d:
        cfg = Path(d) / "config.json"
        cfg.write_text("not json!")
        with patch.object(cctrack, "HOOK_CONFIG_PATH", cfg):
            assert cctrack.read_hook_config() is None


# ── install_hook tests ────────────────────────────────────────────────

def test_install_hook_creates_all_files(capsys):
    """install_hook creates hook script, config.json, and updates settings.json."""
    with tempfile.TemporaryDirectory() as d:
        fake_cctrack = Path(d) / ".claude" / "cctrack"
        fake_hooks = Path(d) / ".claude" / "hooks"
        fake_claude = Path(d) / ".claude"

        with patch.object(cctrack, "CCTRACK_DIR", fake_cctrack), \
             patch.object(cctrack, "HOOK_SCRIPT_PATH", fake_hooks / "cctrack-hook.py"), \
             patch.object(cctrack, "HOOK_CONFIG_PATH", fake_cctrack / "config.json"), \
             patch.object(cctrack, "CLAUDE_SETTINGS_PATH", fake_claude / "settings.json"):
            cctrack.install_hook()

        # hook script exists and is executable
        hook = fake_hooks / "cctrack-hook.py"
        assert hook.exists()
        assert hook.stat().st_mode & stat.S_IXUSR

        # config.json exists with expected keys
        config = json.loads((fake_cctrack / "config.json").read_text())
        assert config["version"] == "0.3.0"
        assert "installed_at" in config

        # settings.json exists with statusLine
        settings = json.loads((fake_claude / "settings.json").read_text())
        assert settings["statusLine"]["type"] == "command"
        assert "cctrack-hook.py" in settings["statusLine"]["command"]

        # Confirmation printed
        out = capsys.readouterr().out
        assert "installed successfully" in out


def test_install_hook_preserves_existing_settings(capsys):
    """install_hook preserves existing settings.json content."""
    with tempfile.TemporaryDirectory() as d:
        fake_cctrack = Path(d) / ".claude" / "cctrack"
        fake_hooks = Path(d) / ".claude" / "hooks"
        fake_claude = Path(d) / ".claude"
        fake_claude.mkdir(parents=True)

        # Pre-existing settings
        settings_path = fake_claude / "settings.json"
        settings_path.write_text(json.dumps({
            "theme": "dark",
            "permissions": {"allow": ["bash"]},
        }))

        with patch.object(cctrack, "CCTRACK_DIR", fake_cctrack), \
             patch.object(cctrack, "HOOK_SCRIPT_PATH", fake_hooks / "cctrack-hook.py"), \
             patch.object(cctrack, "HOOK_CONFIG_PATH", fake_cctrack / "config.json"), \
             patch.object(cctrack, "CLAUDE_SETTINGS_PATH", settings_path):
            cctrack.install_hook()

        settings = json.loads(settings_path.read_text())
        assert settings["theme"] == "dark"
        assert settings["permissions"] == {"allow": ["bash"]}
        assert "statusLine" in settings


def test_install_hook_overwrites_existing_statusline(capsys):
    """install_hook updates an existing statusLine entry."""
    with tempfile.TemporaryDirectory() as d:
        fake_cctrack = Path(d) / ".claude" / "cctrack"
        fake_hooks = Path(d) / ".claude" / "hooks"
        fake_claude = Path(d) / ".claude"
        fake_claude.mkdir(parents=True)

        settings_path = fake_claude / "settings.json"
        settings_path.write_text(json.dumps({
            "statusLine": {"type": "command", "command": "old-command"},
        }))

        with patch.object(cctrack, "CCTRACK_DIR", fake_cctrack), \
             patch.object(cctrack, "HOOK_SCRIPT_PATH", fake_hooks / "cctrack-hook.py"), \
             patch.object(cctrack, "HOOK_CONFIG_PATH", fake_cctrack / "config.json"), \
             patch.object(cctrack, "CLAUDE_SETTINGS_PATH", settings_path):
            cctrack.install_hook()

        settings = json.loads(settings_path.read_text())
        assert "cctrack-hook.py" in settings["statusLine"]["command"]
        assert "old-command" not in settings["statusLine"]["command"]


# ── uninstall_hook tests ──────────────────────────────────────────────

def test_uninstall_hook_removes_hook_keeps_data(capsys):
    """uninstall_hook removes hook script and config.json but keeps statusline.jsonl."""
    with tempfile.TemporaryDirectory() as d:
        fake_cctrack = Path(d) / ".claude" / "cctrack"
        fake_cctrack.mkdir(parents=True)
        fake_hooks = Path(d) / ".claude" / "hooks"
        fake_hooks.mkdir(parents=True)
        fake_claude = Path(d) / ".claude"

        hook_path = fake_hooks / "cctrack-hook.py"
        config_path = fake_cctrack / "config.json"
        data_file = fake_cctrack / "statusline-2026-04-17.jsonl"
        settings_path = fake_claude / "settings.json"

        # Simulate installed state
        hook_path.write_text("#!/usr/bin/env python3\n")
        config_path.write_text('{"version": "0.3.0"}')
        data_file.write_text('{"_ts": "2026-04-17T00:00:00+00:00"}\n')
        settings_path.write_text(json.dumps({
            "theme": "dark",
            "statusLine": {"type": "command", "command": f"python3 {hook_path}"},
        }))

        with patch.object(cctrack, "CCTRACK_DIR", fake_cctrack), \
             patch.object(cctrack, "HOOK_SCRIPT_PATH", hook_path), \
             patch.object(cctrack, "HOOK_CONFIG_PATH", config_path), \
             patch.object(cctrack, "CLAUDE_SETTINGS_PATH", settings_path):
            cctrack.uninstall_hook()

        # hook script and config.json removed
        assert not hook_path.exists()
        assert not config_path.exists()

        # Data preserved
        assert data_file.exists()

        # settings.json still has theme, no statusLine
        settings = json.loads(settings_path.read_text())
        assert settings["theme"] == "dark"
        assert "statusLine" not in settings

        out = capsys.readouterr().out
        assert "uninstalled" in out
        assert "preserved" in out


def test_uninstall_hook_handles_missing_files(capsys):
    """uninstall_hook doesn't crash when files are already missing."""
    with tempfile.TemporaryDirectory() as d:
        fake_cctrack = Path(d) / ".cctrack"
        fake_claude = Path(d) / ".claude"

        with patch.object(cctrack, "CCTRACK_DIR", fake_cctrack), \
             patch.object(cctrack, "HOOK_SCRIPT_PATH", fake_cctrack / "hook.py"), \
             patch.object(cctrack, "HOOK_CONFIG_PATH", fake_cctrack / "config.json"), \
             patch.object(cctrack, "CLAUDE_SETTINGS_PATH", fake_claude / "settings.json"):
            cctrack.uninstall_hook()


        out = capsys.readouterr().out
        assert "uninstalled" in out


# ── prompt_hook_install tests ─────────────────────────────────────────

def test_prompt_hook_install_non_tty_is_silent(capsys):
    """prompt_hook_install does nothing when stdin is not a TTY."""
    from unittest.mock import patch
    with patch("sys.stdin") as mock_stdin:
        mock_stdin.isatty.return_value = False
        cctrack.prompt_hook_install()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_prompt_hook_install_decline(capsys):
    """prompt_hook_install prints message and doesn't install when user declines."""
    from unittest.mock import patch
    with patch("sys.stdin") as mock_stdin, \
         patch("builtins.input", return_value="n"):
        mock_stdin.isatty.return_value = True
        cctrack.prompt_hook_install()
    captured = capsys.readouterr()
    assert "accurate cost tracking" in captured.err


def test_prompt_hook_install_accept(capsys):
    """prompt_hook_install installs when user says yes."""
    import tempfile
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as d:
        fake_cctrack = Path(d) / ".claude" / "cctrack"
        fake_hooks = Path(d) / ".claude" / "hooks"
        fake_claude = Path(d) / ".claude"
        with patch("sys.stdin") as mock_stdin, \
             patch("builtins.input", return_value="y"), \
             patch.object(cctrack, "CCTRACK_DIR", fake_cctrack), \
             patch.object(cctrack, "HOOK_SCRIPT_PATH", fake_hooks / "cctrack-hook.py"), \
             patch.object(cctrack, "HOOK_CONFIG_PATH", fake_cctrack / "config.json"), \
             patch.object(cctrack, "CLAUDE_SETTINGS_PATH", fake_claude / "settings.json"):
            mock_stdin.isatty.return_value = True
            cctrack.prompt_hook_install()
        assert (fake_hooks / "cctrack-hook.py").exists()
        assert (fake_cctrack / "config.json").exists()


# ── Hook script integration tests ────────────────────────────────────

def _run_hook_script(stdin_data: str, home_dir: str) -> subprocess.CompletedProcess:
    """Run the hook script as a subprocess with custom HOME."""
    hook_path = os.path.join(home_dir, "hook.py")
    with open(hook_path, "w") as f:
        f.write(cctrack.HOOK_SCRIPT)
    os.chmod(hook_path, 0o755)

    env = os.environ.copy()
    env["HOME"] = home_dir

    return subprocess.run(
        [sys.executable, hook_path],
        input=stdin_data,
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )


def _find_hook_data_file(home_dir: str) -> Path | None:
    """Find the dated statusline JSONL file written by the hook."""
    cctrack_dir = Path(home_dir) / ".claude" / "cctrack"
    files = list(cctrack_dir.glob("statusline-*.jsonl"))
    return files[0] if files else None


def test_hook_script_processes_valid_input():
    """Hook script correctly processes valid statusline JSON."""
    with tempfile.TemporaryDirectory() as d:
        input_data = json.dumps({
            "cost": {"total_cost_usd": 1.23},
            "session_id": "sess_abc",
            "model": {"id": "claude-opus-4-6"},
            "context_window": {
                "total_input_tokens": 50000,
                "total_output_tokens": 10000,
                "current_usage": {
                    "cache_read_input_tokens": 300,
                    "cache_creation_input_tokens": 200,
                },
            },
        })

        result = _run_hook_script(input_data, d)
        assert result.returncode == 0

        data_file = _find_hook_data_file(d)
        assert data_file is not None
        assert data_file.name.startswith("statusline-")

        line = data_file.read_text().strip()
        record = json.loads(line)
        assert record["session_id"] == "sess_abc"
        assert record["cost_usd"] == 1.23
        assert record["model"] == "claude-opus-4-6"
        assert record["input_tokens"] == 50000
        assert record["output_tokens"] == 10000
        assert record["cache_read"] == 300
        assert record["cache_write"] == 200
        assert "_ts" in record


def test_hook_script_handles_malformed_input():
    """Hook script does not crash on malformed input."""
    with tempfile.TemporaryDirectory() as d:
        result = _run_hook_script("not json at all {{{", d)
        assert result.returncode == 0
        assert result.stderr == ""


def test_hook_script_handles_empty_input():
    """Hook script does not crash on empty stdin."""
    with tempfile.TemporaryDirectory() as d:
        result = _run_hook_script("", d)
        assert result.returncode == 0
        assert result.stderr == ""


def test_hook_script_handles_partial_data():
    """Hook script handles JSON missing expected fields."""
    with tempfile.TemporaryDirectory() as d:
        input_data = json.dumps({"session_id": "sess_123"})
        result = _run_hook_script(input_data, d)
        assert result.returncode == 0

        data_file = _find_hook_data_file(d)
        assert data_file is not None
        record = json.loads(data_file.read_text().strip())
        assert record["session_id"] == "sess_123"
        assert record["cost_usd"] == 0.0
        assert record["model"] == ""
        assert record["input_tokens"] == 0
        assert record["output_tokens"] == 0


def test_hook_script_creates_cctrack_dir():
    """Hook script creates ~/.claude/cctrack directory if missing."""
    with tempfile.TemporaryDirectory() as d:
        cctrack_dir = Path(d) / ".claude" / "cctrack"
        assert not cctrack_dir.exists()

        input_data = json.dumps({
            "cost": {"total_cost_usd": 0.5},
            "session_id": "sess_new",
            "model": {"id": "claude-sonnet-4"},
            "context_window": {
                "total_input_tokens": 1000,
                "total_output_tokens": 500,
                "current_usage": {},
            },
        })

        result = _run_hook_script(input_data, d)
        assert result.returncode == 0
        assert cctrack_dir.exists()
        assert _find_hook_data_file(d) is not None


def test_hook_script_appends_multiple_records():
    """Hook script appends to same daily file (doesn't overwrite)."""
    with tempfile.TemporaryDirectory() as d:
        for i in range(3):
            input_data = json.dumps({
                "cost": {"total_cost_usd": float(i)},
                "session_id": f"sess_{i}",
                "model": {"id": "claude-sonnet-4"},
                "context_window": {
                    "total_input_tokens": 100 * (i + 1),
                    "total_output_tokens": 50 * (i + 1),
                    "current_usage": {},
                },
            })
            result = _run_hook_script(input_data, d)
            assert result.returncode == 0

        data_file = _find_hook_data_file(d)
        assert data_file is not None
        lines = [l for l in data_file.read_text().strip().split("\n") if l]
        assert len(lines) == 3
        records = [json.loads(l) for l in lines]
        assert records[0]["session_id"] == "sess_0"
        assert records[1]["session_id"] == "sess_1"
        assert records[2]["session_id"] == "sess_2"


# ── CLI argument tests ────────────────────────────────────────────────

def test_main_install_hook_flag(capsys):
    """--install-hook flag triggers install_hook and exits."""
    with tempfile.TemporaryDirectory() as d:
        fake_cctrack = Path(d) / ".claude" / "cctrack"
        fake_hooks = Path(d) / ".claude" / "hooks"
        fake_claude = Path(d) / ".claude"

        with patch.object(cctrack, "CCTRACK_DIR", fake_cctrack), \
             patch.object(cctrack, "HOOK_SCRIPT_PATH", fake_hooks / "cctrack-hook.py"), \
             patch.object(cctrack, "HOOK_CONFIG_PATH", fake_cctrack / "config.json"), \
             patch.object(cctrack, "CLAUDE_SETTINGS_PATH", fake_claude / "settings.json"), \
             patch("sys.argv", ["cctrack", "--install-hook"]):
            cctrack.main()

        assert (fake_hooks / "cctrack-hook.py").exists()
        assert (fake_cctrack / "config.json").exists()
        out = capsys.readouterr().out
        assert "installed successfully" in out


def test_main_uninstall_hook_flag(capsys):
    """--uninstall-hook flag triggers uninstall_hook and exits."""
    with tempfile.TemporaryDirectory() as d:
        fake_cctrack = Path(d) / ".claude" / "cctrack"
        fake_cctrack.mkdir(parents=True)
        fake_hooks = Path(d) / ".claude" / "hooks"
        fake_hooks.mkdir(parents=True)
        fake_claude = Path(d) / ".claude"

        hook_path = fake_hooks / "cctrack-hook.py"
        config_path = fake_cctrack / "config.json"
        settings_path = fake_claude / "settings.json"
        hook_path.write_text("# hook")
        config_path.write_text('{"version": "0.3.0"}')
        settings_path.write_text(json.dumps({"statusLine": {"type": "command", "command": "x"}}))

        with patch.object(cctrack, "CCTRACK_DIR", fake_cctrack), \
             patch.object(cctrack, "HOOK_SCRIPT_PATH", hook_path), \
             patch.object(cctrack, "HOOK_CONFIG_PATH", config_path), \
             patch.object(cctrack, "CLAUDE_SETTINGS_PATH", settings_path), \
             patch("sys.argv", ["cctrack", "--uninstall-hook"]):
            cctrack.main()

        assert not hook_path.exists()
        assert not config_path.exists()
        out = capsys.readouterr().out
        assert "uninstalled" in out


# ── remote_install_hook tests ────────────────────────────────────────

def test_remote_install_hook_success(capsys):
    """remote_install_hook prints success on clean SSH run."""
    fake_result = subprocess.CompletedProcess(
        args=[], returncode=0,
        stdout="Hook script: /home/user/.claude/hooks/cctrack-hook.py\nData dir:    /home/user/.claude/cctrack\nSettings:    /home/user/.claude/settings.json\n",
        stderr="",
    )
    with patch("subprocess.run", return_value=fake_result) as mock_run:
        cctrack.remote_install_hook("dgx")

    out = capsys.readouterr().out
    assert "installed on dgx" in out
    assert "Hook script:" in out

    call_args = mock_run.call_args
    assert "ssh" in call_args[0][0][0]
    assert "dgx" in call_args[0][0]
    assert "python3" in call_args[0][0]
    assert "base64" in call_args[1]["input"]


def test_remote_install_hook_ssh_failure(capsys):
    """remote_install_hook reports failure on SSH error."""
    fake_result = subprocess.CompletedProcess(
        args=[], returncode=255,
        stdout="", stderr="Connection refused",
    )
    with patch("subprocess.run", return_value=fake_result):
        cctrack.remote_install_hook("bad-host")

    err = capsys.readouterr().err
    assert "failed" in err
    assert "Connection refused" in err


def test_remote_install_hook_timeout(capsys):
    """remote_install_hook handles SSH timeout."""
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("ssh", 30)):
        cctrack.remote_install_hook("slow-host")

    err = capsys.readouterr().err
    assert "timed out" in err


def test_remote_install_hook_no_ssh(capsys):
    """remote_install_hook handles missing ssh binary."""
    with patch("subprocess.run", side_effect=FileNotFoundError):
        cctrack.remote_install_hook("any-host")

    err = capsys.readouterr().err
    assert "ssh command not found" in err


def test_remote_install_hook_installer_contains_hook_script():
    """The installer script sent over SSH contains the HOOK_SCRIPT via base64."""
    import base64
    captured_input = None

    def capture_run(cmd, **kwargs):
        nonlocal captured_input
        captured_input = kwargs.get("input", "")
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="Hook script: x\n", stderr="")

    with patch("subprocess.run", side_effect=capture_run):
        cctrack.remote_install_hook("test-host")

    assert captured_input is not None
    encoded = base64.b64encode(cctrack.HOOK_SCRIPT.encode()).decode()
    assert encoded in captured_input


# ── remote_uninstall_hook tests ──────────────────────────────────────

def test_remote_uninstall_hook_success(capsys):
    """remote_uninstall_hook prints success on clean SSH run."""
    fake_result = subprocess.CompletedProcess(
        args=[], returncode=0,
        stdout="uninstalled\nData preserved: /home/user/.claude/cctrack/statusline.jsonl\n",
        stderr="",
    )
    with patch("subprocess.run", return_value=fake_result):
        cctrack.remote_uninstall_hook("dgx")

    out = capsys.readouterr().out
    assert "uninstalled on dgx" in out
    assert "Data preserved:" in out


def test_remote_uninstall_hook_ssh_failure(capsys):
    """remote_uninstall_hook reports failure on SSH error."""
    fake_result = subprocess.CompletedProcess(
        args=[], returncode=255,
        stdout="", stderr="Connection refused",
    )
    with patch("subprocess.run", return_value=fake_result):
        cctrack.remote_uninstall_hook("bad-host")

    err = capsys.readouterr().err
    assert "failed" in err


def test_remote_uninstall_hook_timeout(capsys):
    """remote_uninstall_hook handles SSH timeout."""
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("ssh", 30)):
        cctrack.remote_uninstall_hook("slow-host")

    err = capsys.readouterr().err
    assert "timed out" in err


# ── CLI remote hook flags ────────────────────────────────────────────

def test_main_install_hook_remote(capsys):
    """--install-hook --remote dgx calls remote_install_hook."""
    with patch.object(cctrack, "remote_install_hook") as mock_remote:
        with patch("sys.argv", ["cctrack", "--install-hook", "--remote", "dgx"]):
            cctrack.main()
    mock_remote.assert_called_once_with("dgx")


def test_main_install_hook_remote_multiple(capsys):
    """--install-hook --remote host1 host2 calls remote_install_hook for each."""
    with patch.object(cctrack, "remote_install_hook") as mock_remote:
        with patch("sys.argv", ["cctrack", "--install-hook", "--remote", "dgx", "macbook"]):
            cctrack.main()
    assert mock_remote.call_count == 2
    mock_remote.assert_any_call("dgx")
    mock_remote.assert_any_call("macbook")


def test_main_uninstall_hook_remote(capsys):
    """--uninstall-hook --remote dgx calls remote_uninstall_hook."""
    with patch.object(cctrack, "remote_uninstall_hook") as mock_remote:
        with patch("sys.argv", ["cctrack", "--uninstall-hook", "--remote", "dgx"]):
            cctrack.main()
    mock_remote.assert_called_once_with("dgx")
