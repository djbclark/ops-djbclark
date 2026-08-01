# Ralph TUI + Beads + Beads Viewer — Setup Guide (self-reference)

Written 2026-08-01 from a full source-level research pass against
`~/src/vendor/ralph-tui` (commit `8191b80`, v0.12.0), `gastownhall/beads`
(tag `v1.1.2`), and `Dicklesworthstone/beads_viewer` (HEAD). Binaries in use:
`~/.bun/bin/ralph-tui`, `~/go/bin/bd`, `~/go/bin/bv`.

This supersedes Herdr for task-runner orchestration going forward. Herdr's
pane/tab layout conventions still apply to anything Ralph doesn't cover
(ad hoc human-relayed agent chains) — see the `herdr-orchestration` skill.

## 0. Facts that shape every decision below

- **Ralph is single-git-root per session.** One `cwd`, one session lock
  (`.ralph-tui/ralph.lock`), one worktree manager, one merge queue — all
  keyed off one `RalphConfig.cwd`. Confirmed in source
  (`src/config/types.ts`, `src/session/lock.ts`, `src/parallel/worktree-manager.ts`,
  `src/parallel/merge-engine.ts`) and by the maintainers' own PR #391
  description ("one global scheduler, one session branch, one merge queue").
  **There is no per-task working-directory override, undocumented or
  otherwise.** Do not go looking for one again — this was checked
  exhaustively against the spawn path in `src/plugins/agents/base.ts`.
- **Non-Anthropic agent backends are fully real, not a gap.** Built-in
  plugins exist for `claude`, `codex`, `cursor`, `opencode`, `gemini`,
  `github-copilot`, `kimi`, `kiro`, `pi`, `droid`
  (`src/plugins/agents/builtin/`). Grok has no standalone plugin — reachable
  only via `opencode` configured against the xAI provider. Custom agents can
  be dropped into `~/.config/ralph-tui/plugins/agents/` without forking.
  → pick the cheapest capable backend per project; this is a real
  token-economics lever, not a reason to default to `claude` everywhere.
- **A tracker's `workingDir` (in `trackers[].options.workingDir`) is
  independent of the engine's `cwd`.** This is what makes a shared
  control-plane Beads DB possible at all — the agent still edits code in
  its own repo's `cwd`, but `bd`/`bv` calls can point at a `.beads/` living
  somewhere else entirely. This key is programmatic-only — not offered by
  `ralph-tui setup`'s interactive questions — so it has to be hand-written
  into `config.toml`.
- **GH issue subsy/ralph-tui#397 is open, unfixed, zero maintainer
  engagement** (filed 2026-07-09). `getNextTask()` requires a *local*
  `<cwd>/.beads` directory to exist even when `BEADS_DIR` env var points
  elsewhere — silently reports `no_tasks` otherwise. **Workaround: create an
  empty `<cwd>/.beads/` directory.** This is sound, not hacky — tracker
  spawns pass the full unfiltered environment, so `bd`/`bv` still resolve
  the real store via `BEADS_DIR`; the empty dir only satisfies Ralph's own
  `access()`-based readiness check, which never inspects contents. Re-check
  this issue before relying on it long-term; a real fix would make the
  workaround unnecessary but shouldn't break it either.
- **`parallel.mode` requires `autoCommit: true`.** Ralph will interactively
  block ("Auto-commit is currently disabled...") if you try to run parallel
  workers without it. Defaults: `autoCommit: false`, `parallel.mode: 'never'`,
  `parallel.maxWorkers: 3`, `parallel.worktreeDir: '.ralph-tui/worktrees'`,
  `parallel.directMerge: false`, `conflictResolution.enabled: true`,
  `conflictResolution.timeoutMs: 120000`.
