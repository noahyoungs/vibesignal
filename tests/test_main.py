import time
import os
import subprocess
import sys
from pathlib import Path

from vibesignal import __main__ as cli
from vibesignal import store

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _child_env(tmp_path):
    env = dict(os.environ)
    env["VIBECODING_SIGNAL_DIR"] = str(tmp_path)
    env["PYTHONPATH"] = str(PROJECT_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return env


def test_event_does_not_hang_on_open_empty_stdin(tmp_path):
    # Regression for the High finding: an open, dataless stdin pipe must not hang
    # the hook. With the bounded read it falls back and exits within the timeout.
    proc = subprocess.Popen(
        [sys.executable, "-m", "vibesignal", "event", "--agent", "claude", "--state", "working"],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=_child_env(tmp_path),
    )
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise AssertionError("event hung on an open empty stdin pipe")
    finally:
        if proc.stdin:
            proc.stdin.close()
    assert proc.returncode == 0


def test_event_reads_session_from_stdin(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-m", "vibesignal", "event", "--agent", "claude", "--state", "needs_input"],
        input='{"session_id":"xyz","cwd":"C:/p/proj"}',
        capture_output=True, text=True, timeout=10, env=_child_env(tmp_path),
    )
    assert proc.returncode == 0
    assert "claude/xyz" in proc.stdout


def test_event_quiet_suppresses_normal_stdout(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-m", "vibesignal", "event", "--agent", "claude",
         "--state", "working", "--quiet"],
        input='{"session_id":"xyz","cwd":"C:/p/proj"}',
        capture_output=True, text=True, timeout=10, env=_child_env(tmp_path),
    )
    assert proc.returncode == 0
    assert proc.stdout == ""


