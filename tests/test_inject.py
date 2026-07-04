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
