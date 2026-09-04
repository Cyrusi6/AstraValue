from __future__ import annotations

import io
import json
import os
import subprocess
from pathlib import Path

import pytest

from analysis.governance.canonical import canonical_json_bytes
from analysis.governance.codex_exec_runner import (
    CodexExecCapabilities,
    CodexExecRunner,
    RESEARCH_RESULT_SCHEMA_HASH,
    _supervise,
    _extract_final_payload,
    _is_network_event,
    build_codex_argv,
    build_research_prompt,
    filtered_environment,
    probe_codex_capabilities,
)
from analysis.governance.models import (
    BudgetUsage,
    GovernancePerspective,
    ResearchResultBundle,
    ResearchTaskStatus,
    SourceRole,
)
from analysis.governance.research_gate import (
    create_research_task,
    seal_research_result_bundle,
)

from .codex_test_support import NOW, make_budget


def _task():
    return create_research_task(
        parent_session_id="govsession:exec",
        company_id="cn-600519",
        question_ids=("GOV.Q09.REGULATORY_DISCLOSURE",),
        gap_ids=("govgap:exec",),
        question="find formal regulatory material",
        state_at=NOW,
        known_at=NOW,
        perspective=GovernancePerspective.STRICT,
        known_evidence_ids=(),
        allowed_source_roles=(SourceRole.REGULATOR_EXCHANGE,),
        budget=make_budget(),
        result_schema_hash=RESEARCH_RESULT_SCHEMA_HASH,
        created_at=NOW,
    )


def _capabilities() -> CodexExecCapabilities:
    return CodexExecCapabilities(
        executable="codex",
        version="codex-cli test",
        search=True,
        read_only_sandbox=True,
        approval_policy=True,
        working_directory=True,
        ephemeral=True,
        output_schema=True,
        jsonl=True,
        ignore_user_config=True,
        ignore_rules=True,
        skip_git_repo_check=True,
    )


class _CaptureStdin(io.BytesIO):
    def close(self):
        self.flush()


class _Process:
    def __init__(self, stdout: bytes, stderr: bytes = b"", returncode: int = 0):
        self.stdin = _CaptureStdin()
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.returncode = returncode
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


class _RunningProcess(_Process):
    def __init__(self):
        super().__init__(b"")
        self.returncode = None

    def poll(self):
        return self.returncode


def test_capability_probe_requires_all_governed_flags() -> None:
    def command(argv, *, env):
        if argv[-1] == "--version":
            return subprocess.CompletedProcess(argv, 0, "codex-cli 1.2.3", "")
        if "exec" in argv:
            return subprocess.CompletedProcess(
                argv,
                0,
                (
                    "--ephemeral --ignore-user-config --ignore-rules "
                    "--skip-git-repo-check --output-schema --json"
                ),
                "",
            )
        return subprocess.CompletedProcess(
            argv,
            0,
            "--search --sandbox read-only --ask-for-approval --cd",
            "",
        )

    capabilities = probe_codex_capabilities("codex", command_runner=command, env={})
    assert capabilities.supported
    assert capabilities.version == "codex-cli 1.2.3"


def test_argv_stdin_no_shell_and_env_allowlist(tmp_path: Path) -> None:
    argv = build_codex_argv(
        "codex",
        task_root=tmp_path,
        output_schema_path=tmp_path / "result.schema.json",
    )
    assert argv[0] == "codex"
    assert "--search" in argv
    assert argv[-1] == "-"
    assert "--ephemeral" in argv
    assert "--ignore-user-config" in argv
    assert "--ignore-rules" in argv
    assert "--skip-git-repo-check" in argv
    assert "read-only" in argv
    assert filtered_environment(
        {"PATH": "safe", "OPENAI_API_KEY": "secret", "COOKIE": "secret"}
    ) == {"PATH": "safe"}
    prompt = build_research_prompt(_task()).decode("utf-8")
    assert "find formal regulatory material" in prompt
    assert "parent_transcript" not in prompt
    assert "browser_profile" not in prompt


