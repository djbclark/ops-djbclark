# Headless Claude Code worker contract (design)

**Status:** v0 explicit runner implemented as `bin/agent-coord run`; automatic
Herdr/worktree hooks and proof-pack generation remain staged work.

**Scope:** how a supervising process (orchestrator, Ralph controller, cron
job, or another agent) should launch and manage a **headless** `claude`
subprocess as a worker, so that the machine-wide rules in
[ops-worktrees-layout.md#cross-agent-rules](ops-worktrees-layout.md#cross-agent-rules)
and the claim model in
[agent-coordination-protocol.md](agent-coordination-protocol.md) hold even
when no human is watching the session.

This adapts the general "agent worker orchestration" pattern — isolate,
supervise via a structured event stream, verify independently of the
worker's own self-report, escalate rather than guess — to Claude Code 2.x's
actual headless surface (`claude -p`, `--output-format stream-json`, session
resume, and the permission-settings model). It does not assume or depend on
any specific third-party framework.

## 1. Isolated worktree, one worker per workspace

A headless worker gets its own task workspace under
`~/src/ops-worktrees/<task>/`, created per the
[task workspace convention](ops-worktrees-layout.md#create-a-new-task-workspace),
before the subprocess is launched — never a shared or reused worktree.
This is [Cross-Agent Rule 1](ops-worktrees-layout.md#cross-agent-rules)
applied to an automated launcher: creating the workspace is what establishes
ownership, so the launcher must create it itself rather than pointing a
worker at a path it merely found unoccupied.

One worker process per workspace. If a supervisor wants N concurrent
headless workers, it creates N task workspaces, not N processes sharing one
checkout.

## 2. Invocation: argv subprocess with stdin prompt, subscription-authenticated

Launch `claude` as a subprocess with an explicit argv array and send the
prompt through stdin (e.g. Python `subprocess.Popen([...])`, Node
`child_process.spawn([...])`), never by interpolating a prompt or path into a
shell string. Prompts and file paths
are attacker- and model-controlled text; shell interpolation of either is a
command-injection hazard independent of anything Claude-specific.

Launch the worker so it authenticates the same way an interactive session
would — inheriting the operator's normal `claude` login/config (the
"subscription" auth path) — rather than a stripped-down invocation that
drops the user's config/credentials and falls back to a separate,
metered API key. A headless fleet that silently shifted from subscription
usage to pay-per-token API billing because the launcher used a bare
environment would be a costly and easy-to-miss mistake. Concretely: don't
scrub `HOME`/config directories or override credentials env vars when
constructing the subprocess environment unless that's a deliberate,
documented choice; the default should look like "the same `claude` the
operator would run by hand," not a minimal/bare reimplementation.

## 3. Supervision via `stream-json`, not raw stdout scraping

Run the worker in print mode with structured streaming output:

```bash
claude -p "<task prompt>" \
  --output-format stream-json \
  --verbose \
  --permission-mode <mode>
```

On this machine, use the subscription-preserving `claude-sub` launcher rather
than calling a bare/API-key configuration. Its `--stdin` mode supplies `-p`
without putting the prompt in process arguments:

```bash
claude-sub --stdin \
  --model sonnet \
  --max-turns 10 \
  --permission-mode dontAsk \
  --output-format stream-json --verbose --include-partial-messages
```

The launcher must reject `--bare` and API-key overrides for this workflow.
Those options can silently move the run off the operator's subscription path.
The exact executable and authentication state should be recorded in
`run.json` without recording credentials.

`stream-json` emits one JSON object per line (NDJSON): a `system`/`init`
event first (carries the session id, model, cwd, and effective tool/permission
config), then `assistant`/`user`/tool-use events as the turn progresses, and
a terminal `result` event. A supervisor should parse this stream
line-by-line rather than waiting for the process to exit and scraping
stdout as text — that's what makes turn-by-turn oversight (timeouts,
mid-run cancellation, live progress) possible at all.

Treat the exact field names and event shape as **version-dependent** — verify
against the installed CLI (`claude -p --help`, and a smoke run against a
throwaway prompt) before hard-coding field access, since this surface has
changed across Claude Code releases and will again.

## 4. Typed result handling — `error_max_turns` is resumable, not failed

The terminal `result` event carries a subtype distinguishing at least:
normal completion, hitting the turn budget, and an execution error. A
supervisor must not collapse these into a single pass/fail bit:

- **Turn-budget exhaustion** ("ran out of turns" style result) means the
  worker was still making progress and stopped only because of the `--max-turns`
  bound. The correct response is to **resume the same session** (§5) with
  either the same instruction or a shorter follow-up — not to report failure,
  and not to start a fresh session that repeats work already done.
- **Execution error** results (crashed, aborted) are a real failure and
  should be surfaced, not silently retried in a loop.
- **Success** results should still go through independent verification
  (§7) before anything downstream trusts them.

The v0 runner records only sanitized event metadata by default. Response
content capture is intentionally opt-in and bounded; the prompt is never
persisted. This is stronger than the general contract's raw-event suggestion
because prompts and model responses may contain credentials or other private
data.

## 5. Session resume for continuation, not re-prompting from scratch

Capture the session id from the initial `system`/`init` event and use it to
continue a worker that stopped mid-task (turn-budget exhaustion, a transient
error, or a deliberate pause):

```bash
claude -p "<continuation prompt>" \
  --resume <session-id> \
  --output-format stream-json --verbose
```

Resuming preserves the model's accumulated context about what it already
changed; re-issuing the original prompt as a new session does not, and risks
the worker redoing or conflicting with its own earlier edits in the same
worktree. Persist the session id in the run's artifacts (§6) specifically so
a later supervisor invocation — possibly a different process — can resume
it.

## 6. Bounded artifacts per run

Each worker invocation writes a fixed, bounded set of artifacts under
`~/.local/state/agent-coord/runs/<run-id>/` — not an unbounded
or ad hoc log dump. Raw event and error logs are truncated or rotated at a
configured byte limit; the limit is part of the run configuration and a
truncation marker is recorded in `run.json`:

- **`run.json`** — a manifest: command/argv invoked, cwd, start/end
  timestamps, session id, permission mode, the terminal result subtype, and
  exit code. This is the thing a supervisor or human reads first to decide
  what happened.
- **stdout/stderr** — sanitized stream metadata and bounded stderr, captured
  so a runaway or looping worker cannot fill the disk. Prompt and response
  content are omitted by default.
- **proof pack** — the evidence a human or the deterministic verification
  step (§7) needs to check the worker's claim: the diff it produced
  (`git diff`), and the output of whatever build/test/lint commands were run
  against its changes. This is what makes "the worker said it passed tests"
  checkable after the fact instead of taken on faith.

The run directory is unique and owned by the invoking user; the claimed
worktree remains the worker's own task workspace. This is consistent with
[Cross-Agent Rule 5](ops-worktrees-layout.md#cross-agent-rules) (one
owner/integrator per workspace).

## 7. Deterministic verification, independent of the worker's self-report

A worker claiming "tests pass" or "done" is a claim, not a fact. Before
anything treats a headless run as successful, a **separate** step —
run by the supervisor, not by asking the same Claude session to grade its
own work — re-runs the actual, deterministic checks (build, test suite,
linter, `git diff` review against the stated scope) against the worktree
state the worker left behind. This mirrors
[Cross-Agent Rule 3](ops-worktrees-layout.md#cross-agent-rules)'s point that
green checks are evidence the code works, not evidence you know what you're
merging — applied one step earlier, to trusting the worker's own report at
all.

If no deterministic check exists for a given task, that's a gap to flag,
not a reason to fall back to trusting the model's narrative.

## 8. Protected-path escalation

Certain paths are never something a headless worker resolves on its own,
regardless of what its permission-mode would otherwise allow:

- secrets and security-boundary files,
- release-cutting and tagging operations,
- the `~/ops` deploy checkouts (see the root `CLAUDE.md`/`AGENTS.md` policy:
  these are deploy-only, not development targets, for *any* agent, headless
  or interactive),
- anything else the operator has designated a hard-gate chokepoint per
  [agent-coordination-protocol.md §7](agent-coordination-protocol.md#7-advisory-with-audit-first-teeth-only-at-high-value-gates).

A headless worker that reaches one of these should stop and escalate —
write a clear `run.json` entry describing what it needed and why, and end
the run — rather than proceeding under a broad permission grant. This is
the same "narrow the teeth to what's already treated as
serialized-and-dangerous" principle the coordination protocol proposes for
interactive agents; a headless worker has less opportunity for a human to
notice a mistake in the moment, so the escalation trigger should if
anything be stricter, not looser.

## 9. No autonomous commit, push, or merge

A headless worker prepares changes — it does not land them. It may stage
and describe a diff inside its own worktree, but committing, pushing, or
merging is a decision point that stays with the supervisor or a human,
every time, regardless of permission mode. This is not a headless-specific
rule; it's [Cross-Agent Rule 2 and the pre-merge provenance
gate](ops-worktrees-layout.md#cross-agent-rules) applied without the
loophole that "nobody was watching this run" might otherwise create. If a
supervisor wants a fully autonomous commit/push pipeline, that is a
separate, explicit policy decision to make at the supervisor level — it is
not the default posture for a headless worker under this contract, and
this document does not grant it.

## 10. Herdr is the interactive fallback, not the default path

When a headless run hits something it cannot resolve deterministically —
an ambiguous instruction, a permission decision the configured policy
doesn't cover, a protected-path escalation (§8) that needs a real answer —
the fallback is to hand the situation to a human via an interactive
session (e.g. a Herdr pane), not to guess, not to loosen the permission
policy to make the block go away, and not to retry the same headless
invocation hoping for a different outcome. Herdr's role here is strictly
the human-in-the-loop escape hatch for cases the headless contract itself
identifies as needing one — it is not part of normal headless supervision,
and nothing in this document should be read as making Herdr the required
launcher for headless workers.

## 11. Permissions: `allowedTools` filters requests, it doesn't remove tools

A common misreading of Claude Code's permission flags: `--allowedTools` /
`--disallowedTools` (and the equivalent `permissions.allow` /
`permissions.deny` settings) do **not** remove tools from what the model
knows it can attempt. The model still sees the full tool set it was given
and can still emit a tool-use request for something not on the allow list —
the permission layer then approves or blocks that specific request. Sizing
a headless run's safety on "the model can't even see that tool" is wrong;
size it on "every request the model could plausibly make either resolves
without a prompt or is explicitly denied," because a headless process has
no terminal to answer an interactive permission prompt on.

For a headless worker, that means:

- Write an explicit **allow/deny policy** (`permissions.allow` /
  `permissions.deny` in settings, or the equivalent CLI flags) that covers
  every tool the task genuinely needs, rather than relying on a broad
  allow and hoping the model doesn't reach further.
- Configure the run so it never blocks on an interactive prompt it cannot
  answer — a headless process attached to no TTY that hits an unresolved
  "ask" decision will hang, not fail loudly. That's the practical meaning
  of running with a "don't ask" (non-interactive) permission posture: every
  request must resolve to allow or deny from policy alone, with no implicit
  third option.
- Verify the exact settings/flag names and defaults against the installed
  CLI version before relying on them — this is exactly the kind of surface
  that has shifted across Claude Code releases (see §3's caveat) and where
  a stale assumption becomes a silent security gap rather than a visible
  error.

The upstream references for these semantics are [Claude Code programmatic
usage](https://code.claude.com/docs/en/headless) and the [Agent SDK permission
evaluation](https://code.claude.com/docs/en/agent-sdk/permissions). The
coordinator should pin or record the installed Claude Code version in
`run.json`; it must not silently assume that a future CLI release preserves
the same event or permission surface.

## Relationship to the coordination protocol

This document is a worker-level contract: it governs how one supervisor
runs one headless `claude` process safely. It does not replace
[agent-coordination-protocol.md](agent-coordination-protocol.md), which
governs how *multiple* agents (headless or interactive) avoid colliding
with each other across the whole `~/src/ops-worktrees/` machine. A headless
worker built to this contract is still subject to every cross-agent rule in
[ops-worktrees-layout.md](ops-worktrees-layout.md#cross-agent-rules) and,
once implemented, to the coordination protocol's claim/registry model —
this document just makes explicit what "following those rules" requires
when there is no human in the loop to notice a near-miss.
