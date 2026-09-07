"""Versioned user selection of announcement bodies; discovery rows stay intact."""
from __future__ import annotations

from dataclasses import replace
import html
import re
from typing import Any
from urllib.parse import unquote, urlsplit


NO_STANDALONE_AUDIT_PDF_V1 = "business_model_no_standalone_audit_pdf_v1"
AUDIT_EXCLUSION_REASON = "excluded_standalone_audit_pdf"
NO_AUDIT_ENGLISH_ANNUAL_V1 = "business_model_no_audit_english_annual_v1"
ENGLISH_ANNUAL_EXCLUSION_REASON = "excluded_english_annual_report"
CONTENT_EXCLUSION_REASONS = frozenset({AUDIT_EXCLUSION_REASON, ENGLISH_ANNUAL_EXCLUSION_REASON})


def _is_pdf(url: str, expected_mime_types) -> bool:
    mime_types = {str(value).lower().split(";", 1)[0].strip() for value in expected_mime_types}
    return "application/pdf" in mime_types and (
        unquote(urlsplit(url).path).lower().endswith(".pdf")
        or mime_types == {"application/pdf"}
    )


def is_standalone_audit_pdf(title: str, url: str, expected_mime_types=()) -> bool:
    if not _is_pdf(url, expected_mime_types):
        return False
    compact = re.sub(r"[\s《》]", "", html.unescape(re.sub(r"<[^>]*>", "", title)))
    # Notices referring to an audit, IPO appendices and full annual reports
    # retain their own identity. The body is never searched to make this choice.
    if re.search(r"关于|公告|决议|议案|通知|说明|回复|答复|年度报告|半年度报告|招股|上市", compact):
        return False
    chinese = r"审计报告(?:及(?:合并)?财务(?:报表|报告))?(?:[（(][^（）()]{0,80}[）)])?$"
    english = r"(?:independentauditor(?:['’]s|s['’]?)?report|auditreport)(?:andfinancialstatements)?(?:\([^()]{0,80}\))?$"
    return bool(re.search(chinese, compact) or re.search(english, compact, re.I))


def is_english_annual_report(title: str, url: str, expected_mime_types=()) -> bool:
    if not _is_pdf(url, expected_mime_types):
        return False
    title = html.unescape(re.sub(r"<[^>]*>", "", title))
    compact = re.sub(r"[\s《》]", "", title).lower()
    if re.search(r"关于|公告|决议|议案|通知|说明|回复|答复|半年度|中期|季度|招股|上市|中文|中英|双语|"
                 r"社会责任|可持续|环境|内控|内部控制|esg|sustainab|environment|"
                 r"socialresponsibility|internalcontrol|interim|semi.?annual|quarter|"
                 r"prospectus|notice|opinion|resolution|chinese|bilingual", compact):
        return False
    return bool(re.search(r"(?:年年度报告|年度报告|年报).*(?:英文|english)", compact)
                or re.search(r"(?:英文|english).*(?:年度报告|年报)", compact)
                or re.search(r"annualreport", compact))


def content_exclusion_reason(title: str, url: str, expected_mime_types, policy: str | None) -> str | None:
    """Shared by discovery and the consumer's frozen-policy revalidation."""
    if policy is None:
        return None
    if policy not in {NO_STANDALONE_AUDIT_PDF_V1, NO_AUDIT_ENGLISH_ANNUAL_V1}:
        raise ValueError("unknown_content_selection_policy")
    if is_standalone_audit_pdf(title, url, expected_mime_types):
        return AUDIT_EXCLUSION_REASON
    if policy == NO_AUDIT_ENGLISH_ANNUAL_V1 and is_english_annual_report(title, url, expected_mime_types):
        return ENGLISH_ANNUAL_EXCLUSION_REASON
    return None


def apply_content_selection(resource: Any, policy: str | None) -> Any:
    """Only lower required_fetch; preserve all original source/proof fields."""
    reason = content_exclusion_reason(resource.title, resource.resource_url,
                                      resource.expected_mime_types, policy)
    if not resource.required_fetch:
        return resource
    if reason is None:
        return resource
    decision = {"policy_id": policy, "action": "metadata_only", "reason_code": reason,
                "original_required_fetch": bool(resource.required_fetch)}
    return replace(resource, required_fetch=False,
                   metadata={**(resource.metadata or {}), "content_selection": decision})
