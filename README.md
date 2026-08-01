# ops-djbclark

Control-plane repo for the djbclark ops suite's task/issue orchestration.
Holds **only** the shared Beads DB (`.beads/`) and Ralph TUI config
fragments used to route work across the code repos — no application code
or infrastructure config of its own.

## Orchestrated repos

| Repo | Role | `bd` issue prefix |
|------|------|--------------------|
| [djbclark/stayturgid](https://github.com/djbclark/stayturgid) | Android device automation / ops tooling | `st-` |
| [djbclark/site-djbclark](https://github.com/djbclark/site-djbclark) | Public site / ansible playbooks | `sd-` |
| [djbclark/site-private](https://github.com/djbclark/site-private) | Private site config + agent memory | `sp-` |
| [djbclark/Shizuku](https://github.com/djbclark/Shizuku) | Fork (RikkaApps → thedjchi → djbclark) providing the Android permission broker stayturgid depends on | `shz-` |

## Why this repo exists

Ralph TUI (`~/.bun/bin/ralph-tui`) is architecturally single-git-root per
session — one `cwd`, one lock, one worktree manager, one merge queue.
It cannot orchestrate multiple repos as a single process. The working
pattern instead is **one Ralph controller run per repo**, each pointed at
that repo's own `--cwd`, with every controller's tracker
(`trackers[].options.workingDir`) pointed at this repo's `.beads/` instead
of a repo-local one. That gives each repo's coding agent its own git root
while sharing one task graph across all of them.

Each orchestrated repo also carries an empty local `.beads/` placeholder
directory (workaround for
[subsy/ralph-tui#397](https://github.com/subsy/ralph-tui/issues/397) — an
open, unfixed bug where Ralph's tracker-readiness check requires a
*local* `.beads/` to exist even when `BEADS_DIR` points elsewhere).

## What does NOT live here

- No application code, ansible config, or device-automation logic — that
  stays in the four repos above.
- No release process. This repo is **not** part of the ops suite's
  coordinated `ops-vMAJOR.MINOR.PATCH` release contract
  (see `site-private`'s `CLAUDE.md` / `docs/OPS-RELEASES.md` in
  `site-djbclark`) — it's operational task data, versioned by ordinary
  commits, not tagged releases.
- Shizuku's own release process (`v13.7.0-thedjchi+stayturgid-releaseNN`
  tags, independent signing key) is untouched by any of this — only its
  *task/issue graph* is shared here.

See the full setup writeup (agent-facing, not yet migrated into this repo)
for the researched rationale behind these choices.