def test_apply_light_does_not_cache_on_failed_write(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBECODING_SIGNAL_DIR", str(tmp_path))
    monkeypatch.setattr(cli.light, "set_color", lambda rgb: False)  # simulate no device
    store.record("claude", "s1", "working")
    assert store.get_last_color() is None
    state, color = cli._apply_light()
    assert color == [0, 200, 60]            # working -> green
    assert store.get_last_color() is None   # not cached, because the write failed


def test_apply_light_caches_on_success(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBECODING_SIGNAL_DIR", str(tmp_path))
    monkeypatch.setattr(cli.light, "set_color", lambda rgb: True)  # simulate a device
    store.record("claude", "s1", "working")
    cli._apply_light()
    assert store.get_last_color() == [0, 200, 60]


def test_off_does_not_cache_on_failed_write(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBECODING_SIGNAL_DIR", str(tmp_path))
    store.set_last_color([255, 170, 0])                            # device believed amber
    monkeypatch.setattr(cli.light, "set_color", lambda rgb: False)  # no device
    cli.cmd_off(None)
    assert store.get_last_color() == [255, 170, 0]                 # unchanged: write failed


def test_event_accepts_blocked_done_and_alias(tmp_path):
    for state in ("blocked", "done", "needs_input"):
        proc = subprocess.run(
            [sys.executable, "-m", "vibesignal", "event", "--agent", "claude",
             "--state", state, "--session", "s"],
            input="{}", capture_output=True, text=True, timeout=10, env=_child_env(tmp_path),
        )
        assert proc.returncode == 0, f"state {state} rejected"


def test_event_quiet_suppresses_stdout(tmp_path):
    # Codex parses a hook's stdout as JSON, so --quiet must yield EMPTY stdout
    # while still recording state. Without it, the status line prints.
    env = _child_env(tmp_path)
    quiet = subprocess.run(
        [sys.executable, "-m", "vibesignal", "event", "--agent", "codex",
         "--state", "working", "--session", "s", "--quiet"],
        input="{}", capture_output=True, text=True, timeout=10, env=env,
    )
    assert quiet.returncode == 0
    assert quiet.stdout == ""
    verbose = subprocess.run(
        [sys.executable, "-m", "vibesignal", "event", "--agent", "codex",
         "--state", "working", "--session", "s2"],
        input="{}", capture_output=True, text=True, timeout=10, env=env,
    )
    assert "[vibesignal]" in verbose.stdout
    status = subprocess.run(
        [sys.executable, "-m", "vibesignal", "status"],
        capture_output=True, text=True, timeout=10, env=env,
    )
    assert "codex/s" in status.stdout  # state recorded despite the silence


def test_watch_once_renders_active_session(tmp_path):
    env = _child_env(tmp_path)
    subprocess.run(
        [sys.executable, "-m", "vibesignal", "event", "--agent", "claude",
         "--state", "working", "--session", "s2"],
        input='{"cwd":"C:/p/random"}', capture_output=True, text=True, timeout=10, env=env,
    )
    subprocess.run(
        [sys.executable, "-m", "vibesignal", "event", "--agent", "claude",
         "--state", "blocked", "--session", "s1"],
        input='{"cwd":"C:/p/aegis"}', capture_output=True, text=True, timeout=10, env=env,
    )
    proc = subprocess.run(
        [sys.executable, "-m", "vibesignal", "watch", "--once"],
        capture_output=True, text=True, timeout=10, env=env,
    )
    assert proc.returncode == 0
    assert "aegis" in proc.stdout
    # The blocked session sorts above the working session in the panel.
    assert proc.stdout.index("aegis") < proc.stdout.index("random")


def test_end_clears_only_the_ending_session(tmp_path):
    env = _child_env(tmp_path)
    for sid, state in (("s1", "blocked"), ("s2", "working")):
        subprocess.run(
            [sys.executable, "-m", "vibesignal", "event", "--agent", "claude",
             "--state", state, "--session", sid],
            input="{}", capture_output=True, text=True, timeout=10, env=env,
        )
    # SessionEnd for s1: the session id comes from the hook stdin, not an arg.
    proc = subprocess.run(
        [sys.executable, "-m", "vibesignal", "end", "--agent", "claude"],
        input='{"session_id":"s1"}', capture_output=True, text=True, timeout=10, env=env,
    )
    assert proc.returncode == 0
    status = subprocess.run(
        [sys.executable, "-m", "vibesignal", "status"],
        capture_output=True, text=True, timeout=10, env=env,
    )
    assert "claude/s2" in status.stdout      # the other session stays
    assert "claude/s1" not in status.stdout  # the ended one is cleared


def test_end_quiet_suppresses_normal_stdout(tmp_path):
    env = _child_env(tmp_path)
    subprocess.run(
        [sys.executable, "-m", "vibesignal", "event", "--agent", "claude",
         "--state", "blocked", "--session", "s1"],
        input="{}", capture_output=True, text=True, timeout=10, env=env,
    )
    proc = subprocess.run(
        [sys.executable, "-m", "vibesignal", "end", "--agent", "claude", "--quiet"],
        input='{"session_id":"s1"}', capture_output=True, text=True, timeout=10, env=env,
    )
    assert proc.returncode == 0
    assert proc.stdout == ""


def test_end_without_session_id_is_noop(tmp_path):
    # End with no --session and no session_id in hook stdin must NOT clear the
    # "default" bucket: it cannot know which session ended, so it no-ops.
    env = _child_env(tmp_path)
    subprocess.run(
        [sys.executable, "-m", "vibesignal", "event", "--agent", "claude",
         "--state", "blocked", "--session", "default"],
        input="{}", capture_output=True, text=True, timeout=10, env=env,
    )
    proc = subprocess.run(
        [sys.executable, "-m", "vibesignal", "end", "--agent", "claude"],
        input="{}", capture_output=True, text=True, timeout=10, env=env,
    )
    assert proc.returncode == 0
    status = subprocess.run(
        [sys.executable, "-m", "vibesignal", "status"],
        capture_output=True, text=True, timeout=10, env=env,
    )
    assert "claude/default" in status.stdout


def test_status_lists_blocked_older_than_store_ttl(tmp_path, monkeypatch):
    # Regression: `status` must agree with the light/panel. A blocked session older
    # than the store TTL stays listed (per-state lifetime), not hidden as inactive.
    import time
    monkeypatch.setenv("VIBECODING_SIGNAL_DIR", str(tmp_path))
    old = time.time() - (store.DEFAULT_TTL_SECONDS + 200)
    store.record("claude", "s1", "blocked", project="p", now=old)
    proc = subprocess.run(
        [sys.executable, "-m", "vibesignal", "status"],
        capture_output=True, text=True, timeout=10, env=_child_env(tmp_path),
    )
    assert proc.returncode == 0
    assert "claude/s1" in proc.stdout
    assert "(no active sessions)" not in proc.stdout


# ----- Platform-aware install messaging. The installer dispatches by platform
# (covered in test_installer.py); these guard that the CLI's *words* match the
# platform too, so a Windows user never sees "LaunchAgent" / "Spotlight". -----

def test_install_launcher_message_is_windows_on_win32(capsys, monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr(
        "vibesignal.installer.install_launcher",
        lambda: Path(r"C:\Start Menu\VibeSignal.lnk"),
    )
    assert cli.cmd_install_launcher(None) == 0
    out = capsys.readouterr().out
    assert "Start menu" in out
    assert "Spotlight" not in out and "Dock" not in out


def test_install_autostart_message_is_windows_on_win32(capsys, monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr(
        "vibesignal.installer.install_autostart",
        lambda launch_now=True: Path(r"C:\Startup\VibeSignal.lnk"),
    )
    assert cli.cmd_install_autostart(None) == 0
    out = capsys.readouterr().out
    assert "shortcut" in out
    assert "LaunchAgent" not in out


def test_install_autostart_message_is_macos_on_darwin(capsys, monkeypatch):
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr(
        "vibesignal.installer.install_autostart",
        lambda launch_now=True: Path("/Users/j/Library/LaunchAgents/io.github.yzhao062.vibesignal.plist"),
    )
    assert cli.cmd_install_autostart(None) == 0
    out = capsys.readouterr().out
    assert "LaunchAgent" in out


def test_version_matches_pyproject():
    # Guard the two version strings (pyproject [project].version and the package
    # __version__) against drift -- the exact release-correctness bug caught in
    # the 0.1.1 review.
    import tomllib

    import vibesignal

    with (PROJECT_ROOT / "pyproject.toml").open("rb") as fh:
        pyproject_version = tomllib.load(fh)["project"]["version"]
    assert vibesignal.__version__ == pyproject_version


def test_install_autostart_no_launch_passes_launch_now_false(capsys, monkeypatch):
    # `--no-launch` must thread through to installer.install_autostart(launch_now=False)
    # and the message must say "next login", not "starts now".
    import types
    seen = {}

    def fake_install(launch_now=True):
        seen["launch_now"] = launch_now
        return Path(r"C:\Startup\VibeSignal.lnk")

    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr("vibesignal.installer.install_autostart", fake_install)
    assert cli.cmd_install_autostart(types.SimpleNamespace(no_launch=True)) == 0
    assert seen["launch_now"] is False
    out = capsys.readouterr().out
    assert "next login" in out
    assert "starts now" not in out


def _event(state, ts=None):
    argv = ["event", "--agent", "opencode", "--session", "s1", "--state", state, "--quiet"]
    return cli.main(argv + (["--ts", str(ts)] if ts is not None else []))


def test_event_ts_drops_stale_event_finishing_last(tmp_path, monkeypatch):
    # OpenCode fires busy -> idle -> session.idle within ms, one process each;
    # the stale `working` can finish last. Its older --ts must not win.
    monkeypatch.setenv("VIBECODING_SIGNAL_DIR", str(tmp_path))
    monkeypatch.setattr(cli.light, "set_color", lambda rgb: False)
    monkeypatch.setattr(cli, "_read_hook_stdin", lambda: {})
    now = time.time()
    _event("done", ts=now + 0.002)      # idle processes finish first
    _event("done", ts=now + 0.002)
    _event("working", ts=now)           # busy process finishes last
    assert store.read_session("opencode", "s1")["state"] == "done"
    _event("working", ts=now + 5)       # a genuinely newer event still applies
    assert store.read_session("opencode", "s1")["state"] == "working"


def test_event_without_ts_always_records(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBECODING_SIGNAL_DIR", str(tmp_path))
    monkeypatch.setattr(cli.light, "set_color", lambda rgb: False)
    monkeypatch.setattr(cli, "_read_hook_stdin", lambda: {})
    _event("done", ts=time.time() + 60)
    _event("working")                   # hooks without --ts keep last-writer-wins
    assert store.read_session("opencode", "s1")["state"] == "working"


def test_event_stores_hook_transcript_path(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBECODING_SIGNAL_DIR", str(tmp_path))
    monkeypatch.setattr(cli.light, "set_color", lambda rgb: False)
    monkeypatch.setattr(cli, "_read_hook_stdin",
                        lambda: {"session_id": "s9", "transcript_path": "/x/s9.jsonl"})
    cli.main(["event", "--agent", "claude", "--state", "blocked", "--quiet"])
    assert store.read_session("claude", "s9")["transcript"] == "/x/s9.jsonl"
