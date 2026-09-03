from __future__ import annotations

import json
from pathlib import Path
from typing import Any


REQUIRED_CATEGORIES = {
    "普通制造",
    "消费",
    "科技",
    "银行",
    "保险",
    "券商",
    "地产",
    "资源周期",
    "公用事业",
    "尚未盈利",
}
PENDING = "pending_manual_validation"
VALIDATED = "validated"
ACCEPTED_CHECK_STATUSES = {"matched", "explained_difference"}


def validate_golden_manifest(path: Path | str, *, strict: bool = False) -> dict[str, Any]:
    manifest_path = Path(path).resolve()
    errors: list[str] = []
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _summary(0, 0, 0, [f"清单无法读取: {exc}"], strict)

    acceptance = data.get("acceptance", {})
    annual_required = _positive_int(acceptance.get("annual_reports"), "annual_reports", errors)
    quarter_required = _positive_int(acceptance.get("quarters"), "quarters", errors)
    facts_required = _positive_int(
        acceptance.get("minimum_manually_checked_facts"),
        "minimum_manually_checked_facts",
        errors,
    )
    samples = data.get("samples")
    if not isinstance(samples, list):
        return _summary(0, 0, 0, [*errors, "samples必须为数组"], strict)

    categories: set[str] = set()
    tickers: set[str] = set()
    validated_count = 0
    pending_count = 0
    for index, sample in enumerate(samples, 1):
        prefix = f"samples[{index}]"
        if not isinstance(sample, dict):
            errors.append(f"{prefix}必须为对象")
            continue
        category = str(sample.get("category", "")).strip()
        ticker = str(sample.get("ticker", "")).strip()
        company = str(sample.get("company", "")).strip()
        status = sample.get("status")
        if not category or not ticker or not company:
            errors.append(f"{prefix}缺少category、ticker或company")
            continue
        if category in categories:
            errors.append(f"黄金样本行业重复: {category}")
        if ticker in tickers:
            errors.append(f"黄金样本股票代码重复: {ticker}")
        categories.add(category)
        tickers.add(ticker)
        if status == PENDING:
            pending_count += 1
            continue
        if status != VALIDATED:
            errors.append(f"{prefix}.status不支持: {status}")
            continue
        validated_count += 1
        validation_file = sample.get("validation_file")
        if not validation_file:
            errors.append(f"{prefix}已标记validated但缺少validation_file")
            continue
        sample_path = _safe_child(manifest_path.parent, validation_file, errors, prefix)
        if sample_path is None:
            continue
        _validate_sample_file(
            sample_path,
            sample,
            annual_required,
            quarter_required,
            facts_required,
            errors,
        )

    missing_categories = sorted(REQUIRED_CATEGORIES - categories)
    if missing_categories:
        errors.append(f"黄金样本缺少行业: {', '.join(missing_categories)}")
    return _summary(len(samples), validated_count, pending_count, errors, strict)


def _validate_sample_file(
    path: Path,
    manifest_sample: dict[str, Any],
    annual_required: int,
    quarter_required: int,
    facts_required: int,
    errors: list[str],
) -> None:
    prefix = f"{manifest_sample['ticker']}验证文件"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"{prefix}无法读取: {exc}")
        return
    for field in ("ticker", "company", "category"):
        if payload.get(field) != manifest_sample.get(field):
            errors.append(f"{prefix}.{field}与清单不一致")
    if not str(payload.get("reviewer", "")).strip() or not str(payload.get("reviewed_at", "")).strip():
        errors.append(f"{prefix}缺少reviewer或reviewed_at")

    annual_periods = _unique_strings(payload.get("annual_periods"))
    quarter_periods = _unique_strings(payload.get("quarter_periods"))
    if len(annual_periods) < annual_required:
        errors.append(f"{prefix}完整年报仅{len(annual_periods)}期，要求至少{annual_required}期")
    if len(quarter_periods) < quarter_required:
        errors.append(f"{prefix}季度仅{len(quarter_periods)}期，要求至少{quarter_required}期")

    fact_checks = payload.get("fact_checks")
    if not isinstance(fact_checks, list):
        errors.append(f"{prefix}.fact_checks必须为数组")
        return
    if len(fact_checks) < facts_required:
        errors.append(f"{prefix}人工核对事实仅{len(fact_checks)}条，要求至少{facts_required}条")
    fact_ids: set[str] = set()
    for index, check in enumerate(fact_checks, 1):
        check_prefix = f"{prefix}.fact_checks[{index}]"
        if not isinstance(check, dict):
            errors.append(f"{check_prefix}必须为对象")
            continue
        required = {"fact_id", "metric_id", "period_end", "status", "primary_source", "crosscheck_source"}
        missing = sorted(key for key in required if not check.get(key))
        if missing:
            errors.append(f"{check_prefix}缺少字段: {', '.join(missing)}")
            continue
        fact_id = str(check["fact_id"])
        if fact_id in fact_ids:
            errors.append(f"{check_prefix}.fact_id重复: {fact_id}")
        fact_ids.add(fact_id)
        if check["status"] not in ACCEPTED_CHECK_STATUSES:
            errors.append(f"{check_prefix}.status必须为matched或explained_difference")
        primary = check["primary_source"]
        crosscheck = check["crosscheck_source"]
        if not isinstance(primary, dict) or not isinstance(crosscheck, dict):
            errors.append(f"{check_prefix}的两个来源必须为对象")
            continue
        primary_upstream = primary.get("upstream_source_id")
        crosscheck_upstream = crosscheck.get("upstream_source_id")
        if not primary_upstream or not crosscheck_upstream:
            errors.append(f"{check_prefix}的两个来源必须记录upstream_source_id")
        elif primary_upstream == crosscheck_upstream:
            errors.append(f"{check_prefix}的复核来源并不独立")


def _positive_int(value: Any, label: str, errors: list[str]) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        errors.append(f"acceptance.{label}必须为正整数")
        return 1
    return value


def _unique_strings(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {str(item).strip() for item in value if str(item).strip()}


def _safe_child(parent: Path, child: Any, errors: list[str], prefix: str) -> Path | None:
    candidate = (parent / str(child)).resolve()
    try:
        candidate.relative_to(parent.resolve())
    except ValueError:
        errors.append(f"{prefix}.validation_file必须位于黄金样本目录内")
        return None
    return candidate


def _summary(total: int, validated: int, pending: int, errors: list[str], strict: bool) -> dict[str, Any]:
    structurally_valid = not errors
    ready = structurally_valid and total > 0 and validated == total and pending == 0
    return {
        "structurally_valid": structurally_valid,
        "ready_for_acceptance": ready,
        "strict": strict,
        "sample_count": total,
        "validated_count": validated,
        "pending_count": pending,
        "errors": errors,
        "passed": structurally_valid and (ready if strict else True),
    }
