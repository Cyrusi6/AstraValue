"""JSON command interface using the same registered operations as MCP."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .workspace import ROOT, ResearchWorkspace


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", help="prepare_research/get_task/query_research/read_evidence/...")
    parser.add_argument("--arguments", default="{}", help="JSON arguments, or @UTF-8-file")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    from .tools import operations
    registry = operations(ResearchWorkspace(args.root))
    if args.operation not in registry:
        parser.error("Unknown operation; supported: " + ", ".join(registry))
    data = args.arguments
    if data.startswith("@"):
        data = Path(data[1:]).read_text(encoding="utf-8-sig")
    try:
        value = registry[args.operation](**json.loads(data))
        print(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (ValueError, KeyError, OSError) as exc:
        print(json.dumps({"status": "error", "reason": str(exc)}, ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
