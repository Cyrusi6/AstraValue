"""The CLI exposes the live tool contract and works in an isolated offline workspace."""
import json

import pytest

from analysis.research import cli
from analysis.research.tools import operations
from test_research_workspace import workspace, dump


@pytest.fixture
def configured_cli(workspace, monkeypatch):
    from analysis.research.jobs import MaterialJobs

    monkeypatch.setattr(MaterialJobs, "resume_task", lambda *args, **kwargs: pytest.fail("offline CLI started a worker"))
    config = {**workspace.config, "state_root": "isolated-state", "offline": True}
    config_path = workspace.root / "custom-workspace.json"
    dump(config_path, config)
    return workspace, ["--root", str(workspace.root), "--config", str(config_path)]


@pytest.mark.parametrize("operation,field,maximum", [
    ("query_research", "page_size", 40),
    ("list_materials", "page_size", 40),
    ("read_material", "max_tokens", 4000),
    ("read_evidence", "max_tokens", 4000),
    ("read_document_page", "max_tokens", 4000),
])
def test_describe_publishes_actual_bounds_without_invoking_operation(configured_cli, capsys, operation, field, maximum):
    _, options = configured_cli
    # Describing does not require a research ID or even a readable argument file.
    assert cli.main([operation, *options, "--describe", "--arguments", "@does-not-exist.json"]) == 0
    description = json.loads(capsys.readouterr().out)
    assert description["operation"] == operation
    schema = description["input_schema"]
    assert schema["additionalProperties"] is False
    assert "research_id" in schema["required"]
    assert schema["properties"]["page"]["minimum"] == 1
    assert schema["properties"][field]["minimum"] == 1
    assert schema["properties"][field]["maximum"] == maximum


def test_describe_includes_nested_calculation_contract(configured_cli, capsys):
    _, options = configured_cli
    assert cli.main(["calculate", *options, "--describe"]) == 0
    schema = json.loads(capsys.readouterr().out)["input_schema"]
    assert {"research_id", "method", "bindings"} == set(schema["required"])
    assert "pe_scenarios" in schema["properties"]["method"]["enum"]
    assert schema["$defs"]["Scenario"]["properties"]["growth"]["exclusiveMinimum"] == -1
    assert schema["$defs"]["Scenario"]["properties"]["valid_until"]["format"] == "date"
    assert schema["$defs"]["ScenarioAssumption"]["properties"]["value"]["maxItems"] == 3


def test_all_registered_operations_have_describable_contracts(workspace):
    for name, function in operations(workspace).items():
        schema = cli.argument_model(name, function).model_json_schema()
        assert schema["type"] == "object"


def test_custom_config_is_used_without_touching_default_state(configured_cli, capsys):
    workspace, options = configured_cli
    arguments = workspace.root / "arguments.json"
    arguments.write_text(json.dumps({"company": "贵州茅台", "as_of": "2025-01-01"}, ensure_ascii=False), encoding="utf-8-sig")
    original_db = workspace.db.read_bytes()
    assert cli.main(["prepare_research", *options, "--arguments", "@" + str(arguments)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["research_id"].startswith("r_")
    assert (workspace.root / "isolated-state/research-tasks.sqlite").exists()
    assert workspace.db.read_bytes() == original_db
    assert cli.main(["get_task", *options, "--arguments", json.dumps({"research_id": result["research_id"]})]) == 0
    assert json.loads(capsys.readouterr().out)["research_id"] == result["research_id"]


def test_existing_root_and_arguments_flags_still_work(workspace, capsys):
    dump(workspace.root / "config/research_workspace.json", {**workspace.config, "offline": True})
    assert cli.main(["prepare_research", "--root", str(workspace.root), "--arguments",
                     '{"company":"600519","as_of":"2025-01-01","offline":true}']) == 0
    assert json.loads(capsys.readouterr().out)["research_id"].startswith("r_")


@pytest.mark.parametrize("arguments,field,error_type", [
    ('{"research_id":"missing","unknown":1}', "unknown", "extra_forbidden"),
    ('{}', "research_id", "missing"),
    ('{"research_id":"missing","page_size":50}', "page_size", "less_than_equal"),
    ('{"research_id":"missing","page_size":0}', "page_size", "greater_than_equal"),
    ('{"research_id":"missing","page_size":1.5}', "page_size", "int_type"),
    ('{"research_id":"missing","page_size":true}', "page_size", "int_type"),
    ('{"research_id":"missing","period_type":"annual"}', "period_type", "literal_error"),
    ('[]', "$", "model_type"),
    ('not-json', "$", "json_invalid"),
])
def test_invalid_arguments_return_field_errors_before_data_access(configured_cli, capsys, arguments, field, error_type):
    _, options = configured_cli
    assert cli.main(["query_research", *options, "--arguments", arguments]) == 1
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["status"] == "error"
    assert result["errors"][0]["field"] == field
    assert result["errors"][0]["type"] == error_type
    assert field in result["reason"]
    assert "unknown_research_id" not in result["reason"]
    assert "Traceback" not in captured.err


@pytest.mark.parametrize("content", [None, b"\xff", b"not-json"])
def test_argument_file_failures_are_json_errors(configured_cli, capsys, content):
    workspace, options = configured_cli
    path = workspace.root / "broken-arguments.json"
    if content is not None:
        path.write_bytes(content)
    assert cli.main(["get_task", *options, "--arguments", "@" + str(path)]) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out)["status"] == "error"
    assert "Traceback" not in captured.err


@pytest.mark.parametrize("content", [None, b"\xff", b"not-json", b"[]", b"{}"])
def test_config_file_failures_are_json_errors(workspace, capsys, content):
    path = workspace.root / "broken-config.json"
    if content is not None:
        path.write_bytes(content)
    assert cli.main(["get_task", "--root", str(workspace.root), "--config", str(path), "--describe"]) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out)["status"] == "error"
    assert "Traceback" not in captured.err


def test_unknown_operation_retains_argparse_error(configured_cli, capsys):
    _, options = configured_cli
    with pytest.raises(SystemExit) as error:
        cli.main(["unknown-operation", *options])
    assert error.value.code == 2
    assert "Unknown operation; supported:" in capsys.readouterr().err
