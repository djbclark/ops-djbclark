# ops-djbclark

Control-plane repo for the djbclark ops suite's task/issue orchestration.
Holds **only** the shared Beads DB (`.beads/`) and Ralph TUI config
fragments used to route work across the code repos — no application code
or infrastructure config of its own.

## Orchestrated repos

| Repo | Role | Routing label |
|------|------|--------------------|
| [djbclark/stayturgid](https://github.com/djbclark/stayturgid) | Android device automation / ops tooling | `repo:stayturgid` |
| [djbclark/site-djbclark](https://github.com/djbclark/site-djbclark) | Public site / ansible playbooks | `repo:site-djbclark` |
| [djbclark/site-private](https://github.com/djbclark/site-private) | Private site config + agent memory | `repo:site-private` |
| [djbclark/Shizuku](https://github.com/djbclark/Shizuku) | Fork (RikkaApps → thedjchi → djbclark) providing the Android permission broker stayturgid depends on | `repo:shizuku` |

All issues live in **this repo's single shared Beads DB** — not one DB
per code repo. Every per-repo Ralph controller's tracker config points
`workingDir` at this same checkout and filters with `--label <routing
label>` (Ralph's `beads-bv` tracker only ever forwards the *first* configured
label, so one routing label per repo, no multi-label filtering). This was
chosen over true Beads federation (`bd repo add`/per-repo `.beads/` +
distinct ID prefixes) because everything runs on one machine — one shared
directory sidesteps Dolt-remote/JSONL sync entirely. Revisit federation only
if controllers ever need to run from different machines.

## Why this repo exists

Ralph TUI (`~/.bun/bin/ralph-tui`) is architecturally single-git-root per
session — one `cwd`, one lock, one worktree manager, one merge queue.
It cannot orchestrate multiple repos as a single process. The working
pattern instead is **one Ralph controller run per repo**, each pointed at
that repo's own `--cwd`, with every controller's tracker
(`trackers[].options.workingDir`) pointed at this repo's `.beads/` instead
of a repo-local one. That gives each repo's coding agent its own git root
while sharing one task graph across all of them.

Each orchestrated repo's `.ralph-tui/config.toml` sets
`trackers[].options.workingDir` to this repo's **absolute path** directly
(not a `BEADS_DIR` env var). That's a deliberate choice, confirmed against
`beads-bv`'s actual source
(`getWorkingDir()`/`detect()` in `src/plugins/trackers/builtin/beads-bv/index.ts`):
`detect()`'s readiness check is `access(join(workingDir, beadsDir))`, and
`getWorkingDir()` returns the configured `workingDir` verbatim — so pointing
it straight at this repo's absolute path makes the check pass against the
*real* `.beads/` here, with no local placeholder needed in the orchestrated
repos at all. [subsy/ralph-tui#397](https://github.com/subsy/ralph-tui/issues/397)
is a real, separate bug (about the `BEADS_DIR` *env var* path specifically,
when no `workingDir` override is configured) — not applicable to this setup
since we don't use that path, but still worth the upstream fix and worth
re-checking if this design ever changes to rely on `BEADS_DIR` instead.

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
