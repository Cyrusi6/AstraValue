from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


PENDING = "pending_manual_validation"
VALIDATED = "validated"
EXPECTED_CHECK_IDS: tuple[str, ...] = (
    "field_evidence_spans",
    "eight_readable_traces",
    "people_roles_and_acting",
    "explicit_cross_company_links",
    "ownership_control_chain",
    "pledge_partial_release",
    "strict_reconstructed_comparison",
    "report_layering",
)


def validate_governance_golden(
    path: Path | str,
    *,
    strict: bool = False,
) -> dict[str, Any]:
    manifest_path = Path(path)
    errors: list[str] = []
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return _summary(0, 0, 0, [f"清单无法读取: {exc}"], strict)
    if not isinstance(data, dict):
        return _summary(0, 0, 0, ["清单顶层必须是object"], strict)
    if data.get("schema_version") != "governance-golden.v1":
        errors.append("schema_version必须为governance-golden.v1")
    if data.get("change_id") != "governance-management-data-foundation-v1":
        errors.append("change_id不匹配")
    if data.get("contains_raw_evidence") is not False:
        errors.append("黄金清单不得包含或声明包含原始公告")
    checks = data.get("checks")
    if not isinstance(checks, list):
        return _summary(0, 0, 0, [*errors, "checks必须为数组"], strict)
    ids = tuple(
        str(item.get("check_id", "")) if isinstance(item, dict) else ""
        for item in checks
    )
    if ids != EXPECTED_CHECK_IDS:
        errors.append("check_id必须精确且按规范顺序排列")
    validated = 0
    pending = 0
    for index, check in enumerate(checks, 1):
        prefix = f"checks[{index}]"
        if not isinstance(check, dict):
            errors.append(f"{prefix}必须为object")
            continue
        if not str(check.get("description_zh", "")).strip():
            errors.append(f"{prefix}缺少description_zh")
        status = check.get("status")
        if status == PENDING:
            pending += 1
            if check.get("reviewer") or check.get("reviewed_at") or check.get("object_refs"):
                errors.append(f"{prefix}待核验项不得伪填签署信息")
            continue
        if status != VALIDATED:
            errors.append(f"{prefix}.status不支持: {status}")
            continue
        validated += 1
        if not str(check.get("reviewer", "")).strip():
            errors.append(f"{prefix}已核验但缺少reviewer")
        reviewed_at = check.get("reviewed_at")
        try:
            parsed = datetime.fromisoformat(str(reviewed_at).replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError
        except (TypeError, ValueError):
            errors.append(f"{prefix}.reviewed_at必须是timezone-aware RFC3339")
        refs = check.get("object_refs")
        if not isinstance(refs, list) or not refs:
            errors.append(f"{prefix}已核验但缺少object_refs")
        elif any(
            not isinstance(ref, dict)
            or not str(ref.get("object_id", "")).strip()
            or not _is_sha256(ref.get("object_hash"))
            or ref.get("result") not in {"passed", "failed"}
            for ref in refs
        ):
            errors.append(
                f"{prefix}.object_refs必须包含object_id、64位hash和passed/failed"
            )
    declared_status = data.get("status")
    expected_status = VALIDATED if checks and validated == len(checks) and pending == 0 else PENDING
    if declared_status != expected_status:
        errors.append(f"顶层status应为{expected_status}")
    return _summary(len(checks), validated, pending, errors, strict)


def _is_sha256(value: Any) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def _summary(
    total: int,
    validated: int,
    pending: int,
    errors: list[str],
    strict: bool,
) -> dict[str, Any]:
    structurally_valid = not errors
    ready = structurally_valid and total > 0 and validated == total and pending == 0
    return {
        "status": VALIDATED if ready else PENDING,
        "structurally_valid": structurally_valid,
        "ready_for_acceptance": ready,
        "strict": strict,
        "check_count": total,
        "validated_count": validated,
        "pending_count": pending,
        "errors": errors,
        "passed": structurally_valid and (ready if strict else True),
    }


__all__ = ["validate_governance_golden"]