def test_exec_runner_parses_jsonl_and_uses_ephemeral_minimal_root() -> None:
    task = _task()
    provisional = ResearchResultBundle(
        research_result_bundle_id="govresearchbundle:exec",
        research_task_id=task.research_task_id,
        task_status=ResearchTaskStatus.COMPLETED,
        budget_used=BudgetUsage(
            rounds=1,
            child_tasks=1,
            network_requests=1,
            wall_clock_milliseconds=1,
            output_bytes=1,
        ),
        created_at=NOW,
        canonical_hash="0" * 64,
    )
    bundle = seal_research_result_bundle(provisional)
    stdout = b"\n".join(
        (
            canonical_json_bytes(
                {
                    "type": "item.completed",
                    "item": {"id": "item_0", "type": "web_search"},
                }
            ),
            canonical_json_bytes(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item_1",
                        "type": "agent_message",
                        "text": canonical_json_bytes(
                            bundle.model_dump(mode="json")
                        ).decode("utf-8"),
                    },
                }
            ),
        )
    ) + b"\n"
    captured = {}
    process = _Process(stdout)

    def factory(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        captured["files"] = sorted(path.name for path in Path(kwargs["cwd"]).iterdir())
        return process

    runner = CodexExecRunner(
        process_factory=factory,
        capability_probe=lambda *args, **kwargs: _capabilities(),
        env={"PATH": "safe", "OPENAI_API_KEY": "secret"},
    )
    result = runner.submit(task)
    assert result.research_result_bundle_id == bundle.research_result_bundle_id
    assert result.research_task_id == bundle.research_task_id
    assert result.task_status == bundle.task_status
    assert result.budget_used.network_requests == 1
    assert result.budget_used.output_bytes == len(stdout)
    assert result.budget_used != bundle.budget_used
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["env"] == {"PATH": "safe"}
    assert captured["files"] == ["research-result.schema.json", "research-task.json"]
    assert "--ephemeral" in captured["argv"]
    assert "--ignore-user-config" in captured["argv"]
    assert "--ignore-rules" in captured["argv"]
    assert "--skip-git-repo-check" in captured["argv"]
    if os.name == "nt":
        assert captured["kwargs"]["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert captured["kwargs"]["start_new_session"] is True
    assert b"formal regulatory material" in process.stdin.getvalue()


def test_timeout_size_and_request_budget_terminate() -> None:
    timed = _RunningProcess()
    timeout_result = _supervise(
        timed,
        stdin_payload=b"{}",
        wall_clock_seconds=0,
        max_stream_bytes=100,
        max_network_requests=1,
    )
    assert timeout_result.violation == "wall_clock_budget"
    assert timed.terminated

    oversized = _Process(b"x" * 101 + b"\n")
    size_result = _supervise(
        oversized,
        stdin_payload=b"{}",
        wall_clock_seconds=1,
        max_stream_bytes=100,
        max_network_requests=1,
    )
    assert size_result.violation == "stream_size_limit"

    combined_stdout = canonical_json_bytes(
        {"type": "turn.started", "padding": "x" * 30}
    ) + b"\n"
    combined_stderr = b"y" * 60 + b"\n"
    assert len(combined_stdout) < 100 and len(combined_stderr) < 100
    combined = _Process(combined_stdout, combined_stderr)
    combined_result = _supervise(
        combined,
        stdin_payload=b"{}",
        wall_clock_seconds=1,
        max_stream_bytes=100,
        max_network_requests=1,
    )
    assert combined_result.violation == "stream_size_limit"
    assert combined_result.output_bytes == len(combined_stdout) + len(combined_stderr)

    request_lines = b"\n".join(
        canonical_json_bytes({"type": "web_search"}) for _ in range(2)
    ) + b"\n"
    requests = _Process(request_lines)
    request_result = _supervise(
        requests,
        stdin_payload=b"{}",
        wall_clock_seconds=1,
        max_stream_bytes=1000,
        max_network_requests=1,
    )
    assert request_result.violation == "network_request_budget"
    assert request_result.network_requests == 2


def test_nested_jsonl_fixture_counts_network_and_extracts_structured_output() -> None:
    fixture = Path(__file__).parent / "fixtures" / "codex_exec_jsonl_v1.jsonl"
    events = [json.loads(line) for line in fixture.read_text(encoding="utf-8").splitlines()]
    assert sum(_is_network_event(event) for event in events) == 1
    assert _extract_final_payload(events) == {
        "kind": "research_result_bundle",
        "fixture": "nested-item-text",
    }


def test_recursive_or_unapproved_child_tool_event_fails_closed() -> None:
    recursive = _Process(
        canonical_json_bytes(
            {
                "type": "item.completed",
                "item": {"type": "spawn_agent", "name": "spawn_agent"},
            }
        )
        + b"\n"
    )
    result = _supervise(
        recursive,
        stdin_payload=b"{}",
        wall_clock_seconds=1,
        max_stream_bytes=1000,
        max_network_requests=1,
    )
    assert result.violation == "recursive_agent_event"

    command = _Process(
        canonical_json_bytes(
            {
                "type": "item.completed",
                "item": {"type": "command_execution", "command": "curl example.invalid"},
            }
        )
        + b"\n"
    )
    result = _supervise(
        command,
        stdin_payload=b"{}",
        wall_clock_seconds=1,
        max_stream_bytes=1000,
        max_network_requests=1,
    )
    assert result.violation == "unapproved_tool_event"


def test_task_output_schema_hash_must_match_installed_schema() -> None:
    task = _task().model_copy(update={"result_schema_hash": "f" * 64})
    # Reseal the otherwise valid task so this exercises the schema binding,
    # rather than the outer task-integrity guard.
    from analysis.governance.research_gate import research_task_hash

    task = task.model_copy(
        update={"canonical_hash": research_task_hash(task)}
    )
    runner = CodexExecRunner(
        process_factory=lambda *args, **kwargs: pytest.fail(
            "schema mismatch must be rejected before process creation"
        ),
        capability_probe=lambda *args, **kwargs: _capabilities(),
        env={"PATH": "safe"},
    )
    with pytest.raises(Exception) as mismatch:
        runner.submit(task)
    assert getattr(mismatch.value, "code", None) == "result_schema_binding_mismatch"
