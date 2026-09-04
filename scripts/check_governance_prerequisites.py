from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path


REQUIRED_IDENTITY_FIELDS: tuple[str, ...] = (
    "acquisition_scope",
    "question_set_id",
    "question_set_version",
    "query_pack_version",
    "source_registry_version",
)

MODEL_CONTRACTS: tuple[str, ...] = (
    "AcquisitionRun",
    "CoverageEntry",
    "SourceCheckpoint",
    "EvidenceSnapshotManifest",
)


def _class_fields(source: str, class_name: str) -> set[str]:
    tree = ast.parse(source)
    classes = {
        node.name: node for node in tree.body if isinstance(node, ast.ClassDef)
    }
    cache: dict[str, set[str]] = {}

    def visit(name: str, trail: frozenset[str] = frozenset()) -> set[str]:
        if name in cache:
            return cache[name]
        if name in trail or name not in classes:
            return set()
        node = classes[name]
        fields = {
            item.target.id
            for item in node.body
            if isinstance(item, ast.AnnAssign)
            and isinstance(item.target, ast.Name)
            and not item.target.id.startswith("_")
        }
        for base in node.bases:
            if isinstance(base, ast.Name):
                fields.update(visit(base.id, trail | {name}))
        cache[name] = fields
        return fields

    return visit(class_name)


def _files_containing_all(paths: list[Path], needles: tuple[str, ...]) -> list[Path]:
    hits: list[Path] = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        if all(needle in text for needle in needles):
            hits.append(path)
    return hits


def validate_prerequisites(project_root: Path | str) -> list[str]:
    root = Path(project_root).resolve()
    errors: list[str] = []
    acquisition = root / "src" / "analysis" / "acquisition"
    models_path = acquisition / "models.py"
    if not models_path.is_file():
        return ["shared acquisition kernel尚未合入"]
    try:
        models_source = models_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return [f"无法读取shared acquisition models: {exc}"]
    for model_name in MODEL_CONTRACTS:
        fields = _class_fields(models_source, model_name)
        if not fields:
            errors.append(f"shared kernel缺少模型{model_name}")
            continue
        missing = sorted(set(REQUIRED_IDENTITY_FIELDS) - fields)
        if missing:
            errors.append(f"{model_name}缺少scope identity字段: {','.join(missing)}")

    source_files = sorted(acquisition.rglob("*.py"))
    selector_hits = _files_containing_all(
        source_files,
        (*REQUIRED_IDENTITY_FIELDS, "latest", "consume"),
    )
    if not selector_hits:
        errors.append("shared kernel缺少包含完整scope identity的latest-consume selector")

    api_path = root / "src" / "analysis" / "api.py"
    cli_path = root / "src" / "analysis" / "cli.py"
    for label, path in (("API", api_path), ("CLI", cli_path)):
        if not path.is_file():
            errors.append(f"缺少{label}入口")
            continue
        text = path.read_text(encoding="utf-8")
        missing = [field for field in REQUIRED_IDENTITY_FIELDS if field not in text]
        if missing:
            errors.append(f"{label}缺少scope identity过滤字段: {','.join(missing)}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查治理接入所需shared kernel合同")
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    args = parser.parse_args(argv)
    try:
        errors = validate_prerequisites(args.project_root)
    except (OSError, SyntaxError, ValueError) as exc:
        errors = [f"prerequisite检查失败: {exc}"]
    if errors:
        print("GOVERNANCE_PREREQUISITES_BLOCKED", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 2
    print("GOVERNANCE_PREREQUISITES_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
