"""Deterministic accounting for the complete knowledge release gate.

This validates the identity and coverage of recorded evidence. It does not infer
that a financial rule is correct, or turn an agent review into human acceptance.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
import re
from typing import Any


def _strings(value: Any) -> list[str]:
    return value if isinstance(value, list) and all(isinstance(x, str) for x in value) else []


def _rows(value: Any) -> list[Mapping[str, Any]]:
    return value if isinstance(value, list) and all(isinstance(x, Mapping) for x in value) else []


def _reasoned(row: Mapping[str, Any]) -> bool:
    return bool(_strings(row.get("reasons"))) and all(x.strip() for x in row["reasons"])


def prepare_evidence(
    bundle_id: str,
    methods: list[dict[str, Any]],
    frozen_reviews: list[dict[str, Any]],
    acceptance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Combine frozen content reviews with separately recorded trial acceptance.

    External records cannot replace source/case reviews. Old records keep their
    old bundle identity so the release gate can reject their reuse.
    """
    acceptance = acceptance or {}
    identities = {row["method_id"]: row["content_sha256"] for row in methods}
    result: dict[str, Any] = {
        "bundle_id": acceptance.get("bundle_id", bundle_id),
        "agent_samples": deepcopy(acceptance.get("agent_samples", [])),
        "human_review": deepcopy(acceptance.get("human_review", {})),
    }
    # A recorded acceptance may pin the complete candidate payload in
    # addition to its bundle id.  Preserve that pin for the release gate; it
    # must be checked against the bundle being inspected instead of relying on
    # a reusable id alone.
    if "candidate_content_sha256" in acceptance:
        result["candidate_content_sha256"] = acceptance["candidate_content_sha256"]
    for kind, key in [("source", "source_checks"), ("case", "case_checks")]:
        result[key] = [
            deepcopy(row)
            for row in frozen_reviews
            if row.get("kind") == kind
            and row.get("method_id") in identities
            and row.get("content_sha256") == identities[row["method_id"]]
        ]
    return result


def evaluate_release(
    coverage: Mapping[str, Any],
    evidence: Mapping[str, Any],
    expected_question_ids: Sequence[str],
) -> dict[str, Any]:
    """Check a selected candidate version without making it the default.

    ``coverage`` carries bundle_id, ready_question_ids and immutable method
    identities. ``evidence`` carries source/case checks, eight-step agent samples
    and a separately recorded human sample review, all for the same version.
    The expected question set is supplied by the authoritative question registry.
    """
    expected = list(expected_question_ids)
    if not expected or len(expected) != len(set(expected)) or not all(
        isinstance(q, str) and re.fullmatch(r"ES\d{2}\.Q\d{2}", q) for q in expected
    ):
        raise ValueError("Expected question identifiers must be nonempty, valid and unique")
    expected_set = set(expected)
    steps = {q.split(".")[0] for q in expected}
    ready = _strings(coverage.get("ready_question_ids"))
    ready_set = set(ready)
    bundle_id = coverage.get("bundle_id")
    methods = _rows(coverage.get("methods"))
    method_ids = [m.get("method_id") for m in methods]
    identities = {
        m["method_id"]: m["content_sha256"]
        for m in methods
        if isinstance(m.get("method_id"), str)
        and m["method_id"].strip()
        and isinstance(m.get("content_sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", m["content_sha256"])
    }
    valid_methods = bool(identities) and len(identities) == len(methods) == len(set(method_ids))

    recorded_content_sha256 = evidence.get("candidate_content_sha256")
    candidate_content_identity = (
        "candidate_content_sha256" not in evidence
        or (
            isinstance(coverage.get("content_sha256"), str)
            and re.fullmatch(r"[0-9a-f]{64}", coverage["content_sha256"])
            and recorded_content_sha256 == coverage["content_sha256"]
        )
    )

    def method_checks(kind: str) -> bool:
        rows = _rows(evidence.get(kind))
        if not valid_methods or not rows:
            return False
        accepted: set[str] = set()
        for row in rows:
            method_id = row.get("method_id")
            if (
                method_id not in identities
                or row.get("content_sha256") != identities[method_id]
                or row.get("outcome") != "passed"
                or not _reasoned(row)
                or row.get("unresolved_issues", [])
            ):
                return False
            accepted.add(method_id)
        return accepted == set(identities)

    sample_steps: set[str] = set()
    for row in _rows(evidence.get("agent_samples")):
        row_methods = _strings(row.get("method_ids"))
        if (
            row.get("bundle_id") == bundle_id
            and row.get("outcome") == "passed"
            and row.get("step_id") in steps
            and row_methods
            and set(row_methods) <= set(identities)
            and _reasoned(row)
            and not row.get("unresolved_issues", [])
        ):
            sample_steps.add(row["step_id"])
    human = evidence.get("human_review")
    human = human if isinstance(human, Mapping) else {}
    human_ok = (
        human.get("bundle_id") == bundle_id
        and human.get("reviewer_kind") == "human"
        and isinstance(human.get("reviewer"), str)
        and bool(human["reviewer"].strip())
        and human.get("outcome") == "passed"
        and steps <= set(_strings(human.get("sampled_steps")))
        and human.get("important_issues") == []
        and _reasoned(human)
    )
    checks = {
        "bundle_identity": isinstance(bundle_id, str) and bool(bundle_id) and evidence.get("bundle_id") == bundle_id,
        "candidate_content_identity": candidate_content_identity,
        "question_coverage": ready_set == expected_set and len(ready) == len(ready_set),
        "method_identities": valid_methods,
        "source_checks": method_checks("source_checks"),
        "case_checks": method_checks("case_checks"),
        "agent_samples": sample_steps == steps,
        "human_review": bool(human_ok),
    }
    return {
        "passed": all(checks.values()),
        "bundle_id": bundle_id,
        "ready": len(ready_set & expected_set),
        "total": len(expected),
        "missing_question_ids": [q for q in expected if q not in ready_set],
        "unexpected_question_ids": sorted(ready_set - expected_set),
        "checks": checks,
        "failed_checks": [key for key, passed in checks.items() if not passed],
    }
