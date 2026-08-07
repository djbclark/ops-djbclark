"""Read and mutate the machine-wide agent coordination event log.

The status command is deliberately read-only. Claim, release, and renew append
one JSON object per event under ``~/.local/state/agent-coord/events.jsonl``.
Readers fold the append-only log; no mutable shared registry file is used.
"""

from __future__ import annotations

import argparse
import datetime as datetime_module
import fcntl
import json
import os
import socket
import subprocess
import sys
import uuid
from collections.abc import Iterable, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

DEFAULT_ROOT = Path.home() / "src" / "ops-worktrees"
DEFAULT_STATE = Path.home() / ".local" / "state" / "agent-coord" / "events.jsonl"
GIT_TIMEOUT_SECONDS = 10


class ClaimError(RuntimeError):
    """A structured refusal to create or mutate a claim."""


@dataclass(frozen=True)
class Worktree:
    path: str
    repo: str
    head: str
    branch: str | None
    bare: bool
    dirty: bool
    status_lines: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["status_lines"] = list(self.status_lines)
        return value


@dataclass(frozen=True)
class ClaimEvent:
    event: str
    claim_id: str
    workspace: str
    repo: str
    branch: str
    holder: dict[str, Any]
    operation: str
    created_at: str
    expires_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ClaimEvent:
        if not isinstance(value, dict):
            raise TypeError("claim event must be a JSON object")
        required = ("event", "claim_id", "workspace", "repo", "branch", "holder", "operation", "created_at")
        missing = [key for key in required if key not in value]
        if missing:
            raise ValueError(f"claim event missing fields: {', '.join(missing)}")
        if not isinstance(value["holder"], dict):
            raise TypeError("claim event holder must be an object")
        try:
            _parse_time(str(value["created_at"]))
            if value.get("expires_at") is not None:
                _parse_time(str(value["expires_at"]))
        except ValueError as exc:
            raise ValueError(f"claim event has invalid timestamp: {exc}") from exc
        return cls(
            event=str(value["event"]),
            claim_id=str(value["claim_id"]),
            workspace=str(value["workspace"]),
            repo=str(value["repo"]),
            branch=str(value["branch"]),
            holder=dict(value["holder"]),
            operation=str(value["operation"]),
            created_at=str(value["created_at"]),
            expires_at=None if value.get("expires_at") is None else str(value["expires_at"]),
        )


def parse_worktree_porcelain(output: str) -> list[Worktree]:
    """Parse ``git worktree list --porcelain`` without invoking git."""
    records: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    for line in output.splitlines() + [""]:
        if not line:
            if current:
                records.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            current["path"] = value
        elif key == "HEAD":
            current["head"] = value
        elif key == "branch":
            current["branch"] = value.removeprefix("refs/heads/")
        elif key == "detached":
            current["branch"] = None
        elif key == "bare":
            current["bare"] = True

    return [
        Worktree(
            path=str(record["path"]),
            repo="",
            head=str(record.get("head", "")),
            branch=record.get("branch"),
            bare=bool(record.get("bare", False)),
            dirty=False,
            status_lines=(),
        )
        for record in records
    ]


def _run_git(args: Sequence[str], cwd: Path | None = None) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise ClaimError(f"git probe failed: {' '.join(args)}: {exc}") from exc
    return result.stdout


def discover_worktrees(root: Path) -> list[Worktree]:
    """Discover worktrees from every bare store below ``root/.store``."""
    store_dir = root / ".store"
    if not store_dir.is_dir():
        raise ClaimError(f"worktree store directory does not exist: {store_dir}")

    discovered: list[Worktree] = []
    for store in sorted(store_dir.glob("*.git")):
        repo = store.name.removesuffix(".git")
        entries = parse_worktree_porcelain(_run_git(["worktree", "list", "--porcelain"], cwd=store))
        for entry in entries:
            if entry.bare:
                discovered.append(Worktree(**{**entry.to_dict(), "repo": repo, "status_lines": tuple(entry.status_lines)}))
                continue
            status = _run_git(["status", "--porcelain", "--untracked-files=all"], cwd=Path(entry.path))
            lines = tuple(status.splitlines())
            discovered.append(
                Worktree(
                    path=entry.path,
                    repo=repo,
                    head=entry.head,
                    branch=entry.branch,
                    bare=False,
                    dirty=bool(lines),
                    status_lines=lines[:100],
                )
            )
    return discovered


def fold_claim_events(events: Iterable[ClaimEvent]) -> dict[str, ClaimEvent]:
    """Fold claim/renew/release events to the latest active claims."""
    active: dict[str, ClaimEvent] = {}
    for event in events:
        if event.event in {"claim", "renew"}:
            active[event.claim_id] = event
        elif event.event == "release":
            active.pop(event.claim_id, None)
        else:
            raise ClaimError(f"unknown claim event type: {event.event}")
    return active