- **`bv --robot-next`/`--robot-triage` silently ignore `--label` — confirmed
  broken 2026-08-01 on the first live run, filed as
  [subsy/ralph-tui#401](https://github.com/subsy/ralph-tui/issues/401).**
  `bv --help` documents `-l/--label` as scoping only
  `--robot-insights`/`--robot-plan`/`--robot-priority`; empirically it's a
  no-op for `--robot-next`/`--robot-triage` (identical top pick regardless
  of the label value, even a nonexistent one). The flag that does filter
  (`--robot-by-label`) requires `--robot-priority` and hard-errors when
  combined with `--robot-next`/`--robot-triage`. `beads-bv`'s `getNextTask()`
  verifies its epic-scoped pick against `bd list --parent <epicId>` and
  falls back to the base `beads` tracker (`bd ready --parent`) if bv's pick
  doesn't belong — but has **no equivalent verification for labels**, so a
  label-scoped `beads-bv` tracker can silently hand an agent a task from a
  different repo entirely, with no error. **§2 below now uses epic-based
  scoping, not labels, because of this.** See the guide's git history for
  the superseded label-only design if you need it for context.

## 1. Standard setup — simple single-repo project (the common case)

This is what most future projects will need. No control-plane, no
multi-repo glue.

1. `cd` into the project repo.
2. `bd init` if the repo has no `.beads/` yet — creates the local Beads DB.
3. `ralph-tui setup` — interactive wizard. It will ask for:
   - Agent plugin (pick the cheapest capable one — see facts above; default
     to `claude` only if the project genuinely needs Claude-specific
     capability).
   - Tracker: pick `beads-bv` (not plain `beads`) so `bv`'s robot-triage
     modes are available to the agent for task selection, not just raw
     `bd` listing.
   - `beadsDir` (defaults `.beads`, leave it — this is a same-repo project,
     no need for the workingDir override).
4. Review the generated `config.toml` (project root, `.ralph-tui/config.toml`
   or similar per what setup writes) — confirm `autoCommit` matches whether
   you intend to ever use parallel mode later. If yes now or foreseeably,
   set `autoCommit: true` up front rather than hitting the interactive
   block mid-session.
5. `ralph-tui run` (or via the TUI) to start a session.
6. `bd`/`bv` work exactly as normal single-repo tools — nothing special.

That's the whole story for a simple project. Everything below is only for
the multi-repo ops suite (and now Shizuku — see §3).

## 2. Multi-repo pattern — as actually built (2026-08-01)

**Ralph cannot orchestrate multiple repos as one session** (see §0). The
pattern actually implemented, across stayturgid / site-djbclark /
site-private / Shizuku:

- **One Ralph "controller" run per repo**, each invoked from a **dedicated
  `ralph/<repo>` task workspace** (`~/src/ops-worktrees/ralph-<repo>/<repo>`,
  its own branch, its own `cwd`) — **not** `main/<repo>`.
  **Corrected 2026-08-02** (same day as the epic-scoping fix, after a
  cross-session review caught the original design before parallel mode was
  ever enabled): Ralph's own session lock, worktree manager, and merge
  queue all live wherever its `cwd` is. `main/<repo>` is meant to stay the
  clean, shared reference checkout — running Ralph there directly meant
  that the moment `parallel.mode` ever got turned on, its forced
  `autoCommit: true` (see §0) would autocommit straight onto `main/`'s
  checked-out branch (`master`), with no PR, no review, no isolation from
  what every other tool/human treats as the clean baseline. Routing Ralph
  through its own disposable branch instead means autocommits land there,
  and reaching `master` still requires the normal PR-and-merge step, same
  as every other piece of work in `ops-worktrees`. `main/<repo>` is never
  touched by any Ralph controller.
- **One single shared Beads DB**, not per-repo federation — the
  control-plane repo `djbclark/ops-djbclark`
  (`~/src/ops-worktrees/main/ops-djbclark`, `.beads/` prefix `ops-djbclark`).
  Chosen over `bd repo add`/federation because every controller runs on the
  same machine — one shared directory sidesteps Dolt-remote/JSONL sync
  entirely. `SourceRepo`'s `json:"-"` tag / federation's per-repo-prefix
  metadata (both real, both investigated in §Q5 of the original research
  report) turned out to be unnecessary complexity for a single-machine
  setup — don't reach for federation unless controllers ever need to run
  from different machines.
- Each repo's `.ralph-tui/config.toml` sets
  `trackers[].options.workingDir` to the **absolute path** of
  `ops-djbclark`'s checkout directly — not a `BEADS_DIR` env var. Verified
  against actual `beads-bv` source
  (`getWorkingDir()`/`detect()` in `src/plugins/trackers/builtin/beads-bv/index.ts`):
  `detect()`'s readiness check is `access(join(workingDir, beadsDir))`, and
  `getWorkingDir()` returns the configured `workingDir` verbatim. Pointing
  it straight at the control-plane's absolute path makes the check pass
  against the *real* `.beads/` there — **no empty local `.beads/`
  placeholder needed in any of the four orchestrated repos.**
  subsy/ralph-tui#397 is a real, separate bug — specifically about the
  `BEADS_DIR` *env var* path when no `workingDir` override is configured —
  not applicable here since this design doesn't use that path. (An earlier
  draft of this guide assumed the #397 workaround was required and had
  placeholder `.beads/` dirs scaffolded into all four repos; that was wrong
  and has been corrected/removed.)
