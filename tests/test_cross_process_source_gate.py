from __future__ import annotations

import multiprocessing
import os
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from analysis.acquisition.source_gate import (
    CrossProcessSourceGate,
    LocalSourceGateTimeout,
)
from analysis.structured.scheduler import StructuredExecutionBridge


def _gate_worker(
    workspace: str,
    lock_root: str,
    source_id: str,
    host: str,
    interval: float,
    hold_seconds: float,
    output,
    upstream_identity: str | None = None,
) -> None:
    gate = CrossProcessSourceGate(workspace, lock_root=lock_root)
    requested = time.monotonic()
    with gate.hold(
        source_id,
        host,
        min_interval_seconds=interval,
        deadline_monotonic=time.monotonic() + 5,
        upstream_identity=upstream_identity,
    ):
        started = time.monotonic()
        output.put(("start", os.getpid(), requested, started))
        time.sleep(hold_seconds)
        output.put(("end", os.getpid(), time.monotonic()))


def _crash_worker(workspace: str, lock_root: str, ready) -> None:
    gate = CrossProcessSourceGate(workspace, lock_root=lock_root)
    with gate.hold(
        "cninfo.disclosures",
        "www.cninfo.com.cn",
        min_interval_seconds=0.08,
        deadline_monotonic=time.monotonic() + 5,
    ):
        ready.put(time.monotonic())
        ready.close()
        ready.join_thread()
        os._exit(17)


def _long_holder(workspace: str, lock_root: str, ready) -> None:
    gate = CrossProcessSourceGate(workspace, lock_root=lock_root)
    with gate.hold(
        "sse.disclosures",
        "query.sse.com.cn",
        min_interval_seconds=0.02,
        deadline_monotonic=time.monotonic() + 5,
    ):
        ready.put(True)
        time.sleep(0.6)


def test_cross_process_gate_serializes_different_namespace_same_source_gate(
    tmp_path: Path,
) -> None:
    context = multiprocessing.get_context("spawn")
    output = context.Queue()
    args = (
        str(tmp_path / "workspace"),
        str(tmp_path / "locks"),
        "cninfo.disclosures",
        "www.cninfo.com.cn",
        0.08,
        0.15,
        output,
    )
    first = context.Process(target=_gate_worker, args=args)
    second = context.Process(target=_gate_worker, args=args)
    first.start()
    second.start()
    messages = [output.get(timeout=8) for _ in range(4)]
    first.join(5)
    second.join(5)
    assert first.exitcode == second.exitcode == 0

    by_pid: dict[int, dict[str, float]] = {}
    for message in messages:
        if message[0] == "start":
            _, pid, _requested, started = message
            by_pid.setdefault(pid, {})["start"] = started
        else:
            _, pid, ended = message
            by_pid.setdefault(pid, {})["end"] = ended
    windows = sorted(by_pid.values(), key=lambda item: item["start"])
    assert len(windows) == 2
    assert windows[1]["start"] >= windows[0]["end"] + 0.06


