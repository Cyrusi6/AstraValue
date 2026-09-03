from __future__ import annotations

import multiprocessing
import os
import time
from pathlib import Path

import pytest

from analysis.acquisition.source_gate import (
    CrossProcessSourceGate,
    LocalSourceGateTimeout,
)


def _gate_worker(
    workspace: str,
    lock_root: str,
    source_id: str,
    host: str,
    interval: float,
    hold_seconds: float,
    output,
) -> None:
    gate = CrossProcessSourceGate(workspace, lock_root=lock_root)
    requested = time.monotonic()
    with gate.hold(
        source_id,
        host,
        min_interval_seconds=interval,
        deadline_monotonic=time.monotonic() + 5,
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
