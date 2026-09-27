"""JSON command interface using the same registered operations as MCP."""
from __future__ import annotations

import argparse
import inspect
import json
from pathlib import Path
from typing import Any, get_type_hints

from pydantic import ConfigDict, ValidationError, create_model

from .workspace import ROOT, ResearchWorkspace


def argument_model(operation, function):
    """Publish and validate the registered callable's contract, including Annotated bounds."""
    hints = get_type_hints(function, include_extras=True)
    fields = {
        name: (
            hints.get(name, Any),
            parameter.default if parameter.default is not inspect.Parameter.empty else ...,
        )
        for name, parameter in inspect.signature(function).parameters.items()
    }
    return create_model(f"{operation}Arguments", __config__=ConfigDict(extra="forbid"), **fields)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", help="prepare_research/get_task/query_research/read_evidence/...")
    parser.add_argument("--arguments", default="{}", help="JSON arguments, or @UTF-8-file")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--config", type=Path, help="UTF-8 workspace configuration JSON; paths resolve under --root")
    parser.add_argument("--describe", action="store_true", help="Print the operation's JSON input schema without executing it")
    args = parser.parse_args(argv)
    from .tools import operations
    try:
        if args.config is None:
            workspace = ResearchWorkspace(args.root)
        else:
            config = json.loads(args.config.read_text(encoding="utf-8-sig"))
            if not isinstance(config, dict) or not config:
                raise ValueError("--config must contain a non-empty JSON object")
            workspace = ResearchWorkspace(args.root, config=config)
        registry = operations(workspace)
        if args.operation not in registry:
            parser.error("Unknown operation; supported: " + ", ".join(registry))
        function = registry[args.operation]
        model = argument_model(args.operation, function)
        if args.describe:
            print(json.dumps({"operation": args.operation, "description": inspect.getdoc(function) or "",
                              "input_schema": model.model_json_schema()}, ensure_ascii=False, indent=2))
            return 0
        data = args.arguments
        if data.startswith("@"):
            data = Path(data[1:]).read_text(encoding="utf-8-sig")
        arguments = model.model_validate_json(data, strict=True)
        value = function(**arguments.model_dump(mode="json"))
        print(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except ValidationError as exc:
        errors = [{"field": ".".join(map(str, error["loc"])) or "$",
                   "message": error["msg"], "type": error["type"]}
                  for error in exc.errors(include_input=False, include_context=False, include_url=False)]
        reason = "invalid_arguments: " + "; ".join(f"{error['field']}: {error['message']}" for error in errors)
        print(json.dumps({"status": "error", "reason": reason, "errors": errors}, ensure_ascii=False))
        return 1
    except (ValueError, KeyError, OSError) as exc:
        print(json.dumps({"status": "error", "reason": str(exc)}, ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
