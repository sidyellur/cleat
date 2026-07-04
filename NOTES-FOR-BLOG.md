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
