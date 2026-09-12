from __future__ import annotations

from datetime import datetime, timezone

from ..models import FactRecord, VerificationStatus


CONSUMABLE_STATUSES = frozenset(
    {
        VerificationStatus.DUAL_SOURCE,
        VerificationStatus.AUTHORITATIVE_SINGLE,
        VerificationStatus.SUPPLIER_DIRECT,
        VerificationStatus.DERIVED,
        VerificationStatus.ESTIMATED,
    }
)


def _utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def fact_consumption_failures(
    fact: FactRecord,
    *,
    as_of: datetime | None = None,
    strict_historical: bool = False,
    materialization_selected_ids: frozenset[str] | None = None,
) -> tuple[str, ...]:
    """Return stable reasons why a fact cannot enter deterministic inputs."""

    failures: list[str] = []
    # Materialization stores all immutable revisions. A caller must first resolve
    # the cutoff-specific manifest, rather than run a metric-only latest selector
    # across competing sources, periods or revisions in the evidence table.
    if (fact.metadata.get("requires_materialization_selection")
            and fact.fact_id not in (materialization_selected_ids or ())):
        failures.append("materialization_selection_required")
    if fact.value is None:
        failures.append("value_missing")
    if fact.verification_status not in CONSUMABLE_STATUSES:
        failures.append("status_not_consumable")

    admission = fact.structured_admission
    if fact.verification_status == VerificationStatus.SUPPLIER_DIRECT:
        if admission is None:
            failures.append("structured_admission_missing")
        elif not admission.programmatic_eligible:
            failures.append("structured_admission_failed")
        if not fact.source_ids:
            failures.append("source_binding_missing")

    if as_of is not None:
        cutoff = _utc(as_of)
        if _utc(fact.as_of) > cutoff:
            failures.append("fact_after_as_of")
        if strict_historical and admission is not None:
            available_at = admission.available_at or admission.retrieved_at
            if _utc(available_at) > cutoff:
                failures.append("not_available_at_as_of")

    return tuple(dict.fromkeys(failures))


def is_fact_consumable(
    fact: FactRecord,
    *,
    as_of: datetime | None = None,
    strict_historical: bool = False,
    materialization_selected_ids: frozenset[str] | None = None,
) -> bool:
    return not fact_consumption_failures(
        fact,
        as_of=as_of,
        strict_historical=strict_historical,
        materialization_selected_ids=materialization_selected_ids,
    )


def latest_consumable_facts(
    facts: list[FactRecord],
    *,
    as_of: datetime | None = None,
    strict_historical: bool = False,
) -> dict[str, FactRecord]:
    latest: dict[str, FactRecord] = {}
    for fact in facts:
        if not is_fact_consumable(
            fact,
            as_of=as_of,
            strict_historical=strict_historical,
        ):
            continue
        current = latest.get(fact.metric_id)
        key = (fact.period_end or fact.as_of.date(), fact.as_of, fact.fact_id)
        current_key = (
            current.period_end or current.as_of.date(),
            current.as_of,
            current.fact_id,
        ) if current is not None else None
        if current is None or key > current_key:
            latest[fact.metric_id] = fact
    return latest


__all__ = [
    "CONSUMABLE_STATUSES",
    "fact_consumption_failures",
    "is_fact_consumable",
    "latest_consumable_facts",
]