def test_holder_crash_releases_lock_but_next_waits_full_interval(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    workspace = str(tmp_path / "workspace")
    lock_root = str(tmp_path / "locks")
    process = context.Process(target=_crash_worker, args=(workspace, lock_root, ready))
    process.start()
    ready.get(timeout=5)
    process.join(5)
    assert process.exitcode == 17

    gate = CrossProcessSourceGate(workspace, lock_root=lock_root)
    acquired_call = time.monotonic()
    with gate.hold(
        "cninfo.disclosures",
        "www.cninfo.com.cn",
        min_interval_seconds=0.08,
        deadline_monotonic=time.monotonic() + 2,
    ):
        entered = time.monotonic()
    assert entered - acquired_call >= 0.06


def test_two_domains_for_one_real_upstream_share_one_gate(tmp_path: Path) -> None:
    assert CrossProcessSourceGate._gate_key(
        "structured-eastmoney-finance",
        "emweb.securities.eastmoney.com",
        upstream_identity="eastmoney",
    ) == CrossProcessSourceGate._gate_key(
        "structured-eastmoney-data",
        "datacenter.eastmoney.com",
        upstream_identity="EASTMONEY",
    )
    assert CrossProcessSourceGate._gate_key(
        "structured-baostock",
        "baostock.example",
        upstream_identity="baostock",
    ) != CrossProcessSourceGate._gate_key(
        "structured-eastmoney-data",
        "datacenter.eastmoney.com",
        upstream_identity="eastmoney",
    )

    context = multiprocessing.get_context("spawn")
    output = context.Queue()
    shared = (
        str(tmp_path / "workspace"),
        str(tmp_path / "locks"),
    )
    first = context.Process(
        target=_gate_worker,
        args=(
            *shared,
            "structured-eastmoney-finance",
            "emweb.securities.eastmoney.com",
            0.08,
            0.15,
            output,
            "eastmoney",
        ),
    )
    second = context.Process(
        target=_gate_worker,
        args=(
            *shared,
            "structured-eastmoney-data",
            "datacenter.eastmoney.com",
            0.08,
            0.15,
            output,
            "eastmoney",
        ),
    )
    first.start()
    second.start()
    messages = [output.get(timeout=8) for _ in range(4)]
    first.join(5)
    second.join(5)
    assert first.exitcode == second.exitcode == 0

    by_pid: dict[int, dict[str, float]] = {}
    for message in messages:
        if message[0] == "start":
            _, pid, _requested, started = message
            by_pid.setdefault(pid, {})["start"] = started
        else:
            _, pid, ended = message
            by_pid.setdefault(pid, {})["end"] = ended
    windows = sorted(by_pid.values(), key=lambda item: item["start"])
    assert windows[1]["start"] >= windows[0]["end"] + 0.06


def test_structured_bridge_enforces_source_minimums_and_invalid_lease_never_enters_gate(
    tmp_path: Path,
) -> None:
    class RecordingGate:
        def __init__(self):
            self.calls = []

        @contextmanager
        def hold(self, source_definition_id, host, **kwargs):
            self.calls.append((source_definition_id, host, kwargs))
            yield SimpleNamespace(source_definition_id=source_definition_id, host=host)

    class Storage:
        storage_namespace_id = "namespace-1"

        @staticmethod
        def get_run_context(run_id, expected_pins=None):
            return SimpleNamespace(
                run_id=run_id,
                storage_namespace_id="namespace-1",
                ticker="600519",
            )

    class Repository:
        @staticmethod
        def get_run(run_id):
            return {
                "run_id": run_id,
                "storage_namespace_id": "namespace-1",
                "ticker": "600519",
            }

    recording = RecordingGate()
    bridge = StructuredExecutionBridge(
        repository=Repository(),
        storage=Storage(),
        source_gate=recording,
        snapshot_service=SimpleNamespace(storage_namespace_id="namespace-1"),
    )
    guard = lambda **_: None
    for upstream, expected in (("eastmoney", 3.0), ("baostock", 3.0), ("cninfo", 5.0)):
        with bridge.hold_source(
            run_id="run-1",
            source_definition={
                "source_definition_id": f"structured-{upstream}",
                "upstream_identity": upstream,
                "rate_limit": {"min_interval_seconds": 0.01},
            },
            host=f"{upstream}.example",
            deadline_monotonic=time.monotonic() + 10,
            lease_guard=guard,
        ):
            pass
        assert recording.calls[-1][2]["min_interval_seconds"] == expected
        assert recording.calls[-1][2]["upstream_identity"] == upstream

    real_gate = CrossProcessSourceGate(
        tmp_path / "workspace", lock_root=tmp_path / "lease-locks"
    )
    entered = False

    def invalid_lease(*, force=False):
        raise RuntimeError("stale structured lease")

    with pytest.raises(RuntimeError, match="stale structured lease"):
        with real_gate.hold(
            "structured-eastmoney",
            "datacenter.eastmoney.com",
            upstream_identity="eastmoney",
            min_interval_seconds=0.01,
            deadline_monotonic=time.monotonic() + 1,
            lease_guard=invalid_lease,
        ):
            entered = True
    assert entered is False


def test_local_source_gate_timeout_is_typed(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    workspace = str(tmp_path / "workspace")
    lock_root = str(tmp_path / "locks")
    process = context.Process(target=_long_holder, args=(workspace, lock_root, ready))
    process.start()
    ready.get(timeout=5)
    gate = CrossProcessSourceGate(workspace, lock_root=lock_root)
    with pytest.raises(LocalSourceGateTimeout) as caught:
        with gate.hold(
            "sse.disclosures",
            "query.sse.com.cn",
            min_interval_seconds=0.02,
            deadline_monotonic=time.monotonic() + 0.08,
        ):
            raise AssertionError("gate unexpectedly acquired")
    assert caught.value.reason_code == "local_source_gate_timeout"
    process.join(5)
    assert process.exitcode == 0
