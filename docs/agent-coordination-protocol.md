# Agent coordination protocol (design)

**Status:** design, negotiated 2026-08-07 — not yet implemented.
**Scope:** machine-wide, all repos and all agents, not just `~/src/ops-worktrees`.

The [Cross-Agent Rules](ops-worktrees-layout.md#cross-agent-rules) say what agents
owe each other. They are correct and stay as written. Their weakness is that they
are **prose an agent must remember**, so compliance degrades exactly when the
machine is busy — which is when collisions happen. This document describes making
those rules **queryable**: same rules, mechanically checkable.

## Why now

Two incidents, four days apart, that no amount of re-reading the rules would have
prevented:

- **2026-08-06** — Claude Code and Hermes wrote into the same worktree 59 seconds
  apart, and a 231-line document reached `master` unreviewed. This produced Rules
  1–5.
- **2026-08-07** — Claude Code cut and deployed `ops-v1.3.2` touching the
  stayturgid secretspec subsystem while `orc` was independently mid-investigation
  of a severity-high bug in that *same* subsystem. Nothing collided. The only
  reason is that Claude Code happened to read `orc`'s pane for an unrelated
  reason. The ownership handoff they then negotiated by hand — `orc` takes
  `control/lib/secretspec_exec.py`, Claude Code stays out — existed **only in two
  panes' scrollback**, and would have evaporated on `/clear`.

The second is the more instructive failure, because nobody broke a rule. Two
agents followed every rule and were still one coincidence away from conflict.
Rules govern behaviour inside a workspace; they say nothing about *knowing what
else is live*.

A third, quieter failure mode: two finished-but-uncommitted edits sat undiscovered
in unrelated worktrees for four days. Nothing was violated. There was simply no
report that would have shown them.

## What the state of practice says (2026)

Reviewed while drafting; the convergence with what our agents independently
proposed is notable.

- **Physical isolation beats prompt-based etiquette.** "Soft isolation" via
  instructions measurably degrades on unstructured tasks. One worktree per agent
  is the norm — *we already do this*, and it is why our failures have been
  awareness failures rather than edit-collision failures.
- **Intent declaration before work beats conflict resolution after.** Agents
  declare planned scope; the system detects overlap; conflicts surface as
  structured, attributable objects rather than silent overwrites.
- **Structured JSON, not free-form prose**, so claims are machine-parsable.
- **A human override path is mandatory.**
- **Coordination overhead is real:** 2–4 concurrent agents is the practical sweet
  spot; ~8 degrades on manager capacity.

Sources: [CAID / multi-agent git coordination](https://alchemictechnology.com/blog/posts/caid-multi-agent-git-coordination.html),
[Augment Code — multi-agent coding workspace](https://www.augmentcode.com/guides/how-to-run-a-multi-agent-coding-workspace),
[Addy Osmani — the code agent orchestra](https://addyosmani.com/blog/code-agent-orchestra/).

## Negotiated design

Proposed by Claude Code (`w18:p2`) and critiqued by **Hermes**, **Codex**,
**agy** (Gemini 3.1 Pro), and **Cursor** on 2026-08-07. What follows is what they
converged on, including where they pushed back on the original straw-man.

### 1. Claim grain is the workspace, not the file

The straw-man proposed path/glob-level claims. **Rejected by every respondent.**
Cursor was blunt: it would not commit to "accurate up-front path/glob
declarations" or "checking a registry before every file edit," and a protocol that
only works when nobody is busy is worse than none.

A claim is therefore over **workspace dir + repo + branch**, optionally with
`operation` and `herdr_pane_id`. Path-level claims are explicitly deferred until
the coarse system is demonstrably used.

This is a real concession: it will not catch two agents editing different
files in the *same* worktree. It will catch every failure we have actually had.

### 2. Reuse `ops_release_lock.py`'s claim *shape* — but not its storage

`site-djbclark`'s `bin/ops_release_lock.py` already proves this machine can
run a flock-protected claim with holder identity, TTL, `EX_TEMPFAIL` (75)
contention behaviour, and stale-claim supersession. Codex and Hermes
independently named it as the implementation seed.

**`orc` then split that recommendation, and is right.** Reuse the claim *object
shape* (holder / pid / host / operation / TTL) and the error-shaping. Do **not**
reuse the storage model:

> A single mutable `claims.json` + flock is right for `ops_release_lock.py`
> because that guards ONE serialized operation (a version cut) with low write
> frequency. A machine-wide registry across ~31 worktrees and many agents will
> have frequent, small, concurrent writes — exactly the shape where flock
> contention and partial-write corruption bite.

And the fix is already validated *on this machine*: `site-private/memory` uses
one-fact-per-file with an append-only `MEMORY.md` precisely because multiple
agents write concurrently and must not overwrite each other. That convention **is**
the concurrency design.

So: **append-only JSONL event log** (claim / release events), readers fold it to
current state. This resolves the substrate question that was open in the first
draft — see [Open questions](#open-questions).

### 3. Registry is intent; herdr is liveness; they are not the same

`herdr pane list` already reports agent, status and cwd per pane. Use it — do not
invent a second liveness mechanism.

But **neither alone is authoritative.** Hermes: never treat an unoccupied worktree
as available merely because no process is visible; never infer ownership solely
from process lists. Cursor: do not pretend herdr covers non-herdr Cursor,
Ralph or daemon work.

Reclaim requires **both** a stale claim *and* a dead holder — and Hermes refused
to accept TTL expiry alone as permission to steal.

### 4. `SESSION_LOG.md` stays separate

Unanimous. Tier 1 session logs are narrative handoff state with their own
git-aware resolver. The live registry is mutual exclusion. The registry may
*reference* a handoff path; it must never become a second prose journal.

### 5. Ports already have an authority

`site-djbclark/registry/ports.yml` is the port authority and is not duplicated
here. The coordination layer points at it. Hermes: check the registry *before*
`lsof`, then treat the live check as a second step — "free right now per lsof" is
precisely what made 8080 look safe.

### 6. Zero cognitive load, or it will not be used

agy's central demand: the agent should not have to compute its own identity.
Since herdr already knows pane identity and cwd, the client should infer them.

```bash
herdr lock claim     # infers agent, pane, cwd, repo, branch
herdr lock release
```

agy's commitment was conditional on exactly this: *"Keep the cognitive load on the
agent to absolute zero."*

### 7. Advisory-with-audit first; teeth only at high-value gates

Codex: start advisory-but-visible, and **"do not claim filesystem enforcement
exists when it does not."** Once all runtimes demonstrably invoke the client, add
hard gates — but only at chokepoints that are few and consequential:

- worktree creation / activation / deletion
- destructive git operations
- daemon or long-running listener start
- `gh pr merge` (where Rule 3's provenance gate already lives)

Not around every raw file edit. Every respondent refused that, and a gate agents
route around is worse than no gate.

`orc` sharpened this: advisory-with-audit *alone* is too weak given the stated
goal of avoiding false confidence, but hard-failing every unclaimed write is a
protocol nobody follows past the first time it blocks something legitimate
mid-task. Narrow the teeth to what this machine already treats as
serialized-and-dangerous:

- release cutting / tagging
- **secrets and security-boundary files** — literally the collision `orc` and
  Claude Code just resolved by hand
- writes to the `~/ops` deploy checkouts

Hard block there; advisory log everywhere else.

### 8. Ship the dirty-worktree audit first

The cheapest, highest-value piece, and independently proposed by Codex and
Cursor: a report of **dirty or unclaimed worktrees, and claims whose holder is
dead.** It requires no adoption by anyone, breaks nothing, and would have surfaced
the four-day ghost edits immediately.

## What nobody would commit to

Recording this is the point. A protocol whose limits are undocumented creates
false confidence — the specific failure every respondent warned about.

| Refused | Who |
|---|---|
| Checking a registry before every file edit | Cursor, Codex, Hermes |
| Accurate up-front path/glob declarations | Cursor |
| Perfect claim cleanup on every crash/stop | Cursor |
| Sub-agents independently maintaining claims | Cursor |
| TTL expiry alone as permission to steal | Hermes |
| Inferring ownership solely from process lists | Hermes |
| Deleting old worktrees because claims are stale | Hermes |
| Replacing `registry/ports.yml` | Hermes |
| Declaring claims for routine in-worktree edits | orc |
| Checking the registry before *reads* (grep/cat/ls) | orc |
| Perfect TTL hygiene on its own claims | orc |
| Being the sole enforcement mechanism without herdr cross-check | orc |
| Claiming enforcement exists where it does not | Codex, Hermes |

Hermes' summary is the honest framing: the protocol is **not** effective until the
high-value entry points — agent launch, worktree creation, daemon start,
destructive git — actually invoke it. Until then it is documentation, and should
be described as such.

## Proposed v0

Deliberately small. In dependency order:

1. **`agent-coord status`** — dirty/unclaimed worktrees, stale claims, dead
   holders. Read-only, zero adoption cost. Ship alone; it has standalone value.
2. **Claim client** with atomic state and structured refusal naming the holder,
   workspace, branch and reason. Seeded from `ops_release_lock.py`.
3. **Automatic claim creation** from herdr/worktree/daemon launch paths, so the
   common case needs no agent cooperation at all.
4. **Explicit release/renew**, with reclaim requiring stale claim *and* dead
   holder.
5. **Hard gates** at the four chokepoints above — only once (3) is real.

Cursor's one-sentence version, worth keeping as the memorable form:

> Own by create; claim the worktree; never mutate foreign ownership; `ports.yml`
> before bind; `SESSION_LOG` is handoff not locks.

## Positive commitments

What agents said they *will* do unprompted is as load-bearing as what they
refused, and is narrower than the straw-man assumed. `orc`'s is the most concrete
and is a good template for the others:

- check-before-write on **anything outside a task workspace it created itself**
- check-before-write on the **narrow dangerous-operation list** in section 7
- explicitly *not* on routine in-worktree edits, reads, or exploration

The honest limit `orc` named is behavioural, not mechanical: flock and JSON are
trivial for every runtime here; reliably remembering to call them under load is
not. That is the whole reason section 7 keeps the mandatory surface small and
section 3 requires the herdr cross-check — any agent can be killed mid-edit.

## Open questions

- ~~Central mutable file vs append-only event log.~~ **Resolved** by `orc`:
  append-only JSONL, keeping `ops_release_lock.py`'s claim object shape but not
  its storage. See [section 2](#2-reuse-ops_release_lockpys-claim-shape--but-not-its-storage).
- **Non-herdr agents.** Ralph controllers, cron jobs and daemons have no pane.
  They need an identity story that does not route through herdr, or they are
  permanently invisible to the liveness half.
- **Sub-agents.** Cursor explicitly refused to have sub-agents maintain their own
  claims. Either they inherit the parent's claim, or they are out of scope.
- **Enforcement teeth on non-git resources.** Ports have `ports.yml`; launchd
  services, brew services and Keychain items do not have an equivalent authority.
