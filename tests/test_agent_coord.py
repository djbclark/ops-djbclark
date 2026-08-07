import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path

from bin.agent_coord import (
    ClaimError,
    ClaimEvent,
    GateDecision,
    Worktree,
    build_status,
    evaluate_gate,
    fold_claim_events,
    holder_alive,
    load_events,
    parse_worktree_porcelain,
    run_guard,
    run_worker,
    validate_claim_target,
)


class ParseWorktreePorcelainTests(unittest.TestCase):
    def test_parses_branch_detached_and_bare_entries(self):
        output = """worktree /Users/dan/src/ops-worktrees/task/site-djbclark
HEAD abc123
branch refs/heads/feature/task

worktree /Users/dan/src/ops-worktrees/main/site-djbclark
HEAD def456
detached

worktree /Users/dan/src/ops-worktrees/.store/site-djbclark.git
HEAD 789abc
bare
"""

        entries = parse_worktree_porcelain(output)

        self.assertEqual(len(entries), 3)
        self.assertEqual(entries[0].branch, "feature/task")
        self.assertFalse(entries[0].bare)
        self.assertIsNone(entries[1].branch)
        self.assertTrue(entries[2].bare)


class ClaimFoldTests(unittest.TestCase):
    def test_release_removes_claim_and_renew_updates_expiry(self):
        events = [
            ClaimEvent(
                event="claim",
                claim_id="c1",
                workspace="/work/task/site",
                repo="site",
                branch="feature/task",
                holder={"agent": "hermes", "pid": 11, "host": "host"},
                operation="edit",
                created_at="2026-08-07T10:00:00Z",
                expires_at="2026-08-07T11:00:00Z",
            ),
            ClaimEvent(
                event="renew",
                claim_id="c1",
                workspace="/work/task/site",
                repo="site",
                branch="feature/task",
                holder={"agent": "hermes", "pid": 11, "host": "host"},
                operation="edit",
                created_at="2026-08-07T10:30:00Z",
                expires_at="2026-08-07T12:00:00Z",
            ),
        ]

        active = fold_claim_events(events)
        self.assertEqual(active["c1"].expires_at, "2026-08-07T12:00:00Z")

        released = events + [
            ClaimEvent(
                event="release",
                claim_id="c1",
                workspace="/work/task/site",
                repo="site",
                branch="feature/task",
                holder={"agent": "hermes", "pid": 11, "host": "host"},
                operation="edit",
                created_at="2026-08-07T12:30:00Z",
                expires_at=None,
            )
        ]
        self.assertEqual(fold_claim_events(released), {})

    def test_unknown_event_type_fails_closed(self):
        with self.assertRaisesRegex(ClaimError, "unknown claim event type"):
            fold_claim_events(
                [
                    ClaimEvent(
                        event="unknown",
                        claim_id="c1",
                        workspace="/work/task/site",
                        repo="site",
                        branch="feature/task",
                        holder={"agent": "hermes"},
                        operation="edit",
                        created_at="2026-08-07T10:00:00Z",
                        expires_at=None,
                    )
                ]
            )

    def test_malformed_json_shapes_and_timestamps_are_reported(self):
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as stream:
            stream.write("[]\n")
            stream.write(
                '{"event":"claim","claim_id":"x","workspace":"/x","repo":"r",'
                '"branch":"b","holder":{},"operation":"edit","created_at":"not-a-time"}\n'
            )
            stream.flush()
            events, errors = load_events(Path(stream.name))
        self.assertEqual(events, [])
        self.assertEqual(len(errors), 2)


class ClaimValidationTests(unittest.TestCase):
    def test_nonpositive_pid_is_not_alive(self):
        self.assertFalse(holder_alive({"host": socket.gethostname(), "pid": 0}))
        self.assertFalse(holder_alive({"host": socket.gethostname(), "pid": -1}))

    def test_claim_target_must_be_a_registered_worktree(self):
        worktree = Worktree(
            path="/work/task/site",
            repo="site",
            head="abc",
            branch="feature/task",
            bare=False,
            dirty=False,
            status_lines=(),
        )
        validate_claim_target(worktree, [worktree])

        with self.assertRaisesRegex(ClaimError, "not a registered worktree"):
            validate_claim_target(
                Worktree(
                    path="/work/other/site",
                    repo="site",
                    head="abc",
                    branch="feature/other",
                    bare=False,
                    dirty=False,
                    status_lines=(),
                ),
                [worktree],
            )

    def test_bare_worktree_cannot_be_claimed(self):
        bare = Worktree(
            path="/work/.store/site.git",
            repo="site",
            head="abc",
            branch=None,
            bare=True,
            dirty=False,
            status_lines=(),
        )
        with self.assertRaisesRegex(ClaimError, "bare"):
            validate_claim_target(bare, [bare])


