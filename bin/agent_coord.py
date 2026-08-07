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
import selectors
import signal
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Iterable, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

DEFAULT_ROOT = Path.home() / "src" / "ops-worktrees"
DEFAULT_STATE = Path.home() / ".local" / "state" / "agent-coord" / "events.jsonl"
DEFAULT_ARTIFACT_ROOT = Path.home() / ".local" / "state" / "agent-coord" / "runs"
GIT_TIMEOUT_SECONDS = 10
MAX_ARTIFACT_BYTES = 1_000_000
MAX_STDERR_BYTES = 256_000


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


@dataclass(frozen=True)
class WorkerRun:
    run_id: str
    artifact_dir: Path
    claim_id: str
    exit_code: int
    timed_out: bool
    result_subtype: str | None
    session_id: str | None


_SENSITIVE_EVENT_KEYS = frozenset(
    {"content", "input", "message", "prompt", "result", "structured_output", "text"}
)


def _sanitize_event(value: Any, key: str | None = None) -> Any:
    if key is not None and key.lower() in _SENSITIVE_EVENT_KEYS:
        return "[content redacted]"
    if isinstance(value, dict):
        return {name: _sanitize_event(item, name) for name, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_event(item) for item in value]
    return value


def _write_bounded(path: Path, data: bytes, limit: int, prompt: str) -> bool:
    safe = data.replace(prompt.encode("utf-8", "replace"), b"[prompt redacted]")
    path.write_bytes(safe[:limit])
    return len(safe) > limit


