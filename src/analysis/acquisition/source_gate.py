from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator


class LocalSourceGateTimeout(TimeoutError):
    """The shared local source gate could not be acquired before deadline."""

    reason_code = "local_source_gate_timeout"


@dataclass(frozen=True)
class SourceGatePermit:
    source_definition_id: str
    host: str
    acquired_monotonic: float
    request_not_before_monotonic: float
    upstream_identity: str | None = None


class CrossProcessSourceGate:
    """Serialize access to one upstream source across local Python processes.

    The lock identity intentionally excludes the evidence namespace.  Two
    isolated pilots therefore still share the same upstream rate/concurrency
    boundary.  A process always waits one complete configured interval after
    acquiring the OS lock.  This conservative rule also applies after a lock
    holder crashes and the operating system releases the lock.
    """

    def __init__(
        self,
        workspace_root: Path | str,
        *,
        lock_root: Path | str | None = None,
        poll_interval_seconds: float = 0.025,
        clock=time.monotonic,
        sleeper=time.sleep,
    ) -> None:
        workspace = Path(workspace_root).expanduser().resolve()
        workspace_identity = hashlib.sha256(
            os.path.normcase(str(workspace)).encode("utf-8")
        ).hexdigest()
        base = (
            Path(lock_root).expanduser().resolve()
            if lock_root is not None
            else Path(tempfile.gettempdir()) / "astravalue-source-gates"
        )
        self._directory = base / workspace_identity
        self._directory.mkdir(parents=True, exist_ok=True)
        self._poll_interval = max(0.001, float(poll_interval_seconds))
        self._clock = clock
        self._sleep = sleeper

    @staticmethod
    def _gate_key(
        source_definition_id: str,
        host: str,
        *,
        upstream_identity: str | None = None,
    ) -> str:
        normalized_source = source_definition_id.strip().lower()
        normalized_host = host.strip().lower().rstrip(".")
        if not normalized_source or not normalized_host:
            raise ValueError("source_definition_id and host are required")
        normalized_upstream = (upstream_identity or "").strip().lower()
        identity = (
            f"upstream\n{normalized_upstream}"
            if normalized_upstream
            else f"source-host\n{normalized_source}\n{normalized_host}"
        )
        return hashlib.sha256(
            identity.encode("utf-8")
        ).hexdigest()

    @property
    def lock_directory(self) -> Path:
        return self._directory

    @contextmanager
    def hold(
        self,
        source_definition_id: str,
        host: str,
        *,
        min_interval_seconds: float,
        deadline_monotonic: float,
        lease_guard: Callable[..., None] | None = None,
        upstream_identity: str | None = None,
    ) -> Iterator[SourceGatePermit]:
        if min_interval_seconds <= 0:
            raise ValueError("min_interval_seconds must be positive")
        gate_key = self._gate_key(
            source_definition_id,
            host,
            upstream_identity=upstream_identity,
        )
        path = self._directory / f"{gate_key}.lock"
        path.touch(exist_ok=True)
        # Initialize the lock byte before opening the random-access handle.
        # Two Windows processes can both observe a new zero-length file; using
        # append mode makes concurrent initialization harmless instead of
        # letting the loser overwrite byte 0 after the winner has locked it.
        with path.open("ab", buffering=0) as initializer:
            if path.stat().st_size == 0:
                initializer.write(b"\0")
                initializer.flush()
        with path.open("r+b", buffering=0) as handle:
            self._guard_lease(lease_guard)
            self._acquire(
                handle,
                deadline_monotonic,
                lease_guard=lease_guard,
            )
            try:
                self._guard_lease(lease_guard, force=True)
                acquired = self._clock()
                not_before = acquired + float(min_interval_seconds)
                if not_before > deadline_monotonic:
                    raise LocalSourceGateTimeout(
                        "shared source gate interval exceeds request deadline"
                    )
                self._sleep_until(
                    not_before,
                    deadline_monotonic,
                    lease_guard=lease_guard,
                )
                # A process may have been suspended while it held or waited
                # for the gate.  Fence again immediately before allowing the
                # caller to issue network I/O.
                self._guard_lease(lease_guard, force=True)
                started = self._clock()
                self._write_audit_metadata(
                    handle,
                    {
                        "source_definition_id": source_definition_id,
                        "upstream_identity": upstream_identity,
                        "host": host.lower().rstrip("."),
                        "request_started_monotonic": started,
                        "pid": os.getpid(),
                    },
                )
                yield SourceGatePermit(
                    source_definition_id=source_definition_id,
                    host=host.lower().rstrip("."),
                    acquired_monotonic=acquired,
                    request_not_before_monotonic=not_before,
                    upstream_identity=upstream_identity,
                )
            finally:
                self._release(handle)

    def _sleep_until(
        self,
        target: float,
        deadline: float,
        *,
        lease_guard: Callable[..., None] | None = None,
    ) -> None:
        while True:
            self._guard_lease(lease_guard)
            now = self._clock()
            if now >= target:
                return
            if now >= deadline:
                raise LocalSourceGateTimeout(
                    "shared source gate interval exceeded request deadline"
                )
            self._sleep(min(target - now, deadline - now, self._poll_interval))

    def _acquire(
        self,
        handle,
        deadline: float,
        *,
        lease_guard: Callable[..., None] | None = None,
    ) -> None:
        while True:
            self._guard_lease(lease_guard)
            try:
                _lock_one_byte(handle)
                return
            except OSError as exc:
                now = self._clock()
                if now >= deadline:
                    raise LocalSourceGateTimeout(
                        "shared source gate was busy until request deadline"
                    ) from exc
                self._sleep(min(self._poll_interval, deadline - now))

    @staticmethod
    def _guard_lease(
        lease_guard: Callable[..., None] | None,
        *,
        force: bool = False,
    ) -> None:
        if lease_guard is not None:
            lease_guard(force=force)

    @staticmethod
    def _release(handle) -> None:
        _unlock_one_byte(handle)

    @staticmethod
    def _write_audit_metadata(handle, payload: dict[str, object]) -> None:
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        handle.seek(1)
        handle.write(encoded)
        handle.truncate()
        handle.flush()
        os.fsync(handle.fileno())


if os.name == "nt":
    import msvcrt

    def _lock_one_byte(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock_one_byte(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock_one_byte(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock_one_byte(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
