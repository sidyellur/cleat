## Blog journal

The running blog journal for this project no longer lives in the repo:
`NOTES-FOR-BLOG.md` was retired in July 2026 (see commit cfc2e6e) and the
journal moved to tether (memory "cleat blog journal", tag
`blog-journal,proj:cleat`). Do not recreate the file. If you are working
in an environment with tether access, append brief entries there capturing
key decisions and *why* you made them — not a full transcript, just the
moments a reader would care about (a design choice, a tricky bug and its
root cause, a pivot in direction). Keep entries short, a few sentences each.
Without tether access, summarize those moments in the PR description or the
commit message instead.

Near a natural stopping point (a PR merges, a milestone is reached, the user
indicates they're wrapping up), ask the user: "Is this blog-worthy done? I
can write up a draft post about it." If they say yes, spawn the
`expert-blog-writer` subagent to produce the draft. If they say no, do
nothing further — the journal stays as-is for the next check-in.
