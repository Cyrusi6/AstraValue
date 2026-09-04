from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from uuid import uuid4

from .canonical import canonical_sha256
from .models import (
    DecisionValue,
    ExtractorKind,
    GovernancePerson,
    PersonAlias,
    PersonLinkCandidate,
    PersonLinkDecision,
)


class IdentityResolutionError(ValueError):
    """Raised when identity evidence cannot be resolved without guessing."""


def _aware_utc(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise IdentityResolutionError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _clean(value: str, *, field_name: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise IdentityResolutionError(f"{field_name} must be non-empty")
    return cleaned


def normalize_person_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = re.sub(r"[\s·•・._-]+", "", normalized)
    if not normalized:
        raise ValueError("person name cannot normalize to an empty value")
    return normalized


def deterministic_local_key(company_id: str, stable_evidence_key: str) -> str:
    company_id = _clean(company_id, field_name="company_id")
    stable_evidence_key = _clean(
        stable_evidence_key, field_name="stable_evidence_key"
    )
    digest = canonical_sha256(
        {
            "company_id": company_id,
            "stable_evidence_key": stable_evidence_key,
        },
        schema_name="governance-person-local-key",
        schema_version="1",
    )
    return f"det-{digest[:24]}"


def _hash(schema_name: str, payload: dict[str, object]) -> str:
    return canonical_sha256(
        payload,
        schema_name=schema_name,
        schema_version="1.0.0",
    )


class CompanyPersonResolver:
    """Resolve identities only inside one company namespace.

    Cross-company similarity is intentionally not indexed here. A caller must
    create a PersonLinkCandidate and append a PersonLinkDecision instead.
    """

    def __init__(self, id_factory: Callable[[], str] | None = None) -> None:
        self._id_factory = id_factory or (lambda: uuid4().hex)
        self._people: dict[str, GovernancePerson] = {}
        self._evidence_keys: dict[tuple[str, str], str] = {}
        self._aliases: dict[tuple[str, str], set[str]] = {}
        self._alias_available_at: dict[tuple[str, str, str], datetime] = {}

    def resolve(
        self,
        *,
        company_id: str,
        canonical_name: str,
        source_claim_ids: tuple[str, ...],
        available_at: datetime,
        stable_evidence_key: str | None = None,
    ) -> GovernancePerson:
        company_id = _clean(company_id, field_name="company_id")
        canonical_name = _clean(canonical_name, field_name="canonical_name")
        available_at = _aware_utc(available_at, field_name="available_at")
        if not source_claim_ids:
            raise IdentityResolutionError("source_claim_ids must be non-empty")
        if stable_evidence_key is not None:
            stable_evidence_key = _clean(
                stable_evidence_key, field_name="stable_evidence_key"
            )
            local_key = deterministic_local_key(company_id, stable_evidence_key)
            identity_key = (company_id, stable_evidence_key)
            existing_id = self._evidence_keys.get(identity_key)
            if existing_id is not None:
                existing = self._people[existing_id]
                if normalize_person_name(existing.canonical_name) != normalize_person_name(
                    canonical_name
                ):
                    raise IdentityResolutionError(
                        "the stable evidence key is already bound to a different name; "
                        "append an evidenced alias instead of silently merging"
                    )
                return existing
        else:
            local_key = ""
            for _ in range(32):
                candidate_key = _clean(
                    str(self._id_factory()), field_name="allocated local key"
                )
                if any(char.isspace() for char in candidate_key):
                    raise IdentityResolutionError(
                        "allocated local key must not contain whitespace"
                    )
                candidate_local_key = f"rnd-{candidate_key}"
                candidate_id = f"govp:{company_id}:{candidate_local_key}"
                if candidate_id not in self._people:
                    local_key = candidate_local_key
                    break
            if not local_key:
                raise IdentityResolutionError(
                    "persistent local-key allocator repeatedly returned an existing ID"
                )
            identity_key = None
        person_id = f"govp:{company_id}:{local_key}"
        payload: dict[str, object] = {
            "person_id": person_id,
            "company_id": company_id,
            "stable_local_key": local_key,
            "canonical_name": canonical_name,
            "source_claim_ids": tuple(sorted(source_claim_ids)),
            "available_at": available_at,
        }
        person = GovernancePerson(
            **payload,
            canonical_hash=_hash("governance-person", payload),
        )
        self._people[person_id] = person
        self._aliases.setdefault(
            (company_id, normalize_person_name(canonical_name)), set()
        ).add(person_id)
        self._alias_available_at[
            (company_id, normalize_person_name(canonical_name), person_id)
        ] = available_at
        if identity_key is not None:
            self._evidence_keys[identity_key] = person_id
        return person

    def candidates_for_alias(
        self,
        *,
        company_id: str,
        alias: str,
        known_at: datetime | None = None,
    ) -> tuple[GovernancePerson, ...]:
        company_id = _clean(company_id, field_name="company_id")
        normalized_alias = normalize_person_name(alias)
        cutoff = (
            _aware_utc(known_at, field_name="known_at")
            if known_at is not None
            else None
        )
        ids = self._aliases.get((company_id, normalized_alias), set())
        return tuple(
            self._people[item]
            for item in sorted(ids)
            if cutoff is None
            or self._alias_available_at[(company_id, normalized_alias, item)] <= cutoff
        )

    def register_alias(self, alias: PersonAlias) -> None:
        if alias.person_id not in self._people:
            raise KeyError(alias.person_id)
        person = self._people[alias.person_id]
        if alias.company_id != person.company_id:
            raise ValueError("alias cannot cross a company namespace")
        self._aliases.setdefault(
            (alias.company_id, normalize_person_name(alias.alias)), set()
        ).add(alias.person_id)
        self._alias_available_at[
            (alias.company_id, normalize_person_name(alias.alias), alias.person_id)
        ] = alias.available_at


def build_person_alias(
    *,
    company_id: str,
    person_id: str,
    alias: str,
    alias_type: str,
    raw_snapshot_id: str,
    evidence_span_id: str,
    extractor_version: str,
    available_at: datetime,
) -> PersonAlias:
    company_id = _clean(company_id, field_name="company_id")
    person_id = _clean(person_id, field_name="person_id")
    alias = _clean(alias, field_name="alias")
    alias_type = _clean(alias_type, field_name="alias_type")
    raw_snapshot_id = _clean(raw_snapshot_id, field_name="raw_snapshot_id")
    evidence_span_id = _clean(evidence_span_id, field_name="evidence_span_id")
    extractor_version = _clean(extractor_version, field_name="extractor_version")
    available_at = _aware_utc(available_at, field_name="available_at")
    semantic: dict[str, object] = {
        "company_id": company_id,
        "person_id": person_id,
        "alias": alias,
        "alias_type": alias_type,
        "raw_snapshot_id": raw_snapshot_id,
        "evidence_span_id": evidence_span_id,
        "extractor_version": extractor_version,
        "available_at": available_at,
    }
    digest = _hash("governance-person-alias", semantic)
    return PersonAlias(
        alias_id=f"govalias:{digest}",
        **semantic,
        canonical_hash=digest,
    )


def build_person_link_candidate(
    *,
    company_ids: tuple[str, ...],
    person_ids: tuple[str, ...],
    basis: str,
    producer: str,
    evidence_span_ids: tuple[str, ...],
    available_at: datetime,
    extractor_kind: ExtractorKind = ExtractorKind.DETERMINISTIC,
) -> PersonLinkCandidate:
    if len(company_ids) != len(person_ids):
        raise IdentityResolutionError("company_ids and person_ids must be aligned")
    pairs = tuple(
        sorted(
            (
                _clean(company_id, field_name="company_id"),
                _clean(person_id, field_name="person_id"),
            )
            for company_id, person_id in zip(company_ids, person_ids, strict=True)
        )
    )
    company_ids = tuple(company_id for company_id, _ in pairs)
    person_ids = tuple(person_id for _, person_id in pairs)
    basis = _clean(basis, field_name="basis")
    producer = _clean(producer, field_name="producer")
    available_at = _aware_utc(available_at, field_name="available_at")
    if not evidence_span_ids or any(
        not item.startswith("govspan:") for item in evidence_span_ids
    ):
        raise IdentityResolutionError(
            "link candidate evidence_span_ids must use the govspan namespace"
        )
    semantic: dict[str, object] = {
        "company_ids": company_ids,
        "person_ids": person_ids,
        "basis": basis,
        "producer": producer,
        "extractor_kind": extractor_kind,
        "evidence_span_ids": tuple(sorted(evidence_span_ids)),
        "available_at": available_at,
    }
    digest = _hash("governance-person-link-candidate", semantic)
    return PersonLinkCandidate(
        person_link_candidate_id=f"govlinkcandidate:{digest}",
        **semantic,
        canonical_hash=digest,
    )


def build_person_link_decision(
    *,
    candidate: PersonLinkCandidate,
    decision: DecisionValue,
    decision_source: str,
    producer: str,
    rationale: str,
    decided_at: datetime,
    available_at: datetime,
    supersedes_decision_id: str | None = None,
) -> PersonLinkDecision:
    decided_at = _aware_utc(decided_at, field_name="decided_at")
    available_at = _aware_utc(available_at, field_name="available_at")
    decision_source = _clean(decision_source, field_name="decision_source")
    producer = _clean(producer, field_name="producer")
    rationale = _clean(rationale, field_name="rationale")
    if decided_at < candidate.available_at or available_at < candidate.available_at:
        raise IdentityResolutionError(
            "link decision cannot precede its candidate evidence"
        )
    if supersedes_decision_id is not None and not supersedes_decision_id.startswith(
        "govlinkdecision:"
    ):
        raise IdentityResolutionError(
            "supersedes_decision_id must use the govlinkdecision namespace"
        )
    semantic: dict[str, object] = {
        "person_link_candidate_id": candidate.person_link_candidate_id,
        "company_ids": candidate.company_ids,
        "person_ids": candidate.person_ids,
        "decision": decision,
        "decision_source": decision_source,
        "producer": producer,
        "rationale": rationale,
        "evidence_span_ids": candidate.evidence_span_ids,
        "decided_at": decided_at,
        "available_at": available_at,
        "supersedes_decision_id": supersedes_decision_id,
    }
    digest = _hash("governance-person-link-decision", semantic)
    return PersonLinkDecision(
        person_link_decision_id=f"govlinkdecision:{digest}",
        **semantic,
        canonical_hash=digest,
    )


def select_visible_link_decisions(
    decisions: Iterable[PersonLinkDecision],
    *,
    known_at: datetime,
    candidates: Iterable[PersonLinkCandidate] | None = None,
) -> tuple[PersonLinkDecision, ...]:
    """Return the latest visible approved decision for each candidate.

    A later rejected decision suppresses an earlier approval only after the
    rejection itself is visible at ``known_at``.
    """

    cutoff = _aware_utc(known_at, field_name="known_at")
    # Visibility is deliberately the first operation. Hidden future decisions are
    # not inspected even to resolve a chain.
    visible = [item for item in decisions if item.available_at <= cutoff]
    by_id: dict[str, PersonLinkDecision] = {}
    for item in visible:
        existing = by_id.get(item.person_link_decision_id)
        if existing is not None and existing != item:
            raise IdentityResolutionError("one decision ID has multiple payloads")
        by_id[item.person_link_decision_id] = item

    candidate_index = None
    if candidates is not None:
        candidate_index = {}
        for item in candidates:
            if item.available_at > cutoff:
                continue
            existing = candidate_index.get(item.person_link_candidate_id)
            if existing is not None and existing != item:
                raise IdentityResolutionError("one candidate ID has multiple payloads")
            candidate_index[item.person_link_candidate_id] = item
    for item in visible:
        if candidate_index is not None:
            candidate = candidate_index.get(item.person_link_candidate_id)
            if candidate is None:
                raise IdentityResolutionError("link decision references an unknown candidate")
            if (
                candidate.company_ids != item.company_ids
                or candidate.person_ids != item.person_ids
            ):
                raise IdentityResolutionError(
                    "link decision identities differ from its candidate"
                )
        target_id = item.supersedes_decision_id
        if target_id is None:
            continue
        target = by_id.get(target_id)
        if target is None:
            raise IdentityResolutionError(
                "visible link decision has a dangling supersedes reference"
            )
        if (
            target.person_link_candidate_id != item.person_link_candidate_id
            or target.company_ids != item.company_ids
            or target.person_ids != item.person_ids
        ):
            raise IdentityResolutionError(
                "link decision may supersede only the same candidate and identities"
            )
        if target.available_at > item.available_at or target.decided_at > item.decided_at:
            raise IdentityResolutionError("link decision chain is not append-only")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(decision_id: str) -> None:
        if decision_id in visiting:
            raise IdentityResolutionError("link decision supersedes chain contains a cycle")
        if decision_id in visited:
            return
        visiting.add(decision_id)
        parent = by_id[decision_id].supersedes_decision_id
        if parent is not None:
            visit(parent)
        visiting.remove(decision_id)
        visited.add(decision_id)

    for decision_id in sorted(by_id):
        visit(decision_id)

    superseded_ids = {
        item.supersedes_decision_id
        for item in visible
        if item.supersedes_decision_id is not None
    }
    heads_by_candidate: dict[str, list[PersonLinkDecision]] = {}
    for item in visible:
        if item.person_link_decision_id not in superseded_ids:
            heads_by_candidate.setdefault(item.person_link_candidate_id, []).append(item)

    selected: list[PersonLinkDecision] = []
    for candidate_id, heads in sorted(heads_by_candidate.items()):
        if len(heads) != 1:
            raise IdentityResolutionError(
                f"candidate {candidate_id} has parallel visible decision heads"
            )
        if heads[0].decision == DecisionValue.APPROVED:
            selected.append(heads[0])
    return tuple(selected)


def approved_link_groups(
    decisions: Iterable[PersonLinkDecision],
    *,
    known_at: datetime,
) -> tuple[tuple[str, ...], ...]:
    approved = select_visible_link_decisions(decisions, known_at=known_at)
    parent: dict[str, str] = {}

    def find(item: str) -> str:
        parent.setdefault(item, item)
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            if left_root < right_root:
                parent[right_root] = left_root
            else:
                parent[left_root] = right_root

    for decision in approved:
        first, *rest = decision.person_ids
        find(first)
        for person_id in rest:
            union(first, person_id)
    connected: dict[str, set[str]] = {}
    for person_id in sorted(parent):
        connected.setdefault(find(person_id), set()).add(person_id)
    return tuple(
        sorted(tuple(sorted(group)) for group in connected.values() if len(group) >= 2)
    )


__all__ = [
    "CompanyPersonResolver",
    "IdentityResolutionError",
    "approved_link_groups",
    "build_person_alias",
    "build_person_link_candidate",
    "build_person_link_decision",
    "deterministic_local_key",
    "normalize_person_name",
    "select_visible_link_decisions",
]
