#!/usr/bin/env python3
"""
inject.py - OSC 133 shell-integration injection (an engine concern).

We want the shells we spawn to emit FinalTerm/OSC 133 marks WITHOUT touching the
user's real config. Each shell has its own injection seam:

    zsh  -> $ZDOTDIR points at a temp dir whose .zshrc re-sources the user's
            config then installs the marks.
    bash -> --rcfile <tempfile> that sources ~/.bashrc then installs the marks.
    fish -> `fish -C 'source <tempdir>/marks.fish'` installs marks via an
            on-event function, in ADDITION to fish's own native OSC 133 (fish
            >= 4). We deliberately do NOT override $XDG_CONFIG_HOME - children
            spawned in the session inherit env, and clobbering it would break
            git/tools that read fish's config location. fish's native marks
            aren't nonced, so structure.py's nonce filtering ignores them
            instead of double-counting. fish < 4 has no native integration and
            is unsupported for TUI/prompt detection either way.

prepare(shell, env) returns (argv, env, cleanup_dir, nonce): the argv to spawn,
the env to spawn it with, a temp dir to rmtree on exit (or None), and the
per-session nonce embedded in every injected mark (or None if nothing was
injected). Shared by microterm.py (interactive demonstrator) and engine.py.
No third-party deps.

Parser contract (see structure.py): only C (output-begins) and D;<exit> matter.
A (prompt-start) is emitted where easy; B (prompt-end) is skipped. Every C/D/A
mark carries a `;k=<nonce>` param so structure.py can authenticate it - see
"Nonce-authenticated marks" in structure.py's docstring.
"""

import os
import secrets
import tempfile


OSC133_ZSHRC = r'''# --- headless terminal layer: injected zsh rcfile ---
ZDOTDIR="${_HEADLESS_REAL_ZDOTDIR:-$HOME}"
[ -f "$ZDOTDIR/.zshenv" ] && source "$ZDOTDIR/.zshenv"
[ -f "$ZDOTDIR/.zshrc" ]  && source "$ZDOTDIR/.zshrc"

autoload -Uz add-zsh-hook
_h133_preexec() { printf '\033]133;C;k=@NONCE@\007' }
_h133_precmd()  { printf '\033]133;D;%s;k=@NONCE@\007\033]133;A;k=@NONCE@\007' "$?" }
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
  __h133_preexec() { printf '\033]133;C;k=@NONCE@\007'; }
  # D;<exit> + A: capture $? FIRST, then emit prev exit code + next-prompt mark.
  __h133_precmd() {
    local ec=$?
    printf '\033]133;D;%s;k=@NONCE@\007\033]133;A;k=@NONCE@\007' "$ec"
  }
  preexec_functions+=(__h133_preexec)
  precmd_functions+=(__h133_precmd)
fi
# NOTE: bash-preexec does NOT fire preexec when the command's first token is a
# subshell (...) or brace group { ...; } - such a command emits no C mark. The
# parser recovers its exit code as a zero-output record (see structure.py).
'''


# Installed via `fish -C 'source <path>'`, NOT $XDG_CONFIG_HOME (see module
# docstring). fish >= 4 also emits its own native, un-nonced OSC 133 marks;
# structure.py's nonce filtering ignores those rather than double-counting.
OSC133_FISH = r'''# --- headless terminal layer: injected fish marks ---
function __h133_pre --on-event fish_preexec
    printf '\033]133;C;k=@NONCE@\007'
end
function __h133_post --on-event fish_postexec
    printf '\033]133;D;%s;k=@NONCE@\007\033]133;A;k=@NONCE@\007' $status
end
'''


# Terminal-identity env vars that ambient third-party shell integration
# (iTerm2, VS Code, Kitty, WezTerm) gates its own self-install on - they emit
# their OWN un-nonced OSC 133 marks when they think they're running inside
# their real host terminal. Since these vars are inherited from whatever
# process spawned the cleat/MCP-server process itself (its own outer
# terminal, if any), and the injected rcfile re-sources the user's REAL
# .zshrc/.bashrc, those integrations happily self-install here too even
# though this synthetic PTY session isn't actually their host terminal -
# causing legitimate ambient marks to be misread as forged ones and
# incrementing spoofed_marks on essentially every command (issue #20).
# Stripping these before spawning gives them an accurate signal instead.
_AMBIENT_INTEGRATION_ENV_VARS = (
    "TERM_PROGRAM", "TERM_PROGRAM_VERSION",   # generic; also gates VS Code/iTerm2
    "ITERM_SESSION_ID", "ITERM_PROFILE",       # iTerm2
    "VSCODE_INJECTION", "VSCODE_PID",          # VS Code
    "KITTY_WINDOW_ID", "KITTY_PID",            # Kitty
    "WEZTERM_EXECUTABLE", "WEZTERM_PANE",      # WezTerm
)


def _write(dirpath, name, content):
    path = os.path.join(dirpath, name)
    with open(path, "w") as f:
        f.write(content)
    return path


def prepare(shell, base_env):
    """Return (argv, env, cleanup_dir, nonce) to spawn `shell` with nonce-
    authenticated OSC 133 injected. nonce is a fresh secrets.token_hex(8) per
    call, or None for an unknown shell (nothing was injected)."""
    base = os.path.basename(shell)
    env = dict(base_env)
    if base in ("zsh", "bash", "fish"):
        # Only the shells whose rcfile we actually re-source the user's own
        # config in (and only when we're doing nonce authentication at all) -
        # see _AMBIENT_INTEGRATION_ENV_VARS above.
        for var in _AMBIENT_INTEGRATION_ENV_VARS:
            env.pop(var, None)

    if base == "zsh":
        nonce = secrets.token_hex(8)
        d = tempfile.mkdtemp(prefix="headless-inj-")
        _write(d, ".zshrc", OSC133_ZSHRC.replace("@NONCE@", nonce))
        env["_HEADLESS_REAL_ZDOTDIR"] = env.get("ZDOTDIR", env.get("HOME", ""))
        env["ZDOTDIR"] = d
        return [shell], env, d, nonce

    if base == "bash":
        nonce = secrets.token_hex(8)
        d = tempfile.mkdtemp(prefix="headless-inj-")
        # bash-preexec.sh is vendored next to this module so its absolute path
        # survives even though the rcfile is written into a throwaway temp dir.
        bp = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "vendor", "bash-preexec.sh")
        rc = _write(d, "bashrc", OSC133_BASHRC
                    .replace("@BASH_PREEXEC_PATH@", bp)
                    .replace("@NONCE@", nonce))
        return [shell, "--rcfile", rc], env, d, nonce

    if base == "fish":
        # fish >= 4 emits its own native, un-nonced OSC 133 marks too - the
        # nonce filter in structure.py ignores those instead of double-
        # counting them. We inject via `-C`, not $XDG_CONFIG_HOME (see module
        # docstring), so children spawned in the session see a normal env.
        nonce = secrets.token_hex(8)
        d = tempfile.mkdtemp(prefix="headless-inj-")
        marks_path = _write(d, "marks.fish", OSC133_FISH.replace("@NONCE@", nonce))
        return [shell, "-C", f"source '{marks_path}'"], env, d, nonce

    # Unknown shell: spawn as-is, no marks (structure source will see nothing).
    return [shell], env, None, None
