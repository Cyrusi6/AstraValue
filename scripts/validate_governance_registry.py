from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from analysis.governance.registry import (
    GovernanceRegistryError,
    load_query_pack,
    load_question_registry,
    load_registry_bundle,
)


def _configure_stdio() -> None:
    """Keep the command contract UTF-8 even under a Windows GBK console."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="校验治理问题集和查询包")
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument("--query-pack", type=Path)
    parser.add_argument(
        "--require-plan-traceability",
        action="store_true",
        help="要求每个问题都有正式来源查询并验证物理查询去重",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    args = _parser().parse_args(argv)
    try:
        questions = load_question_registry(args.questions)
        if args.query_pack is None:
            if args.require_plan_traceability:
                raise GovernanceRegistryError(
                    "--require-plan-traceability需要同时提供--query-pack"
                )
            print(
                "GOVERNANCE_QUESTIONS_OK "
                f"questions={len(questions.questions)} "
                f"hash={questions.canonical_hash}"
            )
            return 0

        if args.require_plan_traceability:
            bundle = load_registry_bundle(args.questions, args.query_pack)
            print(
                "GOVERNANCE_REGISTRY_OK "
                f"questions={len(bundle.questions.questions)} "
                f"queries={len(bundle.query_pack.queries)} "
                f"physical_queries={bundle.query_pack.physical_query_count} "
                f"hash={bundle.canonical_hash}"
            )
        else:
            pack = load_query_pack(args.query_pack)
            print(
                "GOVERNANCE_QUERY_PACK_OK "
                f"queries={len(pack.queries)} "
                f"physical_queries={pack.physical_query_count} "
                f"hash={pack.canonical_hash}"
            )
        return 0
    except GovernanceRegistryError as exc:
        print(f"GOVERNANCE_REGISTRY_INVALID: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
