from __future__ import annotations

import ssl
from datetime import datetime, timedelta, timezone

import httpcore
import httpx
import pytest

from analysis.acquisition.models import AcquisitionRun
from analysis.acquisition.orchestrator import AcquisitionExecutionError
from analysis.acquisition.registry import INITIAL_REGISTRY_PATH, SourceRegistryLoader
from analysis.acquisition.repository import AcquisitionNotFoundError
from analysis.acquisition.runtime import AcquisitionRuntime
from analysis.acquisition.source_gate import CrossProcessSourceGate
from analysis.acquisition.transport import RegistryBoundHttpTransport


def _runtime(root):
    return AcquisitionRuntime.create(root / "analysis.db", root / "data",
                                     registry_path=INITIAL_REGISTRY_PATH, workspace_root=root)


def _plan(runtime, *, persist=True):
    cutoff = datetime.now(timezone.utc)
    return runtime.create_plan(runtime.build_profile("600519"), mode="incremental",
                               run_kind="smoke", as_of=cutoff,
                               start_at=cutoff - timedelta(days=1), persist=persist)


@pytest.mark.parametrize("mode", ["windows_system", "environment", "no_proxy_only"])
def test_default_clients_use_direct_pools_despite_proxy_configuration(tmp_path, monkeypatch, mode):
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv(key.lower(), raising=False)
    if mode == "environment":
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy-user:proxy-secret@127.0.0.1:9")
    if mode == "no_proxy_only":
        monkeypatch.setenv("NO_PROXY", "www.cninfo.com.cn")
    # Simulate the common output of urllib's Windows proxy discovery as well
    # as environment proxy resolution, without opening any network connection.
    monkeypatch.setattr("httpx._utils.getproxies", lambda: {
        "http": "http://127.0.0.1:9", "https": "http://127.0.0.1:9"})
    runtime = _runtime(tmp_path / mode)
    definition = SourceRegistryLoader().load_registry(INITIAL_REGISTRY_PATH).definition("cninfo.disclosures")
    transport = RegistryBoundHttpTransport(definition, CrossProcessSourceGate(tmp_path / "gate"))
    try:
        for client in (runtime.http_client, transport._client):
            for url in ("https://www.cninfo.com.cn/new/hisAnnouncement/query",
                        "https://static.cninfo.com.cn/finalpage/test.pdf",
                        "https://query.sse.com.cn/security/test"):
                pool = client._transport_for_url(httpx.URL(url))._pool
                assert type(pool) is httpcore.ConnectionPool
                assert pool._ssl_context.verify_mode == ssl.CERT_REQUIRED
                assert pool._ssl_context.check_hostname
        plan = _plan(runtime)
        assert plan.run.http_route_policy == "direct-v1"
        assert "proxy-secret" not in plan.run.model_dump_json()
    finally:
        transport.close()
        runtime.close()


def test_route_policy_survives_restart_and_environment_change(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    plan = _plan(runtime)
    before = runtime.repository.get_run(plan.run.run_id).model_dump_json()
    runtime.close()
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    reopened = _runtime(tmp_path)
    try:
        assert reopened.repository.get_run(plan.run.run_id).model_dump_json() == before
        assert type(reopened.http_client._transport_for_url(httpx.URL("https://www.cninfo.com.cn"))._pool) is httpcore.ConnectionPool
    finally:
        reopened.close()


def test_legacy_unfinished_run_is_readable_but_cannot_silently_change_route(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    plan = _plan(runtime, persist=False)
    old_payload = plan.run.model_dump(mode="python")
    old_payload.pop("http_route_policy")
    legacy = AcquisitionRun.model_validate(old_payload)
    runtime.repository.save_plan_bundle(legacy, plan.physical_query_plan_items,
                                        plan.coverage_entries, plan.coverage_links)
    calls = []
    monkeypatch.setattr(runtime.http_client, "send", lambda *a, **k: calls.append(True))
    try:
        assert runtime.repository.get_run(legacy.run_id).http_route_policy is None
        with pytest.raises(AcquisitionExecutionError, match="http_route_policy_missing"):
            runtime.orchestrator.execute_run(legacy.run_id)
        assert runtime.repository.list_attempts(run_id=legacy.run_id) == []
        with pytest.raises(AcquisitionNotFoundError, match="lease not found"):
            runtime.repository.get_lease(legacy.run_id)
        assert calls == []
        assert runtime.repository.get_run(legacy.run_id) == legacy
        sentinel = object()
        monkeypatch.setattr(runtime.orchestrator, "_final_event", lambda _run_id: sentinel)
        monkeypatch.setattr(runtime.orchestrator, "_result_from_final", lambda _run, _event: sentinel)
        assert runtime.orchestrator.execute_run(legacy.run_id) is sentinel
    finally:
        runtime.close()
