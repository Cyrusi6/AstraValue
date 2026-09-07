"""Versioned user selection of announcement bodies; discovery rows stay intact."""
from __future__ import annotations

from dataclasses import replace
import html
import re
from typing import Any
from urllib.parse import unquote, urlsplit


NO_STANDALONE_AUDIT_PDF_V1 = "business_model_no_standalone_audit_pdf_v1"
AUDIT_EXCLUSION_REASON = "excluded_standalone_audit_pdf"


def is_standalone_audit_pdf(title: str, url: str, expected_mime_types=()) -> bool:
    mime_types = {str(value).lower().split(";", 1)[0].strip() for value in expected_mime_types}
    is_pdf = "application/pdf" in mime_types and (
        unquote(urlsplit(url).path).lower().endswith(".pdf")
        or mime_types == {"application/pdf"}
    )
    if not is_pdf:
        return False
    compact = re.sub(r"[\s《》]", "", html.unescape(re.sub(r"<[^>]*>", "", title)))
    # Notices referring to an audit, IPO appendices and full annual reports
    # retain their own identity. The body is never searched to make this choice.
    if re.search(r"关于|公告|决议|议案|通知|说明|回复|答复|年度报告|半年度报告|招股|上市", compact):
        return False
    chinese = r"审计报告(?:及(?:合并)?财务(?:报表|报告))?(?:[（(][^（）()]{0,80}[）)])?$"
    english = r"(?:independentauditor(?:['’]s|s['’]?)?report|auditreport)(?:andfinancialstatements)?(?:\([^()]{0,80}\))?$"
    return bool(re.search(chinese, compact) or re.search(english, compact, re.I))


def apply_content_selection(resource: Any, policy: str | None) -> Any:
    """Only lower required_fetch; preserve all original source/proof fields."""
    if policy is None:
        return resource
    if policy != NO_STANDALONE_AUDIT_PDF_V1:
        raise ValueError("unknown_content_selection_policy")
    if not resource.required_fetch:
        return resource
    if not is_standalone_audit_pdf(resource.title, resource.resource_url,
                                   resource.expected_mime_types):
        return resource
    decision = {"policy_id": policy, "action": "metadata_only", "reason_code": AUDIT_EXCLUSION_REASON,
                "original_required_fetch": bool(resource.required_fetch)}
    return replace(resource, required_fetch=False,
                   metadata={**(resource.metadata or {}), "content_selection": decision})
