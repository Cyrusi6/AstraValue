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