class StatusTests(unittest.TestCase):
    def test_status_reports_dirty_unclaimed_and_stale_claims(self):
        worktrees = [
            Worktree(
                path="/work/task/site",
                repo="site",
                head="abc",
                branch="feature/task",
                bare=False,
                dirty=True,
                status_lines=(" M file.py",),
            ),
            Worktree(
                path="/work/main/site",
                repo="site",
                head="def",
                branch="master",
                bare=False,
                dirty=False,
                status_lines=(),
            ),
        ]
        claim = ClaimEvent(
            event="claim",
            claim_id="stale",
            workspace="/work/task/site",
            repo="site",
            branch="feature/task",
            holder={"agent": "dead", "pid": 99999, "host": socket.gethostname()},
            operation="edit",
            created_at="2020-01-01T00:00:00Z",
            expires_at="2020-01-01T01:00:00Z",
        )

        report = build_status(worktrees, {"stale": claim}, now="2026-08-07T12:00:00Z")

        self.assertEqual(report["summary"]["dirty"], 1)
        self.assertEqual(report["summary"]["unclaimed"], 1)
        self.assertEqual(report["summary"]["stale_claims"], 1)
        self.assertTrue(report["worktrees"][0]["dirty"])
        self.assertTrue(report["claims"][0]["stale"])
        self.assertFalse(report["worktrees"][0]["claim_mismatch"])


class HardGateTests(unittest.TestCase):
    def setUp(self):
        self.worktree = Worktree(
            path="/work/task/site",
            repo="site",
            head="abc",
            branch="feature/task",
            bare=False,
            dirty=False,
            status_lines=(),
        )
        self.claim = ClaimEvent(
            event="claim",
            claim_id="c1",
            workspace=self.worktree.path,
            repo=self.worktree.repo,
            branch="feature/task",
            holder={"agent": "hermes", "id": "session", "pid": 1, "host": socket.gethostname()},
            operation="edit",
            created_at="2026-08-07T10:00:00Z",
            expires_at="2099-01-01T00:00:00Z",
        )

    def test_commit_requires_matching_active_claim(self):
        decision = evaluate_gate(self.worktree, {"c1": self.claim}, "commit", claim_id="c1")
        self.assertIsInstance(decision, GateDecision)
        self.assertTrue(decision.allowed)

    def test_push_requires_clean_worktree(self):
        dirty = Worktree(
            path=self.worktree.path,
            repo=self.worktree.repo,
            head=self.worktree.head,
            branch=self.worktree.branch,
            bare=False,
            dirty=True,
            status_lines=(" M file",),
        )
        decision = evaluate_gate(dirty, {"c1": self.claim}, "push", claim_id="c1")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "dirty_worktree")

    def test_missing_claim_is_structured_refusal(self):
        decision = evaluate_gate(self.worktree, {}, "merge", claim_id=None)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "missing_claim")

    def test_target_escape_is_refused(self):
        decision = evaluate_gate(
            self.worktree,
            {"c1": self.claim},
            "commit",
            claim_id="c1",
            target_paths=("/work/other/site/file",),
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "path_escape")

    def test_deploy_and_secret_writes_require_human_approval(self):
        for operation in ("deploy_write", "secret_write"):
            decision = evaluate_gate(self.worktree, {"c1": self.claim}, operation, claim_id="c1")
            self.assertFalse(decision.allowed)
            self.assertEqual(decision.reason, "human_approval_required")

    def test_security_boundary_path_is_refused_inside_claimed_workspace(self):
        decision = evaluate_gate(
            self.worktree,
            {"c1": self.claim},
            "commit",
            claim_id="c1",
            target_paths=(".env.local",),
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "security_boundary_protected")


