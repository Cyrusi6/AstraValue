from __future__ import annotations

from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def compact(value: str) -> str:
    return re.sub(r"\s+", "", value)


class EvidenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class Citation(EvidenceModel):
    snapshot_id: str = Field(min_length=1)
    derived_artifact_id: str = Field(min_length=1)
    page_number: int | None = Field(ge=1)
    section: str = Field(min_length=1)
    quote: str = Field(min_length=8)


class FactInput(EvidenceModel):
    record_id: str = Field(min_length=1)
    company_id: str = Field(pattern=r"^\d{6}$")
    subject: str = Field(min_length=1)
    subject_role: Literal["company", "subsidiary", "investee", "third_party"]
    period: str = Field(min_length=1)
    metric: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    dimensions: dict[str, str]
    basis: str = Field(min_length=1)
    event_stage: Literal["reported", "planned", "approved", "in_progress", "completed", "company_claim", "reclassification"]
    value_type: Literal["decimal", "text"]
    value: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    question_ids: tuple[str, ...] = Field(min_length=1)
    citations: tuple[Citation, ...] = Field(min_length=1)
    revision: str = Field(default="original", min_length=1)

    @field_validator("question_ids")
    @classmethod
    def questions(cls, values):
        if any(not re.fullmatch(r"Q(?:0[1-9]|10)", v) for v in values):
            raise ValueError("unknown_business_question")
        return tuple(sorted(set(values)))

    def identity(self) -> dict:
        return self.model_dump(exclude={"record_id", "value", "question_ids", "citations", "revision"})

    def fact(self) -> dict:
        value = self.value
        if self.value_type == "decimal":
            # Units/scales are deliberately not inferred or converted.
            if not re.fullmatch(r"-?\d+(?:\.\d+)?", value):
                raise ValueError("decimal_requires_explicit_unit_and_unformatted_value")
            try:
                value = format(Decimal(value), "f")
            except InvalidOperation as exc:
                raise ValueError("invalid_decimal") from exc
            if "." in value:
                value = value.rstrip("0").rstrip(".")
            if Decimal(value) == 0:
                value = "0"
        identity = self.identity()
        key = digest(identity)
        payload = {**identity, "value": value, "fact_key": key, "revision": self.revision,
                   "review_status": "ai_reviewed", "human_review": False}
        return {**payload, "fact_id": "fact-" + digest(payload)}


class CorrectionInput(EvidenceModel):
    previous_record_id: str = Field(min_length=1)
    replacement_record_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    citation: Citation


class ReviewBatch(EvidenceModel):
    schema_version: Literal["1.0.0"]
    reviewer: str = Field(min_length=1)
    records: tuple[FactInput, ...] = Field(min_length=1)
    corrections: tuple[CorrectionInput, ...] = ()
