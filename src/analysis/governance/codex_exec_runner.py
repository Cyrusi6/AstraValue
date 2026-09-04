from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable

from .canonical import canonical_json_bytes
from .models import (
    BudgetUsage,
    ResearchResultBundle,
    ResearchTask,
    ResearchTaskStatus,
)
from .research_gate import (
    ResearchIntegrityError,
    research_result_bundle_hash,
    research_task_hash,
)


SAFE_ENV_NAMES: frozenset[str] = frozenset(
    {
        "APPDATA",
        "CODEX_HOME",
        "COMSPEC",
        "HOME",
        "LANG",
        "LC_ALL",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "WINDIR",
    }
)

_RESEARCH_RESULT_SCHEMA_BYTES = canonical_json_bytes(
    ResearchResultBundle.model_json_schema()
)
RESEARCH_RESULT_SCHEMA_HASH = hashlib.sha256(
    _RESEARCH_RESULT_SCHEMA_BYTES
).hexdigest()


class CodexExecError(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class CodexExecCapabilities:
    executable: str
    version: str
    search: bool
    read_only_sandbox: bool
    approval_policy: bool
    working_directory: bool
    ephemeral: bool
    output_schema: bool
    jsonl: bool
    ignore_user_config: bool
    ignore_rules: bool
    skip_git_repo_check: bool

    @property
    def supported(self) -> bool:
        return all(
            (
                self.search,
                self.read_only_sandbox,
                self.approval_policy,
                self.working_directory,
                self.ephemeral,
                self.output_schema,
                self.jsonl,
                self.ignore_user_config,
                self.ignore_rules,
                self.skip_git_repo_check,
            )
        )


@dataclass(frozen=True, slots=True)
class _StreamResult:
    stdout: bytes
    stderr: bytes
    events: tuple[Mapping[str, Any], ...]
    network_requests: int
    output_bytes: int
    elapsed_ms: int
    violation: str | None
    returncode: int


def filtered_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    source = os.environ if source is None else source
    return {
        name: value
        for name, value in source.items()
        if name.upper() in SAFE_ENV_NAMES
    }


def build_codex_argv(
    executable: str,
    *,
    task_root: Path,
    output_schema_path: Path,
) -> list[str]:
    return [
        executable,
        "--search",
        "--sandbox",
        "read-only",
        "--ask-for-approval",
        "never",
        "--cd",
        str(task_root),
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--skip-git-repo-check",
        "--output-schema",
        str(output_schema_path),
        "--json",
        "-",
    ]


def build_research_prompt(task: ResearchTask) -> bytes:
    if task.recursion_depth != 1:
        raise CodexExecError("recursion_rejected", "v1 child tasks cannot recurse")
    payload = {
        "instruction": (
            "执行最小治理证据研究任务。只返回给定 JSON Schema；不要创建子任务；"
            "正式原文仅作为 authoritative_source_candidates，任何叙述不得写成权威事实。"
        ),
        "research_task": task.model_dump(mode="python"),
    }
    return canonical_json_bytes(payload)


def _run_probe(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    timeout: int = 15,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        input="",
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        shell=False,
        env=dict(env),
        creationflags=0x08000000 if os.name == "nt" else 0,
    )


def probe_codex_capabilities(
    executable: str = "codex",
    *,
    env: Mapping[str, str] | None = None,
    command_runner: Callable[..., subprocess.CompletedProcess[str]] = _run_probe,
) -> CodexExecCapabilities:
    clean_env = filtered_environment(env)
    root = command_runner([executable, "--help"], env=clean_env)
    exec_help = command_runner([executable, "exec", "--help"], env=clean_env)
    version_result = command_runner([executable, "--version"], env=clean_env)
    if root.returncode != 0 or exec_help.returncode != 0:
        raise CodexExecError("capability_probe_failed", "Codex help probe failed")
    root_text = f"{root.stdout}\n{root.stderr}"
    exec_text = f"{exec_help.stdout}\n{exec_help.stderr}"
    capabilities = CodexExecCapabilities(
        executable=executable,
        version=(version_result.stdout or version_result.stderr).strip() or "unknown",
        search="--search" in root_text,
        read_only_sandbox="--sandbox" in root_text and "read-only" in root_text,
        approval_policy="--ask-for-approval" in root_text,
        working_directory="--cd" in root_text,
        ephemeral="--ephemeral" in exec_text,
        output_schema="--output-schema" in exec_text,
        jsonl="--json" in exec_text,
        ignore_user_config="--ignore-user-config" in exec_text,
        ignore_rules="--ignore-rules" in exec_text,
        skip_git_repo_check="--skip-git-repo-check" in exec_text,
    )
    if not capabilities.supported:
        raise CodexExecError(
            "missing_required_capability",
            "installed Codex CLI lacks a required governed-run capability",
        )
    return capabilities


def _event_nodes(value: Any, *, depth: int = 0) -> tuple[Mapping[str, Any], ...]:
    """Return only the bounded JSONL envelope nodes used by the Codex protocol."""

    if depth > 6:
        return ()
    if isinstance(value, Mapping):
        nodes: list[Mapping[str, Any]] = [value]
        for key in (
            "item",
            "result",
            "output",
            "message",
            "structured_output",
            "content",
        ):
            if key in value:
                nodes.extend(_event_nodes(value[key], depth=depth + 1))
        return tuple(nodes)
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray, memoryview)
    ):
        nodes = []
        for item in value:
            nodes.extend(_event_nodes(item, depth=depth + 1))
        return tuple(nodes)
    return ()


