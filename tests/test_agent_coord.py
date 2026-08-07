import socket
import tempfile
import unittest
from pathlib import Path

from bin.agent_coord import (
    ClaimError,
    ClaimEvent,
    Worktree,
    build_status,
    fold_claim_events,
    holder_alive,
    load_events,
    parse_worktree_porcelain,
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


if __name__ == "__main__":
    unittest.main()