class HerdrGuardTests(unittest.TestCase):
    def test_guard_releases_after_nonzero_child(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "events.jsonl"
            result = run_guard(
                Path.cwd(),
                root=Path.cwd().parents[1],
                state=state,
                agent="herdr-test",
                holder_id="herdr-session",
                operation="edit",
                command=[sys.executable, "-c", "raise SystemExit(7)"],
                ttl_seconds=10,
            )
            self.assertEqual(result.exit_code, 7)
            events, errors = load_events(state)
            self.assertFalse(errors)
            self.assertEqual([event.event for event in events], ["claim", "release"])

    def test_guard_renews_a_long_running_child(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "events.jsonl"
            result = run_guard(
                Path.cwd(),
                root=Path.cwd().parents[1],
                state=state,
                agent="herdr-test",
                holder_id="herdr-session",
                operation="edit",
                command=[sys.executable, "-c", "import time; time.sleep(1.2)"],
                ttl_seconds=0.5,
            )
            self.assertEqual(result.exit_code, 0)
            events, errors = load_events(state)
            self.assertFalse(errors)
            self.assertIn("renew", [event.event for event in events])
            self.assertEqual(events[-1].event, "release")

    def test_guard_reports_child_launch_failure_and_releases(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "events.jsonl"
            result = run_guard(
                Path.cwd(),
                root=Path.cwd().parents[1],
                state=state,
                agent="herdr-test",
                holder_id="herdr-session",
                operation="edit",
                command=["definitely-not-a-real-executable"],
                ttl_seconds=10,
            )
            self.assertEqual(result.exit_code, 127)
            self.assertIsNotNone(result.error)
            events, errors = load_events(state)
            self.assertFalse(errors)
            self.assertEqual([event.event for event in events], ["claim", "release"])


class WorkerRunTests(unittest.TestCase):
    def test_prompt_is_stdin_only_and_artifacts_are_sanitized(self):
        fake = (
            "import json, sys; "
            "prompt=sys.stdin.read(); "
            "print(json.dumps({'type':'system','subtype':'init','session_id':'s1'})); "
            "print(json.dumps({'type':'result','subtype':'success','session_id':'s1','result':prompt}))"
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path.cwd().parents[1]
            state = Path(temp) / "events.jsonl"
            result = run_worker(
                Path.cwd(),
                root=root,
                state=state,
                artifact_root=Path(temp) / "runs",
                prompt="secret prompt that must not persist",
                agent="test-worker",
                holder_id="worker-session",
                operation="verify",
                command=[sys.executable, "-c", fake],
                timeout_seconds=10,
                max_turns=2,
            )
            self.assertEqual(result.exit_code, 0)
            manifest = json.loads((result.artifact_dir / "run.json").read_text())
            self.assertFalse(manifest["prompt_persisted"])
            self.assertNotIn("secret prompt", json.dumps(manifest))
            self.assertNotIn("secret prompt", (result.artifact_dir / "stdout.ndjson").read_text())
            events, errors = load_events(state)
            self.assertFalse(errors)
            self.assertEqual([event.event for event in events], ["claim", "release"])

    def test_timeout_releases_claim_and_returns_typed_timeout(self):
        fake = "import time; time.sleep(2)"
        with tempfile.TemporaryDirectory() as temp:
            result = run_worker(
                Path.cwd(),
                root=Path.cwd().parents[1],
                state=Path(temp) / "events.jsonl",
                artifact_root=Path(temp) / "runs",
                prompt="timeout prompt",
                agent="test-worker",
                holder_id="worker-session",
                operation="verify",
                command=[sys.executable, "-c", fake],
                timeout_seconds=0.1,
                max_turns=2,
            )
            self.assertEqual(result.exit_code, 124)
            self.assertTrue(result.timed_out)

    def test_max_turns_is_resumable_exit_75(self):
        fake = (
            "import json; "
            "print(json.dumps({'type':'result','subtype':'error_max_turns','session_id':'resume-me'})); "
            "raise SystemExit(1)"
        )
        with tempfile.TemporaryDirectory() as temp:
            result = run_worker(
                Path.cwd(),
                root=Path.cwd().parents[1],
                state=Path(temp) / "events.jsonl",
                artifact_root=Path(temp) / "runs",
                prompt="continue later",
                agent="test-worker",
                holder_id="worker-session",
                operation="verify",
                command=[sys.executable, "-c", fake],
                timeout_seconds=10,
                max_turns=1,
            )
            self.assertEqual(result.exit_code, 75)
            self.assertEqual(result.result_subtype, "error_max_turns")
            self.assertEqual(result.session_id, "resume-me")


if __name__ == "__main__":
    unittest.main()