def load_events(path: Path) -> tuple[list[ClaimEvent], list[str]]:
    if not path.exists():
        return [], []
    events: list[ClaimEvent] = []
    errors: list[str] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            events.append(ClaimEvent.from_dict(json.loads(line)))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"line {line_number}: {exc}")
    return events, errors


def _parse_time(value: str) -> datetime_module.datetime:
    return datetime_module.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(datetime_module.timezone.utc)


def _now() -> datetime_module.datetime:
    return datetime_module.datetime.now(datetime_module.timezone.utc)


def holder_alive(holder: dict[str, Any]) -> bool | None:
    """Return false for a dead local pid, true for a live one, unknown remotely."""
    if holder.get("host") != socket.gethostname():
        return None
    try:
        pid = int(holder["pid"])
        if pid <= 0:
            return False
        os.kill(pid, 0)
    except (KeyError, TypeError, ValueError, ProcessLookupError):
        return False
    except PermissionError:
        return True
    return True


def validate_claim_target(target: Worktree, registered: Sequence[Worktree]) -> None:
    if target.bare:
        raise ClaimError(f"cannot claim bare worktree: {target.path}")
    if not any(item.path == target.path and item.repo == target.repo for item in registered):
        raise ClaimError(f"not a registered worktree: {target.path}")
    if not target.branch:
        raise ClaimError(f"cannot claim detached worktree: {target.path}")


def build_status(
    worktrees: Sequence[Worktree],
    claims: dict[str, ClaimEvent],
    *,
    now: str | None = None,
) -> dict[str, Any]:
    current = _parse_time(now) if now else _now()
    claim_rows: list[dict[str, Any]] = []
    claims_by_workspace: dict[str, list[ClaimEvent]] = {}
    for claim in claims.values():
        stale = claim.expires_at is not None and _parse_time(claim.expires_at) <= current
        alive = holder_alive(claim.holder)
        row = claim.to_dict()
        row.update({"stale": stale, "holder_alive": alive, "reclaimable": stale and alive is False})
        claim_rows.append(row)
        claims_by_workspace.setdefault(claim.workspace, []).append(claim)

    worktree_rows: list[dict[str, Any]] = []
    for worktree in worktrees:
        attached = claims_by_workspace.get(worktree.path, [])
        row = worktree.to_dict()
        row["claims"] = [claim.claim_id for claim in attached]
        row["claimed"] = bool(attached)
        row["claim_mismatch"] = any(
            claim.repo != worktree.repo or claim.branch != worktree.branch for claim in attached
        )
        worktree_rows.append(row)

    nonbare = [item for item in worktrees if not item.bare]
    return {
        "version": 1,
        "generated_at": current.isoformat().replace("+00:00", "Z"),
        "summary": {
            "worktrees": len(nonbare),
            "dirty": sum(item.dirty for item in nonbare),
            "unclaimed": sum(not claims_by_workspace.get(item.path) for item in nonbare),
            "stale_claims": sum(row["stale"] for row in claim_rows),
            "reclaimable_claims": sum(row["reclaimable"] for row in claim_rows),
            "claim_mismatches": sum(row["claim_mismatch"] for row in worktree_rows),
        },
        "worktrees": worktree_rows,
        "claims": sorted(claim_rows, key=lambda value: value["claim_id"]),
    }


