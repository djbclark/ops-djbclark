# ops-worktrees — local workspace layout (mirrored doc)

`~/src/ops-worktrees/` is djbclark's local multi-repo worktree workspace for
this control-plane repo plus the four repos it orchestrates
(`stayturgid`, `site-djbclark`, `site-private`, `Shizuku`). That directory is
**not itself a git repository** — it's a plain local folder holding bare
stores and worktree checkouts — so its own `README.md` can't be versioned in
place. This file is the tracked copy of that README, kept here so the layout
and conventions survive outside one machine's disk. Update both when either
changes.

## Architecture: Bare-Store + Task Workspaces

This directory uses the **Hybrid Bare-Store + Task Workspaces** pattern —
the recommended approach for managing multiple related git repositories
where cross-repo feature work is common.

### Why this pattern?

- **Shared git objects** — Bare repos in `.store/` hold all git data once;
  worktrees are lightweight checkouts that share the object store.
- **Task isolation** — Each feature/task gets its own directory containing
  worktrees for every repo it touches. No branch conflicts, no stale state.
- **Parallel agents** — Multiple AI agents (or humans) can work on different
  task workspaces simultaneously without interference.
- **IDE-friendly** — Open any task directory (e.g., `kotlin-tooling/`) as a
  multi-root workspace in VS Code or IntelliJ.

## Directory Layout

```text
ops-worktrees/
├── .store/                          # Bare git repos (shared object stores)
│   ├── stayturgid.git
│   ├── site-private.git
│   └── site-djbclark.git
├── main/                            # Production baseline (master branches)
│   ├── stayturgid/
│   ├── site-private/
│   └── site-djbclark/
├── kotlin-tooling/                  # Task workspace (feature branches)
│   ├── stayturgid/                  # branch: feature/kotlin-tooling
│   ├── site-private/                # branch: feature/kotlin-tooling
│   └── site-djbclark/              # branch: feature/kotlin-tooling
└── README.md
```

## Repositories

| Repo | GitHub | Description |
|------|--------|-------------|
| stayturgid | `djbclark/stayturgid` | Main ops tooling project |
| site-private | `djbclark/site-private` | Private site configuration |
| site-djbclark | `djbclark/site-djbclark` | Public site / ansible playbooks |
| Shizuku | `djbclark/Shizuku` | Fork (RikkaApps → thedjchi → djbclark) — Android permission broker stayturgid depends on. Own independent release cadence (`v13.7.0-thedjchi+stayturgid-releaseNN` tags) — NOT part of the ops-vX.Y.Z coordinated release suite. |
| ops-djbclark | `djbclark/ops-djbclark` | Control-plane repo: shared Beads task DB + Ralph TUI config routing tasks across the other four. No app code, no release process of its own. |

Added 2026-08-01 as part of the Ralph TUI + Beads orchestration migration
(see `ops-djbclark`'s README for the rationale). stayturgid/site-private/
site-djbclark still form their own separate coordinated `ops-vX.Y.Z` release
suite — Shizuku and ops-djbclark are additions to *task orchestration*, not
to that release contract.

### Shizuku remote naming (fixed 2026-08-02)

`Shizuku` is a two-hop fork chain (`RikkaApps/Shizuku` → `thedjchi/Shizuku` →
`djbclark/Shizuku`), so `.store/Shizuku.git` carries three remotes instead of
the usual `origin`/`upstream` pair every other repo here has:

| Remote | Points to | Role |
|---|---|---|
| `origin` | `djbclark/Shizuku` | Your own fork — fetch/push target, PR base |
| `thedjchi` | `thedjchi/Shizuku` | Immediate parent fork |
| `upstream` | `RikkaApps/Shizuku` | Root upstream |

This used to be misconfigured — `origin` pointed at `thedjchi/Shizuku` (the
parent fork, not yours) and the actual push target was named `fork`. That's a
trap for any command that assumes `origin` == "the repo I own": e.g.
`git rebase origin/master` silently rebased onto **thedjchi's** stale master
instead of djbclark/Shizuku's, producing a rebase that looked clean locally
but conflicted on push. Fixed by renaming remotes to the table above so
`origin` means what it means everywhere else in this workspace. If a fresh
clone of `.store/Shizuku.git` is ever needed, re-apply this renaming before
trusting `origin` in scripts or rebases.

The other four repos (`stayturgid`, `site-djbclark`, `site-private`,
`ops-djbclark`) aren't forks — each has only a single `origin` remote
pointing at the matching `djbclark/<repo>`, so this trap doesn't apply to
them (checked 2026-08-02).

### Ralph controller workspaces (added 2026-08-02)

Each of the 4 orchestrated repos (stayturgid, site-djbclark, site-private,
Shizuku) has a dedicated `ralph-<repo>/<repo>` task workspace, on its own
`ralph/<repo>` branch — this is where each repo's Ralph TUI controller
actually runs, **not** `main/<repo>`. Deliberate: Ralph's forced
`autoCommit=true` under parallel mode would otherwise land straight on
`main/`'s checked-out `master` branch with no PR/review step. `main/<repo>`
stays the clean, untouched reference checkout for everything else. See
`ops-djbclark/docs/ralph-tui-setup-guide.md` §2/§4a for the full rationale
and current per-repo config paths.

## Common Operations

### Fetch latest from all repos

```bash
for repo in .store/*.git; do git -C "$repo" fetch origin; done
```

### Create a new task workspace

```bash
TASK="my-new-feature"
mkdir -p "$TASK"
for repo in .store/*.git; do
  name=$(basename "$repo" .git)
  git -C "$repo" worktree add -b "feature/$TASK" "$(pwd)/$TASK/$name" master
done

# Bootstrap per-worktree dependencies in stayturgid (.venv-test, node_modules, .ansible/collections):
(cd "$TASK/stayturgid" && just worktree-setup)
```

### Remove a task workspace

Write a final Tier 2 doc first if the work is being abandoned —
abandonment rationale is exactly the failed-approach data the
session-handoff system exists to preserve (see site-private
`docs/session-handoff-compaction-spec.md`).

```bash
TASK="my-old-feature"
for repo in .store/*.git; do
  name=$(basename "$repo" .git)
  git -C "$repo" worktree remove "$(pwd)/$TASK/$name"
done
rmdir "$TASK"
rm -rf ~/.local/state/handoffs/*/"$TASK"
```

### List all worktrees

```bash
for repo in .store/*.git; do
  echo "=== $(basename "$repo" .git) ==="
  git -C "$repo" worktree list
done
```

### Push a task branch

```bash
cd kotlin-tooling/stayturgid
git push origin feature/kotlin-tooling
```

## Key Rules

1. **Never check out the same branch in two worktrees** — Git enforces this
   to prevent index corruption.
2. **Each worktree has its own build artifacts** — `node_modules/`, `target/`,
   `.venv-test/`, `.ansible/collections/`, etc. are per-worktree. Run `just worktree-setup` in `stayturgid` after creating a new task workspace.
3. **Git hooks are shared** — Hooks live in `.store/<repo>.git/hooks/` and
   apply to all worktrees of that repo.
4. **Always use the bare repo for git worktree commands** —
   `git -C .store/stayturgid.git worktree add ...`
