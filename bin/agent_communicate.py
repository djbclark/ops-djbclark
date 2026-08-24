"""Reproducible, bounded communication with installed AI CLIs.

Prompts are accepted only from stdin or a file, never as command-line text.
Adapters either use native stdin/private-file transport or report a typed
unsupported result. Raw evidence and optional response capture are bounded.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence

MAX_OUTPUT_BYTES = 1_000_000
MAX_ERROR_BYTES = 256_000
TRANSIENT = re.compile(
    r"(?:rate.?limit|overloaded|temporar(?:y|ily)|connection (?:reset|refused)|timed? out|timeout|try again)",
    re.IGNORECASE,
)
AUTH = re.compile(r"(?:not authenticated|login required|unauthorized|forbidden|invalid api key|authentication)", re.I)


class CommunicationError(RuntimeError):
    """Invalid request or unsupported communication contract."""


@dataclass(frozen=True)
class Adapter:
    name: str
    executables: tuple[str, ...]
    transport: str
    read_only: bool
    persistent_session: bool
    version_args: tuple[str, ...] = ("--version",)
    note: str = ""


@dataclass(frozen=True)
class CommunicationRun:
    run_id: str
    client: str
    status: str
    exit_code: int
    artifact_dir: Path
    attempts: int
    timed_out: bool
    response_captured: bool


ADAPTERS: dict[str, Adapter] = {
    "claude": Adapter("claude", ("claude-sub",), "stdin", True, False),
    "codex": Adapter("codex", ("codex",), "stdin", True, False),
    "goose": Adapter("goose", ("goose",), "stdin", True, False),
    "grok": Adapter("grok", ("grok",), "file", True, False),
    "aider": Adapter("aider", ("aider",), "file", True, False),
    "prime": Adapter(
        "prime",
        ("prime-agent",),
        "file_context",
        True,
        False,
        note="uses Prime Agent @file context; live validation required",
    ),
    "opencode": Adapter(
        "opencode",
        ("opencode",),
        "file_context",
        True,
        True,
        note="OpenCode has no no-session flag; conversation metadata may persist",
    ),
    "hermes": Adapter("hermes", ("hermes",), "unsupported", False, True, note="no native stdin/file prompt option"),
    "gemini": Adapter("gemini", ("gemini",), "unsupported", False, True, note="--print accepts argv text only"),
    "agy": Adapter("agy", ("agy",), "unsupported", False, True, note="--print accepts argv text only"),
    "cursor": Adapter(
        "cursor",
        ("/opt/homebrew/bin/cursor-agent", "cursor-agent"),
        "unsupported",
        False,
        True,
        note="prompt is positional; no prompt-file/stdin contract exposed",
    ),
}


def _resolve(adapter: Adapter) -> str | None:
    for candidate in adapter.executables:
        if "/" in candidate:
            path = Path(candidate)
            if path.is_file() and os.access(path, os.X_OK):
                return str(path)
        else:
            found = shutil.which(candidate)
            if found:
                return found
    return None


def client_inventory() -> list[dict[str, Any]]:
    result = []
    for name, adapter in sorted(ADAPTERS.items()):
        executable = _resolve(adapter)
        version = None
        version_error = None
        if executable:
            try:
                probe = subprocess.run(
                    [executable, *adapter.version_args],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                text = (probe.stdout or probe.stderr).strip().splitlines()
                version = text[0][:300] if text else None
                if probe.returncode and not version:
                    version_error = f"version probe exited {probe.returncode}"
            except (OSError, subprocess.TimeoutExpired) as exc:
                version_error = str(exc)
        result.append(
            {
                **asdict(adapter),
                "installed": executable is not None,
                "executable": executable,
                "version": version,
                "version_error": version_error,
            }
        )
    return result


def _argv(adapter: Adapter, executable: str, prompt_path: Path, cwd: Path) -> tuple[list[str], bytes | None]:
    if adapter.name == "claude":
        return (
            [
                executable,
                "--stdin",
                "-p",
                "--safe-mode",
                "--no-session-persistence",
                "--disable-slash-commands",
                "--tools",
                "",
                "--permission-mode",
                "plan",
                "--output-format",
                "json",
            ],
            prompt_path.read_bytes(),
        )
    if adapter.name == "codex":
        return (
            [
                executable,
                "exec",
                "--sandbox",
                "read-only",
                "--ephemeral",
                "--skip-git-repo-check",
                "--ignore-rules",
                "-C",
                str(cwd),
                "--json",
                "-",
            ],
            prompt_path.read_bytes(),
        )
    if adapter.name == "goose":
        return (
            [
                executable,
                "run",
                "--instructions",
                "-",
                "--no-session",
                "--max-turns",
                "1",
                "--quiet",
                "--no-profile",
                "--output-format",
                "json",
            ],
            prompt_path.read_bytes(),
        )
    if adapter.name == "grok":
        return (
            [
                executable,
                "--prompt-file",
                str(prompt_path),
                "--permission-mode",
                "plan",
                "--tools",
                "",
                "--no-memory",
                "--no-subagents",
                "--disable-web-search",
                "--output-format",
                "json",
                "--verbatim",
                "--max-turns",
                "1",
                "--cwd",
                str(cwd),
            ],
            None,
        )
    if adapter.name == "aider":
        return (
            [
                executable,
                "--message-file",
                str(prompt_path),
                "--dry-run",
                "--no-git",
                "--no-auto-commits",
                "--input-history-file",
                "/dev/null",
                "--chat-history-file",
                "/dev/null",
                "--llm-history-file",
                "/dev/null",
                "--env-file",
                "/dev/null",
                "--no-pretty",
                "--no-stream",
                "--analytics-disable",
                "--no-check-update",
                "--disable-playwright",
            ],
            None,
        )
    if adapter.name == "prime":
        return (
            [
                executable,
                "-p",
                "--mode",
                "json",
                "--no-session",
                "--no-tools",
                "--no-builtin-tools",
                "--no-extensions",
                "--no-skills",
                "--no-prompt-templates",
                "--no-context-files",
                "--cwd",
                str(cwd),
                f"@{prompt_path}",
                "--",
                "Answer the attached request exactly. Do not modify state or call tools.",
            ],
            None,
        )
    if adapter.name == "opencode":
        return (
            [
                executable,
                "run",
                "Answer the request in the attached file exactly. Do not modify state.",
                "--file",
                str(prompt_path),
                "--agent",
                "plan",
                "--pure",
                "--dir",
                str(cwd),
                "--format",
                "json",
            ],
            None,
        )
    raise CommunicationError(f"client {adapter.name} has no safe prompt transport: {adapter.note}")


def _bounded_append(buffer: bytearray, chunk: bytes, limit: int) -> bool:
    remaining = limit + 1 - len(buffer)
    if remaining > 0:
        buffer.extend(chunk[:remaining])
    return len(buffer) > limit


def _terminate(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def _execute(argv: Sequence[str], stdin_data: bytes | None, cwd: Path, timeout: float) -> tuple[int, bytes, bytes, bool]:
    process = subprocess.Popen(
        list(argv),
        cwd=cwd,
        stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    if stdin_data is not None:
        assert process.stdin is not None
        try:
            process.stdin.write(stdin_data)
            process.stdin.close()
        except (BrokenPipeError, OSError):
            pass
    assert process.stdout is not None and process.stderr is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    out = bytearray()
    err = bytearray()
    deadline = time.monotonic() + timeout
    timed_out = False
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                _terminate(process)
                break
            for key, _ in selector.select(max(0.05, min(remaining, 0.5))):
                fd = key.fileobj if isinstance(key.fileobj, int) else key.fileobj.fileno()
                chunk = os.read(fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                elif key.data == "stdout":
                    _bounded_append(out, chunk, MAX_OUTPUT_BYTES)
                else:
                    _bounded_append(err, chunk, MAX_ERROR_BYTES)
    finally:
        selector.close()
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()
    code = 124 if timed_out else process.wait()
    return code, bytes(out[:MAX_OUTPUT_BYTES]), bytes(err[:MAX_ERROR_BYTES]), timed_out


def _classify(code: int, stdout: bytes, stderr: bytes, timed_out: bool) -> str:
    text = (stdout + b"\n" + stderr).decode("utf-8", "replace")
    if timed_out:
        return "timeout"
    if code == 127:
        return "launch_error"
    if code == 0:
        return "success"
    if AUTH.search(text):
        return "auth_error"
    if TRANSIENT.search(text):
        return "transient_error"
    return "client_error"


def communicate(
    client: str,
    *,
    prompt: str,
    cwd: Path,
    artifact_root: Path,
    timeout_seconds: float = 300,
    max_retries: int = 0,
    capture_response: bool = False,
) -> CommunicationRun:
    if not prompt.strip():
        raise CommunicationError("prompt must not be empty")
    if timeout_seconds <= 0 or max_retries < 0:
        raise CommunicationError("invalid timeout or retry count")
    adapter = ADAPTERS.get(client)
    if adapter is None:
        raise CommunicationError(f"unknown client: {client}")
    executable = _resolve(adapter)
    run_id = uuid.uuid4().hex
    artifact_dir = artifact_root / run_id
    artifact_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    prompt_path = artifact_dir / "prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")
    prompt_path.chmod(0o600)
    started = time.time()
    attempts = 0
    status = "unsupported_transport"
    code = 64 if executable else 127
    timed_out = False
    stdout = b""
    stderr = b""
    argv: list[str] = []
    version = None
    try:
        if not executable:
            status = "not_installed"
        elif adapter.transport == "unsupported":
            status = "unsupported_transport"
        else:
            try:
                probe = subprocess.run(
                    [executable, *adapter.version_args], capture_output=True, text=True, timeout=10, check=False
                )
                version = ((probe.stdout or probe.stderr).strip().splitlines() or [None])[0]
            except (OSError, subprocess.TimeoutExpired):
                version = None
            argv, stdin_data = _argv(adapter, executable, prompt_path, cwd.resolve())
            while True:
                attempts += 1
                code, stdout, stderr, timed_out = _execute(argv, stdin_data, cwd.resolve(), timeout_seconds)
                status = _classify(code, stdout, stderr, timed_out)
                if status != "transient_error" or attempts > max_retries:
                    break
                time.sleep(min(2**attempts, 8))
        if capture_response and stdout:
            (artifact_dir / "response.txt").write_bytes(stdout[:MAX_OUTPUT_BYTES])
        if stderr:
            (artifact_dir / "stderr.txt").write_bytes(stderr[:MAX_ERROR_BYTES])
    finally:
        prompt_path.unlink(missing_ok=True)
        manifest = {
            "run_id": run_id,
            "client": client,
            "adapter": asdict(adapter),
            "executable": executable,
            "version": version,
            "argv": argv,
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "prompt_bytes": len(prompt.encode()),
            "prompt_in_argv": prompt in argv,
            "prompt_persisted": False,
            "capture_response": capture_response,
            "status": status,
            "exit_code": code,
            "attempts": attempts,
            "timed_out": timed_out,
            "stdout_bytes": len(stdout),
            "stderr_bytes": len(stderr),
            "started_unix": started,
            "finished_unix": time.time(),
        }
        (artifact_dir / "run.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return CommunicationRun(run_id, client, status, code, artifact_dir, attempts, timed_out, capture_response)
