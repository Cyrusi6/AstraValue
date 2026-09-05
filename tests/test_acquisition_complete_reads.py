from collections import Counter
from datetime import datetime, timedelta, timezone

from analysis.acquisition.models import AcquisitionAttempt, AcquisitionAttemptEvent
from orchestrator_support import make_runtime, no_data_adapter, targeted_plan


def test_late_failure_halt_and_abandoned_are_visible_beyond_display_page(tmp_path):
    runtime = make_runtime(tmp_path, no_data_adapter())
    plan = targeted_plan(runtime)
    repo = runtime.repository
    now = datetime.now(timezone.utc)
    lease, token = repo.claim_lease(plan.run.run_id, now=now, ttl_seconds=600)
    item = plan.physical_query_plan_items[0]
    attempts = []
    for index in range(503):
        attempt = AcquisitionAttempt(
            run_id=plan.run.run_id,
            source_definition_id=item.source_definition_id,
            source_definition_version=item.source_definition_version,
            physical_query_plan_item_id=item.plan_item_id,
            execution_key=item.execution_key, attempt_kind="discovery",
            query_id=item.query_id, time_start=item.time_start, time_end=item.time_end,
            page_number=1, work_position="page:1", retry_group_id=f"group-{index}",
            lease_epoch=lease.lease_epoch, started_at=now + timedelta(microseconds=index),
        )
        repo.save_attempt(attempt, owner_token=token)
        attempts.append(attempt)
        if index == 502:
            continue
        outcome, reason = (
            ("policy_skipped", "fixture_skip") if index < 500 else
            ("timeout", "http_504") if index == 500 else
            ("restricted", "upstream_bot_challenge")
        )
        repo.append_attempt_event(AcquisitionAttemptEvent(
            attempt_id=attempt.attempt_id, event_type="outcome_terminal",
            outcome=outcome, reason_code=reason, lease_epoch=lease.lease_epoch,
            occurred_at=now + timedelta(milliseconds=1),
        ), owner_token=token)

    assert len(repo.list_attempts(run_id=plan.run.run_id)) == 500
    assert repo.list_attempts(run_id=plan.run.run_id, offset=500) == attempts[500:]
    assert repo.list_attempts(run_id=plan.run.run_id, limit=None) == attempts
    assert repo.list_attempts(retry_group_id="group-502", limit=None) == [attempts[-1]]
    assert runtime.orchestrator._latest_attempt(item.plan_item_id) == attempts[-1]
    facts = runtime.orchestrator._attempt_facts(plan.run.run_id)
    assert Counter(f.outcome.value for f in facts) == {
        "policy_skipped": 500, "timeout": 1, "restricted": 1,
    }
    runtime.orchestrator._rebuild_source_halts(plan.run.run_id)
    halt = runtime.orchestrator._source_halts[
        runtime.orchestrator._halt_key(plan.run.run_id, item)
    ]
    assert halt["halt_opening_attempt_id"] == attempts[501].attempt_id
    resumable = runtime.orchestrator._close_abandoned_attempts(
        plan.run, lease.lease_epoch, token,
    )
    assert resumable[item.plan_item_id] == attempts[502]
    assert [e.event_type.value for e in repo.list_attempt_events(attempts[-1].attempt_id)] == ["abandoned"]
    assert len(runtime.orchestrator._attempt_facts(plan.run.run_id)) == 502
    runtime.close()


def test_reused_resource_from_distinct_discovery_rows_keeps_all_coverage_links(tmp_path):
    from analysis.acquisition.models import AcquisitionPlan
    from orchestrator_support import ScenarioAdapter, discovery_result, envelope, resource
    adapter = ScenarioAdapter(lambda work: envelope(url=work.url),
        lambda _, work: discovery_result(work, resources=(resource(),), declared_total=1),
        lambda work: envelope(url=work.resource.url, body=b"%PDF-1.4\nfixture\n%%EOF", content_type="application/pdf"))
    runtime = make_runtime(tmp_path, adapter)
    now = datetime.now(timezone.utc)
    base = runtime.create_plan(runtime.build_profile("600519"), mode="incremental",
        run_kind="ad_hoc", as_of=now, start_at=now-timedelta(days=1), persist=False)
    plans = tuple(p for p in base.physical_query_plan_items
                  if p.query_id in {"cninfo.prospectus", "cninfo.periodic_report"})
    plan_ids = {p.plan_item_id for p in plans}
    links = tuple(l for l in base.coverage_links if l.plan_item_id in plan_ids)
    coverage_ids = {l.coverage_entry_id for l in links}
    plan = AcquisitionPlan(run=base.run, physical_query_plan_items=plans, coverage_links=links,
        coverage_entries=tuple(c for c in base.coverage_entries if c.coverage_entry_id in coverage_ids))
    result = runtime.orchestrator.execute_plan(plan)
    assert len(adapter.query_calls) == 2 and len(adapter.fetch_calls) == 1
    assert result.material_gap_count == 0
    attempts = runtime.repository.list_attempts(run_id=plan.run.run_id, limit=None)
    fetch = next(a for a in attempts if a.attempt_kind.value == "fetch")
    fetch_links = runtime.repository.list_physical_query_coverage_links(plan_item_id=fetch.physical_query_plan_item_id)
    assert {l.coverage_entry_id for l in fetch_links} == coverage_ids
    assert all(fetch.attempt_id in r.attempt_ids for r in runtime.repository.list_coverage_resolutions(plan.run.run_id))
    runtime.close()