- **Routing which repo a task belongs to: a per-repo Beads epic, not
  labels.** (Corrected 2026-08-01 after the first live run — see §0's `bv
  --label` gotcha and `docs/ralph-tui-setup-guide.md` git history for the
  superseded label-only design; do not resurrect it.) Each repo gets one
  epic issue in the shared `ops-djbclark` DB (`bd create "<repo> controller
  epic" --type epic`), and its `.ralph-tui/config.toml` sets
  `trackers.options.epicId` to that epic's ID. `beads-bv`'s `getNextTask()`
  verifies bv's top pick is actually a child of the configured epic
  (`bd list --parent <epicId>`) and falls back to the base `beads` tracker's
  `bd ready --parent <epicId>` selection if not — both empirically confirmed
  correct against the real DB (2026-08-01). This keeps bv's full
  graph-aware smart selection (PageRank/centrality) in play when it picks
  correctly, with a verified-correct fallback when it doesn't — unlike
  switching to the plain `beads` tracker, which would lose smart selection
  entirely. `repo:*` labels are kept on every task as human-readable
  metadata/bd-query convenience only — **never rely on them for task
  scoping**, `bv` doesn't honor them for `--robot-next`/`--robot-triage`.
  ID-prefix-based routing (via `bd repo add`/federation +
  `.bv/workspace.yaml` `--repo <prefix>`) remains a real, unused
  alternative at the `bd`/`bv` CLI level if epic-scoping ever proves
  insufficient — revisit only if needed.
- `autoCommit: false` in every config for now — deliberate, this is the
  scaffolding phase. Flip to `true` per-repo only once you're ready to run
  parallel workers there (see §0's cross-key interaction).

Net effect: this is **coordination via a shared task graph**, not a single
merged git session. Each repo's own release process (the `just
ops-release-*` flow, `ops-vMAJOR.MINOR.PATCH` tags) is untouched by any of
this — Ralph only automates the day-to-day task/PR loop per repo, same as
Herdr did, just with a shared backlog instead of ad hoc human relay.

## 3. Shizuku — should it join the umbrella?

**Recommendation: yes, as a 4th orchestrated repo using the same pattern as
§2 — but keep its release process fully independent.**

Grounding (checked 2026-08-01):

- `~/src/Shizuku` is `djbclark/Shizuku`, an actively-maintained fork chain
  (`RikkaApps/Shizuku` → `thedjchi/Shizuku` → `djbclark/Shizuku`), not a
  passive vendored dependency. Real commits with real content: boot-retry
  hardening, TCP-mode reconnect, Fire-OS-specific native-lib fixes,
  headless-start support for the stayturgid device fleet — this is code
  stayturgid depends on operationally, not upstream code being merely
  tracked.
- It already has its own independent release cadence and versioning scheme
  distinct from the ops suite: tags like
  `v13.7.0-thedjchi+stayturgid-release25`, its own signing key
  (`shizuku-djbclark-release.jks`, gitignored per repo convention), its own
  GH Releases (`gh release list -R djbclark/Shizuku` — 25+ releases, most
  recent 2026-07-31).
- It is **not currently** part of the `~/src/ops-worktrees` bare-store
  layout — no `Shizuku.git` in `.store/`. (One stray plain checkout exists
  at `ops-worktrees/coderabbit-manual-review-gate-Shizuku/` from an earlier
  one-off task; it is not wired into the bare-store pattern and shouldn't be
  mistaken for that.)

Why fold it in at all: the whole reason this came up is that a stayturgid
agent doing device-automation work will genuinely need to open PRs against
Shizuku (boot-retry bugs, TCP-mode issues, new Fire OS quirks) as *part of*
stayturgid task work, not as a separate context-switch. Giving it a `bd`
prefix (suggest `shz-`) and hydrating it into the same control-plane Beads
DB via `bd repo add ~/src/Shizuku` (or the bare-store equivalent path) means
a stayturgid-triggered task that turns out to be a Shizuku-side fix creates
a linked, routable issue instead of a disconnected TODO.

Why keep releases independent: Shizuku's release identity is tied to
tracking an upstream project (`RikkaApps/Shizuku`) with its own versioning
(`v13.7.0...`) that has nothing to do with the ops suite's
`ops-vMAJOR.MINOR.PATCH` coordinated-release contract described in
`site-private/CLAUDE.md`. Folding Shizuku's release process into that
contract would be a category error — don't do it. It stays on its own `gh
release create` workflow exactly as today; only the *task/issue graph* gets
shared.

**Decided 2026-08-01: migrate to the bare-store layout.** The concurrency
case that tipped it: Shizuku work isn't one undifferentiated stream, it's
two genuinely different lines that need to coexist without colliding —
(a) the fork's own accumulated-changes line (`fork/master`, everything
djbclark/stayturgid depends on) and (b) individual clean-room branches
meant to become upstream PRs (`thedjchi/Shizuku` or `RikkaApps/Shizuku`),
which must be based on *upstream*, not on top of (a)'s accumulated diffs,
or the PR would be unreviewable noise. That's exactly what git worktrees
solve — several branches, several bases, checked out simultaneously without
the "can't checkout the same branch twice" conflict a single working copy
would hit.

Implementation (done): `~/src/ops-worktrees/.store/Shizuku.git` is now a
bare mirror with all three remotes wired up, same convention as the
original standalone checkout (`fork` = djbclark, `origin` = thedjchi,
`upstream` = RikkaApps). `main/Shizuku` tracks `fork/master`. The original
`~/src/Shizuku` standalone checkout was left completely untouched — it was
mid-task on a feature branch (`fix-build-workflow-retag`) with uncommitted
changes in `api/` and 6 commits behind `fork/master` on GitHub when this
migration happened; that in-progress work needs to be resolved (committed
or discarded) on its own, separately from this migration.

Usage pattern going forward:
- **"Our" continuous fork work** → work directly in `main/Shizuku` (or a
  task workspace branched from `fork/master`), same as any other repo here.
- **Upstream PR candidates** → new task workspace per candidate, branched
  from `upstream/master` (not `fork/master`), e.g.:
  ```
  TASK="upstream-pr-boot-retry"
  git -C .store/Shizuku.git worktree add -b "$TASK" "$(pwd)/$TASK/Shizuku" upstream/master
  ```
  Cherry-pick or hand-port just the one fix, keep the diff minimal and
  upstream-reviewable, `gh pr create --repo RikkaApps/Shizuku` (or
  `thedjchi/Shizuku` depending which layer the fix targets) from there.
  Remove the task workspace once the PR is merged or closed, same as any
  other task workspace.

## 4a. As-built quick reference (updated 2026-08-02, current state)

| Repo | `.ralph-tui/config.toml` | Controller epic (`epicId`) | Routing label (metadata only) |
|---|---|---|---|
| stayturgid | `~/src/ops-worktrees/ralph-stayturgid/stayturgid/.ralph-tui/config.toml` | `ops-djbclark-cr0` | `repo:stayturgid` |
| site-djbclark | `~/src/ops-worktrees/ralph-site-djbclark/site-djbclark/.ralph-tui/config.toml` | `ops-djbclark-6ub` | `repo:site-djbclark` |
| site-private | `~/src/ops-worktrees/ralph-site-private/site-private/.ralph-tui/config.toml` | `ops-djbclark-6qp` | `repo:site-private` |
| Shizuku | `~/src/ops-worktrees/ralph-Shizuku/Shizuku/.ralph-tui/config.toml` | `ops-djbclark-bk7` | `repo:shizuku` |

Each `ralph-<repo>/<repo>` is its own dedicated task workspace on a
`ralph/<repo>` branch (branched from `origin/master`, or `fork/master` for
Shizuku) — **not** `main/<repo>`, per §2's cwd-placement fix. `main/<repo>`
stays untouched by every controller.

All four: `defaultAgent = "claude"`, `defaultTracker = "beads-bv"`,
`autoCommit = false`, `trackers.options.workingDir =
"/Users/djbclark/src/ops-worktrees/main/ops-djbclark"` (the control-plane
repo's path — unaffected by the controllers' own cwd move, since
`workingDir` was always independent of `cwd`), `trackers.options.epicId =
"<that repo's epic ID above>"`.

Validated with `ralph-tui doctor` from each `ralph-<repo>/<repo>` after
the relocation — all four HEALTHY, Claude Code CLI detected and
preflight-responsive.

**When filing a task meant for a specific repo's controller, parent it
under that repo's epic** (labels are metadata only — see §0/§2):
```bash
cd ~/src/ops-worktrees/main/ops-djbclark
bd create "Title" --type task --parent ops-djbclark-6ub --labels repo:site-djbclark
# or reparent an existing task:
bd update <id> --parent ops-djbclark-6ub
```
A task with no matching parent epic won't surface to any controller's
`--robot-next`/`--robot-triage`/`bd ready` selection (each only asks for
its own one `--parent <epicId>`).

**First tracking issue, dogfooding this convention:** `ops-djbclark-bc9`
(parented under the Shizuku epic `ops-djbclark-bk7`) — the
orphaned-checkout anomaly found in `~/src/Shizuku` during this migration
(also filed as
[ops-djbclark#1](https://github.com/djbclark/ops-djbclark/issues/1) since
`djbclark/Shizuku` has GitHub issues disabled). Not investigated further or
fixed — tracked only, per explicit instruction.

**First live run, 2026-08-01 — caught a real bug, not a clean pass.** The
very first `ralph-tui run` (headless, single iteration, against
`site-djbclark`, back when scoping was still label-based, and back when
the controller still ran from `main/site-djbclark`) picked up
`ops-djbclark-bc9` (a *different* repo's task) instead of the seeded
`ops-djbclark-fws`, and spent several tool calls investigating (and
concluding "safe to delete") a branch on the `~/src/Shizuku` standalone
checkout — work explicitly flagged as hands-off. Caught and stopped via
`TaskStop` before anything destructive happened; verified via `git reflog`
that nothing was modified. This is what led to the epic-scoping fix.
Filed upstream: [subsy/ralph-tui#401](https://github.com/subsy/ralph-tui/issues/401).

**Re-run, same day, after the epic-scoping fix — verified correct, not
just at the CLI level.** Re-ran the `site-djbclark` controller live a
second time (still from `main/site-djbclark` — this predates the cwd
relocation): it correctly picked `ops-djbclark-fws`, verified against
`Epic: ops-djbclark-6ub` in the session header/iteration log *before*
letting the agent proceed. The task completed cleanly (`max_iterations`
exit, no errors) and produced a real, substantive, well-hedged research
comment on the source GitHub issue
([djbclark/site-djbclark#35](https://github.com/djbclark/site-djbclark/issues/35))
— independently re-verified via `gh issue view`, not just trusted from the
controller's self-report. It correctly declined to close the issue itself,
per the task's own instruction to leave that for explicit confirmation.

**Standing rule going forward, not optional:** do not trust a clean exit
code alone as proof scoping worked. Verify the picked task's ID against
`bd list --parent <epicId>` yourself before letting the agent touch
anything, every time a controller goes live for the first time (or after
any config change to how it's invoked).

**Resolved 2026-08-02: no separate gate for external actions.** The
site-djbclark run posting directly to a live GitHub issue raised the
question of whether external, visible actions (GitHub comments/issues/PRs)
need their own review step distinct from `autoCommit`. Decided: no —
the task's own scope is the real control, same philosophy as
`autoCommit` itself. If a task's instructions call for posting somewhere
(as that one explicitly did — "paste this into a fresh AI session for an
independent read"), a well-scoped controller does it autonomously. This
places the responsibility on task authoring (write tasks that only ask
for what you actually want done) rather than on a manual approval step
for every external action — consistent with the repo now being public
and the operator's general risk tolerance for this project.

**Second controller live-validated, 2026-08-01/02: `stayturgid`, from its
correct post-relocation workspace.** Before going live, two prerequisites
were handled: (1) `ops-djbclark-bc9` (the Shizuku orphaned-checkout task)
got an explicit "tracked only, do not act" constraint added to its own
description — it had only existed in memory/docs before, invisible to any
agent reading the task itself, a real gap under the no-separate-gate
policy just above; (2) a research-only task
(`ops-djbclark-cr0.1`, covering the remaining app-disposition scope of
[stayturgid#151](https://github.com/djbclark/stayturgid/issues/151)) was
seeded under stayturgid's epic, same safe shape as the site-djbclark task.
Ran `ralph-tui run --headless --iterations 1 --verify --epic
ops-djbclark-cr0` from `ralph-stayturgid/stayturgid` — verified `Epic:
ops-djbclark-cr0` / task `ops-djbclark-cr0.1` in the session header
*before* letting the agent proceed, same discipline as site-djbclark's
re-run. Completed cleanly (5m12s, `max_iterations` exit), independently
re-verified after (not just trusted from the self-report):
`git status` in the task workspace was completely clean — zero code
changes, despite the task's no-code-changes scope being enforced by
nothing but the task's own text (the agent had full Bash/Edit access the
entire time). Real GitHub comment confirmed via `gh issue view`
([stayturgid#151](https://github.com/djbclark/stayturgid/issues/151#issuecomment-5154020315),
posted 2026-08-01T23:39:50Z), issue correctly left open, bd bead correctly
closed. This is the clearest evidence yet that "task scope is the real
control" holds under real conditions, not just as a policy statement.

**Not yet done:** `site-private` and `Shizuku` remain unvalidated —
`Shizuku` has a properly hands-off-scoped task waiting
(`ops-djbclark-bc9`) if it's ever picked up; `site-private` has no task
seeded yet. **Operator explicitly paused here (2026-08-02)** to check in
given how much shipped this session — do not seed or bring up a third
controller without that check-in first.

## 4. Before trusting any of this in production

- **Resolved 2026-08-01:** `source_repo` confirmed absent from `bd list
  --json` (bd v1.1.2) — struct tag was right, docs' worked example was
  wrong.
- Re-check subsy/ralph-tui#397 periodically — it's a real bug, not a
  documented feature; a fix could change or remove the empty-`.beads/`
  workaround's necessity. (Still not applicable to this design either way
  — see §0.)
- **New 2026-08-01:** re-check
  [subsy/ralph-tui#401](https://github.com/subsy/ralph-tui/issues/401)
  (the `beads-bv` label-verification gap that made the original label-based
  design unsafe) periodically — if it's fixed upstream, labels could
  become a viable *additional* filter again, but epic-based scoping should
  stay the primary mechanism regardless since it has a verified-correct
  fallback path and labels don't.
- The `beads-bv` tracker plugin not forwarding `--workspace`/`--repo` to
  `bv` means any BV-native multi-repo view has to be driven outside Ralph
  for now. If cross-repo task routing becomes a frequent pain point, that's
  the specific, narrow fork target (`src/plugins/trackers/builtin/beads-bv/index.ts`)
  — not a reason to fork more broadly.
- Always watch the first live iteration of any new controller closely and
  verify the picked task ID against `bd list --parent <epicId>` yourself —
  see §4a's 2026-08-01 first-live-run writeup for why this isn't optional.
