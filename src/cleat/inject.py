#!/usr/bin/env python3
"""
inject.py - OSC 133 shell-integration injection (an engine concern).

We want the shells we spawn to emit FinalTerm/OSC 133 marks WITHOUT touching the
user's real config. Each shell has its own injection seam:

    zsh  -> $ZDOTDIR points at a temp dir whose .zshrc re-sources the user's
            config then installs the marks.
    bash -> --rcfile <tempfile> that sources ~/.bashrc then installs the marks.
    fish -> -C "source <tempfile>"; fish still auto-loads the user's config, so
            we only append the marks.

prepare(shell, env) returns (argv, env, cleanup_dir): the argv to spawn, the env
to spawn it with, and a temp dir to rmtree on exit (or None). Shared by
microterm.py (interactive demonstrator) and engine.py. No third-party deps.

Parser contract (see structure.py): only C (output-begins) and D;<exit> matter.
A (prompt-start) is emitted where easy; B (prompt-end) is skipped.
"""

import os
import re
import tempfile
import subprocess


OSC133_ZSHRC = r'''# --- headless terminal layer: injected zsh rcfile ---
ZDOTDIR="${_HEADLESS_REAL_ZDOTDIR:-$HOME}"
[ -f "$ZDOTDIR/.zshenv" ] && source "$ZDOTDIR/.zshenv"
[ -f "$ZDOTDIR/.zshrc" ]  && source "$ZDOTDIR/.zshrc"

autoload -Uz add-zsh-hook
_h133_preexec() { printf '\033]133;C\007' }
_h133_precmd()  { printf '\033]133;D;%s\007\033]133;A\007' "$?" }
add-zsh-hook preexec _h133_preexec
add-zsh-hook precmd  _h133_precmd

# Keep the C->D region clean: drop zsh's partial-line indicator.
unsetopt PROMPT_SP 2>/dev/null
PROMPT_EOL_MARK=''
'''


# @BASH_PREEXEC_PATH@ is filled in by prepare() with the ABSOLUTE path to the
# vendored bash-preexec.sh (it ships next to this module). The rcfile itself
# lives in a throwaway temp dir, so it must reference the vendored file by an
# absolute path. bash-preexec gives reliable preexec/precmd hook arrays that fire
# once per interactive command and - unlike a hand-rolled DEBUG-trap armed flag -
# is not fooled by a PS1 that runs command substitution (the bash 4.x/5.x bug).
OSC133_BASHRC = r'''# --- headless terminal layer: injected bash rcfile (bash-preexec) ---
[ -f "$HOME/.bashrc" ] && source "$HOME/.bashrc"

# Source vendored bash-preexec LAST (it preserves any PROMPT_COMMAND ~/.bashrc set).
if [ -r '@BASH_PREEXEC_PATH@' ]; then
  source '@BASH_PREEXEC_PATH@'

  # C: right before each command runs.
  __h133_preexec() { printf '\033]133;C\007'; }
  # D;<exit> + A: capture $? FIRST, then emit prev exit code + next-prompt mark.
  __h133_precmd() {
    local ec=$?
    printf '\033]133;D;%s\007\033]133;A\007' "$ec"
  }
  preexec_functions+=(__h133_preexec)
  precmd_functions+=(__h133_precmd)
fi
# NOTE: bash-preexec does NOT fire preexec when the command's first token is a
# subshell (...) or brace group { ...; } - such a command emits no C mark. The
# parser recovers its exit code as a zero-output record (see structure.py).
'''


OSC133_FISH = r'''# --- headless terminal layer: injected fish init (OSC 133) ---
function __h133_c --on-event fish_preexec
    printf '\033]133;C\007'
end
function __h133_d --on-event fish_postexec
    set -l ec $status
    printf '\033]133;D;%d\007\033]133;A\007' $ec
end
'''


def _write(dirpath, name, content):
    path = os.path.join(dirpath, name)
    with open(path, "w") as f:
        f.write(content)
    return path


def _fish_has_native_osc133(shell):
    """fish >= 4 emits OSC 133 shell-integration marks on its own."""
    try:
        out = subprocess.run([shell, "--version"], capture_output=True,
                             text=True, timeout=5).stdout
        m = re.search(r"version (\d+)", out)   # "fish, version 4.7.1"
        return bool(m) and int(m.group(1)) >= 4
    except Exception:
        return False


def prepare(shell, base_env):
    """Return (argv, env, cleanup_dir) to spawn `shell` with OSC 133 injected."""
    base = os.path.basename(shell)
    env = dict(base_env)

    if base == "zsh":
        d = tempfile.mkdtemp(prefix="headless-inj-")
        _write(d, ".zshrc", OSC133_ZSHRC)
        env["_HEADLESS_REAL_ZDOTDIR"] = env.get("ZDOTDIR", env.get("HOME", ""))
        env["ZDOTDIR"] = d
        return [shell], env, d

    if base == "bash":
        d = tempfile.mkdtemp(prefix="headless-inj-")
        # bash-preexec.sh is vendored next to this module so its absolute path
        # survives even though the rcfile is written into a throwaway temp dir.
        bp = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "vendor", "bash-preexec.sh")
        rc = _write(d, "bashrc", OSC133_BASHRC.replace("@BASH_PREEXEC_PATH@", bp))
        return [shell, "--rcfile", rc], env, d

    if base == "fish":
        # fish >= 4 emits OSC 133 natively; injecting ours too would DOUBLE every
        # mark (two C's, two D's per command) and make command correlation racy.
        # So only inject for older fish that lacks native shell integration.
        if _fish_has_native_osc133(shell):
            return [shell], env, None
        d = tempfile.mkdtemp(prefix="headless-inj-")
        init = _write(d, "init.fish", OSC133_FISH)
        return [shell, "-C", f"source {init}"], env, d

    # Unknown shell: spawn as-is, no marks (structure source will see nothing).
    return [shell], env, None
