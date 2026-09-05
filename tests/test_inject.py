"""Unit tests for OSC 133 injection template rendering - pure, no PTY spawn."""

import os
import shutil

import pytest

from cleat.inject import prepare


def _cleanup(cleanup_dir):
    if cleanup_dir:
        shutil.rmtree(cleanup_dir, ignore_errors=True)


@pytest.mark.parametrize("shell,rcfile", [
    ("/bin/zsh", ".zshrc"),
    ("/bin/bash", "bashrc"),
    ("/usr/bin/fish", "marks.fish"),
])
def test_nonce_substituted_into_rcfile(shell, rcfile):
    argv, env, cleanup_dir, nonce = prepare(shell, {"HOME": "/home/x"})
    try:
        assert nonce is not None
        assert len(nonce) == 16  # secrets.token_hex(8) -> 16 hex chars
        assert all(c in "0123456789abcdef" for c in nonce)

        content = open(os.path.join(cleanup_dir, rcfile)).read()
        assert f"k=@NONCE@" not in content       # placeholder fully substituted
        assert f";k={nonce}" in content           # every mark carries the nonce
        assert content.count(f"k={nonce}") == 3   # C, D, and A marks
    finally:
        _cleanup(cleanup_dir)


def test_fresh_nonce_per_call():
    _, _, d1, nonce1 = prepare("/bin/zsh", {"HOME": "/home/x"})
    _, _, d2, nonce2 = prepare("/bin/zsh", {"HOME": "/home/x"})
    try:
        assert nonce1 != nonce2
    finally:
        _cleanup(d1)
        _cleanup(d2)


def test_fish_uses_dash_c_not_xdg_config_home():
    # Injection must not touch $XDG_CONFIG_HOME - children in the session
    # inherit env, and clobbering it would corrupt fish's config location for
    # anything the session shells out to.
    argv, env, cleanup_dir, nonce = prepare("/usr/bin/fish", {"HOME": "/home/x"})
    try:
        assert "XDG_CONFIG_HOME" not in env
        assert argv[0] == "/usr/bin/fish"
        assert "-C" in argv
        marks_path = os.path.join(cleanup_dir, "marks.fish")
        assert f"source '{marks_path}'" in argv
    finally:
        _cleanup(cleanup_dir)


def test_unknown_shell_no_injection():
    argv, env, cleanup_dir, nonce = prepare("/bin/tcsh", {"HOME": "/home/x"})
    assert argv == ["/bin/tcsh"]
    assert cleanup_dir is None
    assert nonce is None


def test_zsh_zdotdir_redirected_to_tempdir():
    argv, env, cleanup_dir, nonce = prepare("/bin/zsh", {"HOME": "/home/x"})
    try:
        assert env["ZDOTDIR"] == cleanup_dir
        assert env["_HEADLESS_REAL_ZDOTDIR"] == "/home/x"
    finally:
        _cleanup(cleanup_dir)


def test_bash_rcfile_references_vendored_preexec_by_absolute_path():
    argv, env, cleanup_dir, nonce = prepare("/bin/bash", {"HOME": "/home/x"})
    try:
        rc_path = argv[argv.index("--rcfile") + 1]
        content = open(rc_path).read()
        assert "@BASH_PREEXEC_PATH@" not in content
        assert os.path.isabs(content.split("if [ -r '")[1].split("'")[0])
    finally:
        _cleanup(cleanup_dir)


# -- ambient shell-integration false positives (issue #20) -------------------
# The injected rcfile re-sources the user's OWN .zshrc/.bashrc, which may
# itself install ambient, un-nonced OSC-133-alike shell integration (iTerm2,
# VS Code, Kitty, WezTerm) - those tools gate their self-install on terminal-
# identity env vars, inherited here from whatever real terminal launched the
# cleat/MCP-server process itself, even though this synthetic PTY session
# isn't actually that terminal. Stripping those vars lets such integrations
# correctly detect they're not running inside their real host and skip
# self-installing, instead of firing marks cleat then has to (mis)classify.
_AMBIENT_ENV = {
    "TERM_PROGRAM": "iTerm.app",
    "TERM_PROGRAM_VERSION": "3.5.0",
    "ITERM_SESSION_ID": "w0t0p0:ABC",
    "ITERM_PROFILE": "Default",
    "VSCODE_INJECTION": "1",
    "VSCODE_PID": "1234",
    "KITTY_WINDOW_ID": "1",
    "KITTY_PID": "5678",
    "WEZTERM_EXECUTABLE": "/usr/bin/wezterm",
    "WEZTERM_PANE": "0",
}


@pytest.mark.parametrize("shell", ["/bin/zsh", "/bin/bash", "/usr/bin/fish"])
def test_ambient_terminal_identity_env_vars_stripped(shell):
    base_env = {"HOME": "/home/x", "SOME_OTHER_VAR": "keep-me", **_AMBIENT_ENV}
    argv, env, cleanup_dir, nonce = prepare(shell, base_env)
    try:
        for var in _AMBIENT_ENV:
            assert var not in env, f"{var} should have been stripped"
        assert env["SOME_OTHER_VAR"] == "keep-me"
        assert env["HOME"] == "/home/x"
    finally:
        _cleanup(cleanup_dir)
