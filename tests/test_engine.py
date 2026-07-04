"""Integration tests for the Engine. These spawn real shells under a PTY.

Core behaviors are parametrized over whichever supported shells are installed
(bash/zsh/fish); shell- or program-specific cases skip when unavailable, so the
suite runs anywhere (CI included)."""

import re
import shutil
import subprocess
import threading
import time

import pytest

from cleat.engine import Engine, _MAX_RAW


def _fish_supported(path):
    """fish >= 4 emits OSC 133 natively; older fish is unsupported."""
    if not path:
        return False
    try:
        out = subprocess.run([path, "--version"], capture_output=True,
                             text=True, timeout=5).stdout
        m = re.search(r"version (\d+)", out)
        return bool(m) and int(m.group(1)) >= 4
    except Exception:
        return False


_SUPPORTED = [p for p in (shutil.which("bash"), shutil.which("zsh")) if p]
if _fish_supported(shutil.which("fish")):
    _SUPPORTED.append(shutil.which("fish"))


@pytest.fixture(params=_SUPPORTED, ids=lambda p: p.rsplit("/", 1)[-1])
def eng(request):
    e = Engine(shell=request.param).start()
    yield e
    e.close()


@pytest.fixture
def bash_eng():
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash not installed")
    e = Engine(shell=bash).start()
    yield e
    e.close()


# -- core behaviors, every supported shell ---------------------------------
def test_echo(eng):
    assert eng.run_command("echo hi") == {
        "stdout": "hi", "exit_code": 0, "completed": True, "state": "idle"}


def test_nonzero_exit(eng):
    r = eng.run_command("false")
    assert r["exit_code"] == 1 and r["completed"]


def test_persistence_cd(eng):
    eng.run_command("cd /tmp")
    r = eng.run_command("pwd")
    assert r["stdout"] in ("/tmp", "/private/tmp")  # macOS symlinks /tmp


def test_slow_command_completes(eng):
    r = eng.run_command("sleep 1; echo woke", timeout=5)
    assert r["completed"] and r["stdout"] == "woke"


def test_python_repl_interactive(eng):
    if eng.shell.rsplit("/", 1)[-1] == "fish":
        pytest.skip("interactive REPL driving verified on bash/zsh; "
                    "fish is supported for command execution")
    r = eng.run_command("python3", timeout=10)
    # REPL stays open (doesn't "complete"). The ">>>" prompt can lag the banner
    # on slow runners, so prove the REPL actually works by computing in it rather
    # than asserting on prompt-emit timing.
    assert not r["completed"]
    r = eng.send_keys("print(6*7)", enter=True)
    assert "42" in r["screen"]
    r = eng.send_keys("exit()", enter=True)
    assert r["completed"] and r["exit_code"] is not None


# -- polling after completion (issue #1) -----------------------------------
def test_read_output_after_completion_reports_done_promptly(eng):
    # Polling right after a command finished must NOT block the full timeout and
    # must NOT lie with completed=False: the shell is idle at a prompt, so report
    # completion immediately with the real last exit code. (issue #1)
    eng.run_command("echo hi")
    t0 = time.monotonic()
    r = eng.read_output(timeout=2.0)
    elapsed = time.monotonic() - t0
    assert r["completed"] is True
    assert r["exit_code"] == 0
    assert r["output"] == ""          # no NEW command output; prompt chrome != output
    assert elapsed < 1.0              # returned promptly, didn't burn the timeout


def test_read_output_after_completion_reports_failure_exit(eng):
    eng.run_command("false")
    r = eng.read_output(timeout=2.0)
    assert r["completed"] is True and r["exit_code"] == 1


def test_read_output_preserves_tail_of_command_finishing_between_polls(eng):
    # A long-runner whose output lands AFTER run_command's window must not be
    # lost: the idle short-circuit only fires when nothing is buffered, so the
    # tail + completion are still delivered by the following poll. (issue #1)
    r = eng.run_command("sleep 1; echo LATE", timeout=0.3)
    assert not r["completed"]                      # timed out mid-sleep
    r2 = eng.read_output(timeout=3.0)
    assert "LATE" in r2["output"]                  # tail preserved, not dropped
    assert r2["completed"] is True and r2["exit_code"] == 0


def test_read_screen_after_completion_returns_promptly(eng):
    eng.run_command("echo hi")
    t0 = time.monotonic()
    scr = eng.read_screen(timeout=2.0)
    elapsed = time.monotonic() - t0
    assert "hi" in scr["screen"]
    assert elapsed < 1.0              # idle + nothing pending => no settle wait


# -- specific cases (bash) -------------------------------------------------
def test_bash_subshell_exit_recovered(bash_eng):
    # First token '(' emits no C mark under bash-preexec; exit code still recovered.
    r = bash_eng.run_command("(exit 7)", timeout=4)
    assert r["exit_code"] == 7 and r["completed"]


def test_resize(bash_eng):
    bash_eng.resize(80, 24)
    assert bash_eng.run_command("tput cols")["stdout"] == "80"


def test_files_changed(bash_eng, tmp_path):
    bash_eng.set_watch_root(str(tmp_path))
    r = bash_eng.run_command(f"touch {tmp_path}/created.txt")
    assert r["files_changed"]["created"] == [str(tmp_path / "created.txt")]


def test_ctrl_c_interrupts(bash_eng):
    r = bash_eng.run_command("sleep 30", timeout=1.0)
    assert not r["completed"]
    r = bash_eng.send_keys("\x03")
    assert r["completed"] and r["exit_code"] is not None


def test_memory_bounded(bash_eng):
    for _ in range(8):
        bash_eng.run_command("head -c 200000 /dev/zero | tr '\\0' x", timeout=8)
    assert len(bash_eng._raw) <= 2 * _MAX_RAW and bash_eng._base > 0


def test_concurrent_calls_serialized(bash_eng):
    errors = []

    def worker(tag):
        for i in range(10):
            r = bash_eng.run_command(f"echo {tag}-{i}")
            if r["stdout"] != f"{tag}-{i}" or r["exit_code"] != 0:
                errors.append((tag, i, r))

    ts = [threading.Thread(target=worker, args=(t,)) for t in ("A", "B", "C")]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert errors == []


def test_tui_renders_and_quits(bash_eng):
    if not shutil.which("vim"):
        pytest.skip("vim not installed")
    bash_eng.run_command("vim -u NONE -N", timeout=5)
    scr = bash_eng.read_screen()
    assert "~" in scr["screen"]
    r = bash_eng.send_keys("\x1b:q!", enter=True)
    assert r["completed"]
