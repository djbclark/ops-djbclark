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

## 2. Multi-repo pattern — stayturgid / site-djbclark / site-private

**Ralph cannot orchestrate all three as one session** (see §0). The
realistic pattern, confirmed feasible from source:

- **One Ralph "controller" run per repo**, each invoked with
  `--cwd <that-repo's-worktree>`.
- **A shared control-plane Beads DB** — either a dedicated 4th repo holding
  only `.beads/` + shared `config.toml` fragments, or (simpler to start)
  one of the three repos' own `.beads/` promoted to "primary" via
  `bd repo add <path>` pulling in the others as `additional`.
- Each per-repo Ralph's tracker config sets
  `trackers[].options.workingDir` to the control-plane repo's path, so all
  three controllers read/write the same task graph while each agent process
  still edits code in its own repo's `cwd`.
- **Apply the #397 workaround** in each per-repo `cwd`: an empty
  `.beads/` placeholder directory, with the real store reached via
  `BEADS_DIR` env var (or `--db`) pointing at the control-plane.
- **Routing which repo a task belongs to**: `bd repo add/sync` tags
  hydrated issues with `SourceRepo`, but this field is `json:"-"` in the Go
  struct — **verify empirically** (`bd list --json | jq '.[0]'`) whether
  `source_repo` actually appears in JSON output before depending on it for
  routing. The robust fallback that's guaranteed visible either way: give
  each repo its own `bd` issue-ID prefix (e.g. `st-`, `sd-`, `sp-`) and
  route/filter by prefix. Beads Viewer's `.bv/workspace.yaml`
  (`repos[].prefix`, `--repo <prefix>` CLI flag) already models this
  natively — but Ralph's shipped `beads-bv` tracker plugin does **not**
  forward `--workspace`/`--repo` to `bv` (checked `execBv()` call sites
  directly — only `--robot-next`/`--robot-triage`/`--label` are ever
  passed). Using BV's workspace/prefix view from *inside* Ralph requires
  forking `src/plugins/trackers/builtin/beads-bv/index.ts` to append those
  flags; without that fork, prefix-based routing has to happen in whatever
  is deciding *which* per-repo Ralph controller to hand a task to (i.e.
  external to Ralph itself, e.g. bare `bv --workspace ... --repo ...`
  queries run by you/an orchestrating script, not by Ralph's own task
  selection).
- If any repo needs parallel workers, remember `autoCommit: true` is
  mandatory for that repo's config.

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

## 4. Before trusting any of this in production

- Verify `source_repo` actually appears in `bd list --json` output against
  the real installed `bd` v1.1.2 — the Go struct tag (`json:"-"`) says it
  shouldn't, the docs' worked examples say it does. Don't guess; run it.
- Re-check subsy/ralph-tui#397 periodically — it's a real bug, not a
  documented feature; a fix could change or remove the empty-`.beads/`
  workaround's necessity.
- The `beads-bv` tracker plugin not forwarding `--workspace`/`--repo` to
  `bv` means any BV-native multi-repo view has to be driven outside Ralph
  for now. If cross-repo task routing becomes a frequent pain point, that's
  the specific, narrow fork target (`src/plugins/trackers/builtin/beads-bv/index.ts`)
  — not a reason to fork more broadly.