@contextmanager
def _state_lock(path: Path):
    """Serialize claim decisions with a sidecar lock, not the event log."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _append_event_unlocked(path: Path, event: ClaimEvent) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(event.to_dict(), sort_keys=True) + "\n").encode("utf-8")
    with path.open("ab") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


def _event(
    event: str,
    *,
    claim_id: str,
    workspace: Worktree,
    agent: str,
    holder_id: str,
    operation: str,
    ttl_seconds: int | None,
) -> ClaimEvent:
    created = _now()
    expiry = None if ttl_seconds is None else (created + datetime_module.timedelta(seconds=ttl_seconds)).isoformat().replace("+00:00", "Z")
    return ClaimEvent(
        event=event,
        claim_id=claim_id,
        workspace=workspace.path,
        repo=workspace.repo,
        branch=workspace.branch or "",
        holder={"agent": agent, "id": holder_id, "pid": os.getpid(), "host": socket.gethostname()},
        operation=operation,
        created_at=created.isoformat().replace("+00:00", "Z"),
        expires_at=expiry,
    )


def claim_workspace(
    path: Path,
    *,
    root: Path,
    state: Path,
    agent: str,
    holder_id: str,
    operation: str,
    ttl_seconds: int,
) -> ClaimEvent:
    if ttl_seconds <= 0:
        raise ClaimError("ttl-seconds must be positive")
    worktrees = discover_worktrees(root)
    target = next((item for item in worktrees if Path(item.path).resolve() == path.resolve()), None)
    if target is None:
        raise ClaimError(f"not a registered worktree: {path}")
    validate_claim_target(target, worktrees)
    with _state_lock(state):
        events, errors = load_events(state)
        if errors:
            raise ClaimError("cannot claim with malformed event log: " + "; ".join(errors))
        active = fold_claim_events(events)
        now = _now()
        for existing in active.values():
            if existing.workspace != target.path:
                continue
            stale = existing.expires_at is not None and _parse_time(existing.expires_at) <= now
            if not (stale and holder_alive(existing.holder) is False):
                raise ClaimError(
                    f"workspace already claimed: holder={existing.holder.get('agent')} "
                    f"pid={existing.holder.get('pid')} claim={existing.claim_id}"
                )
        claim = _event(
            "claim",
            claim_id=uuid.uuid4().hex,
            workspace=target,
            agent=agent,
            holder_id=holder_id,
            operation=operation,
            ttl_seconds=ttl_seconds,
        )
        _append_event_unlocked(state, claim)
        return claim


def mutate_claim(
    claim_id: str,
    *,
    root: Path,
    state: Path,
    agent: str,
    holder_id: str,
    event_type: str,
    ttl_seconds: int | None = None,
) -> ClaimEvent:
    with _state_lock(state):
        events, errors = load_events(state)
        if errors:
            raise ClaimError("cannot mutate malformed event log: " + "; ".join(errors))
        active = fold_claim_events(events)
        existing = active.get(claim_id)
        if existing is None:
            raise ClaimError(f"active claim not found: {claim_id}")
        existing_holder_id = existing.holder.get("id", existing.holder.get("agent"))
        if existing.holder.get("agent") != agent or existing_holder_id != holder_id:
            raise ClaimError(f"claim is held by another process: {claim_id}")
        if event_type == "renew" and (ttl_seconds is None or ttl_seconds <= 0):
            raise ClaimError("ttl-seconds must be positive")
        target = Worktree(existing.workspace, existing.repo, "", existing.branch, False, False, ())
        event = _event(
            event_type,
            claim_id=claim_id,
            workspace=target,
            agent=agent,
            holder_id=holder_id,
            operation=existing.operation,
            ttl_seconds=ttl_seconds,
        )
        _append_event_unlocked(state, event)
        return event


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-coord")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    sub = parser.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status")
    status.add_argument("--format", choices=("json", "text"), default="json")
    claim = sub.add_parser("claim")
    claim.add_argument("workspace", type=Path)
    claim.add_argument("--agent", default=os.environ.get("AGENT_COORD_AGENT", "unknown"))
    claim.add_argument("--holder-id", default=os.environ.get("AGENT_COORD_HOLDER_ID"))
    claim.add_argument("--operation", default="edit")
    claim.add_argument("--ttl-seconds", type=int, default=3600)
    for name in ("release", "renew"):
        command = sub.add_parser(name)
        command.add_argument("claim_id")
        command.add_argument("--agent", default=os.environ.get("AGENT_COORD_AGENT", "unknown"))
        command.add_argument("--holder-id", default=os.environ.get("AGENT_COORD_HOLDER_ID"))
        if name == "renew":
            command.add_argument("--ttl-seconds", type=int, default=3600)
    return parser


def _print_status(report: dict[str, Any], output_format: str) -> None:
    if output_format == "json":
        print(json.dumps(report, indent=2, sort_keys=True))
        return
    summary = report["summary"]
    print("worktrees={worktrees} dirty={dirty} unclaimed={unclaimed} stale_claims={stale_claims} reclaimable={reclaimable_claims} claim_mismatches={claim_mismatches}".format(**summary))
    for item in report["worktrees"]:
        state = "dirty" if item["dirty"] else "clean"
        claim = ",".join(item["claims"]) or "unclaimed"
        print(f"{item['repo']} {item['branch'] or '(detached)'} {state} {claim} {item['path']}")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "status":
            worktrees = discover_worktrees(args.root)
            events, errors = load_events(args.state)
            report = build_status(worktrees, fold_claim_events(events))
            if errors:
                report["event_log_errors"] = errors
            _print_status(report, args.format)
            return 0 if not errors else 2
        if args.command == "claim":
            claim = claim_workspace(
                args.workspace,
                root=args.root,
                state=args.state,
                agent=args.agent,
                holder_id=args.holder_id or args.agent,
                operation=args.operation,
                ttl_seconds=args.ttl_seconds,
            )
        else:
            claim = mutate_claim(
                args.claim_id,
                root=args.root,
                state=args.state,
                agent=args.agent,
                holder_id=args.holder_id or args.agent,
                event_type=args.command,
                ttl_seconds=getattr(args, "ttl_seconds", None),
            )
        print(json.dumps(claim.to_dict(), indent=2, sort_keys=True))
        return 0
    except ClaimError as exc:
        print(json.dumps({"error": "claim_refused", "message": str(exc)}, sort_keys=True), file=sys.stderr)
        return 75


if __name__ == "__main__":
    raise SystemExit(main())
