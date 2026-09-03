from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from analysis.acquisition.registry import (  # noqa: E402
    DEFAULT_QUESTIONS_PATH,
    DEFAULT_REGISTRY_PATH,
    SourceRegistryError,
    SourceRegistryLoader,
)


SCHEMA_PATH = PROJECT_ROOT / "config" / "data_sources" / "source_registry.schema.json"
SENSITIVE_KEY_PARTS = ("authorization", "cookie", "password", "secret", "token")


def _validate_schema_document(path: Path) -> None:
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceRegistryError(f"来源注册表schema不可读: {path}: {exc}") from exc
    required = set(schema.get("required") or [])
    expected = {
        "schema_version",
        "registry_id",
        "registry_version",
        "effective_at",
        "question_set_version",
        "definitions",
        "legacy_definitions",
        "aliases",
    }
    if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        raise SourceRegistryError("source_registry.schema.json必须使用JSON Schema 2020-12")
    if schema.get("additionalProperties") is not False or not expected <= required:
        raise SourceRegistryError("source registry schema缺少严格顶层必填项")
    definitions = schema.get("$defs") or {}
    if "source_definition" not in definitions or "query" not in definitions:
        raise SourceRegistryError("source registry schema缺少definition/query约束")


def _reject_sensitive_keys(value: Any, *, location: str = "$") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if any(part in normalized for part in SENSITIVE_KEY_PARTS):
                raise SourceRegistryError(f"注册表不得包含敏感配置键: {location}.{key}")
            _reject_sensitive_keys(item, location=f"{location}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_sensitive_keys(item, location=f"{location}[{index}]")


def _read_for_sensitive_key_check(path: Path) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceRegistryError(f"注册表不可读: {path}: {exc}") from exc
    _reject_sensitive_keys(payload)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="校验AstraValue版本化来源注册表")
    parser.add_argument(
        "--registry",
        default=str(DEFAULT_REGISTRY_PATH),
        help="来源注册表JSON路径",
    )
    parser.add_argument(
        "--questions",
        default=str(DEFAULT_QUESTIONS_PATH),
        help="business_model问题清单JSON路径",
    )
    parser.add_argument(
        "--require-plan-traceability",
        action="store_true",
        help="要求十项question均可追踪到source query family",
    )
    parser.add_argument(
        "--expect-business-model-v1",
        type=int,
        default=None,
        help="要求business_model v1定义数量并检查固定v1边界",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        registry_path = Path(args.registry)
        questions_path = Path(args.questions)
        _validate_schema_document(SCHEMA_PATH)
        _read_for_sensitive_key_check(registry_path)
        loader = SourceRegistryLoader()
        questions = loader.load_questions(questions_path)
        registry = loader.load_registry(
            registry_path,
            question_set=questions if args.require_plan_traceability else None,
            expect_business_model_v1=args.expect_business_model_v1,
        )
        if args.require_plan_traceability:
            loader.validate_traceability(registry.registry, questions.question_set)
        print(
            "SOURCE_REGISTRY_OK "
            f"registry={registry.registry.registry_id}@{registry.registry.registry_version} "
            f"hash={registry.content_hash} questions={len(questions.question_set.topics)}"
        )
        return 0
    except (SourceRegistryError, OSError, ValueError) as exc:
        print(f"SOURCE_REGISTRY_INVALID: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
