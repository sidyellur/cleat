# Blog notes

<!-- Append short entries below as you work: key decisions and why, not a full transcript.
     expert-blog-writer reads this file (plus README.md and git log) to draft a blog post
     once you confirm this project is "blog-worthy done." -->

- Went hunting for a small robustness gap and found one in the OSC 133 parser: the
  exit-code check used `lstrip("-").isdigit()`, so a malformed mark like `D;--5`
  passed the check but blew up `int()` with a ValueError. The kicker is *where* it
  raises: `feed()` runs on the engine's read-loop thread with no exception guard
  (unlike the neighboring `pyte.feed`), so any program printing bytes that merely
  *resemble* a broken D mark could kill the read loop and wedge the session. A
  parser fed untrusted terminal byte soup has to treat every field as hostile.
  Fixed with `re.fullmatch(r"-?\d+", ...)` so garbage degrades to
  `exit_code=None` instead of raising; kept negative codes and added tests.

## v0.2: session-state oracle + nonce-authenticated marks (issue #5)

- The nonce check in `structure.py._mark()` runs *before* any state mutation
  for C/D/A marks, and returns `None` immediately on a miss. That ordering is
  what makes the security property clean: a forged mark can't partially apply
  (e.g. bump `commands_started` before being rejected) — it's as if the byte
  never arrived at all, just silently counted in `spoofed_marks`.

- `_probe_state()`'s derivation order is genuinely load-bearing, not just a
  style choice: I verified empirically (spawning a raw PTY and reading
  termios) that bash's readline puts the terminal in raw mode — ECHO off,
  ICANON off — even at a *plain prompt*, not just mid-REPL. If the "marks
  idle" check didn't run before the termios check, every idle prompt would
  misreport as `awaiting-input`. The plan's ORDER IS LOAD-BEARING comment
  earned its all-caps.

- Hit a real environment landmine while testing alt-screen tracking: this
  sandbox inherits `TERM=linux` from its host, which has no alt-screen
  capability (no `smcup`/`rmcup`), so vim correctly never emitted
  `ESC[?1049h` and `state` stayed stuck on `awaiting-input` instead of `tui`.
  Root cause wasn't my regex — it was `env.setdefault("TERM", ...)` only
  applying when TERM was *unset*, not when it was set to something
  alt-screen-incapable. Since cleat always renders through pyte regardless of
  what TERM the host process happened to inherit, the fix was to force
  `TERM=xterm-256color` unconditionally rather than defensively defer to
  whatever's inherited. Any real deployment with `TERM=linux` or `TERM=dumb`
  in its environment would have silently broken TUI detection.

- Found a second real bug the same way: once fish was actually being injected
  and exercised through `structure.py`'s stdout capture (previously it never
  was — fish was entirely unsupported pre-#5), its default prompt theme's
  `ESC ( B` charset-designation escape leaked straight into stdout, because
  `_ANSI_RE` never covered that escape class. Fixed by extending the regex.
  Both of these bugs are examples of the same pattern: a security/state
  feature that finally drives real code paths (alt-screen detection, fish
  stdout capture) that were previously untested, surfacing latent gaps that
  had nothing to do with the feature itself.

- Confirmed one pre-existing, unrelated test failure via `git stash`/checkout
  against the base commit before this work started:
  `test_read_output_preserves_tail_of_command_finishing_between_polls[zsh]`
  fails deterministically on zsh, but it's not a regression — zsh was simply
  never installed/tested in this environment before. Left it alone as out of
  scope for this issue rather than scope-creeping into an unrelated fix.

## wait_for: block until the session needs attention (issue #10)

- Picked this as the next feature deliberately, not by default: it's the one
  candidate from the original landscape research that's built directly on
  top of the state oracle rather than being a standalone concern (secret
  redaction, audit trails, multi-session). Wrote the plan with an explicit
  non-goals section up front — no output pattern matching, no target-state
  parameter, no cancellation — because each of those quietly reintroduces
  the exact fragile-heuristic problem the state oracle was built to remove,
  just moved into a new tool's parameters. Scope discipline mattered more
  here than for v0.2, precisely because the implementation itself is tiny
  (a thin wrapper on `_cond`/`_probe_state()`).

- Testing surfaced a real race the plan didn't anticipate: on zsh,
  `_probe_state()` can transiently report `awaiting-input` for one beat
  *right as a command is about to finish* — zsh reclaims the foreground
  pgid and re-enters its own raw ZLE mode the instant the child exits, but
  its `precmd` hook's D/A marks (what actually closes the record) haven't
  reached the reader thread yet. Existing methods never hit this because
  they block on idle-silence/record-count, not on `state` in a tight loop —
  `wait_for` is the first caller that does, so it's the first to expose it.
  Fixed with a short bounded grace window scoped entirely inside `wait_for`
  (no changes to `_probe_state` itself), gated on `fg == shell pid` so a
  genuine interactive wait (where a *child* still owns the terminal) is
  never delayed by it.

- Spun up an independent review agent with deliberately zero conversation
  context — just the PR number and pointers to the issue/plan — specifically
  so it couldn't inherit my own blind spots about my own fix. It verified
  everything itself (fresh clone, ran the real test suite, reran the new
  test 5x) rather than trusting the PR description, and it caught something
  real: the race fix was a bounded mitigation, not a full resolution, and
  had no regression test — only manual reruns on real zsh timing, which
  only reproduced the bug ~3/4 of the time. Took the finding at face value
  and added a test that forces the exact false reading deterministically via
  `monkeypatch` (pinned `tcgetpgrp`, cleared `ICANON`) instead of depending
  on real shell timing — confirmed it actually catches the regression by
  temporarily reverting the fix and watching the new test fail.

- Shipped 0.2.0 → 0.2.1 as a plain patch release (additive tool, no breaking
  changes) and learned the repo's `release.yml` only publishes to PyPI on a
  pushed `v*` tag — merging to `main` alone doesn't ship anything to
  `pip install cleat` users. Easy to miss since nothing fails loudly when a
  feature merges but the version never bumps; worth checking for on every
  "done" project that publishes a package.
