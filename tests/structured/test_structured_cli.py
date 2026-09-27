import json

from analysis import cli


class FakeService:
    def __init__(self):
        self.calls = []

    def plan(self, ticker, **kwargs):
        self.calls.append(("plan", ticker, kwargs))
        return {
            "plan_id": "preview:1",
            "performed_network_io": False,
            "mode": kwargs["mode"],
        }

    def run(self, run_id):
        self.calls.append(("run", run_id))
        return {"run_id": run_id, "single_round": True}

    def resume(self, run_id):
        self.calls.append(("resume", run_id))
        return {"run_id": run_id, "frozen_context": True}

    def status(self, run_id):
        return {"run_id": run_id, "status": "partial"}

    def repair_plan(self, run_id, **kwargs):
        self.calls.append(("repair-plan", run_id, kwargs))
        return {
            "run_id": run_id,
            "manifest_id": "structured-repair-" + "a" * 24,
            "manifest_sha256": "a" * 64,
            "target_count": 2,
            "performed_network_io": False,
        }

    def repair_run(self, manifest, **kwargs):
        self.calls.append(("repair-run", manifest, kwargs))
        return {
            "manifest_id": "structured-repair-" + "a" * 24,
            "attempted_job_ids": ["job-1"],
            "single_round": True,
        }

    def repair_status(self, manifest, **kwargs):
        self.calls.append(("repair-status", manifest, kwargs))
        return {
            "manifest_id": "structured-repair-" + "a" * 24,
            "performed_network_io": False,
        }

    def records(self, **kwargs):
        return {"total": 501, "items": [{"record_id": "last"}]}

    def reading_tasks(self, **kwargs):
        return {"total": 0, "items": []}

    def resolve_company(self, query, **kwargs):
        return {"query": query, "status": "resolved", "performed_io": False}

    def industry_profile(self, ticker):
        return {"ticker": ticker, "status": "pending"}

    def peer_candidates(self, ticker, **kwargs):
        return {"ticker": ticker, "recursive_expansion": False}

    def coverage(self, snapshot_id, **kwargs):
        return {"coverage_snapshot_id": snapshot_id, "total": 0, "items": []}

    def report(self, pack_dir, **kwargs):
        self.calls.append(("report", pack_dir, kwargs))
        return {
            "report_id": "report-1",
            "data_snapshot_id": "snapshot-1",
            "outputs": {"md": {"relative_path": "600519/report.md"}},
            "performed_network_io": False,
        }


def invoke(monkeypatch, capsys, fake, *arguments):
    monkeypatch.setattr(cli, "_create_structured_service", lambda db, root: fake)
    result = cli.main(
        ["structured", *arguments, "--db", "isolated.db", "--data-root", "isolated-data", "--json"]
    )
    output = json.loads(capsys.readouterr().out)
    return result, output


def test_structured_plan_is_zero_network_and_keeps_due_mode(monkeypatch, capsys):
    fake = FakeService()
    code, output = invoke(
        monkeypatch,
        capsys,
        fake,
        "plan",
        "600519",
        "--mode",
        "due",
        "--dataset",
        "balance_fields",
    )
    assert code == 0
    assert output["performed_network_io"] is False
    assert output["mode"] == "due"
    assert fake.calls[0][2]["datasets"] == ("balance_fields",)


def test_structured_reconcile_plan_passes_parent_selector(monkeypatch, capsys):
    fake = FakeService()
    code, output = invoke(
        monkeypatch,
        capsys,
        fake,
        "plan",
        "600519",
        "--mode",
        "reconcile",
        "--from-run",
        "structured-run-parent",
    )
    assert code == 0
    assert output["mode"] == "reconcile"
    assert fake.calls[0][2]["parent_run_id"] == "structured-run-parent"
    assert fake.calls[0][2]["from_latest"] is False

    fake = FakeService()
    code, _ = invoke(
        monkeypatch,
        capsys,
        fake,
        "plan",
        "600519",
        "--mode",
        "reconcile",
        "--from-latest",
    )
    assert code == 0
    assert fake.calls[0][2]["parent_run_id"] is None
    assert fake.calls[0][2]["from_latest"] is True


def test_run_and_resume_execute_one_explicit_call(monkeypatch, capsys):
    fake = FakeService()
    code, output = invoke(monkeypatch, capsys, fake, "run", "run:1")
    assert code == 0
    assert output["single_round"] is True
    assert fake.calls == [("run", "run:1")]

    code, output = invoke(monkeypatch, capsys, fake, "resume", "run:1")
    assert code == 0
    assert output["frozen_context"] is True
    assert fake.calls[-1] == ("resume", "run:1")


def test_query_subcommands_bind_database_and_do_not_install_scheduler(monkeypatch, capsys):
    fake = FakeService()
    for arguments in (
        ("status", "run:1"),
        ("records", "--limit", "1000", "--offset", "500"),
        ("reading-tasks",),
        ("resolve", "贵州茅台"),
        ("profile", "600519"),
        ("peers", "600519"),
        ("coverage", "snapshot:1"),
    ):
        code, _ = invoke(monkeypatch, capsys, fake, *arguments)
        assert code == 0


def test_structured_report_passes_pack_output_and_formats(monkeypatch, capsys):
    fake = FakeService()
    code, output = invoke(
        monkeypatch,
        capsys,
        fake,
        "report",
        "--pack",
        "lite-pack",
        "--output",
        "reports",
        "--format",
        "md",
        "--format",
        "xlsx",
    )
    assert code == 0
    assert output["report_id"] == "report-1"
    assert output["performed_network_io"] is False
    assert fake.calls[-1] == (
        "report",
        "lite-pack",
        {"output_dir": "reports", "formats": ("md", "xlsx"), "industry": None},
    )


def test_repair_commands_are_explicit_bounded_and_do_not_echo_storage_paths(
    monkeypatch, capsys
):
    fake = FakeService()
    code, planned = invoke(
        monkeypatch,
        capsys,
        fake,
        "repair-plan",
        "run:1",
        "--dataset",
        "baostock_calendar",
        "--reason",
        "ProtocolError:BaoStock login failed: blocked",
        "--revision",
        "d" * 40,
        "--output",
        "ignored/private/repair.json",
    )
    assert code == 0
    assert planned["performed_network_io"] is False
    assert "isolated.db" not in json.dumps(planned)
    assert "isolated-data" not in json.dumps(planned)
    assert fake.calls[-1][2]["datasets"] == ("baostock_calendar",)

    code, executed = invoke(
        monkeypatch,
        capsys,
        fake,
        "repair-run",
        "ignored/private/repair.json",
        "--revision",
        "d" * 40,
        "--max-jobs",
        "2",
    )
    assert code == 0
    assert executed["single_round"] is True
    assert fake.calls[-1][2]["max_jobs_per_round"] == 2

    code, status = invoke(
        monkeypatch,
        capsys,
        fake,
        "repair-status",
        "ignored/private/repair.json",
        "--revision",
        "d" * 40,
    )
    assert code == 0
    assert status["performed_network_io"] is False
    assert fake.calls[-1][0] == "repair-status"