def run_worker(
    workspace: Path,
    *,
    root: Path,
    state: Path,
    artifact_root: Path = DEFAULT_ARTIFACT_ROOT,
    prompt: str,
    agent: str,
    holder_id: str,
    operation: str,
    command: Sequence[str] | None = None,
    timeout_seconds: float = 900,
    max_turns: int = 10,
    model: str = "sonnet",
    resume: str | None = None,
    allowed_tools: Sequence[str] = (),
) -> WorkerRun:
    """Claim a workspace, supervise one headless Claude process, then release it."""
    if not prompt:
        raise ClaimError("prompt must not be empty")
    if timeout_seconds <= 0:
        raise ClaimError("timeout-seconds must be positive")
    if max_turns <= 0:
        raise ClaimError("max-turns must be positive")

    workspace = workspace.resolve()
    worktrees = discover_worktrees(root)
    target = next((item for item in worktrees if Path(item.path).resolve() == workspace), None)
    if target is None:
        raise ClaimError(f"workspace is not a registered worktree: {workspace}")
    claim = claim_workspace(
        workspace,
        root=root,
        state=state,
        agent=agent,
        holder_id=holder_id,
        operation=operation,
        ttl_seconds=max(60, int(timeout_seconds) + 300),
    )

    run_id = uuid.uuid4().hex
    artifact_dir = artifact_root / run_id
    artifact_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    stdout_path = artifact_dir / "stdout.ndjson"
    stderr_path = artifact_dir / "stderr.txt"
    binary = list(command or (os.environ.get("AGENT_COORD_CLAUDE_BIN", "claude-sub"), "--stdin"))
    argv = binary + ["--model", model, "--max-turns", str(max_turns), "--permission-mode", "dontAsk"]
    argv += ["--output-format", "stream-json", "--verbose", "--include-partial-messages"]
    for tool in allowed_tools:
        argv += ["--allowedTools", tool]
    if resume:
        argv += ["--resume", resume]

    started_at = _now().isoformat().replace("+00:00", "Z")
    result_event: dict[str, Any] | None = None
    stream_errors: list[str] = []
    stderr_data = bytearray()
    stdout_buffer = b""
    stdout_written = 0
    stdout_truncated = False
    timed_out = False
    return_code = 1
    process = None
    selector = None
    try:
        process = subprocess.Popen(
            argv,
            cwd=workspace,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            assert process.stdin is not None
            process.stdin.write(prompt.encode("utf-8"))
            process.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        assert process.stdout is not None and process.stderr is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        deadline = time.monotonic() + timeout_seconds
        with stdout_path.open("w", encoding="utf-8") as stdout_file:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0 and process.poll() is None:
                    timed_out = True
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                    break
                for key, _ in selector.select(max(0.05, min(remaining, 0.5))):
                    fd = key.fileobj if isinstance(key.fileobj, int) else key.fileobj.fileno()
                    chunk = os.read(fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    if key.data == "stderr":
                        remaining_stderr = MAX_STDERR_BYTES + 1 - len(stderr_data)
                        if remaining_stderr > 0:
                            stderr_data.extend(chunk[:remaining_stderr])
                        continue
                    stdout_buffer += chunk
                    while b"\n" in stdout_buffer:
                        raw_line, stdout_buffer = stdout_buffer.split(b"\n", 1)
                        try:
                            event = json.loads(raw_line)
                        except (TypeError, ValueError, json.JSONDecodeError) as exc:
                            stream_errors.append(str(exc))
                            continue
                        if not isinstance(event, dict):
                            stream_errors.append("stream event is not an object")
                            continue
                        if event.get("type") == "result":
                            result_event = {
                                key: event[key]
                                for key in ("subtype", "session_id", "is_error", "duration_ms", "num_turns", "usage")
                                if key in event
                            }
                        safe_line = json.dumps(_sanitize_event(event), sort_keys=True) + "\n"
                        if stdout_written < MAX_ARTIFACT_BYTES and stdout_written + len(safe_line) <= MAX_ARTIFACT_BYTES:
                            stdout_file.write(safe_line)
                            stdout_written += len(safe_line)
                            stdout_file.flush()
                        else:
                            stdout_truncated = True
            if stdout_buffer.strip():
                stream_errors.append("stream ended with an incomplete JSON line")
            selector.close()
            selector = None
        return_code = process.returncode if process.returncode is not None else process.wait()
    except OSError as exc:
        stream_errors.append(str(exc))
        return_code = 127
    finally:
        if selector is not None:
            selector.close()
        if process is not None:
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()
        stderr_truncated = _write_bounded(stderr_path, bytes(stderr_data), MAX_STDERR_BYTES, prompt)
        subtype = str(result_event.get("subtype")) if result_event and result_event.get("subtype") else None
        session_id = str(result_event.get("session_id")) if result_event and result_event.get("session_id") else None
        if timed_out:
            return_code = 124
        elif subtype == "error_max_turns" and return_code == 0:
            return_code = 75
        release_error: str | None = None
        try:
            mutate_claim(
                claim.claim_id,
                root=root,
                state=state,
                agent=agent,
                holder_id=holder_id,
                event_type="release",
            )
        except ClaimError as exc:
            release_error = str(exc)
            return_code = 75
        manifest = {
            "run_id": run_id,
            "claim_id": claim.claim_id,
            "workspace": str(workspace),
            "operation": operation,
            "argv": argv,
            "prompt_persisted": False,
            "started_at": started_at,
            "finished_at": _now().isoformat().replace("+00:00", "Z"),
            "exit_code": return_code,
            "timed_out": timed_out,
            "result_subtype": subtype,
            "session_id": session_id,
            "stream_errors": stream_errors,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "claim_released": release_error is None,
        }
        if release_error:
            manifest["release_error"] = release_error
        (artifact_dir / "run.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return WorkerRun(run_id, artifact_dir, claim.claim_id, return_code, timed_out, subtype, session_id)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-coord")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    sub = parser.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status")
    status.add_argument("--format", choices=("json", "text"), default="json")
    run = sub.add_parser("run")
    run.add_argument("workspace", type=Path)
    run.add_argument("--prompt-file", type=Path)
    run.add_argument("--artifact-root", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    run.add_argument("--agent", default=os.environ.get("AGENT_COORD_AGENT", "hermes"))
    run.add_argument("--holder-id", default=os.environ.get("AGENT_COORD_HOLDER_ID"))
    run.add_argument("--operation", default="edit")
    run.add_argument("--timeout-seconds", type=float, default=900)
    run.add_argument("--max-turns", type=int, default=10)
    run.add_argument("--model", default="sonnet")
    run.add_argument("--resume")
    run.add_argument("--allowed-tool", action="append", default=[])
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
        elif args.command == "run":
            prompt = args.prompt_file.read_text(encoding="utf-8") if args.prompt_file else sys.stdin.read()
            result = run_worker(
                args.workspace,
                root=args.root,
                state=args.state,
                artifact_root=args.artifact_root,
                prompt=prompt,
                agent=args.agent,
                holder_id=args.holder_id or args.agent,
                operation=args.operation,
                timeout_seconds=args.timeout_seconds,
                max_turns=args.max_turns,
                model=args.model,
                resume=args.resume,
                allowed_tools=args.allowed_tool,
            )
            print(
                json.dumps(
                    {
                        "run_id": result.run_id,
                        "artifact_dir": str(result.artifact_dir),
                        "claim_id": result.claim_id,
                        "exit_code": result.exit_code,
                        "timed_out": result.timed_out,
                        "result_subtype": result.result_subtype,
                        "session_id": result.session_id,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return result.exit_code
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