def _event_labels(event: Mapping[str, Any]) -> tuple[str, ...]:
    labels: list[str] = []
    for node in _event_nodes(event):
        for key in ("type", "event", "tool_name", "name"):
            label = node.get(key)
            if isinstance(label, str):
                labels.append(label.strip().lower())
    return tuple(labels)


def _is_network_event(event: Mapping[str, Any]) -> bool:
    root_type = str(event.get("type", "")).strip().lower()
    if root_type in {"item.started", "item.updated"}:
        return False
    return any(
        any(
            marker in label
            for marker in (
                "web_search",
                "network_request",
                "browser_request",
                "http_request",
            )
        )
        for label in _event_labels(event)
    )


def _tool_event_violation(event: Mapping[str, Any]) -> str | None:
    """Fail closed if the child escapes the minimal web-search-only tool surface."""

    for label in _event_labels(event):
        if any(
            marker in label
            for marker in (
                "spawn_agent",
                "create_thread",
                "fork_thread",
                "handoff_thread",
                "subagent",
            )
        ):
            return "recursive_agent_event"
        if label in {
            "command_execution",
            "file_change",
            "mcp_tool_call",
            "computer_use",
        }:
            return "unapproved_tool_event"
    return None


def _terminate(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    pid = getattr(process, "pid", None)
    terminated_tree = False
    if isinstance(pid, int) and pid > 0:
        if os.name == "nt":
            try:
                result = subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    check=False,
                    shell=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                terminated_tree = result.returncode == 0
            except OSError:
                terminated_tree = False
        else:
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
                terminated_tree = True
            except (OSError, ProcessLookupError):
                terminated_tree = False
    if not terminated_tree:
        process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def _supervise(
    process: subprocess.Popen[bytes],
    *,
    stdin_payload: bytes,
    wall_clock_seconds: float,
    max_stream_bytes: int,
    max_network_requests: int,
) -> _StreamResult:
    if process.stdin is None or process.stdout is None or process.stderr is None:
        _terminate(process)
        raise CodexExecError("launcher_contract", "Codex process pipes are required")

    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    events: list[Mapping[str, Any]] = []
    state = {"total": 0, "network": 0, "violation": None}
    lock = threading.Lock()

    def consume(stream: BinaryIO, chunks: list[bytes], name: str) -> None:
        pending = bytearray()

        def parse_line(raw_line: bytes) -> None:
            if not raw_line.strip():
                return
            try:
                parsed = json.loads(raw_line.decode("utf-8", errors="strict"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                with lock:
                    if state["violation"] is None:
                        state["violation"] = "invalid_jsonl"
                return
            if not isinstance(parsed, Mapping):
                with lock:
                    if state["violation"] is None:
                        state["violation"] = "invalid_jsonl"
                return
            violation = _tool_event_violation(parsed)
            with lock:
                events.append(dict(parsed))
                if violation is not None and state["violation"] is None:
                    state["violation"] = violation
                if _is_network_event(parsed):
                    state["network"] += 1
                    if (
                        state["network"] > max_network_requests
                        and state["violation"] is None
                    ):
                        state["violation"] = "network_request_budget"

        while True:
            chunk = stream.read(4096)
            if not chunk:
                break
            with lock:
                remaining = max(0, max_stream_bytes - int(state["total"]))
                retained = chunk[:remaining]
                if retained:
                    chunks.append(retained)
                state["total"] += len(chunk)
                if state["total"] > max_stream_bytes and state["violation"] is None:
                    state["violation"] = "stream_size_limit"
            if name == "stdout":
                pending.extend(retained)
                while b"\n" in pending:
                    raw_line, _, remainder = pending.partition(b"\n")
                    pending = bytearray(remainder)
                    parse_line(raw_line)
            with lock:
                if state["violation"] is not None:
                    break
        if name == "stdout" and pending:
            parse_line(bytes(pending))

    stdout_thread = threading.Thread(
        target=consume, args=(process.stdout, stdout_chunks, "stdout"), daemon=True
    )
    stderr_thread = threading.Thread(
        target=consume, args=(process.stderr, stderr_chunks, "stderr"), daemon=True
    )
    started = time.monotonic()
    stdout_thread.start()
    stderr_thread.start()
    try:
        process.stdin.write(stdin_payload)
        process.stdin.close()
    except (BrokenPipeError, OSError):
        pass

    while process.poll() is None:
        elapsed = time.monotonic() - started
        with lock:
            violation = state["violation"]
        if violation is not None:
            _terminate(process)
            break
        if elapsed > wall_clock_seconds:
            with lock:
                state["violation"] = "wall_clock_budget"
            _terminate(process)
            break
        time.sleep(0.01)
    stdout_thread.join(timeout=2)
    stderr_thread.join(timeout=2)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    with lock:
        return _StreamResult(
            stdout=b"".join(stdout_chunks),
            stderr=b"".join(stderr_chunks),
            events=tuple(events),
            network_requests=int(state["network"]),
            output_bytes=int(state["total"]),
            elapsed_ms=elapsed_ms,
            violation=state["violation"],
            returncode=process.returncode if process.returncode is not None else -1,
        )


def _extract_final_payload(events: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    def visit(candidate: Any, *, depth: int = 0) -> Mapping[str, Any] | None:
        if depth > 8:
            return None
        if isinstance(candidate, str):
            try:
                candidate = json.loads(candidate)
            except json.JSONDecodeError:
                return None
        if isinstance(candidate, Mapping):
            if candidate.get("kind") == "research_result_bundle":
                return candidate
            for key in (
                "structured_output",
                "item",
                "result",
                "output",
                "message",
                "content",
                "text",
            ):
                if key in candidate:
                    found = visit(candidate[key], depth=depth + 1)
                    if found is not None:
                        return found
        elif isinstance(candidate, Sequence) and not isinstance(
            candidate, (str, bytes, bytearray, memoryview)
        ):
            for item in reversed(candidate):
                found = visit(item, depth=depth + 1)
                if found is not None:
                    return found
        return None

    for event in reversed(events):
        found = visit(event)
        if found is not None:
            return found
    raise ResearchIntegrityError(
        "result_schema_invalid", "Codex JSONL contains no research result bundle"
    )


def _terminal_bundle(
    task: ResearchTask,
    *,
    status: ResearchTaskStatus,
    reason: str,
    usage: BudgetUsage,
    created_at: datetime,
) -> ResearchResultBundle:
    provisional = ResearchResultBundle(
        research_result_bundle_id=f"govresearchbundle:{task.research_task_id.split(':', 1)[1]}:{status.value}",
        research_task_id=task.research_task_id,
        task_status=status,
        budget_used=usage,
        failure_reason=reason,
        created_at=created_at,
        canonical_hash="0" * 64,
    )
    digest = research_result_bundle_hash(provisional)
    return ResearchResultBundle.model_validate(
        {**provisional.model_dump(mode="python"), "canonical_hash": digest},
        strict=True,
    )


class CodexExecRunner:
    """Governed fresh-context Codex broker using argv/stdin and JSONL only."""

    def __init__(
        self,
        *,
        executable: str = "codex",
        env: Mapping[str, str] | None = None,
        process_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        capability_probe: Callable[..., CodexExecCapabilities] = probe_codex_capabilities,
        max_parallelism: int = 2,
        staging_parent: Path | None = None,
    ) -> None:
        if max_parallelism < 1:
            raise ValueError("max_parallelism must be positive")
        self.executable = executable
        self.env = filtered_environment(env)
        self._process_factory = process_factory
        self._capability_probe = capability_probe
        self._semaphore = threading.BoundedSemaphore(max_parallelism)
        self._max_parallelism = max_parallelism
        self._session_limit_lock = threading.Lock()
        self._session_limits: dict[str, tuple[int, threading.BoundedSemaphore]] = {}
        self._staging_parent = staging_parent
        self._capabilities: CodexExecCapabilities | None = None

    def probe(self) -> CodexExecCapabilities:
        if self._capabilities is None:
            self._capabilities = self._capability_probe(
                self.executable,
                env=self.env,
            )
        return self._capabilities

    def submit(self, task: ResearchTask) -> ResearchResultBundle:
        submitted_at = time.monotonic()
        if research_task_hash(task) != task.canonical_hash:
            raise ResearchIntegrityError("task_hash_mismatch", "research task hash mismatch")
        if task.recursion_depth != 1:
            raise CodexExecError("recursion_rejected", "v1 recursion depth is fixed at one")
        if task.budget.max_parallelism > self._max_parallelism:
            raise CodexExecError(
                "parallelism_not_supported", "task parallelism exceeds runner limit"
            )
        if (
            task.budget.max_rounds == 0
            or task.budget.max_child_tasks == 0
            or task.budget.max_network_requests == 0
        ):
            return _terminal_bundle(
                task,
                status=ResearchTaskStatus.BUDGET_EXHAUSTED,
                reason="budget_unavailable",
                usage=BudgetUsage(
                    rounds=0,
                    child_tasks=0,
                    network_requests=0,
                    wall_clock_milliseconds=0,
                    output_bytes=0,
                ),
                created_at=datetime.now(timezone.utc),
            )
        self.probe()
        with self._session_limit_lock:
            configured = self._session_limits.get(task.parent_session_id)
            if configured is None:
                configured = (
                    task.budget.max_parallelism,
                    threading.BoundedSemaphore(task.budget.max_parallelism),
                )
                self._session_limits[task.parent_session_id] = configured
            elif configured[0] != task.budget.max_parallelism:
                raise CodexExecError(
                    "parallelism_contract_changed",
                    "one session must use one immutable parallelism budget",
                )
            session_semaphore = configured[1]

        def remaining_seconds() -> float:
            return max(
                0.0,
                task.budget.wall_clock_seconds - (time.monotonic() - submitted_at),
            )

        session_acquired = session_semaphore.acquire(timeout=remaining_seconds())
        if not session_acquired:
            return _terminal_bundle(
                task,
                status=ResearchTaskStatus.BUDGET_EXHAUSTED,
                reason="parallelism_budget",
                usage=BudgetUsage(
                    rounds=0,
                    child_tasks=0,
                    network_requests=0,
                    wall_clock_milliseconds=int(
                        (time.monotonic() - submitted_at) * 1000
                    ),
                    output_bytes=0,
                ),
                created_at=datetime.now(timezone.utc),
            )
        global_acquired = False
        try:
            global_acquired = self._semaphore.acquire(timeout=remaining_seconds())
            if not global_acquired:
                return _terminal_bundle(
                    task,
                    status=ResearchTaskStatus.BUDGET_EXHAUSTED,
                    reason="parallelism_budget",
                    usage=BudgetUsage(
                        rounds=0,
                        child_tasks=0,
                        network_requests=0,
                        wall_clock_milliseconds=int(
                            (time.monotonic() - submitted_at) * 1000
                        ),
                        output_bytes=0,
                    ),
                    created_at=datetime.now(timezone.utc),
                )
            return self._execute(task, submitted_at=submitted_at)
        finally:
            if global_acquired:
                self._semaphore.release()
            session_semaphore.release()

    def _execute(
        self,
        task: ResearchTask,
        *,
        submitted_at: float,
    ) -> ResearchResultBundle:
        parent = None if self._staging_parent is None else str(self._staging_parent)
        with tempfile.TemporaryDirectory(
            prefix="governance-research-", dir=parent
        ) as directory:
            root = Path(directory).resolve()
            schema_path = root / "research-result.schema.json"
            task_path = root / "research-task.json"
            if (
                task.result_schema_name != ResearchResultBundle.schema_name
                or task.result_schema_version != "1.0.0"
                or task.result_schema_hash != RESEARCH_RESULT_SCHEMA_HASH
            ):
                raise ResearchIntegrityError(
                    "result_schema_binding_mismatch",
                    "research task is not bound to the installed output schema",
                )
            schema_path.write_bytes(_RESEARCH_RESULT_SCHEMA_BYTES)
            task_path.write_bytes(canonical_json_bytes(task.model_dump(mode="python")))
            argv = build_codex_argv(
                self.executable,
                task_root=root,
                output_schema_path=schema_path,
            )
            process_kwargs: dict[str, Any] = {
                "stdin": subprocess.PIPE,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "cwd": str(root),
                "env": self.env,
                "shell": False,
            }
            if os.name == "nt":
                process_kwargs["creationflags"] = (
                    getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                )
            else:
                process_kwargs["start_new_session"] = True
            process = self._process_factory(argv, **process_kwargs)
            remaining = task.budget.wall_clock_seconds - (
                time.monotonic() - submitted_at
            )
            if remaining <= 0:
                _terminate(process)
                return _terminal_bundle(
                    task,
                    status=ResearchTaskStatus.BUDGET_EXHAUSTED,
                    reason="wall_clock_budget",
                    usage=BudgetUsage(
                        rounds=1,
                        child_tasks=1,
                        network_requests=0,
                        wall_clock_milliseconds=int(
                            (time.monotonic() - submitted_at) * 1000
                        ),
                        output_bytes=0,
                    ),
                    created_at=datetime.now(timezone.utc),
                )
            result = _supervise(
                process,
                stdin_payload=build_research_prompt(task),
                wall_clock_seconds=remaining,
                max_stream_bytes=task.budget.max_output_bytes,
                max_network_requests=task.budget.max_network_requests,
            )
        usage = BudgetUsage(
            rounds=1,
            child_tasks=1,
            network_requests=result.network_requests,
            wall_clock_milliseconds=int((time.monotonic() - submitted_at) * 1000),
            output_bytes=result.output_bytes,
        )
        now = datetime.now(timezone.utc)
        if result.violation is not None:
            budget_violations = {
                "network_request_budget",
                "stream_size_limit",
                "wall_clock_budget",
            }
            return _terminal_bundle(
                task,
                status=(
                    ResearchTaskStatus.BUDGET_EXHAUSTED
                    if result.violation in budget_violations
                    else ResearchTaskStatus.FAILED
                ),
                reason=result.violation,
                usage=usage,
                created_at=now,
            )
        if result.returncode != 0:
            return _terminal_bundle(
                task,
                status=ResearchTaskStatus.FAILED,
                reason="codex_process_failed",
                usage=usage,
                created_at=now,
            )
        payload = _extract_final_payload(result.events)
        try:
            # Validate in JSON mode: tuples/enums/datetimes necessarily arrive as
            # JSON arrays/strings even though Python-mode construction is strict.
            bundle = ResearchResultBundle.model_validate_json(
                canonical_json_bytes(payload), strict=True
            )
        except Exception as exc:
            raise ResearchIntegrityError(
                "result_schema_invalid", "Codex result failed schema validation"
            ) from exc
        if bundle.research_task_id != task.research_task_id:
            raise ResearchIntegrityError(
                "task_binding_mismatch", "Codex result is bound to another task"
            )
        if bundle.canonical_hash != research_result_bundle_hash(bundle):
            raise ResearchIntegrityError(
                "bundle_hash_mismatch", "Codex result canonical hash mismatch"
            )
        # Child-reported usage is untrusted.  The process supervisor is the
        # authority and the parent reseals the immutable result accordingly.
        observed = bundle.model_copy(
            update={"budget_used": usage, "canonical_hash": "0" * 64}
        )
        return observed.model_copy(
            update={"canonical_hash": research_result_bundle_hash(observed)}
        )


__all__ = [
    "CodexExecCapabilities",
    "CodexExecError",
    "CodexExecRunner",
    "RESEARCH_RESULT_SCHEMA_HASH",
    "SAFE_ENV_NAMES",
    "build_codex_argv",
    "build_research_prompt",
    "filtered_environment",
    "probe_codex_capabilities",
]
